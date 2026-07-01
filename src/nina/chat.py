"""Core chat turn orchestration (spec §3)."""
from __future__ import annotations

import json
import time
import uuid

from jsonschema import Draft202012Validator

from .action_input_coalesce import coalesce_action_input
from .catalog_rail import validate_catalog_mutation
from .errors import LLMError, StoreError, fail, now_iso, ok
from .executor import execute_action
from .auth_queue import pop_replay_if_ready, save_queued_intent
from .cart_flow import begin_cart_add, continue_cart_add
from .critic import check_action_alignment
from .fast_path import try_catalog_search_fast_path, try_fast_path, try_reference_cart_fast_path
from .guardrails import (
    blocked_turn_payload,
    detect_injection_semantic,
    run_post_parse_checks,
    run_pre_llm_checks,
    scrub_pii,
)
from .intent import (
    build_system_prompt,
    generate_clarification,
    normalize_resolution,
)
from .planner import (
    on_step_complete,
    pending_auto_action,
    plan_status,
    pop_plan_resume_if_ready,
)
from .reasoner import maybe_reason
from .responder import compose_chitchat, compose_response
from .session import (
    products_from_result,
    resolve_product_reference,
    resolve_reference_parameters,
    seed_reference_map_from_client,
    update_reference_map,
)


def _shape(value):
    if value is None:
        return "none"
    if isinstance(value, dict):
        return f"dict({len(value)} keys)"
    if isinstance(value, list):
        return f"list({len(value)} items)"
    return type(value).__name__


def _debug_print(turn, state, user_message):
    rm = state.get("referenceMap") or {}
    refs = ", ".join(k for k, v in rm.items() if v) or "none"
    pending = state.get("pending")
    pending_s = f'{pending["type"]} for {pending["action"]}' if pending else "none"
    reasoner = "ran" if turn["reasoningUsed"] else "skipped"
    if turn.get("reasoningSummary"):
        reasoner += f" — {turn['reasoningSummary']}"
    nlr = turn["naturalLanguageResponse"]
    response = nlr[:120] + ("..." if len(nlr) > 120 else "")
    action_input = (
        json.dumps(turn["actionInput"], indent=2, ensure_ascii=True)
        if turn["actionInput"]
        else "-"
    )
    block = f"""=== NINA DEBUG - turn {turn['turnId']} ===
transcript   : {user_message}
session      : {turn['sessionId']}
reasoner     : {reasoner}
intent       : {turn['intent']}  (confidence {turn['confidence']:.2f})
action       : {turn['actionCalled'] or 'none'}
references   : {refs}
pending      : {pending_s}
input passed : {action_input}
result shape : {_shape(turn['actionResult'])}
response     : {response}
latency      : {turn['usage'].get('latencyMs', 0)}ms
========================================"""
    try:
        print(block)
    except UnicodeEncodeError:
        print(block.encode("ascii", errors="replace").decode("ascii"))


def _validate_inputs(user_message, session_id):
    if not isinstance(user_message, str):
        return fail("NINA_MESSAGE_INVALID", "message must be a string."), None
    msg = user_message.strip()
    if not msg or len(msg) > 8000:
        return (
            fail(
                "NINA_MESSAGE_INVALID",
                "message must be 1-8000 characters after trim.",
            ),
            None,
        )
    if not isinstance(session_id, str) or not session_id:
        return (
            fail(
                "NINA_SESSION_ID_INVALID",
                "sessionId must be a non-empty string.",
            ),
            None,
        )
    return None, msg


def _intent_label(resolution: str, action_name: str | None = None) -> str:
    return {
        "clarify": "clarification",
        "confirm": "confirmation",
        "chitchat": "chitchat",
        "unsupported": "unsupported",
        "action": action_name or "action",
    }.get(resolution, resolution or "unsupported")


def _validate_input(schema: dict, data: dict) -> list[str]:
    validator = Draft202012Validator(schema)
    return [
        f"{'.'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
        for err in validator.iter_errors(data or {})
    ]


def _missing_field_question(action_name: str, schema_errors: list[str]) -> str:
    joined = " ".join(schema_errors)
    if action_name in ("search", "search_products") and "query" in joined:
        return "What would you like me to search for?"
    if action_name == "open_category" and "categorySlug" in joined:
        return "Which category would you like to browse?"
    return (
        f"I need a bit more detail to run {action_name.replace('_', ' ')}: "
        + schema_errors[0]
    )


def _merge_usage(*parts) -> dict:
    usage = {"promptTokens": 0, "completionTokens": 0, "latencyMs": 0}
    for part in parts:
        if not part:
            continue
        usage["promptTokens"] += part.get("promptTokens") or 0
        usage["completionTokens"] += part.get("completionTokens") or 0
    return usage


