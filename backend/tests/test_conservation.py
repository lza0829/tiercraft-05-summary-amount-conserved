from __future__ import annotations

import sqlite3

import pytest

from app.db import connect
from app.services import pipeline


def _setup_layers(client, domain_id=1):
    client.post("/api/jobs/paste-to-detail", json={"domain_id": domain_id})
    return client.post("/api/jobs/detail-to-summary", json={"domain_id": domain_id}).json()


def test_chinese_amount_conserved_and_report_persisted(client):
    job = _setup_layers(client)
    assert job["status"] == "成功"
    assert job["conserved"] is True
    assert job["restored"] is False
    assert abs(job["amount_detail_total"] - job["amount_summary_total"]) <= pipeline.SUMMARY_AMOUNT_TOLERANCE
    assert abs(job["amount_drift"]) <= pipeline.SUMMARY_AMOUNT_TOLERANCE
    assert job["amount_detail_total"] == pytest.approx(400.5)
    assert job["amount_summary_total"] == pytest.approx(400.5)

    # 报告刷新后仍在（从库再读）
    jobs = client.get("/api/jobs?domain_id=1").json()
    assert jobs[0]["id"] == job["id"]
    assert jobs[0]["conservation"]["成败"] == "成功" if "成败" in jobs[0]["conservation"] else True
    assert jobs[0]["amount_detail_total"] == pytest.approx(job["amount_detail_total"])

    state = client.get("/api/layers/summary/state?domain_id=1").json()
    assert state["当前守恒"] is True
    assert abs(state["当前漂移"]) <= pipeline.SUMMARY_AMOUNT_TOLERANCE
    assert state["汇总层"]["行数"] == 4
    # 报告数字与汇总层聚合结果一致
    assert state["汇总层"]["金额合计"] == pytest.approx(job["amount_summary_total"])


def test_drift_fails_task_and_restores_previous_snapshot(client):
    # 先建立一份旧汇总（任务开始前状态）
    _setup_layers(client)
    before = pipeline.layer_rows("summary", 1)
    assert len(before) == 4

    # 注入漂移：模拟聚合丢失一个键（半截/错误聚合），汇总会与明细合计不一致
    from app.services import pipeline as p

    orig = p.aggregate_detail

    def lossy(detail_rows):
        buckets, keys = orig(detail_rows)
        buckets.pop(next(iter(buckets)))
        return buckets, keys

    p.aggregate_detail = lossy
    try:
        job = client.post("/api/jobs/detail-to-summary", json={"domain_id": 1}).json()
    finally:
        p.aggregate_detail = orig

    assert job["status"] == "失败"
    assert job["conserved"] is False
    assert job["restored"] is True
    assert "金额不守恒" in job["message"]
    assert "已恢复到任务开始前快照" in job["message"]
    assert job["conservation"]["恢复"]["恢复成功"] is True

    # 汇总层必须完全回到任务前快照：同样的行、同样的键、同样的金额
    after = pipeline.layer_rows("summary", 1)
    assert after == before
    state = client.get("/api/layers/summary/state?domain_id=1").json()
    assert state["汇总层"]["行数"] == 4
    assert state["汇总层"]["金额合计"] == pytest.approx(400.5)
    # 报告主字段 = 守恒校验时的实际值（失败依据，漂移可见）
    assert job["amount_drift"] != 0
    assert job["amount_drift"] <= -100  # 少了一个键的金额
    # 任务结束时汇总层实际值 = 恢复后的快照态（与列表 / 对账同源）
    assert job["conservation"]["任务结束时汇总合计"] == pytest.approx(state["汇总层"]["金额合计"])
    assert job["conservation"]["任务结束时汇总行数"] == state["汇总层"]["行数"]
    assert job["conservation"]["任务结束时漂移"] == pytest.approx(0.0, abs=1e-6)
    # 本次尝试写入的残缺值单独留档
    assert job["conservation"]["本次尝试汇总合计"] < job["amount_detail_total"]
    assert job["conservation"]["本次尝试漂移"] < 0

    # 失败的守恒报告仍在（刷新持久）
    jobs = client.get("/api/jobs?domain_id=1").json()
    assert jobs[0]["status"] == "失败"
    assert "金额不守恒" in jobs[0]["message"]

    # 汇总层没有半截键 / 新旧混杂键
    keys = {(r["biz_day"], r["store_code"]) for r in after}
    assert keys == {(r["biz_day"], r["store_code"]) for r in before}


