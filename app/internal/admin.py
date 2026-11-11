"""内部接口：不属于某个资源的运维/维护逻辑。默认不对外暴露，记得加鉴权。"""

import logging

from fastapi import APIRouter

from app.db import engine_info, init_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/admin", tags=["internal"])


@router.get("/db-info")
def db_info() -> dict:
    """看一眼当前连的是哪个库、哪个方言，排查环境问题时最省事。"""
    return engine_info()


@router.post("/init-db", status_code=204)
def init_database() -> None:
    """手工建表，等价于启动时做的 create_all。"""
    init_db()
