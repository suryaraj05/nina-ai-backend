"""Hosted NINA Console (hybrid model) for onboarding and key management.

This module intentionally ships as a lightweight in-memory control plane so
teams can run and extend it without external dependencies.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import logging

from .pool import NinaPool
from .crypto import seal_llm_config, unseal_llm_config
from .console_pack import (
    build_onboarding_pack_files,
    resolve_site_fields,
    zip_onboarding_pack,
)
from .contract_validate import validate_executable
from .generator.pipeline import run_pipeline


def _now_ts() -> int:
    return int(time.time())


def _slug(text: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in text.strip())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-") or "site"


def _parse_origin(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None


def _rand_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _hash_key(raw: str) -> str:
    secret = os.environ.get("NINA_CONSOLE_KEY_HASH_SECRET")
    if not secret:
        import warnings
        warnings.warn(
            "NINA_CONSOLE_KEY_HASH_SECRET is not set — using insecure default. "
            "Set this env var before running in production.",
            RuntimeWarning,
            stacklevel=2,
        )
        secret = "nina-console-dev"
    return hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()


def _issue_key(prefix: str) -> tuple[str, str]:
    visible = f"{prefix}{secrets.token_urlsafe(24)}"
    return visible, _hash_key(visible)


# ── SSRF guard ───────────────────────────────────────────────────────────────
_BLOCKED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / AWS metadata
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _validate_external_url(url: str, label: str = "URL") -> None:
    """Raise HTTPException 400 if the URL targets private/loopback addresses."""
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid {label}.")
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail=f"{label} must use http or https.")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise HTTPException(status_code=400, detail=f"{label} must have a hostname.")
    if host == "localhost":
        raise HTTPException(status_code=400, detail=f"Internal {label.lower()} is not allowed.")
    try:
        addr = ipaddress.ip_address(host)
        for net in _BLOCKED_NETS:
            if addr in net:
                raise HTTPException(status_code=400, detail=f"Internal {label.lower()} is not allowed.")
    except ValueError:
        pass  # hostname, not a bare IP literal — DNS resolution still poses risk,
              # but blocking IP literals is the critical fix for cloud metadata attacks.


# ── Safe local path resolver ─────────────────────────────────────────────────
_BLOCKED_PATH_PREFIXES = ("/etc/", "/proc/", "/sys/", "/root/", "/boot/", "/dev/")


def _validate_local_path(raw: str) -> Path:
    """Resolve a user-supplied path and block access to system directories."""
    try:
        p = Path(raw).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path.")
    s = str(p)
    for prefix in _BLOCKED_PATH_PREFIXES:
        if s.startswith(prefix):
            raise HTTPException(status_code=400, detail="Path is in a restricted system directory.")
    return p


# ── In-memory rate limiters ──────────────────────────────────────────────────
class _RateLimiter:
    """Sliding-window rate limiter. NOT shared across processes — use Redis for multi-instance."""

    def __init__(self, per_minute: int) -> None:
        self._max = per_minute
        self._lock = threading.Lock()
        self._hits: dict[str, collections.deque] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        cutoff = now - 60.0
        with self._lock:
            q = self._hits.setdefault(key, collections.deque())
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self._max:
                return False
            q.append(now)
            return True


_IP_LIMITER  = _RateLimiter(per_minute=60)   # per source IP  (DoS)
_KEY_LIMITER = _RateLimiter(per_minute=200)  # per API key    (quota exhaustion)

_MAX_WEBHOOK_EVENTS = 500

from .plans import PLAN_LIMITS as _PLAN_LIMITS, current_period as _current_period, VALID_PLANS as _VALID_PLANS

# ── Store write lock — prevents concurrent save() corruption ─────────────────
_STORE_WRITE_LOCK = threading.Lock()

# ── Structured JSON logging ──────────────────────────────────────────────────
class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log: dict[str, Any] = {
            "ts": int(record.created),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        for key in ("site_id", "ip", "method", "path", "status", "duration_ms", "error_code", "plan"):
            val = getattr(record, key, None)
            if val is not None:
                log[key] = val
        return json.dumps(log, ensure_ascii=False)


_log_handler = logging.StreamHandler()
_log_handler.setFormatter(_JSONFormatter())
logger = logging.getLogger("nina.console")
logger.addHandler(_log_handler)
logger.setLevel(logging.INFO)
logger.propagate = False

# ── Optional Sentry error tracking ───────────────────────────────────────────
_sentry_dsn = os.environ.get("SENTRY_DSN")
if _sentry_dsn:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=_sentry_dsn, traces_sample_rate=0.05, profiles_sample_rate=0.01)
        logger.info("Sentry initialized")
    except ImportError:
        import warnings
        warnings.warn("SENTRY_DSN is set but sentry-sdk is not installed. Run: pip install sentry-sdk", RuntimeWarning)

# ── In-process metrics ────────────────────────────────────────────────────────
class _Metrics:
    """Lightweight in-process counters. Resets on restart. Use Prometheus for persistence."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.query_total    = 0
        self.query_ok       = 0
        self.query_error    = 0
        self.quota_exceeded = 0
        self.rate_limited   = 0
        self.auth_failed    = 0
        self._latencies: collections.deque = collections.deque(maxlen=1000)

    def record(self, *, ok: bool, latency_ms: int, error_code: str = "") -> None:
        with self._lock:
            self.query_total += 1
            if ok:
                self.query_ok += 1
            else:
                self.query_error += 1
            if error_code == "QUOTA_EXCEEDED":
                self.quota_exceeded += 1
            elif error_code == "RATE_LIMITED":
                self.rate_limited += 1
            elif error_code in ("UNAUTHORIZED",):
                self.auth_failed += 1
            self._latencies.append(latency_ms)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            lats = sorted(self._latencies)
            n = len(lats)
            return {
                "queryTotal":    self.query_total,
                "queryOk":       self.query_ok,
                "queryError":    self.query_error,
                "quotaExceeded": self.quota_exceeded,
                "rateLimited":   self.rate_limited,
                "authFailed":    self.auth_failed,
                "latencyP50Ms":  lats[n // 2]       if n else None,
                "latencyP95Ms":  lats[int(n * 0.95)] if n else None,
                "latencyAvgMs":  int(sum(lats) / n)  if n else None,
            }


METRICS = _Metrics()


@dataclass
class ConsoleStore:
    orgs: dict[str, dict[str, Any]] = field(default_factory=dict)
    sites: dict[str, dict[str, Any]] = field(default_factory=dict)
    api_keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    cli_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    webhook_events: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, dict[str, Any]] = field(default_factory=dict)
    store_path: Path | None = field(default=None, repr=False)

    def load(self, path: Path) -> None:
        """Load persisted state from `path` if present; remember it for future saves."""
        self.store_path = path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.orgs = data.get("orgs", {})
        self.sites = data.get("sites", {})
        self.api_keys = data.get("api_keys", {})
        self.cli_tokens = data.get("cli_tokens", {})
        self.webhook_events = data.get("webhook_events", [])
        self.usage = data.get("usage", {})

    def save(self) -> None:
        """Snapshot state to disk atomically. No-op if load() was never called."""
        if not self.store_path:
            return
        with _STORE_WRITE_LOCK:
            try:
                self.store_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.store_path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(
                        {
                            "orgs": self.orgs,
                            "sites": self.sites,
                            "api_keys": self.api_keys,
                            "cli_tokens": self.cli_tokens,
                            "webhook_events": self.webhook_events,
                            "usage": self.usage,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                tmp.replace(self.store_path)
            except OSError as exc:
                import warnings
                warnings.warn(f"ConsoleStore: failed to persist state — {exc}", RuntimeWarning, stacklevel=2)

    def create_org(self, name: str, owner_email: str | None) -> dict[str, Any]:
        org_id = _rand_id("org")
        raw_token, token_digest = _issue_key("dt_")
        org = {
            "id": org_id,
            "name": name,
            "ownerEmail": owner_email,
            "dashboardTokenDigest": token_digest,
            "dashboardTokenPrefix": raw_token[:16],
            "createdAt": _now_ts(),
        }
        self.orgs[org_id] = org
        self.save()
        # dashboardToken is returned ONCE and never stored in plain text.
        # Merchants must save it — recovery requires the admin secret.
        return {**org, "dashboardToken": raw_token}

    def rotate_dashboard_token(self, org_id: str) -> dict[str, Any]:
        """Generate a new dashboard token. Requires admin secret — called by operator on merchant request."""
        if org_id not in self.orgs:
            raise ValueError("Unknown org_id")
        raw_token, token_digest = _issue_key("dt_")
        self.orgs[org_id]["dashboardTokenDigest"] = token_digest
        self.orgs[org_id]["dashboardTokenPrefix"] = raw_token[:16]
        self.orgs[org_id]["tokenRotatedAt"] = _now_ts()
        self.save()
        return {"orgId": org_id, "dashboardToken": raw_token}

    def verify_dashboard_token(self, raw_token: str) -> dict[str, Any] | None:
        """Return the org if the dashboard token is valid, else None."""
        digest = _hash_key(raw_token)
        for org in self.orgs.values():
            stored = org.get("dashboardTokenDigest", "")
            if stored and hmac.compare_digest(stored, digest):
                return org
        return None

    def create_site(
        self,
        org_id: str,
        name: str,
        base_url: str,
        *,
        locales: list[str] | None = None,
        markets: list[str] | None = None,
        allowed_origins: list[str] | None = None,
        currency: str | None = None,
        plan: str = "free",
    ) -> dict[str, Any]:
        if org_id not in self.orgs:
            raise ValueError("Unknown org_id")
        if plan not in _PLAN_LIMITS:
            raise ValueError(f"Unknown plan: {plan}. Choose from {list(_PLAN_LIMITS)}")
        site_id = _slug(name)
        if site_id in self.sites:
            site_id = f"{site_id}-{secrets.token_hex(4)}"
        origin = _parse_origin(base_url)
        site = {
            "id": site_id,
            "orgId": org_id,
            "name": name,
            "baseUrl": base_url,
            "plan": plan,
            "currency": currency or "INR",
            "locales": locales or ["en"],
            "markets": markets or [],
            "allowedOrigins": allowed_origins or ([origin] if origin else []),
            "verification": {"sandbox": "verified", "production": "pending"},
            "createdAt": _now_ts(),
        }
        self.sites[site_id] = site
        self.save()
        return site

    def set_plan(self, site_id: str, plan: str) -> None:
        if site_id not in self.sites:
            raise ValueError("Unknown site_id")
        if plan not in _PLAN_LIMITS:
            raise ValueError(f"Unknown plan: {plan}")
        self.sites[site_id]["plan"] = plan
        self.save()

    def enforce_quota(self, site_id: str) -> tuple[bool, int, int | None]:
        """Returns (allowed, current_calls, monthly_limit). limit=None means unlimited."""
        site = self.sites.get(site_id)
        if not site:
            return False, 0, 0
        plan = site.get("plan", "free")
        limit = _PLAN_LIMITS.get(plan)
        period = _current_period()
        rec = self.usage.get(site_id, {})
        # Calls from a previous billing period don't count toward this month's quota.
        current = rec.get("calls", 0) if rec.get("period") == period else 0
        if limit is not None and current >= limit:
            return False, current, limit
        return True, current, limit

    def issue_api_key(self, site_id: str, env: str, kind: str) -> dict[str, Any]:
        if site_id not in self.sites:
            raise ValueError("Unknown site_id")
        if env not in {"test", "live"}:
            raise ValueError("env must be test|live")
        if kind not in {"pk", "sk"}:
            raise ValueError("kind must be pk|sk")
        raw, digest = _issue_key(f"{kind}_{env}_")
        key_id = _rand_id("key")
        record = {
            "id": key_id,
            "siteId": site_id,
            "environment": env,
            "kind": kind,
            "prefix": raw[:14],
            "digest": digest,
            "revoked": False,
            "createdAt": _now_ts(),
        }
        self.api_keys[key_id] = record
        self.save()
        return {**record, "token": raw}

    def issue_cli_token(self, org_id: str, label: str) -> dict[str, Any]:
        if org_id not in self.orgs:
            raise ValueError("Unknown org_id")
        raw, digest = _issue_key("nk_")
        token_id = _rand_id("token")
        record = {
            "id": token_id,
            "orgId": org_id,
            "label": label,
            "digest": digest,
            "prefix": raw[:14],
            "revoked": False,
            "createdAt": _now_ts(),
        }
        self.cli_tokens[token_id] = record
        self.save()
        return {**record, "token": raw}

    def attach_contract(self, site_id: str, contract: dict[str, Any]) -> None:
        if site_id not in self.sites:
            raise ValueError("Unknown site_id")
        self.sites[site_id]["agentContract"] = contract
        self.save()

    def attach_llm_config(self, site_id: str, llm_config: dict[str, Any]) -> None:
        if site_id not in self.sites:
            raise ValueError("Unknown site_id")
        self.sites[site_id]["llmConfig"] = seal_llm_config(llm_config)
        self.save()

    def record_usage(self, site_id: str) -> None:
        period = _current_period()
        rec = self.usage.setdefault(site_id, {"calls": 0, "lastCallAt": None, "period": period})
        if rec.get("period") != period:
            rec["calls"] = 0
            rec["period"] = period
        rec["calls"] += 1
        rec["lastCallAt"] = _now_ts()

    def get_usage(self, site_id: str) -> dict[str, Any]:
        period = _current_period()
        rec = self.usage.get(site_id, {})
        if rec.get("period") != period:
            return {"calls": 0, "lastCallAt": None, "period": period}
        return rec

    def resolve_key_to_site(
        self, raw_key: str, origin: str | None
    ) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
        """Verify a publishable key and return (ok, site_record, error).

        Extends verify_publishable_key to also return the matched site so
        the multi-tenant query endpoint can load contract + llmConfig without
        a second store lookup.
        """
        digest = _hash_key(raw_key)
        for rec in self.api_keys.values():
            if rec["kind"] != "pk" or rec["revoked"]:
                continue
            if not hmac.compare_digest(rec["digest"], digest):
                continue
            site = self.sites.get(rec["siteId"])
            if not site:
                continue
            if origin and origin not in (site.get("allowedOrigins") or []):
                return False, None, {"code": "UNAUTHORIZED", "message": "Origin is not allowed for this key."}
            return True, site, None
        return False, None, {"code": "UNAUTHORIZED", "message": "Unknown or revoked API key."}

    def verify_publishable_key(self, raw_key: str, site_id: str | None, origin: str | None) -> tuple[bool, dict[str, Any] | None]:
        digest = _hash_key(raw_key)
        for rec in self.api_keys.values():
            if rec["kind"] != "pk" or rec["revoked"]:
                continue
            if not hmac.compare_digest(rec["digest"], digest):
                continue
            site = self.sites.get(rec["siteId"])
            if not site:
                continue
            if site_id and site["id"] != site_id:
                return False, {"code": "UNAUTHORIZED", "message": "API key does not match site."}
            if origin and origin not in (site.get("allowedOrigins") or []):
                return False, {"code": "UNAUTHORIZED", "message": "Origin is not allowed for this key."}
            return True, None
        return False, {"code": "UNAUTHORIZED", "message": "Unknown or revoked API key."}

    def embed_snippet(self, site_id: str, api_url: str, manifest_url: str, pk_key: str) -> str:
        return (
            '<script src="https://cdn.nina.dev/sdk/nina-bootstrap.js"\n'
            f'        data-site-id="{site_id}"\n'
            f'        data-api="{api_url}"\n'
            f'        data-manifest="{manifest_url}"\n'
            f'        data-api-key="{pk_key}"\n'
            '        data-panel="right"\n'
            "        defer></script>"
        )

    # ── unified accessor helpers (mirror PgStore API) ─────────────────────────

    def get_org(self, org_id: str) -> dict[str, Any] | None:
        return self.orgs.get(org_id)

    def get_site(self, site_id: str) -> dict[str, Any] | None:
        return self.sites.get(site_id)

    def get_api_key(self, key_id: str) -> dict[str, Any] | None:
        return self.api_keys.get(key_id)

    def list_orgs(self) -> list[dict[str, Any]]:
        return list(self.orgs.values())

    def list_sites(self, org_id: str | None = None) -> list[dict[str, Any]]:
        items = list(self.sites.values())
        if org_id:
            items = [s for s in items if s.get("orgId") == org_id]
        return items

    def list_api_keys(self) -> list[dict[str, Any]]:
        return list(self.api_keys.values())

    def revoke_api_key(self, key_id: str) -> None:
        if key_id not in self.api_keys:
            raise ValueError("Unknown key_id")
        self.api_keys[key_id]["revoked"] = True
        self.save()

    def revoke_cli_token(self, token_id: str) -> None:
        if token_id not in self.cli_tokens:
            raise ValueError("Unknown token_id")
        self.cli_tokens[token_id]["revoked"] = True
        self.save()

    def list_api_keys_for_site(self, site_id: str) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in rec.items() if k != "digest"}
            for rec in self.api_keys.values()
            if rec["siteId"] == site_id
        ]

    def find_site_by_wa_number(self, wa_number_id: str) -> dict[str, Any] | None:
        return next((s for s in self.sites.values() if s.get("waNumberId") == wa_number_id), None)

    def update_site_fields(self, site_id: str, **kwargs: Any) -> None:
        site = self.sites.get(site_id)
        if not site:
            raise ValueError("Unknown site_id")
        site.update(kwargs)
        self.save()

    def push_webhook_event(self, event_type: str, payload: dict[str, Any]) -> int:
        if len(self.webhook_events) >= _MAX_WEBHOOK_EVENTS:
            self.webhook_events = self.webhook_events[-(  _MAX_WEBHOOK_EVENTS - 1):]
        self.webhook_events.append({"type": event_type, "receivedAt": _now_ts(), "payload": payload})
        self.save()
        return sum(1 for e in self.webhook_events if e.get("type") == event_type)

    def list_webhook_events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        if event_type:
            return [e for e in self.webhook_events if e.get("type") == event_type]
        return list(self.webhook_events)

    def count_orgs(self) -> int:
        return len(self.orgs)

    def count_sites(self) -> int:
        return len(self.sites)

    def count_keys(self) -> int:
        return len(self.api_keys)


