# NINA — Full Project Context

> A single document to bring a teammate from zero to fully understanding NINA — product, architecture, internals, security, operations, current state, and business. Read top-to-bottom; each section is self-contained.

Last updated: 2026-06-23 · Version: 1.0.0 · 197 tests passing

---

## 1. What NINA is (in one breath, then deeper)

**One line:** NINA is a *conversational action layer* you drop onto any website with one `<script>` tag. Instead of just chatting, it understands what a visitor wants and **does it** — searches products, adds to cart, checks an order, books a slot — by calling the site's own APIs.

**The deeper version:** Most "AI chat" widgets are glorified FAQ bots — they *answer*, they don't *act*. NINA is built around a contract of **actions** the host site exposes. A shopper says "add the blue one to my cart," NINA resolves that to the `add_to_cart` action, extracts `{productId: "...", qty: 1}`, validates it, and triggers the real API call. It's a thin, safe, model-agnostic bridge between natural language and a system's existing capabilities.

**Why it's different:**
- **Acts, not just answers.** The unit of work is an *action*, not a message.
- **One line of code.** Adoption is a script tag — no SDK rewrite, no platform lock-in.
- **Platform-agnostic.** Not tied to Shopify/WooCommerce — works on any stack (the demo store is NestJS; the reference example is FastAPI).
- **Bring-your-own-LLM (BYOLLM).** Merchants can plug in their own model key; their customer data never trains anyone's model.

---

## 2. The problem & why now

A website is a silent brochure. A visitor with a question — "is this in stock?", "where's my order?" — usually leaves rather than digging. The store owner never even knows they were there. Existing tools either (a) answer canned FAQs, or (b) require deep, platform-specific integration work.

NINA's bet: an LLM is now good enough to map fuzzy human intent onto a structured set of actions *reliably*, if you wrap it with the right guardrails (schema validation, grounding, a verification critic). The hard part isn't the chat — it's making the *action resolution* trustworthy and the *adoption* frictionless. That's the whole product.

---

## 3. Core mental model

Five nouns explain 90% of the system:

| Concept | What it is |
|---|---|
| **Site** | A merchant's store/app. Has a `site_id`, a base URL, allowed origins, an LLM config, and a contract. |
| **Contract** | The declaration of what NINA can do on a site: a list of **actions** (+ pages, selectors, auth rules). |
| **Action** | One capability: `name`, `description`, parameter schema, and an `execute` block (how to perform it — an API call and/or DOM steps). |
| **Turn** | One user message → NINA's resolution → a reply + optional instructions. The atomic unit of conversation. |
| **Instruction** | A machine step NINA emits for the widget or server to perform (e.g. `api_call`, `navigate`, `scroll_to`). |

The flow in one sentence: **a Turn resolves a user message against a Site's Contract, producing a reply and Instructions that carry out the chosen Action.**

---

## 4. System architecture

NINA is two things wearing one repo:

1. **The SDK / engine** (`src/nina/`) — a Python library (`Nina` class) that resolves and executes turns. Usable standalone.
2. **The multi-tenant console** (`console_app.py`) — a FastAPI service that wraps the engine for many merchants: onboarding, auth, key management, per-site isolation, and the `/v1/query` hot path the widget calls.

```
                       Browser (merchant's storefront)
                                 │  nina-bootstrap.js  (the widget)
                                 ▼
                       POST /v1/query  (X-NINA-API-Key)
                                 │
            ┌────────────────────▼─────────────────────┐
            │            FastAPI console_app            │
            │  middleware: request-id · admin-auth ·    │
            │              CORS · rate-limit            │
            │                                           │
            │  resolve API key ──► Site (Store)         │
            │  decrypt llmConfig ──► NinaPool           │
            └────────────────────┬─────────────────────┘
                                 ▼
                     NinaPool (one Nina() per site)
                                 │
                    run_turn()  ── the engine ──►  LLM provider
                                 │
                returns: reply + instructions (api_call, …)
                                 ▼
              Widget executes instructions in the browser
                  (calls the merchant's own API directly)
```

