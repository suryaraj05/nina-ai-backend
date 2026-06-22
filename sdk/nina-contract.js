/**
 * NINA Contract runtime — load agent.json, match pages, build page_context.
 */
(function (global) {
  "use strict";

  function fnmatch(path, pattern) {
    const esc = pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*");
    const re = new RegExp("^" + esc + "$");
    return re.test(path);
  }

  function pathOf(url) {
    try {
      return new URL(url, global.location?.href).pathname || "/";
    } catch (_) {
      return "/";
    }
  }

  function matchFromRoutes(routes, path, normalized) {
    let best = { exact: 0, specificity: 0, id: null };
    for (const route of routes || []) {
      const pattern = route.pattern || "";
      const pageId = route.pageId;
      if (!pattern || !pageId) continue;
      const exact =
        path === pattern || normalized === pattern.replace(/\/$/, "");
      const matched =
        exact || fnmatch(path, pattern) || fnmatch(normalized, pattern);
      if (!matched) continue;
      const specificity = pattern.replace(/\*/g, "").length;
      if (
        exact > best.exact ||
        (exact === best.exact && specificity >= best.specificity)
      ) {
        best = { exact: exact ? 1 : 0, specificity, id: pageId };
      }
    }
    return best.id;
  }

  function matchPageId(contract, url) {
    const path = pathOf(url);
    const normalized = path.replace(/\/$/, "") || "/";
    const routeId = matchFromRoutes(contract.routes, path, normalized);
    if (routeId) return routeId;
    let best = { exact: 0, specificity: 0, id: null };
    for (const page of contract.pages || []) {
      const pattern = page.urlPattern || "";
      if (!pattern) continue;
      const exact =
        path === pattern || normalized === pattern.replace(/\/$/, "");
      const matched =
        exact || fnmatch(path, pattern) || fnmatch(normalized, pattern);
      if (!matched) continue;
      const specificity = pattern.replace(/\*/g, "").length;
      if (
        exact > best.exact ||
        (exact === best.exact && specificity >= best.specificity)
      ) {
        best = { exact: exact ? 1 : 0, specificity, id: page.id };
      }
    }
    return best.id;
  }

  function resolveSelector(contract, step) {
    if (step.selector) return step.selector;
    const sid = step.selectorId;
    if (sid && contract.selectors) return contract.selectors[sid] || null;
    return null;
  }

  function expandSteps(contract, action, params) {
    params = params || {};
    const steps = (action.execute && action.execute.steps) || [];
    const out = [];
    for (const step of steps) {
      const op = step.op;
      if (!op) continue;
      const inst = { type: op === "scroll" ? "scroll_to" : op };
      const sel = resolveSelector(contract, step);
      if (sel) inst.selector = sel;
      if (step.selectorId) inst.selectorId = step.selectorId;
      if (op === "fill") {
        inst.value = String(params[step.param] ?? step.value ?? "");
      } else if (op === "navigate") {
        let url = step.url || "";
        Object.keys(params).forEach((k) => {
          url = url.replace("{" + k + "}", params[k]);
        });
        inst.url = url;
      } else if (op === "api_call") {
        inst.method = step.method || "GET";
        inst.url = step.url;
        inst.body = step.body;
      } else if (op === "wait") {
        inst.ms = step.ms || 0;
      } else if (op === "toast" || op === "show_message") {
        inst.message = step.message || "";
        if (step.level) inst.level = step.level;
      } else if (op === "scroll") {
        inst.block = step.block || "start";
      }
      inst._actionId = action.id;
      inst._stepIndex = out.length;
      out.push(inst);
    }
    return out;
  }

  function collectSnapshot() {
    const headings = [];
    document.querySelectorAll("h1, h2, h3").forEach((el) => {
      const t = (el.textContent || "").trim();
      if (t && headings.length < 12) headings.push(t);
    });
    const labels = [];
    document.querySelectorAll("[aria-label], label").forEach((el) => {
      const t = (el.getAttribute("aria-label") || el.textContent || "").trim();
      if (t && labels.length < 20) labels.push(t);
    });
    return {
      title: document.title,
      headings,
      visibleLabels: labels,
    };
  }

  function sessionHints() {
    const hints = { cookies: {}, localStorage: {}, authenticated: false };
    try {
      document.cookie.split(";").forEach((pair) => {
        const [k, v] = pair.trim().split("=");
        if (k) hints.cookies[k] = v || "";
      });
    } catch (_) {}
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k) hints.localStorage[k] = localStorage.getItem(k);
      }
    } catch (_) {}
    return hints;
  }

  function buildPageContext(contract) {
    const url = global.location?.href || "";
    return {
      url,
      title: document.title,
      pageId: matchPageId(contract, url),
      contractVersion: contract.version,
      siteId: contract.site && contract.site.id,
    };
  }

  async function loadManifest(url) {
    const resp = await fetch(url, { credentials: "same-origin" });
    if (!resp.ok) throw new Error("Failed to load agent.json: " + resp.status);
    const contract = await resp.json();
    const routesUrl = url.replace(/agent\.json(\?.*)?$/i, "routes.manifest.json$1");
    if (routesUrl !== url) {
      try {
        const routesResp = await fetch(routesUrl, { credentials: "same-origin" });
        if (routesResp.ok) {
          const manifest = await routesResp.json();
          if (manifest.routes && !contract.routes) {
            contract.routes = manifest.routes;
          }
        }
      } catch (_) {}
    }
    return contract;
  }

  global.NinaContract = {
    fnmatch,
    matchPageId,
    resolveSelector,
    expandSteps,
    collectSnapshot,
    sessionHints,
    buildPageContext,
    loadManifest,
  };
})(typeof window !== "undefined" ? window : globalThis);
