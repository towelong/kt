"""签名原文与结果的固定向量：实现和文档在线测试脚本必须逐字节一致。"""

import hashlib
import hmac

from app.integrations.ailink.signing import body_sha256, request_signature, signature_base


def test_body_hash_is_sha256_of_raw_bytes():
    body = '{"remark":"广告投放卡"}'.encode()
    assert body_sha256(body) == hashlib.sha256(body).hexdigest()


def test_signature_base_matches_doc_formula():
    body = b'{"a":1}'
    base = signature_base(
        method="post",
        path="/openapi/v1/card-bins/query",
        timestamp_ms="1700000000000",
        nonce="nonce-1",
        body=body,
    )
    expected = "\n".join(
        [
            "POST",
            "/openapi/v1/card-bins/query",
            "1700000000000",
            "nonce-1",
            hashlib.sha256(body).hexdigest(),
        ]
    )
    assert base == expected


def test_request_signature_known_vector():
    """固定输入 + 独立算出的 hex，防止签名串某天被“顺手改顺”。"""
    signature = request_signature(
        "sk_demo_secret",
        method="POST",
        path="/openapi/v1/account/convert",
        timestamp_ms="1730900000000",
        nonce="abc123",
        body=b'{"fromCurrency":"USDT","toCurrency":"USD","amount":"100.00"}',
    )
    assert signature == "d732488c328a41be063c9968bc9779985e797923612db3f133c9bbb24e0de080"


def test_request_signature_is_hmac_sha256_hex():
    body = b"{}"
    base = signature_base(
        method="POST",
        path="/openapi/v1/account/balance/query",
        timestamp_ms="1",
        nonce="n",
        body=body,
    )
    expected = hmac.new(b"secret", base.encode(), hashlib.sha256).hexdigest()
    assert (
        request_signature(
            "secret",
            method="POST",
            path="/openapi/v1/account/balance/query",
            timestamp_ms="1",
            nonce="n",
            body=body,
        )
        == expected
    )
