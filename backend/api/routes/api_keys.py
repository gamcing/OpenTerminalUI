from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import AliasChoices, BaseModel, Field
from typing import List, Optional
from datetime import datetime

from backend.shared.db import SessionLocal
from backend.models.api_key import APIKeyORM
from backend.models.user import User
from backend.core.api_key_auth import generate_api_key
from backend.cloudflare.api_keys import get_d1_api_keys
from backend.cloudflare.d1 import D1BatchError
from backend.auth.deps import get_current_user

router = APIRouter()


def _d1_repository():
    try:
        return get_d1_api_keys()
    except (RuntimeError, ValueError):
        raise HTTPException(status_code=503, detail="API-key storage unavailable") from None


def _d1_call(operation, *args):
    try:
        return operation(*args)
    except (D1BatchError, ValueError, KeyError):
        raise HTTPException(status_code=503, detail="API-key storage unavailable") from None


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class APIKeyCreate(BaseModel):
    name: str
    permissions: str = "read"

class APIKeyResponse(BaseModel):
    id: int
    name: str
    # ORM rows carry the column as key_prefix; create_api_key passes a dict keyed "prefix".
    prefix: str = Field(validation_alias=AliasChoices("prefix", "key_prefix"))
    permissions: str
    is_active: int
    last_used_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True

class APIKeyNewResponse(APIKeyResponse):
    key: str # Full key only returned once

@router.post("/settings/api-keys", response_model=APIKeyNewResponse)
def create_api_key(
    data: APIKeyCreate, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    identity_keys = _d1_repository()
    if identity_keys is not None and data.permissions not in {"read", "read_write"}:
        raise HTTPException(status_code=422, detail="Invalid API-key permissions")
    full_key, prefix, key_hash = generate_api_key()
    if identity_keys is not None:
        new_key = _d1_call(identity_keys.create, current_user.id, data.name, prefix, key_hash, data.permissions)
        if new_key is None:
            raise HTTPException(status_code=401, detail="User not found")
        return {"id": new_key.id, "name": new_key.name, "prefix": new_key.key_prefix,
                "permissions": new_key.permissions, "is_active": new_key.is_active,
                "last_used_at": new_key.last_used_at, "created_at": new_key.created_at, "key": full_key}

    new_key = APIKeyORM(
        name=data.name,
        key_prefix=prefix,
        key_hash=key_hash,
        permissions=data.permissions,
        user_id=current_user.id
    )
    db.add(new_key)
    db.commit()
    db.refresh(new_key)
    
    return {
        "id": new_key.id,
        "name": new_key.name,
        "prefix": new_key.key_prefix,
        "permissions": new_key.permissions,
        "is_active": new_key.is_active,
        "last_used_at": new_key.last_used_at,
        "created_at": new_key.created_at,
        "key": full_key
    }

@router.get("/settings/api-keys", response_model=List[APIKeyResponse])
def list_api_keys(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    identity_keys = _d1_repository()
    if identity_keys is not None:
        return _d1_call(identity_keys.list, current_user.id)
    keys = db.query(APIKeyORM).filter(
        APIKeyORM.is_active == 1,
        APIKeyORM.user_id == current_user.id
    ).all()
    return keys

@router.delete("/settings/api-keys/{key_id}")
def revoke_api_key(
    key_id: int, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    identity_keys = _d1_repository()
    if identity_keys is not None:
        if not _d1_call(identity_keys.revoke, current_user.id, key_id):
            raise HTTPException(status_code=404, detail="API key not found")
        return {"status": "revoked"}
    api_key = db.query(APIKeyORM).filter(
        APIKeyORM.id == key_id,
        APIKeyORM.user_id == current_user.id
    ).first()
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")
    
    api_key.is_active = 0
    db.commit()
    return {"status": "revoked"}
