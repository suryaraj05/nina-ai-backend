"""Auto-detect the web framework used in a project directory."""
from __future__ import annotations

from pathlib import Path


def detect_framework(project: Path) -> str | None:
    """Return the framework name or None if detection fails."""

    # --- Python frameworks ---
    py_files = set(project.rglob("*.py"))
    src_text = _sample_text(py_files, limit=20)

    if _any_file_exists(project, ["requirements.txt", "pyproject.toml", "setup.py"]):
        if "fastapi" in src_text.lower() or _dep_present(project, "fastapi"):
            return "fastapi"
        if "flask" in src_text.lower() or _dep_present(project, "flask"):
            return "flask"
        if "django" in src_text.lower() or _dep_present(project, "django") or (project / "manage.py").exists():
            return "django"

    # --- JavaScript/TypeScript (NestJS / Express / Fastify) ---
    pkg = project / "package.json"
    if pkg.exists():
        pkg_text = pkg.read_text(encoding="utf-8", errors="ignore").lower()
        if "@nestjs/core" in pkg_text or "@nestjs/common" in pkg_text:
            return "nestjs"
        if "express" in pkg_text:
            return "express"
        if "fastify" in pkg_text:
            return "express"  # compatible scanning approach

    # --- PHP (Laravel) ---
    if (project / "artisan").exists() or (project / "composer.json").exists():
        return "laravel"

    # --- Ruby (Rails) ---
    if (project / "config" / "routes.rb").exists() or (project / "Gemfile").exists():
        gemfile = (project / "Gemfile").read_text(encoding="utf-8", errors="ignore").lower() if (project / "Gemfile").exists() else ""
        if "rails" in gemfile:
            return "rails"

    return None


def _sample_text(files: set, limit: int = 20) -> str:
    """Read a sample of files and return combined text for keyword detection."""
    chunks: list[str] = []
    for i, f in enumerate(files):
        if i >= limit:
            break
        try:
            chunks.append(f.read_text(encoding="utf-8", errors="ignore")[:2000])
        except OSError:
            pass
    return "\n".join(chunks)


def _any_file_exists(project: Path, names: list[str]) -> bool:
    return any((project / n).exists() for n in names)


def _dep_present(project: Path, dep: str) -> bool:
    """Check if a dependency appears in common dependency files."""
    for fname in ["requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile"]:
        f = project / fname
        if f.exists():
            try:
                if dep in f.read_text(encoding="utf-8", errors="ignore").lower():
                    return True
            except OSError:
                pass
    return False