_db_url = os.environ.get("DATABASE_URL", "")
if _db_url:
    from .pg_store import PgStore
    STORE: Any = PgStore()
    STORE.load()
else:
    STORE = ConsoleStore()
    STORE.load(Path(os.environ.get("NINA_CONSOLE_STORE_PATH", "nina_console_store.json")))

POOL = NinaPool()


class OrgCreate(BaseModel):
    name: str
    ownerEmail: str | None = None


class SiteCreate(BaseModel):
    orgId: str
    name: str
    baseUrl: str
    currency: str = "USD"
    locales: list[str] = Field(default_factory=lambda: ["en"])
    markets: list[str] = Field(default_factory=list)
    allowedOrigins: list[str] = Field(default_factory=list)


class KeyIssueIn(BaseModel):
    siteId: str
    environment: str = "test"
    kind: str = "pk"


class KeyVerifyIn(BaseModel):
    apiKey: str
    siteId: str | None = None
    origin: str | None = None
    pageUrl: str | None = None
    clientIp: str | None = None


class CliTokenIn(BaseModel):
    orgId: str
    label: str = "default"


class WizardInitIn(BaseModel):
    orgName: str
    ownerEmail: str | None = None
    siteName: str
    baseUrl: str
    country: str = "IN"
    currency: str = "INR"
    languages: list[str] = Field(default_factory=lambda: ["en", "hi"])


