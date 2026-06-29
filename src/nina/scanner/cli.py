"""nina-scan CLI — scan merchant API source code and generate a NINA manifest.

Usage:
    nina-scan [project_dir] [options]

The manifest contains only route structure (paths, methods, auth flags, roles).
No source code is included or uploaded. The merchant can inspect the manifest
before submitting it to the NINA console.

Examples:
    nina-scan .                              # auto-detect, write nina-manifest.json
    nina-scan ./my-api --framework fastapi  # force FastAPI scanner
    nina-scan . --verify --base-url http://localhost:8000   # live-probe each route
    nina-scan . --output ./infra/nina-manifest.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .detector import detect_framework
from .manifest import build_manifest, sign_manifest
from .verifier import verify_manifest


def _get_scanner(framework: str):
    if framework == "fastapi":
        from .scanners.fastapi_scanner import FastAPIScanner
        return FastAPIScanner()
    if framework == "express":
        from .scanners.express_scanner import ExpressScanner
        return ExpressScanner()
    if framework == "django":
        from .scanners.django_scanner import DjangoScanner
        return DjangoScanner()
    if framework == "flask":
        from .scanners.flask_scanner import FlaskScanner
        return FlaskScanner()
    if framework == "laravel":
        from .scanners.laravel_scanner import LaravelScanner
        return LaravelScanner()
    if framework == "rails":
        from .scanners.rails_scanner import RailsScanner
        return RailsScanner()
    if framework == "nestjs":
        from .scanners.nestjs_scanner import NestJSScanner
        return NestJSScanner()
    print(f"[nina-scan] Unknown framework: {framework}", file=sys.stderr)
    sys.exit(1)


def _print_preflight(manifest: dict) -> bool:
    """Print a go/no-go preflight report. Returns True if ok to proceed."""
    print("\n── NINA Pre-flight Report ─────────────────────────────────")
    s = manifest["summary"]
    print(f"  Framework:       {manifest['framework']}")
    print(f"  Total routes:    {s['totalRoutes']}")
    print(f"  Customer routes: {s['customerRoutes']}")
    print(f"  Admin routes:    {s['adminRoutes']}  ← NINA will NOT expose these")
    print(f"  Public routes:   {s['publicRoutes']}")
    print(f"  Auth-required:   {s['authRequired']}")
    print(f"  Checksum:        {manifest['checksum']}")

    v = manifest.get("verification")
    if v:
        ok = v["passed"] >= v["total"] - v["skipped"]
        print(f"\n  Live verification: {v['passed']} passed / {v['failed']} failed / {v['skipped']} skipped")
        if v["failed"]:
            print("  ⚠  Failed routes:")
            for r in v["results"]:
                if r["status"] == "fail":
                    print(f"     {r['method']} {r['path']}  → {r.get('httpStatus', r.get('error', '?'))}")
        status = "GO ✓" if not v["failed"] else "NO-GO ✗ (some routes unreachable)"
    else:
        status = "GO ✓ (no live verification)"

    print(f"\n  Status: {status}")
    print("───────────────────────────────────────────────────────────\n")
    return "NO-GO" not in status


def main(argv: list[str] | None = None) -> int:
    # Windows terminals default to cp1252 which can't print box-drawing chars.
    # Reconfigure stdout/stderr to UTF-8 if possible (Python 3.7+).
    import io
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        prog="nina-scan",
        description="Scan API source code and generate a NINA action manifest.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "project_dir", nargs="?", default=".",
        help="Project root directory (default: current directory)",
    )
    parser.add_argument(
        "--framework", default="auto",
        choices=["auto", "fastapi", "express", "nestjs", "django", "flask", "laravel", "rails"],
        help="Web framework (default: auto-detect)",
    )
    parser.add_argument(
        "--output", default="nina-manifest.json",
        help="Output file path (default: nina-manifest.json)",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="Probe each endpoint live before writing (requires --base-url)",
    )
    parser.add_argument(
        "--base-url", default="",
        help="Server base URL for live verification (e.g. http://localhost:8000)",
    )
    parser.add_argument(
        "--bearer-token", default="",
        help="Bearer token to use when probing auth-required routes",
    )
    parser.add_argument(
        "--no-preflight", action="store_true",
        help="Skip the pre-flight report printout",
    )
    args = parser.parse_args(argv)

    project = Path(args.project_dir).resolve()
    if not project.exists():
        print(f"[nina-scan] Error: {project} does not exist.", file=sys.stderr)
        return 1

    # ── Detect framework ──────────────────────────────────────────────────────
    framework = args.framework
    if framework == "auto":
        framework = detect_framework(project)
        if not framework:
            print(
                "[nina-scan] Could not auto-detect framework.\n"
                "  Hint: use --framework fastapi|express|nestjs|django|flask|laravel|rails",
                file=sys.stderr,
            )
            return 1
        print(f"[nina-scan] Detected framework: {framework}")

    # ── Scan routes ───────────────────────────────────────────────────────────
    scanner = _get_scanner(framework)
    print(f"[nina-scan] Scanning {project} ...")
    routes = scanner.scan(project)

    if not routes:
        print("[nina-scan] Warning: no routes found. Check the project directory and framework.", file=sys.stderr)

    print(f"[nina-scan] Found {len(routes)} routes  "
          f"({sum(1 for r in routes if r.role == 'customer')} customer / "
          f"{sum(1 for r in routes if r.role == 'admin')} admin / "
          f"{sum(1 for r in routes if r.role == 'superadmin')} superadmin)")

    # ── Build + sign manifest ─────────────────────────────────────────────────
    manifest = build_manifest(project, framework, routes)
    manifest = sign_manifest(manifest)

    # ── Optional live verification ────────────────────────────────────────────
    if args.verify:
        if not args.base_url:
            print("[nina-scan] --verify requires --base-url", file=sys.stderr)
            return 1
        print(f"[nina-scan] Verifying endpoints against {args.base_url} ...")
        manifest = verify_manifest(
            manifest,
            args.base_url,
            bearer_token=args.bearer_token,
        )

    # ── Pre-flight report ─────────────────────────────────────────────────────
    if not args.no_preflight:
        _print_preflight(manifest)

    # ── Write output ──────────────────────────────────────────────────────────
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[nina-scan] Manifest written → {output}")
    print(f"[nina-scan] Checksum: {manifest['checksum']}")
    print()
    print("  Next step: upload this manifest to the NINA Console:")
    print("    PUT /v1/sites/{site_id}/contract")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
