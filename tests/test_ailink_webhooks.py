"""Webhook 验签 / 解析 / 三要素解密。"""

import base64
import copy
import hashlib
import hmac
import json
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.integrations.ailink.crypto import decrypt_sensitive, derive_sensitive_key
from app.integrations.ailink.errors import (
    AiLinkDecryptError,
    AiLinkPayloadError,
    WebhookHeaderError,
    WebhookSignatureError,
)
from app.integrations.ailink.events import (
    Card3dsEvent,
    CardIssueEvent,
    CardTransactionEvent,
    UnknownEvent,
)
from app.integrations.ailink.webhooks import parse_event, verify_webhook_signature

WEBHOOK_SECRET = "whsec_test_secret"


def encrypt_field(value: str, secret: str) -> str:
    """按文档口径造一条密文：base64url(iv + ciphertext + tag)，不带填充。"""
    key = derive_sensitive_key(secret)
    iv = os.urandom(12)
    sealed = AESGCM(key).encrypt(iv, value.encode(), None)
    return base64.urlsafe_b64encode(iv + sealed).decode().rstrip("=")


def test_derive_key_matches_doc_formula():
    expected = hmac.new(b"whsec", b"vcc-webhook-sensitive-v1", hashlib.sha256).digest()
    assert derive_sensitive_key("whsec") == expected


def test_verify_signature_accepts_valid_headers(sign_webhook, card_issue_body):
    headers = sign_webhook(card_issue_body)
    verify_webhook_signature(card_issue_body, headers, WEBHOOK_SECRET)


def test_verify_signature_rejects_tampered_body(sign_webhook, card_issue_body):
    headers = sign_webhook(card_issue_body)
    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(card_issue_body + b" ", headers, WEBHOOK_SECRET)


def test_verify_signature_rejects_wrong_secret(sign_webhook, card_issue_body):
    headers = sign_webhook(card_issue_body, secret="whsec_other")
    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(card_issue_body, headers, WEBHOOK_SECRET)


def test_verify_signature_requires_all_headers(card_issue_body):
    with pytest.raises(WebhookHeaderError):
        verify_webhook_signature(card_issue_body, {}, WEBHOOK_SECRET)


def test_verify_signature_rejects_unknown_scheme(sign_webhook, card_issue_body):
    headers = sign_webhook(card_issue_body)
    headers["X-VCC-WEBHOOK-SIGNATURE"] = "v2=deadbeef"
    with pytest.raises(WebhookHeaderError):
        verify_webhook_signature(card_issue_body, headers, WEBHOOK_SECRET)


def test_verify_signature_optional_skew_check(sign_webhook, card_issue_body):
    headers = sign_webhook(card_issue_body, timestamp="1700000000000")
    verify_webhook_signature(card_issue_body, headers, WEBHOOK_SECRET)
    with pytest.raises(WebhookSignatureError):
        verify_webhook_signature(
            card_issue_body,
            headers,
            WEBHOOK_SECRET,
            max_skew_seconds=60,
        )


def test_parse_card_issue_success(card_issue_body):
    event = parse_event(card_issue_body)
    assert isinstance(event, CardIssueEvent)
    assert event.data.apply_status == "SUCCESS"
    assert event.data.initial_funding_status == "PENDING"
    assert event.data.card_ids == ["VC202607020001"]
    assert event.data.cards[0].effective_limit_config is not None
    assert event.data.cards[0].effective_limit_config.card_life_cycle_limit is not None


def test_decrypt_card_secrets_roundtrip(sign_webhook, card_issue_body, make_body):
    payload = copy.deepcopy(json.loads(card_issue_body))
    card = payload["data"]["cards"][0]
    card["cardNumberCiphertext"] = encrypt_field("4865321234561298", WEBHOOK_SECRET)
    card["expiryDateCiphertext"] = encrypt_field("12/29", WEBHOOK_SECRET)
    card["cvvCiphertext"] = encrypt_field("123", WEBHOOK_SECRET)

    event = parse_event(make_body(payload))
    assert isinstance(event, CardIssueEvent)
    secrets = event.data.decrypt_cards(WEBHOOK_SECRET)
    assert secrets[0].card_number == "4865321234561298"
    assert secrets[0].expiry_date == "12/29"
    assert secrets[0].cvv == "123"
    assert "4865321234561298" not in repr(secrets[0])
    assert "123" not in repr(secrets[0])


