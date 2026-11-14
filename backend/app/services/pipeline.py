from __future__ import annotations

import json
import math
import uuid

from app.db import connect, rows_to_dicts


STEP_LABEL = {
    "paste_to_detail": "贴源到明细",
    "detail_to_summary": "明细到汇总",
}

# 汇总金额守恒的绝对容差（元）。明细金额与汇总金额都由 REAL 存储，
# 聚合顺序不同会引入二进制浮点舍入误差；绝对差不超过该值即视为守恒。
SUMMARY_AMOUNT_TOLERANCE = 1e-6
TOLERANCE_NOTE = "汇总金额合计与明细金额合计的绝对差 ≤ 0.000001 视为守恒（二进制浮点舍入误差）"


class ConservationError(Exception):
    """汇总写入完成后，金额/键守恒校验不通过。"""

    def __init__(self, reason: str, metrics: dict):
        super().__init__(reason)
        self.reason = reason
        self.metrics = metrics


def _fmt_amount(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def list_domains() -> list[dict]:
    conn = connect()
    try:
        return rows_to_dicts(conn.execute("SELECT * FROM domains ORDER BY id").fetchall())
    finally:
        conn.close()


def get_domain(domain_id: int) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM domains WHERE id=?", (domain_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def layer_rows(table: str, domain_id: int | None = None) -> list[dict]:
    allowed = {
        "paste": "layer_paste",
        "detail": "layer_detail",
        "summary": "layer_summary",
    }
    if table not in allowed:
        raise ValueError("未知分层")
    sql = f"SELECT * FROM {allowed[table]}"
    args: tuple = ()
    if domain_id is not None:
        sql += " WHERE domain_id=?"
        args = (domain_id,)
    sql += " ORDER BY id"
    conn = connect()
    try:
        return rows_to_dicts(conn.execute(sql, args).fetchall())
    finally:
        conn.close()


def layer_counts(domain_id: int | None = None) -> dict:
    conn = connect()
    try:
        where = " WHERE domain_id=?" if domain_id is not None else ""
        args = (domain_id,) if domain_id is not None else ()
        paste = conn.execute(f"SELECT COUNT(*) AS c FROM layer_paste{where}", args).fetchone()["c"]
        detail = conn.execute(f"SELECT COUNT(*) AS c FROM layer_detail{where}", args).fetchone()["c"]
        summary = conn.execute(f"SELECT COUNT(*) AS c FROM layer_summary{where}", args).fetchone()["c"]
        return {
            "贴源层": int(paste),
            "明细层": int(detail),
            "汇总层": int(summary),
        }
    finally:
        conn.close()


def _decode_job(row) -> dict:
    d = dict(row)
    d["step_label"] = STEP_LABEL.get(d["step"], d["step"])
    raw = d.get("conservation_json") or "{}"
    try:
        d["conservation"] = json.loads(raw)
    except (TypeError, ValueError):
        d["conservation"] = {}
    d.pop("conservation_json", None)
    d["conserved"] = None if d.get("conserved") is None else bool(d["conserved"])
    d["restored"] = bool(d.get("restored"))
    return d


def list_jobs(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM layer_jobs ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM layer_jobs WHERE domain_id=? ORDER BY id DESC", (domain_id,)
            ).fetchall()
        return [_decode_job(r) for r in rows]
    finally:
        conn.close()


def run_paste_to_detail(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        conn.execute("DELETE FROM layer_detail WHERE domain_id=?", (domain_id,))
        paste = conn.execute(
            "SELECT * FROM layer_paste WHERE domain_id=? ORDER BY id", (domain_id,)
        ).fetchall()
        out = drop = 0
        detail_total = 0.0
        for row in paste:
            store_name = (row["store_name"] or "").strip()
            amount = float(row["amount"])
            if not store_name or amount < 0:
                drop += 1
                continue
            conn.execute(
                """
                INSERT INTO layer_detail(
                  domain_id, biz_day, store_code, store_name, category, amount, qty, clean_flag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    domain_id,
                    row["biz_day"],
                    row["store_code"],
                    store_name,
                    row["category"],
                    amount,
                    int(row["qty"]),
                    "已清洗",
                ),
            )
            detail_total += amount
            out += 1
        cur = conn.execute(
            """
            INSERT INTO layer_jobs(domain_id, step, status, message, in_count, out_count, drop_count,
                                   amount_detail_total, amount_summary_total, amount_drift,
                                   conserved, restored, conservation_json)
            VALUES (?, 'paste_to_detail', '成功', ?, ?, ?, ?, ?, 0, 0, NULL, 0, ?)
            """,
            (
                domain_id,
                "清洗完成，剔除门店名为空或金额为负的行",
                len(paste),
                out,
                drop,
                detail_total,
                json.dumps(
                    {
                        "说明": "贴源到明细不参与汇总守恒校验",
                        "明细金额合计": detail_total,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        conn.commit()
        job_id = int(cur.lastrowid)
        return _decode_job(
            conn.execute("SELECT * FROM layer_jobs WHERE id=?", (job_id,)).fetchone()
        )
    finally:
        conn.close()


def aggregate_detail(detail_rows) -> tuple[dict[tuple, dict], set[tuple]]:
    """明细按 (业务日, 门店编码) 聚合。独立成函数，便于验证调用。"""
    buckets: dict[tuple, dict] = {}
    expected_keys: set[tuple] = set()
    for row in detail_rows:
        key = (row["biz_day"], row["store_code"])
        expected_keys.add(key)
        b = buckets.setdefault(
            key,
            {
                "biz_day": row["biz_day"],
                "store_code": row["store_code"],
                "amount": 0.0,
                "qty": 0,
                "n": 0,
            },
        )
        b["amount"] += float(row["amount"])
        b["qty"] += int(row["qty"])
        b["n"] += 1
    return buckets, expected_keys


def _summary_totals(conn, domain_id: int) -> dict:
    rows = conn.execute(
        "SELECT * FROM layer_summary WHERE domain_id=? ORDER BY id", (domain_id,)
    ).fetchall()
    return {
        "rows": len(rows),
        "amount": math.fsum(float(r["amount_sum"]) for r in rows),
        "line_count": sum(int(r["line_count"]) for r in rows),
        "keys": {(r["biz_day"], r["store_code"]) for r in rows},
        "rows_data": rows,
    }


def _detail_total(conn, domain_id: int) -> float:
    amounts = [
        float(r["amount"])
        for r in conn.execute(
            "SELECT amount FROM layer_detail WHERE domain_id=? ORDER BY id", (domain_id,)
        ).fetchall()
    ]
    return math.fsum(amounts)


def _snapshot_summary(conn, domain_id: int) -> tuple[str, list[dict]]:
    """把任务开始前的汇总层完整写入持久化快照表，并 commit，保证后续失败可恢复。"""
    token = uuid.uuid4().hex
    rows = rows_to_dicts(
        conn.execute(
            "SELECT * FROM layer_summary WHERE domain_id=? ORDER BY id", (domain_id,)
        ).fetchall()
    )
    # 快照是恢复依据，必须独立于后续写入事务提交。
    conn.execute("DELETE FROM layer_summary_snapshots WHERE domain_id=?", (domain_id,))
    for r in rows:
        conn.execute(
            """
            INSERT INTO layer_summary_snapshots(
              domain_id, token, row_id, biz_day, store_code, amount_sum, qty_sum, line_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                token,
                int(r["id"]),
                r["biz_day"],
                r["store_code"],
                float(r["amount_sum"]),
                int(r["qty_sum"]),
                int(r["line_count"]),
            ),
        )
    conn.commit()
    return token, rows


def _drop_snapshot(conn, domain_id: int, token: str) -> None:
    conn.execute(
        "DELETE FROM layer_summary_snapshots WHERE domain_id=? AND token=?", (domain_id, token)
    )


def _restore_summary(conn, domain_id: int, snapshot_rows: list[dict]) -> dict:
    """把该主题域汇总层确保为快照内容；返回恢复后的实际状态。

    写入与校验在同一事务内完成，失败后 rollback 通常已把汇总层带回快照。
    因此先核对当前状态：已经与快照一致就不重复写，避免无谓的二次写入；
    不一致（例如异常发生在事务之外）才按快照整体重写。
    """
    before_keys = {(r["biz_day"], r["store_code"]) for r in snapshot_rows}
    before_amount = math.fsum(float(r["amount_sum"]) for r in snapshot_rows)
    before_lines = sum(int(r["line_count"]) for r in snapshot_rows)
    before_ids = [int(r["id"]) for r in snapshot_rows]

    current = _summary_totals(conn, domain_id)
    already_snapshot = (
        current["keys"] == before_keys
        and abs(current["amount"] - before_amount) <= SUMMARY_AMOUNT_TOLERANCE
        and current["line_count"] == before_lines
        and [int(r["id"]) for r in current["rows_data"]] == before_ids
    )
    if not already_snapshot:
        conn.execute("DELETE FROM layer_summary WHERE domain_id=?", (domain_id,))
        for r in snapshot_rows:
            conn.execute(
                """
                INSERT INTO layer_summary(
                  id, domain_id, biz_day, store_code, amount_sum, qty_sum, line_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(r["id"]),
                    domain_id,
                    r["biz_day"],
                    r["store_code"],
                    float(r["amount_sum"]),
                    int(r["qty_sum"]),
                    int(r["line_count"]),
                ),
            )
    after = _summary_totals(conn, domain_id)
    same_keys = after["keys"] == before_keys
    same_amount = abs(after["amount"] - before_amount) <= SUMMARY_AMOUNT_TOLERANCE
    same_lines = after["line_count"] == before_lines
    same_ids = [int(r["id"]) for r in after["rows_data"]] == before_ids
    verified = same_keys and same_amount and same_lines and same_ids
    return {
        "恢复成功": verified,
        "恢复方式": "已回滚至快照" if already_snapshot else "按快照重写",
        "恢复行数": after["rows"],
        "恢复金额合计": after["amount"],
        "恢复明细行数合计": after["line_count"],
        "键与原快照一致": same_keys,
        "金额与原快照一致": same_amount,
        "行数与原快照一致": same_lines,
    }


def _validate_conservation(
    detail_total: float,
    detail_count: int,
    summary: dict,
    expected_keys: set[tuple],
) -> None:
    drift = summary["amount"] - detail_total
    problems: list[str] = []

    if abs(drift) > SUMMARY_AMOUNT_TOLERANCE:
        problems.append(
            "金额不守恒：明细合计 "
            f"{_fmt_amount(detail_total)}，汇总合计 {_fmt_amount(summary['amount'])}，"
            f"漂移 {_fmt_amount(drift)}，超出容差 {SUMMARY_AMOUNT_TOLERANCE:g}"
        )
    if summary["line_count"] != detail_count:
        problems.append(
            f"明细行数不守恒：明细 {detail_count} 行，汇总行数合计 {summary['line_count']} 行"
        )
    missing = expected_keys - summary["keys"]
    extra = summary["keys"] - expected_keys
    if missing:
        problems.append(
            "汇总键缺失："
            + "、".join(f"{d}/{s}" for d, s in sorted(missing)[:5])
            + ("…" if len(missing) > 5 else "")
        )
    if extra:
        problems.append(
            "汇总键多余："
            + "、".join(f"{d}/{s}" for d, s in sorted(extra)[:5])
            + ("…" if len(extra) > 5 else "")
        )
    if problems:
        raise ConservationError("；".join(problems), {"漂移": drift})


def run_detail_to_summary(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    snapshot_rows: list[dict] = []
    snapshot_token = ""
    final_amount = 0.0
    final_rows = 0
    try:
        detail = conn.execute(
            "SELECT * FROM layer_detail WHERE domain_id=? ORDER BY id", (domain_id,)
        ).fetchall()
        detail_total = math.fsum(float(r["amount"]) for r in detail)
        buckets, expected_keys = aggregate_detail(detail)
        snapshot_token, snapshot_rows = _snapshot_summary(conn, domain_id)
        before = _summary_totals(conn, domain_id)

        attempted_amount = 0.0
        try:
            # ---- 写入阶段：先清空该主题域汇总层，再按键写入新汇总 ----
            conn.execute("DELETE FROM layer_summary WHERE domain_id=?", (domain_id,))
            for b in buckets.values():
                conn.execute(
                    """
                    INSERT INTO layer_summary(
                      domain_id, biz_day, store_code, amount_sum, qty_sum, line_count
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (domain_id, b["biz_day"], b["store_code"], b["amount"], b["qty"], b["n"]),
                )
                attempted_amount += float(b["amount"])

            # ---- 守恒校验：读回库内实际汇总，和本次参与聚合的明细合计比对 ----
            summary_now = _summary_totals(conn, domain_id)
            _validate_conservation(detail_total, len(detail), summary_now, expected_keys)

            drift = summary_now["amount"] - detail_total
            conservation = {
                "明细合计": detail_total,
                "汇总合计": summary_now["amount"],
                "漂移": drift,
                "容差": SUMMARY_AMOUNT_TOLERANCE,
                "守恒": True,
                "成败": "成功",
                "明细行数": len(detail),
                "汇总行数合计": summary_now["line_count"],
                "汇总键数": len(summary_now["keys"]),
                "容差说明": TOLERANCE_NOTE,
            }
            message = (
                "按业务日+门店编码汇总；守恒校验通过："
                f"明细合计 {_fmt_amount(detail_total)}，"
                f"汇总合计 {_fmt_amount(summary_now['amount'])}，"
                f"漂移 {_fmt_amount(drift)}（容差 {SUMMARY_AMOUNT_TOLERANCE:g}）"
            )
            cur = conn.execute(
                """
                INSERT INTO layer_jobs(
                  domain_id, step, status, message, in_count, out_count, drop_count,
                  amount_detail_total, amount_summary_total, amount_drift, tolerance,
                  conserved, restored, summary_before_rows, summary_after_rows,
                  summary_before_amount, summary_after_amount, snapshot_token,
                  conservation_json
                ) VALUES (?, 'detail_to_summary', '成功', ?, ?, ?, 0, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    domain_id,
                    message,
                    len(detail),
                    len(buckets),
                    detail_total,
                    summary_now["amount"],
                    drift,
                    SUMMARY_AMOUNT_TOLERANCE,
                    before["rows"],
                    summary_now["rows"],
                    before["amount"],
                    summary_now["amount"],
                    snapshot_token,
                    json.dumps(conservation, ensure_ascii=False),
                ),
            )
            _drop_snapshot(conn, domain_id, snapshot_token)
            conn.commit()
            job_id = int(cur.lastrowid)
            return _decode_job(
                conn.execute("SELECT * FROM layer_jobs WHERE id=?", (job_id,)).fetchone()
            )
        except Exception as exc:  # 守恒失败或任意写入异常：整次任务失败并恢复快照
            # 先在同一事务内读回“本次尝试写入后的残缺汇总”，作为失败现场留档，
            # 再回滚 / 用快照覆盖恢复。
            try:
                partial = _summary_totals(conn, domain_id)
                partial_amount = partial["amount"]
                partial_rows = partial["rows"]
                partial_lines = partial["line_count"]
                partial_keys = len(partial["keys"])
            except Exception:
                partial_amount = attempted_amount
                partial_rows = len(buckets)
                partial_lines = len(detail)
                partial_keys = len(buckets)
            conn.rollback()

            if isinstance(exc, ConservationError):
                reason = exc.reason
            else:
                reason = f"汇总写入异常：{type(exc).__name__}: {exc}"

            restore_info: dict
            try:
                restore_info = _restore_summary(conn, domain_id, snapshot_rows)
                # 恢复后再次核对，确保没有留下本次写入的半截键或新旧混杂键。
                restored = _summary_totals(conn, domain_id)
                restored_drift = restored["amount"] - math.fsum(
                    float(x["amount_sum"]) for x in snapshot_rows
                )
                restore_info["恢复后金额漂移"] = restored_drift
            except Exception as restore_exc:
                conn.rollback()
                restore_info = {
                    "恢复成功": False,
                    "恢复错误": f"{type(restore_exc).__name__}: {restore_exc}",
                }
            else:
                # 落盘恢复结果；快照已无保留价值，恢复成功后清理。
                _drop_snapshot(conn, domain_id, snapshot_token)
                if not restore_info.get("恢复成功"):
                    # 恢复不完整时保留快照，便于人工按快照重新恢复。
                    for r in snapshot_rows:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO layer_summary_snapshots(
                              domain_id, token, row_id, biz_day, store_code,
                              amount_sum, qty_sum, line_count
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                domain_id, snapshot_token, int(r["id"]), r["biz_day"],
                                r["store_code"], float(r["amount_sum"]), int(r["qty_sum"]),
                                int(r["line_count"]),
                            ),
                        )

            if restore_info.get("恢复成功"):
                final_amount = float(restore_info["恢复金额合计"])
                final_rows = int(restore_info["恢复行数"])
                restore_text = (
                    "汇总层已恢复到任务开始前快照："
                    f"{restore_info['恢复行数']} 行，金额合计 "
                    f"{_fmt_amount(restore_info['恢复金额合计'])}；"
                    "不含本次写入的半截键或新旧混杂键。"
                )
            else:
                final_amount = partial_amount
                final_rows = partial_rows
                restore_text = (
                    "汇总层恢复失败，请按快照人工恢复："
                    + str(restore_info.get("恢复错误") or "")
                )
            message = f"{reason}；{restore_text}"

            # 报告主字段记录「守恒校验时」的明细/汇总/漂移——这才是判定成败的依据，
            # 也能让失败原因（漂移非零）在报告里可见。
            # 任务结束时汇总层实际状态（恢复后的快照态）另存于 summary_after_*，
            # 与汇总层列表、对账数字同源。
            conservation = {
                "明细合计": detail_total,
                "汇总合计": partial_amount,
                "漂移": partial_amount - detail_total,
                "容差": SUMMARY_AMOUNT_TOLERANCE,
                "守恒": False,
                "成败": "失败",
                "失败原因": reason,
                "恢复": restore_info,
                "容差说明": TOLERANCE_NOTE,
                "本次尝试汇总合计": partial_amount,
                "本次尝试漂移": partial_amount - detail_total,
                "本次尝试汇总行数": partial_rows,
                "本次尝试汇总行数合计": partial_lines,
                "本次尝试汇总键数": partial_keys,
                "任务结束时汇总合计": final_amount,
                "任务结束时汇总行数": final_rows,
                "任务结束时漂移": final_amount - detail_total,
                "快照行数": len(snapshot_rows),
                "快照金额合计": math.fsum(float(x["amount_sum"]) for x in snapshot_rows),
            }
            cur = conn.execute(
                """
                INSERT INTO layer_jobs(
                  domain_id, step, status, message, in_count, out_count, drop_count,
                  amount_detail_total, amount_summary_total, amount_drift, tolerance,
                  conserved, restored, summary_before_rows, summary_after_rows,
                  summary_before_amount, summary_after_amount, snapshot_token,
                  conservation_json
                ) VALUES (?, 'detail_to_summary', '失败', ?, ?, 0, 0, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    domain_id,
                    message,
                    len(detail),
                    detail_total,
                    partial_amount,
                    partial_amount - detail_total,
                    SUMMARY_AMOUNT_TOLERANCE,
                    1 if restore_info.get("恢复成功") else 0,
                    len(snapshot_rows),
                    final_rows,
                    math.fsum(float(x["amount_sum"]) for x in snapshot_rows),
                    final_amount,
                    snapshot_token,
                    json.dumps(conservation, ensure_ascii=False),
                ),
            )
            conn.commit()
            job_id = int(cur.lastrowid)
            return _decode_job(
                conn.execute("SELECT * FROM layer_jobs WHERE id=?", (job_id,)).fetchone()
            )
    finally:
        conn.close()


def summary_state(domain_id: int) -> dict:
    """汇总层当前实际状态 + 最近一次明细到汇总任务，供界面展示守恒与恢复情况。"""
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        summary = _summary_totals(conn, domain_id)
        detail_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM layer_detail WHERE domain_id=?", (domain_id,)
            ).fetchone()["c"]
        )
        detail_total = _detail_total(conn, domain_id)
        last_row = conn.execute(
            "SELECT * FROM layer_jobs WHERE domain_id=? AND step='detail_to_summary' "
            "ORDER BY id DESC LIMIT 1",
            (domain_id,),
        ).fetchone()
        last = _decode_job(last_row) if last_row else None
        if last:
            last["step_label"] = STEP_LABEL.get(last["step"], last["step"])
        live_drift = summary["amount"] - detail_total
        return {
            "主题域": domain["name"],
            "主题域编号": domain_id,
            "汇总层": {
                "行数": summary["rows"],
                "金额合计": summary["amount"],
                "明细行数合计": summary["line_count"],
                "键数": len(summary["keys"]),
            },
            "明细层": {
                "行数": detail_count,
                "金额合计": detail_total,
            },
            "当前漂移": live_drift,
            "当前守恒": abs(live_drift) <= SUMMARY_AMOUNT_TOLERANCE
            and summary["line_count"] == detail_count,
            "容差": SUMMARY_AMOUNT_TOLERANCE,
            "容差说明": TOLERANCE_NOTE,
            "最近任务": last,
        }
    finally:
        conn.close()


def run_reconcile(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        paste_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM layer_paste WHERE domain_id=?", (domain_id,)).fetchone()[
                "c"
            ]
        )
        detail_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM layer_detail WHERE domain_id=?", (domain_id,)
            ).fetchone()["c"]
        )
        summary_rows = conn.execute(
            "SELECT amount_sum, line_count FROM layer_summary WHERE domain_id=?",
            (domain_id,),
        ).fetchall()
        summary_line_sum = sum(int(r["line_count"]) for r in summary_rows)
        summary_amount = math.fsum(float(r["amount_sum"]) for r in summary_rows)
        detail_amount = _detail_total(conn, domain_id)
        key_mismatch = 1 if summary_line_sum != detail_count else 0
        amount_drift = summary_amount - detail_amount
        status = (
            "一致"
            if key_mismatch == 0 and abs(amount_drift) <= SUMMARY_AMOUNT_TOLERANCE
            else "存在差异"
        )
        detail = {
            "贴源行数": paste_count,
            "明细行数": detail_count,
            "汇总行数合计": summary_line_sum,
            "明细金额合计": detail_amount,
            "汇总金额合计": summary_amount,
            "键对齐": key_mismatch == 0,
            "金额漂移": amount_drift,
            "守恒容差": SUMMARY_AMOUNT_TOLERANCE,
        }
        cur = conn.execute(
            """
            INSERT INTO reconcile_reports(
              domain_id, paste_count, detail_count, summary_line_sum, key_mismatch, amount_drift, status, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                paste_count,
                detail_count,
                summary_line_sum,
                key_mismatch,
                amount_drift,
                status,
                json.dumps(detail, ensure_ascii=False),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM reconcile_reports WHERE id=?", (int(cur.lastrowid),)).fetchone()
        d = dict(row)
        d["detail"] = json.loads(d.pop("detail_json"))
        return d
    finally:
        conn.close()


def list_reconcile(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM reconcile_reports ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reconcile_reports WHERE domain_id=? ORDER BY id DESC",
                (domain_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out
    finally:
        conn.close()


def dashboard() -> dict:
    domains = list_domains()
    counts = layer_counts()
    jobs = list_jobs()[:5]
    reports = list_reconcile()[:5]
    return {
        "主题域数": len(domains),
        "分层计数": counts,
        "最近任务": [
            {
                "编号": j["id"],
                "步骤": STEP_LABEL.get(j["step"], j["step"]),
                "状态": j["status"],
                "写入": j["out_count"],
                "剔除": j["drop_count"],
                "明细合计": j.get("amount_detail_total"),
                "汇总合计": j.get("amount_summary_total"),
                "漂移": j.get("amount_drift"),
            }
            for j in jobs
        ],
        "最近对账": [
            {
                "编号": r["id"],
                "状态": r["status"],
                "明细行数": r["detail_count"],
                "汇总行数合计": r["summary_line_sum"],
                "明细金额合计": r["detail"].get("明细金额合计"),
                "汇总金额合计": r["detail"].get("汇总金额合计"),
                "金额漂移": r["detail"].get("金额漂移"),
            }
            for r in reports
        ],
        "主题域": [{"编号": d["id"], "名称": d["name"], "业务键": d["biz_key"]} for d in domains],
    }