async def _finish_cart_flow_turn(
    core,
    state,
    session_id,
    user_message,
    started,
    flow: dict,
    *,
    reasoning_used: bool = False,
    reasoning_summary=None,
    usage_parts=None,
) -> dict:
    turn = await _build_turn(
        core,
        state,
        session_id,
        user_message,
        started,
        intent=flow.get("intent") or "cart_guidance",
        confidence=1.0,
        natural_language_response=flow.get("reply") or "",
        action_called=flow.get("actionCalled"),
        action_input=flow.get("actionInput"),
        action_result=flow.get("actionResult"),
        reasoning_used=reasoning_used,
        reasoning_summary=reasoning_summary,
        usage_parts=usage_parts or [],
    )
    chips = flow.get("chips")
    if chips:
        turn["suggestionChips"] = list(chips)
    instructions = flow.get("instructions")
    if instructions:
        turn["instructions"] = list(instructions)
    if core.debug:
        _debug_print(turn, state, user_message)
    return ok(turn)


def _action_summary(result) -> str:
    if result is None:
        return ""
    if isinstance(result, dict):
        items = result.get("results")
        if items is None:
            items = result.get("data")
        if items:
            names = [
                (r.get("name") or r.get("title") or r.get("id"))
                for r in (items or [])[:3]
                if isinstance(r, dict)
            ]
            return ", ".join(str(n) for n in names if n) or json.dumps(
                result, ensure_ascii=False, default=str
            )[:200]
        if "cart" in result:
            n = len((result.get("cart") or {}).get("items") or [])
            return f"cart with {n} item(s)"
    return json.dumps(result, ensure_ascii=False, default=str)[:200]


async def _record_turn(state, turn_id, user_message, turn):
    safe_content = scrub_pii(user_message)
    state["history"].append(
        {
            "turnId": turn_id,
            "role": "user",
            "content": safe_content,
            "intent": None,
            "actionCalled": None,
            "timestamp": now_iso(),
        }
    )
    state["history"].append(
        {
            "turnId": turn_id,
            "role": "nina",
            "content": turn["naturalLanguageResponse"],
            "intent": turn["intent"],
            "actionCalled": turn["actionCalled"],
            "timestamp": now_iso(),
            "actionSummary": _action_summary(turn["actionResult"])
            if turn["actionCalled"]
            else None,
        }
    )
    state["turnCount"] += 1


async def _build_turn(
    core,
    state,
    session_id,
    user_message,
    started,
    *,
    intent,
    confidence,
    natural_language_response,
    action_called=None,
    action_input=None,
    action_result=None,
    action_error=None,
    clarification_needed=None,
    products=None,
    reasoning_used=False,
    reasoning_summary=None,
    usage_parts=(),
):
    latency_ms = int((time.perf_counter() - started) * 1000)
    usage = _merge_usage(*usage_parts)
    usage["latencyMs"] = latency_ms
    turn_id = str(uuid.uuid4())
    turn = {
        "turnId": turn_id,
        "sessionId": session_id,
        "intent": intent,
        "actionCalled": action_called,
        "actionInput": action_input,
        "actionResult": action_result,
        "actionError": action_error,
        "naturalLanguageResponse": natural_language_response,
        "confidence": confidence,
        "clarificationNeeded": clarification_needed,
        "reasoningUsed": reasoning_used,
        "reasoningSummary": reasoning_summary,
        "usage": usage,
    }
    if products:
        turn["products"] = products
    if isinstance(action_result, dict) and action_result.get("suggestionChips"):
        turn["suggestionChips"] = list(action_result["suggestionChips"])
    await _record_turn(state, turn_id, user_message, turn)
    await core.sessions.save(state)
    return turn


async def _build_guardrail_turn(
    core,
    state,
    session_id,
    user_message,
    started,
    guard_payload,
    usage_parts=(),
):
    turn_id = str(uuid.uuid4())
    latency_ms = int((time.perf_counter() - started) * 1000)
    usage = _merge_usage(*usage_parts)
    usage["latencyMs"] = latency_ms
    base = blocked_turn_payload(guard_payload)
    turn = {
        "turnId": turn_id,
        "sessionId": session_id,
        "intent": base["intent"],
        "actionCalled": None,
        "actionInput": None,
        "actionResult": None,
        "actionError": None,
        "naturalLanguageResponse": base["naturalLanguageResponse"],
        "confidence": base["confidence"],
        "clarificationNeeded": None,
        "reasoningUsed": False,
        "reasoningSummary": None,
        "usage": usage,
        "guardrail": base.get("guardrail"),
        "instructions": base.get("instructions") or [],
    }
    await _record_turn(state, turn_id, user_message, turn)
    await core.sessions.save(state)
    if core.debug:
        _debug_print(turn, state, user_message)
    return turn