def test_decrypt_with_wrong_secret_raises(card_issue_body, make_body):
    payload = copy.deepcopy(json.loads(card_issue_body))
    payload["data"]["cards"][0]["cardNumberCiphertext"] = encrypt_field(
        "4865321234561298", WEBHOOK_SECRET
    )
    event = parse_event(make_body(payload))
    assert isinstance(event, CardIssueEvent)
    with pytest.raises(AiLinkDecryptError):
        event.data.decrypt_cards("whsec_other")


def test_decrypt_rejects_unsupported_alg():
    with pytest.raises(AiLinkDecryptError):
        decrypt_sensitive("abc", b"0" * 32, alg="AES-128-CBC", version="v1")


def test_parse_card_issue_failed(make_body):
    body = make_body(
        {
            "eventId": "evt_failed",
            "eventType": "CARD_ISSUE.FAILED",
            "eventCategory": "CARD_ISSUE",
            "eventTime": "2026-07-02 11:45:00",
            "apiVersion": "2026-06-01",
            "merchantId": 10001,
            "data": {
                "taskId": 902,
                "applyStatus": "FAILED",
                "fundStatus": "REFUNDED",
                "cards": [],
                "failItems": [
                    {
                        "itemId": 8891,
                        "status": "FAILED",
                        "fundStatus": "REFUNDED",
                        "failReasonCode": "CARD_ISSUE_FAILED",
                    }
                ],
            },
        }
    )
    event = parse_event(body)
    assert isinstance(event, CardIssueEvent)
    assert event.data.fail_items[0].fail_reason_code == "CARD_ISSUE_FAILED"


def test_parse_transaction_settled(make_body):
    body = make_body(
        {
            "eventId": "evt_txn",
            "eventType": "CARD_TRANSACTION.SETTLED",
            "eventCategory": "CARD_TRANSACTION",
            "eventTime": "2026-07-02 12:05:00",
            "apiVersion": "2026-06-01",
            "merchantId": 10001,
            "data": {
                "transactionId": "txn_900001",
                "eventStatus": "SETTLED",
                "transactionType": "PURCHASE",
                "originalAmount": "35.60",
                "actualAmount": "36.13",
                "feeAmount": "0.53",
                "actualCurrency": "USD",
                "cardId": "card_88481234",
            },
        }
    )
    event = parse_event(body)
    assert isinstance(event, CardTransactionEvent)
    assert event.data.actual_amount is not None
    assert str(event.data.actual_amount) == "36.13"
    assert event.data.fee_amount is not None
    assert str(event.data.fee_amount) == "0.53"


def test_parse_3ds_keeps_otp_out_of_repr(make_body):
    body = make_body(
        {
            "eventId": "evt_3ds",
            "eventType": "CARD_3DS.OTP_RECEIVED",
            "eventCategory": "CARD_3DS",
            "eventTime": "2026-08-22 14:31:06",
            "apiVersion": "2026-06-01",
            "merchantId": 10001,
            "data": {
                "otpId": 98231,
                "cardId": "VC202608220001",
                "otpCode": "387123",
                "merchantName": "GrabRegistrationSG",
            },
        }
    )
    event = parse_event(body)
    assert isinstance(event, Card3dsEvent)
    assert event.data.otp_code.get_secret_value() == "387123"
    assert "387123" not in repr(event)
    assert "387123" not in str(event.data)


def test_unknown_event_type_is_kept(make_body):
    body = make_body(
        {
            "eventId": "evt_new",
            "eventType": "CARD_FUTURE.SOMETHING",
            "eventCategory": "CARD_FUTURE",
            "eventTime": "2026-07-02 12:00:00",
            "apiVersion": "2026-06-01",
            "merchantId": 10001,
            "data": {"foo": "bar"},
        }
    )
    event = parse_event(body)
    assert isinstance(event, UnknownEvent)
    assert event.data == {"foo": "bar"}


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        b'{"eventId":"e1"}',
        b'{"eventId":"e1","eventType":"CARD_ISSUE.SUCCESS"}',
    ],
)
def test_bad_payloads_raise_payload_error(body):
    with pytest.raises(AiLinkPayloadError):
        parse_event(body)
