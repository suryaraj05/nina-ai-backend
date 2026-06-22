# NINA

NINA is a conversational action layer for existing systems. You register your system's capabilities as actions — name, description, input schema, handler — and NINA maps natural-language user messages onto them: resolving intent, extracting parameters, asking for clarification when ambiguous, requesting confirmation for destructive operations, and composing a natural-language reply from your handler's result.

NINA is not a chatbot and it is not a UI: it operates your business logic and returns structured turn data. Every public call returns a result envelope — NINA never raises exceptions into your application.

## Install

```bash
pip install -e ".[dev]"   # from this repo
# or when published:
pip install nina-sdk
```

| Extra | Installs | Purpose |
|-------|----------|---------|
| `voice` | websockets, httpx | Full voice layer |
| `deepgram` | websockets | Streaming speech-to-text |
| `whisper` | httpx | File-based speech-to-text |
| `elevenlabs` | httpx | Streaming text-to-speech |

## Quickstart (local Ollama)

Prerequisites: [Ollama](https://ollama.com) running locally (`ollama serve`) with a model pulled, e.g. `ollama pull llama3.2`.

```python
import asyncio
from nina import Nina

async def main():
    nina = Nina()
    await nina.init({
        "llm": {
            "provider": "ollama",
            "model": "llama3.2",
            "endpoint": "http://localhost:11434",  # default
            "temperature": 0.2,
        }
    })
    await nina.register({
        "name": "track_order",
        "description": (
            "Looks up the shipping status of an order by its order id. "
            "Use when the user asks where an order is. Do not use for "
            "placing or cancelling orders."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "orderId": {
                    "type": "string",
                    "description": "Order id, e.g. ORD-1042.",
                }
            },
            "required": ["orderId"],
        },
        "handler": lambda inp, ctx: {
            "id": inp["orderId"],
            "status": "shipped",
        },
    })
    result = await nina.chat("where is my order ORD-1042?", "session-1")
    print(result["data"]["naturalLanguageResponse"])

asyncio.run(main())
```

Scaffold a runnable project: `nina init`

### Cloud providers

Set `provider` to `anthropic` or `openai` and supply `apiKey` as before. Ollama needs no API key for a local daemon.

| Provider | `endpoint` default | `apiKey` |
|----------|-------------------|----------|
| `ollama` | `http://localhost:11434` | optional |
| `anthropic` | `https://api.anthropic.com` | required |
| `openai` | `https://api.openai.com` | required |

**Recommended Ollama models:** `llama3.2`, `mistral`, `qwen2.5` — any model that follows JSON instructions reliably. Use `NINA_DEBUG=true` to inspect routing per turn.

## Architecture

```
src/nina/
├── chat.py          # Turn orchestration (resolve → execute → compose)
├── intent.py        # Prompt assembly + clarification
├── reasoner.py      # Pre-reasoning for goal-shaped queries
├── session.py       # History, pending flows, reference map
├── registry.py      # Action validation (V1–V8)
├── voice/           # Optional STT/TTS transport layer
examples/
├── ecommerce-fastapi/   # Production-style async demo + voice WS
└── legacy-flask/        # Sync Flask legacy integration
```

See [docs/API_CONTRACT.md](docs/API_CONTRACT.md) for the full v1.0.0 contract.

## Voice

```python
from nina.voice.config import build_voice_session

voice = build_voice_session(
    nina,
    "session-1",
    input_config={"provider": "deepgram", "api_key": DG_KEY},
    output_config={
        "provider": "elevenlabs",
        "api_key": EL_KEY,
        "voice_id": VOICE_ID,
    },
)
result = await voice.turn(audio_chunks)
```

## Website embed (overlay on your live site)

NINA is **not** a replacement website. Add one script tag; the site keeps working manually or via NINA:

```html
<script src="https://your-cdn/sdk/nina-bootstrap.js"
        data-site-id="your-site-id"
        data-api="/v1/query"
        data-manifest="/agent.json"
        defer></script>
```

Flow: user message → `POST /v1/query` with `page_context` → contract resolver → `instructions[]` → `NinaExecutor` runs `execute.steps` on the live DOM.

## Site contract and generator

| Artifact | Purpose |
|----------|---------|
| `nina.site.yaml` + `sitemap.xml` | Generator inputs (repo) |
| `auth.policy.yaml`, `risk.policy.yaml` | Auth gates and confirm/block rules |
| `agent.json` | Published site contract (pages, actions, selectors) |

```bash
nina-generate contracts/examples --dry-run   # validate pipeline
nina-generate contracts/examples             # write dist/agent.json
```

Schemas: [schemas/](schemas/). Docs: [docs/CONFIG_MODEL.md](docs/CONFIG_MODEL.md), [docs/RECOVERY_LOOP.md](docs/RECOVERY_LOOP.md).

See [sdk/](sdk/) and [examples/ecommerce-fastapi/public/agent.json](examples/ecommerce-fastapi/public/agent.json).

## Examples

- [examples/ecommerce-fastapi](examples/ecommerce-fastapi) — Split-panel store + embed SDK + Ollama
- [examples/legacy-flask](examples/legacy-flask) — Same catalogue on synchronous Flask

## Development

```bash
pip install -e ".[dev]"
pytest -v
```

## License

MIT — see [LICENSE](LICENSE).
