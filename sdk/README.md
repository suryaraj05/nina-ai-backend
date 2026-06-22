# NINA Browser SDK

Embeddable overlay for sites you control. The host website keeps working manually; NINA adds a panel that executes contract-bound actions on the live DOM.

## Quick embed

```html
<script src="https://your-cdn/sdk/nina-bootstrap.js"
        data-site-id="your-site-id"
        data-api="https://api.example.com/v1/query"
        data-manifest="https://your-cdn/sites/your-site-id/agent.json"
        data-report-url="https://api.example.com/v1/report-broken-selector"
        defer></script>
```

## Scripts

| File | Role |
|------|------|
| `nina-bootstrap.js` | Auto-init from script tag; loads CSS + modules |
| `nina-embed.js` | Chat panel UI; sends `page_context` + `snapshot` to API |
| `nina-contract.js` | Load `agent.json`, match pages, expand `execute.steps` |
| `nina-executor.js` | Run typed instructions; report broken selectors |
| `nina-panel.css` | Panel styles (isolated class prefix) |

## Runtime flow

1. Bootstrap loads `agent.json` and configures `NinaExecutor`.
2. User message → `POST /v1/query` with `transcript`, `page_context`, `session_hints`, `snapshot`.
3. API returns `instructions[]` (DOM ops, demo handlers, or `run_action`).
4. Executor runs steps; failures → `POST /v1/report-broken-selector`.

## SPA support

`nina-bootstrap.js` hooks `history.pushState`, `replaceState`, and `popstate` to refresh `page_context` on route changes.

## Manual init

```html
<script src="/sdk/nina-contract.js"></script>
<script src="/sdk/nina-executor.js"></script>
<script src="/sdk/nina-embed.js"></script>
<script>
  NinaContract.loadManifest("/agent.json").then(function (contract) {
    NinaExecutor.configure({ contract, reportUrl: "/v1/report-broken-selector" });
    NINA.init({ apiUrl: "/v1/query", contract: contract, panel: "right" });
  });
</script>
```

See [docs/CONFIG_MODEL.md](../docs/CONFIG_MODEL.md) and [docs/RECOVERY_LOOP.md](../docs/RECOVERY_LOOP.md).
