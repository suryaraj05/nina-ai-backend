"""Session state, history window, pending flows, TTL, and reference map (spec §4)."""
from __future__ import annotations

import inspect
import json
import re
from datetime import datetime, timedelta, timezone

from .errors import StoreError, fail, now_iso, ok

# Approx character budget for retained history (≈1 token per 4 chars). Caps the
# context contributed by history so a few large API-result payloads can't blow
# past a small model's window even when the turn count is within max_turns.
_HISTORY_CHAR_BUDGET = 16000  # ~4k tokens


def _prune_history_to_budget(history: list[dict], char_budget: int) -> list[dict]:
    """Keep the most recent entries that fit within *char_budget* characters.
    Always keeps at least the last entry so a single huge turn still survives."""
    kept: list[dict] = []
    total = 0
    for entry in reversed(history):
        size = len(entry.get("content") or "") + len(entry.get("actionSummary") or "")
        if kept and total + size > char_budget:
            break
        total += size
        kept.append(entry)
    kept.reverse()
    return kept


# --- Capability 3: reference map ---------------------------------------------

_ITEM_ID_KEYS = ("id", "sku", "productId", "uid")
_ITEM_NAME_KEYS = ("name", "title", "displayName", "label")
_LIST_KEYS = ("results", "items", "products", "matches", "hits")
_ID_PARAM_NAME_RE = re.compile(r"(?:^id$|^sku$|Id$)")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _empty_reference_map():
    return {
        "lastSearchResults": [],
        "lastSingleItem": None,
        "cartContents": None,
        "lastActionResult": None,
    }


def _compact(item, limit=8):
    if not isinstance(item, dict):
        return {"value": item}
    out = {}
    for key in _ITEM_ID_KEYS + _ITEM_NAME_KEYS:
        if key in item:
            out[key] = item[key]
    for k, v in item.items():
        if k not in out and isinstance(v, (str, int, float, bool)) and len(out) < limit:
            out[k] = v
    return out


def _item_id(item):
    if not isinstance(item, dict):
        return None
    return next((item[k] for k in _ITEM_ID_KEYS if k in item), None)


def _extract_list(result):
    if isinstance(result, list) and result and all(isinstance(x, dict) for x in result):
        return result
    if isinstance(result, dict):
        for key in _LIST_KEYS:
            v = result.get(key)
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                return v
    return None


def update_reference_map(state, action_name, result):
    """Refresh the reference map after a successful action turn."""
    rm = state.setdefault("referenceMap", _empty_reference_map())
    rm["lastActionResult"] = {
        "action": action_name,
        "summary": json.dumps(result, ensure_ascii=False, default=str)[:500],
    }
    is_cart = "cart" in action_name or (
        isinstance(result, dict) and "cart" in result
    )
    if is_cart:
        cart = (
            result.get("cart")
            if isinstance(result, dict) and isinstance(result.get("cart"), dict)
            else result
        )
        items = cart.get("items") if isinstance(cart, dict) else None
        rm["cartContents"] = {
            "sourceAction": action_name,
            "items": [_compact(i) for i in items]
            if isinstance(items, list)
            else _compact(cart),
        }
        return
    listing = _extract_list(result)
    if listing is not None:
        rm["lastSearchResults"] = [
            {"index": i + 1, "sourceAction": action_name, **_compact(item)}
            for i, item in enumerate(listing[:10])
        ]
        rm["lastSingleItem"] = None
        return
    if isinstance(result, dict) and any(
        k in result for k in _ITEM_ID_KEYS + _ITEM_NAME_KEYS
    ):
        rm["lastSingleItem"] = {"sourceAction": action_name, **_compact(result)}
        results = rm.get("lastSearchResults") or []
        if results and results[0].get("sourceAction") != action_name:
            known_ids = {_item_id(r) for r in results}
            if _item_id(result) not in known_ids:
                rm["lastSearchResults"] = []


def _slugify(text) -> str:
    return _SLUG_RE.sub("-", str(text).lower()).strip("-")


