"""PostgreSQL-backed ConsoleStore for NINA.

Drop-in replacement for the JSON-file ConsoleStore. Enabled automatically
when DATABASE_URL is set in the environment. Implements the same public
interface so console_app.py requires no logic changes.

Requires: psycopg2-binary (or psycopg2)
    pip install psycopg2-binary
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    _HAS_PSYCOPG2 = True
except ImportError:
    _HAS_PSYCOPG2 = False

from .crypto import hash_key, seal_llm_config
from .store_util import issue_key as _issue_key_shared, now_ts, parse_origin, rand_id, slug
from .plans import PLAN_LIMITS as _PLAN_LIMITS, current_period as _current_period


# ── helpers ───────────────────────────────────────────────────────────────────
# now_ts / rand_id / slug / parse_origin are imported from store_util (shared
# with ConsoleStore so both stores generate ids and slugs identically).
_parse_origin = parse_origin
_now_ts = now_ts
_rand_id = rand_id
_slug = slug


def _jd(value: Any) -> str:
    """Serialize a Python value to a compact JSON string for storage."""
    return json.dumps(value, ensure_ascii=False)


def _jl(raw: str | None) -> Any:
    """Load JSON from stored TEXT, returning None if null/empty."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


# ── row → dict converters ─────────────────────────────────────────────────────

def _org_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id":                   row["id"],
        "name":                 row["name"],
        "ownerEmail":           row["owner_email"],
        "dashboardTokenDigest": row["dashboard_token_digest"],
        "dashboardTokenPrefix": row["dashboard_token_prefix"],
        "tokenRotatedAt":       row.get("token_rotated_at"),
        "createdAt":            row["created_at"],
    }


def _site_row(row: dict[str, Any]) -> dict[str, Any]:
    site: dict[str, Any] = {
        "id":             row["id"],
        "orgId":          row["org_id"],
        "name":           row["name"],
        "baseUrl":        row["base_url"],
        "plan":           row["plan"],
        "currency":       row["currency"],
        "locales":        _jl(row.get("locales")) or ["en"],
        "markets":        _jl(row.get("markets")) or [],
        "allowedOrigins": _jl(row.get("allowed_origins")) or [],
        "verification":   _jl(row.get("verification")) or {"sandbox": "verified", "production": "pending"},
        "createdAt":      row["created_at"],
    }
    if row.get("agent_contract"):
        site["agentContract"] = _jl(row["agent_contract"])
    if row.get("llm_config"):
        site["llmConfig"] = _jl(row["llm_config"])
    if row.get("wa_number_id"):
        site["waNumberId"] = row["wa_number_id"]
    return site


def _key_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id":          row["id"],
        "siteId":      row["site_id"],
        "environment": row["environment"],
        "kind":        row["kind"],
        "prefix":      row["prefix"],
        "revoked":     bool(row["revoked"]),
        "createdAt":   row["created_at"],
    }


def _token_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id":        row["id"],
        "orgId":     row["org_id"],
        "label":     row["label"],
        "digest":    row["digest"],
        "prefix":    row["prefix"],
        "revoked":   bool(row["revoked"]),
        "createdAt": row["created_at"],
    }


# ── PgStore ───────────────────────────────────────────────────────────────────

