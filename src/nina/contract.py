"""Load, validate, and resolve NINA site contracts (agent.json)."""

from __future__ import annotations

import json
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jsonschema

from .api_template import apply_params_to_string, build_request_body, resolve_api_url

_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"


def _schemas_dir() -> Path:
    """Locate schemas/ in dev (repo root), Docker (/app/schemas), or editable installs."""
    here = Path(__file__).resolve()
    for base in (here.parents[2], here.parents[1], Path("/app"), Path.cwd()):
        candidate = base / "schemas"
        if (candidate / "agent.schema.json").is_file():
            return candidate
    return _SCHEMAS_DIR


def _load_schema(name: str) -> dict[str, Any]:
    path = _schemas_dir() / name
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def validate_agent(contract: dict[str, Any]) -> list[str]:
    """Validate contract against agent.schema.json. Returns error messages."""
    schema = _load_schema("agent.schema.json")
    validator = jsonschema.Draft202012Validator(schema)
    return [e.message for e in sorted(validator.iter_errors(contract), key=lambda e: e.path)]


def load_agent(path: str | Path) -> dict[str, Any]:
    """Load and validate agent.json from disk."""
    p = Path(path)
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    errors = validate_agent(data)
    if errors:
        raise ValueError("Invalid agent.json:\n" + "\n".join(f"  - {e}" for e in errors))
    return data


def _match_routes(routes: list[dict[str, Any]], path: str, normalized: str) -> str | None:
    best: tuple[int, int, str | None] = (-1, -1, None)
    for route in routes:
        pattern = route.get("pattern", "")
        page_id = route.get("pageId")
        if not pattern or not page_id:
            continue
        matched = (
            path == pattern
            or normalized == pattern.rstrip("/")
            or fnmatch(path, pattern)
            or fnmatch(normalized, pattern)
        )
        if not matched:
            continue
        exact = 1 if (path == pattern or normalized == pattern.rstrip("/")) else 0
        specificity = len(pattern.replace("*", ""))
        score = (exact, specificity)
        if score >= (best[0], best[1]):
            best = (score[0], score[1], page_id)
    return best[2]


def match_page_id(contract: dict[str, Any], url: str) -> str | None:
    """Match URL path against routes, then pages[].urlPattern; return page id."""
    parsed = urlparse(url)
    path = parsed.path or "/"
    normalized = path.rstrip("/") or "/"

    route_hit = _match_routes(contract.get("routes") or [], path, normalized)
    if route_hit:
        return route_hit

    best: tuple[int, int, str | None] = (-1, -1, None)
    for page in contract.get("pages", []):
        pattern = page.get("urlPattern", "")
        if not pattern:
            continue
        matched = (
            path == pattern
            or normalized == pattern.rstrip("/")
            or fnmatch(path, pattern)
            or fnmatch(normalized, pattern)
        )
        if not matched:
            continue
        exact = 1 if (path == pattern or normalized == pattern.rstrip("/")) else 0
        specificity = len(pattern.replace("*", ""))
        score = (exact, specificity)
        if score >= (best[0], best[1]):
            best = (score[0], score[1], page["id"])
    return best[2]


def get_action(contract: dict[str, Any], action_id: str) -> dict[str, Any] | None:
    for action in contract.get("actions", []):
        if action.get("id") == action_id:
            return action
    return None


def action_available_on_page(
    contract: dict[str, Any],
    action: dict[str, Any],
    page_id: str | None,
) -> bool:
    available = action.get("availableOn")
    if not available:
        return True
    if page_id and page_id in available:
        return True
    return False


def get_execute_runtime(execute: dict[str, Any]) -> str:
    if execute.get("runtime"):
        return execute["runtime"]
    etype = execute.get("type", "dom")
    if etype == "api":
        return "server"
    if etype == "message":
        return "dom_only"
    return "dom_only"


