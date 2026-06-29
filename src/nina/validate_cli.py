"""CLI: nina-validate agent.json --executable [--probe]"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nina.contract_validate import validate_executable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nina-validate",
        description="Validate agent.json for schema and executability",
    )
    parser.add_argument("agent_path", type=Path, help="Path to agent.json")
    parser.add_argument(
        "--executable",
        action="store_true",
        help="Enforce API-first / DOM executability rules",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Probe server API endpoints (HEAD)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=True,
        help="Treat DOM selector gaps as errors (default: true)",
    )
    parser.add_argument("--json", action="store_true", help="Print result as JSON")
    args = parser.parse_args(argv)

    path = args.agent_path
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1

    with path.open(encoding="utf-8") as f:
        contract = json.load(f)

    if args.executable:
        ok, errors, warnings = validate_executable(
            contract,
            strict=args.strict,
            probe=args.probe,
        )
    else:
        from nina.contract import validate_agent
        from nina.generator.stages.validate import validate_contract

        errors = list(validate_agent(contract))
        ok_cross, cross = validate_contract(contract)
        errors.extend(cross)
        ok = not errors and ok_cross
        warnings = []

    if args.json:
        print(json.dumps({"ok": ok, "errors": errors, "warnings": warnings}, indent=2))
    else:
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        if ok:
            print(f"OK: {path}", file=sys.stderr)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