async def _execute_action_turn(
    core,
    state,
    session_id,
    user_message,
    started,
    action_name,
    action_input,
    confidence,
    *,
    actions,
    enrichment=None,
    reasoning_used=False,
    reasoning_summary=None,
    usage_parts=None,
):
    usage_parts = list(usage_parts or [])
    action_def = core.registry.get(action_name)
    if not action_def:
        turn = await _build_turn(
            core,
            state,
            session_id,
            user_message,
            started,
            intent="clarification",
            confidence=confidence,
            natural_language_response=(
                f"I don't have an action called '{action_name}'. "
                "What would you like to do?"
            ),
            reasoning_used=reasoning_used,
            reasoning_summary=reasoning_summary,
            usage_parts=usage_parts,
        )
        if core.debug:
            _debug_print(turn, state, user_message)
        return ok(turn)

    action_input = coalesce_action_input(
        action_name,
        action_input,
        user_message,
        action_def.get("inputSchema"),
    )
    schema_errors = _validate_input(action_def["inputSchema"], action_input)
    if schema_errors:
        question = _missing_field_question(action_name, schema_errors)
        state["pending"] = {
            "type": "clarification",
            "action": action_name,
            "collectedInput": action_input,
            "missingFields": schema_errors,
            "attemptsUsed": 1,
            "clarificationStrategy": "missing_field",
        }
        turn = await _build_turn(
            core,
            state,
            session_id,
            user_message,
            started,
            intent="clarification",
            confidence=confidence,
            natural_language_response=question,
            clarification_needed={
                "missingFields": schema_errors,
                "question": question,
                "pendingAction": action_name,
            },
            reasoning_used=reasoning_used,
            reasoning_summary=reasoning_summary,
            usage_parts=usage_parts,
        )
        if core.debug:
            _debug_print(turn, state, user_message)
        return ok(turn)

    state["pending"] = None
    catalog_rows = (core.config or {}).get("_productCatalog") or []
    action_input = resolve_product_reference(
        state,
        action_name,
        action_input,
        user_message,
        catalog_rows=catalog_rows,
    )
    action_input = resolve_reference_parameters(state, action_input)
    hooks = core.hooks or {}
    if hooks.get("onActionCall"):
        try:
            hooks["onActionCall"](action_name, action_input, session_id)
        except Exception:
            pass

    ok_mut, mut_reason = validate_catalog_mutation(action_name, action_input, catalog_rows)
    if not ok_mut:
        rm = state.get("referenceMap") or {}
        had_results = bool(rm.get("lastSearchResults"))
        turn = await _build_turn(
            core,
            state,
            session_id,
            user_message,
            started,
            intent="clarification",
            confidence=confidence,
            natural_language_response=(
                "I can see that product in the catalog — tap Add to Cart on its card, "
                "or say \"the second one\" / \"add the gray hoodie\"."
                if had_results
                else (
                    "I can't find that in the verified catalog."
                    if "catalog" in mut_reason
                    else mut_reason.replace("_", " ").capitalize() + "."
                )
            ),
            reasoning_used=reasoning_used,
            reasoning_summary=reasoning_summary,
            usage_parts=usage_parts,
        )
        if core.debug:
            _debug_print(turn, state, user_message)
        return ok(turn)

    if action_name in ("add_to_cart", "add_item_to_cart"):
        page_id = (core.config or {}).get("_pageId")
        on_pdp = page_id in ("product_detail", "pdp")
        flow = begin_cart_add(
            state,
            action_input,
            session_hints=(core.config or {}).get("_sessionHints") or {},
            contract=(core.config or {}).get("_agentContract") or {},
            on_pdp=on_pdp,
            skills=getattr(core, "skills", None) or [],
        )
        if flow:
            return await _finish_cart_flow_turn(
                core,
                state,
                session_id,
                user_message,
                started,
                flow,
                reasoning_used=reasoning_used,
                reasoning_summary=reasoning_summary,
                usage_parts=usage_parts,
            )

    context = {
        "sessionId": session_id,
        "userMessage": user_message,
        "locale": core.behavior.get("language", "auto"),
        "sessionData": state.get("data") or {},
        "productCatalog": catalog_rows,
        "agentContract": (core.config or {}).get("_agentContract") or {},
    }
    result, action_error = await execute_action(action_def, action_input, context)

    if hooks.get("onActionResult"):
        try:
            hooks["onActionResult"](action_name, result, session_id)
        except Exception:
            pass
    if action_error and hooks.get("onError"):
        try:
            hooks["onError"](action_error, session_id)
        except Exception:
            pass

    if not action_error:
        update_reference_map(state, action_name, result)
        auth_hint = (core.config or {}).get("_sessionAuthenticated", True)
        plan_next = on_step_complete(state, action_name, authenticated=auth_hint)
        if plan_next and plan_next.get("paused"):
            state["pendingPlan"] = state.get("pendingPlan") or {}
            if plan_next.get("reason") == "confirm_required":
                state["pending"] = {
                    "type": "confirmation",
                    "action": (plan_next.get("next") or {}).get("action"),
                    "collectedInput": (plan_next.get("next") or {}).get("params") or {},
                    "missingFields": [],
                    "attemptsUsed": 0,
                    "fromPlan": True,
                }

    nlr, compose_usage = await compose_response(
        core.llm,
        core.identity,
        core.behavior,
        user_message,
        action_name,
        result,
        action_error,
        skills=getattr(core, "skills", None) or [],
    )
    usage_parts.append(compose_usage)

    # Surface browsable results (search/recommendation) as product cards for the
    # widget. Empty for cart/checkout/auth actions or non-listing results.
    products = []
    if not action_error:
        contract = (core.config or {}).get("_agentContract") or {}
        apis = contract.get("apis") or {}
        base_url = (apis.get("default") or {}).get("baseUrl") or (contract.get("site") or {}).get("baseUrl")
        products = products_from_result(result, action_name, base_url=base_url)

    turn = await _build_turn(
        core,
        state,
        session_id,
        user_message,
        started,
        intent=action_name,
        confidence=confidence,
        natural_language_response=nlr,
        action_called=action_name,
        action_input=action_input,
        action_result=result if not action_error else None,
        action_error=action_error,
        products=products,
        reasoning_used=reasoning_used,
        reasoning_summary=reasoning_summary,
        usage_parts=usage_parts,
    )
    turn["planStatus"] = plan_status(state)
    if core.debug:
        _debug_print(turn, state, user_message)
    return ok(turn)