class WizardApiConnectIn(BaseModel):
    siteId: str
    apiBaseUrl: str
    searchPath: str = "/api/v1/products/search"
    listCategoriesPath: str = "/api/v1/categories"


class WizardGenerateIn(BaseModel):
    configDir: str
    strict: bool = True
    probe: bool = False


class WizardValidateIn(BaseModel):
    agentPath: str
    strict: bool = True
    probe: bool = False


class RegistrarExportIn(BaseModel):
    siteId: str
    outputPath: str


class SeoSitemapIn(BaseModel):
    siteId: str
    sitemapUrl: str | None = None
    rawSitemapXml: str | None = None


class SeoEmbedHealthIn(BaseModel):
    siteUrl: str


class SiteContractIn(BaseModel):
    contract: dict[str, Any]


class SiteLlmConfigIn(BaseModel):
    llmConfig: dict[str, Any]


class MultiTenantQueryIn(BaseModel):
    message: str = ""
    transcript: str = ""
    sessionId: str
    page_context: dict[str, Any] | None = None
    session_hints: dict[str, Any] | None = None
    confirmed: bool = False
    replayQueued: bool = False


class OnboardingPackIn(BaseModel):
    siteId: str | None = None
    siteName: str | None = None
    baseUrl: str | None = None
    locales: list[str] = Field(default_factory=lambda: ["en"])
    markets: list[str] = Field(default_factory=list)
    allowedOrigins: list[str] = Field(default_factory=list)
    apiBaseUrl: str | None = None
    sitemapUrl: str | None = None
    rawSitemapXml: str | None = None
    capabilities: list[str] = Field(
        default_factory=lambda: ["search", "list_categories", "cart", "checkout"]
    )
    includeAuth: bool = False
    includeRisk: bool = False
    includeSkills: bool = True