**Key architectural choices:**
- **One `Nina()` instance per site, cached** (`NinaPool`) with LRU eviction and a per-site async lock (isolates config, prevents cross-tenant bleed).
- **Actions usually run in the *browser*, not on our server.** NINA emits an `api_call` instruction; the visitor's browser calls the merchant's API. This means (a) we never proxy customer data, and (b) it works for `localhost` dev stores that our server could never reach.
- **Storage is swappable**: JSON file (`ConsoleStore`) for dev, PostgreSQL (`PgStore`) for production — selected by the `DATABASE_URL` env var.

---

## 5. The turn lifecycle (what happens on every message)

`chat.py → run_turn()` is the orchestrator. In order:

1. **Validate** the request (message present, session id, size limits — input capped to prevent prompt bloat).
2. **Load session** state (history, pending flow, reference map) from the session store.
3. **Pre-LLM guardrails** — prompt-injection screening; untrusted user text is wrapped in explicit `<<<UNTRUSTED_USER_BEGIN>>> … END>>>` markers so the model never confuses it with instructions.
4. **Fast-path** — deterministic pattern matches (e.g. obvious "reset") skip the LLM entirely.
5. **Build the system prompt** — persona, domain context, the contract's registered actions (each with description, input schema, examples), conversation history, pending flow, and a few-shot `<examples>` block. Context blocks are wrapped in XML tags so weaker models parse them reliably.
6. **Resolve (LLM call #1)** — the model returns a structured JSON object:
   ```json
   {"resolution":"action|clarify|confirm|chitchat|unsupported",
    "action": "...", "input": {...}, "missing_fields": [...],
    "confidence": 0.0-1.0, "user_reply": "..."}
   ```
7. **Normalize & ground** — clamp confidence; **parameter grounding** fixes hallucinated IDs (the model says `productId: "ipad-pro"` → NINA maps it back to the real `prod_987` via the session's reference map, no extra LLM call).
8. **Safety gates** — confidence threshold → clarify; `requiresAuth` → login gate; `requires_confirmation` → confirm step; the **critic** (LLM #) independently checks high-risk actions for parameters the user never actually asked for.
9. **Execute** the action (if resolved) — emit `api_call`/DOM instructions, or run a server-side handler.
10. **Compose (LLM call #2)** — turn the result (or chitchat) into a natural reply. Chitchat/unsupported replies are *generated* here, not taken from the structured field (weak models tend to echo the user otherwise).
11. **Record** the turn into history (capped by turn count *and* a ~4k-token character budget), persist session, return the envelope.

Every response is the same envelope: `{"ok": bool, "data": {...} | null, "error": {...} | null}`.

---

## 6. The contract & action system

A contract (`agent.json`) declares capabilities. An action looks like:

```json
{
  "id": "search_products",
  "description": "Search products by keyword or category",
  "parameters": { "query": {"type":"string","required":false} },
  "risk": "low",
  "requiresAuth": false,
  "execute": {
    "type": "api",
    "runtime": "browser",
    "apiRef": { "method": "GET", "path": "/search", "paramMap": {"q": "query"} }
  }
}
```

- **`execute.type`**: `api` (call an endpoint), `dom` (manipulate the page — click, scroll, fill), or `hybrid` (both).
- **`execute.runtime`**: `browser` (the widget calls the API — used for localhost & to avoid proxying data) or `server` (NINA's server calls it — guarded by SSRF checks).
- **`apiRef`** + the contract's `apis.<id>.baseUrl` (or `site.baseUrl`) → the full URL. `paramMap`/`bodyTemplate` map LLM-extracted params into the query/body.
- **Pages, selectors, auth, risk** blocks govern where actions are available, how DOM ops target elements, which actions are gated behind login, and which require confirmation.

---

## 7. Capability discovery — how NINA learns what a site can do

This is the crux of adoption friction, and there's a **tiered model**:

| Tier | Capability | How it's discovered | Dev effort |
|---|---|---|---|
| **Read-only** | "show me laptops", "return policy" | **Crawl the public storefront** (sitemap + product pages, via the generator pipeline) | **zero** |
| **Actions** | "add to cart", "track order" | **OpenAPI/Swagger URL** → `spec_to_actions` auto-generates actions | ~zero (paste a URL) |
| **Actions (fallback)** | same | **`nina-scan` CLI** on source → manifest of routes → (conversion needed) | high (CLI + repo) |

**Important nuance / known gap:** `nina-scan` produces a manifest of raw **routes** (`{path, method, authRequired, …}`), which is **not** the same as a **contract of actions** (which needs descriptions, schemas, execute blocks). A routes→actions conversion step is required and is currently the weak link. **The strategic direction is OpenAPI-URL-first** (especially since the demo store is NestJS, which exposes Swagger with a few lines) — it yields descriptions + schemas directly and needs no repo access or CLI. Source-scan is the last resort for APIs that can't expose a spec.

Supported scanner frameworks: FastAPI · Django · Flask · Express · NestJS · Laravel · Rails.

---

## 8. LLM integration

- **Provider abstraction (Strategy pattern):** `AnthropicProvider`, `OpenAIProvider`, `OllamaProvider`, `CustomProvider`, behind a common `resolve()` / `compose()` interface. `build_llm_client()` is the factory.
- **OpenAI-compatible endpoint is the workhorse** — it covers OpenAI, **Google Gemini** (via its OpenAI-compat URL), and **OpenRouter** (any model) just by changing the model name + endpoint.
- **Two calls per turn:** `resolve` (structured intent, forced tool/JSON output) and `compose` (natural-language reply). Resolve is the brittle one on weak models.
- **Robustness layers** (added because free models misbehave):
  - 3-tier resolution parsing: forced tool-call → JSON-from-content → retry without tools.
  - String-aware **balanced-brace** JSON extraction (tolerates prose, markdown fences, and *multiple* JSON blocks).
  - Retry with **exponential backoff + jitter**, honoring `retry-after`, on transient errors; **self-correction nudge** on malformed output (instead of resending the identical low-temperature prompt).
  - Few-shot exemplars + XML-delimited prompt for higher structured-output reliability.

**Hard-won lessons (recorded so nobody repeats them):**
- Google **Gemini free tier** gave `limit: 0` quota for some models (`gemini-2.0-flash`) on some accounts — the working free model is **`gemini-2.5-flash`**. Pay-as-you-go (enable billing) unlocks real limits and is the recommended demo path.
- **OpenRouter free Llama** *connects* but is too weak for reliable structured resolution even after all the tolerance fixes.
- A startup **ping** on init wasted a free-tier quota slot and rate-limited the first real query — removed.

---

## 9. Multi-tenancy & the pool

- A request carries `X-NINA-API-Key` → `resolve_key_to_site()` maps it to a Site (also enforces allowed-origin checks).
- `NinaPool` lazily builds and caches one `Nina()` per `site_id`, evicting LRU when over `NINA_POOL_MAX_SITES`.
- A **per-site async lock** serializes concurrent requests to the *same* site (prevents `_core.config` races); different sites run concurrently.
- A **circuit breaker** trips a site after N consecutive LLM failures and resets after a cooldown, so one broken merchant config can't hammer a provider.
- Pool eviction / shutdown **closes the LLM HTTP clients** (no TCP/fd leak).

---

## 10. Data model & storage

Two interchangeable stores implement the same interface:
- **`ConsoleStore`** — single JSON file (`nina_console_store.json`). Dev only. *Never committed* (contains merchant data).
- **`PgStore`** — PostgreSQL (Neon in production). Selected automatically when `DATABASE_URL` is set. Connections are health-checked (`SELECT 1`) and reconnected (Neon/Render idle-closes them).

Core entities: **Org** (a merchant account; owns a dashboard login token) → **Sites** (stores) → **API keys** (publishable `pk_…` for the widget; per-site) and **CLI tokens** (`nk_…`). Each site holds: `allowedOrigins`, encrypted `llmConfig`, `agentContract`, plan, usage counters.

---

## 11. Security model

- **API keys & tokens** are stored only as **HMAC-SHA256 digests** (one canonical algorithm shared by both stores — `crypto.hash_key`). Raw keys are shown once at issuance, never recoverable.
- **LLM keys encrypted at rest** with Fernet (`NINA_ENCRYPT_KEY`).
- **Timing-safe comparisons** everywhere (`hmac.compare_digest`).
- **Admin middleware** gates all `/v1/*` except the widget query (`/v1/query`), merchant-auth endpoints (`/v1/auth/*`, which validate a dashboard token internally), and static assets. Operator actions (e.g. token rotation) live under `/v1/` so the admin secret protects them.
- **SSRF guard** (`net_guard.py`) — resolves DNS and blocks loopback/private/link-local/cloud-metadata ranges; applied to every server-side outbound fetch (contract API handlers, wizard probes). Redirects disabled on server-side action calls.
- **Fail-closed in production** (`NINA_ENV=production`): the service refuses to start if `NINA_ENCRYPT_KEY`, `NINA_CONSOLE_KEY_HASH_SECRET`, or `NINA_CONSOLE_ADMIN_SECRET` are missing. In dev they fall back with warnings.
- **Rate limiting**: 60 req/min per IP, 200 req/min per API key (in-process; use Redis for multi-instance).
- **Prompt-injection defenses**: untrusted-input markers + XML-tagged context blocks.
- **Widget output safety**: LLM text is rendered as `textContent`, never `innerHTML`.

---

## 12. Session & memory

- `SessionManager` over a pluggable store: `MemoryStore` (default) or `RedisStore` (when `NINA_REDIS_URL` set).
- **History window**: kept to `maxTurns` *complete* turns (each turn = a user + a NINA entry), then additionally pruned to a ~4k-token character budget so a few large API payloads can't blow a small model's context.
- **TTL** via `expiresAt`; expired sessions are dropped.
- **Reference map**: recent search results are compacted into the prompt so pronouns resolve ("add *it* to cart") and hallucinated IDs get grounded to real catalog IDs.
- **Pending flows**: multi-turn clarification/confirmation state (collected inputs, missing fields, attempts).

---

## 13. Safety: guardrails, critic, hallucination mitigation

- **Schema validation** of extracted parameters against each action's `inputSchema` before any execution; a self-correction retry feeds the validation error back to the model.
- **Action existence check**: an unknown action name → `unsupported`, never a fabricated call.
- **Deterministic parameter grounding**: slug/name → real catalog ID via the reference map (no extra LLM call).
- **Action critic** (`critic.py`): an independent verification pass on high-risk actions — blocks when the model injected a parameter the user never stated (e.g. an unrequested coupon).
- **Confirmation & auth gates** for risky/gated actions.

---

## 14. The widget (embed)

`sdk/nina-bootstrap.js` — a self-contained script. Merchant pastes:

```html
<script src="https://nina-ai.onrender.com/sdk/nina-bootstrap.js"
        data-site-id="..." data-api="https://nina-ai.onrender.com"
        data-api-key="pk_..." defer></script>
```

It renders a chat button + panel, POSTs messages to `/v1/query`, and **executes the returned instructions in the browser** (`api_call` → `fetch` to the merchant's API; `navigate`, `scroll_to`, etc.). Session ids use `crypto.getRandomValues` (128-bit), never `Math.random`.

---

## 15. Merchant onboarding & dashboard

- **Onboarding wizard** (`index.html`, served at `/`): create org → site → publishable key → (optional) contract. Returns a **dashboard login token** (`dt_…`) shown **once** (recently fixed: it's now displayed on the success page with a save-it warning).
- **Merchant dashboard** (`/dashboard`): self-serve — sign in with the dashboard token, configure the AI provider (Gemini/OpenAI/OpenRouter presets + custom endpoint), manage allowed origins, upload/replace the agent contract, copy the widget snippet, issue/revoke keys, view usage. All its calls go through `/v1/auth/*` (dashboard-token-scoped), never the admin secret.

---

## 16. Deployment & operations

- **Stack:** FastAPI + Uvicorn, containerized (`Dockerfile`), on **Render free tier**, with **Neon** free PostgreSQL → effectively zero-cost hosting. Port **8787**.
- **Workers:** 1 by default (JSON store is single-process); multiple workers are safe only with `DATABASE_URL` set.
- **Observability:** structured JSON logs (ip, method, path, status, duration, error code, plan), per-request **correlation IDs** (`X-NINA-Request-Id`), in-process metrics (P50/P95 latency, success/fail/rate-limit counts) at `/v1/metrics`, optional **Sentry** (`SENTRY_DSN`) — internal chat exceptions are now explicitly captured.
- **Health:** `/health` reports the active store backend.
- **Caveat:** Render free instances spin down when idle (cold-start latency); Neon idle-closes connections (handled by reconnect logic).

---

## 17. Configuration (env vars)

| Var | Purpose |
|---|---|
| `DATABASE_URL` | If set → PostgreSQL (`PgStore`); else JSON file. Must be `postgresql://`. |
| `NINA_ENV` | `production` enables fail-closed security checks. |
| `NINA_CONSOLE_ADMIN_SECRET` | Bearer secret gating admin `/v1/*` routes. |
| `NINA_ENCRYPT_KEY` | Fernet key for encrypting LLM configs at rest. |
| `NINA_CONSOLE_KEY_HASH_SECRET` | HMAC secret for hashing API keys/tokens. |
| `NINA_REDIS_URL` | Use Redis for session storage. |
| `NINA_DEFAULT_LLM_CONFIG` | Operator fallback LLM (JSON) for sites without their own key. |
| `NINA_LLM_TIMEOUT_SECONDS` | LLM HTTP timeout (default 20). |
| `NINA_POOL_MAX_SITES`, `NINA_CIRCUIT_TRIP_AFTER`, `NINA_CIRCUIT_OPEN_SECONDS` | Pool & circuit-breaker tuning. |
| `UVICORN_WORKERS` | Worker count (keep 1 without `DATABASE_URL`). |

---

## 18. Repository layout (orientation)

```
src/nina/
  __init__.py          # the Nina facade: init / register / chat / session
  init.py              # config validation + LLM providers + LLMClient (resolve/compose, retries)
  chat.py              # run_turn() — the turn orchestrator
  contract.py          # contract → runtime instructions (api/dom/hybrid)
  contract_registry.py # contract actions → registered handlers (server-side API calls)
  api_template.py      # URL/body building from apiRef + params
  intent.py            # system-prompt assembly + resolution normalization
  prompt.py            # prompt templates (system, compose, chitchat, examples)
  responder.py         # compose_response / compose_chitchat
  critic.py            # action-alignment verification
  session.py           # session state, history window, reference map
  registry.py          # action registry + validation
  executor.py          # handler invocation with timeout
  pool.py              # NinaPool (per-site instances, LRU, circuit breaker)
  crypto.py            # hashing (HMAC) + Fernet seal/unseal + is_production
  net_guard.py         # SSRF guard (DNS-resolving)
  store_util.py        # shared store helpers (ids, slugs, key issuance)
  store.py             # Store Protocol (typed seam both stores satisfy)
  console_app.py       # FastAPI app factory + middleware + router mounts (wiring only)
  console_deps.py      # STORE/POOL singletons + dashboard-token/ownership guards
  console_infra.py     # rate limiters, JSON logging, metrics, SSRF/path validators
  console_schemas.py   # Pydantic request models
  console_store.py     # ConsoleStore (JSON file store)
  pg_store.py          # PgStore (PostgreSQL store)
  console_routes_*.py  # routers by domain: admin, auth, wizard, tools, query, channels
  console_static/      # index.html (onboarding), dashboard.html, admin.html
  sdk/nina-bootstrap.js# the embeddable widget
  scanner/             # nina-scan: framework detectors + per-framework scanners
  generator/           # site-crawl → contract generation pipeline (Playwright)
  voice/               # voice adapters (Deepgram/ElevenLabs/Whisper) — optional
docs/                  # this file + API_CONTRACT, SECURITY_MODEL, INTEGRATION, etc.
tests/                 # 197 tests
```

CLI entry points: `nina`, `nina-console`, `nina-scan`, `nina-generate`, `nina-validate`, `nina-probe-openapi`, `nina-dev`.

---

## 19. Testing

197 tests (`pytest`) covering the engine, multi-tenancy, usage metering, trust/prompt boundaries, skills, contract resolution, generator, and security paths. This suite is the safety net that makes internal refactors cheap — it's why architectural cleanups can be deferred without fear.

---

## 20. Current state (honest snapshot, 2026-06-23)

**Live & working:**
- Deployed at `https://nina-ai.onrender.com` (Render + Neon).
- Multi-tenant onboarding, merchant dashboard, key management, auth — all functional.
- The full **conversation engine works end-to-end** on the demo NestJS storefront (`localhost:3010`) with **`gemini-2.5-flash`** — coherent replies, no echo/malformed/rate-limit errors. `query ok` confirmed in logs.
- A large hardening pass is done: unified HMAC hashing, SSRF guard, fail-closed config, retry/backoff, robust JSON parsing, Sentry capture, HTTP-client cleanup, correlation IDs, prompt hardening, helper de-duplication, and several real bug fixes (history halving, dashboard-token display, unauthenticated rotate-token).

**The one open gap that matters for the demo:**
- NINA currently **chats but can't take commerce actions on the demo store**, because the uploaded contract is a raw `nina-scan` manifest (routes), not a contract of actions. The fix is the routes→actions step — strategically, **OpenAPI-URL-first** (the NestJS store can expose Swagger trivially).

---

## 21. Known gaps & roadmap

**Near-term:**
- **Routes → actions** generation (OpenAPI-first; the missing link for Tier-2 actions).
- Publish `nina-sdk` to PyPI (currently `pip install git+https://github.com/...`).
- Reshape onboarding to lead with "paste your API URL," not "run a CLI."

**Deliberately deferred (legitimate but not urgent — test suite makes them cheap anytime):**
- Sync DB calls inside async routes → wrap in `to_thread` / move to `asyncpg` (only matters under real concurrency).
- SRP split of the large `create_app` / `run_turn`; a formal service layer; a store ABC/Protocol.

**Guiding principle:** *External contracts* (API shapes, hashing, security posture, the widget interface) are expensive to change after launch — fix those now. *Internal structure* is cheap to refactor anytime because of the tests — defer until the product shape stabilizes.

---

## 22. Business model & go-to-market

- **Positioning:** the only conversational layer that *acts* (not just chats), adopted in one line, on any platform.
- **BYOLLM** is both a trust feature (customer data never trains a third-party model) and a margin lever (merchant pays for the platform, not our token bill — ~90%+ gross margin on BYOLLM plans).
- **India-first, global-second:** INR pricing and two moats foreign competitors can't easily match — **WhatsApp commerce** (Indians message, they don't fill forms) and **regional-language** support (Hindi/Tamil/Telugu/…). Then the same product at USD pricing globally.
- **Pricing shape:** a free tier to build trust, then paid tiers (hosted vs BYOLLM), with WhatsApp/Business features bundled higher. Break-even is a handful of paying merchants given the near-zero infra cost.
- **Funding context:** early-stage, working MVP with strong test coverage and clean unit economics; target programs are the low-friction accelerators/credits (YC, Antler, 100X.VC, Google/Microsoft/AWS credits, Anthropic/OpenAI startup programs).

---

## 23. The landing-page vision (product north star)

The marketing site should itself be a live NINA — **the page is the demo**. A provocative, self-narrating hero ("your website can't answer a single customer question — want to see mine read *yours*?") invites the visitor to **paste their store URL** and watch NINA read it in real time. The "scan-in-motion" (reading the homepage, finding products, learning the policy) carries most of the wow and is low-risk; the live answer is the high-risk payoff that *requires* a capable (paid) model. Provoke the *website's* limitation, never the person; always leave a calm, credible page underneath for skeptics who won't chat.

---

## 24. Glossary

- **Contract / `agent.json`** — a site's declaration of actions, pages, selectors, auth, risk.
- **Action** — one capability NINA can perform; has parameters + an `execute` block.
- **Resolve** — LLM call that maps a message to a structured resolution (action/clarify/confirm/chitchat/unsupported).
- **Compose** — LLM call that writes the natural-language reply.
- **Instruction** — a machine step the widget/server executes (`api_call`, `navigate`, …).
- **Reference map** — compacted recent results used for pronoun resolution & ID grounding.
- **Critic** — independent verification pass for high-risk actions.
- **NinaPool** — per-site cache of `Nina()` instances.
- **BYOLLM** — Bring-Your-Own-LLM: the merchant supplies their model key.
- **Publishable key (`pk_…`)** — public, goes in the widget snippet.
- **Dashboard token (`dt_…`)** — the merchant's dashboard login key, shown once.