def test_failure_before_any_prior_summary_leaves_empty_layer(client):
    # 没有旧汇总时，失败应恢复为空汇总层，而不是残留半截键
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO layer_detail(domain_id, biz_day, store_code, store_name, category, amount, qty, clean_flag)"
            " VALUES (1,'2026-11-10','SX','测试店','饮料',10.0,1,'已清洗')"
        )
        conn.commit()
    finally:
        conn.close()

    # 制造写入异常：汇总键与唯一约束冲突以外的异常，用 monkeypatch 模拟
    from app.services import pipeline as p
    orig = p._validate_conservation

    def boom(detail_total, detail_count, summary, expected_keys):
        raise p.ConservationError("模拟守恒失败", {"漂移": 99.0})

    p._validate_conservation = boom
    try:
        job = client.post("/api/jobs/detail-to-summary", json={"domain_id": 1}).json()
    finally:
        p._validate_conservation = orig

    assert job["status"] == "失败"
    assert job["restored"] is True
    assert pipeline.layer_rows("summary", 1) == []
    assert job["summary_after_rows"] == 0


def test_restore_snapshot_survives_process_restart(client):
    _setup_layers(client)
    snapshot = pipeline.layer_rows("summary", 1)

    # 模拟服务重启：重新初始化数据库（同一 SQLite 文件）
    from app import seed
    seed.init_db()
    assert pipeline.layer_rows("summary", 1) == snapshot


def test_conservation_tolerance_is_documented_and_tiny(client):
    assert pipeline.SUMMARY_AMOUNT_TOLERANCE <= 1e-6
    assert "浮点" in pipeline.TOLERANCE_NOTE

    job = _setup_layers(client)
    assert job["tolerance"] == pipeline.SUMMARY_AMOUNT_TOLERANCE
    state = client.get("/api/layers/summary/state?domain_id=1").json()
    assert state["容差"] == pipeline.SUMMARY_AMOUNT_TOLERANCE
    assert "浮点" in state["容差说明"]


def test_float_rounding_within_tolerance_is_conserved(client):
    conn = connect()
    try:
        conn.execute("DELETE FROM layer_detail WHERE domain_id=1")
        # 0.1 类十进制金额累加会产生二进制浮点误差
        for i in range(1000):
            conn.execute(
                "INSERT INTO layer_detail(domain_id, biz_day, store_code, store_name, category, amount, qty, clean_flag)"
                " VALUES (1,'2026-11-10','S01','城东店','饮料',0.1,1,'已清洗')"
            )
        conn.commit()
    finally:
        conn.close()

    job = client.post("/api/jobs/detail-to-summary", json={"domain_id": 1}).json()
    assert job["status"] == "成功"
    assert job["conserved"] is True
    assert abs(job["amount_drift"]) <= pipeline.SUMMARY_AMOUNT_TOLERANCE


def test_restore_rewrite_path_removes_partial_and_mixed_keys(client):
    """rollback 不足（例如异常发生在事务之外）时，按快照重写也必须回到纯净快照态。"""
    _setup_layers(client)
    before = pipeline.layer_rows("summary", 1)
    snapshot = before

    conn = connect()
    try:
        # 模拟半截 + 新旧混杂：删两个键，改一个键的金额，插一个新键
        conn.execute("DELETE FROM layer_summary WHERE domain_id=1 AND store_code='S02'")
        conn.execute("UPDATE layer_summary SET amount_sum = 999.0 WHERE id = (SELECT MIN(id) FROM layer_summary WHERE domain_id=1)")
        conn.execute(
            "INSERT INTO layer_summary(domain_id, biz_day, store_code, amount_sum, qty_sum, line_count)"
            " VALUES (1,'2026-12-01','S99',77.0,7,1)"
        )
        conn.commit()
        corrupted = pipeline.layer_rows("summary", 1)
        assert len(corrupted) == 3  # 确实处于半截/混杂状态

        info = pipeline._restore_summary(conn, 1, snapshot)
        conn.commit()
    finally:
        conn.close()

    assert info["恢复成功"] is True
    assert info["恢复方式"] == "按快照重写"
    assert pipeline.layer_rows("summary", 1) == before
    assert "S99" not in {r["store_code"] for r in pipeline.layer_rows("summary", 1)}
