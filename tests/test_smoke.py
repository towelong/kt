"""冒烟测试：临时 SQLite 上跑一遍启动、健康检查和迁移状态。

数据库由 tests/conftest.py 统一指到临时文件，测试之间不清理数据。
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    from app.config import get_settings

    get_settings.cache_clear()

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_db_info_reports_dialect(client):
    assert client.get("/internal/admin/db-info").json()["dialect"] == "sqlite"


def test_migrations_applied_on_startup(client):
    state = client.get("/internal/admin/migration-state").json()
    assert state["current"] == state["head"]
    assert state["current"] is not None
