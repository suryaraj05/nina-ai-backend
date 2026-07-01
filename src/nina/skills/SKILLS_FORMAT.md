# NINA skills format

Skills are markdown playbooks that teach NINA **how to decide** — not how to
execute trust-critical mechanics (catalog lookup, SSRF, DOM clicks stay in code).

Inspired by [Cursor Agent Skills](https://cursor.com/docs): YAML frontmatter +
focused markdown body. **NINA maintains templates** in `skills/*.md`; at runtime
`skill_synth.py` maps them onto each site's contract action ids automatically.
Merchants do not author or upload skills.

## How skills are applied (automatic)

```
agent.json (actions, parameters, pages, risk)
        +
product catalog (optional — apparel → size chip flow)
        ↓
skill_synth.synthesize_skills()
        ↓
Per-site skill list → resolution / fast-path / cart chips / compose
```

On every `/v1/query`, the pool synthesizes skills when the contract fingerprint
changes (action ids, confirmActions, pages, catalog size, DOM size signals, locale).

### Automatic signals

| Signal | Source | Effect |
|--------|--------|--------|
| Size buttons on PDP | Crawl → `contract.signals.productDetail` | Enable cart size chips; set `chipsDefault` from DOM |
| Selector ids | `contract.selectors` (`size`, `variant`) | Enable size flow |
| Apparel catalog | Product names/categories | Enable size flow |
| `en-IN` / `IN` market | `contract.site.locales` / `markets` | Hindi COD phrasing; support fallback when no COD action |
| `pincode` param on COD action | OpenAPI / action schema | Auto `clarifyGuidance` to collect PIN first |
| `order_id` on tracking action | Action schema | Auto clarify guidance |
| `max_price` on search | Action schema | Body hint for budget queries |

## File layout (NINA repo only)

```
skills/
  SKILLS_FORMAT.md     # this file
  search.md            # template — role matched to contract search_* actions
  cart.md
  checkout.md
  ...
```

## Frontmatter (required + optional)

```yaml
---
# REQUIRED
name: cart-skill                      # template id; used by skill_synth role map
description: >                         # third person; WHAT + WHEN (≤1024 chars)
  Guides add-to-cart with size and quantity chips before DOM add.
appliesTo: [add_to_cart, add_item_to_cart]   # canonical examples; synth rewrites to site ids

# OPTIONAL — deterministic shortcuts (see fast_path.py)
fastPath:
  - "add {query} to cart"

# OPTIONAL — chip clarification (cart_flow.py); auto-disabled for non-apparel sites
clarificationFlow:
  enabled: true
  steps: [...]

composeGuidance: |
  One short sentence for the compose stage.

searchUX:
  emptyStrict: "..."
  emptyAlternatives: "..."

clarifyGuidance: |
  Extra context for clarification composer.
---
```

Synth appends **## This site's parameters** from each matched action's schema.

## Body (markdown)

Resolution-time rules for the LLM: when to act, parameter sources, examples, never-do list.

## Pipeline stages

| Stage | Field | Module |
|-------|--------|--------|
| Synthesis | contract + catalog | `skill_synth.synthesize_skills` |
| Action resolution | `body` + `description` | `intent.build_system_prompt` |
| Fast literal match | `fastPath` | `fast_path` |
| Chip flows | `clarificationFlow` | `skill_runtime` → `cart_flow` |
| Clarification | `clarifyGuidance` + body | `intent.generate_clarification` |
| Compose | `composeGuidance` | `responder.compose_response` |
| Search empty UX | `searchUX` | `catalog_rail.grounded_reply` |

## Authoring checklist (NINA engineers)

1. Add `role` mapping in `skill_synth._ROLE_BY_TEMPLATE` for new templates.
2. `appliesTo` lists canonical action id examples the role matcher should catch.
3. Test: `pytest tests/test_skill_synth.py tests/test_skills.py -q`
