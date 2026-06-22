/**
 * NINA Embed SDK — panel overlay on any site; executes contract-bound actions.
 */
(function (global) {
  "use strict";

  const QUEUE_KEY = "nina-queued-intent";
  const PLAN_AUTH_KEY = "nina-plan-awaiting-auth";

  const state = {
    apiUrl: "/v1/query",
    apiKey: null,
    reportUrl: "/v1/report-broken-selector",
    sessionId: null,
    open: true,
    busy: false,
    panelSide: "right",
    contract: null,
    manifestUrl: "/agent.json",
    siteId: null,
    pageContext: null,
    onPageChange: null,
  };

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function ensureSessionId() {
    if (!state.sessionId) {
      state.sessionId =
        global.sessionStorage?.getItem("nina-session") || crypto.randomUUID();
      try {
        global.sessionStorage?.setItem("nina-session", state.sessionId);
      } catch (_) {}
    }
    return state.sessionId;
  }

  function refreshPageContext() {
    if (state.contract && global.NinaContract) {
      state.pageContext = global.NinaContract.buildPageContext(state.contract);
    } else {
      state.pageContext = {
        url: global.location?.href,
        title: document.title,
        pageId: null,
      };
    }
    return state.pageContext;
  }

  function mountPanel() {
    if (document.getElementById("nina-root")) return;

    const root = el("div", "nina-root");
    root.id = "nina-root";
    root.dataset.side = state.panelSide;

    const toggle = el("button", "nina-toggle", "Ask NINA");
    toggle.type = "button";
    toggle.title = "Open NINA assistant";

    const panel = el("aside", "nina-panel");
    panel.innerHTML =
      '<header class="nina-panel__head">' +
      "<div><strong>NINA</strong><span>acts on this page</span></div>" +
      '<button type="button" class="nina-panel__close" aria-label="Close panel">×</button>' +
      "</header>" +
      '<div class="nina-panel__thread" id="nina-thread"></div>' +
      '<form class="nina-panel__form" id="nina-form">' +
      '<input id="nina-input" placeholder="Ask NINA to search, navigate, checkout…" autocomplete="off">' +
      "<button type=\"submit\">Send</button>" +
      "</form>";

    root.appendChild(toggle);
    root.appendChild(panel);
    document.body.appendChild(root);

    document.body.classList.add("nina-embed-active");
    if (state.open) document.body.classList.add("nina-panel-open");

    toggle.onclick = () => setOpen(!state.open);
    panel.querySelector(".nina-panel__close").onclick = () => setOpen(false);
    panel.querySelector("#nina-form").onsubmit = (e) => {
      e.preventDefault();
      const input = panel.querySelector("#nina-input");
      const text = input.value.trim();
      if (!text || state.busy) return;
      input.value = "";
      send(text);
    };

    const siteName =
      (state.contract && state.contract.site && state.contract.site.name) ||
      "this site";
    appendNina(
      "Hi — I can help you use " +
        siteName +
        ". Ask me to search, navigate, or complete tasks on this page.",
      false
    );
  }

  function setOpen(open) {
    state.open = open;
    document.body.classList.toggle("nina-panel-open", open);
  }

  function thread() {
    return document.getElementById("nina-thread");
  }

  function appendUser(text) {
    const t = thread();
    if (!t) return;
    t.appendChild(el("div", "nina-msg nina-msg--user", text));
    t.scrollTop = t.scrollHeight;
  }

  function appendNina(text, clarify) {
    const t = thread();
    if (!t) return;
    const cls =
      "nina-msg nina-msg--nina" + (clarify ? " nina-msg--clarify" : "");
    t.appendChild(el("div", cls, text));
    t.scrollTop = t.scrollHeight;
  }

  function appendConfirm(onReply) {
    const t = thread();
    if (!t) return;
    const wrap = el("div", "nina-confirm");
    [
      ["Yes", "yes"],
      ["No", "no"],
    ].forEach(([label, reply]) => {
      const b = el("button", reply === "yes" ? "yes" : "", label);
      b.type = "button";
      b.onclick = () => {
        wrap.querySelectorAll("button").forEach((x) => (x.disabled = true));
        onReply(reply);
      };
      wrap.appendChild(b);
    });
    t.appendChild(wrap);
    t.scrollTop = t.scrollHeight;
  }

  function buildQueryBody(text, extra) {
    refreshPageContext();
    const NC = global.NinaContract;
    const body = {
      transcript: text,
      message: text,
      sessionId: ensureSessionId(),
      siteId: state.siteId,
      page_context: state.pageContext,
      contractVersion: state.contract?.version,
    };
    if (NC) {
      body.snapshot = NC.collectSnapshot();
      body.session_hints = NC.sessionHints();
    }
    if (extra && extra.replayQueued) {
      body.replayQueued = true;
    }
    return Object.assign(body, extra || {});
  }

  function persistQueuedIntent(instructions) {
    if (!instructions) return;
    for (const inst of instructions) {
      if (inst.type === "needs_login" && inst.queuedIntent) {
        try {
          global.sessionStorage?.setItem(
            QUEUE_KEY,
            JSON.stringify(inst.queuedIntent)
          );
        } catch (_) {}
      }
    }
  }

  function isAuthenticated() {
    const NC = global.NinaContract;
    if (!state.contract || !NC) return false;
    const indicator = (state.contract.auth || {}).sessionIndicator;
    if (!indicator) return false;
    const hints = NC.sessionHints();
    if (indicator.type === "cookie" && indicator.name) {
      return Boolean(hints.cookies && hints.cookies[indicator.name]);
    }
    if (indicator.type === "localStorage" && indicator.name) {
      return Boolean(hints.localStorage && hints.localStorage[indicator.name]);
    }
    return Boolean(hints.authenticated);
  }

  async function tryAuthReplay() {
    if (!global.sessionStorage?.getItem(QUEUE_KEY)) return;
    if (!isAuthenticated()) return;
    global.sessionStorage.removeItem(QUEUE_KEY);
    appendNina("Signed in — continuing your previous request…", false);
    await sendInternal("", { replayQueued: true }, false);
  }

  function persistPlanAuthPause(planStatus) {
    if (!planStatus) return;
    try {
      if (planStatus.awaitingAuth) {
        global.sessionStorage?.setItem(PLAN_AUTH_KEY, "1");
      } else if (planStatus.status === "completed" || planStatus.status === "cancelled") {
        global.sessionStorage?.removeItem(PLAN_AUTH_KEY);
      }
    } catch (_) {}
  }

  async function tryPlanResume() {
    if (!global.sessionStorage?.getItem(PLAN_AUTH_KEY)) return;
    if (!isAuthenticated()) return;
    global.sessionStorage.removeItem(PLAN_AUTH_KEY);
    appendNina("Signed in — resuming your plan…", false);
    await sendInternal("", { replayPlan: true }, false);
  }

  async function tryPostLoginContinue() {
    if (!isAuthenticated()) return;
    await tryAuthReplay();
    await tryPlanResume();
  }

  async function sendInternal(text, extra, showUserLine) {
    if (showUserLine && text) appendUser(text);
    state.busy = true;
    const typing = el("div", "nina-msg nina-msg--typing", "Working on it…");
    thread()?.appendChild(typing);
    thread().scrollTop = thread().scrollHeight;

    try {
      const headers = { "Content-Type": "application/json" };
      if (state.apiKey) headers["X-NINA-API-Key"] = state.apiKey;
      const resp = await fetch(state.apiUrl, {
        method: "POST",
        headers,
        body: JSON.stringify(buildQueryBody(text, extra)),
      });
      const envelope = await resp.json();
      typing.remove();

      if (!envelope.ok) {
        appendNina(
          (envelope.error?.code || "error") +
            ": " +
            (envelope.error?.message || "failed"),
          false
        );
        return;
      }

      const data = envelope.data;
      const message =
        data.naturalLanguageResponse ||
        data.message ||
        data.reply ||
        "Done.";
      appendNina(message, data.intent === "clarification");

      const instructions =
        data.instructions || data.clientInstructions || [];
      persistQueuedIntent(instructions);
      if (instructions.length && global.NinaExecutor) {
        await global.NinaExecutor.run(instructions);
      }

      if (data.planStatus) {
        persistPlanAuthPause(data.planStatus);
      }

      if (data.intent === "confirmation" || data.needsConfirmation) {
        appendConfirm((reply) =>
          send(reply, { confirmed: reply === "yes", priorIntent: data.intent })
        );
      }
    } catch (err) {
      typing.remove();
      appendNina("Could not reach NINA — is the server running?", false);
    } finally {
      state.busy = false;
    }
  }

  async function send(text, extra) {
    await sendInternal(text, extra, true);
  }

  function init(options) {
    options = options || {};
    if (options.apiUrl) state.apiUrl = options.apiUrl;
    if (options.reportUrl) state.reportUrl = options.reportUrl;
    if (options.sessionId) state.sessionId = options.sessionId;
    if (options.panel) state.panelSide = options.panel;
    if (options.open === false) state.open = false;
    if (options.contract) state.contract = options.contract;
    if (options.manifestUrl) state.manifestUrl = options.manifestUrl;
    if (options.siteId) state.siteId = options.siteId;
    if (options.apiKey) state.apiKey = options.apiKey;

    ensureSessionId();
    refreshPageContext();

    const api = {
      send,
      setOpen,
      tryAuthReplay,
      tryPlanResume,
      tryPostLoginContinue,
      getSessionId: () => state.sessionId,
      getPageContext: refreshPageContext,
      onPageChange: null,
    };

    mountPanel();
    setTimeout(() => tryPostLoginContinue(), 300);
    return api;
  }

  global.NINA = {
    init,
    send,
    setOpen,
    tryAuthReplay,
    tryPlanResume,
    tryPostLoginContinue,
  };
})(typeof window !== "undefined" ? window : globalThis);
