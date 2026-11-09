"""D1 persistence for the existing user-keyed launchpad layouts."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement


class D1UserLayoutsRepository:
    def __init__(self, client: D1BatchClient) -> None:
        self.client = client

    def get(self, user_key: str) -> list:
        result = self.client.batch([D1Statement(
            "SELECT layouts_json FROM user_layouts WHERE user_key=?", [user_key],
        )])[0].get("results")
        if not isinstance(result, list) or len(result) > 1 or any(not isinstance(row, dict) for row in result):
            raise D1BatchOutcomeUnknown("D1 returned an unexpected layouts response")
        if not result:
            return []
        items = json.loads(result[0]["layouts_json"])
        if not isinstance(items, list):
            raise ValueError("invalid layout data in D1")
        return items

    def put(self, user_key: str, items: list) -> None:
        encoded = json.dumps(items, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
        results = self.client.batch([D1Statement(
            "INSERT INTO user_layouts (id,user_key,layouts_json,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_key) DO UPDATE SET layouts_json=excluded.layouts_json,updated_at=excluded.updated_at "
            "RETURNING id", [str(uuid4()), user_key, encoded, now],
        )])[0].get("results")
        if (not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict)
                or not isinstance(results[0].get("id"), str)):
            raise D1BatchOutcomeUnknown("D1 returned an unexpected layouts write response")
