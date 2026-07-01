---
name: checkout-skill
appliesTo: [checkout, place_order]
description: >
  High-risk checkout and order placement. Always confirm before executing;
  never auto-charge on the first mention.
composeGuidance: |
  State order reference exactly as returned. Never fabricate confirmation numbers.
clarifyGuidance: |
  If cart is empty or user is not logged in, say what is blocking checkout and
  offer the next step (view cart, sign in).
---
## When to act
- User explicitly wants to pay, place order, or finish purchase **after**
  confirming → resolve to `confirm` first on the initial ask.
- Only call checkout after an unambiguous yes ("yes", "confirm", "place order").

## Parameters
- Use cart/session context from REFERENCE MAP; do not invent line items.

## Never
- Call checkout on "checkout" or "buy now" without confirmation.
- Bypass login — let auth-replay handle gated checkout.
- Promise success before the action returns.