async def _apply_contract_safety_gate(
    core,
    state,
    session_id,
    msg,
    started,
    action_name,
    action_input,
    confidence,
    *,
    confirmed: bool,
    threshold: float,
    security: dict,
    reasoning_used: bool = False,
    reasoning_summary=None,
    usage_parts=None,
) -> dict | None:
    """Contract-level needs_login / confirm / blocked / critic gate.

    Every action execution path must go through this before reaching
    _execute_action_turn -- not just the primary LLM/fast-path resolution.
    Plan auto-continuation, auth-replay, and plan-resume previously called
    _execute_action_turn directly and skipped this entirely, so a
    contract's risk.confirmActions/blockActions (and the alignment critic)
    could be bypassed for any action reached via those paths. Returns an
    `ok(turn)` envelope if gated, or None if it's safe to proceed.
    """
    contract = (core.config or {}).get("_agentContract")
    if not contract or not action_name:
        return None

    usage_parts = usage_parts if usage_parts is not None else []
    from .contract import get_action as _get_contract_action, resolve_intent

    session_hints = (core.config or {}).get("_sessionHints") or {}
    page_id = (core.config or {}).get("_pageId")
    auth_check = resolve_intent(
        contract,
        intent=action_name,
        params=action_input,
        confidence=confidence,
        page_id=page_id,
        session_hints=session_hints,
        confidence_threshold=threshold,
        confirmed=confirmed,
    )
    instr = auth_check.get("instructions") or []

    if instr and instr[0].get("type") == "needs_login":
        qi = instr[0].get("queuedIntent") or {"intent": action_name, "params": action_input}
        save_queued_intent(state, qi.get("intent", action_name), qi.get("params", action_input))
        turn = await _build_turn(
            core, state, session_id, msg, started,
            intent="needs_login",
            confidence=confidence,
            natural_language_response=auth_check.get("message") or "Please sign in to continue.",
            reasoning_used=reasoning_used,
            reasoning_summary=reasoning_summary,
            usage_parts=usage_parts,
        )
        turn["instructions"] = instr
        await core.sessions.save(state)
        if core.debug:
            _debug_print(turn, state, msg)
        return ok(turn)

    if auth_check.get("error_code") == "BLOCKED":
        turn = await _build_turn(
            core, state, session_id, msg, started,
            intent="blocked",
            confidence=confidence,
            natural_language_response=auth_check.get("message") or "That action is not allowed on this site.",
            reasoning_used=reasoning_used,
            reasoning_summary=reasoning_summary,
            usage_parts=usage_parts,
        )
        turn["instructions"] = instr
        await core.sessions.save(state)
        if core.debug:
            _debug_print(turn, state, msg)
        return ok(turn)

    if instr and instr[0].get("type") == "confirm":
        state["pending"] = {
            "type": "confirmation",
            "action": action_name,
            "collectedInput": action_input,
            "missingFields": [],
            "attemptsUsed": 0,
        }
        turn = await _build_turn(
            core, state, session_id, msg, started,
            intent="confirmation",
            confidence=confidence,
            natural_language_response=instr[0].get("message")
            or f"Should I go ahead with {action_name.replace('_', ' ')}?",
            reasoning_used=reasoning_used,
            reasoning_summary=reasoning_summary,
            usage_parts=usage_parts,
        )
        await core.sessions.save(state)
        if core.debug:
            _debug_print(turn, state, msg)
        return ok(turn)

    contract_action = _get_contract_action(contract, action_name)
    if (
        contract_action
        and contract_action.get("risk") == "high"
        and security.get("enableActionCritic", True)
    ):
        misalignment = await check_action_alignment(
            core.llm, msg, action_name,
            contract_action.get("description", action_name),
            action_input,
        )
        if misalignment:
            turn = await _build_turn(
                core, state, session_id, msg, started,
                intent="blocked",
                confidence=confidence,
                natural_language_response=(
                    "I'm not going to do that — it doesn't look like what you "
                    "asked for. Could you rephrase your request?"
                ),
                reasoning_used=reasoning_used,
                reasoning_summary=reasoning_summary,
                usage_parts=usage_parts,
            )
            turn["guardrail"] = {
                "code": "ACTION_ALIGNMENT_FAILED",
                "message": misalignment["reason"],
            }
            await core.sessions.save(state)
            if core.debug:
                _debug_print(turn, state, msg)
            return ok(turn)

    return None


