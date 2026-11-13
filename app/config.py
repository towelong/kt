"""服务配置：全部来自环境变量或 .env 文件，代码里不出现数据库分支。"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 例：sqlite:///./app.db
    # MySQL：mysql+pymysql://<用户名>@<主机>:3306/kt?charset=utf8mb4（密码放 .env）
    database_url: str = "sqlite:///./app.db"
    app_name: str = "kt"
    debug: bool = False

    # 只对连接池型数据库（MySQL）生效，SQLite 走 NullPool 之外的默认行为用不到这些
    pool_size: int = 5
    max_overflow: int = 10
    pool_recycle_seconds: int = 3600

    api_prefix: str = "/api"
    cors_origins: list[str] = []
    # 启动时自动执行 alembic upgrade head。默认关闭，开发环境在 .env 里打开；
    # 生产/多副本一律在发布流程里单独跑迁移
    auto_migrate: bool = False

    # AiLink VCC 开放 API。Key/Secret 用 SecretStr，避免 repr、日志、异常里意外带出明文
    ailink_base_url: str = "https://card.ailinkvcc.com"
    ailink_api_key: SecretStr | None = None
    ailink_api_secret: SecretStr | None = None
    ailink_webhook_secret: SecretStr | None = None
    ailink_timeout_seconds: float = 10.0
    # Webhook 正文上限，默认 1 MiB；开卡批次事件会稍大，但远小于这个值
    ailink_webhook_max_body_bytes: int = 1024 * 1024
    # 排查用：成功请求也打印 header/body（敏感字段会被打码）。生产保持 false
    ailink_webhook_debug: bool = False

    # 卡库存扫描：默认关闭，配好 Key/Secret 再打开
    card_stock_enabled: bool = False
    card_stock_interval_seconds: int = 300
    # 占位卡在这段时间内没等到开卡 webhook，就先不重复开新卡
    card_stock_pending_minutes: int = 30
    # 补卡任务重试上限：开卡要么开成、要么到次数置 failed，不能无限等
    open_card_max_attempts: int = 5
    # USDT→USD 折算率：10 USDT 约到账 9.97 USD
    card_stock_usdt_usd_rate: float = 0.997

    # BYOC 订阅（Strong Exchange）：下单前先取报价，订单只认 BYOC 的终态
    byoc_base_url: str = "https://wangbohui.com"
    byoc_api_key: SecretStr | None = None
    byoc_callback_secret: SecretStr | None = None
    # 任务里 session 落库用的 AES-256-GCM key：32 字节 base64，或任意字符串（内部 SHA256 派生）
    task_payload_key: SecretStr | None = None
    subscription_worker_enabled: bool = False
    subscription_worker_interval_seconds: int = 10
    subscription_callback_max_attempts: int = 5
    # BYOC 失败一律保留 payload 重试（同 request_id），达到次数才置为失败
    subscription_max_submit_attempts: int = 10
    subscription_retry_backoff_seconds: int = 60
    subscription_retry_max_backoff_seconds: int = 300

    # 平台限流：每个真实 HTTP 请求占一个配额，重试也占
    ailink_rate_limit_per_minute: int = 450
    byoc_rate_limit_per_minute: int = 300

    @property
    def ailink_configured(self) -> bool:
        """Key/Secret 都非空才能调开放 API；空串不等于已配置。"""
        return _secret_configured(self.ailink_api_key) and _secret_configured(
            self.ailink_api_secret
        )

    @property
    def ailink_webhook_configured(self) -> bool:
        return _secret_configured(self.ailink_webhook_secret)

    @property
    def byoc_configured(self) -> bool:
        return _secret_configured(self.byoc_api_key)

    @property
    def task_payload_configured(self) -> bool:
        return _secret_configured(self.task_payload_key)


def _secret_configured(value: SecretStr | None) -> bool:
    return value is not None and bool(value.get_secret_value().strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()


def database_backend(url: str) -> Literal["sqlite", "mysql"]:
    """仅用于日志/自检，不参与业务逻辑。"""
    if url.startswith("sqlite"):
        return "sqlite"
    if url.startswith("mysql"):
        return "mysql"
    raise ValueError(f"不支持的 DATABASE_URL：{url.split('://', 1)[0]}://...")


def mask_url(url: str | URL) -> str:
    """脱敏后的连接串，日志和运维接口用；不含凭据。"""
    parsed = make_url(url) if isinstance(url, str) else url
    if parsed.get_backend_name() == "sqlite":
        return parsed.render_as_string()
    return (
        f"{parsed.drivername}://{parsed.username or ''}@"
        f"{parsed.host or ''}:{parsed.port or 3306}/{parsed.database or ''}"
    )
