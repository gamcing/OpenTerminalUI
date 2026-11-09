from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from backend.api.deps import get_db
from backend.auth.deps import get_current_user
from backend.cloudflare.d1 import D1BatchError
from backend.cloudflare.watchlist_storage import get_d1_watchlists
from backend.db.models import WatchlistORM

router = APIRouter(prefix="/api/watchlists", tags=["watchlists"])


def _d1_repository():
    try:
        return get_d1_watchlists()
    except (RuntimeError, ValueError):
        raise HTTPException(status_code=503, detail="Watchlist storage unavailable") from None


def _d1_call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except (D1BatchError, ValueError, KeyError):
        raise HTTPException(status_code=503, detail="Watchlist storage unavailable") from None


def _d1_response(row):
    if row is None:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return {"id": row.id, "name": row.name, "symbols": row.symbols_json,
            "column_config": row.column_config_json, "created_at": row.created_at}


class WatchlistBase(BaseModel):
    name: str
    column_config: dict = Field(default_factory=dict)


class WatchlistCreate(WatchlistBase):
    symbols: List[str] = Field(default_factory=list)


class WatchlistUpdate(BaseModel):
    name: Optional[str] = None
    symbols: Optional[List[str]] = None
    column_config: Optional[dict] = None


class WatchlistResponse(WatchlistBase):
    id: str
    symbols: List[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


@router.get("", response_model=List[WatchlistResponse])
def list_watchlists(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    user_id = current_user.id
    d1 = _d1_repository()
    if d1 is not None:
        rows = _d1_call(d1.list_or_create_default, user_id)
        if not rows:
            raise HTTPException(status_code=401, detail="User not found")
        return [_d1_response(row) for row in rows]
    items = db.query(WatchlistORM).filter(WatchlistORM.user_id == user_id).all()

    # If no watchlists exist, create a default one
    if not items:
        default_wl = WatchlistORM(
            id=str(uuid4()),
            user_id=user_id,
            name="Default Watchlist",
            symbols_json=["AAPL", "MSFT", "TSLA", "GOOGL"],
            column_config_json={},
            created_at=datetime.now(timezone.utc)
        )
        db.add(default_wl)
        db.commit()
        db.refresh(default_wl)
        items = [default_wl]

    results = []
    for item in items:
        results.append({
            "id": item.id,
            "name": item.name,
            "symbols": item.symbols_json,
            "column_config": item.column_config_json,
            "created_at": item.created_at
        })
    return results


@router.post("", response_model=WatchlistResponse)
def create_watchlist(payload: WatchlistCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    user_id = current_user.id
    d1 = _d1_repository()
    if d1 is not None:
        symbols = list(dict.fromkeys(s.strip().upper() for s in (payload.symbols or []) if s and s.strip()))
        row = _d1_call(d1.create, user_id, payload.name, symbols, payload.column_config)
        if row is None:
            raise HTTPException(status_code=401, detail="User not found")
        return _d1_response(row)
    new_wl = WatchlistORM(
        id=str(uuid4()),
        user_id=user_id,
        name=payload.name,
        # Honour the symbols sent on create (previously always stored []).
        symbols_json=list(dict.fromkeys(s.strip().upper() for s in (payload.symbols or []) if s and s.strip())),
        column_config_json=payload.column_config,
        created_at=datetime.now(timezone.utc)
    )
    db.add(new_wl)
    db.commit()
    db.refresh(new_wl)
    return {
        "id": new_wl.id,
        "name": new_wl.name,
        "symbols": new_wl.symbols_json,
        "column_config": new_wl.column_config_json,
        "created_at": new_wl.created_at
    }


@router.put("/{watchlist_id}", response_model=WatchlistResponse)
def update_watchlist(watchlist_id: str, payload: WatchlistUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    d1 = _d1_repository()
    if d1 is not None:
        return _d1_response(_d1_call(d1.update, current_user.id, watchlist_id, name=payload.name,
                                     symbols=payload.symbols, config=payload.column_config))
    wl = db.query(WatchlistORM).filter(WatchlistORM.id == watchlist_id, WatchlistORM.user_id == current_user.id).first()
    if not wl:
        raise HTTPException(status_code=404, detail="Watchlist not found")

    if payload.name is not None:
        wl.name = payload.name
    if payload.symbols is not None:
        wl.symbols_json = payload.symbols
    if payload.column_config is not None:
        wl.column_config_json = payload.column_config

    db.commit()
    db.refresh(wl)
    return {
        "id": wl.id,
        "name": wl.name,
        "symbols": wl.symbols_json,
        "column_config": wl.column_config_json,
        "created_at": wl.created_at
    }


@router.delete("/{watchlist_id}")
def delete_watchlist(watchlist_id: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    d1 = _d1_repository()
    if d1 is not None:
        if not _d1_call(d1.delete, current_user.id, watchlist_id):
            raise HTTPException(status_code=404, detail="Watchlist not found")
        return {"status": "deleted"}
    wl = db.query(WatchlistORM).filter(WatchlistORM.id == watchlist_id, WatchlistORM.user_id == current_user.id).first()
    if not wl:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    db.delete(wl)
    db.commit()
    return {"status": "deleted"}


@router.post("/{watchlist_id}/symbols", response_model=WatchlistResponse)
def add_symbols(watchlist_id: str, symbols: List[str], db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    d1 = _d1_repository()
    if d1 is not None:
        return _d1_response(_d1_call(d1.add_symbols, current_user.id, watchlist_id, symbols))
    wl = db.query(WatchlistORM).filter(WatchlistORM.id == watchlist_id, WatchlistORM.user_id == current_user.id).first()
    if not wl:
        raise HTTPException(status_code=404, detail="Watchlist not found")

    current_symbols = list(wl.symbols_json)
    for s in symbols:
        s_upper = s.strip().upper()
        if s_upper not in current_symbols:
            current_symbols.append(s_upper)

    wl.symbols_json = current_symbols
    db.commit()
    db.refresh(wl)
    return {
        "id": wl.id,
        "name": wl.name,
        "symbols": wl.symbols_json,
        "column_config": wl.column_config_json,
        "created_at": wl.created_at
    }


@router.delete("/{watchlist_id}/symbols/{symbol}", response_model=WatchlistResponse)
def remove_symbol(watchlist_id: str, symbol: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    d1 = _d1_repository()
    if d1 is not None:
        return _d1_response(_d1_call(d1.remove_symbol, current_user.id, watchlist_id, symbol))
    wl = db.query(WatchlistORM).filter(WatchlistORM.id == watchlist_id, WatchlistORM.user_id == current_user.id).first()
    if not wl:
        raise HTTPException(status_code=404, detail="Watchlist not found")

    s_upper = symbol.strip().upper()
    current_symbols = [s for s in wl.symbols_json if s != s_upper]

    wl.symbols_json = current_symbols
    db.commit()
    db.refresh(wl)
    return {
        "id": wl.id,
        "name": wl.name,
        "symbols": wl.symbols_json,
        "column_config": wl.column_config_json,
        "created_at": wl.created_at
    }