async def _handle_pending_continuations(
    core, state, session_id, msg, started, *,
    threshold_for_gate, security, replay_queued, resume_plan,
) -> dict | None:
    """Run any pending continuation before normal resolution: an auto-action
    step, an auth-gated replay, or a resumed plan. Returns a turn envelope if
    one fired, else None to fall through to normal resolution."""
    cart_flow = continue_cart_add(state, msg)
    if cart_flow:
        await core.sessions.save(state)
        return await _finish_cart_flow_turn(
            core, state, session_id, msg, started, cart_flow,
        )

    auto_step = pending_auto_action(state)
    if auto_step and auto_step.get("action"):
        gated = await _apply_contract_safety_gate(
            core, state, session_id, msg, started,
            auto_step["action"], auto_step.get("params") or {}, 1.0,
            confirmed=False, threshold=threshold_for_gate, security=security,
        )
        if gated:
            return gated
        return await _execute_action_turn(
            core,
            state,
            session_id,
            msg,
            started,
            auto_step["action"],
            auto_step.get("params") or {},
            1.0,
            actions=core.registry.all(),
        )

    authenticated = bool((core.config or {}).get("_sessionAuthenticated"))
    replay = pop_replay_if_ready(
        state,
        authenticated=authenticated,
        replay_requested=replay_queued,
    )
    if replay:
        await core.sessions.save(state)
        gated = await _apply_contract_safety_gate(
            core, state, session_id, msg or f"(continuing {replay['intent']})", started,
            replay["intent"], replay.get("params") or {}, 1.0,
            confirmed=False, threshold=threshold_for_gate, security=security,
        )
        if gated:
            return gated
        result = await _execute_action_turn(
            core,
            state,
            session_id,
            msg or f"(continuing {replay['intent']})",
            started,
            replay["intent"],
            replay.get("params") or {},
            1.0,
            actions=core.registry.all(),
        )
        if result.get("ok") and result.get("data"):
            data = dict(result["data"])
            data["intent"] = data.get("intent") or "auth_replay"
            data["replayedQueuedIntent"] = True
            data["naturalLanguageResponse"] = (
                "You're signed in — continuing where we left off. "
                + (data.get("naturalLanguageResponse") or "")
            )
            result = {**result, "data": data}
        return result

    plan_step = pop_plan_resume_if_ready(
        state,
        authenticated=authenticated,
        resume_requested=resume_plan,
    )
    if plan_step:
        await core.sessions.save(state)
        gated = await _apply_contract_safety_gate(
            core, state, session_id, msg or f"(continuing plan: {plan_step['action']})", started,
            plan_step["action"], plan_step.get("params") or {}, 1.0,
            confirmed=False, threshold=threshold_for_gate, security=security,
        )
        if gated:
            return gated
        result = await _execute_action_turn(
            core,
            state,
            session_id,
            msg or f"(continuing plan: {plan_step['action']})",
            started,
            plan_step["action"],
            plan_step.get("params") or {},
            1.0,
            actions=core.registry.all(),
        )
        if result.get("ok") and result.get("data"):
            data = dict(result["data"])
            data["resumedPlan"] = True
            data["naturalLanguageResponse"] = (
                "You're signed in — continuing your plan. "
                + (data.get("naturalLanguageResponse") or "")
            )
            result = {**result, "data": data}
        return result

    if resume_plan and not (msg or "").strip():
        turn = await _build_turn(
            core,
            state,
            session_id,
            msg or "(plan resume)",
            started,
            intent="chitchat",
            confidence=1.0,
            natural_language_response=(
                "You're signed in. What would you like to do next?"
            ),
        )
        return ok(turn)
    return None