def create_app() -> FastAPI:
    app = FastAPI(title="NINA Console", version="0.1.0")

    # ── Request logging middleware (outermost — captures all requests) ─────────
    @app.middleware("http")
    async def _request_logger(request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            "%s %s %d",
            request.method,
            request.url.path,
            response.status_code,
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "ip": (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                       or (request.client.host if request.client else "unknown")),
            },
        )
        return response

    # ── Admin auth: protect all /v1/* except the widget query endpoint ────────
    # Set NINA_CONSOLE_ADMIN_SECRET in production. When unset (local dev) the
    # middleware is a no-op so everything works without configuration.
    @app.middleware("http")
    async def _admin_auth(request: Request, call_next):
        path = request.url.path
        # Public: health check, widget query, merchant auth, static assets
        if path in ("/health",) or path == "/v1/query" or path.startswith("/v1/auth/") or not path.startswith("/v1/"):
            return await call_next(request)
        secret = os.environ.get("NINA_CONSOLE_ADMIN_SECRET")
        if secret:
            auth = request.headers.get("authorization", "")
            expected = f"Bearer {secret}"
            if not hmac.compare_digest(
                auth.encode() if auth else b"",
                expected.encode(),
            ):
                return JSONResponse(
                    status_code=401,
                    content={"ok": False, "error": {"code": "UNAUTHORIZED", "message": "Console admin secret required. Set Authorization: Bearer <NINA_CONSOLE_ADMIN_SECRET>."}},
                )
        return await call_next(request)

    # ── CORS: widget must be callable from any merchant domain ────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["*", "Authorization"],
        expose_headers=["X-NINA-Request-Id"],
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "nina-console",
            "store": {
                "orgs":  STORE.count_orgs(),
                "sites": STORE.count_sites(),
                "keys":  STORE.count_keys(),
                "backend": "postgresql" if _db_url else "json-file",
            },
            "pool": {
                "cached":  len(POOL._instances),
                "max":     int(os.environ.get("NINA_POOL_MAX_SITES", "100")),
                "circuits_open": len(POOL._circuit_until),
            },
        }

    @app.get("/v1/metrics")
    def get_metrics() -> dict[str, Any]:
        return {"ok": True, "data": METRICS.snapshot()}

    @app.post("/v1/orgs")
    def create_org(body: OrgCreate) -> dict[str, Any]:
        return {"ok": True, "data": STORE.create_org(body.name, body.ownerEmail)}

    @app.get("/v1/orgs")
    def list_orgs() -> dict[str, Any]:
        return {"ok": True, "data": STORE.list_orgs()}

    @app.post("/v1/sites")
    def create_site(body: SiteCreate) -> dict[str, Any]:
        site = STORE.create_site(
            body.orgId,
            body.name,
            body.baseUrl,
            locales=body.locales,
            markets=body.markets,
            allowed_origins=body.allowedOrigins,
            currency=body.currency,
        )
        return {"ok": True, "data": site}

    @app.get("/v1/sites")
    def list_sites(org_id: str | None = None) -> dict[str, Any]:
        return {"ok": True, "data": STORE.list_sites(org_id)}

    @app.put("/v1/sites/{site_id}/contract")
    def put_site_contract(site_id: str, body: SiteContractIn) -> dict[str, Any]:
        try:
            STORE.attach_contract(site_id, body.contract)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        POOL.evict(site_id)
        return {"ok": True, "data": {"siteId": site_id, "contractAttached": True}}

    @app.put("/v1/sites/{site_id}/llm-config")
    def put_site_llm_config(site_id: str, body: SiteLlmConfigIn) -> dict[str, Any]:
        try:
            STORE.attach_llm_config(site_id, body.llmConfig)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        POOL.evict(site_id)
        return {"ok": True, "data": {"siteId": site_id, "llmConfigAttached": True}}

    @app.post("/v1/query")
    async def multi_tenant_query(
        body: MultiTenantQueryIn,
        request: Request,
        x_nina_api_key: str | None = Header(default=None, alias="X-NINA-API-Key"),
    ) -> dict[str, Any]:
        # Rate limiting — per source IP and per API key
        forwarded = request.headers.get("x-forwarded-for", "")
        client_ip = forwarded.split(",")[0].strip() if forwarded else (
            request.client.host if request.client else "unknown"
        )
        if not _IP_LIMITER.allow(client_ip):
            return JSONResponse(status_code=429, content={"ok": False, "data": None, "error": {"code": "RATE_LIMITED", "message": "Too many requests. Please slow down."}})
        if x_nina_api_key and not _KEY_LIMITER.allow(x_nina_api_key[:32]):
            return JSONResponse(status_code=429, content={"ok": False, "data": None, "error": {"code": "RATE_LIMITED", "message": "API key rate limit exceeded."}})

        origin = request.headers.get("origin")
        ok_key, site, key_err = STORE.resolve_key_to_site(x_nina_api_key or "", origin)
        if not ok_key:
            return JSONResponse(status_code=401, content={"ok": False, "data": None, "error": key_err})

        # Quota enforcement — hard block when monthly limit is reached
        quota_ok, current_calls, limit = STORE.enforce_quota(site["id"])
        if not quota_ok:
            return JSONResponse(
                status_code=402,
                content={"ok": False, "data": None, "error": {
                    "code": "QUOTA_EXCEEDED",
                    "message": f"Monthly query limit of {limit:,} reached on the {site.get('plan','free')} plan. Upgrade to continue.",
                }},
            )

        contract = site.get("agentContract")
        if not contract:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "data": None, "error": {"code": "NO_CONTRACT", "message": "No agent contract configured for this site. Upload one via PUT /v1/sites/{id}/contract."}},
            )
        sealed_llm = site.get("llmConfig")
        if not sealed_llm:
            # Fall back to NINA_DEFAULT_LLM_CONFIG (operator's own key for free tier).
            # Format: JSON string, e.g. '{"provider":"openai","model":"gpt-4o-mini","apiKey":"sk-..."}'
            _default_raw = os.environ.get("NINA_DEFAULT_LLM_CONFIG")
            if _default_raw:
                try:
                    sealed_llm = json.loads(_default_raw)
                except json.JSONDecodeError:
                    pass
        if not sealed_llm:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "data": None, "error": {"code": "NO_LLM_CONFIG", "message": "No LLM config for this site. Upload one via PUT /v1/sites/{id}/llm-config."}},
            )
        try:
            llm_config = unseal_llm_config(sealed_llm)
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content={"ok": False, "data": None, "error": {"code": "LLM_CONFIG_DECRYPT_FAILED", "message": str(exc)}},
            )

        page_context = body.page_context or {}
        _t0 = time.time()
        envelope = await POOL.run(
            site["id"],
            llm_config,
            contract,
            body.transcript or body.message,
            body.sessionId,
            session_hints=body.session_hints,
            page_id=page_context.get("pageId"),
            replay_queued=body.replayQueued,
        )
        if envelope.get("ok") and envelope.get("data"):
            from .instructions import turn_to_instructions
            turn = dict(envelope["data"])
            if turn.get("intent") != "blocked":
                turn["instructions"] = turn_to_instructions(
                    contract, turn, page_context=page_context,
                    session_hints=body.session_hints, confirmed=body.confirmed,
                )
            envelope = {**envelope, "data": turn}
        _latency_ms = int((time.time() - _t0) * 1000)
        _err_code = (envelope.get("error") or {}).get("code", "")
        METRICS.record(ok=bool(envelope.get("ok")), latency_ms=_latency_ms, error_code=_err_code)
        logger.info(
            "query %s site=%s latency=%dms",
            "ok" if envelope.get("ok") else f"err:{_err_code}",
            site["id"],
            _latency_ms,
            extra={"site_id": site["id"], "duration_ms": _latency_ms, "error_code": _err_code or None},
        )
        STORE.record_usage(site["id"])
        return envelope

    @app.get("/v1/sites/{site_id}/usage")
    def site_usage(site_id: str) -> dict[str, Any]:
        site = STORE.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail="Unknown site_id")
        plan = site.get("plan", "free")
        limit = _PLAN_LIMITS.get(plan)
        usage = STORE.get_usage(site_id)
        return {"ok": True, "data": {
            "siteId": site_id,
            "plan": plan,
            "limit": limit,
            "remaining": (limit - usage["calls"]) if limit is not None else None,
            **usage,
        }}

    @app.put("/v1/sites/{site_id}/plan")
    def set_site_plan(site_id: str, body: dict[str, Any]) -> dict[str, Any]:
        plan = body.get("plan", "")
        try:
            STORE.set_plan(site_id, plan)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "data": {"siteId": site_id, "plan": plan, "limit": _PLAN_LIMITS[plan]}}

    # ── Merchant dashboard auth ───────────────────────────────────────────────
    @app.get("/v1/auth/whoami")
    def auth_whoami(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Validate a merchant dashboard token and return org info."""
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Dashboard token required.")
        raw = authorization.removeprefix("Bearer ").strip()
        org = STORE.verify_dashboard_token(raw)
        if not org:
            raise HTTPException(status_code=401, detail="Invalid or expired dashboard token.")
        sites = STORE.list_sites(org_id=org["id"])
        return {"ok": True, "data": {"org": {k: v for k, v in org.items() if k != "dashboardTokenDigest"}, "sites": sites}}

    def _require_dashboard_token(authorization: str | None) -> dict[str, Any]:
        """Validate dashboard token and return org. Raises 401 on failure."""
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Dashboard token required.")
        raw = authorization.removeprefix("Bearer ").strip()
        org = STORE.verify_dashboard_token(raw)
        if not org:
            raise HTTPException(status_code=401, detail="Invalid or expired dashboard token.")
        return org

    def _require_site_ownership(org: dict[str, Any], site_id: str) -> dict[str, Any]:
        """Confirm org owns site_id. Raises 403 if not."""
        site = STORE.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail="Site not found.")
        if site.get("orgId") != org["id"]:
            raise HTTPException(status_code=403, detail="Access denied.")
        return site

    @app.get("/v1/auth/sites/{site_id}/usage")
    def merchant_get_usage(site_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        org = _require_dashboard_token(authorization)
        _require_site_ownership(org, site_id)
        usage = STORE.get_usage(site_id)
        plan = STORE.get_site(site_id).get("plan", "free")
        return {"ok": True, "data": {**(usage or {}), "plan": plan}}

    @app.get("/v1/auth/sites/{site_id}/keys")
    def merchant_list_keys(site_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        org = _require_dashboard_token(authorization)
        _require_site_ownership(org, site_id)
        return {"ok": True, "data": STORE.list_api_keys_for_site(site_id)}

    @app.put("/v1/auth/sites/{site_id}/llm-config")
    def merchant_set_llm_config(site_id: str, body: SiteLlmConfigIn, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        org = _require_dashboard_token(authorization)
        _require_site_ownership(org, site_id)
        try:
            STORE.attach_llm_config(site_id, body.llmConfig)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        POOL.evict(site_id)
        return {"ok": True, "data": {"siteId": site_id, "llmConfigAttached": True}}

    @app.put("/v1/auth/sites/{site_id}/contract")
    def merchant_set_contract(site_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        org = _require_dashboard_token(authorization)
        _require_site_ownership(org, site_id)
        contract = body.get("contract")
        if not contract:
            raise HTTPException(status_code=400, detail="contract field required.")
        try:
            STORE.attach_contract(site_id, contract)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        POOL.evict(site_id)
        return {"ok": True, "data": {"siteId": site_id, "contractAttached": True}}

    @app.post("/v1/auth/keys/issue")
    def merchant_issue_key(body: KeyIssueIn, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        org = _require_dashboard_token(authorization)
        _require_site_ownership(org, body.siteId)
        try:
            rec = STORE.issue_api_key(body.siteId, body.environment, body.kind)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "data": rec}

    @app.post("/v1/auth/keys/{key_id}/revoke")
    def merchant_revoke_key(key_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        org = _require_dashboard_token(authorization)
        key = STORE.get_api_key(key_id)
        if not key:
            raise HTTPException(status_code=404, detail="Key not found.")
        _require_site_ownership(org, key["siteId"])
        try:
            STORE.revoke_api_key(key_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "data": {"keyId": key_id, "revoked": True}}

    @app.post("/v1/auth/sites/{site_id}/generate-from-url")
    async def merchant_generate_from_url(site_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        org = _require_dashboard_token(authorization)
        _require_site_ownership(org, site_id)
        api_base_url = body.get("apiBaseUrl", "")
        if not api_base_url:
            raise HTTPException(status_code=400, detail="apiBaseUrl required.")
        from .openapi_probe import fetch_openapi_spec, spec_to_actions
        try:
            spec = fetch_openapi_spec(api_base_url if "openapi" in api_base_url else api_base_url.rstrip("/") + "/openapi.json")
            actions = spec_to_actions(spec)
            contract = {"actions": actions}
            STORE.attach_contract(site_id, contract)
            POOL.evict(site_id)
            return {"ok": True, "data": {"siteId": site_id, "actionsFound": len(actions)}}
        except Exception as exc:
            return {"ok": False, "errors": [str(exc)]}

    @app.put("/v1/auth/sites/{site_id}/settings")
    def merchant_update_settings(site_id: str, body: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        org = _require_dashboard_token(authorization)
        _require_site_ownership(org, site_id)
        allowed = body.get("allowedOrigins")
        if allowed is not None:
            if not isinstance(allowed, list):
                raise HTTPException(status_code=400, detail="allowedOrigins must be a list of URL strings.")
            STORE.update_site_fields(site_id, allowedOrigins=allowed)
        return {"ok": True, "data": {"siteId": site_id, "updated": True}}

    @app.post("/v1/auth/rotate-token")
    def auth_rotate_token(body: dict[str, Any]) -> dict[str, Any]:
        """Rotate a merchant's dashboard token. Requires admin secret (operator action)."""
        org_id = body.get("orgId", "")
        try:
            rec = STORE.rotate_dashboard_token(org_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "data": rec}

    @app.post("/v1/keys/issue")
    def issue_key(body: KeyIssueIn) -> dict[str, Any]:
        try:
            rec = STORE.issue_api_key(body.siteId, body.environment, body.kind)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "data": rec}

    @app.post("/v1/keys/verify")
    def verify_key(body: KeyVerifyIn, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        expected_secret = os.environ.get("NINA_CONSOLE_VERIFY_SECRET")
        if expected_secret:
            if authorization != f"Bearer {expected_secret}":
                return {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "Invalid verifier secret."}}
        origin = body.origin or _parse_origin(body.pageUrl)
        ok, err = STORE.verify_publishable_key(body.apiKey, body.siteId, origin)
        return {"ok": ok, "error": err}

    @app.post("/v1/keys/{key_id}/revoke")
    def revoke_key(key_id: str) -> dict[str, Any]:
        try:
            STORE.revoke_api_key(key_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "data": {"keyId": key_id, "revoked": True}}

    @app.get("/v1/sites/{site_id}/keys")
    def list_site_keys(site_id: str) -> dict[str, Any]:
        if not STORE.get_site(site_id):
            raise HTTPException(status_code=404, detail="Unknown site_id")
        return {"ok": True, "data": STORE.list_api_keys_for_site(site_id)}

    @app.get("/v1/orgs/{org_id}")
    def get_org(org_id: str) -> dict[str, Any]:
        org = STORE.get_org(org_id)
        if not org:
            raise HTTPException(status_code=404, detail="Unknown org_id")
        return {"ok": True, "data": {k: v for k, v in org.items() if k != "dashboardTokenDigest"}}

    @app.get("/v1/sites/{site_id}")
    def get_site(site_id: str) -> dict[str, Any]:
        site = STORE.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail="Unknown site_id")
        return {"ok": True, "data": site}

    @app.post("/v1/tokens/cli")
    def issue_cli_token(body: CliTokenIn) -> dict[str, Any]:
        try:
            rec = STORE.issue_cli_token(body.orgId, body.label)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "data": rec}

    @app.get("/v1/embed/snippet")
    def embed_snippet(site_id: str, api_url: str, manifest_url: str, key_id: str) -> dict[str, Any]:
        rec = STORE.get_api_key(key_id)
        if not rec or rec["siteId"] != site_id:
            raise HTTPException(status_code=404, detail="Unknown key_id for site.")
        return {
            "ok": True,
            "data": {
                "snippet": STORE.embed_snippet(site_id, api_url, manifest_url, rec["prefix"] + "..."),
                "note": "Use the full token returned during issuance; prefix is shown for safety.",
            },
        }

    # 10-step onboarding wizard backend
    @app.post("/v1/wizard/init")
    def wizard_init(body: WizardInitIn) -> dict[str, Any]:
        org = STORE.create_org(body.orgName, body.ownerEmail)
        site = STORE.create_site(
            org["id"],
            body.siteName,
            body.baseUrl,
            locales=body.languages,
            markets=[body.country],
            currency=body.currency,
        )
        key = STORE.issue_api_key(site["id"], "test", "pk")
        return {"ok": True, "data": {"org": org, "site": site, "publishableKey": key}}

    @app.post("/v1/wizard/connect-apis")
    def wizard_connect_apis(body: WizardApiConnectIn) -> dict[str, Any]:
        _validate_external_url(body.apiBaseUrl, "API base URL")
        checks: list[dict[str, Any]] = []
        paths = [body.searchPath, body.listCategoriesPath]
        with httpx.Client(timeout=5.0, follow_redirects=True) as client:
            for path in paths:
                url = f"{body.apiBaseUrl.rstrip('/')}/{path.lstrip('/')}"
                try:
                    resp = client.options(url)
                    checks.append({"url": url, "ok": resp.status_code < 500, "status": resp.status_code})
                except Exception as exc:
                    checks.append({"url": url, "ok": False, "error": str(exc)})
        return {"ok": True, "data": {"checks": checks}}

    @app.post("/v1/wizard/generate-contract")
    def wizard_generate_contract(body: WizardGenerateIn) -> dict[str, Any]:
        result = run_pipeline(Path(body.configDir), dry_run=False, strict=body.strict, probe=body.probe)
        return {
            "ok": result.ok,
            "data": {
                "outputPath": str(result.output_path) if result.output_path else None,
                "stats": result.stats,
            },
            "errors": result.errors,
        }

    @app.post("/v1/wizard/validate-contract")
    def wizard_validate_contract(body: WizardValidateIn) -> dict[str, Any]:
        p = Path(body.agentPath)
        if not p.exists():
            raise HTTPException(status_code=404, detail="agent.json not found")
        contract = json.loads(p.read_text(encoding="utf-8"))
        ok, errors, warnings = validate_executable(contract, strict=body.strict, probe=body.probe)
        return {"ok": ok, "errors": errors, "warnings": warnings}

    @app.post("/v1/wizard/onboarding-pack")
    def wizard_onboarding_pack(body: OnboardingPackIn) -> Response:
        """Build and download zip: nina.site.yaml, api.manifest.yaml, sitemap.xml, policies."""
        site = STORE.get_site(body.siteId) if body.siteId else None
        if body.siteId and not site:
            raise HTTPException(status_code=404, detail="Unknown site_id")
        try:
            fields = resolve_site_fields(
                site,
                site_name=body.siteName,
                base_url=body.baseUrl,
                locales=body.locales,
                markets=body.markets,
                allowed_origins=body.allowedOrigins,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        files = build_onboarding_pack_files(
            site_id=fields["site_id"],
            site_name=fields["site_name"],
            base_url=fields["base_url"],
            locales=fields["locales"],
            markets=fields["markets"],
            allowed_origins=fields["allowed_origins"],
            api_base_url=body.apiBaseUrl,
            capabilities=body.capabilities,
            sitemap_url=body.sitemapUrl,
            raw_sitemap_xml=body.rawSitemapXml,
            include_auth=body.includeAuth,
            include_risk=body.includeRisk,
            include_skills=body.includeSkills,
        )
        payload, filename = zip_onboarding_pack(files, archive_name=fields["site_id"])
        return Response(
            content=payload,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/v1/wizard/steps")
    def wizard_steps() -> dict[str, Any]:
        steps = [
            "Welcome",
            "Your store",
            "Capabilities",
            "Connect APIs",
            "Verify domain",
            "Build contract",
            "Review actions",
            "Install NINA",
            "Test sandbox",
            "Go live",
        ]
        return {"ok": True, "data": steps}

    # Developer workspace: files, CLI token, webhooks
    @app.get("/v1/developer/files")
    def developer_files(config_dir: str) -> dict[str, Any]:
        base = _validate_local_path(config_dir)
        names = ["nina.site.yaml", "api.manifest.yaml", "auth.policy.yaml", "risk.policy.yaml"]
        out: dict[str, str] = {}
        for name in names:
            p = base / name
            if p.exists():
                out[name] = p.read_text(encoding="utf-8")
        return {"ok": True, "data": out}

    @app.post("/v1/developer/files")
    def developer_write_file(config_dir: str, filename: str, content: str) -> dict[str, Any]:
        if filename not in {"nina.site.yaml", "api.manifest.yaml", "auth.policy.yaml", "risk.policy.yaml"}:
            raise HTTPException(status_code=400, detail="Unsupported config file")
        base = _validate_local_path(config_dir)
        base.mkdir(parents=True, exist_ok=True)
        path = base / filename
        path.write_text(content, encoding="utf-8")
        return {"ok": True, "data": {"path": str(path)}}

    @app.post("/v1/webhooks/broken-selector")
    def webhook_broken_selector(payload: dict[str, Any]) -> dict[str, Any]:
        count = STORE.push_webhook_event("broken-selector", payload)
        return {"ok": True, "data": {"queued": True, "count": count}}

    @app.get("/v1/webhooks/broken-selector")
    def webhook_list_broken_selector() -> dict[str, Any]:
        return {"ok": True, "data": STORE.list_webhook_events("broken-selector")}

    # Registrar + GEO
    @app.post("/v1/registrar/verify-domain")
    def registrar_verify_domain(site_id: str, method: str, token: str | None = None) -> dict[str, Any]:
        site = STORE.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail="Unknown site_id")
        method = method.lower()
        if method not in {"dns_txt", "html_meta", "well_known"}:
            raise HTTPException(status_code=400, detail="Unsupported verification method")
        new_status = "verified" if token else "pending"
        verification = dict(site.get("verification") or {})
        verification["production"] = new_status
        STORE.update_site_fields(site_id, verification=verification)
        return {"ok": True, "data": {"siteId": site_id, "method": method, "status": new_status}}

    @app.post("/v1/registrar/export-nina-site")
    def registrar_export_nina_site(body: RegistrarExportIn) -> dict[str, Any]:
        site = STORE.get_site(body.siteId)
        if not site:
            raise HTTPException(status_code=404, detail="Unknown site_id")
        data = {
            "site": {
                "id": site["id"],
                "name": site["name"],
                "baseUrl": site["baseUrl"],
                "locales": site["locales"],
                "allowedOrigins": site["allowedOrigins"],
            },
            "generator": {"sitemap": "sitemap.xml", "docsDir": "docs", "crawl": {"maxPages": 50, "respectRobots": True, "delayMs": 500}},
            "publish": {"outputDir": "dist", "uploadUrl": ""},
        }
        path = Path(body.outputPath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return {"ok": True, "data": {"path": str(path)}}

    # SEO toolkit
    @app.post("/v1/seo/sitemap")
    async def seo_sitemap(body: SeoSitemapIn) -> dict[str, Any]:
        xml = body.rawSitemapXml
        if not xml and body.sitemapUrl:
            _validate_external_url(body.sitemapUrl, "Sitemap URL")
            with httpx.Client(timeout=8.0, follow_redirects=True) as client:
                resp = client.get(body.sitemapUrl)
                resp.raise_for_status()
                xml = resp.text
        if not xml:
            raise HTTPException(status_code=400, detail="No sitemap content provided")
        urls = [line.split("<loc>", 1)[1].split("</loc>", 1)[0].strip() for line in xml.splitlines() if "<loc>" in line]
        return {"ok": True, "data": {"siteId": body.siteId, "urlCount": len(urls), "urls": urls[:100]}}

    @app.post("/v1/seo/embed-health")
    def seo_embed_health(body: SeoEmbedHealthIn) -> dict[str, Any]:
        _validate_external_url(body.siteUrl, "Site URL")
        checks: dict[str, Any] = {"siteUrl": body.siteUrl}
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            try:
                resp = client.get(body.siteUrl)
                html = resp.text if resp.status_code < 500 else ""
                checks["pageStatus"] = resp.status_code
                checks["bootstrapPresent"] = "nina-bootstrap.js" in html
            except Exception as exc:
                return {"ok": False, "error": {"code": "FETCH_FAILED", "message": str(exc)}}
            manifest = body.siteUrl.rstrip("/") + "/agent.json"
            query = body.siteUrl.rstrip("/") + "/v1/query"
            for key, url in [("manifestStatus", manifest), ("queryStatus", query)]:
                try:
                    r = client.get(url)
                    checks[key] = r.status_code
                except Exception:
                    checks[key] = None
        checks["ok"] = bool(checks.get("bootstrapPresent")) and checks.get("manifestStatus") == 200
        return {"ok": checks["ok"], "data": checks}

    # ── Self-serve contract generation from site URL ──────────────────────────
    class GenerateFromUrlIn(BaseModel):
        apiBaseUrl: str | None = None
        openApiUrl: str | None = None

    @app.post("/v1/sites/{site_id}/generate-from-url")
    async def generate_from_url(site_id: str, body: GenerateFromUrlIn) -> dict[str, Any]:
        site = STORE.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail="Unknown site_id")

        import asyncio
        import tempfile
        from .console_pack import build_nina_site_yaml, build_api_manifest

        api_base = body.apiBaseUrl or site.get("baseUrl", "")
        locales = site.get("locales") or ["en"]
        allowed = site.get("allowedOrigins") or []

        site_yaml = build_nina_site_yaml(
            site_id=site["id"],
            name=site["name"],
            base_url=site["baseUrl"],
            locales=locales,
            allowed_origins=allowed,
        )

        def _run_pipeline(config_dir_str: str) -> "GenerationResult":
            from .generator.pipeline import run_pipeline as _rp
            cfg = Path(config_dir_str)
            (cfg / "nina.site.yaml").write_text(site_yaml, encoding="utf-8")
            manifest_yaml = build_api_manifest(api_base_url=api_base)
            (cfg / "api.manifest.yaml").write_text(manifest_yaml, encoding="utf-8")
            return _rp(cfg, dry_run=False, strict=False, probe=False)

        try:
            with tempfile.TemporaryDirectory() as tmp:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, _run_pipeline, tmp)
        except Exception as exc:
            return {"ok": False, "errors": [str(exc)], "data": None}

        if result.ok and result.contract:
            STORE.attach_contract(site_id, result.contract)
            POOL.evict(site_id)
            return {"ok": True, "data": {"siteId": site_id, "stats": result.stats}}

        return {"ok": False, "errors": result.errors, "data": {"stats": result.stats}}

    # ── WhatsApp channel (H1–H3) ─────────────────────────────────────────────
    # Set NINA_WHATSAPP_VERIFY_TOKEN for webhook verification handshake.
    # Set NINA_AISENSY_API_KEY to send replies via AiSensy BSP.
    # Map WhatsApp business numbers to site IDs in the site's waNumber field.

    class _WAWebhookIn(BaseModel):
        object: str = ""
        entry: list[dict[str, Any]] = Field(default_factory=list)

    @app.get("/v1/channels/whatsapp/webhook")
    def whatsapp_verify(
        hub_mode: str | None = None,
        hub_verify_token: str | None = None,
        hub_challenge: str | None = None,
    ) -> Any:
        """WhatsApp webhook verification handshake (Meta requirement)."""
        expected = os.environ.get("NINA_WHATSAPP_VERIFY_TOKEN", "")
        if hub_mode == "subscribe" and hub_verify_token == expected:
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(hub_challenge or "")
        raise HTTPException(status_code=403, detail="Verification token mismatch.")

    @app.post("/v1/channels/whatsapp/webhook")
    async def whatsapp_webhook(body: _WAWebhookIn, request: Request) -> dict[str, Any]:
        """Receive WhatsApp messages from AiSensy BSP and route through NINA."""
        # Extract messages from the WhatsApp webhook payload
        for entry in body.entry:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                wa_number = value.get("metadata", {}).get("phone_number_id", "")

                # Find which site is registered to this WhatsApp number
                site = STORE.find_site_by_wa_number(wa_number)
                if not site:
                    logger.info("whatsapp: unknown wa_number_id=%s", wa_number)
                    continue

                for msg in value.get("messages", []):
                    msg_type = msg.get("type", "")
                    if msg_type != "text":
                        continue  # Only handle text messages for now

                    from_number = msg.get("from", "")
                    text = msg.get("text", {}).get("body", "")
                    session_id = f"wa_{from_number}"

                    sealed_llm = site.get("llmConfig")
                    if not sealed_llm:
                        _raw = os.environ.get("NINA_DEFAULT_LLM_CONFIG")
                        if _raw:
                            try:
                                sealed_llm = json.loads(_raw)
                            except json.JSONDecodeError:
                                pass
                    if not sealed_llm or not site.get("agentContract"):
                        continue

                    try:
                        llm_config = unseal_llm_config(sealed_llm)
                    except Exception:
                        continue

                    envelope = await POOL.run(
                        site["id"],
                        llm_config,
                        site["agentContract"],
                        text,
                        session_id,
                    )
                    reply = ""
                    if envelope.get("ok") and envelope.get("data"):
                        reply = (envelope["data"].get("naturalLanguageResponse") or "").strip()

                    if reply:
                        await _send_whatsapp_reply(wa_number, from_number, reply)

                    STORE.record_usage(site["id"])

        return {"ok": True}

    async def _send_whatsapp_reply(wa_number_id: str, to: str, text: str) -> None:
        """Send a text reply via the WhatsApp Cloud API / AiSensy."""
        api_key = os.environ.get("NINA_WHATSAPP_API_KEY", "")
        if not api_key:
            logger.info("whatsapp: NINA_WHATSAPP_API_KEY not set — reply not sent")
            return
        url = f"https://graph.facebook.com/v18.0/{wa_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text[:4096]},  # WhatsApp max text length
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json=payload, headers={"Authorization": f"Bearer {api_key}"})
        except Exception as exc:
            logger.info("whatsapp: send failed — %s", exc)

    @app.put("/v1/sites/{site_id}/whatsapp")
    def configure_whatsapp(site_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Associate a WhatsApp business number ID with a site."""
        if not STORE.get_site(site_id):
            raise HTTPException(status_code=404, detail="Unknown site_id")
        wa_number_id = body.get("waNumberId", "")
        if not wa_number_id:
            raise HTTPException(status_code=400, detail="waNumberId is required")
        STORE.update_site_fields(site_id, waNumberId=wa_number_id)
        return {"ok": True, "data": {"siteId": site_id, "waNumberId": wa_number_id}}

    # ── Razorpay billing webhooks (H4–H6) ────────────────────────────────────
    # Set NINA_RAZORPAY_WEBHOOK_SECRET to validate Razorpay webhook signatures.
    # Events handled: subscription.activated, subscription.charged,
    #                 subscription.cancelled, subscription.expired

    # Map Razorpay plan IDs to NINA plan names — set via env or hardcode
    _RAZORPAY_PLAN_MAP = {
        os.environ.get("RAZORPAY_PLAN_STARTER", "plan_starter"):    "starter",
        os.environ.get("RAZORPAY_PLAN_GROWTH",  "plan_growth"):     "growth",
        os.environ.get("RAZORPAY_PLAN_SCALE",   "plan_scale"):      "scale",
        os.environ.get("RAZORPAY_PLAN_ENTERPRISE", "plan_ent"):     "enterprise",
    }

    @app.post("/v1/billing/razorpay/webhook")
    async def razorpay_webhook(request: Request) -> dict[str, Any]:
        """Validate Razorpay webhook signature and apply plan changes."""
        body_bytes = await request.body()
        secret = os.environ.get("NINA_RAZORPAY_WEBHOOK_SECRET", "")

        if secret:
            # Razorpay signs with HMAC-SHA256 of the raw body
            import hmac as _hmac
            import hashlib as _hashlib
            sig = request.headers.get("x-razorpay-signature", "")
            expected_sig = _hmac.new(
                secret.encode(), body_bytes, _hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected_sig):
                raise HTTPException(status_code=400, detail="Invalid Razorpay signature.")

        try:
            payload = json.loads(body_bytes)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.")

        event = payload.get("event", "")
        entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
        notes = entity.get("notes", {})
        site_id = notes.get("nina_site_id", "")
        plan_id = entity.get("plan_id", "")

        if not site_id:
            logger.info("razorpay: webhook %s — no nina_site_id in notes", event)
            return {"ok": True, "data": {"processed": False, "reason": "no nina_site_id"}}

        site = STORE.get_site(site_id)
        if not site:
            logger.info("razorpay: webhook %s — unknown site_id=%s", event, site_id)
            return {"ok": True, "data": {"processed": False, "reason": "unknown_site"}}

        plan = _RAZORPAY_PLAN_MAP.get(plan_id)

        if event in ("subscription.activated", "subscription.charged") and plan:
            STORE.set_plan(site_id, plan)
            logger.info("razorpay: site=%s upgraded to plan=%s", site_id, plan)
            return {"ok": True, "data": {"siteId": site_id, "plan": plan, "event": event}}

        if event in ("subscription.cancelled", "subscription.expired", "subscription.halted"):
            STORE.set_plan(site_id, "free")
            logger.info("razorpay: site=%s downgraded to free (event=%s)", site_id, event)
            return {"ok": True, "data": {"siteId": site_id, "plan": "free", "event": event}}

        return {"ok": True, "data": {"processed": False, "event": event}}

    # ── Named HTML pages (must be before the catch-all static mount) ─────────
    console_static = Path(__file__).resolve().parent / "console_static"

    @app.get("/dashboard", include_in_schema=False)
    def serve_dashboard() -> FileResponse:
        return FileResponse(console_static / "dashboard.html")

    # ── Static assets — SDK first, then console UI (catch-all must be last) ──
    sdk_dir = Path(__file__).resolve().parent / "sdk"
    if sdk_dir.exists():
        app.mount("/sdk", StaticFiles(directory=sdk_dir), name="nina-sdk")

    if console_static.exists():
        app.mount("/", StaticFiles(directory=console_static, html=True), name="console-ui")

    return app


app = create_app()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nina-console", description="Run NINA Console API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    import uvicorn

    uvicorn.run("nina.console_app:app", host=args.host, port=args.port, reload=False)
    return 0

