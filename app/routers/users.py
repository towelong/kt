"""User 路由。"""

from fastapi import APIRouter, HTTPException, status

from app import crud
from app.dependencies import PageDep, SessionDep
from app.models import UserCreate, UserPublic

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserPublic])
def list_users(session: SessionDep, page: PageDep) -> list[UserPublic]:
    _, users = crud.list_users(session, offset=page.offset, limit=page.limit)
    return [UserPublic.model_validate(user, from_attributes=True) for user in users]


@router.post("", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, session: SessionDep) -> UserPublic:
    try:
        user = crud.create_user(session, payload)
    except crud.ConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return UserPublic.model_validate(user, from_attributes=True)


@router.get("/{user_id}", response_model=UserPublic)
def get_user(user_id: int, session: SessionDep) -> UserPublic:
    try:
        user = crud.get_user(session, user_id)
    except crud.NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return UserPublic.model_validate(user, from_attributes=True)