def resolve_reference_parameters(state, action_input):
    """Correct id-shaped params the model derived from a slug/title instead of
    the real id in the reference map (e.g. variantId="probook-air-14" instead
    of the catalog's real id). LLMs — especially smaller/local ones — reliably
    fabricate a plausible-looking id from display text under conversational
    pressure even when told not to; this makes the fix deterministic instead
    of depending on prompt wording holding up turn after turn.
    """
    rm = state.get("referenceMap") or {}
    candidates = list(rm.get("lastSearchResults") or [])
    if rm.get("lastSingleItem"):
        candidates.append(rm["lastSingleItem"])
    if not candidates or not isinstance(action_input, dict):
        return action_input

    known_ids = {str(_item_id(c)) for c in candidates if _item_id(c) is not None}
    out = dict(action_input)
    for key, value in action_input.items():
        if not isinstance(value, str) or not _ID_PARAM_NAME_RE.search(key):
            continue
        if value in known_ids:
            continue
        target_slug = _slugify(value)
        for c in candidates:
            real_id = _item_id(c)
            title = next((c[k] for k in _ITEM_NAME_KEYS if c.get(k)), None)
            if real_id is not None and title and _slugify(title) == target_slug:
                out[key] = real_id
                break
    return out


class MemoryStore:
    def __init__(self):
        self._data: dict[str, dict] = {}

    async def get(self, session_id):
        return self._data.get(session_id)

    async def set(self, session_id, state):
        self._data[session_id] = state

    async def delete(self, session_id):
        self._data.pop(session_id, None)


async def _call(fn, *args):
    res = fn(*args)
    if inspect.isawaitable(res):
        res = await res
    return res


class SessionManager:
    def __init__(self, store, ttl_seconds: int, max_turns: int, is_custom: bool):
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.max_turns = max_turns
        self.is_custom = is_custom

    def _expires_at(self):
        if not self.ttl_seconds:
            return None
        return (
            datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
        ).isoformat()

    def _new_state(self, session_id: str) -> dict:
        ts = now_iso()
        return {
            "sessionId": session_id,
            "createdAt": ts,
            "lastActiveAt": ts,
            "expiresAt": self._expires_at(),
            "turnCount": 0,
            "history": [],
            "pending": None,
            "pendingPlan": None,
            "queuedIntent": None,
            "authReplayPending": False,
            "planResumePending": False,
            "data": {},
            "referenceMap": _empty_reference_map(),
        }

    async def _get(self, session_id: str) -> dict | None:
        try:
            state = await _call(self.store.get, session_id)
        except Exception as exc:
            raise StoreError("get", str(exc)) from exc
        if state and state.get("expiresAt"):
            if datetime.fromisoformat(state["expiresAt"]) <= datetime.now(
                timezone.utc
            ):
                await self.delete(session_id)
                return None
        return state

    async def get(self, session_id: str) -> dict | None:
        return await self._get(session_id)

    async def load_or_create(self, session_id: str) -> dict:
        return await self._get(session_id) or self._new_state(session_id)

    async def save(self, state: dict):
        state["lastActiveAt"] = now_iso()
        state["expiresAt"] = self._expires_at()
        # Each turn appends two entries (user + nina), so keep max_turns*2 entries
        # to retain the configured number of complete turns, then additionally
        # prune by character budget so oversized payloads can't bloat the context.
        history = state["history"][-(self.max_turns * 2) :]
        state["history"] = _prune_history_to_budget(history, _HISTORY_CHAR_BUDGET)
        try:
            await _call(self.store.set, state["sessionId"], state)
        except Exception as exc:
            raise StoreError("set", str(exc)) from exc

    async def delete(self, session_id: str):
        try:
            await _call(self.store.delete, session_id)
        except Exception as exc:
            raise StoreError("delete", str(exc)) from exc


def _view(state: dict) -> dict:
    """Session object per §4 — strips internal-only fields."""
    history = [
        {k: v for k, v in entry.items() if k != "actionSummary"}
        for entry in state["history"]
    ]
    return {
        "sessionId": state["sessionId"],
        "createdAt": state["createdAt"],
        "lastActiveAt": state["lastActiveAt"],
        "expiresAt": state["expiresAt"],
        "turnCount": state["turnCount"],
        "history": history,
        "pending": state["pending"],
        "data": state["data"],
    }


