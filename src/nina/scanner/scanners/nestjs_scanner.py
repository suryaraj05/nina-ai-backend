"""NestJS route scanner — extracts routes from @Controller / @Get / @Post decorators."""
from __future__ import annotations

import re
from pathlib import Path

from . import BaseScanner, Route

# @Controller('prefix') or @Controller({ path: 'prefix' })
_CONTROLLER_RE = re.compile(
    r'@Controller\s*\(\s*(?:["\']([^"\']*)["\']|\{\s*path\s*:\s*["\']([^"\']*)["\'])',
    re.IGNORECASE,
)
# @Get('/path'), @Post('/path'), @Put, @Patch, @Delete, @Head, @Options
_METHOD_RE = re.compile(
    r'@(Get|Post|Put|Patch|Delete|Head|Options)\s*\(\s*(?:["\`\']([^"\'`\)]*)["\`\']|\)',
    re.IGNORECASE,
)
# @UseGuards(...) — any guard = auth required
_GUARD_RE = re.compile(r'@UseGuards\s*\(', re.IGNORECASE)
# @Roles('admin') or @Roles(Role.ADMIN)
_ROLES_RE = re.compile(r'@Roles\s*\([^)]*\b(admin|superadmin|super_admin)\b', re.IGNORECASE)
# Colon-style :param (NestJS supports both :param and {param})
_COLON_PARAM_RE = re.compile(r':(\w+)')
_CURLY_PARAM_RE = re.compile(r'\{(\w+)\}')

_SKIP_DIRS = {"node_modules", "dist", ".next", "build", "coverage", ".git", "test", "spec", "__tests__"}


class NestJSScanner(BaseScanner):
    def scan(self, project: Path) -> list[Route]:
        routes: list[Route] = []
        ts_files = [
            f for f in project.rglob("*.ts")
            if not any(part in _SKIP_DIRS for part in f.parts)
            and not f.name.endswith((".spec.ts", ".test.ts", ".d.ts"))
        ]
        for filepath in ts_files:
            try:
                text = filepath.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "@Controller" not in text:
                continue
            routes.extend(self._scan_file(filepath, text, project))
        return routes

    def _scan_file(self, filepath: Path, text: str, project: Path) -> list[Route]:
        routes: list[Route] = []
        rel = str(filepath.relative_to(project)).replace("\\", "/")
        lines = text.splitlines()

        # Find controller prefix (may be empty)
        ctrl_prefix = ""
        cm = _CONTROLLER_RE.search(text)
        if cm:
            ctrl_prefix = (cm.group(1) or cm.group(2) or "").strip("/")

        # Scan line-by-line for HTTP method decorators
        for i, line in enumerate(lines):
            mm = _METHOD_RE.search(line)
            if not mm:
                continue

            http_method = mm.group(1).upper()
            route_suffix = (mm.group(2) or "").strip("/")

            # Build full path
            if ctrl_prefix and route_suffix:
                full_path = "/" + ctrl_prefix + "/" + route_suffix
            elif ctrl_prefix:
                full_path = "/" + ctrl_prefix
            elif route_suffix:
                full_path = "/" + route_suffix
            else:
                full_path = "/"

            # Normalise path params to {param} style
            full_path = _COLON_PARAM_RE.sub(r'{\1}', full_path)
            path_params = _CURLY_PARAM_RE.findall(full_path)

            # Check surrounding context for guards / roles
            ctx_start = max(0, i - 3)
            ctx_end   = min(len(lines), i + 8)
            context   = "\n".join(lines[ctx_start:ctx_end])

            auth_required = bool(_GUARD_RE.search(context))
            role_match    = _ROLES_RE.search(context)
            if role_match:
                raw_role = role_match.group(1).lower().replace("_", "")
                role = "superadmin" if "super" in raw_role else "admin"
            else:
                role = self._infer_role(full_path, middlewares=[context])

            if auth_required and role == "customer":
                role = self._infer_role(full_path)

            # Try to grab handler name from the next non-decorator line
            handler_name = ""
            for j in range(i + 1, min(i + 5, len(lines))):
                stripped = lines[j].strip()
                if stripped and not stripped.startswith("@"):
                    m2 = re.search(r'(?:async\s+)?(\w+)\s*\(', stripped)
                    if m2:
                        handler_name = m2.group(1)
                    break

            routes.append(Route(
                path=full_path,
                method=http_method,
                auth_required=auth_required,
                role=role,
                path_params=path_params,
                handler_name=handler_name,
                source_file=rel,
                source_line=i + 1,
                tags=["nestjs"],
            ))
        return routes
