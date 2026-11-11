"""可复用的依赖：会话、分页参数。业务依赖放各自的 router 里，别都堆这儿。"""

from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import APIKeyHeader
from sqlmodel import Session

from app.db import get_session
from app.models import ApiKey
from app.services.api_keys import verify_api_key

SessionDep = Annotated[Session, Depends(get_session)]


class PaginationParams:
    def __init__(
        self,
        offset: int = Query(0, ge=0, description="跳过的记录数"),
        limit: int = Query(20, ge=1, le=100, description="本次返回的最大条数"),
    ) -> None:
        self.offset = offset
        self.limit = limit


PageDep = Annotated[PaginationParams, Depends()]


api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="对外接口密钥，放在请求头里；完整 key 只在创建时展示一次。",
)


def require_api_key(
    api_key: Annotated[str | None, Depends(api_key_header)],
    session: SessionDep,
) -> ApiKey:
    record, reason = verify_api_key(session, api_key or "")
    if reason == "not_found":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "缺少或无效的 API Key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if reason in {"disabled", "expired"}:
        message = "API Key 已禁用" if reason == "disabled" else "API Key 已过期"
        raise HTTPException(status.HTTP_403_FORBIDDEN, message)
    assert record is not None
    return record


ApiKeyDep = Annotated[ApiKey, Depends(require_api_key)]
