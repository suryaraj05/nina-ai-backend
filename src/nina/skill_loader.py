"""Load NINA skills — markdown playbooks attached to one or more actions.

A skill gives the LLM richer procedural guidance for a tricky action (e.g.
how to map a search result onto add_to_cart's variantId, or the
confirm/risk flow for checkout) than a one-line action description can
carry. Skills are injected directly into the existing single-call
resolution prompt next to the action they apply to — there is no separate
"load this skill" round trip and no new LLM call, so this can't introduce
a new way for a turn to fail or stall mid-conversation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "skills"

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


def _parse_skill_file(path: Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    applies_to = meta.get("appliesTo") or []
    if isinstance(applies_to, str):
        applies_to = [applies_to]
    fast_path = meta.get("fastPath") or []
    if isinstance(fast_path, str):
        fast_path = [fast_path]
    parsed: dict[str, Any] = {
        "name": meta.get("name", path.stem),
        "description": meta.get("description", ""),
        "appliesTo": [a for a in applies_to if isinstance(a, str)],
        "fastPath": [p for p in fast_path if isinstance(p, str)],
        "body": match.group(2).strip(),
    }
    for key in ("clarificationFlow", "searchUX"):
        val = meta.get(key)
        if isinstance(val, dict):
            parsed[key] = val
    for key in ("composeGuidance", "clarifyGuidance"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            parsed[key] = val.strip()
    return parsed


def load_skills(*dirs: Path | str | None) -> list[dict[str, Any]]:
    """Load .md skill files from one or more directories.

    Later directories override earlier ones when a skill `name` collides,
    so a site-specific skills dir can override a built-in skill by name.
    """
    skills: dict[str, dict[str, Any]] = {}
    for d in dirs:
        if not d:
            continue
        directory = Path(d)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            parsed = _parse_skill_file(path)
            if parsed:
                skills[parsed["name"]] = parsed
    return list(skills.values())


def skills_by_action(skills: list[dict[str, Any]]) -> dict[str, str]:
    """Map action_id -> concatenated skill body text (an action may have >1 skill)."""
    grouped: dict[str, list[str]] = {}
    for skill in skills:
        body = skill.get("body") or ""
        if not body.strip():
            continue
        for action_id in skill["appliesTo"]:
            grouped.setdefault(action_id, []).append(body)
    return {action_id: "\n\n".join(bodies) for action_id, bodies in grouped.items()}


def apply_skills_to_core(core: Any, skills_dir: Path | str | None = None) -> list[dict[str, Any]]:
    """Load built-in skill templates onto a Nina _Core (SDK / tests).

    Multi-tenant console uses :func:`skill_synth.apply_skills_for_contract`
    instead — skills are synthesized from the agent contract automatically.
    """
    from .fast_path import compile_fast_path_patterns

    cache_key = f"builtin:{skills_dir or ''}"
    skills = load_skills(BUILTIN_SKILLS_DIR, skills_dir)
    core.skills = skills
    core.skills_by_action = skills_by_action(skills)
    core.fast_path_patterns = compile_fast_path_patterns(skills)
    core._skills_cache_key = cache_key
    return skills
