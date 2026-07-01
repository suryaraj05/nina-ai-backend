---
name: view-cart-skill
appliesTo: [view_cart, cart]
description: >
  Shows the current cart contents when the user asks what is in their cart or
  wants to review items before checkout.
fastPath:
  - "what's in my cart"
  - "show my cart"
  - "view cart"
composeGuidance: |
  List items and total only from the action result. Suggest checkout only if cart
  is non-empty and user seems ready.
---
## When to act
- "What's in my cart?", "show cart", "cart total", "how much is my order".

## Parameters
- Usually no parameters; use session cart if the schema requires ids.

## Never
- Invent line items or totals not in the action response.
