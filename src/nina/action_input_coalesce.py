"""Fill obvious action parameters from the user's message when the LLM
picked the right action but left required fields empty."""

from __future__ import annotations

from typing import Any

_SEARCH_PREFIXES = (
    "show me the ",
    "show me ",
    "show ",
    "find me ",
    "find ",
    "search for ",
    "search ",
    "look for ",
    "looking for ",
    "i want ",
    "i need ",
    "get me ",
    "can i see ",
    "do you have ",
)
_QUESTION_STARTERS = (
    "how ",
    "what ",
    "why ",
    "when ",
    "where ",
    "who ",
    "would you ",
    "is there ",
    "are there ",
)
_POLITE_PREFIXES = (
    "can you please ",
    "could you please ",
    "please ",
    "can you ",
    "could you ",
    "will you ",
    "would you ",
)


def _normalize_for_search(message: str) -> str:
    """Strip polite/question wrappers so 'can you show me hoodies' → 'show me hoodies'."""
    text = message.strip().rstrip(".!?")
    if not text:
        return text
    lower = text.lower()
    changed = True
    while changed:
        changed = False
        for prefix in sorted(_POLITE_PREFIXES, key=len, reverse=True):
            if lower.startswith(prefix):
                text = text[len(prefix) :].strip()
                lower = text.lower()
                changed = True
                break
    return text


def _strip_search_prefix(message: str) -> str | None:
    text = message.strip().rstrip(".!?")
    if not text:
        return None
    lower = text.lower()
    for prefix in sorted(_SEARCH_PREFIXES, key=len, reverse=True):
        if lower.startswith(prefix):
            return text[len(prefix) :].strip()
    return None


def _whole_message_as_query(message: str) -> str | None:
    text = message.strip().rstrip(".!?")
    if not text:
        return None
    lower = text.lower()
    if lower.startswith(_QUESTION_STARTERS):
        return None
    if len(text.split()) > 8:
        return None
    return text


def infer_search_query(user_message: str) -> str | None:
    normalized = _normalize_for_search(user_message)
    stripped = _strip_search_prefix(normalized)
    if stripped:
        return stripped
    return _whole_message_as_query(normalized)


def _enum_values(schema: dict[str, Any], field: str) -> list[str]:
    props = (schema or {}).get("properties") or {}
    field_schema = props.get(field) or {}
    enum = field_schema.get("enum")
    return [str(v) for v in enum] if isinstance(enum, list) else []


def _match_enum_slug(user_message: str, enum_values: list[str]) -> str | None:
    if not enum_values:
        return None
    text = user_message.strip().rstrip(".!?")
    lower = text.lower()
    candidates = [infer_search_query(text), text, lower]
    for candidate in candidates:
        if not candidate:
            continue
        cand = candidate.strip().lower()
        for slug in enum_values:
            slug_lower = slug.lower()
            if cand == slug_lower or slug_lower in cand.split():
                return slug
    return None


def coalesce_action_input(
    action_name: str,
    action_input: dict[str, Any] | None,
    user_message: str,
    input_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(action_input or {})
    schema = input_schema or {}

    if action_name in ("search", "search_products") and not merged.get("query"):
        query = infer_search_query(user_message)
        if query:
            merged["query"] = query

    if action_name == "open_category" and not merged.get("categorySlug"):
        slug = _match_enum_slug(user_message, _enum_values(schema, "categorySlug"))
        if slug:
            merged["categorySlug"] = slug

    return merged
