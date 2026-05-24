from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from backend.config import get_settings

log = logging.getLogger("headscale_client")

# ── Module-level singleton ────────────────────────────────────────────────────

_client_instance: Optional["HeadscaleClient"] = None


def get_headscale_client() -> "HeadscaleClient":
    """
    Returns the process-wide HeadscaleClient singleton.
    Creates it on first call using settings from the environment.
    """
    global _client_instance
    if _client_instance is None:
        _client_instance = HeadscaleClient.from_settings()
    return _client_instance


async def close_headscale_client() -> None:
    """
    Gracefully closes the underlying httpx.AsyncClient.
    Call this from the FastAPI lifespan shutdown or worker teardown.

    Example (FastAPI lifespan):
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            yield
            await close_headscale_client()
    """
    global _client_instance
    if _client_instance is not None:
        await _client_instance.aclose()
        _client_instance = None


class HeadscaleClient:
    """
    Thin async wrapper around the Headscale v1 REST API.

    A single httpx.AsyncClient is shared across all method calls on this
    instance, enabling connection reuse and persistent TLS sessions.

    All methods raise RuntimeError on unexpected API responses so callers
    can catch a single exception type regardless of which operation failed.

    Prefer the module-level `get_headscale_client()` over direct instantiation
    so the connection pool is shared across the whole process.
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        # Strip trailing slash so every endpoint path can start with /
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        )

    @classmethod
    def from_settings(cls) -> "HeadscaleClient":
        settings = get_settings()
        return cls(
            base_url=settings.HEADSCALE_API_URL,
            api_key=settings.HEADSCALE_API_KEY,
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client and release connections."""
        await self._http.aclose()

    # ── Internal HTTP helpers ─────────────────────────────────────────────────

    async def _get(self, path: str, **params) -> Any:
        resp = await self._http.get(path, params=params)
        self._raise_for_status(resp, path)
        return resp.json()

    async def _post(self, path: str, body: dict) -> Any:
        resp = await self._http.post(path, json=body)
        self._raise_for_status(resp, path)
        return resp.json()

    async def _delete(self, path: str) -> Any:
        resp = await self._http.delete(path)
        self._raise_for_status(resp, path)
        # DELETE responses are often empty — return {} if body is blank
        try:
            return resp.json()
        except Exception:
            return {}

    @staticmethod
    def _raise_for_status(resp: httpx.Response, path: str) -> None:
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Headscale API error on {path}: "
                f"HTTP {resp.status_code} — {resp.text}"
            )

    # ── Users ─────────────────────────────────────────────────────────────────

    async def ensure_user(self, username: str) -> Dict[str, Any]:
        """
        Ensures a Headscale user with the given username exists.
        Returns {"id": int, "name": str}.
        """
        log.info("Ensuring Headscale user exists: %s", username)

        data = await self._get("/api/v1/user")
        users = data.get("users") or []

        for u in users:
            if u.get("name") == username:
                uid = int(u["id"])
                log.info("Headscale user already exists: %s (id=%s)", username, uid)
                return {"id": uid, "name": username}

        # User not found — create it
        log.info("Headscale user not found, creating: %s", username)
        created = await self._post("/api/v1/user", {"name": username})
        user = created.get("user") or created

        uid = int(user["id"])
        log.info("Headscale user created: %s (id=%s)", username, uid)
        return {"id": uid, "name": username}

    # ── Pre-auth keys ─────────────────────────────────────────────────────────

    async def create_preauth_key(
        self,
        hs_user_id: int,
        expiration: datetime,
        reusable: bool,
        ephemeral: bool,
        acl_tags: List[str],
    ) -> Dict[str, Any]:
        """
        Creates a Headscale pre-auth key for the given user.
        Returns {"key": str, "id": Optional[str], "created_at": Optional[datetime]}.
        """
        if not isinstance(hs_user_id, int):
            raise TypeError(
                f"hs_user_id must be an int, got {type(hs_user_id).__name__}: {hs_user_id!r}"
            )

        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=timezone.utc)

        seconds = int((expiration - datetime.now(timezone.utc)).total_seconds())
        if seconds <= 60:
            raise ValueError(f"Expiration too soon to mint a usable key (only {seconds}s remaining)")

        body = {
            "user": str(hs_user_id),
            "reusable": reusable,
            "ephemeral": ephemeral,
            "expiration": expiration.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "aclTags": acl_tags or [],
        }

        log.info(
            "Creating preauth key for Headscale user_id=%s expiration=%s",
            hs_user_id, body["expiration"],
        )

        data = await self._post("/api/v1/preauthkey", body)
        preauth_key = data.get("preAuthKey") or data

        key = preauth_key.get("key")
        if not key:
            raise RuntimeError(
                f"Headscale API response did not contain a key field: {data}"
            )

        created_at = self._parse_dt(preauth_key.get("createdAt"))

        log.info("Preauth key created successfully (id=%s)", preauth_key.get("id"))
        return {
            "key": key,
            "id": preauth_key.get("id"),
            "created_at": created_at,
        }

    # ── Nodes ─────────────────────────────────────────────────────────────────

    async def list_nodes(self) -> List[Dict[str, Any]]:
        """Returns the full list of nodes registered in Headscale."""
        data = await self._get("/api/v1/node")
        return data.get("nodes") or []

    async def delete_node(self, node_id: str) -> None:
        """Deletes a single node by its numeric string ID."""
        log.info("Deleting Headscale node id=%s", node_id)
        await self._delete(f"/api/v1/node/{node_id}")
        log.info("Headscale node id=%s deleted", node_id)

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_dt(value: Any) -> Optional[datetime]:
        """Safely parse a datetime from whatever Headscale returns."""
        if not value:
            return None
        if isinstance(value, str):
            s = value
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(s)
            except ValueError:
                return None
        # Protobuf Timestamp object: {"seconds": int, "nanos": int}
        if isinstance(value, dict) and "seconds" in value:
            return datetime.fromtimestamp(value["seconds"], tz=timezone.utc)
        return None