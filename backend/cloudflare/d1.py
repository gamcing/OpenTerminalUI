"""Client for the experimental D1 atomic-batch gateway.

A batch is one transaction, not an interactive session. Callers must assemble
all statements before sending them and must not retry an uncertain write blindly.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit


class D1BatchError(RuntimeError):
    """Base class for a gateway failure; details never include credentials."""


class D1BatchRejected(D1BatchError):
    """The gateway rejected the request before handing it to D1."""


class D1BatchOutcomeUnknown(D1BatchError):
    """The client cannot know whether D1 committed; do not retry blindly."""


@dataclass(frozen=True)
class D1Statement:
    sql: str
    params: Sequence[Any] = ()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _open(request: urllib.request.Request, timeout: float):  # type: ignore[no-untyped-def]
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


class D1BatchClient:
    def __init__(
        self,
        gateway_url: str,
        token: str,
        *,
        timeout: float = 30,
        allow_insecure_localhost: bool = False,
        allow_intercepted_container_origin: bool = False,
        opener: Callable[..., Any] = _open,
    ) -> None:
        url = urlsplit(gateway_url)
        local_http = allow_insecure_localhost and url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        intercepted = (allow_intercepted_container_origin and url.scheme == "http"
                       and url.hostname == "openterminal-d1.internal" and url.port is None)
        if ((url.scheme != "https" and not local_http and not intercepted) or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ("", "/")):
            raise ValueError("gateway_url must be an HTTPS origin")
        if len(token) < 32:
            raise ValueError("gateway token must contain at least 32 characters")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._endpoint = gateway_url.rstrip("/") + "/batch"
        self._token = token
        self._timeout = timeout
        self._opener = opener

    def batch(self, statements: Sequence[D1Statement]) -> list[dict[str, Any]]:
        if not 1 <= len(statements) <= 50:
            raise ValueError("a D1 batch must contain between 1 and 50 statements")
        payload = {"statements": [{"sql": s.sql, "params": list(s.params)} for s in statements]}
        body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(body) > 1024 * 1024:
            raise ValueError("D1 batch exceeds the gateway request size limit")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in {400, 401, 404, 405, 413, 415}:
                raise D1BatchRejected(f"D1 gateway rejected the batch (HTTP {exc.code})") from None
            raise D1BatchOutcomeUnknown(f"D1 batch outcome unknown (HTTP {exc.code})") from None
        except (OSError, ValueError, TypeError):
            raise D1BatchOutcomeUnknown("D1 batch outcome unknown") from None
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list) or len(results) != len(statements) or any(
            not isinstance(item, dict) or item.get("success") is not True for item in results
        ):
            raise D1BatchOutcomeUnknown("D1 gateway returned an unexpected response")
        return results
