"""内部接口：不属于某个资源的运维/维护逻辑。默认不对外暴露，记得加鉴权。"""

from fastapi import APIRouter

from app.config import get_settings
from app.db import engine, engine_info
from app.migrations import current_revision, head_revision, upgrade_to_head

router = APIRouter(prefix="/internal/admin", tags=["internal"])


@router.get("/db-info")
def db_info() -> dict:
    """看一眼当前连的是哪个库、哪个方言，排查环境问题时最省事。"""
    return engine_info()


@router.get("/migration-state")
def migration_state() -> dict[str, str | None]:
    """当前版本 vs 代码里的最新版本，两者不一致就说明有迁移没跑。"""
    return {
        "current": current_revision(engine),
        "head": head_revision(get_settings().database_url),
    }


@router.post("/migrate", status_code=204)
def migrate() -> None:
    """手工把库升到最新，等价于 alembic upgrade head。"""
    upgrade_to_head()
