/**
 * NINA Bootstrap — auto-init from script tag attributes.
 *
 * <script src="/sdk/nina-bootstrap.js"
 *         data-site-id="dhaaga-thread"
 *         data-api="/v1/query"
 *         data-manifest="/agent.json"
 *         defer></script>
 */
(function (global) {
  "use strict";

  function currentScript() {
    const scripts = document.getElementsByTagName("script");
    return scripts[scripts.length - 1];
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  function loadCss(href) {
    const l = document.createElement("link");
    l.rel = "stylesheet";
    l.href = href;
    document.head.appendChild(l);
  }

  function basePath() {
    const script = currentScript();
    const src = script && script.src;
    if (!src) return "/sdk/";
    return src.replace(/\/[^/]+$/, "/");
  }

  function observeSpaRoutes(onChange) {
    const origPush = history.pushState;
    const origReplace = history.replaceState;
    function notify() {
      onChange(global.location.href);
    }
    history.pushState = function () {
      origPush.apply(this, arguments);
      notify();
    };
    history.replaceState = function () {
      origReplace.apply(this, arguments);
      notify();
    };
    global.addEventListener("popstate", notify);
    return () => {
      history.pushState = origPush;
      history.replaceState = origReplace;
      global.removeEventListener("popstate", notify);
    };
  }

  async function bootstrap() {
    const script = currentScript();
    const ds = (script && script.dataset) || {};
    const sdkBase = basePath();
    const manifestUrl = ds.manifest || "/agent.json";
    const apiUrl = ds.api || "/v1/query";
    const reportUrl = ds.reportUrl || "/v1/report-broken-selector";
    const apiKey = ds.apiKey || null;
    const panel = ds.panel || "right";
    const requireApiKey = (ds.requireApiKey || "").toLowerCase() === "true";
    if (requireApiKey && !apiKey) {
      console.error("[NINA] data-require-api-key is true, but data-api-key is missing.");
      return null;
    }

    loadCss(sdkBase + "nina-panel.css");
    await loadScript(sdkBase + "nina-contract.js");
    await loadScript(sdkBase + "nina-executor.js");
    await loadScript(sdkBase + "nina-embed.js");

    let contract = null;
    try {
      contract = await global.NinaContract.loadManifest(manifestUrl);
    } catch (err) {
      console.warn("[NINA] Could not load manifest:", err);
    }

    if (contract && global.NinaExecutor) {
      global.NinaExecutor.configure({ contract, reportUrl });
    }

    const embed = global.NINA.init({
      apiUrl,
      apiKey,
      panel,
      contract,
      manifestUrl,
      reportUrl,
      siteId: ds.siteId || (contract && contract.site && contract.site.id),
    });

    observeSpaRoutes(function () {
      if (contract && global.NinaContract) {
        global.NINA.tryPostLoginContinue();
      }
    });

    global.addEventListener("focus", function () {
      global.NINA.tryPostLoginContinue();
    });

    global.NINA_BOOTSTRAPPED = true;
    return embed;
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})(typeof window !== "undefined" ? window : globalThis);
