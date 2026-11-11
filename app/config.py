"""服务配置：全部来自环境变量或 .env 文件，代码里不出现数据库分支。"""

from functools import lru_cache
from typing import Literal

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
    # 启动时自动执行 alembic upgrade head；多进程部署建议关掉，改在发布流程里跑
    auto_migrate: bool = True


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
