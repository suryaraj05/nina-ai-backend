---
name: support-skill
appliesTo: [show_message, contact_support, shipping_info, returns_policy]
description: >
  Handles store policy questions (shipping, returns, sizing help) and routes to
  show_message or support actions when the contract provides them.
composeGuidance: |
  Answer from action result or contract pages only. If unknown, say to check
  the store's policy page — do not invent return windows or shipping times.
---
## When to act
- Shipping cost/time, return policy, size guide, contact us, store hours.
- Prefer a contract action over chitchat when one exists.

## Parameters
- Pass `topic` or `messageId` per schema when required.

## Never
- Guarantee refunds, COD, or delivery timelines without verified data.
- For COD-specific questions, defer to the cod-skill actions when present.
