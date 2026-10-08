/* Homepage-side driver for the handoff browser test.

   Loaded by test_homepage_handoff.py into the BUILT marketing site
   (site/dist/public/index.html), before the site's own module script. The
   chat iframe it drives is the real frontend/ app with handoff_child.js.

   window.__HANDOFF_MODE__ selects what runs once the page has loaded:
   - "main": protocol rejection checks on both sides, the real first-prompt
     handoff, Back/Forward, a second handoff, storage checks; then a report
     to the loopback test server and a reload of /app/ (handoff_child.js
     reports that standalone page separately);
   - "shot-expanded": send one prompt and stop (for screenshots);
   - "shot-back": the same, then browser Back (for screenshots);
   - anything else: do nothing.

   It interacts with the chat only through its DOM controls, the same way a
   user does, and with the homepage only through the browser (postMessage,
   history, layout). */
(function () {
  "use strict";

  var MODE = window.__HANDOFF_MODE__ || "";
  var ORIGIN = location.origin;
  var CROSS_ORIGIN = window.__HANDOFF_CROSS_ORIGIN__ || "";
  var PROMPT = "kalillac:embed:prompt-submitted";
  var PRESENTATION = "kalillac:embed:presentation";
  var realFetch = window.fetch.bind(window);
  var writes = [];

  // Storage and cookie writes made by the homepage itself.
  (function instrument(win, log) {
    ["setItem", "removeItem", "clear"].forEach(function (name) {
      var original = win.Storage.prototype[name];
      win.Storage.prototype[name] = function () { log.push("Storage." + name); return original.apply(this, arguments); };
    });
    var cookie = Object.getOwnPropertyDescriptor(win.Document.prototype, "cookie");
    Object.defineProperty(win.Document.prototype, "cookie", {
      configurable: true,
      get: function () { return cookie.get.call(this); },
      set: function (value) { log.push("document.cookie"); cookie.set.call(this, value); }
    });
    ["open", "deleteDatabase"].forEach(function (name) {
      var original = win.IDBFactory.prototype[name];
      win.IDBFactory.prototype[name] = function () { log.push("indexedDB." + name); return original.apply(this, arguments); };
    });
    if (win.CookieStore) {
      ["set", "delete"].forEach(function (name) {
        var original = win.CookieStore.prototype[name];
        win.CookieStore.prototype[name] = function () { log.push("cookieStore." + name); return original.apply(this, arguments); };
      });
    }
  })(window, writes);

  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // indexedDB.databases() can stay pending in a headless browser; bound it.
  function databaseNames(win) {
    if (!win.indexedDB || !win.indexedDB.databases) return Promise.resolve("unsupported");
    return Promise.race([
      win.indexedDB.databases().then(function (list) {
        return list.map(function (d) { return d.name; });
      }),
      new Promise(function (r) { setTimeout(function () { r("no-answer"); }, 1000); })
    ]);
  }

  async function waitFor(check, label) {
    for (var waited = 0; waited < 15000; waited += 25) {
      var value = null;
      try { value = check(); } catch (e) { value = null; }
      if (value) return value;
      await wait(25);
    }
    throw new Error("timeout waiting for " + label);
  }

  function describe(el) {
    if (!el) return null;
    return el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") +
      (typeof el.className === "string" && el.className ? "." + el.className.trim().split(/\s+/).join(".") : "");
  }

  function frameEl() { return document.querySelector('[data-testid="embedded-chat"] iframe'); }
  function card() { return document.querySelector('[data-testid="embedded-chat"]'); }

  function rect(el) {
    var r = el.getBoundingClientRect();
    return { top: Math.round(r.top), left: Math.round(r.left),
             width: Math.round(r.width), height: Math.round(r.height),
             bottom: Math.round(r.bottom), right: Math.round(r.right) };
  }

  // Run code inside a same-origin frame's own realm, so the message it posts
  // really comes from that frame's window.
  function runIn(doc, code) {
    var script = doc.createElement("script");
    script.textContent = code;
    doc.body.appendChild(script);
    script.remove();
  }

  function loadFrame(src) {
    return new Promise(function (resolve) {
      var f = document.createElement("iframe");
      f.style.cssText = "position:absolute;left:-9999px;top:0;width:400px;height:300px;border:0";
      f.addEventListener("load", function () { resolve(f); }, { once: true });
      f.src = src;
      document.body.appendChild(f);
    });
  }

  var visitedUrls = [];
  var frameWindow = null;

  // Every message the homepage window receives, so each rejection check can
  // prove its message was actually delivered.
  var received = [];
  window.addEventListener("message", function (event) {
    var data = event.data;
    received.push({
      origin: event.origin,
      fromChat: !!frameWindow && event.source === frameWindow,
      fromSelf: event.source === window,
      type: data && typeof data === "object" ? data.type : null
    });
  });

  function snap(label) {
    document.documentElement.setAttribute("data-handoff-step", label);
    var frame = frameEl();
    var win = frame.contentWindow;
    var doc = frame.contentDocument;
    var input = doc.getElementById("composer-input");
    var send = doc.getElementById("send-btn");
    var conv = doc.getElementById("conversation");
    var inputRect = input.getBoundingClientRect();
    var sendRect = send.getBoundingClientRect();
    var ix = inputRect.left + Math.min(inputRect.width / 2, 40);
    var iy = inputRect.top + inputRect.height / 2;
    var scroller = document.scrollingElement;
    var childScroller = doc.scrollingElement;

    visitedUrls.push(location.href);

    return {
      label: label,
      path: location.pathname, search: location.search, hash: location.hash,
      href: location.href,
      historyLength: history.length,
      historyState: JSON.stringify(history.state),
      viewport: { width: innerWidth, height: innerHeight },
      cardExpanded: card().classList.contains("is-expanded"),
      cardRect: rect(card()),
      frameRect: rect(frame),
      htmlOverflow: getComputedStyle(document.documentElement).overflow,
      bodyOverflow: getComputedStyle(document.body).overflow,
      frameIsTopAtCenter: document.elementFromPoint(innerWidth / 2, innerHeight / 2) === frame,
      frameIsTopAtTop: document.elementFromPoint(innerWidth / 2, 5) === frame,
      elementAtTop: describe(document.elementFromPoint(innerWidth / 2, 5)),
      sameFrameWindow: win === frameWindow && win.__handoffIdentity === "original",
      childLoadedAt: win.__HANDOFF_CHILD__.loadedAt,
      childExpanded: doc.body.classList.contains("is-expanded"),
      childEmbedded: doc.body.classList.contains("is-embedded"),
      childHeaderShown: getComputedStyle(doc.querySelector(".app-header")).display !== "none",
      childFooterShown: getComputedStyle(doc.querySelector(".app-footer")).display !== "none",
      childPath: win.location.pathname, childSearch: win.location.search, childHash: win.location.hash,
      users: Array.prototype.map.call(doc.querySelectorAll("#thread .msg-user .bubble"),
                                      function (b) { return b.textContent; }),
      assistants: Array.prototype.map.call(doc.querySelectorAll("#thread .msg-assistant .md"),
                                           function (b) { return b.textContent.trim(); }),
      requests: win.__HANDOFF_CHILD__.requests.slice(),
      composer: input.value,
      composerRect: { top: Math.round(inputRect.top), bottom: Math.round(inputRect.bottom),
                      height: Math.round(inputRect.height) },
      sendRect: { top: Math.round(sendRect.top), bottom: Math.round(sendRect.bottom) },
      childViewportHeight: win.innerHeight,
      composerHit: doc.elementFromPoint(ix, iy) === input,
      sendHit: doc.elementFromPoint(sendRect.left + sendRect.width / 2,
                                    sendRect.top + sendRect.height / 2) === send ||
               send.contains(doc.elementFromPoint(sendRect.left + sendRect.width / 2,
                                                  sendRect.top + sendRect.height / 2)),
      childDocScroll: { scrollHeight: childScroller.scrollHeight, clientHeight: childScroller.clientHeight },
      conversationOverflowY: getComputedStyle(conv).overflowY,
      conversationScroll: { scrollHeight: conv.scrollHeight, clientHeight: conv.clientHeight },
      pageScroll: { scrollHeight: scroller.scrollHeight, clientHeight: scroller.clientHeight,
                    scrollTop: scroller.scrollTop }
    };
  }

  async function typeAndSend(text) {
    var doc = frameEl().contentDocument;
    var input = doc.getElementById("composer-input");
    input.focus();
    input.value = text;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    doc.getElementById("send-btn").click();
  }

  async function popTo(direction) {
    var popped = new Promise(function (r) { window.addEventListener("popstate", r, { once: true }); });
    if (direction === "back") history.back(); else history.forward();
    await popped;
    await wait(150);
  }

  async function ready() {
    var frame = await waitFor(frameEl, "chat iframe");
    await waitFor(function () {
      var d = frame.contentDocument;
      return d && d.readyState === "complete" && d.getElementById("composer-input") &&
             frame.contentWindow.__HANDOFF_CHILD__;
    }, "chat app");
    await wait(300);
    frameWindow = frame.contentWindow;
    frameWindow.__handoffIdentity = "original";
    return frame;
  }

  async function main() {
    var r = { phase: "parent", steps: [], probes: {}, errors: [] };
    var frame = await ready();
    var doc = frame.contentDocument;
    r.steps.push(snap("loaded"));

    // ---- the homepage (parent) rejects ---------------------------------------
    var valid = { type: PROMPT, version: 1 };

    window.postMessage(valid, ORIGIN);                       // its own window
    await wait(150);
    r.steps.push(snap("parent-own-window"));

    var sibling = await loadFrame("/__handoff__/blank");     // another same-origin window
    runIn(sibling.contentDocument, "parent.postMessage(" + JSON.stringify(valid) + ", location.origin);");
    await wait(150);
    r.steps.push(snap("parent-sibling-window"));

    await loadFrame(CROSS_ORIGIN + "/__handoff__/xorigin-sender");   // another origin
    await waitFor(function () {
      return received.some(function (m) { return m.origin === CROSS_ORIGIN && m.type === PROMPT; });
    }, "cross-origin prompt messages");
    await wait(150);
    r.steps.push(snap("parent-cross-origin"));

    var invalid = [
      { type: PROMPT, version: 2 }, { type: PROMPT, version: "1" }, { type: PROMPT, version: 0 },
      { type: PROMPT }, { type: "kalillac:embed:prompt-submited", version: 1 },
      { type: "KALILLAC:EMBED:PROMPT-SUBMITTED", version: 1 },
      { type: PROMPT, version: 1, message: "leaked prompt" }, { type: PROMPT, version: 1, extra: true },
      PROMPT, [PROMPT, 1], null, 1
    ];
    invalid.forEach(function (data) {                       // the real chat window, bad payloads
      runIn(doc, "parent.postMessage(" + JSON.stringify(data) + ", location.origin);");
    });
    await wait(200);
    r.steps.push(snap("parent-invalid-payloads"));

    // ---- the chat (child) rejects -------------------------------------------
    var expand = { type: PRESENTATION, version: 1, expanded: true };
    var collapse = { type: PRESENTATION, version: 1, expanded: false };

    [{ type: PRESENTATION, version: 1, expanded: "true" }, { type: PRESENTATION, version: 2, expanded: true },
     { type: PRESENTATION, version: "1", expanded: true }, { type: PRESENTATION, version: 1 },
     { type: "kalillac:embed:presentatio", version: 1, expanded: true },
     { type: PRESENTATION, version: 1, expanded: true, extra: 1 }, PRESENTATION, [PRESENTATION, 1, true], null
    ].forEach(function (data) { frameWindow.postMessage(data, ORIGIN); });
    await wait(200);
    r.probes.childInvalidFromParent = doc.body.classList.contains("is-expanded");

    runIn(sibling.contentDocument,                           // a sibling same-origin window
          "parent.document.querySelector('[data-testid=\"embedded-chat\"] iframe').contentWindow" +
          ".postMessage(" + JSON.stringify(expand) + ", location.origin);");
    await wait(150);
    r.probes.childFromSibling = doc.body.classList.contains("is-expanded");

    runIn(doc, "window.postMessage(" + JSON.stringify(expand) + ", location.origin);");   // itself
    await wait(150);
    r.probes.childFromItself = doc.body.classList.contains("is-expanded");

    // A page on another origin frames the real /app/ and posts a valid message.
    var xparent = await loadFrame(CROSS_ORIGIN + "/__handoff__/xorigin-parent");
    var grandchild = await waitFor(function () {
      var w = xparent.contentWindow.frames[0];
      return w && w.document && w.document.readyState === "complete" &&
             w.document.body.classList.contains("is-embedded") && w;
    }, "cross-origin parent's chat");
    // Proof the cross-origin messages really arrive (the check is not vacuous).
    var deliveredFromCrossOrigin = function () {
      return grandchild.__HANDOFF_CHILD__.received.filter(function (m) {
        return m.origin === CROSS_ORIGIN && m.fromParent && m.type === PRESENTATION;
      }).length;
    };
    await waitFor(function () { return deliveredFromCrossOrigin() >= 3; }, "cross-origin messages");
    await wait(150);
    r.probes.childFromCrossOriginParent = grandchild.document.body.classList.contains("is-expanded");
    r.probes.crossOriginParentDelivered = deliveredFromCrossOrigin();
    xparent.remove();

    frameWindow.postMessage(expand, ORIGIN);                 // positive control: the real parent
    await wait(150);
    r.probes.childFromRealParent = doc.body.classList.contains("is-expanded");
    frameWindow.postMessage(collapse, ORIGIN);
    await wait(150);
    r.probes.childCollapsedByRealParent = !doc.body.classList.contains("is-expanded");

    // Positive control on the parent: the exact message from the real chat
    // window expands the card -- and by itself sends nothing.
    runIn(doc, "parent.postMessage(" + JSON.stringify(valid) + ", location.origin);");
    await wait(200);
    r.steps.push(snap("parent-valid-message-only"));
    await popTo("back");
    r.steps.push(snap("after-control-back"));

    // ---- the real handoff ---------------------------------------------------
    await typeAndSend("What is 2 + 2?");
    await wait(60);
    r.steps.push(snap("submitted"));
    await wait(600);
    r.steps.push(snap("replied"));

    // The composer is usable after expansion.
    var input = doc.getElementById("composer-input");
    input.focus();
    input.value = "draft";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    r.probes.sendEnabledWithDraft = !doc.getElementById("send-btn").disabled;
    r.probes.composerFocused = doc.activeElement === input;
    input.value = "";
    input.dispatchEvent(new Event("input", { bubbles: true }));

    await popTo("back");
    r.steps.push(snap("back"));
    await popTo("forward");
    r.steps.push(snap("forward"));
    await popTo("back");
    r.steps.push(snap("back-again"));

    await typeAndSend("And 3 + 3?");
    await wait(60);
    r.steps.push(snap("second-submitted"));
    await wait(600);
    r.steps.push(snap("second-replied"));

    // ---- storage ---------------------------------------------------------------
    r.parentWrites = writes.slice();
    r.childWrites = frameWindow.__HANDOFF_CHILD__.writes.slice();
    r.parentLocalStorage = localStorage.length;
    r.parentSessionStorage = sessionStorage.length;
    r.childLocalStorage = frameWindow.localStorage.length;
    r.childSessionStorage = frameWindow.sessionStorage.length;
    r.parentCookie = document.cookie;
    r.childCookie = doc.cookie;
    r.databases = await databaseNames(window);
    r.childDatabases = await databaseNames(frameWindow);
    r.visitedUrls = visitedUrls;
    r.received = received;
    r.childReceived = frameWindow.__HANDOFF_CHILD__.received.slice();
    return r;
  }

  async function shotExpanded() {
    await ready();
    await typeAndSend("What is 2 + 2?");
    await wait(900);
  }

  async function shotBack() {
    await shotExpanded();
    await popTo("back");
  }

  window.addEventListener("load", async function () {
    if (MODE === "shot-expanded") { await shotExpanded(); return; }
    if (MODE === "shot-back") { await shotBack(); return; }
    if (MODE !== "main") return;

    var report;
    try {
      report = await main();
    } catch (e) {
      report = { phase: "parent", errors: [String(e && e.stack || e)] };
    }

    await realFetch("/__handoff__/report", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(report)
    });

    // A refresh at /app/ loads the canonical standalone chat (handoff_child.js
    // reports it). The parent is at /app/ after the second handoff.
    location.reload();
  });
})();
