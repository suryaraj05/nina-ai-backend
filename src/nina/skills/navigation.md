---
name: navigation-skill
appliesTo: [navigate, open_page, open_category, list_categories, product_list]
description: >
  Navigates the shopper to pages, categories, or listing views when they ask
  to go somewhere or browse a section.
fastPath:
  - "go to {query}"
  - "open {query} page"
composeGuidance: |
  One short line confirming where you are sending them. No product claims.
---
## When to act
- "Take me to cart", "show categories", "men's section", "home page".
- Prefer the navigate/open_category action over chitchat when the contract has it.

## Parameters
- Use contract page ids or category slugs when known; otherwise pass the user's
  label as `query` or `categorySlug` per schema.

## Never
- Navigate to checkout without explicit user intent to purchase.
