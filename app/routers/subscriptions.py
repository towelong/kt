"""订阅接口：只投递任务，结果由 worker 处理并通过回调返回。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session

from app import crud
from app.config import Settings, get_settings
from app.db import get_session
from app.dependencies import ApiKeyDep
from app.models import Task
from app.services.card_stock import PLAN_BY_NAME
from app.services.subscriptions import KIND_SUBSCRIPTION, create_subscription_task

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]


class SubscriptionCreate(BaseModel):
    plan: str = Field(description="内部套餐名：plus / pro100 / pro200 / pro500")
    session: dict[str, Any] = Field(description="完整 Session 对象，只以密文落库")
    callback_url: str | None = Field(default=None, description="结果回调地址（http/https）")


class SubscriptionAccepted(BaseModel):
    task_id: int
    request_id: str
    status: str
    message: str = "已受理，结果通过回调返回"


class SubscriptionPublic(BaseModel):
    task_id: int
    request_id: str
    plan: str | None
    status: str
    display_amount: str | None
    payment_status: str | None
    settlement: str | None
    byoc_task_id: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


def _to_public(task: Task) -> SubscriptionPublic:
    return SubscriptionPublic(
        task_id=task.id or 0,
        request_id=task.request_id,
        plan=task.plan,
        status=task.status,
        display_amount=task.display_amount,
        payment_status=task.payment_status,
        settlement=task.settlement,
        byoc_task_id=task.byoc_task_id,
        last_error=task.last_error,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=SubscriptionAccepted)
def create_subscription(
    payload: SubscriptionCreate,
    session: SessionDep,
    settings: SettingsDep,
    api_key: ApiKeyDep,
) -> SubscriptionAccepted:
    if payload.plan not in PLAN_BY_NAME:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"不支持的套餐：{payload.plan}",
        )
    if payload.callback_url:
        address = urlsplit(payload.callback_url)
        if address.scheme not in {"http", "https"} or not address.netloc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "callback_url 不合法")
    if not settings.task_payload_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "未配置 TASK_PAYLOAD_KEY，无法安全保存 session",
        )
    try:
        task = create_subscription_task(
            session,
            plan=payload.plan,
            session_payload=payload.session,
            callback_url=payload.callback_url,
            settings=settings,
            api_key_id=api_key.id,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    return SubscriptionAccepted(
        task_id=task.id or 0,
        request_id=task.request_id,
        status=task.status,
    )


@router.get("/{task_id}", response_model=SubscriptionPublic)
def get_subscription(
    task_id: int,
    session: SessionDep,
    api_key: ApiKeyDep,
) -> SubscriptionPublic:
    try:
        task = crud.get_task(session, task_id)
    except crud.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if task.kind != KIND_SUBSCRIPTION or task.api_key_id != api_key.id:
        # 不区分“不存在”和“不属于这个 key”，避免探测别人任务
        raise HTTPException(status.HTTP_404_NOT_FOUND, "订阅任务不存在")
    return _to_public(task)
