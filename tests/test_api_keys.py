"""API Key：生成、哈希、吊销、过期，以及两个订阅接口的鉴权和任务隔离。"""

from contextlib import contextmanager
from datetime import timedelta

from app import crud
from app.models import utcnow
from app.services.api_keys import (
    create_key,
    hash_api_key,
    revoke_key,
    verify_api_key,
)
from app.tools import api_keys as cli
from tests.test_subscriptions import SESSION, build_app, make_settings


def test_create_key_stores_only_hash(db_session):
    record, token = create_key(db_session, name="partner-a")

    assert token.startswith("kt_")
    assert record.key_prefix == token[:11]
    assert record.key_hash == hash_api_key(token)
    assert record.key_hash != token
    assert token not in record.key_hash
    assert record.enabled is True
    assert record.last_used_at is None


def test_verify_key_updates_last_used(db_session):
    record, token = create_key(db_session, name="partner-b")

    verified, reason = verify_api_key(db_session, token)

    assert reason is None
    assert verified is not None and verified.id == record.id
    db_session.refresh(record)
    assert record.last_used_at is not None


def test_verify_rejects_unknown_and_malformed(db_session):
    assert verify_api_key(db_session, "") == (None, "not_found")
    assert verify_api_key(db_session, "kt_not-a-real-key") == (None, "not_found")


def test_verify_rejects_disabled_and_expired(db_session):
    _, token = create_key(db_session, name="partner-c")
    revoke_key(db_session, prefix=token[:11])
    assert verify_api_key(db_session, token) == (None, "disabled")

    record, token = create_key(db_session, name="partner-d")
    record.expires_at = utcnow() - timedelta(minutes=1)
    crud.save_api_key(db_session, record)
    assert verify_api_key(db_session, token) == (None, "expired")


def test_endpoint_requires_valid_key(db_session):
    client = build_app(db_session, make_settings())
    payload = {"plan": "plus", "session": SESSION}

    assert client.post("/api/subscriptions", json=payload).status_code == 401
    assert (
        client.post(
            "/api/subscriptions", json=payload, headers={"X-API-Key": "kt_wrong"}
        ).status_code
        == 401
    )


def test_endpoint_rejects_revoked_and_expired_key(db_session):
    client = build_app(db_session, make_settings())
    payload = {"plan": "plus", "session": SESSION}

    _, token = create_key(db_session, name="revoked")
    revoke_key(db_session, prefix=token[:11])
    revoked = client.post("/api/subscriptions", json=payload, headers={"X-API-Key": token})
    assert revoked.status_code == 403
    assert "禁用" in revoked.text

    record, token = create_key(db_session, name="expired")
    record.expires_at = utcnow() - timedelta(seconds=1)
    crud.save_api_key(db_session, record)
    expired = client.post("/api/subscriptions", json=payload, headers={"X-API-Key": token})
    assert expired.status_code == 403
    assert "过期" in expired.text


def test_task_is_isolated_per_key(db_session):
    client = build_app(db_session, make_settings())
    _, token_a = create_key(db_session, name="key-a")
    _, token_b = create_key(db_session, name="key-b")

    created = client.post(
        "/api/subscriptions",
        json={"plan": "plus", "session": SESSION},
        headers={"X-API-Key": token_a},
    )
    assert created.status_code == 202
    task_id = created.json()["task_id"]

    own = client.get(f"/api/subscriptions/{task_id}", headers={"X-API-Key": token_a})
    assert own.status_code == 200
    assert own.json()["task_id"] == task_id

    other = client.get(f"/api/subscriptions/{task_id}", headers={"X-API-Key": token_b})
    assert other.status_code == 404

    task = crud.get_task(db_session, task_id)
    assert task.api_key_id is not None


def test_cli_create_list_revoke(db_session, monkeypatch, capsys):
    @contextmanager
    def fake_session_scope():
        yield db_session

    monkeypatch.setattr(cli, "session_scope", fake_session_scope)

    assert cli.main(["create", "--name", "cli-partner"]) == 0
    out = capsys.readouterr().out
    assert "API_KEY=kt_" in out
    token = next(line.split("=", 1)[1] for line in out.splitlines() if line.startswith("API_KEY="))
    prefix = token[:11]

    assert cli.main(["list"]) == 0
    listed = capsys.readouterr().out
    assert prefix in listed
    assert token not in listed

    assert cli.main(["revoke", "--prefix", prefix]) == 0
    assert "已吊销" in capsys.readouterr().out
    assert verify_api_key(db_session, token) == (None, "disabled")

    assert cli.main(["revoke", "--prefix", "kt_missing"]) == 1
