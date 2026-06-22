/**
 * NINA DOM Executor — runs typed instructions and contract execute.steps.
 */
(function (global) {
  "use strict";

  const DEFAULTS = {
    productGrid: "#nina-product-grid",
    cartBadge: "#nina-cart-count",
    cartDrawer: "#nina-cart-drawer",
    cartItems: "#nina-cart-items",
    cartTotal: "#nina-cart-total",
    detailPanel: "#nina-product-detail",
    toastHost: "#nina-toasts",
    flashClass: "nina-flash",
  };

  const state = {
    contract: null,
    reportUrl: "/v1/report-broken-selector",
    onFailure: null,
  };

  function $(sel, root) {
    if (!sel) return null;
    return (root || document).querySelector(sel);
  }

  function isVisible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function isSensitiveElement(el) {
    if (!el) return false;
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "input") {
      const type = (el.getAttribute("type") || "text").toLowerCase();
      if (type === "password") return true;
      const ac = (el.getAttribute("autocomplete") || "").toLowerCase();
      if (
        ac === "current-password" ||
        ac === "new-password" ||
        ac === "one-time-code"
      ) {
        return true;
      }
    }
    if (tag === "textarea" && /password|secret|cvv|cvc/i.test(el.name || "")) {
      return true;
    }
    return false;
  }

  function denySensitiveOp(inst, op) {
    const el = $(inst.selector);
    if (!el || !isSensitiveElement(el)) return false;
    toast(
      "NINA cannot fill or click password or sensitive fields for your security.",
      "warning"
    );
    reportFailure(inst, "not_interactable");
    return true;
  }

  function flash(el) {
    if (!el) return;
    el.classList.add(DEFAULTS.flashClass);
    setTimeout(() => el.classList.remove(DEFAULTS.flashClass), 1200);
  }

  function toast(message, type) {
    const host = $(DEFAULTS.toastHost) || document.body;
    const node = document.createElement("div");
    node.className = "nina-toast" + (type ? " " + type : "");
    node.textContent = message;
    host.appendChild(node);
    requestAnimationFrame(() => node.classList.add("show"));
    setTimeout(() => {
      node.classList.remove("show");
      setTimeout(() => node.remove(), 300);
    }, 3200);
  }

  function reportFailure(inst, reason) {
    const NC = global.NinaContract;
    const contract = state.contract;
    if (!contract || !state.reportUrl) return;
    const pageCtx = NC ? NC.buildPageContext(contract) : {};
    const payload = {
      siteId: contract.site?.id,
      contractVersion: contract.version,
      pageUrl: pageCtx.url || location.href,
      pageId: pageCtx.pageId,
      userAgent: navigator.userAgent,
      failures: [{
        actionId: inst._actionId || "unknown",
        stepIndex: inst._stepIndex ?? 0,
        op: inst.type,
        selector: inst.selector,
        selectorId: inst.selectorId,
        reason,
      }],
      snapshot: NC ? NC.collectSnapshot() : {},
      reportedAt: new Date().toISOString(),
    };
    fetch(state.reportUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      credentials: "same-origin",
    }).catch(() => {});
    if (state.onFailure) state.onFailure(payload);
  }

  function runDomOp(inst) {
    const type = inst.type;
    if (type === "scroll_to" || type === "scroll") {
      const el = $(inst.selector || inst.target);
      if (!el) {
        reportFailure(inst, "not_found");
        return false;
      }
      if (!isVisible(el)) {
        reportFailure(inst, "not_visible");
        return false;
      }
      el.scrollIntoView({ behavior: "smooth", block: inst.block || "start" });
      flash(el);
      return true;
    }
    if (type === "click") {
      const el = $(inst.selector);
      if (!el) {
        reportFailure(inst, "not_found");
        return false;
      }
      if (denySensitiveOp(inst, "click")) return false;
      if (!isVisible(el)) {
        reportFailure(inst, "not_interactable");
        return false;
      }
      el.click();
      flash(el);
      return true;
    }
    if (type === "fill") {
      const el = $(inst.selector);
      if (!el) {
        reportFailure(inst, "not_found");
        return false;
      }
      if (denySensitiveOp(inst, "fill")) return false;
      el.value = inst.value ?? "";
      el.dispatchEvent(new Event("input", { bubbles: true }));
      flash(el);
      return true;
    }
    if (type === "navigate") {
      if (inst.url) window.location.href = inst.url;
      return true;
    }
    if (type === "wait") {
      return new Promise((resolve) => setTimeout(resolve, inst.ms || 0));
    }
    if (type === "api_call") {
      const method = (inst.method || "GET").toUpperCase();
      let url = inst.url || "";
      if (inst.query && typeof inst.query === "object") {
        const qs = new URLSearchParams();
        Object.entries(inst.query).forEach(([k, v]) => {
          if (v != null) qs.set(k, String(v));
        });
        const q = qs.toString();
        if (q) url += (url.includes("?") ? "&" : "?") + q;
      }
      const headers = { Accept: "application/json" };
      const hasBody = inst.body && method !== "GET" && method !== "DELETE";
      if (hasBody) headers["Content-Type"] = "application/json";
      return fetch(url, {
        method,
        headers,
        body: hasBody ? JSON.stringify(inst.body) : undefined,
        credentials: "same-origin",
      })
        .then(async (resp) => {
          let data = null;
          try {
            data = await resp.json();
          } catch (_e) {
            data = { text: await resp.text() };
          }
          if (!resp.ok) {
            const msg =
              (data && (data.message || data.error)) ||
              "API request failed (" + resp.status + ")";
            toast(msg, "error");
            reportFailure(inst, "api_error");
            return false;
          }
          if (data && data._error) {
            toast(data._error.message || "API error", "error");
            reportFailure(inst, "api_error");
            return false;
          }
          if (inst.responseMap && data) {
            const mapped = {};
            Object.entries(inst.responseMap).forEach(([outKey, srcKey]) => {
              mapped[outKey] = data[srcKey];
            });
            inst._apiResult = mapped;
          } else {
            inst._apiResult = data;
          }
          if (inst.render && inst._apiResult) {
            const renderInst = {
              type: inst.render,
              data: inst._apiResult[inst.renderField] || inst._apiResult,
            };
            const fn = demoHandlers[inst.render];
            if (fn) fn(renderInst);
          }
          return true;
        })
        .catch((err) => {
          toast("Could not reach API: " + (err.message || "network error"), "error");
          reportFailure(inst, "timeout");
          return false;
        });
    }
    if (type === "toast") {
      toast(inst.message || "Done.", inst.level);
      return true;
    }
    if (type === "show_message") {
      return true;
    }
    if (type === "needs_login") {
      if (inst.loginUrl) window.location.href = inst.loginUrl;
      return true;
    }
    return null;
  }

  const demoHandlers = {
    render_products(inst) {
      const grid = $(inst.target || DEFAULTS.productGrid);
      if (!grid) return;
      const list = inst.data || [];
      grid.innerHTML = "";
      if (!list.length) {
        grid.innerHTML = '<p class="nina-empty">No products matched.</p>';
      } else {
        for (const p of list) {
          const card = document.createElement("article");
          card.className = "product-card";
          card.dataset.productId = p.id;
          card.innerHTML =
            '<div class="product-card__name"></div>' +
            '<div class="product-card__meta"></div>' +
            '<div class="product-card__price"></div>' +
            '<div class="product-card__tags"></div>';
          card.querySelector(".product-card__name").textContent = p.name;
          card.querySelector(".product-card__meta").textContent = p.category;
          card.querySelector(".product-card__price").textContent =
            "₹" + Number(p.price).toLocaleString("en-IN");
          const tags = card.querySelector(".product-card__tags");
          (p.tags || []).forEach((t) => {
            const s = document.createElement("span");
            s.className = "tag";
            s.textContent = t;
            tags.appendChild(s);
          });
          grid.appendChild(card);
        }
      }
      flash(grid);
    },
    show_product_detail(inst) {
      const panel = $(DEFAULTS.detailPanel);
      const p = inst.data;
      if (!panel || !p) return;
      panel.hidden = false;
      panel.innerHTML =
        '<button type="button" class="detail-close" aria-label="Close">×</button>' +
        "<h2></h2><p class='detail-meta'></p><p class='detail-price'></p>";
      panel.querySelector("h2").textContent = p.name;
      panel.querySelector(".detail-meta").textContent = p.category || "";
      panel.querySelector(".detail-price").textContent =
        "₹" + Number(p.price).toLocaleString("en-IN");
      panel.querySelector(".detail-close").onclick = () => {
        panel.hidden = true;
      };
    },
    update_cart(inst) {
      const cart = inst.data?.cart || inst.data;
      if (!cart) return;
      const badge = $(DEFAULTS.cartBadge);
      const drawer = $(DEFAULTS.cartDrawer);
      const itemsEl = $(DEFAULTS.cartItems);
      const totalEl = $(DEFAULTS.cartTotal);
      const count = (cart.items || []).reduce((s, i) => s + i.qty, 0);
      if (badge) badge.textContent = String(count);
      if (drawer) drawer.classList.add("open");
      if (itemsEl) {
        itemsEl.innerHTML = "";
        for (const it of cart.items || []) {
          const row = document.createElement("div");
          row.className = "cart-row";
          row.innerHTML = "<span></span><span></span>";
          row.children[0].textContent = it.name + " × " + it.qty;
          row.children[1].textContent =
            "₹" + Number(it.subtotal).toLocaleString("en-IN");
          itemsEl.appendChild(row);
        }
      }
      if (totalEl) totalEl.textContent =
        "₹" + Number(cart.total || 0).toLocaleString("en-IN");
    },
    open_cart() {
      const drawer = $(DEFAULTS.cartDrawer);
      if (drawer) drawer.classList.add("open");
    },
    close_cart() {
      const drawer = $(DEFAULTS.cartDrawer);
      if (drawer) drawer.classList.remove("open");
    },
    show_order(inst) {
      const data = inst.data || {};
      toast("Order " + (data.orderId || data.id) + " placed", "success");
    },
    highlight(inst) {
      const el = $(inst.selector);
      if (el) flash(el);
    },
  };

  async function runOne(instruction) {
    if (!instruction || !instruction.type) return;
    if (instruction.type === "run_action" && state.contract && global.NinaContract) {
      const action = (state.contract.actions || []).find(
        (a) => a.id === instruction.actionId
      );
      if (action) {
        const steps = global.NinaContract.expandSteps(
          state.contract,
          action,
          instruction.params
        );
        await run(steps);
      }
      return;
    }
    const domResult = runDomOp(instruction);
    if (domResult !== null) {
      if (domResult instanceof Promise) await domResult;
      return;
    }
    const fn = demoHandlers[instruction.type];
    if (fn) fn(instruction);
    else console.warn("[NINA] Unknown instruction:", instruction.type);
  }

  async function run(instructions) {
    if (!instructions) return;
    const list = Array.isArray(instructions) ? instructions : [instructions];
    for (const inst of list) await runOne(inst);
  }

  function setContract(contract) {
    state.contract = contract;
  }

  function configure(opts) {
    if (opts.reportUrl) state.reportUrl = opts.reportUrl;
    if (opts.onFailure) state.onFailure = opts.onFailure;
    if (opts.contract) state.contract = opts.contract;
  }

  global.NinaExecutor = {
    run,
    runOne,
    setContract,
    configure,
    handlers: demoHandlers,
    config: DEFAULTS,
  };
})(typeof window !== "undefined" ? window : globalThis);
