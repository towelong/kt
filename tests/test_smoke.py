"""冒烟测试：用独立的 SQLite 文件跑一遍读写，确认分层能串起来。"""

import os
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

TEST_DB = Path(tempfile.gettempdir()) / f"kt_test_{uuid4().hex}.db"


@pytest.fixture(scope="session")
def client():
    os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
    from app.config import get_settings

    get_settings.cache_clear()

    from app.main import app

    with TestClient(app) as c:
        yield c

    TEST_DB.unlink(missing_ok=True)


@pytest.fixture
def session(client):
    from app.db import engine

    with Session(engine) as s:
        yield s


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_item_crud(client):
    created = client.post("/api/items", json={"name": "苹果", "price": 3.5})
    assert created.status_code == 201, created.text
    item_id = created.json()["id"]

    got = client.get(f"/api/items/{item_id}")
    assert got.status_code == 200
    assert got.json()["name"] == "苹果"

    patched = client.patch(f"/api/items/{item_id}", json={"price": 4.0})
    assert patched.status_code == 200
    assert patched.json()["price"] == 4.0

    assert client.delete(f"/api/items/{item_id}").status_code == 204
    assert client.get(f"/api/items/{item_id}").status_code == 404


def test_user_unique_conflict(client):
    payload = {"username": "alice", "email": "alice@example.com"}
    assert client.post("/api/users", json=payload).status_code == 201
    dup = client.post("/api/users", json=payload)
    assert dup.status_code == 409, dup.text


def test_pagination(client):
    for i in range(5):
        client.post("/api/items", json={"name": f"批量{i}"})

    page = client.get("/api/items", params={"offset": 1, "limit": 2})
    assert page.status_code == 200
    assert len(page.json()) == 2


def test_db_info_reports_dialect(client):
    assert client.get("/internal/admin/db-info").json()["dialect"] == "sqlite"


def test_migrations_applied_on_startup(client):
    state = client.get("/internal/admin/migration-state").json()
    assert state["current"] == state["head"]
    assert state["current"] is not None
