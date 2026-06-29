"""ConsoleStore — the JSON-file backed Store implementation (dev / single process).

The production counterpart is ``PgStore`` (``pg_store.py``); both satisfy the
``Store`` Protocol (``store.py``). Selected by ``console_app`` when no
``DATABASE_URL`` is set. Keep this and ``PgStore`` in lockstep — the
``Store`` Protocol + ``tests/test_store_protocol.py`` enforce it.

The JSON file holds merchant data and is gitignored; never commit it.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .crypto import hash_key, seal_llm_config
from .plans import PLAN_LIMITS as _PLAN_LIMITS, current_period as _current_period
from .store_util import (
    issue_key as _issue_key,
    now_ts as _now_ts,
    parse_origin as _parse_origin,
    rand_id as _rand_id,
    slug as _slug,
)

# Serializes writes to the JSON file (single-process dev store).
_STORE_WRITE_LOCK = threading.Lock()

# Cap retained webhook events so the JSON file can't grow unbounded.
_MAX_WEBHOOK_EVENTS = 500


def _hash_key(raw: str) -> str:
    # Canonical HMAC-SHA256, shared with PgStore (see crypto.hash_key).
    return hash_key(raw)


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
