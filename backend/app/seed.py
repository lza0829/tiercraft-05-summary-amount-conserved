from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  biz_key TEXT NOT NULL,
  fixture_file TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS layer_paste (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  store_name TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  qty INTEGER NOT NULL,
  raw_json TEXT NOT NULL,
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_detail (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  store_name TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  qty INTEGER NOT NULL,
  clean_flag TEXT NOT NULL,
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_summary (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  amount_sum REAL NOT NULL,
  qty_sum INTEGER NOT NULL,
  line_count INTEGER NOT NULL,
  UNIQUE(domain_id, biz_day, store_code),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  step TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  in_count INTEGER NOT NULL DEFAULT 0,
  out_count INTEGER NOT NULL DEFAULT 0,
  drop_count INTEGER NOT NULL DEFAULT 0,
  amount_detail_total REAL NOT NULL DEFAULT 0,
  amount_summary_total REAL NOT NULL DEFAULT 0,
  amount_drift REAL NOT NULL DEFAULT 0,
  tolerance REAL NOT NULL DEFAULT 0.000001,
  conserved INTEGER,
  restored INTEGER NOT NULL DEFAULT 0,
  summary_before_rows INTEGER NOT NULL DEFAULT 0,
  summary_after_rows INTEGER NOT NULL DEFAULT 0,
  summary_before_amount REAL NOT NULL DEFAULT 0,
  summary_after_amount REAL NOT NULL DEFAULT 0,
  snapshot_token TEXT NOT NULL DEFAULT '',
  conservation_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_summary_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  token TEXT NOT NULL,
  row_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  amount_sum REAL NOT NULL,
  qty_sum INTEGER NOT NULL,
  line_count INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE INDEX IF NOT EXISTS idx_summary_snapshots_token
  ON layer_summary_snapshots(token, id);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  paste_count INTEGER NOT NULL,
  detail_count INTEGER NOT NULL,
  summary_line_sum INTEGER NOT NULL,
  key_mismatch INTEGER NOT NULL,
  amount_drift REAL NOT NULL,
  status TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);
"""


# 旧库升级：CREATE TABLE IF NOT EXISTS 不会补列，这里按 PRAGMA 结果逐列补齐。
JOB_COLUMN_MIGRATIONS = {
    "amount_detail_total": "REAL NOT NULL DEFAULT 0",
    "amount_summary_total": "REAL NOT NULL DEFAULT 0",
    "amount_drift": "REAL NOT NULL DEFAULT 0",
    "tolerance": "REAL NOT NULL DEFAULT 0.000001",
    "conserved": "INTEGER",
    "restored": "INTEGER NOT NULL DEFAULT 0",
    "summary_before_rows": "INTEGER NOT NULL DEFAULT 0",
    "summary_after_rows": "INTEGER NOT NULL DEFAULT 0",
    "summary_before_amount": "REAL NOT NULL DEFAULT 0",
    "summary_after_amount": "REAL NOT NULL DEFAULT 0",
    "snapshot_token": "TEXT NOT NULL DEFAULT ''",
    "conservation_json": "TEXT NOT NULL DEFAULT '{}'",
}


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        _ensure_columns(conn, "layer_jobs", JOB_COLUMN_MIGRATIONS)
        row = conn.execute("SELECT COUNT(*) AS c FROM domains").fetchone()
        if int(row["c"]) == 0:
            _seed_domain(conn)
        conn.commit()
    finally:
        conn.close()


def _ensure_columns(conn, table: str, columns: dict[str, str]) -> None:
    existing = {
        r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _seed_domain(conn) -> None:
    fixture = "门店日销售-贴源.jsonl"
    cur = conn.execute(
        """
        INSERT INTO domains(name, biz_key, fixture_file, description)
        VALUES (?, ?, ?, ?)
        """,
        (
            "门店日销售",
            "业务日+门店编码",
            fixture,
            "演示贴源装载、明细清洗与按门店日汇总",
        ),
    )
    domain_id = int(cur.lastrowid)
    path = FIXTURES_DIR / fixture
    if not path.exists():
        # 仓库内相对路径兜底
        alt = Path(__file__).resolve().parents[2] / "fixtures" / fixture
        path = alt
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        conn.execute(
            """
            INSERT INTO layer_paste(
              domain_id, biz_day, store_code, store_name, category, amount, qty, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                obj.get("业务日") or "",
                obj.get("门店编码") or "",
                obj.get("门店名称") or "",
                obj.get("品类") or "",
                float(obj.get("金额") or 0),
                int(obj.get("件数") or 0),
                json.dumps(obj, ensure_ascii=False),
            ),
        )