def expand_api_instruction(
    contract: dict[str, Any],
    action: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build a single browser api_call instruction from execute.apiRef."""
    execute = action.get("execute") or {}
    api_ref = execute.get("apiRef")
    if not api_ref:
        return None
    params = params or {}
    method = (api_ref.get("method") or "GET").upper()
    url = resolve_api_url(contract, api_ref, params)
    body = build_request_body(api_ref, params)
    inst: dict[str, Any] = {
        "type": "api_call",
        "method": method,
        "url": url,
        "runtime": "browser",
        "_actionId": action.get("id"),
    }
    if body is not None and method not in ("GET", "DELETE"):
        inst["body"] = body
    elif body is not None and method in ("GET", "DELETE"):
        inst["query"] = body
    response_map = api_ref.get("responseMap")
    if response_map:
        inst["responseMap"] = response_map
        for render_type, field in response_map.items():
            if render_type.startswith("render_"):
                inst["render"] = render_type
                inst["renderField"] = field
                break
    return inst


def resolve_selector(contract: dict[str, Any], step: dict[str, Any]) -> str | None:
    if step.get("selector"):
        return step["selector"]
    sid = step.get("selectorId")
    if sid:
        return (contract.get("selectors") or {}).get(sid)
    return None


def expand_dom_steps(
    contract: dict[str, Any],
    action: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Expand optional UI/DOM execute.steps (toast, click, fill, etc.)."""
    params = params or {}
    instructions: list[dict[str, Any]] = []
    execute = action.get("execute") or {}
    api_ref = execute.get("apiRef") or {}
    for step in execute.get("steps") or []:
        op = step.get("op")
        if not op:
            continue
        inst: dict[str, Any] = {
            "type": op if op != "scroll" else "scroll_to",
            "_actionId": action.get("id"),
        }
        selector = resolve_selector(contract, step)
        if not selector and step.get("selector"):
            selector = step["selector"]
        if selector:
            inst["selector"] = apply_params_to_string(selector, params)
        if step.get("selectorId"):
            inst["selectorId"] = step["selectorId"]
        if op == "fill":
            inst["value"] = str(params.get(step.get("param", ""), step.get("value", "")))
        elif op == "navigate":
            inst["url"] = apply_params_to_string(step.get("url", ""), params)
        elif op == "api_call":
            inst["method"] = (step.get("method") or "GET").upper()
            inst["runtime"] = "browser"
            if step.get("url"):
                inst["url"] = apply_params_to_string(step["url"], params)
            else:
                inst["url"] = resolve_api_url(contract, {**api_ref, **step}, params)
            body = step.get("body")
            if body is not None:
                from .api_template import apply_params_to_object
                inst["body"] = apply_params_to_object(body, params)
            elif api_ref:
                built = build_request_body({**api_ref, **step}, params, step_body=None)
                if built is not None:
                    inst["body"] = built
        elif op == "wait":
            inst["type"] = "wait"
            inst["ms"] = step.get("ms", 0)
        elif op in ("toast", "show_message"):
            inst["message"] = step.get("message", "")
            if step.get("level"):
                inst["level"] = step["level"]
        elif op == "scroll":
            inst["block"] = step.get("block", "start")
        instructions.append(inst)
    return instructions


def expand_execute_steps(
    contract: dict[str, Any],
    action: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Expand contract execute into runtime instructions (API + optional DOM)."""
    params = params or {}
    execute = action.get("execute") or {}
    runtime = get_execute_runtime(execute)
    etype = execute.get("type", "dom")
    out: list[dict[str, Any]] = []

    if etype in ("api", "hybrid") and runtime == "browser":
        api_inst = expand_api_instruction(contract, action, params)
        if api_inst:
            out.append(api_inst)

    dom_ops = {"click", "fill", "scroll", "navigate", "wait", "toast", "show_message"}
    for step in expand_dom_steps(contract, action, params):
        if runtime == "server" and step.get("type") == "api_call":
            continue
        if runtime == "server" and step.get("type") not in dom_ops:
            continue
        out.append(step)

    if not out and etype == "api" and runtime == "browser":
        api_inst = expand_api_instruction(contract, action, params)
        if api_inst:
            out.append(api_inst)

    if not out:
        out = expand_dom_steps(contract, action, params)
    return out


def is_authenticated(contract: dict[str, Any], session_hints: dict[str, Any] | None) -> bool:
    """Best-effort auth check from client session hints."""
    session_hints = session_hints or {}
    auth = contract.get("auth") or {}
    indicator = auth.get("sessionIndicator") or {}
    itype = indicator.get("type")
    if itype == "cookie":
        cookies = session_hints.get("cookies") or {}
        name = indicator.get("name", "")
        return bool(name and cookies.get(name))
    if itype == "localStorage":
        storage = session_hints.get("localStorage") or {}
        name = indicator.get("name", "")
        return bool(name and storage.get(name))
    if itype == "selector":
        return bool(session_hints.get("selectorPresent"))
    return bool(session_hints.get("authenticated"))


def resolve_intent(
    contract: dict[str, Any],
    *,
    intent: str,
    params: dict[str, Any] | None = None,
    confidence: float = 1.0,
    page_id: str | None = None,
    page_url: str | None = None,
    session_hints: dict[str, Any] | None = None,
    confidence_threshold: float = 0.75,
    confirmed: bool = False,
) -> dict[str, Any]:
    """
    Deterministic resolver: intent + context → instructions envelope.

    Returns { ok, instructions, error_code, message }.
    """
    params = params or {}
    action = get_action(contract, intent)
    if not action:
        return {
            "ok": False,
            "instructions": [{"type": "no_match", "reason": "unknown_action", "suggestion": "Try rephrasing."}],
            "error_code": "UNKNOWN_ACTION",
            "message": f"Action '{intent}' is not in the site contract.",
        }

    if confidence < confidence_threshold:
        return {
            "ok": False,
            "instructions": [],
            "error_code": "LOW_CONFIDENCE",
            "message": "Could you clarify what you want to do?",
        }

    if not action_available_on_page(contract, action, page_id):
        return {
            "ok": False,
            "instructions": [{
                "type": "no_match",
                "reason": "wrong_page",
                "suggestion": f"'{intent}' is not available on this page.",
            }],
            "error_code": "WRONG_PAGE",
            "message": f"Action '{intent}' is not available on this page.",
        }

    risk = contract.get("risk") or {}
    if intent in (risk.get("blockActions") or []):
        return {
            "ok": False,
            "instructions": [{"type": "no_match", "reason": "blocked", "suggestion": "This action is not allowed."}],
            "error_code": "BLOCKED",
            "message": "This action is blocked by site policy.",
        }

    gated = (contract.get("auth") or {}).get("gatedActions") or []
    needs_auth = action.get("requiresAuth") or intent in gated
    if needs_auth and not is_authenticated(contract, session_hints):
        login_url = (contract.get("auth") or {}).get("loginUrl", "/login")
        return {
            "ok": True,
            "instructions": [{
                "type": "needs_login",
                "loginUrl": login_url,
                "queuedIntent": {"intent": intent, "params": params},
            }],
            "error_code": None,
            "message": "Please sign in to continue.",
        }

    confirm_list = risk.get("confirmActions") or []
    if intent in confirm_list and not confirmed:
        return {
            "ok": True,
            "instructions": [{
                "type": "confirm",
                "actionId": intent,
                "message": f"Confirm you want to run '{intent}'?",
                "params": params,
            }],
            "error_code": None,
            "message": "Confirmation required.",
        }

    for pname, spec in (action.get("parameters") or {}).items():
        if spec.get("required") and pname not in params:
            return {
                "ok": False,
                "instructions": [],
                "error_code": "MISSING_PARAM",
                "message": f"Missing required parameter: {pname}",
            }

    instructions = expand_execute_steps(contract, action, params)
    return {
        "ok": True,
        "instructions": instructions,
        "error_code": None,
        "message": action.get("description", ""),
    }


def validate_report(report: dict[str, Any]) -> list[str]:
    schema = _load_schema("report-broken-selector.schema.json")
    validator = jsonschema.Draft202012Validator(schema)
    return [e.message for e in sorted(validator.iter_errors(report), key=lambda e: e.path)]


def recovery_for_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Produce recovery instructions from a broken-selector report."""
    failures = report.get("failures") or []
    if not failures:
        return [{"type": "show_message", "message": "No failures in report."}]
    actions = {f.get("actionId") for f in failures}
    site_id = report.get("siteId") or "your-site"
    return [{
        "type": "no_match",
        "reason": "selector_failure",
        "suggestion": (
            f"The page layout may have changed ({len(failures)} step(s) failed "
            f"for actions: {', '.join(sorted(a for a in actions if a))}). "
            "Try the action manually, or run "
            f"`nina-generate contracts/{site_id} --heal-from reports.json` "
            "after exporting failure reports."
        ),
    }]
