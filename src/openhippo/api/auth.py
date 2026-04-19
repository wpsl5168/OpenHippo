"""Bearer token authentication middleware."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Callable

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Paths that bypass auth
PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


def _hash_token(token: str) -> str:
    """SHA-256 hash a token for safe comparison."""
    return hashlib.sha256(token.encode()).hexdigest()


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validates Bearer tokens against configured token list.
    
    Supports both plaintext and pre-hashed tokens in config.
    Token comparison uses constant-time hmac.compare_digest.
    """

    def __init__(self, app, tokens: list[dict] | None = None, enabled: bool = False):
        super().__init__(app)
        self.enabled = enabled
        # Build lookup: hash → {name, scopes}
        self._token_map: dict[str, dict] = {}
        if tokens:
            for t in tokens:
                raw = t.get("token", "")
                h = raw if raw.startswith("sha256:") else _hash_token(raw)
                self._token_map[h.removeprefix("sha256:")] = {
                    "name": t.get("name", "unnamed"),
                    "scopes": set(t.get("scopes", ["*"])),
                }
        if enabled:
            logger.info("Auth enabled with %d token(s)", len(self._token_map))

    async def dispatch(self, request: Request, call_next: Callable):
        if not self.enabled:
            return await call_next(request)

        # Skip public paths
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        # Extract token
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth_header[7:]  # Strip "Bearer "
        token_hash = _hash_token(token)

        # Constant-time lookup
        matched = None
        for stored_hash, info in self._token_map.items():
            if hmac.compare_digest(token_hash, stored_hash):
                matched = info
                break

        if not matched:
            logger.warning("Auth failed: invalid token from %s", request.client.host if request.client else "unknown")
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid token"},
            )

        # Attach identity to request state
        request.state.auth_name = matched["name"]
        request.state.auth_scopes = matched["scopes"]
        return await call_next(request)
