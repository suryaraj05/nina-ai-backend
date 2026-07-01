"""NINA — conversational action layer for any system."""
from __future__ import annotations

import sys
import uuid

from . import init as init_module
from .chat import run_turn
from .errors import fail, ok
from .init import (
    VERSION,
    build_llm_client,
    merged_section,
    validate_config,
)
from .registry import Registry, validate_action
from .connector import NinaConnector
from .session import MemoryStore, SessionAPI, SessionManager
from .skill_loader import apply_skills_to_core


def _capture_exception(exc: BaseException) -> None:
    """Report an exception to Sentry if the SDK is initialized; no-op otherwise."""
    sentry = sys.modules.get("sentry_sdk")
    if sentry is not None:
        try:
            sentry.capture_exception(exc)
        except Exception:
            pass


class _Core:
    def __init__(self):
        self.initialized = False
        self.instance_id: str | None = None
        self.debug = False
        self.config: dict | None = None
        self.llm = None
        self.registry = Registry()
        self.sessions: SessionManager | None = None
        self.identity: dict = {}
        self.behavior: dict = {}
        self.hooks: dict = {}
        self.session_store_kind = "memory"
        self.skills: list[dict] = []
        self.skills_by_action: dict[str, str] = {}
        self.fast_path_patterns: list[dict] = []
        self._skills_cache_key: str | None = None


class Nina:
    """Public SDK surface: init, register, chat, session."""

    def __init__(self):
        self._core = _Core()
        self.session = SessionAPI(self._core)

    async def init(self, config: dict) -> dict:
        if self._core.initialized:
            return fail(
                "NINA_ALREADY_INITIALIZED",
                "NINA instance is already initialized.",
            )

        bad = validate_config(config)
        if bad:
            return fail(
                "NINA_CONFIG_INVALID",
                "Configuration failed structural validation.",
                {"paths": bad},
            )

        llm_cfg = config["llm"]
        if llm_cfg.get("provider") == "custom" and not callable(
            llm_cfg.get("adapter")
        ):
            return fail(
                "NINA_ADAPTER_INVALID",
                'provider "custom" requires a callable adapter.',
            )

        sess_cfg = merged_section(config, "session")
        store = sess_cfg.get("store", "memory")
        is_custom = store != "memory"
        if is_custom:
            for method in ("get", "set", "delete"):
                if not callable(getattr(store, method, None)):
                    return fail(
                        "NINA_STORE_INVALID",
                        "StoreAdapter must implement get, set, and delete.",
                    )
        else:
            store = MemoryStore()

        self._core.config = config
        self._core.debug = bool(config.get("debug", False))
        self._core.identity = merged_section(config, "identity")
        self._core.behavior = merged_section(config, "behavior")
        self._core.hooks = config.get("hooks") or {}
        apply_skills_to_core(self._core, config.get("skillsDir"))
        self._core.llm = build_llm_client(llm_cfg)
        self._core.sessions = SessionManager(
            store,
            int(sess_cfg.get("ttlSeconds", 1800)),
            int(sess_cfg.get("maxTurns", 20)),
            is_custom,
        )
        self._core.session_store_kind = "custom" if is_custom else "memory"

        # Ping removed: it wastes one quota slot on every cold start which causes
        # the immediately-following real query to be rate-limited, especially on
        # free-tier providers (Gemini 15 RPM). Key validity surfaces on the first
        # real query instead — a cleaner and cheaper failure mode.

        self._core.instance_id = str(uuid.uuid4())
        self._core.initialized = True
        return ok(
            {
                "instanceId": self._core.instance_id,
                "llmReady": True,
                "sessionStore": self._core.session_store_kind,
                "version": VERSION,
            }
        )

    async def register(self, actions):
        if not self._core.initialized:
            return fail("NINA_NOT_INITIALIZED", "Call nina.init() first.")

        if isinstance(actions, dict):
            actions = [actions]

        registered: list[str] = []
        failed: list[dict] = []
        warnings_out: list[dict] = []

        for action in actions:
            name = (action or {}).get("name", "<unknown>")
            error, warnings = self._core.registry.validate_and_add(
                action, initialized=True
            )
            if error:
                failed.append({"name": name, "error": error})
                continue
            registered.append(name)
            for w in warnings:
                warnings_out.append({"name": name, "warning": w})

        if len(actions) == 1 and not failed:
            payload = {
                "name": registered[0],
                "registered": True,
                "actionCount": len(self._core.registry.all()),
            }
        elif len(actions) == 1 and failed:
            return fail(
                failed[0]["error"]["code"],
                failed[0]["error"]["message"],
                failed[0]["error"].get("details"),
            )
        else:
            payload = {"registered": registered, "failed": failed}

        result = ok(payload)
        if warnings_out:
            result["warnings"] = warnings_out
        return result

    async def chat(
        self,
        user_message: str,
        session_id: str,
        *,
        replay_queued: bool = False,
        resume_plan: bool = False,
        confirmed: bool = False,
    ) -> dict:
        try:
            text = user_message
            if replay_queued and not (text or "").strip():
                text = "(replay queued action)"
            if resume_plan and not (text or "").strip():
                text = "(resuming plan)"
            if confirmed and not (text or "").strip():
                text = "yes"
            return await run_turn(
                self._core,
                text,
                session_id,
                replay_queued=replay_queued,
                resume_plan=resume_plan,
                confirmed=confirmed,
            )
        except Exception as exc:
            # Surface internal failures to error tracking before converting them
            # to the public envelope — otherwise Sentry only ever sees a 200 with
            # {"ok": false} and never alerts on real engine crashes.
            _capture_exception(exc)
            return fail(
                "NINA_INTERNAL",
                "An unexpected internal error occurred during chat.",
            )

    async def aclose(self) -> None:
        """Release resources (LLM HTTP client). Call when discarding an instance."""
        llm = getattr(self._core, "llm", None)
        if llm is not None:
            aclose = getattr(llm, "aclose", None)
            if aclose is not None:
                await aclose()


__all__ = [
    "Nina",
    "NinaConnector",
    "validate_action",
    "VERSION",
    "init_module",
]
