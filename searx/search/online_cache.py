# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers for caching raw responses from online engines."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import typing as t

import httpx
import msgspec

from searx import logger
from searx import valkeydb
from searx.cache import ExpireCacheCfg, ExpireCacheSQLite

if t.TYPE_CHECKING:
    from searx.enginelib import Engine
    from searx.extended_types import SXNG_Response
    from searx.search.processors.online import OnlineParams

log = logger.getChild("search.online_cache")

_PERSISTENT_CACHE_DIR = "/var/cache/searxng"
_DEFAULT_MAX_BODY_SIZE = 1024 * 1024
_FALLBACK_TTL = 60 * 60
_STALE_FALLBACK_STATUS = frozenset((429, 500, 502, 503, 504))


class CachedResponse(msgspec.Struct, frozen=True):
    """Cached payload of an upstream HTTP response."""

    status_code: int
    headers: dict[str, str]
    body: bytes
    fresh_until: int


class ResponseCachePolicy(msgspec.Struct, frozen=True):
    """Per-engine response cache configuration."""

    enabled: bool
    scope: str
    ttl: int
    stale_ttl: int
    max_body_size: int

    @property
    def retain_ttl(self) -> int:
        return max(self.ttl, self.stale_ttl)


def _int_value(value: t.Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sqlite_db_url() -> str:
    if os.path.isdir(_PERSISTENT_CACHE_DIR):
        return os.path.join(_PERSISTENT_CACHE_DIR, "response-cache.db")
    return os.path.join(tempfile.gettempdir(), "sxng_response_cache.db")


class _Backend(t.Protocol):
    def get(self, key: str) -> CachedResponse | None: ...

    def set(self, key: str, record: CachedResponse, expire: int) -> bool: ...

    def delete(self, key: str) -> bool: ...


class _ValkeyBackend:
    prefix = "SearXNG:response_cache:"

    def __init__(self, client):
        self.client = client

    def _key(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def get(self, key: str) -> CachedResponse | None:
        raw = self.client.get(self._key(key))
        if not raw:
            return None
        try:
            return msgspec.msgpack.decode(raw, type=CachedResponse)
        except msgspec.DecodeError:
            log.warning("dropping undecodable response-cache entry: %s", key)
            self.delete(key)
            return None

    def set(self, key: str, record: CachedResponse, expire: int) -> bool:
        if expire <= 0:
            return False
        return bool(self.client.set(self._key(key), msgspec.msgpack.encode(record), ex=expire))

    def delete(self, key: str) -> bool:
        return bool(self.client.delete(self._key(key)))


class _SQLiteBackend:
    def __init__(self):
        self.cache = ExpireCacheSQLite.build_cache(
            ExpireCacheCfg(
                name="ONLINE_RESPONSE_CACHE",
                db_url=_sqlite_db_url(),
                MAX_VALUE_LEN=2 * 1024 * 1024,
                MAXHOLD_TIME=60 * 60 * 24 * 30,
                MAINTENANCE_PERIOD=60 * 60,
            )
        )
        # Initialize the SQLite schema before this backend instance is exposed
        # to concurrent request threads.
        self.cache.DB
        self.ctx = self.cache.normalize_name(self.cache.cfg.name)

    def get(self, key: str) -> CachedResponse | None:
        value = self.cache.get(key)
        if isinstance(value, CachedResponse):
            return value
        if value is not None:
            log.warning("dropping incompatible SQLite response-cache entry: %s", key)
            self.delete(key)
        return None

    def set(self, key: str, record: CachedResponse, expire: int) -> bool:
        return bool(self.cache.set(key=key, value=record, expire=expire))

    def delete(self, key: str) -> bool:
        if self.ctx not in self.cache.table_names:
            return False
        with self.cache.connect() as conn:
            conn.execute(f"DELETE FROM {self.ctx} WHERE key = ?", (key,))
        return True


class OnlineResponseCache:
    """Shared cache for raw responses from online engines."""

    def __init__(self):
        self._backend: _Backend | None = None
        self._backend_lock = threading.Lock()
        self._pending: set[str] = set()
        self._pending_cond = threading.Condition()

    @property
    def backend(self) -> _Backend:
        if self._backend is not None:
            return self._backend
        with self._backend_lock:
            if self._backend is None:
                client = valkeydb.client()
                self._backend = _ValkeyBackend(client) if client else _SQLiteBackend()
            return self._backend

    def get_policy(self, engine: "Engine") -> ResponseCachePolicy | None:
        cfg = getattr(engine, "response_cache", None)
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            return None

        ttl = _int_value(cfg.get("ttl", cfg.get("fresh_ttl")), _FALLBACK_TTL)
        stale_ttl = _int_value(cfg.get("stale_ttl"), ttl)
        max_body_size = _int_value(cfg.get("max_body_size"), _DEFAULT_MAX_BODY_SIZE)
        if ttl <= 0 or stale_ttl <= 0 or max_body_size <= 0:
            return None

        return ResponseCachePolicy(
            enabled=True,
            scope=str(cfg.get("scope") or engine.name),
            ttl=ttl,
            stale_ttl=max(ttl, stale_ttl),
            max_body_size=max_body_size,
        )

    def make_key(self, policy: ResponseCachePolicy, params: "OnlineParams") -> str:
        payload: dict[str, t.Any] = {
            "scope": policy.scope,
            "method": params["method"],
            "url": params["url"],
        }
        headers = params.get("headers") or {}
        if headers.get("Accept-Language"):
            payload["accept_language"] = headers["Accept-Language"]
        if headers.get("Content-Type"):
            payload["content_type"] = headers["Content-Type"]
        cookies = params.get("cookies") or {}
        if cookies:
            payload["cookies"] = cookies
        if params.get("auth"):
            payload["auth"] = params["auth"]
        if params.get("data"):
            payload["data"] = params["data"]
        if params.get("json"):
            payload["json"] = params["json"]
        if params.get("content"):
            payload["content_sha256"] = hashlib.sha256(params["content"]).hexdigest()
            payload["content_len"] = len(params["content"])

        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(self, key: str, params: "OnlineParams", fresh_only: bool = True) -> "SXNG_Response | None":
        record = self.backend.get(key)
        if record is None:
            return None
        if fresh_only and record.fresh_until < int(time.time()):
            return None
        return self._build_response(record, params)

    def set(self, key: str, policy: ResponseCachePolicy, response: "SXNG_Response") -> bool:
        if response.status_code != 200:
            return False
        body = response.content
        if len(body) > policy.max_body_size:
            log.debug("response-cache skip: body too large for key %s (%s bytes)", key, len(body))
            return False

        headers = {}
        content_type = response.headers.get("Content-Type")
        if content_type:
            headers["Content-Type"] = content_type

        record = CachedResponse(
            status_code=response.status_code,
            headers=headers,
            body=body,
            fresh_until=int(time.time()) + policy.ttl,
        )
        return self.backend.set(key, record, expire=policy.retain_ttl)

    def delete(self, key: str) -> bool:
        return self.backend.delete(key)

    def should_fallback_to_stale(self, response: "SXNG_Response") -> bool:
        return response.status_code in _STALE_FALLBACK_STATUS

    def acquire_inflight(self, key: str, wait_timeout: float) -> bool:
        deadline = time.monotonic() + max(wait_timeout, 0)
        with self._pending_cond:
            while key in self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._pending_cond.wait(remaining)
            self._pending.add(key)
            return True

    def release_inflight(self, key: str) -> None:
        with self._pending_cond:
            self._pending.discard(key)
            self._pending_cond.notify_all()

    @staticmethod
    def _build_response(record: CachedResponse, params: "OnlineParams") -> "SXNG_Response":
        request = httpx.Request(params["method"], params["url"] or "")
        response = t.cast(
            "SXNG_Response",
            httpx.Response(
                status_code=record.status_code,
                headers=record.headers,
                content=record.body,
                request=request,
            ),
        )
        response.ok = not response.is_error
        response.search_params = params
        return response


_ONLINE_RESPONSE_CACHE: OnlineResponseCache | None = None
_ONLINE_RESPONSE_CACHE_LOCK = threading.Lock()


def get_online_response_cache() -> OnlineResponseCache:
    global _ONLINE_RESPONSE_CACHE  # pylint: disable=global-statement
    if _ONLINE_RESPONSE_CACHE is not None:
        return _ONLINE_RESPONSE_CACHE
    with _ONLINE_RESPONSE_CACHE_LOCK:
        if _ONLINE_RESPONSE_CACHE is None:
            _ONLINE_RESPONSE_CACHE = OnlineResponseCache()
        return _ONLINE_RESPONSE_CACHE
