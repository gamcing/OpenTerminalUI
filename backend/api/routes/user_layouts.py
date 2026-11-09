from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.cloudflare.d1 import D1BatchError
from backend.cloudflare.user_layouts_storage import get_d1_user_layouts
from backend.models import UserLayoutORM

router = APIRouter(prefix="/user/layouts")


class LaunchpadLayoutsPayload(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


def _user_key_from_request(request: Request) -> str:
    current_user = getattr(request.state, "current_user", None)
    user_id = getattr(current_user, "id", None)
    if isinstance(user_id, str) and user_id.strip():
        return user_id.strip()
    return "anonymous"


@router.get("")
def get_user_layouts(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user_key = _user_key_from_request(request)
    try:
        d1 = get_d1_user_layouts()
        if d1 is not None:
            return {"items": d1.get(user_key)}
    except (D1BatchError, RuntimeError, ValueError, KeyError, TypeError):
        raise HTTPException(status_code=503, detail="User layout storage unavailable") from None
    row = db.query(UserLayoutORM).filter(UserLayoutORM.user_key == user_key).first()
    if not row:
        return {"items": []}
    return {"items": row.layouts_json if isinstance(row.layouts_json, list) else []}


@router.put("")
def put_user_layouts(
    payload: LaunchpadLayoutsPayload,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    user_key = _user_key_from_request(request)
    try:
        d1 = get_d1_user_layouts()
        if d1 is not None:
            d1.put(user_key, payload.items)
            return {"ok": True, "count": len(payload.items)}
    except (D1BatchError, RuntimeError, ValueError, KeyError, TypeError):
        raise HTTPException(status_code=503, detail="User layout storage unavailable") from None
    row = db.query(UserLayoutORM).filter(UserLayoutORM.user_key == user_key).first()
    if row is None:
        row = UserLayoutORM(user_key=user_key, layouts_json=payload.items, updated_at=datetime.now(timezone.utc))
        db.add(row)
    else:
        row.layouts_json = payload.items
        row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "count": len(payload.items)}
