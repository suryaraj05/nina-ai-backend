---
name: order-tracking-skill
appliesTo: [track_order, get_order_status, order_status]
description: >
  Looks up order status or tracking when the user provides an order id or asks
  where their package is.
fastPath:
  - "track order {query}"
  - "where is my order {query}"
composeGuidance: |
  Report status and tracking fields exactly as returned. Never guess delivery dates.
clarifyGuidance: |
  If no order id was given, ask for order number or email used at checkout.
---
## When to act
- "Track my order", "order status", "where is package", "delivery update".

## Parameters
- **orderId** from user message; do not invent ids from chat history unless
  last_action_result included one.

## Never
- Promise delivery dates not in the API response.