class PgStore:
    """PostgreSQL-backed persistent store.  Identical public interface to ConsoleStore."""

    # store_path is None because we don't use a file; health endpoint reads this.
    store_path: Path | None = None

    def __init__(self) -> None:
        self._pool: "psycopg2.pool.ThreadedConnectionPool | None" = None
        self._key_hash_secret = os.environ.get("NINA_CONSOLE_KEY_HASH_SECRET", "nina-console-dev")

    # ── internal ──────────────────────────────────────────────────────────────

    def _connect(self, dsn: str) -> None:
        if not _HAS_PSYCOPG2:
            raise RuntimeError(
                "psycopg2 is not installed. Run: pip install psycopg2-binary\n"
                "Or unset DATABASE_URL to fall back to the JSON file store."
            )
        self._pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn)

    @contextmanager
    def _conn(self) -> Generator[Any, None, None]:
        assert self._pool, "PgStore not connected — call load() first"
        conn = self._pool.getconn()
        try:
            # Neon / Render free tier closes idle connections. Test and reconnect.
            if conn.closed:
                self._pool.putconn(conn, close=True)
                conn = self._pool.getconn()
            else:
                try:
                    conn.cursor().execute("SELECT 1")
                except Exception:
                    self._pool.putconn(conn, close=True)
                    conn = self._pool.getconn()
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._pool.putconn(conn)

    def _cur(self, conn: Any) -> Any:
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def _hash_key(self, raw: str) -> str:
        # Canonical HMAC-SHA256, identical to ConsoleStore (see crypto.hash_key).
        return hash_key(raw, self._key_hash_secret)

    def _issue_key(self, prefix: str) -> tuple[str, str]:
        return _issue_key_shared(prefix, self._key_hash_secret)

    def _ensure_schema(self, conn: Any) -> None:
        sql_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "db_init.sql"
        if sql_path.exists():
            sql = sql_path.read_text(encoding="utf-8")
        else:
            # Inline fallback if scripts/ isn't packaged
            sql = _SCHEMA_SQL
        with self._cur(conn) as cur:
            cur.execute(sql)

    # ── public lifecycle ──────────────────────────────────────────────────────

    def load(self, _path: Path | None = None) -> None:
        """Connect to PostgreSQL and ensure schema. `_path` is ignored (kept for compat)."""
        dsn = os.environ.get("DATABASE_URL", "")
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set")
        self._connect(dsn)
        with self._conn() as conn:
            self._ensure_schema(conn)

    def save(self) -> None:
        """No-op: PgStore writes immediately on every mutation."""

    # ── org methods ───────────────────────────────────────────────────────────

    def create_org(self, name: str, owner_email: str | None) -> dict[str, Any]:
        org_id = _rand_id("org")
        raw_token, token_digest = self._issue_key("dt_")
        prefix = raw_token[:16]
        created_at = str(_now_ts())
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO nina_orgs
                        (id, name, owner_email, dashboard_token_digest, dashboard_token_prefix, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (org_id, name, owner_email, token_digest, prefix, created_at),
                )
        org = {
            "id": org_id, "name": name, "ownerEmail": owner_email,
            "dashboardTokenDigest": token_digest, "dashboardTokenPrefix": prefix,
            "createdAt": created_at,
        }
        return {**org, "dashboardToken": raw_token}

    def rotate_dashboard_token(self, org_id: str) -> dict[str, Any]:
        if not self.get_org(org_id):
            raise ValueError("Unknown org_id")
        raw_token, token_digest = self._issue_key("dt_")
        prefix = raw_token[:16]
        rotated_at = str(_now_ts())
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute(
                    """
                    UPDATE nina_orgs
                    SET dashboard_token_digest = %s,
                        dashboard_token_prefix = %s,
                        token_rotated_at       = %s
                    WHERE id = %s
                    """,
                    (token_digest, prefix, rotated_at, org_id),
                )
        return {"orgId": org_id, "dashboardToken": raw_token}

    def verify_dashboard_token(self, raw_token: str) -> dict[str, Any] | None:
        digest = self._hash_key(raw_token)
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute(
                    "SELECT * FROM nina_orgs WHERE dashboard_token_digest = %s LIMIT 1",
                    (digest,),
                )
                row = cur.fetchone()
        if not row:
            return None
        stored = row.get("dashboard_token_digest", "")
        if stored and hmac.compare_digest(stored, digest):
            return _org_row(dict(row))
        return None

    # ── site methods ──────────────────────────────────────────────────────────

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
        if not self.get_org(org_id):
            raise ValueError("Unknown org_id")
        if plan not in _PLAN_LIMITS:
            raise ValueError(f"Unknown plan: {plan}. Choose from {list(_PLAN_LIMITS)}")

        site_id = _slug(name)
        if self.get_site(site_id):
            site_id = f"{site_id}-{secrets.token_hex(4)}"

        origin = _parse_origin(base_url)
        origins = allowed_origins or ([origin] if origin else [])
        verification = {"sandbox": "verified", "production": "pending"}
        created_at = str(_now_ts())

        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO nina_sites
                        (id, org_id, name, base_url, plan, currency, locales, markets,
                         allowed_origins, verification, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        site_id, org_id, name, base_url, plan,
                        currency or "INR",
                        _jd(locales or ["en"]),
                        _jd(markets or []),
                        _jd(origins),
                        _jd(verification),
                        created_at,
                    ),
                )
        return {
            "id": site_id, "orgId": org_id, "name": name, "baseUrl": base_url,
            "plan": plan, "currency": currency or "INR",
            "locales": locales or ["en"], "markets": markets or [],
            "allowedOrigins": origins, "verification": verification,
            "createdAt": created_at,
        }

    def set_plan(self, site_id: str, plan: str) -> None:
        if not self.get_site(site_id):
            raise ValueError("Unknown site_id")
        if plan not in _PLAN_LIMITS:
            raise ValueError(f"Unknown plan: {plan}")
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute("UPDATE nina_sites SET plan = %s WHERE id = %s", (plan, site_id))

    def attach_contract(self, site_id: str, contract: dict[str, Any]) -> None:
        if not self.get_site(site_id):
            raise ValueError("Unknown site_id")
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute(
                    "UPDATE nina_sites SET agent_contract = %s WHERE id = %s",
                    (_jd(contract), site_id),
                )

    def attach_llm_config(self, site_id: str, llm_config: dict[str, Any]) -> None:
        if not self.get_site(site_id):
            raise ValueError("Unknown site_id")
        sealed = seal_llm_config(llm_config)
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute(
                    "UPDATE nina_sites SET llm_config = %s WHERE id = %s",
                    (_jd(sealed), site_id),
                )

    def update_site_fields(self, site_id: str, **kwargs: Any) -> None:
        """Update arbitrary site fields. Handles both camelCase and snake_case keys."""
        if not kwargs:
            return
        # Map camelCase to DB column names
        _FIELD_MAP = {
            "waNumberId":   "wa_number_id",
            "verification": "verification",
            "plan":         "plan",
            "currency":     "currency",
            "locales":      "locales",
            "markets":      "markets",
            "allowedOrigins": "allowed_origins",
        }
        parts = []
        values = []
        for key, val in kwargs.items():
            col = _FIELD_MAP.get(key, key)
            parts.append(f"{col} = %s")
            # Serialize lists/dicts as JSON text
            values.append(_jd(val) if isinstance(val, (list, dict)) else val)
        values.append(site_id)
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute(
                    f"UPDATE nina_sites SET {', '.join(parts)} WHERE id = %s",
                    values,
                )

    # ── quota / usage ─────────────────────────────────────────────────────────

    def enforce_quota(self, site_id: str) -> tuple[bool, int, int | None]:
        site = self.get_site(site_id)
        if not site:
            return False, 0, 0
        plan = site.get("plan", "free")
        limit = _PLAN_LIMITS.get(plan)
        period = _current_period()
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute("SELECT calls, period FROM nina_usage WHERE site_id = %s", (site_id,))
                row = cur.fetchone()
        current = (row["calls"] if row and row.get("period") == period else 0)
        if limit is not None and current >= limit:
            return False, current, limit
        return True, current, limit

    def record_usage(self, site_id: str) -> None:
        period = _current_period()
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO nina_usage (site_id, calls, last_call_at, period)
                    VALUES (%s, 1, %s, %s)
                    ON CONFLICT (site_id) DO UPDATE
                        SET calls        = CASE WHEN nina_usage.period = EXCLUDED.period
                                               THEN nina_usage.calls + 1
                                               ELSE 1 END,
                            last_call_at = EXCLUDED.last_call_at,
                            period       = EXCLUDED.period
                    """,
                    (site_id, str(_now_ts()), period),
                )

    def get_usage(self, site_id: str) -> dict[str, Any]:
        period = _current_period()
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute(
                    "SELECT calls, last_call_at, period FROM nina_usage WHERE site_id = %s",
                    (site_id,),
                )
                row = cur.fetchone()
        if not row or row.get("period") != period:
            return {"calls": 0, "lastCallAt": None, "period": period}
        return {"calls": row["calls"], "lastCallAt": row["last_call_at"], "period": row["period"]}

    # ── API keys ──────────────────────────────────────────────────────────────

    def issue_api_key(self, site_id: str, env: str, kind: str) -> dict[str, Any]:
        if not self.get_site(site_id):
            raise ValueError("Unknown site_id")
        if env not in {"test", "live"}:
            raise ValueError("env must be test|live")
        if kind not in {"pk", "sk"}:
            raise ValueError("kind must be pk|sk")
        raw, digest = self._issue_key(f"{kind}_{env}_")
        key_id = _rand_id("key")
        prefix = raw[:14]
        created_at = str(_now_ts())
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO nina_api_keys (id, site_id, environment, kind, prefix, digest, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (key_id, site_id, env, kind, prefix, digest, created_at),
                )
        record = {
            "id": key_id, "siteId": site_id, "environment": env, "kind": kind,
            "prefix": prefix, "digest": digest, "revoked": False, "createdAt": created_at,
        }
        return {**record, "token": raw}

    def resolve_key_to_site(
        self, raw_key: str, origin: str | None
    ) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
        digest = self._hash_key(raw_key)
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute(
                    """
                    SELECT k.*, s.*
                    FROM nina_api_keys k
                    JOIN nina_sites s ON s.id = k.site_id
                    WHERE k.kind = 'pk' AND NOT k.revoked AND k.digest = %s
                    LIMIT 1
                    """,
                    (digest,),
                )
                row = cur.fetchone()
        if not row:
            return False, None, {"code": "UNAUTHORIZED", "message": "Unknown or revoked API key."}
        # Timing-safe compare
        if not hmac.compare_digest(row["digest"], digest):
            return False, None, {"code": "UNAUTHORIZED", "message": "Unknown or revoked API key."}
        # Build a fake row dict to pass through _site_row — the JOIN uses same column names
        site_row = {
            "id": row["site_id"], "org_id": row["org_id"], "name": row["name"],
            "base_url": row["base_url"], "plan": row["plan"], "currency": row["currency"],
            "locales": row["locales"], "markets": row["markets"],
            "allowed_origins": row["allowed_origins"], "verification": row["verification"],
            "agent_contract": row["agent_contract"], "llm_config": row["llm_config"],
            "wa_number_id": row.get("wa_number_id"), "created_at": row["created_at"],
        }
        site = _site_row(site_row)
        if origin and origin not in (site.get("allowedOrigins") or []):
            return False, None, {"code": "UNAUTHORIZED", "message": "Origin is not allowed for this key."}
        return True, site, None

    def verify_publishable_key(self, raw_key: str, site_id: str | None, origin: str | None) -> tuple[bool, dict[str, Any] | None]:
        ok, site, err = self.resolve_key_to_site(raw_key, origin)
        if not ok:
            return False, err
        if site_id and site and site["id"] != site_id:
            return False, {"code": "UNAUTHORIZED", "message": "API key does not match site."}
        return True, None

    # ── CLI tokens ────────────────────────────────────────────────────────────

    def issue_cli_token(self, org_id: str, label: str) -> dict[str, Any]:
        if not self.get_org(org_id):
            raise ValueError("Unknown org_id")
        raw, digest = self._issue_key("nk_")
        token_id = _rand_id("token")
        prefix = raw[:14]
        created_at = str(_now_ts())
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO nina_cli_tokens (id, org_id, label, digest, prefix, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (token_id, org_id, label, digest, prefix, created_at),
                )
        record = {
            "id": token_id, "orgId": org_id, "label": label,
            "digest": digest, "prefix": prefix, "revoked": False, "createdAt": created_at,
        }
        return {**record, "token": raw}

    # ── webhook events ────────────────────────────────────────────────────────

    def push_webhook_event(self, event_type: str, payload: dict[str, Any]) -> int:
        """Append a webhook event. Trims table to keep only the newest 500 rows."""
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute(
                    "INSERT INTO nina_webhook_events (event_type, payload, received_at) VALUES (%s, %s, %s)",
                    (event_type, _jd(payload), str(_now_ts())),
                )
                # Evict oldest rows beyond cap
                cur.execute(
                    """
                    DELETE FROM nina_webhook_events
                    WHERE id NOT IN (
                        SELECT id FROM nina_webhook_events ORDER BY id DESC LIMIT 500
                    )
                    """
                )
                cur.execute("SELECT COUNT(*) AS cnt FROM nina_webhook_events WHERE event_type = %s", (event_type,))
                row = cur.fetchone()
        return row["cnt"] if row else 0

    def list_webhook_events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        with self._conn() as conn:
            with self._cur(conn) as cur:
                if event_type:
                    cur.execute(
                        "SELECT event_type, payload, received_at FROM nina_webhook_events WHERE event_type = %s ORDER BY id",
                        (event_type,),
                    )
                else:
                    cur.execute("SELECT event_type, payload, received_at FROM nina_webhook_events ORDER BY id")
                rows = cur.fetchall()
        return [
            {"type": r["event_type"], "payload": _jl(r["payload"]), "receivedAt": r["received_at"]}
            for r in rows
        ]

    # ── lookup helpers ────────────────────────────────────────────────────────

    def get_org(self, org_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute("SELECT * FROM nina_orgs WHERE id = %s", (org_id,))
                row = cur.fetchone()
        return _org_row(dict(row)) if row else None

    def get_site(self, site_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute("SELECT * FROM nina_sites WHERE id = %s", (site_id,))
                row = cur.fetchone()
        return _site_row(dict(row)) if row else None

    def get_api_key(self, key_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute("SELECT * FROM nina_api_keys WHERE id = %s", (key_id,))
                row = cur.fetchone()
        return _key_row(dict(row)) if row else None

    def list_orgs(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute("SELECT * FROM nina_orgs ORDER BY created_at")
                rows = cur.fetchall()
        return [_org_row(dict(r)) for r in rows]

    def list_sites(self, org_id: str | None = None) -> list[dict[str, Any]]:
        with self._conn() as conn:
            with self._cur(conn) as cur:
                if org_id:
                    cur.execute("SELECT * FROM nina_sites WHERE org_id = %s ORDER BY created_at", (org_id,))
                else:
                    cur.execute("SELECT * FROM nina_sites ORDER BY created_at")
                rows = cur.fetchall()
        return [_site_row(dict(r)) for r in rows]

    def list_api_keys(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute("SELECT * FROM nina_api_keys ORDER BY created_at")
                rows = cur.fetchall()
        return [_key_row(dict(r)) for r in rows]

    def list_api_keys_for_site(self, site_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute(
                    "SELECT id, site_id, environment, kind, prefix, revoked, created_at "
                    "FROM nina_api_keys WHERE site_id = %s ORDER BY created_at",
                    (site_id,),
                )
                rows = cur.fetchall()
        return [_key_row(dict(r)) for r in rows]

    def revoke_api_key(self, key_id: str) -> None:
        if not self.get_api_key(key_id):
            raise ValueError("Unknown key_id")
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute("UPDATE nina_api_keys SET revoked = TRUE WHERE id = %s", (key_id,))

    def revoke_cli_token(self, token_id: str) -> None:
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute("SELECT id FROM nina_cli_tokens WHERE id = %s", (token_id,))
                if not cur.fetchone():
                    raise ValueError("Unknown token_id")
                cur.execute("UPDATE nina_cli_tokens SET revoked = TRUE WHERE id = %s", (token_id,))

    def find_site_by_wa_number(self, wa_number_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute("SELECT * FROM nina_sites WHERE wa_number_id = %s LIMIT 1", (wa_number_id,))
                row = cur.fetchone()
        return _site_row(dict(row)) if row else None

    # ── counts (for /health) ──────────────────────────────────────────────────

    def count_orgs(self) -> int:
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute("SELECT COUNT(*) AS n FROM nina_orgs")
                return cur.fetchone()["n"]

    def count_sites(self) -> int:
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute("SELECT COUNT(*) AS n FROM nina_sites")
                return cur.fetchone()["n"]

    def count_keys(self) -> int:
        with self._conn() as conn:
            with self._cur(conn) as cur:
                cur.execute("SELECT COUNT(*) AS n FROM nina_api_keys")
                return cur.fetchone()["n"]

    # ── embed snippet (pure string, no DB) ───────────────────────────────────

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


# ── inline schema fallback (if scripts/db_init.sql isn't on disk) ─────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nina_orgs (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_email TEXT,
    dashboard_token_digest TEXT, dashboard_token_prefix TEXT,
    token_rotated_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nina_sites (
    id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES nina_orgs(id),
    name TEXT NOT NULL, base_url TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'free',
    currency TEXT NOT NULL DEFAULT 'INR', locales TEXT NOT NULL DEFAULT '["en"]',
    markets TEXT NOT NULL DEFAULT '[]', allowed_origins TEXT NOT NULL DEFAULT '[]',
    verification TEXT NOT NULL DEFAULT '{"sandbox":"verified","production":"pending"}',
    agent_contract TEXT, llm_config TEXT, wa_number_id TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS nina_sites_org_id ON nina_sites(org_id);
CREATE TABLE IF NOT EXISTS nina_api_keys (
    id TEXT PRIMARY KEY, site_id TEXT NOT NULL REFERENCES nina_sites(id),
    environment TEXT NOT NULL, kind TEXT NOT NULL, prefix TEXT NOT NULL,
    digest TEXT NOT NULL, revoked BOOLEAN NOT NULL DEFAULT FALSE, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS nina_api_keys_digest ON nina_api_keys(digest) WHERE NOT revoked;
CREATE TABLE IF NOT EXISTS nina_cli_tokens (
    id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES nina_orgs(id),
    label TEXT NOT NULL, digest TEXT NOT NULL, prefix TEXT NOT NULL,
    revoked BOOLEAN NOT NULL DEFAULT FALSE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nina_webhook_events (
    id SERIAL PRIMARY KEY, event_type TEXT NOT NULL,
    payload TEXT NOT NULL, received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nina_usage (
    site_id TEXT PRIMARY KEY REFERENCES nina_sites(id),
    calls INTEGER NOT NULL DEFAULT 0, last_call_at TEXT, period TEXT
);
"""
