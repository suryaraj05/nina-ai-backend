---
name: cart-skill
appliesTo: [add_to_cart, add_item_to_cart]
description: >
  Guides add-to-cart from search or product context: resolve the catalog id,
  then collect size and quantity via chips before the widget adds on-page.
fastPath:
  - "add {query} to cart"
  - "add {query} to my cart"
  - "get the {ordinal} one"
clarificationFlow:
  enabled: true
  steps:
    - field: size
      prompt: "Pick a size:"
      promptOnPdp: "Pick a size:"
      promptNavigate: "Opening {productName}. Pick a size:"
      promptRetry: "Tap your size:"
      chipsFrom: productOptions.sizes
      chipsDefault: [XS, S, M, L, XL, XXL]
    - field: quantity
      prompt: "Size {size} — how many?"
      chips: ["1", "2", "3"]
  complete:
    reply: "Added {productName} ({size} × {quantity}) to your cart."
    chips: ["What's in my cart?", "Continue shopping"]
composeGuidance: |
  After add-to-cart, one short sentence. Never quote cart totals unless they
  appear in the action result JSON.
clarifyGuidance: |
  For apparel, ask for size using chips from session productOptions when present.
  Ask quantity only after size is known.
---
## When to act
- Call add_to_cart when the user names a product, picks a search result, or says
  "add it" / "I'll take it" with a clear referent in REFERENCE MAP.
- If the product needs size (apparel) and size is missing, NINA runs the guided
  chip flow — do not skip straight to execute.

## Parameters
- **productId / variantId / sku**: copy verbatim from `last_search_results[].id`
  or `last_single_item.id`. Never use slugs, URLs, or title-derived strings.
- **quantity**: default 1 only when the guided flow already collected it.

## Reference resolution
- "Add it" after search → match title against `last_search_results` and use that
  row's `id`.
- "The second one" → use index from REFERENCE MAP.
- If two rows could match, `clarify` with concrete titles — do not guess.

## Examples
CORRECT: `{"productId": "cm9x7k2p1q003z", "name": "Wireless Mouse"}`
WRONG: `{"productId": "wireless-mouse"}` — slug is not a catalog id.

## Never
- Invent variant ids.
- Claim the item is in the cart without a successful action result.
- Auto-checkout or charge — add_to_cart is low-risk but not checkout.
