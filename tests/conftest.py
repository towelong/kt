"""共享测试工具：Webhook 签名、样例事件、临时数据库。"""

import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

# 在任何 app 模块导入前锁死测试库，避免测试碰到项目里的 app.db
_TEST_DB = Path(tempfile.gettempdir()) / f"kt_pytest_{uuid4().hex}.db"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TEST_DB}")


def pytest_report_header(config) -> str:
    """把本轮测试库路径打出来，跑完数据还在，方便下个业务直接查。"""
    return f"test db: {_TEST_DB}"


from app.integrations.ailink.signing import body_sha256, sign  # noqa: E402

WEBHOOK_SECRET = "whsec_test_secret"


def build_webhook_headers(
    body: bytes,
    *,
    secret: str = WEBHOOK_SECRET,
    event_id: str = "evt_test_1",
    event_type: str = "CARD_ISSUE.SUCCESS",
    nonce: str = "nonce_test",
    timestamp: str | None = None,
) -> dict[str, str]:
    ts = timestamp or str(int(time.time() * 1000))
    base = "\n".join([event_id, event_type, ts, nonce, body_sha256(body)])
    return {
        "X-VCC-WEBHOOK-ID": event_id,
        "X-VCC-WEBHOOK-EVENT": event_type,
        "X-VCC-WEBHOOK-TIMESTAMP": ts,
        "X-VCC-WEBHOOK-NONCE": nonce,
        "X-VCC-WEBHOOK-SIGNATURE": f"v1={sign(secret, base)}",
    }


def to_body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


CARD_ISSUE_PAYLOAD = {
    "eventId": "evt_card_issue_1",
    "eventType": "CARD_ISSUE.SUCCESS",
    "eventCategory": "CARD_ISSUE",
    "eventTime": "2026-07-02 11:30:00",
    "apiVersion": "2026-06-01",
    "merchantId": 10001,
    "data": {
        "taskId": 901,
        "batchNo": "BATCH202607020001",
        "requestNo": "kaika_412321_idsjaidja",
        "cardType": "PREPAID",
        "applyStatus": "SUCCESS",
        "fundStatus": "CONFIRMED",
        "deliveryMode": "POOL_IMMEDIATE",
        "initialFundingStatus": "PENDING",
        "initialFundingPendingCount": 1,
        "initialFundingPendingAmount": 100.00,
        "cardIds": ["VC202607020001"],
        "totalCount": 1,
        "successCount": 1,
        "failCount": 0,
        "cards": [
            {
                "cardId": "VC202607020001",
                "cardNoMask": "486532******1298",
                "cardType": "PREPAID",
                "cardStatus": 1,
                "remark": "广告投放卡",
                "effectiveLimitConfig": {
                    "perTransLimit": "0.00",
                    "dayLimit": "0.00",
                    "monthLimit": "0.00",
                    "cardLifeCycleLimit": "3000.00",
                    "currency": "USD",
                },
                "cardNumberCiphertext": "",
                "expiryDateCiphertext": "",
                "cvvCiphertext": "",
                "sensitiveEncryptAlg": "AES-256-GCM",
                "sensitiveEncryptVersion": "v1",
                "issueBatchNo": "BATCH202607020001",
                "createdAt": "2026-07-02 11:29:58",
            }
        ],
        "failItems": [],
        "completeTime": "2026-07-02 11:30:00",
        "updateTime": "2026-07-02 11:30:00",
    },
}


@pytest.fixture
def sign_webhook() -> Callable[..., dict[str, str]]:
    return build_webhook_headers


@pytest.fixture
def card_issue_body() -> bytes:
    return to_body(CARD_ISSUE_PAYLOAD)


@pytest.fixture
def make_body() -> Callable[[dict], bytes]:
    return to_body


@pytest.fixture
def encrypt_field() -> Callable[[str, str], str]:
    """按文档口径造一条密文：base64url(iv + ciphertext + tag)，不带填充。"""
    import base64
    import os

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from app.integrations.ailink.crypto import derive_sensitive_key

    def _encrypt(value: str, secret: str) -> str:
        key = derive_sensitive_key(secret)
        iv = os.urandom(12)
        sealed = AESGCM(key).encrypt(iv, value.encode(), None)
        return base64.urlsafe_b64encode(iv + sealed).decode().rstrip("=")

    return _encrypt


@pytest.fixture
def db_engine(tmp_path):
    """临时 SQLite：迁移本身就是建表路径，测试不另建表，也不清理数据。"""
    from alembic import command
    from sqlalchemy import create_engine

    from app.migrations import alembic_config

    url = f"sqlite:///{tmp_path / 'cards.db'}"
    command.upgrade(alembic_config(url), "head")
    # alembic 的 fileConfig 会 disable 已存在的 logger，测试里要重新打开才能断言日志
    for logger_name in logging.root.manager.loggerDict:
        logging.getLogger(logger_name).disabled = False
    engine = create_engine(url, connect_args={"check_same_thread": False})
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    from sqlmodel import Session

    with Session(db_engine) as session:
        yield session


@pytest.fixture
def session_factory(db_engine):
    from sqlmodel import Session

    @contextmanager
    def factory():
        with Session(db_engine) as session:
            yield session

    return factory


@pytest.fixture
def api_key(db_session):
    """创建一个可用 key，返回 (记录, 请求头)。"""
    from app.services.api_keys import create_key

    record, token = create_key(db_session, name="test-key")
    return record, {"X-API-Key": token}
