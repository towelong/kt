"""共享测试工具：Webhook 签名、样例事件。"""

import json
import time
from collections.abc import Callable

import pytest

from app.integrations.ailink.signing import body_sha256, sign

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
