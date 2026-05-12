"""HippoTransport — stdlib HTTP transport for the Hippo server.

Why stdlib (urllib) and not requests/httpx?
    The SDK promises zero non-stdlib deps so it can be vendored into agents
    that have strict dependency policies. Loss of niceties is small at this
    surface area.

Behavior preserved from the legacy plugin _post/_get helpers:
    - Swallow all network errors, return None to caller.
    - Caller decides whether to fall back to WAL.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .config import HippoConfig

logger = logging.getLogger("hippo_sdk.transport")


class HippoTransport:
    """Thin HTTP wrapper around the Hippo REST API."""

    def __init__(self, config: HippoConfig):
        self.config = config

    def post(self, path: str, payload: dict, timeout: float | None = None) -> dict | None:
        """POST JSON to the server. Returns parsed JSON or None on failure."""
        t = timeout if timeout is not None else self.config.write_timeout
        url = f"{self.config.base_url}{path}"
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers=self.config.headers(),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=t) as resp:
                body = resp.read()
                if not body:
                    return {}
                return json.loads(body)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            logger.debug("hippo POST %s failed: %s", path, e)
            return None
        except json.JSONDecodeError as e:
            logger.warning("hippo POST %s returned non-JSON: %s", path, e)
            return None
        except Exception as e:  # last-resort guard — never propagate to agent
            logger.warning("hippo POST %s unexpected error: %s", path, e)
            return None

    def get(self, path: str, timeout: float | None = None) -> dict | None:
        """GET JSON from the server."""
        t = timeout if timeout is not None else self.config.search_timeout
        url = f"{self.config.base_url}{path}"
        try:
            req = urllib.request.Request(
                url,
                method="GET",
                headers=self.config.headers(),
            )
            with urllib.request.urlopen(req, timeout=t) as resp:
                body = resp.read()
                if not body:
                    return {}
                return json.loads(body)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            logger.debug("hippo GET %s failed: %s", path, e)
            return None
        except json.JSONDecodeError as e:
            logger.warning("hippo GET %s returned non-JSON: %s", path, e)
            return None
        except Exception as e:
            logger.warning("hippo GET %s unexpected error: %s", path, e)
            return None

    def healthy(self) -> bool:
        """Quick health probe used by client to decide degraded mode."""
        r = self.get("/health", timeout=1.0)
        return bool(r and r.get("status") == "ok")
