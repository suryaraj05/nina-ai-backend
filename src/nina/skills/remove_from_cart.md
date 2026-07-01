---
name: remove-from-cart-skill
appliesTo: [remove_from_cart, clear_cart]
description: >
  Removes items or clears the cart when the user asks to delete, remove, or
  empty cart lines.
composeGuidance: |
  Confirm what was removed using the action result. For clear_cart, warn if the
  cart had multiple items.
clarifyGuidance: |
  If multiple cart lines exist, ask which item using titles from cart_contents.
---
## When to act
- "Remove the hoodie", "delete second item", "empty my cart", "clear cart".

## Parameters
- Resolve line id from `cart_contents` in REFERENCE MAP when possible.
- clear_cart may need no parameters — check schema.

## Never
- Remove items without matching cart_contents when ambiguous.
