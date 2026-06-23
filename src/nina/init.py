"""Configuration validation and the LLM provider abstraction (spec §1).

Built-in providers: anthropic (tool_use), openai (function calling), ollama
(local / remote Ollama with JSON structured resolve). Custom adapters are also
supported.
"""
import asyncio
import inspect
import json
import os
import random

import httpx

from .errors import LLMError

# Transient LLM failures worth retrying with backoff. Auth/config errors are
# permanent and must fail fast.
_RETRYABLE_LLM_CODES = {
    "NINA_LLM_RESPONSE_MALFORMED",
    "NINA_LLM_RATE_LIMITED",
    "NINA_LLM_UNREACHABLE",
}
_RETRY_BASE_DELAY = 0.5  # seconds; exponential backoff base
_RETRY_MAX_ATTEMPTS = 3  # initial attempt + 2 retries

VERSION = "1.0.0"
ANTHROPIC_VERSION = "2023-06-01"

RESOLUTION_SCHEMA = {
    "type": "object",
    "properties": {
        "resolution": {
            "type": "string",
            "enum": ["action", "clarify", "confirm", "chitchat", "unsupported"],
        },
        "action": {"type": ["string", "null"]},
        "input": {"type": ["object", "null"]},
        "missing_fields": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "user_reply": {"type": "string"},
    },
    "required": ["resolution", "missing_fields", "confidence", "user_reply"],
}

DEFAULTS = {
    "behavior": {
        "confidenceThreshold": 0.75,
        "maxClarifications": 2,
        "allowChitchat": True,
        "language": "auto",
    },
    "session": {"store": "memory", "ttlSeconds": 1800, "maxTurns": 20},
    "identity": {"agentName": "NINA", "persona": None, "systemContext": None},
}


def validate_config(config) -> list[str]:
    """Returns list of bad paths; empty list means structurally valid."""
    bad = []
    if not isinstance(config, dict):
        return ["<root>"]
    llm = config.get("llm")
    if not isinstance(llm, dict):
        return ["llm"]
    provider = llm.get("provider")
    if provider not in ("openai", "anthropic", "custom", "ollama"):
        bad.append("llm.provider")
    if provider != "custom":
        if not isinstance(llm.get("model"), str) or not llm.get("model"):
            bad.append("llm.model")
    if provider in ("openai", "anthropic"):
        if not isinstance(llm.get("apiKey"), str) or not llm.get("apiKey"):
            bad.append("llm.apiKey")
    if provider == "ollama" and "apiKey" in llm and not isinstance(
        llm["apiKey"], str
    ):
        bad.append("llm.apiKey")
    if "temperature" in llm and not isinstance(llm["temperature"], (int, float)):
        bad.append("llm.temperature")
    if "maxTokens" in llm and not isinstance(llm["maxTokens"], int):
        bad.append("llm.maxTokens")

    beh = config.get("behavior") or {}
    ct = beh.get("confidenceThreshold")
    if ct is not None and not (isinstance(ct, (int, float)) and 0 <= ct <= 1):
        bad.append("behavior.confidenceThreshold")
    mc = beh.get("maxClarifications")
    if mc is not None and not (isinstance(mc, int) and mc >= 0):
        bad.append("behavior.maxClarifications")

    sess = config.get("session") or {}
    ttl = sess.get("ttlSeconds")
    if ttl is not None and not (isinstance(ttl, (int, float)) and ttl >= 0):
        bad.append("session.ttlSeconds")
    mt = sess.get("maxTurns")
    if mt is not None and not (isinstance(mt, int) and mt > 0):
        bad.append("session.maxTurns")

    for hook in ("onActionCall", "onActionResult", "onError"):
        fn = (config.get("hooks") or {}).get(hook)
        if fn is not None and not callable(fn):
            bad.append(f"hooks.{hook}")
    return bad


def merged_section(config: dict, key: str) -> dict:
    out = dict(DEFAULTS.get(key, {}))
    out.update(config.get(key) or {})
    return out


