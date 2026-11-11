"""可复用的依赖：会话、分页参数。业务依赖放各自的 router 里，别都堆这儿。"""

from typing import Annotated

from fastapi import Depends, Query
from sqlmodel import Session

from app.db import get_session

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
