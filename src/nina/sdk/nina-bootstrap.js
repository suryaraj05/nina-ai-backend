/* nina-bootstrap.js — NINA conversational commerce widget v4.1
   Mobile bottom sheet · desktop dock (Gemini-style) or floating · suggestion chips.
   Session survives same-origin navigations (SPA pushState + sessionStorage).
*/
(function (W, D) {
  'use strict';

  var script = D.currentScript || D.querySelector('script[data-api-key]');
  if (!script) return;

  var CFG = {
    api:    (script.getAttribute('data-api') || '').replace(/\/$/, ''),
    key:    script.getAttribute('data-api-key') || '',
    siteId: script.getAttribute('data-site-id') || '',
    panel:  script.getAttribute('data-panel') || 'right',
    greeting: script.getAttribute('data-greeting') || null,
  };

  var CHAT_NAME = 'NINA';

  if (!CFG.api || !CFG.key) {
    console.warn('[NINA] data-api and data-api-key are required.');
    return;
  }

  var MOBILE_BP = 768;
  var DOCK_WIDTH = 400;
  var SHEET_HALF = 0.52;
  var SHEET_FULL = 0.92;
  var SHEET_CLOSE = 0.18;
  var CHIP_SHOW = 3;

  var WA = {
    header: '#075e54',
    bg: '#efeae2',
    outgoing: '#d9fdd3',
    incoming: '#ffffff',
    foot: '#f0f2f5',
    send: '#00a884',
    fab: '#25d366',
    tickRead: '#53bdeb',
    time: 'rgba(11,20,26,.45)',
    verified: '#0B7A6B',
  };

  var SID_KEY = 'nina_sid_' + CFG.siteId;
  var LAYOUT_KEY = 'nina_layout_' + CFG.siteId;
  var SESSION_KEY = 'nina_session_' + CFG.siteId;

  var _sessionLog = [];

  var CHIP_POOLS = {
    home: [
      'Show me hoodies under ₹2000',
      'What\'s new this week?',
      'Show bestsellers',
      'Gift ideas under ₹1500',
      'Browse all products',
      'Return & exchange policy',
    ],
    pdp: [
      'Is this in stock?',
      'Help me choose a size',
      'Show similar products',
      'Add this to my cart',
      'Do you have this in black?',
      'What\'s the return policy?',
    ],
    cart: [
      'What\'s in my cart?',
      'Apply a discount code',
      'Continue to checkout',
      'Remove an item',
      'Show me more hoodies',
      'Shipping & delivery info',
    ],
    category_hoodies: [
      'Hoodies under ₹2000',
      'Oversized hoodies',
      'Black hoodies',
      'New hoodie arrivals',
      'Best rated hoodies',
      'Compare hoodie prices',
    ],
    category_tees: [
      'T-shirts under ₹999',
      'Oversized tees',
      'Graphic tees',
      'Plain cotton tees',
      'New t-shirt drops',
      'Bundle deals on tees',
    ],
    global: [
      'Track my order',
      'What\'s in my cart?',
      'Contact support',
      'Shipping times',
      'Size guide',
      'Show new arrivals',
    ],
  };

  var _busy = false;
  var _lastUserBubble = null;
  var _lastProducts = [];
  var _lastCartCount = null;
  var _currentChipTexts = [];
  var _panelOpen = false;
  var _openPanelFn = null;

  function getSid() {
    var sid = localStorage.getItem(SID_KEY);
    if (!sid) {
      var arr = new Uint8Array(16);
      (W.crypto || W.msCrypto).getRandomValues(arr);
      sid = 'sid_' + Array.from(arr, function (b) { return b.toString(16).padStart(2, '0'); }).join('');
      try { localStorage.setItem(SID_KEY, sid); } catch (_) {}
    }
    return sid;
  }

  function isMobileSheet() {
    return W.innerWidth <= MOBILE_BP;
  }

  function viewportH() {
    return (W.visualViewport && W.visualViewport.height) || W.innerHeight;
  }

  function getLayoutMode() {
    try {
      var saved = localStorage.getItem(LAYOUT_KEY);
      if (saved === 'float' || saved === 'dock') return saved;
    } catch (_) {}
    return 'dock';
  }

  function setLayoutMode(mode) {
    try { localStorage.setItem(LAYOUT_KEY, mode); } catch (_) {}
  }

  function loadSession() {
    try {
      var raw = sessionStorage.getItem(SESSION_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (_) {
      return null;
    }
  }

  function saveSession() {
    try {
      sessionStorage.setItem(SESSION_KEY, JSON.stringify({
        messages: _sessionLog,
        products: _lastProducts,
        panelOpen: _panelOpen,
      }));
    } catch (_) {}
  }

  function navigateTo(url) {
    saveSession();
    try {
      var target = new URL(url, W.location.href);
      if (target.origin === W.location.origin) {
        var dest = target.pathname + target.search + target.hash;
        var current = W.location.pathname + W.location.search + W.location.hash;
        if (dest !== current) {
          W.history.pushState({ ninaNav: 1 }, '', dest);
          W.dispatchEvent(new PopStateEvent('popstate', { state: { ninaNav: 1 } }));
        }
        renderChips();
        return;
      }
    } catch (_) {}
    W.location.assign(url);
  }

  function pageKind() {
    var path = (W.location.pathname || '').toLowerCase();
    if (path.indexOf('cart') >= 0 || path.indexOf('checkout') >= 0) return 'cart';
    if (path.indexOf('/product') >= 0 || path.indexOf('/p/') >= 0 || path.indexOf('/item/') >= 0) return 'pdp';
    if (path.indexOf('hoodie') >= 0) return 'category_hoodies';
    if (path.indexOf('t-shirt') >= 0 || path.indexOf('tshirt') >= 0 || path.indexOf('/tee') >= 0) return 'category_tees';
    return 'home';
  }

  function shufflePick(arr, n) {
    var copy = arr.slice();
    for (var i = copy.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var tmp = copy[i];
      copy[i] = copy[j];
      copy[j] = tmp;
    }
    return copy.slice(0, n);
  }

  function buildChipPool(extra) {
    var kind = pageKind();
    var pool = (CHIP_POOLS[kind] || []).concat(CHIP_POOLS.global);
    if (extra) pool = [extra].concat(pool);
    var seen = {};
    var uniq = [];
    pool.forEach(function (c) {
      if (!seen[c]) { seen[c] = true; uniq.push(c); }
    });
    return uniq;
  }

  function honestRetryChip(turn, userQuery) {
    var result = turn.actionResult || {};
    var action = (turn.actionCalled || '').toLowerCase();
    var isSearch = action.indexOf('search') >= 0 || action.indexOf('browse') >= 0 || action.indexOf('list') >= 0;
    var zero = (!turn.products || !turn.products.length) && (result.count === 0 || result.count === undefined);
    if (!isSearch || !zero) return null;
    if (result.grounded === false) return null;
    var q = String(result.query || userQuery || '').toLowerCase();
    if (q.indexOf('hoodie') >= 0) return 'Show all hoodies';
    if (q.indexOf('tee') >= 0 || q.indexOf('t-shirt') >= 0) return 'Show all t-shirts';
    var kind = pageKind();
    if (kind === 'category_hoodies') return 'Show all hoodies';
    if (kind === 'category_tees') return 'Show all t-shirts';
    return 'Browse all products';
  }

  function scrapeHostCartCount() {
    var selectors = [
      '[data-cart-count]',
      '[data-testid="cart-count"]',
      '.cart-count',
      '.cart-badge',
      '#cart-count',
      'a[href*="cart"] .badge',
      'a[href*="cart"] span[class*="count"]',
    ];
    for (var i = 0; i < selectors.length; i++) {
      var el = D.querySelector(selectors[i]);
      if (!el) continue;
      var n = parseInt(String(el.textContent || el.getAttribute('data-cart-count') || '').replace(/\D/g, ''), 10);
      if (!isNaN(n)) return n;
    }
    return null;
  }

  var side = CFG.panel === 'left' ? 'left' : 'right';
  var dockBodyClass = side === 'left' ? 'nina-dock-left' : 'nina-dock-right';
  var dockEdge = side === 'left' ? 'left' : 'right';

  var CSS = '\
  body.nina-sheet-active{overflow:hidden;touch-action:none;}\
  body.nina-dock-right{margin-right:0;transition:margin-right .28s ease;}\
  body.nina-dock-left{margin-left:0;transition:margin-left .28s ease;}\
  body.nina-dock-right.nina-dock-open{margin-right:' + DOCK_WIDTH + 'px;}\
  body.nina-dock-left.nina-dock-open{margin-left:' + DOCK_WIDTH + 'px;}\
  #nina-backdrop{position:fixed;inset:0;z-index:2147483645;background:rgba(0,0,0,.38);\
    opacity:0;pointer-events:none;transition:opacity .25s ease;}\
  #nina-backdrop.nina-visible{opacity:1;pointer-events:auto;}\
  #nina-fab{position:fixed;bottom:max(20px,env(safe-area-inset-bottom));' + side + ':20px;\
    z-index:2147483646;width:56px;height:56px;border-radius:50%;background:' + WA.fab + ';\
    border:none;cursor:pointer;box-shadow:0 4px 18px rgba(0,0,0,.28);\
    display:flex;align-items:center;justify-content:center;\
    transition:transform .15s,opacity .2s,box-shadow .15s;}\
  #nina-fab:hover{transform:scale(1.06);}\
  #nina-fab.nina-hidden{opacity:0;pointer-events:none;transform:scale(.85);}\
  #nina-fab svg{width:28px;height:28px;fill:#fff;pointer-events:none;}\
  #nina-badge{position:absolute;top:-2px;right:-2px;width:16px;height:16px;\
    background:#ff4d4d;border-radius:50%;border:2px solid #fff;display:none;}\
  #nina-badge.nina-pulse{display:block;animation:nina-badge-pulse 1s ease 3;}\
  @keyframes nina-badge-pulse{0%,100%{transform:scale(1);}50%{transform:scale(1.35);}}\
  #nina-panel{position:fixed;z-index:2147483647;background:' + WA.bg + ';\
    display:flex;flex-direction:column;overflow:hidden;font-family:"Segoe UI",system-ui,-apple-system,sans-serif;}\
  #nina-panel.nina-mode-sheet{left:0;right:0;bottom:0;width:100%;max-width:100%;\
    border-radius:14px 14px 0 0;transform:translateY(100%);\
    box-shadow:0 -2px 24px rgba(0,0,0,.18);\
    transition:transform .32s cubic-bezier(.32,.72,.24,1),height .28s ease;will-change:transform,height;}\
  #nina-panel.nina-mode-sheet.nina-open{transform:translateY(0);}\
  #nina-panel.nina-mode-sheet.nina-dragging{transition:none;}\
  #nina-panel.nina-mode-desktop{bottom:88px;' + side + ':20px;width:390px;\
    max-width:calc(100vw - 32px);height:min(580px,calc(100vh - 110px));border-radius:14px;\
    box-shadow:0 12px 48px rgba(0,0,0,.22);transform:translateY(16px) scale(.96);opacity:0;\
    pointer-events:none;transition:transform .22s cubic-bezier(.34,1.26,.64,1),opacity .18s ease;}\
  #nina-panel.nina-mode-desktop.nina-open{transform:translateY(0) scale(1);opacity:1;pointer-events:auto;}\
  #nina-panel.nina-mode-dock{top:0;bottom:0;' + dockEdge + ':0;width:' + DOCK_WIDTH + 'px;max-width:100vw;\
    height:100vh;height:100dvh;border-radius:0;box-shadow:-4px 0 24px rgba(0,0,0,.12);\
    transform:translateX(' + (side === 'left' ? '-' : '') + '100%);opacity:1;pointer-events:none;\
    transition:transform .28s cubic-bezier(.32,.72,.24,1);}\
  #nina-panel.nina-mode-dock.nina-open{transform:translateX(0);pointer-events:auto;}\
  #nina-grabber{flex-shrink:0;padding:8px 0 6px;display:flex;justify-content:center;cursor:grab;\
    touch-action:none;user-select:none;background:' + WA.header + ';}\
  #nina-grabber span{display:block;width:36px;height:4px;border-radius:99px;background:rgba(255,255,255,.35);}\
  #nina-grabber:active{cursor:grabbing;}\
  .nina-mode-desktop #nina-grabber,.nina-mode-dock #nina-grabber{display:none;}\
  #nina-head{padding:10px 12px 10px 8px;background:' + WA.header + ';color:#fff;\
    display:flex;align-items:center;justify-content:space-between;flex-shrink:0;\
    box-shadow:0 1px 3px rgba(0,0,0,.14);}\
  .nina-head-left{display:flex;align-items:center;gap:10px;min-width:0;flex:1;}\
  .nina-avatar{position:relative;width:40px;height:40px;border-radius:50%;background:rgba(255,255,255,.18);\
    color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:1.05rem;flex-shrink:0;}\
  #nina-cart-dot{position:absolute;top:-1px;right:-1px;width:11px;height:11px;background:#ff6b6b;\
    border-radius:50%;border:2px solid ' + WA.header + ';display:none;}\
  #nina-cart-dot.nina-pulse{display:block;animation:nina-cart-dot-pulse .9s ease 4;}\
  @keyframes nina-cart-dot-pulse{0%,100%{transform:scale(1);opacity:1;}50%{transform:scale(1.45);opacity:.85;}}\
  .nina-head-name{font-weight:500;font-size:1rem;color:#fff;line-height:1.2;}\
  .nina-head-status{font-size:.75rem;color:rgba(255,255,255,.78);margin-top:1px;}\
  .nina-head-actions{display:flex;align-items:center;gap:2px;flex-shrink:0;}\
  .nina-icon-btn{background:none;border:none;cursor:pointer;color:#fff;opacity:.92;\
    width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;\
    font-size:18px;line-height:1;transition:background .12s;}\
  .nina-icon-btn:hover{background:rgba(255,255,255,.12);opacity:1;}\
  #nina-msgs{flex:1;overflow-y:auto;padding:10px 12px 6px;\
    display:flex;flex-direction:column;gap:3px;background-color:' + WA.bg + ';\
    background-image:url("data:image/svg+xml,%3Csvg width=\'80\' height=\'80\' xmlns=\'http://www.w3.org/2000/svg\'%3E%3Cg fill=\'%23d9d0c6\' fill-opacity=\'.22\'%3E%3Ccircle cx=\'12\' cy=\'12\' r=\'2\'/%3E%3Ccircle cx=\'52\' cy=\'42\' r=\'1.5\'/%3E%3Ccircle cx=\'32\' cy=\'62\' r=\'1\'/%3E%3C/g%3E%3C/svg%3E");\
    scroll-behavior:smooth;-webkit-overflow-scrolling:touch;}\
  #nina-rail{flex-shrink:0;display:none;border-top:1px solid rgba(0,0,0,.06);background:' + WA.foot + ';\
    padding:8px 10px 4px;}\
  #nina-rail.nina-visible{display:block;}\
  #nina-rail-label{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;\
    color:#667781;margin-bottom:6px;display:flex;align-items:center;justify-content:space-between;}\
  #nina-rail .nina-products{padding:0;}\
  .nina-row{display:flex;align-items:flex-end;margin-bottom:2px;}\
  .nina-row.nina-user{justify-content:flex-end;}\
  .nina-row.nina-bot{justify-content:flex-start;}\
  .nina-col{display:flex;flex-direction:column;max-width:85%;}\
  .nina-bubble{position:relative;padding:6px 8px 5px 9px;border-radius:8px;\
    font-size:.9375rem;line-height:1.35;word-break:break-word;white-space:pre-wrap;\
    box-shadow:0 1px .5px rgba(11,20,26,.13);max-width:100%;}\
  .nina-row.nina-bot .nina-bubble{background:' + WA.incoming + ';color:#111b21;border-top-left-radius:0;}\
  .nina-row.nina-user .nina-bubble{background:' + WA.outgoing + ';color:#111b21;border-top-right-radius:0;}\
  .nina-bubble-text{display:inline;padding-right:2px;}\
  .nina-bubble-foot{float:right;margin-left:12px;margin-top:4px;\
    display:inline-flex;align-items:center;gap:3px;vertical-align:bottom;\
    font-size:.6875rem;color:' + WA.time + ';line-height:1;white-space:nowrap;position:relative;top:4px;}\
  .nina-ticks{font-size:.75rem;line-height:1;}\
  .nina-tick-sent{color:rgba(11,20,26,.35);}\
  .nina-tick-delivered{color:rgba(11,20,26,.5);letter-spacing:-3px;}\
  .nina-tick-read{color:' + WA.tickRead + ';letter-spacing:-3px;}\
  .nina-row.nina-sys{justify-content:center;margin:8px 0;}\
  .nina-row.nina-sys .nina-col{max-width:90%;}\
  .nina-row.nina-sys .nina-bubble{background:rgba(255,255,255,.85);color:#54656f;\
    font-size:.75rem;text-align:center;padding:5px 12px;border-radius:8px;box-shadow:0 1px 1px rgba(0,0,0,.06);}\
  .nina-typing-bubble{display:flex;gap:4px;padding:10px 14px 12px;min-width:52px;}\
  .nina-typing-bubble span{width:7px;height:7px;border-radius:50%;background:#90a4ae;animation:nina-bounce .9s infinite;}\
  .nina-typing-bubble span:nth-child(2){animation-delay:.15s;}\
  .nina-typing-bubble span:nth-child(3){animation-delay:.3s;}\
  @keyframes nina-bounce{0%,60%,100%{transform:translateY(0);}30%{transform:translateY(-5px);}}\
  .nina-confirm-row{display:flex;gap:8px;padding:4px 0 6px;max-width:85%;}\
  .nina-confirm-row button{flex:1;padding:10px;border-radius:8px;border:none;cursor:pointer;\
    font-size:.875rem;font-weight:500;box-shadow:0 1px .5px rgba(11,20,26,.13);}\
  .nina-confirm-row .nina-yes{background:' + WA.send + ';color:#fff;}\
  .nina-confirm-row .nina-no{background:#fff;color:#111b21;}\
  #nina-foot{padding:6px 8px calc(6px + env(safe-area-inset-bottom));background:' + WA.foot + ';\
    flex-shrink:0;display:flex;flex-direction:column;gap:6px;}\
  #nina-chips{display:flex;flex-wrap:wrap;gap:6px;max-height:108px;overflow-y:auto;}\
  .nina-chip{border:1px solid rgba(11,20,26,.1);background:#fff;color:#111b21;border-radius:999px;\
    padding:7px 12px;font-size:.75rem;line-height:1.25;cursor:pointer;text-align:left;\
    box-shadow:0 1px 2px rgba(11,20,26,.06);transition:background .12s,transform .1s;max-width:100%;}\
  .nina-chip:hover{background:#f5f6f6;}\
  .nina-chip:active{transform:scale(.98);}\
  .nina-chip:disabled{opacity:.45;cursor:default;}\
  .nina-chip.nina-chip-honest{border-color:' + WA.verified + ';color:' + WA.verified + ';font-weight:600;}\
  #nina-foot-row{display:flex;gap:6px;align-items:flex-end;}\
  .nina-input-wrap{flex:1;display:flex;align-items:flex-end;gap:4px;background:#fff;border-radius:24px;\
    padding:6px 10px 6px 6px;box-shadow:0 1px 2px rgba(11,20,26,.08);}\
  .nina-emoji-btn{background:none;border:none;cursor:pointer;font-size:1.25rem;line-height:1;padding:6px 4px;opacity:.55;}\
  #nina-input{flex:1;border:none;outline:none;font-size:.9375rem;font-family:inherit;resize:none;\
    max-height:100px;min-height:22px;background:transparent;line-height:1.35;color:#111b21;}\
  #nina-input::placeholder{color:#8696a0;}\
  #nina-send{width:42px;height:42px;flex-shrink:0;border-radius:50%;background:' + WA.send + ';border:none;\
    cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .15s,transform .1s;}\
  #nina-send.nina-mic{background:transparent;box-shadow:none;}\
  #nina-send:disabled{opacity:.4;cursor:default;}\
  #nina-brand{text-align:center;padding:4px 8px 6px;font-size:.65rem;color:#8696a0;background:' + WA.foot + ';flex-shrink:0;}\
  #nina-brand a{color:#00a884;text-decoration:none;}\
  .nina-products{display:flex;gap:8px;overflow-x:auto;padding:4px 0 6px;scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;max-width:100%;}\
  .nina-card{flex:0 0 150px;scroll-snap-align:start;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px .5px rgba(11,20,26,.13);}\
  .nina-card-img{width:100%;height:110px;object-fit:cover;display:block;background:#e9edef;border:none;}\
  .nina-card-body{padding:8px;display:flex;flex-direction:column;gap:4px;}\
  .nina-verified{display:inline-flex;align-items:center;gap:3px;font-size:.62rem;font-weight:600;\
    color:' + WA.verified + ';letter-spacing:.01em;}\
  .nina-card-title{font-size:.8rem;line-height:1.25;color:#111b21;display:-webkit-box;-webkit-line-clamp:2;\
    -webkit-box-orient:vertical;overflow:hidden;}\
  .nina-card-price{font-size:.82rem;font-weight:600;color:#111b21;}\
  .nina-card-meta{font-size:.68rem;color:#667781;}\
  .nina-card-btn{border:none;border-radius:6px;cursor:pointer;background:' + WA.send + ';color:#fff;\
    font-size:.75rem;font-weight:500;padding:8px;margin-top:4px;}\
  .nina-products.nina-single .nina-card{flex:1 1 auto;max-width:100%;display:flex;flex-direction:row;}\
  .nina-products.nina-single .nina-card-img{width:90px;height:90px;flex-shrink:0;}\
  #nina-rail .nina-card{flex:0 0 130px;}\
  #nina-rail .nina-card-img{height:80px;}\
  @media(min-width:' + (MOBILE_BP + 1) + 'px){\
    #nina-backdrop.nina-visible{opacity:0;pointer-events:none;}\
    .nina-mode-desktop #nina-head,.nina-mode-dock #nina-head{border-radius:0;}\
  }\
  @media(max-width:' + MOBILE_BP + 'px){body.nina-dock-right.nina-dock-open,body.nina-dock-left.nina-dock-open{margin:0;}}';

  function injectCSS() {
    var el = D.createElement('style');
    el.textContent = CSS;
    D.head.appendChild(el);
  }

  function createEl(tag, attrs, html) {
    var el = D.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    if (html !== undefined) el.innerHTML = html;
    return el;
  }

  function formatTime(d) {
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }

  function bubbleFoot(role, time) {
    var foot = createEl('span', { class: 'nina-bubble-foot' });
    foot.appendChild(D.createTextNode(time));
    var ticksEl = null;
    if (role === 'user') {
      ticksEl = createEl('span', { class: 'nina-ticks nina-tick-sent', 'aria-hidden': 'true' });
      ticksEl.textContent = '\u2713';
      foot.appendChild(ticksEl);
    }
    return { foot: foot, ticks: ticksEl };
  }

  function markUserDelivered() {
    if (!_lastUserBubble || !_lastUserBubble.ticks) return;
    _lastUserBubble.ticks.className = 'nina-ticks nina-tick-delivered';
    _lastUserBubble.ticks.textContent = '\u2713\u2713';
  }

  function markUserRead() {
    if (!_lastUserBubble || !_lastUserBubble.ticks) return;
    _lastUserBubble.ticks.className = 'nina-ticks nina-tick-read';
    _lastUserBubble.ticks.textContent = '\u2713\u2713';
    _lastUserBubble = null;
  }

  function pulseCart() {
    var dot = D.getElementById('nina-cart-dot');
    var badge = D.getElementById('nina-badge');
    if (dot) {
      dot.classList.remove('nina-pulse');
      void dot.offsetWidth;
      dot.classList.add('nina-pulse');
    }
    if (badge) {
      badge.style.display = 'block';
      badge.classList.remove('nina-pulse');
      void badge.offsetWidth;
      badge.classList.add('nina-pulse');
    }
    var status = D.querySelector('.nina-head-status');
    if (status) {
      var count = scrapeHostCartCount();
      if (count != null) status.textContent = 'cart updated · ' + count + ' item' + (count === 1 ? '' : 's');
    }
    setTimeout(function () {
      if (dot) dot.classList.remove('nina-pulse');
      if (status) status.textContent = 'online';
    }, 4000);
  }

  function maybeCartPulse(turn) {
    var action = (turn.actionCalled || '').toLowerCase();
    var cartish = action.indexOf('cart') >= 0 || action.indexOf('add') >= 0 || action.indexOf('checkout') >= 0;
    if (!cartish) {
      (turn.instructions || []).forEach(function (ins) {
        var url = String(ins.url || '').toLowerCase();
        if (url.indexOf('cart') >= 0 || url.indexOf('checkout') >= 0) cartish = true;
      });
    }
    if (!cartish) return;
    setTimeout(function () {
      var count = scrapeHostCartCount();
      if (count != null && _lastCartCount != null && count > _lastCartCount) pulseCart();
      else if (count != null && _lastCartCount == null) pulseCart();
      else if (cartish) pulseCart();
      if (count != null) _lastCartCount = count;
    }, 600);
  }

  var _msgs;
  function msgBox() { return _msgs || (_msgs = D.getElementById('nina-msgs')); }

  function addRow(role, text, opts) {
    opts = opts || {};
    var row = createEl('div', { class: 'nina-row nina-' + role });
    var col = createEl('div', { class: 'nina-col' });
    var bbl = createEl('div', { class: 'nina-bubble' });
    var txt = createEl('span', { class: 'nina-bubble-text' });
    txt.textContent = text;
    bbl.appendChild(txt);
    var ticksEl = null;
    if (role === 'user' || role === 'bot') {
      var footParts = bubbleFoot(role, formatTime(new Date()));
      bbl.appendChild(footParts.foot);
      ticksEl = footParts.ticks;
      if (opts.restored && role === 'user' && ticksEl) {
        ticksEl.className = 'nina-ticks nina-tick-read';
        ticksEl.textContent = '\u2713\u2713';
      }
    }
    col.appendChild(bbl);
    row.appendChild(col);
    msgBox().appendChild(row);
    msgBox().scrollTop = 99999;
    if (role === 'user' && !opts.restored) _lastUserBubble = { row: row, ticks: ticksEl };
    if (!opts.skipLog && (role === 'user' || role === 'bot' || role === 'sys')) {
      _sessionLog.push({ role: role, text: text });
      saveSession();
    }
    return bbl;
  }

  function addTyping() {
    var row = createEl('div', { class: 'nina-row nina-bot' });
    var col = createEl('div', { class: 'nina-col' });
    col.innerHTML = '<div class="nina-bubble"><div class="nina-typing-bubble"><span></span><span></span><span></span></div></div>';
    row.appendChild(col);
    msgBox().appendChild(row);
    msgBox().scrollTop = 99999;
    return row;
  }

  function addConfirm(onYes, onNo) {
    var row = createEl('div', { class: 'nina-confirm-row' });
    row.innerHTML = '<button class="nina-yes">Confirm</button><button class="nina-no">Cancel</button>';
    row.querySelector('.nina-yes').onclick = function () { row.remove(); onYes(); };
    row.querySelector('.nina-no').onclick  = function () { row.remove(); onNo && onNo(); addRow('sys', 'Action cancelled.'); };
    msgBox().appendChild(row);
    msgBox().scrollTop = 99999;
  }

  function buildProductCard(p, opts) {
    opts = opts || {};
    var card = createEl('div', { class: 'nina-card' });
    var img;
    if (p.image) {
      img = createEl('img', { class: 'nina-card-img', alt: p.title || '', loading: 'lazy' });
      img.src = p.image;
    } else {
      img = createEl('div', { class: 'nina-card-img' });
      if (p.swatch) img.style.background = p.swatch;
    }
    var body = createEl('div', { class: 'nina-card-body' });
    if (opts.verified !== false) {
      var badge = createEl('span', { class: 'nina-verified' });
      badge.textContent = '\u2713 Catalog verified';
      body.appendChild(badge);
    }
    var title = createEl('div', { class: 'nina-card-title' });
    title.textContent = p.title || '';
    body.appendChild(title);
    if (p.meta) {
      var meta = createEl('div', { class: 'nina-card-meta' });
      meta.textContent = p.meta;
      body.appendChild(meta);
    }
    var price = createEl('div', { class: 'nina-card-price' });
    if (p.price != null && p.price !== '') {
      var cur = p.currency || '';
      var sym = cur === 'INR' ? '₹' : (cur ? cur + ' ' : '');
      price.textContent = sym + p.price;
    }
    body.appendChild(price);
    var btn = createEl('button', { class: 'nina-card-btn', type: 'button' });
    btn.textContent = p.cta || 'Add to Cart';
    btn.addEventListener('click', function () {
      if (_busy) return;
      sendUserMessage('Add ' + (p.title || 'this') + ' to cart', 'add ' + (p.title || 'this item') + ' to my cart');
    });
    body.appendChild(btn);
    card.appendChild(img);
    card.appendChild(body);
    return card;
  }

  function buildProductsWrap(products, opts) {
    var single = products.length === 1;
    var wrap = createEl('div', { class: 'nina-products' + (single ? ' nina-single' : '') });
    products.forEach(function (p) {
      wrap.appendChild(buildProductCard(p, opts));
    });
    return wrap;
  }

  function isDockedOpen() {
    var panel = D.getElementById('nina-panel');
    return panel && panel.classList.contains('nina-mode-dock') && panel.classList.contains('nina-open');
  }

  function updateRail(products, verified) {
    var rail = D.getElementById('nina-rail');
    var inner = D.getElementById('nina-rail-inner');
    if (!rail || !inner) return;
    if (!products || !products.length || !isDockedOpen()) {
      rail.classList.remove('nina-visible');
      inner.innerHTML = '';
      return;
    }
    _lastProducts = products;
    inner.innerHTML = '';
    inner.appendChild(buildProductsWrap(products, { verified: verified }));
    rail.classList.add('nina-visible');
  }

  function renderProducts(products, verified, opts) {
    opts = opts || {};
    if (!products || !products.length) return;
    _lastProducts = products;
    updateRail(products, verified);
    var wrap = buildProductsWrap(products, { verified: verified });
    var row = createEl('div', { class: 'nina-row nina-bot' });
    var col = createEl('div', { class: 'nina-col' });
    col.appendChild(wrap);
    row.appendChild(col);
    msgBox().appendChild(row);
    msgBox().scrollTop = 99999;
    if (!opts.skipLog) {
      _sessionLog.push({ role: 'products', products: products, verified: verified !== false });
      saveSession();
    }
  }

  function renderChips(honestExtra) {
    var box = D.getElementById('nina-chips');
    if (!box) return;
    var pool = buildChipPool(honestExtra);
    var picks = shufflePick(pool, CHIP_SHOW);
    _currentChipTexts = picks;
    box.innerHTML = '';
    picks.forEach(function (text) {
      var chip = createEl('button', {
        type: 'button',
        class: 'nina-chip' + (honestExtra && text === honestExtra ? ' nina-chip-honest' : ''),
      });
      chip.textContent = text;
      chip.disabled = _busy;
      chip.addEventListener('click', function () {
        sendUserMessage(text, text);
      });
      box.appendChild(chip);
    });
  }

  function setChipsDisabled(disabled) {
    var box = D.getElementById('nina-chips');
    if (!box) return;
    box.querySelectorAll('.nina-chip').forEach(function (c) { c.disabled = disabled; });
  }

  function sendUserMessage(display, apiText) {
    if (_busy) return;
    addRow('user', display);
    chat(apiText || display);
  }

  function execInstructions(instructions) {
    (instructions || []).forEach(function (ins) {
      var t = ins.type;
      if (t === 'navigate' && ins.url) {
        navigateTo(ins.url);
      } else if ((t === 'fetch' || t === 'api_call') && ins.url) {
        var targetUrl;
        try { targetUrl = new URL(ins.url, W.location.href); } catch (_) { return; }
        if (targetUrl.origin !== W.location.origin) return;
        fetch(targetUrl.href, {
          method: ins.method || 'GET',
          headers: Object.assign({ 'Content-Type': 'application/json' }, ins.headers || {}),
          body: ins.body != null ? JSON.stringify(ins.body) : undefined,
          credentials: 'include',
        }).then(function (r) { return r.json().catch(function () { return null; }); })
          .then(function (data) {
            if (data && (data.cart || (data.items && data.items.length))) pulseCart();
          }).catch(function () {});
      } else if (t === 'dom' && ins.selector) {
        var el = D.querySelector(ins.selector);
        if (el && ins.text != null) el.textContent = ins.text;
      } else if (t === 'reload') {
        W.location.reload();
      }
    });
  }

  var SEND_SVG = '<svg viewBox="0 0 24 24"><path d="M1.1 21.9 23 12 1.1 2.1 1 9l13 3-13 3z"/></svg>';
  var MIC_SVG = '<svg viewBox="0 0 24 24"><path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5-3c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-2.08c3.39-.49 6-3.39 6-6.92h-2z"/></svg>';

  function setSendMode(hasText) {
    var send = D.getElementById('nina-send');
    if (!send) return;
    if (hasText) {
      send.classList.remove('nina-mic');
      send.innerHTML = SEND_SVG;
      send.setAttribute('aria-label', 'Send');
      send.disabled = _busy;
    } else {
      send.classList.add('nina-mic');
      send.innerHTML = MIC_SVG;
      send.setAttribute('aria-label', 'Voice message');
      send.disabled = true;
    }
  }

  function setInputEnabled(on) {
    var inp = D.getElementById('nina-input');
    if (!inp) return;
    inp.disabled = !on;
    setSendMode(inp.value.trim().length > 0);
    setChipsDisabled(!on);
  }

  function handleTurn(turn, userQuery) {
    markUserRead();
    var result = turn.actionResult || {};
    var verified = result.grounded === true || (turn.products && turn.products.length > 0);
    if (turn.products && turn.products.length) {
      renderProducts(turn.products, verified);
      if (!isMobileSheet() && !_panelOpen && _openPanelFn) _openPanelFn();
    } else {
      _lastProducts = [];
      updateRail([], verified);
    }

    var honest = honestRetryChip(turn, userQuery);
    renderChips(honest);
    maybeCartPulse(turn);
  }

  function chat(text, opts) {
    opts = opts || {};
    if (_busy) return;
    _busy = true;
    setInputEnabled(false);
    var typing = addTyping();
    var userQuery = text || '';

    fetch(CFG.api + '/v1/query', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-NINA-API-Key': CFG.key,
      },
      body: JSON.stringify({
        message: text || '',
        sessionId: getSid(),
        confirmed: !!opts.confirmed,
        replayQueued: !!opts.replayQueued,
        page_context: { url: W.location.href },
      }),
    })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      markUserDelivered();
      typing.remove();
      if (!data || !data.ok || !data.data) {
        addRow('sys', (data && data.error && data.error.message) || 'Something went wrong. Please try again.');
        renderChips();
        return;
      }
      var turn = data.data;
      var reply = (turn.naturalLanguageResponse || '').trim();
      if (reply) addRow('bot', reply);
      handleTurn(turn, userQuery);
      var intent = turn.intent;
      if (intent === 'confirmation') {
        addConfirm(function () { chat('', { confirmed: true }); }, null);
      } else if (intent === 'needs_login') {
        var loginUrl = null;
        (turn.instructions || []).forEach(function (i) { if (i.url && !loginUrl) loginUrl = i.url; });
        if (loginUrl) {
          var row = createEl('div', { class: 'nina-confirm-row' });
          row.innerHTML = '<button class="nina-yes">Sign in</button>';
          row.querySelector('.nina-yes').onclick = function () { navigateTo(loginUrl); };
          msgBox().appendChild(row);
          msgBox().scrollTop = 99999;
        }
      } else {
        execInstructions(turn.instructions);
      }
    })
    .catch(function () {
      typing.remove();
      addRow('sys', 'Connection error. Check your internet and try again.');
      renderChips();
    })
    .finally(function () {
      _busy = false;
      setInputEnabled(true);
      var inp = D.getElementById('nina-input');
      if (inp && !inp.disabled) inp.focus();
    });
  }

  function buildDOM() {
    var backdrop = createEl('div', { id: 'nina-backdrop', 'aria-hidden': 'true' });
    var fab = createEl('button', { id: 'nina-fab', 'aria-label': 'Open ' + CHAT_NAME },
      '<svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H6l-2 2V4h16v12z"/></svg>' +
      '<span id="nina-badge"></span>');

    var panel = createEl('div', {
      id: 'nina-panel',
      role: 'dialog',
      'aria-label': CHAT_NAME + ' assistant',
      'aria-modal': 'true',
    },
      '<div id="nina-grabber" aria-label="Drag to resize chat"><span></span></div>' +
      '<div id="nina-head">' +
        '<div class="nina-head-left">' +
          '<div class="nina-avatar">N<span id="nina-cart-dot" aria-hidden="true"></span></div>' +
          '<div class="nina-head-info">' +
            '<div class="nina-head-name">' + CHAT_NAME + '</div>' +
            '<div class="nina-head-status">online</div>' +
          '</div>' +
        '</div>' +
        '<div class="nina-head-actions">' +
          '<button type="button" class="nina-icon-btn" id="nina-layout" aria-label="Toggle docked panel" title="Dock / float">&#10697;</button>' +
          '<button type="button" class="nina-icon-btn" id="nina-menu" aria-label="Menu">&#8942;</button>' +
          '<button type="button" class="nina-icon-btn" id="nina-close" aria-label="Close">&#10005;</button>' +
        '</div>' +
      '</div>' +
      '<div id="nina-msgs" role="log" aria-live="polite"></div>' +
      '<div id="nina-rail"><div id="nina-rail-label"><span>Last results</span><span class="nina-verified">\u2713 Catalog verified</span></div><div id="nina-rail-inner"></div></div>' +
      '<div id="nina-foot">' +
        '<div id="nina-chips"></div>' +
        '<div id="nina-foot-row">' +
          '<div class="nina-input-wrap">' +
            '<button type="button" class="nina-emoji-btn" aria-label="Emoji" tabindex="-1">&#9786;</button>' +
            '<textarea id="nina-input" placeholder="Message" rows="1" aria-label="Message to ' + CHAT_NAME + '"></textarea>' +
          '</div>' +
          '<button id="nina-send" class="nina-mic" aria-label="Voice message" disabled>' + MIC_SVG + '</button>' +
        '</div>' +
      '</div>' +
      '<div id="nina-brand">Powered by <a href="https://nina.ai" target="_blank" rel="noopener noreferrer">NINA</a></div>');

    D.body.appendChild(backdrop);
    D.body.appendChild(fab);
    D.body.appendChild(panel);
    D.body.classList.add(dockBodyClass);
    return { backdrop: backdrop, fab: fab, panel: panel };
  }

  function boot() {
    injectCSS();
    var els = buildDOM();
    var backdrop = els.backdrop;
    var fab = els.fab;
    var panel = els.panel;
    var grabber = D.getElementById('nina-grabber');
    var layoutBtn = D.getElementById('nina-layout');
    var isOpen = false;
    var greeted = false;
    var sheetState = 'closed';
    var dragStartY = 0;
    var dragStartH = 0;
    var dragging = false;
    var dragMoved = false;
    var layoutMode = getLayoutMode();

    _lastCartCount = scrapeHostCartCount();

    function isDockMode() {
      return !isMobileSheet() && layoutMode === 'dock';
    }

    function updateLayoutButton() {
      if (!layoutBtn) return;
      if (isMobileSheet()) {
        layoutBtn.style.display = 'none';
        return;
      }
      layoutBtn.style.display = '';
      layoutBtn.innerHTML = isDockMode() ? '&#10697;' : '&#9638;';
      layoutBtn.setAttribute('aria-label', isDockMode() ? 'Switch to floating window' : 'Switch to docked panel');
      layoutBtn.title = isDockMode() ? 'Float' : 'Dock';
    }

    function applyLayoutMode() {
      var mobile = isMobileSheet();
      panel.classList.toggle('nina-mode-sheet', mobile);
      panel.classList.toggle('nina-mode-desktop', !mobile && !isDockMode());
      panel.classList.toggle('nina-mode-dock', !mobile && isDockMode());
      D.body.classList.toggle('nina-dock-open', !mobile && isDockMode() && isOpen);
      updateLayoutButton();
      if (!mobile && isOpen && isDockMode() && _lastProducts.length) {
        updateRail(_lastProducts, true);
      }
      if (!mobile && isOpen && !isDockMode()) {
        panel.style.height = '';
      }
      if (mobile && isOpen && sheetState !== 'closed') {
        setSheetRatio(sheetState === 'full' ? SHEET_FULL : SHEET_HALF, false);
      }
    }

    function setSheetRatio(ratio, animate) {
      var h = Math.round(viewportH() * ratio);
      panel.style.transition = animate ? '' : 'none';
      if (!animate) panel.classList.add('nina-dragging');
      panel.style.height = h + 'px';
      if (!animate) requestAnimationFrame(function () { panel.classList.remove('nina-dragging'); });
    }

    function snapSheet(ratio, animate) {
      sheetState = ratio >= SHEET_FULL - 0.08 ? 'full' : (ratio <= SHEET_CLOSE ? 'closed' : 'half');
      if (sheetState === 'closed') { close(true); return; }
      var target = sheetState === 'full' ? SHEET_FULL : SHEET_HALF;
      panel.classList.remove('nina-dragging');
      panel.style.transition = '';
      setSheetRatio(target, animate);
      panel.classList.toggle('nina-full', sheetState === 'full');
      panel.classList.toggle('nina-half', sheetState === 'half');
    }

    function open() {
      isOpen = true;
      _panelOpen = true;
      saveSession();
      applyLayoutMode();
      panel.classList.add('nina-open');
      backdrop.classList.add('nina-visible');
      fab.classList.add('nina-hidden');
      D.body.classList.toggle('nina-sheet-active', isMobileSheet());
      D.body.classList.toggle('nina-dock-open', isDockMode());

      if (isMobileSheet()) {
        sheetState = 'half';
        panel.classList.add('nina-half');
        setSheetRatio(SHEET_HALF, true);
      }

      D.getElementById('nina-badge').style.display = 'none';
      D.getElementById('nina-badge').classList.remove('nina-pulse');
      var inp = D.getElementById('nina-input');
      if (inp) setTimeout(function () { inp.focus(); }, 220);

      if (!greeted) {
        greeted = true;
        var msg = CFG.greeting || ('Hi! I\'m ' + CHAT_NAME + '. How can I help you today?');
        setTimeout(function () { addRow('bot', msg); renderChips(); }, 180);
      } else {
        renderChips();
      }

      if (isDockMode() && _lastProducts.length) updateRail(_lastProducts, true);
    }

    function close(fromDrag) {
      isOpen = false;
      _panelOpen = false;
      saveSession();
      sheetState = 'closed';
      panel.classList.remove('nina-open', 'nina-half', 'nina-full', 'nina-dragging');
      backdrop.classList.remove('nina-visible');
      fab.classList.remove('nina-hidden');
      D.body.classList.remove('nina-sheet-active', 'nina-dock-open');
      panel.style.height = '';
      if (!fromDrag && isMobileSheet()) panel.style.transform = 'translateY(100%)';
      var rail = D.getElementById('nina-rail');
      if (rail) rail.classList.remove('nina-visible');
    }

    function toggleLayout() {
      if (isMobileSheet()) return;
      layoutMode = isDockMode() ? 'float' : 'dock';
      setLayoutMode(layoutMode);
      applyLayoutMode();
      if (isOpen && isDockMode() && _lastProducts.length) updateRail(_lastProducts, true);
    }

    function onDragStart(clientY) {
      if (!isMobileSheet() || !isOpen) return;
      dragging = true;
      dragMoved = false;
      dragStartY = clientY;
      dragStartH = panel.getBoundingClientRect().height;
      panel.classList.add('nina-dragging');
    }

    function onDragMove(clientY) {
      if (!dragging) return;
      var dy = clientY - dragStartY;
      if (Math.abs(dy) > 6) dragMoved = true;
      var vh = viewportH();
      var next = Math.max(vh * 0.12, Math.min(vh * 0.96, dragStartH - dy));
      panel.style.height = Math.round(next) + 'px';
    }

    function onDragEnd() {
      if (!dragging) return;
      dragging = false;
      var ratio = panel.getBoundingClientRect().height / viewportH();
      snapSheet(ratio, true);
    }

    function bindDrag(el) {
      el.addEventListener('pointerdown', function (e) {
        if (!isMobileSheet() || !isOpen) return;
        el.setPointerCapture(e.pointerId);
        onDragStart(e.clientY);
      });
      el.addEventListener('pointermove', function (e) {
        if (!dragging) return;
        e.preventDefault();
        onDragMove(e.clientY);
      });
      el.addEventListener('pointerup', function (e) {
        if (!dragging) return;
        try { el.releasePointerCapture(e.pointerId); } catch (_) {}
        onDragEnd();
      });
      el.addEventListener('pointercancel', onDragEnd);
    }

    bindDrag(grabber);
    bindDrag(D.getElementById('nina-head'));

    grabber.addEventListener('click', function () {
      if (!isMobileSheet() || !isOpen || dragMoved) return;
      if (sheetState === 'half') snapSheet(SHEET_FULL, true);
      else if (sheetState === 'full') snapSheet(SHEET_HALF, true);
    });

    backdrop.addEventListener('click', function () {
      if (!isDockMode()) close();
    });

    D.addEventListener('click', function (e) {
      if (!isOpen || isMobileSheet() || isDockMode()) return;
      if (!panel.contains(e.target) && e.target !== fab) close();
    });

    if (W.visualViewport) {
      W.visualViewport.addEventListener('resize', function () {
        if (!isOpen || !isMobileSheet()) return;
        var ratio = sheetState === 'full' ? SHEET_FULL : SHEET_HALF;
        setSheetRatio(ratio, false);
      });
    }

    W.addEventListener('resize', function () {
      var wasOpen = isOpen;
      applyLayoutMode();
      if (wasOpen && isMobileSheet()) {
        setSheetRatio(sheetState === 'full' ? SHEET_FULL : SHEET_HALF, false);
      }
    });

    W.addEventListener('popstate', function () { renderChips(); });

    fab.addEventListener('click', function () { isOpen ? close() : open(); });
    D.getElementById('nina-close').addEventListener('click', close);
    layoutBtn.addEventListener('click', toggleLayout);
    D.getElementById('nina-menu').addEventListener('click', function () {
      addRow('sys', 'Tip: use the suggestion chips below, or ask about products, cart, and orders.');
      renderChips();
    });

    D.addEventListener('keydown', function (e) { if (e.key === 'Escape' && isOpen) close(); });

    var input = D.getElementById('nina-input');
    var send  = D.getElementById('nina-send');

    input.addEventListener('input', function () {
      setSendMode(this.value.trim().length > 0);
      this.style.height = 'auto';
      this.style.height = Math.min(this.scrollHeight, 100) + 'px';
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey) {
        e.preventDefault();
        if (!send.disabled && !send.classList.contains('nina-mic')) send.click();
      }
    });

    send.addEventListener('click', function () {
      if (send.classList.contains('nina-mic')) return;
      var text = input.value.trim();
      if (!text || _busy) return;
      input.value = '';
      input.style.height = 'auto';
      setSendMode(false);
      sendUserMessage(text, text);
    });

    applyLayoutMode();
    renderChips();
    _openPanelFn = open;

    function restoreSession() {
      var data = loadSession();
      if (!data || !data.messages || !data.messages.length) return false;
      _sessionLog = data.messages.slice();
      data.messages.forEach(function (m) {
        if (m.role === 'products' && m.products && m.products.length) {
          renderProducts(m.products, m.verified !== false, { skipLog: true });
        } else if (m.text) {
          addRow(m.role, m.text, { skipLog: true, restored: true });
        }
      });
      greeted = true;
      if (data.panelOpen && !isMobileSheet()) {
        setTimeout(function () { if (!isOpen) open(); }, 0);
      }
      return true;
    }

    restoreSession();
  }

  if (D.readyState === 'loading') {
    D.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

}(window, document));