def _usage(prompt_tokens, completion_tokens) -> dict:
    return {"promptTokens": prompt_tokens, "completionTokens": completion_tokens}


def _retry_delay(attempt: int, err: LLMError | None) -> float:
    """Backoff before the next LLM retry. Honors a provider-supplied
    retry-after when present, otherwise exponential backoff with jitter."""
    if err is not None and isinstance(getattr(err, "details", None), dict):
        retry_after_ms = err.details.get("retryAfterMs")
        if isinstance(retry_after_ms, (int, float)) and retry_after_ms > 0:
            return min(retry_after_ms / 1000.0, 10.0)
    return _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.25)


def _iter_balanced_objects(text: str):
    """Yield each top-level balanced ``{...}`` substring in *text*, honoring
    braces inside JSON strings so it doesn't miscount on `{` within values."""
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    yield text[start : i + 1]
                    start = -1


def _extract_json_object(text: str) -> dict:
    """Parse a JSON object from model output, tolerating markdown fences,
    surrounding prose, and multiple JSON blocks. Raises json.JSONDecodeError
    if nothing parses."""
    if not text:
        raise json.JSONDecodeError("empty", text or "", 0)
    cleaned = text.strip()
    # Strip ```json ... ``` or ``` ... ``` fences if present.
    if cleaned.startswith("```"):
        parts = cleaned.split("```", 2)
        cleaned = parts[1] if len(parts) > 1 else text
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
        cleaned = cleaned.strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # Scan for balanced {...} blocks (not find('{')/rfind('}'), which merges two
    # separate blocks into one invalid string). A model may emit a reasoning
    # block then the answer; prefer a block carrying a "resolution" key, else the
    # last valid object (final-answer convention).
    valid: list[dict] = []
    last_err: json.JSONDecodeError | None = None
    for candidate in _iter_balanced_objects(cleaned):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_err = exc
            continue
        if isinstance(obj, dict):
            valid.append(obj)
    resolution_blocks = [d for d in valid if "resolution" in d]
    if resolution_blocks:
        return resolution_blocks[-1]
    if valid:
        return valid[-1]
    if last_err is not None:
        raise last_err
    raise json.JSONDecodeError("no JSON object found", cleaned, 0)