async def _handle_clarification(
    core, state, session_id, msg, started, *,
    res, resolution, action_name, action_input, confidence, threshold,
    pending, reasoning_used, reasoning_summary, usage_parts,
) -> dict | None:
    """If the resolution is ambiguous (explicit clarify, low confidence, or
    missing required fields), ask a clarifying question — or give up after
    maxClarifications. Returns a turn envelope if handled, else None."""
    missing = res.get("missing_fields") or []
    needs_clarify = (
        resolution == "clarify"
        or confidence < threshold
        or (resolution == "action" and missing)
    )

    if needs_clarify and resolution != "confirm":
        question = res.get("user_reply") or ""
        strategy = "missing_field"
        if not question.strip():
            question, strategy = await generate_clarification(
                core.llm,
                core.identity,
                core.behavior,
                state,
                msg,
                action_name or (pending or {}).get("action") or "unknown",
                (pending or {}).get("collectedInput") or action_input,
                missing,
                confidence,
                question,
                skills=getattr(core, "skills", None) or [],
            )
        attempts = ((pending or {}).get("attemptsUsed") or 0) + 1
        max_clar = core.behavior.get("maxClarifications", 2)
        if attempts > max_clar:
            turn = await _build_turn(
                core,
                state,
                session_id,
                msg,
                started,
                intent="unsupported",
                confidence=confidence,
                natural_language_response=(
                    "I'm still not sure what you need. "
                    "Could you rephrase or be more specific?"
                ),
                reasoning_used=reasoning_used,
                reasoning_summary=reasoning_summary,
                usage_parts=usage_parts,
            )
            state["pending"] = None
            await core.sessions.save(state)
            if core.debug:
                _debug_print(turn, state, msg)
            return ok(turn)

        prior_collected = (pending or {}).get("collectedInput") or {}
        state["pending"] = {
            "type": "clarification",
            "action": action_name or (pending or {}).get("action") or "",
            "collectedInput": {**prior_collected, **action_input},
            "missingFields": missing,
            "attemptsUsed": attempts,
            "clarificationStrategy": strategy,
        }
        clar = {
            "missingFields": missing,
            "question": question,
            "pendingAction": state["pending"]["action"],
        }
        turn = await _build_turn(
            core,
            state,
            session_id,
            msg,
            started,
            intent="clarification",
            confidence=confidence,
            natural_language_response=question,
            clarification_needed=clar,
            reasoning_used=reasoning_used,
            reasoning_summary=reasoning_summary,
            usage_parts=usage_parts,
        )
        if core.debug:
            _debug_print(turn, state, msg)
        return ok(turn)
    return None


