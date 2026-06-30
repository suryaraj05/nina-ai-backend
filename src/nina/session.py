"""Session state, history window, pending flows, TTL, and reference map (spec §4)."""
from __future__ import annotations

import inspect
import json
import re
import difflib
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import StoreError, fail, now_iso, ok

# Approx character budget for retained history (~1 token per 4 chars). Caps the
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
_LIST_KEYS = ("results", "items", "products", "matches", "hits", "suggestions")
_ID_PARAM_NAME_RE = re.compile(r"(?:^id$|^sku$|Id$)")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ORDINAL_RE = re.compile(
    r"\b(?:(\d+)\s*(?:st|nd|rd|th)|(?:the\s+)?(first|second|third|fourth|fifth|last))\b",
    re.IGNORECASE,
)
_ORDINAL_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "last": -1,
}
_ADD_TO_CART_RE = re.compile(r"add\s+(.+?)\s+to\s+(?:my\s+)?cart", re.IGNORECASE)
_PRODUCT_REFERENCE_ACTIONS = frozenset({
    "add_to_cart",
    "add_item_to_cart",
    "remove_from_cart",
    "update_cart_item",
    "open_product",
    "get_detail",
    "get_product_detail",
})


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
        # Zero-hit search may still carry fallback picks under suggestions only.
        sug = result.get("suggestions")
        if isinstance(sug, list) and sug and all(isinstance(x, dict) for x in sug):
            return sug
    return None


# --- Product cards: map an action result into shopper-facing cards -----------
# Heuristic mapping that covers common commerce JSON (Shopify products.json,
# generic search APIs). The widget renders whatever fields are present.

_PRICE_KEYS = ("price", "amount", "cost", "salePrice", "sale_price", "minPrice", "min_price")
_IMAGE_KEYS = ("image", "imageUrl", "image_url", "img", "thumbnail", "thumb",
               "photo", "featuredImage", "featured_image")
_URL_KEYS = ("url", "link", "href", "productUrl", "product_url", "permalink")
_CURRENCY_KEYS = ("currency", "currencyCode", "currency_code")
# Action names for which result items are NOT browsable products (cart/checkout
# mutations, auth) — showing cards there would be wrong.
_NON_PRODUCT_ACTION_HINTS = ("add", "cart", "checkout", "remove", "buy", "order", "pay", "login", "logout")


def _first(item, keys):
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return v
    return None


def _image_of(item):
    v = _first(item, _IMAGE_KEYS)
    if isinstance(v, str):
        return v
    if isinstance(v, dict):                       # e.g. Shopify image object
        return v.get("src") or v.get("url")
    imgs = item.get("images")
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("src") or first.get("url")
    return None


def _price_of(item):
    v = _first(item, _PRICE_KEYS)
    if isinstance(v, dict):                        # {amount, currencyCode}
        v = v.get("amount") or v.get("value")
    if isinstance(v, (int, float, str)):
        return v
    variants = item.get("variants")               # Shopify: variants[0].price
    if isinstance(variants, list) and variants and isinstance(variants[0], dict):
        return variants[0].get("price")
    return None


def products_from_result(result, action_name="", *, limit=8, base_url=None):
    """Map an action result into a list of product cards for the widget.

    Returns [] for cart/checkout/auth actions and for results that don't look
    like a product listing, so it's safe to call on every action turn.
    """
    if action_name and any(w in action_name.lower() for w in _NON_PRODUCT_ACTION_HINTS):
        return []
    listing = _extract_list(result)
    if listing is None:
        if isinstance(result, dict) and any(k in result for k in _ITEM_ID_KEYS + _ITEM_NAME_KEYS):
            listing = [result]
        else:
            return []

    products = []
    for item in listing[:limit]:
        if not isinstance(item, dict):
            continue
        title = _first(item, _ITEM_NAME_KEYS)
        if not title:
            continue  # a card without a name is useless
        card = {"title": str(title)}
        pid = _first(item, _ITEM_ID_KEYS)
        if pid is not None:
            card["id"] = pid
        price = _price_of(item)
        if price is not None and price != "":
            card["price"] = price
        currency = _first(item, _CURRENCY_KEYS)
        if currency:
            card["currency"] = currency
        image = _image_of(item)
        if image:
            card["image"] = image
        url = _first(item, _URL_KEYS)
        if not url and base_url and item.get("handle"):   # Shopify handle -> URL
            url = base_url.rstrip("/") + "/products/" + str(item["handle"])
        if url:
            card["url"] = url
        products.append(card)
    return products


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


