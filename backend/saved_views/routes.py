from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.auth.deps import get_current_user
from backend.cloudflare.saved_views import SavedViewRecord
from backend.models.user import User
from backend.saved_views.models import SavedViewORM
from backend.saved_views.d1_storage import get_d1_saved_views
from backend.saved_views.schemas import SavedViewCreate, SavedViewOut, SavedViewUpdate

router = APIRouter(prefix="/saved-views", tags=["saved-views"])


def _to_out(row: SavedViewORM | SavedViewRecord) -> SavedViewOut:
    return SavedViewOut(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        scope=row.scope,
        page=row.page,
        payload=row.payload_json or {},
        description=row.description or "",
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=dict[str, list[SavedViewOut]])
def list_saved_views(
    scope: str | None = Query(default=None),
    page: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, list[SavedViewOut]]:
    d1 = get_d1_saved_views()
    if d1 is not None:
        return {"items": [_to_out(row) for row in d1.list(current_user.id, scope=scope, page=page)]}
    query = db.query(SavedViewORM).filter(SavedViewORM.user_id == current_user.id)
    if scope:
        query = query.filter(SavedViewORM.scope == scope)
    if page:
        query = query.filter(SavedViewORM.page == page)
    rows = query.order_by(SavedViewORM.updated_at.desc()).all()
    return {"items": [_to_out(row) for row in rows]}


@router.post("", response_model=SavedViewOut, status_code=status.HTTP_201_CREATED)
def create_saved_view(
    payload: SavedViewCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SavedViewOut:
    d1 = get_d1_saved_views()
    if d1 is not None:
        row = d1.create(
            current_user.id, name=payload.name, scope=payload.scope, page=payload.page,
            payload=payload.payload, description=payload.description,
        )
        return _to_out(row)
    now = datetime.now(timezone.utc)
    row = SavedViewORM(
        user_id=current_user.id,
        name=payload.name.strip(),
        scope=payload.scope.strip() or "workspace",
        page=payload.page.strip(),
        payload_json=payload.payload,
        description=payload.description,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.patch("/{view_id}", response_model=SavedViewOut)
def update_saved_view(
    view_id: str,
    payload: SavedViewUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SavedViewOut:
    d1 = get_d1_saved_views()
    if d1 is not None:
        row = d1.update(
            current_user.id, view_id, name=payload.name, scope=payload.scope,
            page=payload.page, payload=payload.payload, description=payload.description,
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved view not found")
        return _to_out(row)
    row = db.query(SavedViewORM).filter(SavedViewORM.id == view_id, SavedViewORM.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved view not found")
    if payload.name is not None:
        row.name = payload.name.strip()
    if payload.scope is not None:
        row.scope = payload.scope.strip() or "workspace"
    if payload.page is not None:
        row.page = payload.page.strip()
    if payload.payload is not None:
        row.payload_json = payload.payload
    if payload.description is not None:
        row.description = payload.description
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.delete("/{view_id}", response_model=dict[str, bool])
def delete_saved_view(
    view_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict[str, bool]:
    d1 = get_d1_saved_views()
    if d1 is not None:
        if not d1.delete(current_user.id, view_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved view not found")
        return {"ok": True}
    row = db.query(SavedViewORM).filter(SavedViewORM.id == view_id, SavedViewORM.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Saved view not found")
    db.delete(row)
    db.commit()
    return {"ok": True}