async def run_turn(
    core,
    user_message: str,
    session_id: str,
    *,
    replay_queued: bool = False,
    resume_plan: bool = False,
) -> dict:
    """Execute one chat turn → the universal envelope.

    Pipeline — each stage may short-circuit by returning a turn envelope:
      1. validate inputs + load session
      2. pre-LLM guardrails (prompt-injection screens)
      3. pending continuations (auto-action / auth-replay / plan-resume)
      4. resolve the message into a structured intent (fast-path or LLM)
      5. post-parse guardrails + pending-confirmation (yes/no)
      6. clarification (ambiguous / low-confidence / missing fields)
      7. terminal resolutions (chitchat / confirm / unsupported)
      8. validate input, safety-gate, then execute the action

    Stages 3 and 6 live in _handle_pending_continuations / _handle_clarification.
    """
    if not core.initialized:
        return fail("NINA_NOT_INITIALIZED", "Call nina.init() first.")

    if replay_queued and not (user_message or "").strip():
        user_message = "(replay queued action)"
    if resume_plan and not (user_message or "").strip():
        user_message = "(resuming plan)"

    err, msg = _validate_inputs(user_message, session_id)
    if err:
        return err

    started = time.perf_counter()
    try:
        state = await core.sessions.load_or_create(session_id)
    except StoreError as exc:
        return fail(
            "NINA_SESSION_STORE_FAILURE",
            f"Session store operation '{exc.op}' failed.",
            {"reason": exc.reason},
        )

    seed_reference_map_from_client(
        state,
        (core.config or {}).get("_sessionHints"),
    )

    # ── Stage 2: pre-LLM guardrails ──
    security = ((core.config or {}).get("security")) or {}
    threshold_for_gate = core.behavior.get("confidenceThreshold", 0.75)
    pre_block = run_pre_llm_checks(msg, security)
    if pre_block:
        turn = await _build_guardrail_turn(
            core, state, session_id, msg, started, pre_block
        )
        return ok(turn)

    if security.get("enableSemanticInjectionGuard", True):
        semantic_block = await detect_injection_semantic(core.llm, msg)
        if semantic_block:
            turn = await _build_guardrail_turn(
                core, state, session_id, msg, started, semantic_block
            )
            return ok(turn)

    # ── Stage 3: pending continuations ──
    cont = await _handle_pending_continuations(
        core, state, session_id, msg, started,
        threshold_for_gate=threshold_for_gate, security=security,
        replay_queued=replay_queued, resume_plan=resume_plan,
    )
    if cont is not None:
        return cont

    # ── Stage 4: resolve (fast-path or LLM) ──
    actions = core.registry.all()
    if not actions and not core.behavior.get("allowChitchat", True):
        return fail(
            "NINA_NO_ACTIONS_REGISTERED",
            "No actions registered and chitchat is disabled.",
        )

    contract = (core.config or {}).get("_agentContract") or {}
    risk_cfg = contract.get("risk") or {}
    fast_path_excluded = frozenset(
        (risk_cfg.get("confirmActions") or []) + (risk_cfg.get("blockActions") or [])
    )
    catalog_rows = (core.config or {}).get("_productCatalog") or []
    fast_match = try_fast_path(
        msg,
        actions,
        core.fast_path_patterns,
        excluded_actions=fast_path_excluded,
    )
    if not fast_match:
        fast_match = try_reference_cart_fast_path(
            msg,
            state,
            actions,
            excluded_actions=fast_path_excluded,
            catalog_rows=catalog_rows,
        )
    if not fast_match and catalog_rows:
        fast_match = try_catalog_search_fast_path(
            msg, actions, excluded_actions=fast_path_excluded,
        )

    enrichment = None
    reasoning_used = False
    reasoning_summary = None
    usage_parts: list[dict] = []

    if fast_match:
        res = {
            "resolution": "action",
            "action": fast_match["action"],
            "input": fast_match["input"],
            "missing_fields": [],
            "confidence": 1.0,
            "user_reply": "",
        }
    else:
        enrichment = await maybe_reason(
            core.llm,
            core.identity,
            msg,
            actions,
            state,
        )
        reasoning_used = enrichment is not None
        reasoning_summary = enrichment["summary"] if enrichment else None

        correction = None
        system_prompt = build_system_prompt(
            core.identity,
            core.behavior,
            actions,
            state,
            msg,
            core.sessions.max_turns,
            correction=correction,
            enrichment=enrichment,
            skills_by_action=core.skills_by_action,
        )

        try:
            raw, resolve_usage = await core.llm.resolve(system_prompt)
        except LLMError as exc:
            return fail(exc.code, exc.message, exc.details)

        usage_parts.append(resolve_usage)
        res = normalize_resolution(raw)

    # ── Stage 5: post-parse guardrails + pending confirmation ──
    resolution = res["resolution"]
    action_name = res["action"]
    action_input = res["input"] or {}
    confidence = res["confidence"]
    threshold = core.behavior.get("confidenceThreshold", 0.75)

    post_block = run_post_parse_checks(
        action_name if resolution == "action" else resolution,
        action_input,
        security,
    )
    if post_block:
        turn = await _build_guardrail_turn(
            core, state, session_id, msg, started, post_block, usage_parts
        )
        return ok(turn)

    # Pending confirmation: user affirmed -> execute stored action.
    pending = state.get("pending")
    confirmed_via_pending = False
    if (
        pending
        and pending["type"] == "confirmation"
        and resolution in ("action", "chitchat")
        and msg.lower().strip() in {"yes", "y", "yeah", "yep", "confirm", "ok", "okay"}
    ):
        resolution = "action"
        action_name = pending["action"]
        action_input = pending.get("collectedInput") or {}
        confidence = 1.0
        confirmed_via_pending = True
        state["pending"] = None
    elif pending and pending["type"] == "confirmation" and msg.lower().strip() in {
        "no",
        "n",
        "nope",
        "cancel",
    }:
        state["pending"] = None
        turn = await _build_turn(
            core,
            state,
            session_id,
            msg,
            started,
            intent="confirmation",
            confidence=confidence,
            natural_language_response="Understood — I won't do that.",
            reasoning_used=reasoning_used,
            reasoning_summary=reasoning_summary,
            usage_parts=usage_parts,
        )
        if core.debug:
            _debug_print(turn, state, msg)
        return ok(turn)

    # ── Stage 6: clarification ──
    clarify_turn = await _handle_clarification(
        core, state, session_id, msg, started,
        res=res, resolution=resolution, action_name=action_name,
        action_input=action_input, confidence=confidence, threshold=threshold,
        pending=pending, reasoning_used=reasoning_used,
        reasoning_summary=reasoning_summary, usage_parts=usage_parts,
    )
    if clarify_turn is not None:
        return clarify_turn

    # ── Stage 7: terminal resolutions (chitchat / confirm / unsupported) ──
    if resolution in ("chitchat", "unsupported") or (
        resolution != "action" and resolution != "confirm"
    ):
        fallback = (
            "I can help with the actions registered for this application."
            if resolution == "unsupported"
            else "How can I help?"
        )
        # Generate the reply with a clean completion instead of trusting the
        # structured user_reply field — weaker models tend to echo the user's
        # message there. Fall back to user_reply, then to a static string.
        caps = "\n".join(
            f"- {a.get('name')}: {a.get('description', '').strip()}"
            for a in core.registry.all()
        )
        reply, chit_usage = await compose_chitchat(
            core.llm,
            core.identity,
            core.behavior,
            caps,
            msg,
            res.get("user_reply") or fallback,
        )
        usage_parts = (usage_parts or []) + ([chit_usage] if chit_usage else [])
        turn = await _build_turn(
            core,
            state,
            session_id,
            msg,
            started,
            intent=_intent_label(resolution),
            confidence=confidence,
            natural_language_response=reply,
            reasoning_used=reasoning_used,
            reasoning_summary=reasoning_summary,
            usage_parts=usage_parts,
        )
        if core.debug:
            _debug_print(turn, state, msg)
        return ok(turn)

    if resolution == "confirm" or (
        resolution == "action"
        and action_name
        and (core.registry.get(action_name) or {}).get("confirmation")
        and not (
            pending
            and pending["type"] == "confirmation"
            and msg.lower().strip() in {"yes", "y", "yeah", "confirm", "ok"}
        )
    ):
        action_def = core.registry.get(action_name) if action_name else None
        if action_def:
            state["pending"] = {
                "type": "confirmation",
                "action": action_name,
                "collectedInput": action_input,
                "missingFields": [],
                "attemptsUsed": 0,
            }
            question = res.get("user_reply") or (
                f"Should I go ahead with {action_name.replace('_', ' ')}?"
            )
            turn = await _build_turn(
                core,
                state,
                session_id,
                msg,
                started,
                intent="confirmation",
                confidence=confidence,
                natural_language_response=question,
                reasoning_used=reasoning_used,
                reasoning_summary=reasoning_summary,
                usage_parts=usage_parts,
            )
            if core.debug:
                _debug_print(turn, state, msg)
            return ok(turn)

    # ── Stage 8: validate input, safety-gate, then execute the action ──
    if resolution != "action" or not action_name:
        turn = await _build_turn(
            core,
            state,
            session_id,
            msg,
            started,
            intent="unsupported",
            confidence=confidence,
            natural_language_response=res.get("user_reply")
            or "I'm not sure how to help with that yet.",
            reasoning_used=reasoning_used,
            reasoning_summary=reasoning_summary,
            usage_parts=usage_parts,
        )
        if core.debug:
            _debug_print(turn, state, msg)
        return ok(turn)

    action_def = core.registry.get(action_name)
    if action_def:
        action_input = coalesce_action_input(
            action_name,
            action_input,
            msg,
            action_def.get("inputSchema"),
        )
        schema_errors = _validate_input(action_def["inputSchema"], action_input)
        if schema_errors:
            retry_prompt = build_system_prompt(
                core.identity,
                core.behavior,
                actions,
                state,
                msg,
                core.sessions.max_turns,
                correction=(
                    "Previous extraction failed schema validation: "
                    + "; ".join(schema_errors)
                ),
                enrichment=enrichment,
                skills_by_action=core.skills_by_action,
            )
            try:
                raw2, u2 = await core.llm.resolve(retry_prompt)
                usage_parts.append(u2)
                res = normalize_resolution(raw2)
                action_input = coalesce_action_input(
                    action_name,
                    res["input"] or {},
                    msg,
                    action_def.get("inputSchema"),
                )
                post_block = run_post_parse_checks(action_name, action_input, security)
                if post_block:
                    turn = await _build_guardrail_turn(
                        core, state, session_id, msg, started, post_block, usage_parts
                    )
                    return ok(turn)
                schema_errors = _validate_input(
                    action_def["inputSchema"], action_input
                )
            except LLMError:
                schema_errors = schema_errors

    gated = await _apply_contract_safety_gate(
        core, state, session_id, msg, started, action_name, action_input, confidence,
        confirmed=confirmed_via_pending,
        threshold=threshold,
        security=security,
        reasoning_used=reasoning_used,
        reasoning_summary=reasoning_summary,
        usage_parts=usage_parts,
    )
    if gated:
        return gated

    return await _execute_action_turn(
        core,
        state,
        session_id,
        msg,
        started,
        action_name,
        action_input,
        confidence,
        actions=actions,
        enrichment=enrichment,
        reasoning_used=reasoning_used,
        reasoning_summary=reasoning_summary,
        usage_parts=usage_parts,
    )