def _ordinal_from_message(user_message: str) -> int | None:
    for match in _ORDINAL_RE.finditer(user_message or ""):
        if match.group(1):
            try:
                return int(match.group(1))
            except ValueError:
                continue
        word = (match.group(2) or "").lower()
        if word in _ORDINAL_WORDS:
            return _ORDINAL_WORDS[word]
    return None


def _name_hint_from_message(user_message: str) -> str | None:
    text = (user_message or "").strip()
    add_match = _ADD_TO_CART_RE.search(text)
    if add_match:
        return add_match.group(1).strip(" .\"'")
    return None


def _title_of(candidate: dict[str, Any]) -> str | None:
    return next((candidate.get(k) for k in _ITEM_NAME_KEYS if candidate.get(k)), None)


def _pick_search_candidate(
    candidates: list[dict[str, Any]],
    user_message: str,
) -> dict[str, Any] | None:
    if not candidates:
        return None
    ordinal = _ordinal_from_message(user_message)
    if ordinal is not None:
        if ordinal == -1:
            return candidates[-1]
        for item in candidates:
            if item.get("index") == ordinal:
                return item
        if 1 <= ordinal <= len(candidates):
            return candidates[ordinal - 1]
    name_hint = _name_hint_from_message(user_message)
    msg_lower = (user_message or "").lower()
    probe = (name_hint or user_message or "").strip().lower()
    if not probe:
        return None

    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in candidates:
        title = _title_of(item)
        if not title:
            continue
        title_lower = str(title).lower()
        score = 0.0
        if probe == title_lower or title_lower in probe or probe in title_lower:
            score = 1.0
        else:
            score = difflib.SequenceMatcher(None, probe, title_lower).ratio()
        if score > best_score:
            best_score = score
            best = item
    if best is not None and best_score >= 0.55:
        return best

    if "it" in msg_lower.split() or "that one" in msg_lower or "this one" in msg_lower:
        if len(candidates) == 1:
            return candidates[0]
    return None


def resolve_product_reference(
    state: dict[str, Any],
    action_name: str,
    action_input: dict[str, Any] | None,
    user_message: str,
    *,
    catalog_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Map ordinals and product titles to real ids from lastSearchResults."""
    if action_name not in _PRODUCT_REFERENCE_ACTIONS:
        return dict(action_input or {})
    out = dict(action_input or {})
    if _item_id(out) and str(_item_id(out)) in {
        str(_item_id(c)) for c in (state.get("referenceMap") or {}).get("lastSearchResults") or []
        if _item_id(c) is not None
    }:
        return out

    rm = state.get("referenceMap") or {}
    candidates = list(rm.get("lastSearchResults") or [])
    if rm.get("lastSingleItem"):
        candidates.append(rm["lastSingleItem"])
    picked = _pick_search_candidate(candidates, user_message)

    if not picked and catalog_rows:
        from .catalog_rail import graph_from_rows
        hint = _name_hint_from_message(user_message) or user_message
        product = graph_from_rows(catalog_rows).lookup_name(hint)
        if product:
            picked = {
                "id": product.sku,
                "sku": product.sku,
                "name": product.name,
                "title": product.name,
            }

    if not picked:
        return out

    pid = _item_id(picked)
    title = _title_of(picked)
    if pid is not None:
        for key in ("productId", "variantId", "sku", "id"):
            if not out.get(key):
                out[key] = pid
    if title:
        for key in ("name", "query", "title"):
            if not out.get(key):
                out[key] = title
    return out


def seed_reference_map_from_client(
    state: dict[str, Any],
    session_hints: dict[str, Any] | None,
) -> None:
    """Restore lastSearchResults from the widget when server session was lost."""
    hints = session_hints or {}
    raw = hints.get("lastSearchResults") or hints.get("visibleProducts")
    if not isinstance(raw, list) or not raw:
        return
    rm = state.setdefault("referenceMap", _empty_reference_map())
    if rm.get("lastSearchResults"):
        return
    rows: list[dict[str, Any]] = []
    for i, item in enumerate(raw[:10]):
        if not isinstance(item, dict):
            continue
        row = {"index": i + 1, "sourceAction": "widget"}
        for key in _ITEM_ID_KEYS + _ITEM_NAME_KEYS + ("price", "currency"):
            if item.get(key) not in (None, ""):
                row[key] = item[key]
        if _item_id(row) or _title_of(row):
            rows.append(row)
    if rows:
        rm["lastSearchResults"] = rows


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
