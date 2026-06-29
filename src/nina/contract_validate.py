"""Executable validation — block publish when actions cannot run."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx

from .contract import (
    expand_api_instruction,
    get_execute_runtime,
    resolve_selector,
    validate_agent,
)
from .generator.stages.validate import validate_contract


def _pad_description(desc: str, action_id: str) -> str:
    text = (desc or action_id).strip()
    if len(text) < 20:
        text = f"{text}. Executed via NINA site contract."
    return text[:500]


def validate_executable(
    contract: dict[str, Any],
    *,
    strict: bool = True,
    probe: bool = False,
    timeout: float = 5.0,
) -> tuple[bool, list[str], list[str]]:
    """
    Validate contract is executable. Returns (ok, errors, warnings).
    strict: DOM-only actions without selectors are errors.
    probe: HTTP HEAD/GET apiRef base URLs.
    """
    errors: list[str] = []
    warnings: list[str] = []

    errors.extend(validate_agent(contract))
    ok_schema, cross_errors = validate_contract(contract)
    if not ok_schema:
        errors.extend(cross_errors)

    selectors = contract.get("selectors") or {}
    page_ids = {p["id"] for p in contract.get("pages", [])}

    for action in contract.get("actions") or []:
        aid = action.get("id", "?")
        execute = action.get("execute") or {}
        etype = execute.get("type", "dom")
        runtime = get_execute_runtime(execute)
        api_ref = execute.get("apiRef")

        for pid in action.get("availableOn") or []:
            if pid not in page_ids:
                errors.append(f"Action '{aid}' availableOn unknown page '{pid}'")

        if etype in ("api", "hybrid"):
            if not api_ref and not any(
                s.get("op") == "api_call" for s in (execute.get("steps") or [])
            ):
                errors.append(
                    f"Action '{aid}' type '{etype}' requires execute.apiRef or api_call step"
                )
            elif api_ref:
                if not api_ref.get("path"):
                    errors.append(f"Action '{aid}' apiRef missing path")
                if runtime == "browser" and not expand_api_instruction(contract, action, {}):
                    errors.append(f"Action '{aid}' browser apiRef could not expand")

        if etype == "dom" or (etype == "hybrid" and runtime == "dom_only"):
            has_dom = False
            for step in execute.get("steps") or []:
                op = step.get("op")
                if op in ("click", "fill", "scroll"):
                    has_dom = True
                    sel = resolve_selector(contract, step)
                    if not sel:
                        msg = f"Action '{aid}' step op '{op}' has no resolvable selector"
                        if strict:
                            errors.append(msg)
                        else:
                            warnings.append(msg)
            if etype == "dom" and not has_dom and not api_ref:
                steps = execute.get("steps") or []
                if not steps:
                    errors.append(f"Action '{aid}' dom execute has no steps")

        if len(_pad_description(action.get("description", ""), aid)) < 20:
            warnings.append(f"Action '{aid}' description shorter than registry minimum")

        if probe and api_ref and runtime == "server":
            site_base = (contract.get("site") or {}).get("baseUrl", "")
            apis = contract.get("apis") or {}
            group = apis.get(api_ref.get("apiId") or "default") or {}
            base = group.get("baseUrl") or site_base
            path = api_ref.get("path", "")
            url = path if path.startswith("http") else urljoin(base.rstrip("/") + "/", path.lstrip("/"))
            try:
                with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                    client.head(url)
            except httpx.HTTPError:
                warnings.append(f"Action '{aid}' API probe failed for {url}")

    return len(errors) == 0, errors, warnings
