# NINA Build Log

## Recovery (2026-06-12)

Reconstructed from `nina-prod/phase0.txt`–`phase5.txt` GitLab Fable exports.
GitLab repo `orbitx-group/Nina` had no SDK commits — only chat history.

## v1.0.0 — Phases 0–5

| Phase | Deliverable |
|-------|-------------|
| 0 | API contract spec → `docs/API_CONTRACT.md` |
| 1 | Core SDK: envelope, LLM providers, registry, session |
| 2 | Reasoner, reference map, targeted clarification |
| 3 | `ecommerce-fastapi` + `legacy-flask` demos |
| 4 | Voice layer: Deepgram, Whisper, ElevenLabs |
| 5 | CLI (`nina init`), types, validator V7/V8, debug mode |

**Tests:** 22+ passing (`pytest -v`)

## Ollama provider

- `llm.provider: "ollama"` — local/remote Ollama via `/api/chat`
- Structured JSON resolve using Ollama `format` + schema
- No `apiKey` required for local daemon; demos default to Ollama
