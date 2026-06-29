"""Live endpoint verification — tests each scanned route against a running server.

Called when --verify flag is passed to nina-scan. Sends OPTIONS or GET to each
endpoint and records pass/fail. Admin routes are skipped by default (they
require auth tokens we don't have).

Output is added to manifest["verification"] — does not change routes or checksum.
"""
from __future__ import annotations

import sys
import time
from typing import Any

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False


def verify_manifest(
    manifest: dict[str, Any],
    base_url: str,
    *,
    timeout: float = 5.0,
    skip_auth_routes: bool = True,
    bearer_token: str = "",
) -> dict[str, Any]:
    """Probe each route and annotate the manifest with verification results.

    Args:
        manifest:          The manifest to verify (output of sign_manifest()).
        base_url:          Server root, e.g. "http://localhost:8000".
        timeout:           Per-request timeout in seconds.
        skip_auth_routes:  If True, skip routes that require authentication
                           (we can't authenticate without credentials).
        bearer_token:      Optional Bearer token to send with auth-required routes.
    """
    if not _HAS_HTTPX:
        print("[nina-scan] Warning: httpx not installed — skipping live verification.", file=sys.stderr)
        return manifest

    results: list[dict[str, Any]] = []
    passed = 0
    failed = 0
    skipped = 0

    routes_to_check = manifest.get("routes", [])
    # Also verify admin routes if token is supplied
    if bearer_token:
        routes_to_check = routes_to_check + manifest.get("adminRoutes", [])

    headers = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for route in routes_to_check:
            path = route["path"]
            method = route.get("method", "GET")
            auth_req = route.get("authRequired", False)

            if auth_req and skip_auth_routes and not bearer_token:
                results.append({
                    "path": path,
                    "method": method,
                    "status": "skipped",
                    "reason": "auth_required",
                })
                skipped += 1
                continue

            # Replace path params with placeholder values for probing
            probe_path = _fill_path_params(path, route.get("pathParams", []))
            url = base_url.rstrip("/") + probe_path

            t0 = time.time()
            try:
                # Use OPTIONS first (safe), fall back to GET
                try:
                    resp = client.options(url)
                    actual_method = "OPTIONS"
                except Exception:
                    resp = client.get(url)
                    actual_method = "GET"

                latency_ms = int((time.time() - t0) * 1000)
                # 404 = route not found (fail), 405 = method not allowed but route exists (pass)
                # 401/403 = route exists but auth rejected (pass — endpoint is real)
                ok = resp.status_code not in (404, 502, 503, 504)
                status = "pass" if ok else "fail"
                if ok:
                    passed += 1
                else:
                    failed += 1

                results.append({
                    "path": path,
                    "method": method,
                    "probeMethod": actual_method,
                    "status": status,
                    "httpStatus": resp.status_code,
                    "latencyMs": latency_ms,
                })
            except Exception as exc:
                failed += 1
                results.append({
                    "path": path,
                    "method": method,
                    "status": "fail",
                    "error": str(exc),
                })

    manifest = dict(manifest)
    manifest["verification"] = {
        "baseUrl": base_url,
        "total":   len(results),
        "passed":  passed,
        "failed":  failed,
        "skipped": skipped,
        "results": results,
    }
    return manifest


def _fill_path_params(path: str, params: list[str]) -> str:
    """Replace {param} placeholders with test values so the URL is valid."""
    import re
    result = path
    for param in params:
        # Use plausible test values by param name heuristic
        value = _guess_param_value(param)
        result = re.sub(r'\{' + re.escape(param) + r'\??}', str(value), result)
    return result


def _guess_param_value(param: str) -> str | int:
    lower = param.lower()
    if lower in ("id", "pk", "product_id", "user_id", "order_id"):
        return 1
    if lower in ("slug", "username", "name"):
        return "test"
    if lower in ("uuid",):
        return "00000000-0000-0000-0000-000000000001"
    return 1
