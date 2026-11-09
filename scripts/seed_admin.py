#!/usr/bin/env python
"""Idempotently seed an initial admin user so a fresh install can log in right away.

Reads ``BOOTSTRAP_ADMIN_EMAIL`` / ``BOOTSTRAP_ADMIN_PASSWORD`` from the environment
(or the repo-root ``.env``). Safety rules:

* No-op if **any** user already exists — never clobbers a real deployment.
* No-op if no password is configured — never creates an account with a blank /
  guessable password.

The legacy SQLAlchemy path normally exits 0. The Cloudflare snapshot path
requires a valid initial administrator and exits nonzero on an unknown commit
or if existing users have no administrator. An explicitly selected D1 identity
store also reports failure instead of silently seeding a different store.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Make the repo importable when this file is run directly (python scripts/seed_admin.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config.env import load_local_env


def _seed_cloudflare_snapshot_admin(email: str, password: str) -> int:
    """Fail closed on an empty or unreadable Cloudflare snapshot database."""
    db = None
    try:
        from passlib.context import CryptContext
        from backend.models.user import User, UserRole
        from backend.shared.db import SessionLocal, snapshot_storage

        if snapshot_storage is None:
            raise RuntimeError("snapshot storage unavailable")
        db = SessionLocal()
        if db.query(User).filter(User.role == UserRole.ADMIN).first() is not None:
            print("[seed-admin] an administrator already exists; skipping.")
            return 0
        if db.query(User).first() is not None:
            print("[seed-admin] users exist but no administrator; manual recovery required.")
            return 1
        if (not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
                or len(password) < 8 or len(password.encode("utf-8")) > 72):
            print("[seed-admin] a valid bootstrap email and password are required for a new database.")
            return 1
        db.add(User(email=email, hashed_password=CryptContext(schemes=["bcrypt"], deprecated="auto").hash(password),
                    role=UserRole.ADMIN))
        db.commit()
        print("[seed-admin] initial administrator created.")
        return 0
    except Exception:
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
        # The commit may have succeeded remotely. A restart must restore and
        # fence the database before deciding whether the admin already exists.
        print("[seed-admin] database initialization failed; startup stopped.")
        return 1
    finally:
        if db is not None:
            db.close()


def main() -> int:
    load_local_env()
    email = (os.getenv("BOOTSTRAP_ADMIN_EMAIL") or "").strip().lower()
    password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or ""

    if (os.getenv("OPENTERMINALUI_CF_CONTAINER") == "1"
            and os.getenv("OPENTERMINALUI_DATABASE_STORE") == "d1_snapshot"):
        return _seed_cloudflare_snapshot_admin(email, password)

    if not email or not password:
        print("[seed-admin] BOOTSTRAP_ADMIN_EMAIL/PASSWORD not set; skipping admin seed.")
        return 0

    # Experimental D1 identity is limited to isolated local tests.  Do not
    # fall back to SQLAlchemy if that explicitly selected store is unavailable.
    try:
        from backend.cloudflare.d1 import D1BatchError
        from backend.cloudflare.identity_storage import get_d1_identity
        identity = get_d1_identity()
        if identity is not None:
            from passlib.context import CryptContext
            created = identity.seed_first_admin(email, CryptContext(schemes=["bcrypt"], deprecated="auto").hash(password))
            print("[seed-admin] D1 admin created." if created else "[seed-admin] a D1 user already exists; skipping.")
            return 0
    except (D1BatchError, RuntimeError, ValueError, ImportError):
        print("[seed-admin] identity storage unavailable; refusing fallback.")
        return 1

    try:
        from passlib.context import CryptContext
        from backend.models.user import User, UserRole
        from backend.shared.db import Base, SessionLocal, engine
    except Exception as exc:  # pragma: no cover - defensive, never block startup
        print(f"[seed-admin] dependencies unavailable ({exc}); skipping.")
        return 0

    # Make sure the users table exists (idempotent; harmless if migrations already ran).
    try:
        Base.metadata.create_all(bind=engine, tables=[User.__table__])
    except Exception:
        pass

    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            print("[seed-admin] a user already exists; skipping admin seed.")
            return 0
        db.add(
            User(
                email=email,
                hashed_password=pwd_context.hash(password),
                role=UserRole.ADMIN,
            )
        )
        db.commit()
        print(f"[seed-admin] created initial admin account: {email}")
    except Exception as exc:  # pragma: no cover - defensive
        db.rollback()
        print(f"[seed-admin] could not seed admin ({exc}); skipping.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
