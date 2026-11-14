import os
import tempfile
from pathlib import Path

# 必须在导入 app 之前固定数据目录，保证测试使用独立临时 SQLite。
_TMP = tempfile.mkdtemp(prefix="tiercraft-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["FIXTURES_DIR"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import seed  # noqa: E402
from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    seed.init_db()
    conn = connect()
    try:
        conn.execute("DELETE FROM layer_summary_snapshots")
        conn.execute("DELETE FROM layer_jobs")
        conn.execute("DELETE FROM reconcile_reports")
        conn.execute("DELETE FROM layer_summary")
        conn.execute("DELETE FROM layer_detail")
        conn.commit()
    finally:
        conn.close()
    with TestClient(app) as c:
        yield c
