---
name: product-detail-skill
appliesTo: [get_product_detail, open_product, product_detail]
description: >
  Opens a product detail page when the user asks about a specific item, wants
  more info, or taps a product from search results.
fastPath:
  - "open {query}"
  - "show me {query}"
  - "tell me about {query}"
composeGuidance: |
  Summarize only fields present in the action result (title, price, sizes).
  Offer add-to-cart if the user seems ready.
clarifyGuidance: |
  If multiple search results match, ask which product using titles from REFERENCE MAP.
---
## When to act
- User asks price, sizes, stock, or details for a named product.
- User says "open the first one" after search → use REFERENCE MAP index.

## Parameters
- **productId**: from `last_search_results` or `last_single_item` — never slugs.

## Never
- Invent specifications not returned by the action or catalog.