class _HttpProvider:
    """Shared httpx plumbing + spec-mandated HTTP error mapping."""

    def __init__(self):
        timeout = float(os.environ.get("NINA_LLM_TIMEOUT_SECONDS", "20"))
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        """Close the underlying HTTP client, releasing pooled TCP connections."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _post(self, url: str, payload: dict, headers: dict) -> dict:
        try:
            resp = await self._client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError("NINA_LLM_UNREACHABLE",
                           f"Could not reach LLM provider: {exc}.")
        if resp.status_code in (401, 403):
            raise LLMError("NINA_LLM_AUTH_FAILED",
                           "LLM provider rejected credentials.")
        if resp.status_code == 429:
            ra = resp.headers.get("retry-after")
            retry_ms = int(float(ra) * 1000) if ra else None
            try:
                body_text = resp.text[:500]
            except Exception:
                body_text = ""
            raise LLMError("NINA_LLM_RATE_LIMITED",
                           f"Rate limited. Retry after {retry_ms} ms. Body: {body_text}",
                           {"retryAfterMs": retry_ms})
        if resp.status_code >= 400:
            try:
                body_text = resp.text[:500]
            except Exception:
                body_text = ""
            raise LLMError("NINA_LLM_UNREACHABLE",
                           f"LLM provider HTTP {resp.status_code}: {body_text}")
        return resp.json()


class AnthropicProvider(_HttpProvider):
    def __init__(self, llm_cfg: dict):
        super().__init__()
        self.model = llm_cfg["model"]
        self.api_key = llm_cfg["apiKey"]
        self.endpoint = (llm_cfg.get("endpoint") or "https://api.anthropic.com").rstrip("/")
        self.temperature = llm_cfg.get("temperature", 0.2)
        self.max_tokens = llm_cfg.get("maxTokens", 1024)

    def _headers(self):
        return {"x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json"}

    async def ping(self):
        await self._post(f"{self.endpoint}/v1/messages", {
            "model": self.model, "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }, self._headers())

    async def resolve(self, system_prompt: str):
        body = await self._post(f"{self.endpoint}/v1/messages", {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system_prompt,
            "messages": [{"role": "user",
                          "content": "Resolve the current user message now."}],
            "tools": [{"name": "resolve_turn",
                       "description": "Return the resolution object for the "
                                      "current user message, per the rules in "
                                      "the system prompt.",
                       "input_schema": RESOLUTION_SCHEMA}],
            "tool_choice": {"type": "tool", "name": "resolve_turn"},
        }, self._headers())
        u = body.get("usage") or {}
        usage = _usage(u.get("input_tokens"), u.get("output_tokens"))
        # Preferred: the forced tool_use block.
        for block in body.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "resolve_turn":
                return block["input"], usage
        # Fallback: the model sometimes returns the JSON as plain text instead of
        # a tool_use block. Parse it leniently (parity with OpenAIProvider) rather
        # than failing outright.
        text = "".join(
            b.get("text", "") for b in body.get("content", [])
            if b.get("type") == "text"
        )
        try:
            return _extract_json_object(text), usage
        except (TypeError, json.JSONDecodeError):
            raise LLMError("NINA_LLM_RESPONSE_MALFORMED",
                           "Model returned unparseable output after retries.")

    async def compose(self, prompt: str):
        body = await self._post(f"{self.endpoint}/v1/messages", {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }, self._headers())
        u = body.get("usage") or {}
        text = "".join(b.get("text", "") for b in body.get("content", [])
                       if b.get("type") == "text").strip()
        if not text:
            raise LLMError("NINA_LLM_RESPONSE_MALFORMED",
                           "Model returned unparseable output after retries.")
        return text, _usage(u.get("input_tokens"), u.get("output_tokens"))


class OpenAIProvider(_HttpProvider):
    def __init__(self, llm_cfg: dict):
        super().__init__()
        self.model = llm_cfg["model"]
        self.api_key = llm_cfg["apiKey"]
        # Default endpoint includes /v1 so callers only append /chat/completions.
        # Custom endpoints (e.g. Gemini: .../v1beta/openai) also end at the base
        # path and get /chat/completions appended — no /v1 is added by the code.
        self.endpoint = (llm_cfg.get("endpoint") or "https://api.openai.com/v1").rstrip("/")
        self.temperature = llm_cfg.get("temperature", 0.2)
        self.max_tokens = llm_cfg.get("maxTokens", 1024)

    def _headers(self):
        return {"authorization": f"Bearer {self.api_key}",
                "content-type": "application/json"}

    async def ping(self):
        await self._post(f"{self.endpoint}/chat/completions", {
            "model": self.model, "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }, self._headers())

    async def resolve(self, system_prompt: str):
        body = await self._post(f"{self.endpoint}/chat/completions", {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": (
                    "Resolve the current user message now. Respond with ONLY the "
                    "JSON object described in OUTPUT FORMAT — no markdown, no prose."
                )},
            ],
            "tools": [{"type": "function",
                       "function": {"name": "resolve_turn",
                                    "description": "Return the resolution object.",
                                    "parameters": RESOLUTION_SCHEMA}}],
            "tool_choice": {"type": "function",
                            "function": {"name": "resolve_turn"}},
        }, self._headers())
        u = body.get("usage") or {}
        usage = _usage(u.get("prompt_tokens"), u.get("completion_tokens"))
        msg = ((body.get("choices") or [{}])[0].get("message")) or {}
        # 1) Preferred path: a forced function/tool call (strong models).
        try:
            args = msg["tool_calls"][0]["function"]["arguments"]
            return _extract_json_object(args), usage
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            pass
        # 2) Fallback: many models (esp. free/open ones) ignore tool_choice and
        #    put the JSON in the message content instead. Parse it leniently.
        try:
            return _extract_json_object(msg.get("content") or ""), usage
        except (TypeError, json.JSONDecodeError):
            pass
        # 3) Last resort: retry once WITHOUT tools, asking for plain JSON. Some
        #    free models only emit valid JSON when not given a tool schema.
        retry = await self._post(f"{self.endpoint}/chat/completions", {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": (
                    "Resolve the current user message now. Output ONLY the JSON "
                    "object from OUTPUT FORMAT. No markdown fences, no prose."
                )},
            ],
        }, self._headers())
        ru = retry.get("usage") or {}
        rmsg = ((retry.get("choices") or [{}])[0].get("message")) or {}
        try:
            return _extract_json_object(rmsg.get("content") or ""), _usage(
                ru.get("prompt_tokens"), ru.get("completion_tokens"))
        except (TypeError, json.JSONDecodeError):
            raise LLMError("NINA_LLM_RESPONSE_MALFORMED",
                           "Model returned unparseable output after retries.")

    async def compose(self, prompt: str):
        body = await self._post(f"{self.endpoint}/chat/completions", {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }, self._headers())
        u = body.get("usage") or {}
        text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
        if not text or not text.strip():
            raise LLMError("NINA_LLM_RESPONSE_MALFORMED",
                           "Model returned unparseable output after retries.")
        return text.strip(), _usage(u.get("prompt_tokens"), u.get("completion_tokens"))


class OllamaProvider(_HttpProvider):
    """Local or remote Ollama via /api/chat.

    Uses Ollama's structured JSON output for resolve() and plain text for
    compose(). No API key is required for the default local daemon; set
    endpoint to a remote host or apiKey when your deployment requires it.
    """

    def __init__(self, llm_cfg: dict):
        super().__init__()
        self.model = llm_cfg["model"]
        self.endpoint = (
            llm_cfg.get("endpoint") or "http://localhost:11434"
        ).rstrip("/")
        self.api_key = llm_cfg.get("apiKey") or ""
        self.temperature = llm_cfg.get("temperature", 0.2)
        self.max_tokens = llm_cfg.get("maxTokens", 1024)

    def _headers(self) -> dict:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    async def ping(self):
        try:
            resp = await self._client.get(
                f"{self.endpoint}/api/tags", headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise LLMError(
                "NINA_LLM_UNREACHABLE",
                f"Could not reach Ollama at {self.endpoint}: {exc}. "
                "Is `ollama serve` running?",
            ) from exc
        if resp.status_code >= 400:
            raise LLMError(
                "NINA_LLM_UNREACHABLE",
                f"Could not reach Ollama: HTTP {resp.status_code}.",
            )

    async def _chat(self, messages: list[dict], *, json_format=None):
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }
        if json_format is not None:
            payload["format"] = json_format
        body = await self._post(
            f"{self.endpoint}/api/chat", payload, self._headers()
        )
        text = ((body.get("message") or {}).get("content") or "").strip()
        if not text:
            raise LLMError(
                "NINA_LLM_RESPONSE_MALFORMED",
                "Ollama returned an empty response.",
            )
        usage = _usage(
            body.get("prompt_eval_count"), body.get("eval_count")
        )
        return text, usage

    def _parse_resolution_json(self, text: str) -> dict:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end != -1:
                return json.loads(text[start : end + 1])
            raise

    async def resolve(self, system_prompt: str):
        text, usage = await self._chat(
            [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Resolve the current user message now. Return ONLY the "
                        "JSON object described in OUTPUT FORMAT — no markdown, "
                        "no prose."
                    ),
                },
            ],
            json_format=RESOLUTION_SCHEMA,
        )
        try:
            return self._parse_resolution_json(text), usage
        except (json.JSONDecodeError, TypeError):
            raise LLMError(
                "NINA_LLM_RESPONSE_MALFORMED",
                "Ollama returned unparseable JSON for resolution.",
            ) from None

    async def compose(self, prompt: str):
        text, usage = await self._chat(
            [{"role": "user", "content": prompt}],
        )
        return text, usage


class CustomProvider:
    """Wraps a developer adapter: (promptPayload) -> LLMCompletion."""

    def __init__(self, adapter):
        self.adapter = adapter

    async def _call(self, payload: dict):
        try:
            result = self.adapter(payload)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception as exc:
            raise LLMError("NINA_LLM_UNREACHABLE",
                           f"Could not reach LLM provider: {exc}.")

    async def ping(self):  # no handshake defined for custom adapters
        return None

    async def resolve(self, system_prompt: str):
        out = await self._call({"mode": "resolve", "prompt": system_prompt,
                                "outputSchema": RESOLUTION_SCHEMA})
        if isinstance(out, dict) and "resolution" in out:
            return out, {}
        text = out.get("text") if isinstance(out, dict) else out
        try:
            return json.loads(text), {}
        except (TypeError, json.JSONDecodeError):
            raise LLMError("NINA_LLM_RESPONSE_MALFORMED",
                           "Model returned unparseable output after retries.")

    async def compose(self, prompt: str):
        out = await self._call({"mode": "compose", "prompt": prompt})
        text = out.get("text") if isinstance(out, dict) else out
        if not isinstance(text, str) or not text.strip():
            raise LLMError("NINA_LLM_RESPONSE_MALFORMED",
                           "Model returned unparseable output after retries.")
        return text.strip(), {}


class LLMClient:
    """Retry wrapper. Malformed model output is retried twice (spec §3)."""

    RESOLUTIONS = {"action", "clarify", "confirm", "chitchat", "unsupported"}

    def __init__(self, provider):
        self.provider = provider

    async def ping(self):
        await self.provider.ping()

    async def resolve(self, system_prompt: str):
        last: LLMError | None = None
        for attempt in range(_RETRY_MAX_ATTEMPTS):
            # On a retry after malformed output, nudge the model to self-correct
            # instead of re-sending the identical prompt (deterministic at low
            # temperature, which otherwise reproduces the same bad output).
            prompt = system_prompt
            if attempt > 0 and last and last.code == "NINA_LLM_RESPONSE_MALFORMED":
                prompt = (
                    system_prompt
                    + "\n\n[RETRY] Your previous reply was not valid JSON matching "
                    "the OUTPUT FORMAT. Return ONLY the JSON object, nothing else."
                )
            try:
                resolution, usage = await self.provider.resolve(prompt)
                if (isinstance(resolution, dict)
                        and resolution.get("resolution") in self.RESOLUTIONS):
                    return resolution, usage
                last = LLMError("NINA_LLM_RESPONSE_MALFORMED",
                                "Model returned unparseable output after retries.")
            except LLMError as exc:
                # Fail fast on permanent errors (auth, bad config); retry transient.
                if exc.code not in _RETRYABLE_LLM_CODES:
                    raise
                last = exc

            if attempt < _RETRY_MAX_ATTEMPTS - 1:
                await asyncio.sleep(_retry_delay(attempt, last))
        raise last

    async def compose(self, prompt: str):
        return await self.provider.compose(prompt)

    async def aclose(self) -> None:
        aclose = getattr(self.provider, "aclose", None)
        if aclose is not None:
            await aclose()


def build_llm_client(llm_cfg: dict) -> LLMClient:
    provider_name = llm_cfg["provider"]
    if provider_name == "anthropic":
        return LLMClient(AnthropicProvider(llm_cfg))
    if provider_name == "openai":
        return LLMClient(OpenAIProvider(llm_cfg))
    if provider_name == "ollama":
        return LLMClient(OllamaProvider(llm_cfg))
    return LLMClient(CustomProvider(llm_cfg["adapter"]))