class SessionAPI:
    """Exposed as nina.session — callable (== get) plus management operations."""

    def __init__(self, core):
        self._core = core

    def __call__(self, session_id):
        return self.get(session_id)

    def _guard(self, session_id):
        if not self._core.initialized:
            return fail("NINA_NOT_INITIALIZED", "Call nina.init() first.")
        if not isinstance(session_id, str) or not session_id:
            return fail(
                "NINA_SESSION_ID_INVALID",
                "sessionId must be a non-empty string.",
            )
        return None

    async def _load(self, session_id):
        state = await self._core.sessions.get(session_id)
        if state is None:
            return None, fail(
                "NINA_SESSION_NOT_FOUND", f"No session '{session_id}'."
            )
        return state, None

    async def get(self, session_id):
        if err := self._guard(session_id):
            return err
        try:
            state, err = await self._load(session_id)
            return err or ok(_view(state))
        except StoreError as exc:
            return fail(
                "NINA_SESSION_STORE_FAILURE",
                f"Session store operation '{exc.op}' failed.",
                {"reason": exc.reason},
            )

    async def set_data(self, session_id, data):
        if err := self._guard(session_id):
            return err
        if not isinstance(data, dict):
            return fail(
                "NINA_SESSION_DATA_INVALID",
                "Session data must be JSON-serializable.",
            )
        try:
            json.dumps(data)
        except (TypeError, ValueError):
            return fail(
                "NINA_SESSION_DATA_INVALID",
                "Session data must be JSON-serializable.",
            )
        try:
            state, err = await self._load(session_id)
            if err:
                return err
            state["data"] = {**state["data"], **data}
            await self._core.sessions.save(state)
            return ok(state["data"])
        except StoreError as exc:
            return fail(
                "NINA_SESSION_STORE_FAILURE",
                f"Session store operation '{exc.op}' failed.",
                {"reason": exc.reason},
            )

    async def clear_pending(self, session_id):
        if err := self._guard(session_id):
            return err
        try:
            state, err = await self._load(session_id)
            if err:
                return err
            state["pending"] = None
            await self._core.sessions.save(state)
            return ok(_view(state))
        except StoreError as exc:
            return fail(
                "NINA_SESSION_STORE_FAILURE",
                f"Session store operation '{exc.op}' failed.",
                {"reason": exc.reason},
            )

    async def reset(self, session_id):
        if err := self._guard(session_id):
            return err
        try:
            state, err = await self._load(session_id)
            if err:
                return err
            state["history"] = []
            state["pending"] = None
            state["pendingPlan"] = None
            state["queuedIntent"] = None
            state["turnCount"] = 0
            state["referenceMap"] = _empty_reference_map()
            await self._core.sessions.save(state)
            return ok({"sessionId": session_id, "reset": True})
        except StoreError as exc:
            return fail(
                "NINA_SESSION_STORE_FAILURE",
                f"Session store operation '{exc.op}' failed.",
                {"reason": exc.reason},
            )

    async def delete(self, session_id):
        if err := self._guard(session_id):
            return err
        try:
            state, err = await self._load(session_id)
            if err:
                return err
            await self._core.sessions.delete(session_id)
            return ok({"sessionId": session_id, "deleted": True})
        except StoreError as exc:
            return fail(
                "NINA_SESSION_STORE_FAILURE",
                f"Session store operation '{exc.op}' failed.",
                {"reason": exc.reason},
            )

    async def set_queued_intent(self, session_id, intent, params=None):
        """Persist intent to replay after user logs in on the site."""
        if err := self._guard(session_id):
            return err
        try:
            from .auth_queue import save_queued_intent

            state = await self._core.sessions.load_or_create(session_id)
            save_queued_intent(state, intent, params)
            await self._core.sessions.save(state)
            return ok({"queuedIntent": state["queuedIntent"]})
        except StoreError as exc:
            return fail(
                "NINA_SESSION_STORE_FAILURE",
                f"Session store operation '{exc.op}' failed.",
                {"reason": exc.reason},
            )

    async def schedule_plan(self, session_id, steps):
        """Schedule a multi-action plan (max 5 steps)."""
        if err := self._guard(session_id):
            return err
        if not isinstance(steps, list) or not steps:
            return fail(
                "NINA_PLAN_INVALID",
                "Plan steps must be a non-empty list.",
            )
        try:
            from .planner import MAX_PLAN_STEPS, plan_status, schedule

            state, err = await self._load(session_id)
            if err:
                return err
            if len(steps) > MAX_PLAN_STEPS:
                return fail(
                    "NINA_PLAN_TOO_LONG",
                    f"Plan exceeds maximum of {MAX_PLAN_STEPS} steps.",
                )
            if not schedule(state, steps):
                return fail("NINA_PLAN_INVALID", "Could not schedule plan.")
            await self._core.sessions.save(state)
            return ok(plan_status(state))
        except StoreError as exc:
            return fail(
                "NINA_SESSION_STORE_FAILURE",
                f"Session store operation '{exc.op}' failed.",
                {"reason": exc.reason},
            )
