"""Item 路由：只做 HTTP 层的事（参数、状态码、错误翻译）。"""

from fastapi import APIRouter, HTTPException, status

from app import crud
from app.dependencies import PageDep, SessionDep
from app.models import ItemCreate, ItemPublic, ItemUpdate

router = APIRouter(prefix="/items", tags=["items"])


@router.get("", response_model=list[ItemPublic])
def list_items(session: SessionDep, page: PageDep) -> list[ItemPublic]:
    _, items = crud.list_items(session, offset=page.offset, limit=page.limit)
    return [ItemPublic.model_validate(item, from_attributes=True) for item in items]


@router.post("", response_model=ItemPublic, status_code=status.HTTP_201_CREATED)
def create_item(payload: ItemCreate, session: SessionDep) -> ItemPublic:
    item = crud.create_item(session, payload)
    return ItemPublic.model_validate(item, from_attributes=True)


@router.get("/{item_id}", response_model=ItemPublic)
def get_item(item_id: int, session: SessionDep) -> ItemPublic:
    try:
        item = crud.get_item(session, item_id)
    except crud.NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return ItemPublic.model_validate(item, from_attributes=True)


@router.patch("/{item_id}", response_model=ItemPublic)
def update_item(item_id: int, payload: ItemUpdate, session: SessionDep) -> ItemPublic:
    try:
        item = crud.update_item(session, item_id, payload)
    except crud.NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return ItemPublic.model_validate(item, from_attributes=True)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: int, session: SessionDep) -> None:
    try:
        crud.delete_item(session, item_id)
    except crud.NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
