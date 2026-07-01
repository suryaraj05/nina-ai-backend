---
name: search-skill
appliesTo: [search_products, search, filter_products]
description: >
  Interprets product search requests including price filters, vague browsing
  phrases, and Hindi/English mix. Prefer searching over stalling.
fastPath:
  - "search for {query}"
  - "search {query}"
  - "find {query}"
  - "look for {query}"
searchUX:
  emptyStrict: "I couldn't find anything matching that in the catalog."
  emptyAlternatives: >
    I couldn't find an exact match, but here are some similar options you can try instead.
composeGuidance: |
  For search results, mention count briefly. Never invent products or prices —
  only describe rows returned in the action result.
---
## When to act
- User wants to browse, find, filter, or discover products → call search (or
  filter_products when the contract exposes it).
- Vague requests ("something for summer", "nice hoodies") still warrant a search
  with your best-guess query terms.

## Parameters
- Fold price constraints into the query when there is no dedicated price field:
  "hoodies under 3000", "below 70k" → normalize k/lakh shorthand first.
- Never add fast-path patterns like "show me {query}" — those need reasoning.

## Reference resolution
- After search, `last_search_results` is populated for follow-up "add it" turns.

## Examples
User: "hoodies under ₹3000" → `{"query": "hoodies under 3000"}`
User: "do you have running shoes?" → `{"query": "running shoes"}`

## Never
- State a product exists or quote a price not in the action result.
- If count is 0, say so honestly; alternatives may be offered by the catalog layer.
