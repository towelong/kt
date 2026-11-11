"""AiLink VCC 异常：调用失败按 HTTP 状态码/业务 code 翻译，Webhook 侧单独两类。

约定：异常信息里只放平台返回的 msg 和 requestId，不放请求体（可能含卡三要素）。
"""

from __future__ import annotations

from typing import Any


class AiLinkError(Exception):
    """所有 AiLink 异常的基类。"""

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        status_code: int | None = None,
        code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.request_id = request_id
        self.status_code = status_code
        self.code = code

    def __str__(self) -> str:
        suffix = f"（requestId={self.request_id}）" if self.request_id else ""
        return f"{self.message}{suffix}"


class AiLinkConfigError(AiLinkError):
    """配置缺失：Key/Secret 未填，或 Webhook Secret 未配置。"""


class AiLinkTransportError(AiLinkError):
    """网络层失败或超时；超时时结果未知，资金操作不要直接判定失败。"""


class AiLinkProtocolError(AiLinkError):
    """响应不是预期 JSON 结构，或字段类型不符合文档。"""


class AiLinkBusinessError(AiLinkError):
    """HTTP 400：参数错误或明确业务拒绝（余额不足、卡状态不允许操作等）。"""


class AiLinkAuthError(AiLinkError):
    """HTTP 401：API Key 缺失、无效或未开通 API 权限。"""


class AiLinkPermissionError(AiLinkError):
    """HTTP 403：签名校验失败，或来源 IP 不在白名单。"""


class AiLinkNotFoundError(AiLinkError):
    """HTTP 404：资源不存在或无权访问。"""


class AiLinkRateLimitError(AiLinkError):
    """HTTP 429：请求频率超限，降低频率后重试。"""


class AiLinkServerError(AiLinkError):
    """HTTP 5xx：结果未知，保留 requestId 和原 requestNo 找平台核对，不要换号重提。"""


class AiLinkPayloadError(AiLinkError):
    """Webhook 正文不是合法 JSON / 缺少 eventType / 不符合事件模型。"""


class AiLinkDecryptError(AiLinkError):
    """敏感字段解密失败（Secret 不匹配、密文被篡改或算法版本不支持）。"""


class WebhookVerificationError(AiLinkError):
    """Webhook 验签相关错误的基类。"""


class WebhookHeaderError(WebhookVerificationError):
    """验签 Header 缺失或格式不合法，端点按 401 处理。"""


class WebhookSignatureError(WebhookVerificationError):
    """签名不匹配，端点按 403 处理。"""


class EventDispatchError(AiLinkError):
    """一个或多个处理器失败；端点返回 500 让平台重试。"""

    def __init__(
        self,
        message: str,
        *,
        event_id: str,
        event_type: str,
        failures: list[Any],
    ) -> None:
        super().__init__(message)
        self.event_id = event_id
        self.event_type = event_type
        self.failures = failures
