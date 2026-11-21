/* Executable harness for the chat frontend's error contract.

   Loaded by test_frontend_error_contract.py into the real frontend/index.html,
   before any other script, so it can replace window.fetch before app.js runs.
   It drives the page only through its real DOM controls and events, never
   through app.js internals, and records what the page shows and what it
   would have sent.

   Two modes:
   - /app/?scenario=<name>: run one scenario against the real app.
   - /__harness__/runner?scenarios=a,b,...: run each scenario in a fresh
     same-origin iframe and write every result, as JSON, into <pre id="results">.
     The runner is each scenario's parent window, so it also records every
     message the framed chat posts to its parent -- which must be none: the
     chat never messages the page that embeds it.

   No request leaves the page: fetch is replaced before app.js loads. */
(function () {
  "use strict";

  function wait(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  // ---- runner page -----------------------------------------------------------
  if (location.pathname.indexOf("/__harness__/runner") === 0) {
    var names = (new URLSearchParams(location.search).get("scenarios") || "")
      .split(",").filter(Boolean);

    var runAll = function () {
      var results = {};
      var chain = Promise.resolve();

      names.forEach(function (name) {
        chain = chain.then(function () {
          return new Promise(function (resolve) {
            var frame = document.createElement("iframe");
            var messages = [];
            var onMessage = function (event) {
              if (event.source !== frame.contentWindow) return;
              messages.push({ origin: event.origin, data: event.data });
            };
            window.addEventListener("message", onMessage);
            frame.setAttribute("allow", "clipboard-write");
            // A phone-sized (or other named) viewport for the scenario that measures layout.
            var size = /_(\d{3,4})x(\d{3,4})$/.exec(name);
            if (size) {
              frame.style.border = "0";
              frame.style.width = size[1] + "px";
              frame.style.height = size[2] + "px";
            }
            frame.src = "/app/?scenario=" + encodeURIComponent(name);
            document.body.appendChild(frame);
            var waited = 0;

            (function poll() {
              var win = frame.contentWindow;
              var done = win && win.__HARNESS_RESULT__;

              if (done || waited >= 20000) {
                window.removeEventListener("message", onMessage);
                results[name] = done || { error: "timeout" };
                results[name].parentMessages = messages;
                results[name].runnerOrigin = location.origin;
                frame.parentNode.removeChild(frame);
                resolve();
                return;
              }

              waited += 25;
              setTimeout(poll, 25);
            })();
          });
        });
      });

      chain.then(function () {
        document.getElementById("results").textContent = JSON.stringify(results);
      });
    };

    window.addEventListener("load", runAll);
    return;
  }

  // ---- scenario page: scripted fetch ------------------------------------------
  var scenarioName = new URLSearchParams(location.search).get("scenario") || "";
  var queue = [];
  var requests = [];
  var deferred = {};

  // Reasoning scenarios: the page is marked as a reasoning-modes deployment before app.js runs.
  if (scenarioName.indexOf("reasoning_") === 0 || scenarioName === "spaces_with_modes") {
    document.documentElement.setAttribute("data-kalillac-reasoning", "on");
  }
  if (scenarioName === "reasoning_learn_more_accounts" || scenarioName === "reasoning_unoffered") {
    document.documentElement.setAttribute("data-kalillac-accounts", "on");
  }

  // Accounts scenarios: the page is marked as an accounts deployment before app.js runs.
  if (scenarioName.indexOf("accounts_") === 0) {
    document.documentElement.setAttribute("data-kalillac-accounts", "on");
  }

  // Spaces scenarios: the page is marked as a Spaces deployment before app.js runs, and the one
  // question app.js then asks (may this account use Spaces?) is answered first.
  if (scenarioName.indexOf("spaces_") === 0) {
    document.documentElement.setAttribute("data-kalillac-spaces", "on");
    queue.push(scenarioName === "spaces_not_offered" || scenarioName === "spaces_signed_out_accounts"
      ? { status: 401, json: { error: "not_authenticated" } }
      : { status: 200, json: { spaces: [], next_cursor: null } });
  }

  // Customer navigation: a signed-in account on an accounts and Spaces deployment, and pages opened with a
  // Space and conversation (or ?spaces=open) in the address, as the account pages link back.
  var ADDRESSES = {
    spaces_restore_from_address: "&space=11111111-1111-4111-8111-111111111111&conversation=33333333-3333-4333-8333-333333333333",
    spaces_restore_refused: "&space=11111111-1111-4111-8111-111111111111&conversation=33333333-3333-4333-8333-333333333333",
    spaces_list_from_address: "&spaces=open"
  };
  if (scenarioName === "spaces_account_menu" || ADDRESSES[scenarioName]) {
    document.documentElement.setAttribute("data-kalillac-accounts", "on");
  }
  if (ADDRESSES[scenarioName]) {
    history.replaceState(null, "", location.pathname + location.search + ADDRESSES[scenarioName]);
  }

  // Files scenarios: the page is marked as a files deployment before app.js runs, and the account
  // probe (GET /api/files) app.js then makes is answered first.
  if (scenarioName.indexOf("images_") === 0) {
    document.documentElement.setAttribute("data-kalillac-files", "on");
    document.documentElement.setAttribute("data-kalillac-images", "on");
    queue.push({ status: 200, json: { files: [], can_add: true } });
  }
  if (scenarioName === "files_offered" || scenarioName === "files_offered_no_access"
      || scenarioName === "files_edit_confirm" || scenarioName === "files_summarize") {
    document.documentElement.setAttribute("data-kalillac-files", "on");
    // Any signed-in account may list its own files (F6): an account that may not add any (Free, or
    // after Premium ends) is answered 200 with can_add false, and its own files listed.
    queue.push(scenarioName !== "files_offered_no_access" ? { status: 200, json: { files: [], can_add: true } }
      : { status: 200, json: { can_add: false, files: [{ id: "EeEeEeEeEeEeEeEeEeEeEe", filename: "kept.pdf",
                                                          available: true }] } });
  }

  // ---- scenario page: the End & erase timeout, under the scenario's control --
  // app.js aborts an erase request after 10 seconds. Those timers are held
  // here instead of running, so a scenario fires them when it chooses and
  // never waits ten seconds; every other timer is real.
  var ERASE_TIMEOUT_MS = 10000;
  var eraseTimers = [];
  var realSetTimeout = window.setTimeout;
  var realClearTimeout = window.clearTimeout;

  // The daily-limit pause lifts at the next 00:00 UTC by the device clock.
  // The clock is fixed at noon UTC, so that timer is about twelve hours long;
  // timers of a minute or more are held here and fired by a scenario.
  var FIXED_NOW = Date.UTC(2026, 9, 30, 12, 0, 0);
  Date.now = function () { return FIXED_NOW; };
  var LONG_TIMER_MS = 60000;
  var longTimers = [];

  // The End & erase notice counts its 12 seconds in 250 ms ticks. Those
  // intervals are held too, and a scenario advances them tick by tick.
  var RECEIPT_TICK_MS = 250;
  var tickers = [];
  var realSetInterval = window.setInterval;
  var realClearInterval = window.clearInterval;

  window.setTimeout = function (fn, delay) {
    if (delay === ERASE_TIMEOUT_MS) {
      var timer = { id: "erase-timer-" + eraseTimers.length, fn: fn, cleared: false, fired: false };
      eraseTimers.push(timer);
      return timer.id;
    }
    if (delay >= LONG_TIMER_MS) {
      var held = { id: "long-timer-" + longTimers.length, fn: fn, delay: delay, cleared: false, fired: false };
      longTimers.push(held);
      return held.id;
    }
    return realSetTimeout.apply(window, arguments);
  };

  window.clearTimeout = function (id) {
    for (var i = 0; i < eraseTimers.length; i++) {
      if (eraseTimers[i].id === id) { eraseTimers[i].cleared = true; return; }
    }
    for (var j = 0; j < longTimers.length; j++) {
      if (longTimers[j].id === id) { longTimers[j].cleared = true; return; }
    }
    return realClearTimeout.apply(window, arguments);
  };

  window.setInterval = function (fn, delay) {
    if (delay === RECEIPT_TICK_MS) {
      var ticker = { id: "ticker-" + tickers.length, fn: fn, cleared: false };
      tickers.push(ticker);
      return ticker.id;
    }
    return realSetInterval.apply(window, arguments);
  };

  window.clearInterval = function (id) {
    for (var i = 0; i < tickers.length; i++) {
      if (tickers[i].id === id) { tickers[i].cleared = true; return; }
    }
    return realClearInterval.apply(window, arguments);
  };

  function abortError() {
    return new DOMException("The user aborted a request.", "AbortError");
  }

  function respond(spec, signal) {
    if (spec.reject) {
      return Promise.reject(new TypeError(spec.reject));
    }

    if (spec.hang) {
      return new Promise(function (resolve, reject) {
        if (signal) signal.addEventListener("abort", function () { reject(abortError()); });
      });
    }

    // Pending until the scenario settles it by name (api.settle), so
    // completion order is explicit. ignoreAbort models a response that
    // arrives even though the request was aborted.
    if (spec.defer) {
      return new Promise(function (resolve, reject) {
        deferred[spec.defer] = { resolve: resolve, reject: reject };
        if (signal && !spec.ignoreAbort) {
          signal.addEventListener("abort", function () { reject(abortError()); });
        }
      });
    }

    var headers = { "Content-Type": spec.contentType || "application/json" };
    var response;

    if (spec.bodyError) {
      var stream = new ReadableStream({
        start: function (c) { c.error(new TypeError("RAW-STREAM-SECRET body failed")); }
      });
      response = new Response(stream, { status: spec.status || 200, headers: headers });
    } else {
      var body = spec.json !== undefined ? JSON.stringify(spec.json) : (spec.body || "");
      // A 204 has no body at all (the Response constructor refuses one, even empty).
      response = new Response(spec.status === 204 ? null : body, { status: spec.status || 200, headers: headers });
    }

    if (!spec.delay) return Promise.resolve(response);

    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () { resolve(response); }, spec.delay);
      if (signal) signal.addEventListener("abort", function () {
        clearTimeout(timer);
        reject(abortError());
      });
    });
  }

  // GET /api/chat/reasoning-modes (a reasoning deployment's one question on load) is answered here, outside
  // the scripted queue, and recorded on its own: every mode by default, Quick and Balanced for a Free account.
  var modeRequests = [];
  var FREE_MODES = scenarioName === "reasoning_free_account" || scenarioName.indexOf("reasoning_learn_more") === 0 ||
                   scenarioName.indexOf("reasoning_keyboard") === 0;
  var MODES_ANSWER = scenarioName === "reasoning_unoffered"
    // A deployment that offers Deep to no plan: it is in neither list.
    ? { status: 200, json: { modes: ["quick", "balanced"], default: "balanced", premium: [] } }
    : scenarioName === "reasoning_premium_unstated"
      // An answer without the premium list: nothing may be called Premium on the strength of it.
      ? { status: 200, json: { modes: ["quick", "balanced"], default: "balanced" } }
      : FREE_MODES
        ? { status: 200, json: { modes: ["quick", "balanced"], default: "balanced", premium: ["deep"] } }
        // An account that has every mode (Premium, with Deep enabled): the server lists nothing more to add.
        : { status: 200, json: { modes: ["quick", "balanced", "deep"], default: "balanced", premium: [] } };

  // GET /api/account/entitlements (asked once the Spaces answer shows an account) is answered here too:
  // Premium for the account-menu scenario, Free otherwise.
  var planRequests = [];
  var PLAN_ANSWER = { status: 200, json: { entitlements: { tier: scenarioName === "spaces_account_menu" ? "paid" : "free" } } };
  if (scenarioName === "spaces_signed_out_accounts") {
    document.documentElement.setAttribute("data-kalillac-accounts", "on");
  }

  window.fetch = function (url, init) {
    if (String(url) === "/api/account/entitlements") {
      planRequests.push({ url: String(url), method: init && init.method });
      return respond(PLAN_ANSWER, null);
    }
    if (String(url) === "/api/chat/reasoning-modes") {
      modeRequests.push({ url: String(url), method: init && init.method });
      return respond(MODES_ANSWER, null);
    }
    var record = { url: String(url), method: init && init.method };
    try { record.body = JSON.parse(init.body); } catch (e) { record.body = null; }
    record.keepalive = !!(init && init.keepalive);
    record.contentType = init && init.headers ? init.headers["Content-Type"] || null : null;
    record.hasSignal = !!(init && init.signal);
    record.aborted = false;
    if (init && init.signal) {
      init.signal.addEventListener("abort", function () { record.aborted = true; });
    }
    requests.push(record);

    var spec = queue.shift();
    if (!spec) return Promise.reject(new TypeError("HARNESS: no scripted response"));
    return respond(spec, init && init.signal);
  };

  // ---- scenario page: scripted clipboard -----------------------------------------
  /* Headless browsers do not reliably settle real clipboard writes, so the
     clipboard is scripted like fetch: it records what Copy wrote. */
  var copied = [];
  try {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: function (value) { copied.push(String(value)); return Promise.resolve(); }
      }
    });
  } catch (e) { /* leave the real clipboard */ }

  // ---- scenario page: DOM driver ------------------------------------------------
  function $(id) { return document.getElementById(id); }

  function text(el) { return el ? el.textContent : null; }

  // The daily-limit dialog's state; the cancel event's default is recorded
  // by cancelDialog below.
  var lastCancelPrevented = null;

  function dialogState() {
    var dialog = $("limit-dialog");
    if (!dialog) return null;
    var modal = false;
    try { modal = dialog.open && dialog.matches(":modal"); } catch (e) { modal = null; }
    return {
      open: dialog.open,
      modal: modal,
      text: dialog.textContent.replace(/\s+/g, " ").trim(),
      focusIsOk: document.activeElement === $("limit-dialog-ok"),
      cancelPrevented: lastCancelPrevented
    };
  }

  function snapshot(label) {
    var input = $("composer-input");
    var button = $("send-btn");
    var empty = $("empty");

    return {
      label: label,
      users: Array.prototype.map.call(
        document.querySelectorAll("#thread .msg-user .bubble"), text),
      assistants: Array.prototype.map.call(
        document.querySelectorAll("#thread .msg-assistant"), function (row) {
          var body = row.querySelector(".md");
          return {
            html: body ? body.innerHTML : null,
            text: text(body),
            notes: Array.prototype.map.call(
              row.querySelectorAll(".error-note, .notice-note"), function (n) {
                return { kind: n.className, text: n.textContent };
              }),
            actions: Array.prototype.map.call(
              row.querySelectorAll(".msg-action span"), text),
            visibleActionBar: !!row.querySelector(".msg-actions.is-visible")
          };
        }),
      rowOrder: Array.prototype.map.call(
        document.querySelectorAll("#thread .msg"), function (row) {
          return row.classList.contains("msg-user") ? "user" : "assistant";
        }),
      threadText: text($("thread")),
      composer: input.value,
      readOnly: input.readOnly,
      heightStyle: input.style.height,
      sendLabel: button.getAttribute("aria-label"),
      sendIsStop: button.classList.contains("is-stop"),
      sendDisabled: button.disabled,
      emptyShown: !!(empty && empty.isConnected),
      conversationEmpty: $("conversation").classList.contains("is-empty"),
      focusIsComposer: document.activeElement === input,
      requestCount: requests.length,
      limitDialog: dialogState(),
      limitNotice: limitNoticeState(),
      sendDescribedBy: button.getAttribute("aria-describedby"),
      erase: eraseState()
    };
  }

  function limitNoticeState() {
    var notice = $("limit-notice");
    if (!notice) return null;
    return {
      shown: !notice.hidden && getComputedStyle(notice).display !== "none",
      text: notice.textContent.replace(/\s+/g, " ").trim(),
      role: notice.getAttribute("role"),
      live: notice.getAttribute("aria-live"),
      count: document.querySelectorAll(".limit-notice:not([hidden])").length,
      armedResets: longTimers.filter(function (t) { return !t.cleared && !t.fired; }).length
    };
  }

  function eraseState() {
    var button = $("erase-btn");
    var dialog = $("erase-dialog");
    var receipt = $("erase-receipt");
    if (!button || !dialog || !receipt) return null;
    var modal = false;
    try { modal = dialog.open && dialog.matches(":modal"); } catch (e) { modal = null; }
    var retry = $("erase-retry");
    return {
      buttonDisabled: button.disabled,
      buttonText: button.textContent.replace(/\s+/g, " ").trim(),
      dialogOpen: dialog.open,
      dialogModal: modal,
      dialogText: dialog.textContent.replace(/\s+/g, " ").trim(),
      focusIsCancel: document.activeElement === $("erase-dialog-cancel"),
      focusIsButton: document.activeElement === button,
      receiptShown: !receipt.hidden,
      receiptText: text($("erase-receipt-text")),
      receiptNote: $("erase-receipt-note").hidden ? null : text($("erase-receipt-note")),
      retryShown: !retry.hidden,
      retryDisabled: retry.disabled,
      receiptRole: receipt.getAttribute("role"),
      receiptLive: receipt.getAttribute("aria-live"),
      receiptDisplay: getComputedStyle(receipt).display,
      receiptHeight: receipt.getBoundingClientRect().height,
      receiptLeaving: receipt.classList.contains("is-leaving"),
      dismissText: text($("erase-receipt-dismiss")),
      focusIsConfirm: document.activeElement === $("erase-dialog-confirm"),
      armedTickers: tickers.filter(function (t) { return !t.cleared; }).length
    };
  }

  var steps = [];

  var api = {
    // A user edit: set the composer text and fire the input event it causes.
    type: function (value) {
      var input = $("composer-input");
      input.value = value;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    },
    clickSend: function () { $("send-btn").click(); },
    send: function (value) { api.type(value); api.clickSend(); return wait(30); },
    stop: function () { $("send-btn").click(); return wait(30); },
    wait: wait,
    // Click the last action button whose label is `label`.
    clickAction: function (label) {
      var buttons = document.querySelectorAll("#thread .msg-action");
      for (var i = buttons.length - 1; i >= 0; i--) {
        if (text(buttons[i].querySelector("span")) === label) {
          buttons[i].click();
          return wait(30);
        }
      }
      throw new Error("no action " + label);
    },
    // Click the action labelled `label` on the assistant row at `index`
    // (0 = the first assistant row in the thread).
    clickActionInRow: function (index, label) {
      var row = document.querySelectorAll("#thread .msg-assistant")[index];
      var buttons = row ? row.querySelectorAll(".msg-action") : [];
      for (var i = 0; i < buttons.length; i++) {
        if (text(buttons[i].querySelector("span")) === label) {
          buttons[i].click();
          return wait(30);
        }
      }
      throw new Error("no action " + label + " on assistant row " + index);
    },
    // Make the vendored sanitizer fail once, to simulate a rendering failure.
    breakSanitizeOnce: function () {
      var original = window.DOMPurify.sanitize;
      window.DOMPurify.sanitize = function () {
        window.DOMPurify.sanitize = original;
        throw new Error("SANITIZE-SECRET render failed");
      };
    },
    snap: function (label) { steps.push(snapshot(label)); },
    snapshotOnly: function (label) { return snapshot(label); },
    push: function (snap) { steps.push(snap); },
    // The daily-limit dialog's three close paths, a click inside its panel,
    // the showModal() fallback and a layout measurement.
    clickDialogOk: function () { $("limit-dialog-ok").click(); return wait(30); },
    cancelDialog: function () {
      var event = new Event("cancel", { cancelable: true });
      $("limit-dialog").dispatchEvent(event);
      lastCancelPrevented = event.defaultPrevented;
      return wait(30);
    },
    clickDialogBackdrop: function () {
      $("limit-dialog").dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      return wait(30);
    },
    clickDialogTitle: function () { $("limit-dialog-title").click(); return wait(30); },
    removeShowModal: function () { HTMLDialogElement.prototype.showModal = undefined; },
    // Rendered fenced code: highlighting, token colors, markup, text and
    // page safety, plus layout, as one snapshot.
    inspectCode: function (label) {
      var snap = snapshot(label);
      snap.code = Array.prototype.map.call(
        document.querySelectorAll("#thread .msg-assistant pre > code"), function (code) {
          var match = /(?:^|\s)language-([\w#+.-]+)/.exec(code.className || "");
          var colors = {}, tags = {}, attrs = {};
          Array.prototype.forEach.call(code.querySelectorAll("span"), function (span) {
            if (!(span.className in colors)) colors[span.className] = getComputedStyle(span).color;
          });
          Array.prototype.forEach.call(code.querySelectorAll("*"), function (el) {
            tags[el.tagName.toLowerCase()] = true;
            Array.prototype.forEach.call(el.attributes, function (a) { attrs[a.name] = true; });
          });
          var pre = code.parentNode.getBoundingClientRect();
          return {
            language: match ? match[1] : "",
            highlighted: code.classList.contains("hljs"),
            text: code.textContent,
            spans: code.querySelectorAll("span").length,
            colors: colors,
            tags: Object.keys(tags),
            attrs: Object.keys(attrs),
            plainColor: getComputedStyle(code).color,
            preRight: pre.right,
            preScrolls: code.parentNode.scrollWidth > code.parentNode.clientWidth
          };
        });
      snap.inlineCode = Array.prototype.map.call(
        document.querySelectorAll("#thread .msg-assistant :not(pre) > code"), function (c) {
          return { text: c.textContent, spans: c.querySelectorAll("span").length, highlighted: c.classList.contains("hljs") };
        });
      snap.prose = Array.prototype.map.call(
        document.querySelectorAll("#thread .msg-assistant .md > p"), text);
      snap.page = {
        scripts: document.querySelectorAll("#thread script").length,
        images: document.querySelectorAll("#thread img").length,
        alerts: window.__alerts || [],
        executed: !!window.__executed,
        viewport: [window.innerWidth, window.innerHeight],
        overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth
      };
      steps.push(snap);
    },
    measureDialog: function (label) {
      var snap = snapshot(label);
      var dialog = $("limit-dialog");
      var box = dialog.getBoundingClientRect();
      var ok = $("limit-dialog-ok").getBoundingClientRect();
      snap.layout = {
        viewport: [window.innerWidth, window.innerHeight],
        dialog: [box.left, box.top, box.right, box.bottom],
        button: [ok.left, ok.top, ok.right, ok.bottom],
        dialogOverflowX: dialog.scrollWidth - dialog.clientWidth,
        dialogOverflowY: dialog.scrollHeight - dialog.clientHeight,
        pageOverflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth
      };
      steps.push(snap);
    },
    clickErase: function () { $("erase-btn").click(); return wait(30); },
    confirmErase: function () { $("erase-dialog-confirm").click(); return wait(60); },
    cancelEraseButton: function () { $("erase-dialog-cancel").click(); return wait(30); },
    escapeEraseDialog: function () {
      var event = new Event("cancel", { cancelable: true });
      $("erase-dialog").dispatchEvent(event);
      lastCancelPrevented = event.defaultPrevented;
      return wait(30);
    },
    clickEraseBackdrop: function () {
      $("erase-dialog").dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      return wait(30);
    },
    clickRetryErase: function () { $("erase-retry").click(); return wait(60); },
    // Every other way to submit: Enter in the field, the form's own submit
    // and a click on Send even while it is disabled.
    pressEnter: function () {
      $("composer-input").dispatchEvent(new KeyboardEvent("keydown",
        { key: "Enter", code: "Enter", bubbles: true, cancelable: true }));
      return wait(30);
    },
    submitForm: function () { $("composer").requestSubmit(); return wait(30); },
    // Fires the held daily-limit release (the next 00:00 UTC).
    fireLimitReset: function () {
      longTimers.forEach(function (timer) {
        if (!timer.cleared && !timer.fired) { timer.fired = true; timer.fn(); }
      });
      return wait(30);
    },
    longTimerDelays: function () {
      return longTimers.map(function (t) { return t.delay; });
    },
    // Advances the End & erase notice countdown by n ticks of 250 ms.
    tickReceipt: function (n) {
      for (var k = 0; k < n; k++) {
        tickers.forEach(function (ticker) { if (!ticker.cleared) ticker.fn(); });
      }
      return wait(0);
    },
    hoverReceipt: function (on) {
      $("erase-receipt").dispatchEvent(new MouseEvent(on ? "mouseenter" : "mouseleave"));
      return wait(0);
    },
    focusDismiss: function () { $("erase-receipt-dismiss").focus(); return wait(0); },
    blurDismiss: function () { $("erase-receipt-dismiss").blur(); return wait(0); },
    clickDismiss: function () { $("erase-receipt-dismiss").click(); return wait(0); },
    // prefers-reduced-motion: reduce, as app.js reads it at dismissal time.
    forceReducedMotion: function () {
      var real = window.matchMedia.bind(window);
      window.matchMedia = function (query) {
        if (query === "(prefers-reduced-motion: reduce)") return { matches: true, media: query };
        return real(query);
      };
    },
    // A Tab (or Shift+Tab) key press inside the open erase dialog.
    tabInEraseDialog: function (shift) {
      var event = new KeyboardEvent("keydown", { key: "Tab", code: "Tab", shiftKey: !!shift,
                                                bubbles: true, cancelable: true });
      document.activeElement.dispatchEvent(event);
      return wait(10);
    },
    focusEraseConfirm: function () { $("erase-dialog-confirm").focus(); return wait(10); },
    // Completes a deferred response with an ordinary spec.
    settle: function (name, spec) {
      var pending = deferred[name];
      if (!pending) throw new Error("HARNESS: nothing deferred as " + name);
      delete deferred[name];
      respond(spec, null).then(pending.resolve, pending.reject);
      return wait(60);
    },
    // Fires every erase timeout that is still armed.
    fireEraseTimeouts: function () {
      eraseTimers.forEach(function (timer) {
        if (!timer.cleared && !timer.fired) { timer.fired = true; timer.fn(); }
      });
      return wait(60);
    },
    respondWith: function () {
      for (var i = 0; i < arguments.length; i++) queue.push(arguments[i]);
    },
    // The files a reply shows, the attach control and the attachments row.
    files: function (label) {
      var attach = $("attach-btn"), list = $("attachments");
      steps.push({
        label: label,
        chips: Array.prototype.map.call(document.querySelectorAll("#thread .file-chip"), function (chip) {
          return { href: chip.getAttribute("href"), download: chip.getAttribute("download"),
                   name: text(chip.querySelector(".file-name")), meta: text(chip.querySelector(".file-meta")),
                   html: !!chip.querySelector("img, script, iframe, svg") };  // Any element from a name.
        }),
        attachShown: !!attach && !attach.hidden,
        attachments: list ? Array.prototype.map.call(list.querySelectorAll(".attachment-name"), text) : null,
        attachmentsShown: !!list && !list.hidden,
        thumbs: list ? Array.prototype.map.call(list.querySelectorAll(".attachment img"), function (img) {
          return { blob: img.src.indexOf("blob:") === 0, alt: img.getAttribute("alt") };
        }) : null,
        accept: $("attach-input") ? $("attach-input").accept : null,
        status: $("attach-status") && !$("attach-status").hidden ? text($("attach-status")) : null
      });
    },
    // The header's account link (present only on an accounts deployment).
    // What the Spaces controls show, and how many requests have been made.
    spaces: function (label) {
      var dialog = $("spaces-dialog");
      var bar = $("space-bar");
      var status = $("session-status");
      var erase = $("erase-btn");
      steps.push({
        label: label,
        button: Array.prototype.map.call(document.querySelectorAll(".app-header .spaces-button"), text),
        dialogOpen: !!(dialog && dialog.open),
        title: text($("spaces-dialog-title")),
        status: text($("spaces-status")),
        items: Array.prototype.map.call(document.querySelectorAll("#spaces-list .spaces-item"), function (b) {
          return b.textContent;
        }),
        back: !!$("spaces-back") && !$("spaces-back").hidden,
        barShown: !!bar && !bar.hidden,
        barText: bar ? bar.textContent.replace(/\s+/g, " ").trim() : null,
        save: $("space-save") ? [$("space-save").getAttribute("data-state"), text($("space-save-text")),
                                 $("space-save").getAttribute("role"), !!$("space-save").title] : null,
        searchShown: !!$("spaces-search") && !$("spaces-search").hidden,
        mode: $("spaces-dialog-panel").getAttribute("data-mode"),
        // What is on screen, not only the attribute: a display rule can override [hidden].
        sessionStatusShown: !!status && getComputedStyle(status).display !== "none",
        eraseShown: !!erase && getComputedStyle(erase).display !== "none",
        requestCount: requests.length
      });
    },
    clickSpaces: function () { document.querySelector(".app-header .spaces-button").click(); return wait(80); },
    clickSpacesItem: async function (index) {
      // The list is drawn after its scripted response: wait for it rather than for a fixed pause.
      var item = await until(function () { return document.querySelectorAll("#spaces-list .spaces-item")[index]; }, "spaces item " + index);
      item.click();
      return wait(80);
    },
    createInSpaces: function (value) {
      $("spaces-new-name").value = value;
      $("spaces-new").requestSubmit();
      return wait(80);
    },
    spacesBack: function () { $("spaces-back").click(); return wait(80); },
    leaveSpace: function () { $("space-leave").click(); return wait(40); },
    account: function (label) {
      var links = document.querySelectorAll(".app-header .account-link");
      steps.push({
        label: label,
        plan: text($("app-plan")),
        links: Array.prototype.map.call(links, function (l) {
          return { href: l.getAttribute("href"), text: l.textContent, target: l.getAttribute("target") };
        }),
        requestCount: requests.length
      });
    },
    modes: function (label) {
      var picker = $("mode-picker");
      steps.push({
        label: label,
        pickerShown: !!picker && !picker.hidden,
        checked: picker ? Array.prototype.map.call(picker.querySelectorAll('[aria-checked="true"]'),
                                                   function (o) { return o.getAttribute("data-mode"); }) : null,
        focused: document.activeElement && document.activeElement.getAttribute("data-mode"),
        unavailable: picker ? Array.prototype.map.call(picker.querySelectorAll('[aria-disabled="true"]'),
                                                       function (o) { return o.getAttribute("data-mode"); }) : null,
        labels: picker ? Array.prototype.map.call(picker.querySelectorAll(".mode-option"), function (o) {
          return [text(o.querySelector(".mode-label")), !!o.querySelector("svg.mode-icon")];
        }) : null,
        note: $("mode-note") && !$("mode-note").hidden && !$("mode-note").classList.contains("is-quiet")
          ? text($("mode-note")) : null,
        // In the page and the tab order, but out of sight until its Learn more is focused.
        quietNote: $("mode-note") && !$("mode-note").hidden && $("mode-note").classList.contains("is-quiet")
          ? [text($("mode-note")), Math.round($("mode-note").getBoundingClientRect().width),
             $("mode-learn").tabIndex, getComputedStyle($("mode-learn")).visibility] : null,
        titles: picker ? Array.prototype.map.call(picker.querySelectorAll('[aria-disabled="true"]'), function (o) {
          return [o.getAttribute("data-mode"), o.title, o.querySelectorAll("svg.mode-lock").length,
                  text(o.querySelector(".sr-only"))];
        }) : null,
        learn: $("mode-learn") && !$("mode-note").hidden
          ? [$("mode-learn").tagName, $("mode-learn").type, text($("mode-learn")), $("mode-learn").getAttribute("aria-haspopup")]
          : null
      });
    },
    clickMode: function (mode) {
      document.querySelector('.mode-option[data-mode="' + mode + '"]').click();
      return wait(20);
    },
    keyOnMode: function (mode, key) {
      var option = document.querySelector('.mode-option[data-mode="' + mode + '"]');
      option.focus();
      option.dispatchEvent(new KeyboardEvent("keydown", { key: key, bubbles: true, cancelable: true }));
      return wait(20);
    },
    chooseFile: function (name, content) {
      var input = $("attach-input");
      var transfer = new DataTransfer();
      transfer.items.add(new File([content], name));
      input.files = transfer.files;
      input.dispatchEvent(new Event("change"));
      return wait(60);
    }
  };

  // ---- scenarios ---------------------------------------------------------------
  var SECRET = "RAW-SECRET OpenAI Tavily Traceback (most recent call last)";
  var ok = function (reply, sessionId) {
    return { status: 200, json: { reply: reply, session_id: sessionId || "s-1" } };
  };

  var CODES = {
    invalid_json: 400, invalid_body: 400, invalid_request: 422,
    empty_message: 422, message_too_long: 422, history_too_long: 422,
    processing_limit_reached: 422, busy: 429, request_cancelled: 499,
    service_unavailable: 503, model_provider_unavailable: 503,
    request_timeout: 504, internal_error: 500,
    session_ended: 409, unsupported_media_type: 415,
    reasoning_mode_not_allowed: 403, files_require_premium: 403, not_authenticated: 401,
    daily_limit_reached: 429, account_processing_limit_reached: 429, account_unavailable: 403, too_many_images: 502, image_unavailable: 502,
    file_not_found: 404, file_assistant_unavailable: 502, model_output_unusable: 502,
    model_output_incomplete: 502, model_refused: 502, tool_limit_exceeded: 502
  };

  // A failed send, then a deliberate new message that succeeds.
  async function failThenNewSend(spec, a) {
    a.respondWith(spec, ok("Fine."));
    await a.send("first question");
    a.snap("failed");
    await a.send("next question");
    a.snap("after-new-send");
  }

  var SCENARIOS = {};

  Object.keys(CODES).forEach(function (code) {
    SCENARIOS["code_" + code] = function (a) {
      return failThenNewSend(
        { status: CODES[code], json: { error: code, detail: SECRET, message: SECRET } }, a);
    };
  });

  SCENARIOS.bare_429 = function (a) { return failThenNewSend({ status: 429, body: "" }, a); };

  [502, 503, 504].forEach(function (status) {
    SCENARIOS["proxy_" + status] = function (a) {
      return failThenNewSend({
        status: status, contentType: "text/html",
        body: "<html><body><h1>" + status + " Bad Gateway</h1>nginx " + SECRET + "</body></html>"
      }, a);
    };
  });

  SCENARIOS.unknown_status = function (a) {
    return failThenNewSend({ status: 418, json: { error: "teapot", detail: SECRET } }, a);
  };
  SCENARIOS.fetch_reject = function (a) {
    return failThenNewSend({ reject: "Failed to fetch " + SECRET }, a);
  };
  SCENARIOS.reply_missing = function (a) {
    return failThenNewSend({ status: 200, json: { session_id: "s-1" } }, a);
  };
  SCENARIOS.reply_empty = function (a) { return failThenNewSend(ok(""), a); };
  SCENARIOS.reply_whitespace = function (a) { return failThenNewSend(ok("   \n\t "), a); };
  SCENARIOS.reply_not_string = function (a) {
    return failThenNewSend({ status: 200, json: { reply: 42 } }, a);
  };
  SCENARIOS.success_not_json = function (a) {
    return failThenNewSend({ status: 200, contentType: "text/plain", body: "ok " + SECRET }, a);
  };
  SCENARIOS.success_body_error = function (a) {
    return failThenNewSend({ status: 200, bodyError: true }, a);
  };
  SCENARIOS.success_error_code = function (a) {
    return failThenNewSend({ status: 200, json: { error: "busy" } }, a);
  };

  // A 2xx carrying both a reply and an error code is not a reply.
  SCENARIOS.success_reply_with_error = function (a) {
    return failThenNewSend({
      status: 200,
      json: { reply: "THIS MUST NOT BE RENDERED", error: "busy", detail: SECRET,
              session_id: "s-ambiguous" }
    }, a);
  };

  SCENARIOS.render_exception = async function (a) {
    a.respondWith(ok("**bold** " + SECRET), ok("Fine."));
    a.breakSanitizeOnce();
    await a.send("first question");
    a.snap("failed");
    await a.send("next question");
    a.snap("after-new-send");
  };

  SCENARIOS.success = async function (a) {
    a.respondWith(
      ok("Hello **world** <img src=x onerror=alert(1)><script>alert(2)</script>", "s-1"),
      ok("Second.", "s-1"));
    await a.send("first question");
    a.snap("first");
    await a.send("second question");
    a.snap("second");
  };

  SCENARIOS.stop = async function (a) {
    a.respondWith({ hang: true }, ok("Fine."));
    await a.send("first question");
    a.snap("in-flight");
    await a.stop();
    a.snap("stopped");
    await a.send("next question");
    a.snap("after-new-send");
  };

  SCENARIOS.stop_then_retry = async function (a) {
    a.respondWith({ hang: true }, ok("Recovered."));
    await a.send("first question");
    await a.stop();
    a.snap("stopped");
    await a.clickAction("Retry");
    a.snap("retried");
  };

  SCENARIOS.retry_success = async function (a) {
    a.respondWith(ok("Earlier answer.", "s-1"), { status: 429, json: { error: "busy" } },
                  ok("Recovered."), ok("Third."));
    await a.send("earlier question");
    await a.send("failing question");
    a.snap("failed");
    await a.clickAction("Retry");
    a.snap("retried");
    await a.send("third question");
    a.snap("after-third");
  };

  SCENARIOS.retry_fails_again = async function (a) {
    a.respondWith(ok("Earlier answer.", "s-1"), { status: 429, json: { error: "busy" } },
                  { status: 503, json: { error: "service_unavailable" } });
    await a.send("earlier question");
    await a.send("failing question");
    await a.clickAction("Retry");
    a.snap("failed-twice");
  };

  // Newer text typed while the request is pending is never overwritten.
  SCENARIOS.newer_draft_during_request = async function (a) {
    a.respondWith({ status: 429, json: { error: "busy" }, delay: 200 });
    await a.send("original question");
    a.type("a newer draft");
    await a.wait(300);
    a.snap("failed");
  };

  // The restored text is edited, then a Retry of the old exchange fails again:
  // the edit survives and the Retry still sends the original text.
  SCENARIOS.edited_draft_then_retry = async function (a) {
    a.respondWith({ status: 429, json: { error: "busy" } },
                  { status: 503, json: { error: "service_unavailable" } });
    await a.send("original question");
    a.snap("restored");
    a.type("original question, edited");
    await a.clickAction("Retry");
    a.snap("failed-again");
  };

  // An unedited restored draft is taken back by Retry and restored again if
  // the Retry fails.
  SCENARIOS.unedited_draft_retry = async function (a) {
    a.respondWith({ status: 429, json: { error: "busy" }, delay: 100 },
                  { status: 503, json: { error: "service_unavailable" }, delay: 100 });
    await a.send("original question");
    await a.wait(150);
    await a.clickAction("Retry");
    a.snap("retry-in-flight");
    await a.wait(150);
    a.snap("failed-again");
  };

  // The user deliberately clears the restored draft, then a Retry fails: the
  // cleared composer is the user's newer draft and stays empty.
  SCENARIOS.cleared_draft_then_retry = async function (a) {
    a.respondWith({ status: 429, json: { error: "busy" } },
                  { status: 503, json: { error: "service_unavailable" } });
    await a.send("original question");
    a.snap("restored");
    a.type("");
    a.snap("cleared");
    await a.clickAction("Retry");
    a.snap("failed-again");
  };

  // Regenerating a completed answer while a later exchange has failed.
  SCENARIOS.retry_completed_with_later_failure = async function (a) {
    a.respondWith(ok("Answer one.", "s-1"),
                  { status: 429, json: { error: "busy" } },
                  ok("Answer one, regenerated."),
                  ok("Answer three."));
    await a.send("question one");
    await a.send("question two");
    a.snap("failed");
    await a.clickActionInRow(0, "Retry");
    a.snap("regenerated");
    await a.send("question three");
    a.snap("after-next");
  };

  // One character over the backend's 4,000-character limit: the client sends
  // it unchanged, once, and shows the fixed message_too_long copy.
  SCENARIOS.message_too_long_4001 = async function (a) {
    a.respondWith({ status: 422, json: { error: "message_too_long" } });
    await a.send(new Array(4002).join("x"));
    a.snap("failed");
  };

  SCENARIOS.history_too_long_new_conversation = async function (a) {
    a.respondWith(ok("Earlier answer.", "s-1"),
                  { status: 422, json: { error: "history_too_long" } },
                  ok("Fresh start."));
    await a.send("earlier question");
    await a.send("one more question");
    a.snap("too-long");
    a.type("a draft to keep");
    await a.clickAction("New conversation");
    a.snap("reset");
    await a.send("fresh question");
    a.snap("after-reset-send");
  };

  SCENARIOS.success_copy_and_retry = async function (a) {
    a.respondWith(ok("Answer one.", "s-1"), ok("Answer one, again."), ok("Next."));
    await a.send("question one");
    await a.clickAction("Copy");
    await a.wait(300);
    a.snap("copied");
    await a.clickAction("Retry");
    a.snap("regenerated");
    await a.send("question two");
    a.snap("after-next");
  };

  // An accounts deployment links to the account pages and says where the plan is shown; the link
  // costs no request. Without the mark neither exists (see the plain success scenario).
  SCENARIOS.accounts_offered = async function (a) {
    a.account("loaded");
    a.respondWith({ status: 403, json: { error: "reasoning_mode_not_allowed" } },
                  { status: 403, json: { error: "files_require_premium" } });
    await a.send("first question");
    a.snap("mode-refused");
    await a.send("second question");
    a.snap("files-refused");
  };
  SCENARIOS.success_no_account_link = async function (a) {
    a.account("loaded");
    a.snap("idle");
  };

  // Each fenced block has its own Copy control for exactly its source text;
  // inline code has none, and the answer's Copy still copies the whole reply.
  var BLOCK_COPY_REPLY = "Here:\n\n```python\ndef add(a, b):\n    return a + b\n```\n\nPlain:\n\n```\n" +
    "x < y && \"q\"\n<script>alert(1)</script>\n```\n\nInline `z` stays.";

  SCENARIOS.code_block_copy = async function (a) {
    a.respondWith(ok(BLOCK_COPY_REPLY, "s-code"));
    await a.send("show code");
    a.snap("rendered");
    var buttons = document.querySelectorAll("#thread .code-copy");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].click();
      await a.wait(60);
    }
    a.snap("blocks-copied");
    await a.clickAction("Copy");
    await a.wait(300);
    a.snap("answer-copied");
  };

  // The anonymous daily allowance is used up (with server text that must never
  // be shown).
  var DAILY_LIMIT = {
    status: 429,
    json: { error: "anonymous_daily_limit_reached", detail: SECRET, message: SECRET }
  };

  // The refused prompt is rolled back into the composer, one dialog explains
  // it, then one note stays beside the composer while sending is paused. No
  // way of submitting sends anything until the 00:00 UTC release fires.
  SCENARIOS.code_anonymous_daily_limit_reached = async function (a) {
    a.respondWith(DAILY_LIMIT, ok("Fine."));
    await a.send("first question");
    a.snap("failed");
    await a.clickDialogOk();
    a.snap("closed");
    a.clickSend();
    await a.pressEnter();
    await a.submitForm();
    a.clickSend();
    await a.wait(60);
    a.snap("blocked");
    a.type("first question, revised");
    a.snap("edited");
    var delays = a.longTimerDelays();
    await a.fireLimitReset();
    var released = a.snapshotOnly("released");
    released.longTimerDelays = delays;
    a.push(released);
    await a.send("next question");
    a.snap("after-new-send");
  };

  // A refusal after a completed exchange removes only the refused rows.
  SCENARIOS.daily_limit_after_conversation = async function (a) {
    a.respondWith(ok("Earlier answer.", "s-1"), DAILY_LIMIT);
    await a.send("earlier question");
    await a.send("second question");
    a.snap("refused");
    await a.clickDialogOk();
    a.snap("closed");
  };

  SCENARIOS.daily_limit_escape = async function (a) {
    a.respondWith(DAILY_LIMIT);
    await a.send("first question");
    a.snap("open");
    await a.cancelDialog();
    a.snap("closed");
  };

  SCENARIOS.daily_limit_backdrop = async function (a) {
    a.respondWith(DAILY_LIMIT);
    await a.send("first question");
    await a.clickDialogTitle();
    a.snap("panel-click");
    await a.clickDialogBackdrop();
    a.snap("closed");
  };

  // While paused, a second attempt neither sends nor reopens the dialog;
  // after the release a new refusal opens it once more.
  SCENARIOS.daily_limit_reopen = async function (a) {
    a.respondWith(DAILY_LIMIT, DAILY_LIMIT);
    await a.send("first question");
    await a.clickDialogOk();
    a.snap("first-closed");
    await a.send("second question");
    a.snap("blocked");
    await a.fireLimitReset();
    a.clickSend();
    await a.wait(60);
    a.snap("reopened");
    await a.cancelDialog();
    a.snap("second-closed");
  };

  SCENARIOS.daily_limit_without_show_modal = async function (a) {
    a.removeShowModal();
    a.respondWith(DAILY_LIMIT);
    await a.send("first question");
    a.snap("failed");
  };

  // The daily-limit code on any status but 429 is not the daily limit.
  SCENARIOS.daily_limit_wrong_status = function (a) {
    return failThenNewSend(
      { status: 503, json: { error: "anonymous_daily_limit_reached", detail: SECRET } }, a);
  };
  // The same for a signed-in account's own daily limit.
  SCENARIOS.account_daily_limit_wrong_status = function (a) {
    return failThenNewSend(
      { status: 503, json: { error: "daily_limit_reached", detail: SECRET } }, a);
  };

  // Fenced code in several languages, an unknown label, an unlabeled fence,
  // script-looking code, inline code and prose after the code.
  var CODE_REPLY = [
    "Here is the Python version.",
    "",
    "```python",
    "def greet(name):",
    "    # Say hello.",
    "    return f\"Hello {name}\"",
    "```",
    "",
    "```javascript",
    "const total = [1, 2, 3].reduce((sum, n) => sum + n, 0); // add them",
    "console.log(`total ${total}`);",
    "```",
    "",
    "```sql",
    "SELECT email, COUNT(*) AS n FROM users WHERE active = 1 GROUP BY email HAVING COUNT(*) > 1;",
    "```",
    "",
    "```json",
    "{\"name\": \"Ada\", \"admin\": true, \"age\": 36}",
    "```",
    "",
    "```html",
    "<script>alert(\"test\")</script>",
    "<img src=x onerror=\"window.__executed=1\">",
    "```",
    "",
    "```foobarlang",
    "some text that is not code",
    "```",
    "",
    "```",
    "unlabeled block",
    "  keeps   spacing",
    "```",
    "",
    "Inline `code` stays plain. This explanation remains normal prose outside the code block."
  ].join("\n");

  async function highlightedCode(a) {
    window.__alerts = [];
    window.alert = function (m) { window.__alerts.push(String(m)); };
    a.respondWith(ok(CODE_REPLY));
    await a.send("show me code");
    await a.wait(100);
    a.inspectCode("rendered");
    await a.clickAction("Copy");
    await a.wait(300);
  }

  SCENARIOS.syntax_highlighting = highlightedCode;
  SCENARIOS.syntax_highlighting_360x640 = highlightedCode;

  // Every required language label and its common aliases, through the real
  // rendering pipeline, plus an unknown label and an unlabeled fence.
  var LANGUAGE_FENCES = [
    ["python", "def f(x):\n    return x  # ok"], ["py", "import os"],
    ["javascript", "const x = 1;"], ["js", "let y = 'a';"],
    ["typescript", "let n: number = 1;"], ["ts", "interface A { b: string }"],
    ["html", "<p class=\"a\">hi</p>"], ["css", "a { color: red; }"],
    ["json", "{\"a\": 1}"], ["sql", "SELECT 1;"],
    ["bash", "echo \"hi\""], ["sh", "if [ -f x ]; then echo y; fi"], ["shell", "$ ls -la"],
    ["java", "public class A {}"], ["c", "int main(void) { return 0; }"],
    ["cpp", "#include <vector>"], ["c++", "std::vector<int> v;"],
    ["csharp", "public class A { }"], ["c#", "var x = \"s\";"],
    ["markdown", "# Title"], ["md", "*emphasis*"],
    ["foobarlang", "plain text"], ["", "unlabeled"]
  ];

  SCENARIOS.highlight_languages = async function (a) {
    var reply = LANGUAGE_FENCES.map(function (pair) {
      return "```" + pair[0] + "\n" + pair[1] + "\n```";
    }).join("\n\n");
    a.respondWith(ok(reply));
    await a.send("show me every language");
    await a.wait(100);
    a.inspectCode("rendered");
  };

  SCENARIOS.daily_limit_layout_360x640 = async function (a) {
    a.respondWith(DAILY_LIMIT);
    await a.send("first question");
    a.measureDialog("open");
  };

  // ---- End & erase ---------------------------------------------------------------
  var ENDED = function (state) {
    return { status: 200, json: { ended: true, kalillac_session_state: state,
                                  active_requests: 0, cancellation: "none" } };
  };

  // Enabled only when there is a conversation, a session or a draft.
  SCENARIOS.erase_enablement = async function (a) {
    a.snap("initial");
    a.type("a draft");
    a.snap("draft");
    a.type("");
    a.snap("cleared");
    a.respondWith(ok("Answer.", "s-1"));
    await a.send("question");
    a.snap("conversation");
  };

  // Cancel, Escape and the backdrop all keep the conversation and send nothing.
  SCENARIOS.erase_cancel_paths = async function (a) {
    a.respondWith(ok("Answer.", "s-1"));
    await a.send("question");
    a.type("my draft");
    await a.clickErase();
    a.snap("open");
    await a.cancelEraseButton();
    a.snap("after-cancel");
    await a.clickErase();
    await a.escapeEraseDialog();
    a.snap("after-escape");
    await a.clickErase();
    await a.clickEraseBackdrop();
    a.snap("after-backdrop");
  };

  SCENARIOS.erase_removed = async function (a) {
    a.respondWith(ok("Answer one.", "s-1"), ok("Answer two.", "s-1"), ENDED("removed"), ok("Fresh.", "s-2"));
    await a.send("question one");
    await a.send("question two");
    a.type("an unsent draft");
    await a.clickErase();
    await a.confirmErase();
    a.snap("erased");
    await a.send("a new question");
    a.snap("after-new-send");
  };

  // The completion notice leaves by itself after 48 ticks of 250 ms (12 s),
  // with a 160 ms fade, and leaves nothing behind.
  SCENARIOS.erase_notice_auto_dismiss = async function (a) {
    a.respondWith(ok("Answer.", "s-1"), ENDED("removed"));
    await a.send("question");
    await a.clickErase();
    await a.confirmErase();
    a.snap("shown");
    await a.tickReceipt(47);
    a.snap("tick-47");
    await a.tickReceipt(1);
    a.snap("tick-48");
    await a.wait(250);
    a.snap("dismissed");
  };

  // Hovering or focusing the notice pauses the countdown; it resumes with
  // the time that was left, not from the start.
  SCENARIOS.erase_notice_pause_resume = async function (a) {
    a.respondWith(ok("Answer.", "s-1"), ENDED("removed"));
    await a.send("question");
    await a.clickErase();
    await a.confirmErase();
    await a.tickReceipt(20);
    await a.hoverReceipt(true);
    await a.tickReceipt(100);
    a.snap("hovered");
    await a.hoverReceipt(false);
    await a.focusDismiss();
    await a.tickReceipt(100);
    a.snap("focused");
    await a.blurDismiss();
    await a.tickReceipt(27);
    a.snap("resumed-47");
    await a.tickReceipt(1);
    await a.wait(250);
    a.snap("dismissed");
  };

  // Dismiss closes it at once (after the fade) and hands focus to the composer.
  SCENARIOS.erase_notice_manual_dismiss = async function (a) {
    a.respondWith(ok("Answer.", "s-1"), ENDED("removed"));
    await a.send("question");
    await a.clickErase();
    await a.confirmErase();
    await a.focusDismiss();
    a.snap("focused");
    await a.clickDismiss();
    a.snap("leaving");
    await a.wait(250);
    a.snap("dismissed");
  };

  // Under reduced motion it is removed at once, without a fade.
  SCENARIOS.erase_notice_reduced_motion = async function (a) {
    a.respondWith(ok("Answer.", "s-1"), ENDED("removed"));
    await a.send("question");
    await a.clickErase();
    await a.confirmErase();
    a.forceReducedMotion();
    await a.clickDismiss();
    a.snap("dismissed");
  };

  // Focus cycles inside the dialog; Cancel returns focus to End & erase.
  SCENARIOS.erase_dialog_focus = async function (a) {
    a.respondWith(ok("Answer.", "s-1"));
    await a.send("question");
    await a.clickErase();
    a.snap("open");
    await a.tabInEraseDialog(true);
    a.snap("shift-tab-from-cancel");
    await a.tabInEraseDialog(false);
    a.snap("tab-from-confirm");
    await a.cancelEraseButton();
    a.snap("cancelled");
  };

  // A repeated confirmation erases, and asks the server, exactly once.
  SCENARIOS.erase_confirm_once = async function (a) {
    a.respondWith(ok("Answer.", "s-1"), ENDED("removed"), ENDED("removed"));
    await a.send("question");
    await a.clickErase();
    $("erase-dialog-confirm").click();
    $("erase-dialog-confirm").click();
    $("erase-dialog-confirm").dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    $("erase-dialog-confirm").click();
    await a.wait(120);
    a.snap("erased");
  };

  SCENARIOS.erase_not_found = async function (a) {
    a.respondWith(ok("Answer.", "s-1"), ENDED("not_found"));
    await a.send("question");
    await a.clickErase();
    await a.confirmErase();
    a.snap("erased");
  };

  // The server cannot confirm removal: the device is still cleared, the
  // receipt says so, and Retry erase asks again with the same opaque id.
  SCENARIOS.erase_failure_then_retry = async function (a) {
    a.respondWith(ok("Answer.", "s-1"), { status: 503, json: { error: "service_unavailable", detail: SECRET } },
                  ENDED("removed"));
    await a.send("question");
    await a.clickErase();
    await a.confirmErase();
    a.snap("failed");
    await a.clickRetryErase();
    a.snap("retried");
  };

  SCENARIOS.erase_network_failure = async function (a) {
    a.respondWith(ok("Answer.", "s-1"), { reject: "Failed to fetch " + SECRET },
                  { status: 200, json: { ended: true, kalillac_session_state: "bogus" } });
    await a.send("question");
    await a.clickErase();
    await a.confirmErase();
    a.snap("failed");
    await a.clickRetryErase();
    a.snap("still-failed");
  };

  // Erasing while a reply is in flight: the browser request is aborted, a
  // separate erase request is sent, and nothing from the old exchange returns.
  SCENARIOS.erase_in_flight = async function (a) {
    a.respondWith(ok("Answer.", "s-1"), { hang: true }, ENDED("removed"));
    await a.send("question one");
    await a.send("question two");
    a.snap("in-flight");
    await a.clickErase();
    await a.confirmErase();
    await a.wait(60);
    a.snap("erased");
  };

  // No session id ever reached the page: clear locally, call nothing.
  SCENARIOS.erase_without_session = async function (a) {
    a.type("only a draft");
    await a.clickErase();
    await a.confirmErase();
    a.snap("erased");
  };

  SCENARIOS.erase_first_message_in_flight = async function (a) {
    a.respondWith({ hang: true });
    await a.send("first question");
    await a.clickErase();
    await a.confirmErase();
    await a.wait(60);
    a.snap("erased");
  };

  var ERASE_FAILED = { status: 503, json: { error: "service_unavailable" } };

  // Two overlapping erase rounds, the newer completing first: erase A stays
  // pending, B is begun and erased, B is removed, then A fails. Retry erase is
  // offered for A alone, and its success settles the round.
  SCENARIOS.erase_overlap_newer_first = async function (a) {
    a.respondWith(ok("Answer A.", "s-A"), { defer: "A" }, ok("Answer B.", "s-B"), { defer: "B" },
                  ENDED("not_found"));
    await a.send("question A");
    await a.clickErase();
    await a.confirmErase();
    a.snap("a-pending");
    await a.send("question B");
    await a.clickErase();
    await a.confirmErase();
    a.snap("both-pending");
    await a.settle("B", ENDED("removed"));
    a.snap("b-removed-a-pending");
    await a.settle("A", ERASE_FAILED);
    a.snap("a-failed");
    await a.clickRetryErase();
    a.snap("a-retried");
    // The settled round is forgotten: a later local-only erase is just that.
    a.type("a draft only");
    await a.clickErase();
    await a.confirmErase();
    a.snap("later-local-erase");
  };

  // The inverse order: A fails while B is still pending; nothing is decided
  // until B settles too.
  SCENARIOS.erase_overlap_older_first = async function (a) {
    a.respondWith(ok("Answer A.", "s-A"), { defer: "A" }, ok("Answer B.", "s-B"), { defer: "B" },
                  ENDED("not_found"));
    await a.send("question A");
    await a.clickErase();
    await a.confirmErase();
    await a.send("question B");
    await a.clickErase();
    await a.confirmErase();
    await a.settle("A", ERASE_FAILED);
    a.snap("a-failed-b-pending");
    await a.settle("B", ENDED("removed"));
    a.snap("b-removed");
    await a.clickRetryErase();
    a.snap("a-retried");
  };

  // Every id not found: the round reports "not found", never "removed".
  SCENARIOS.erase_overlap_all_not_found = async function (a) {
    a.respondWith(ok("Answer A.", "s-A"), { defer: "A" }, ok("Answer B.", "s-B"), { defer: "B" });
    await a.send("question A");
    await a.clickErase();
    await a.confirmErase();
    await a.send("question B");
    await a.clickErase();
    await a.confirmErase();
    await a.settle("B", ENDED("not_found"));
    await a.settle("A", ENDED("not_found"));
    a.snap("settled");
  };

  // The erase request times out: it is aborted and offers Retry erase. A late
  // answer to the timed-out attempt cannot change the outcome of the retry.
  SCENARIOS.erase_timeout_then_retry = async function (a) {
    a.respondWith(ok("Answer.", "s-1"), { defer: "first", ignoreAbort: true }, { defer: "second" },
                  ENDED("not_found"));
    await a.send("question");
    await a.clickErase();
    await a.confirmErase();
    a.snap("pending");
    await a.fireEraseTimeouts();
    a.snap("timed-out");
    await a.clickRetryErase();
    a.snap("retry-pending");
    await a.settle("first", ENDED("removed"));         // stale: the timed-out attempt
    a.snap("stale-answer");
    await a.settle("second", ERASE_FAILED);
    a.snap("retry-failed");
    await a.clickRetryErase();
    a.snap("final");
  };

  // A reply carrying files shows them as download links; malformed entries are dropped.
  var GOOD_FILE = { id: "AbCdEfGhIjKlMnOpQrStUv", filename: "launch-plan.pptx", size: 233472,
                    media_type: "application/vnd.openxmlformats-officedocument.presentationml.presentation" };
  SCENARIOS.files_in_reply = async function (a) {
    a.respondWith({ status: 200, json: { reply: "Here is the deck.", session_id: "s-1", files: [
      GOOD_FILE,
      { id: "../../api/account/logout", filename: "x.pdf", size: 1, media_type: "application/pdf" },
      { id: "AAAAAAAAAAAAAAAAAAAAAA", filename: "<img src=x onerror=alert(1)>.pdf", size: 2048,
        media_type: "application/pdf" },
      { id: "BBBBBBBBBBBBBBBBBBBBBB", filename: "", size: 1, media_type: "application/pdf" },
      "not an object"
    ] } });
    await a.send("make a deck");
    a.snap("reply-page");
    a.files("reply");
  };

  // A files deployment and an account with access: attach, then send the attachment with a message.
  SCENARIOS.files_offered = async function (a) {
    await a.wait(60);
    a.files("offered");
    a.respondWith({ status: 201, json: { file: { id: "CcCcCcCcCcCcCcCcCcCcCc", filename: "budget.xlsx", size: 9000,
                                                 media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" } } },
                  { status: 200, json: { reply: "Updated.", session_id: "s-1", files: [GOOD_FILE] } });
    await a.chooseFile("budget.xlsx", "PK fake workbook");
    a.files("attached");
    await a.send("raise the rent line by 5%");
    a.snap("sent-page");
    a.files("sent");
  };

  // A ready file: its card (type, name, state, remove), the next step under it, focus back in the composer.
  // Choosing the file sends nothing; Summarize sends one explicit request and leaves the draft alone.
  function card(a, label) {
    var hint = $("attach-hint");
    a.push({
      label: label,
      cards: Array.prototype.map.call(document.querySelectorAll("#attachments .attachment"), function (li) {
        return { name: text(li.querySelector(".attachment-name")), state: text(li.querySelector(".attachment-state")),
                 icon: !!li.querySelector(".attachment-icon svg"),
                 remove: li.querySelector(".attachment-remove").getAttribute("aria-label") };
      }),
      hint: hint && !hint.hidden ? [text(hint.firstChild), text(hint.lastChild)] : null,
      focused: document.activeElement && document.activeElement.id,
      draft: $("composer-input").value,
      requestCount: requests.length
    });
  }
  SCENARIOS.files_summarize = async function (a) {
    await a.wait(60);
    a.respondWith({ status: 201, json: { file: { id: "CcCcCcCcCcCcCcCcCcCcCc", filename: "Q3 report.pdf", size: 9000,
                                                 media_type: "application/pdf" } } },
                  ok("A summary.", "s-1"));
    await a.chooseFile("Q3 report.pdf", "%PDF-1.7 synthetic");
    await a.wait(40);
    card(a, "ready");
    $("composer-input").value = "my unsent draft";
    $("attach-summarize").click();
    await a.wait(120);
    card(a, "summarized");
    a.snap("summarized-page");
  };

  // Images on: an image is attached, its stored clean copy is previewed, and it goes with the message.
  SCENARIOS.images_attach_send = async function (a) {
    await a.wait(60);
    a.files("offered");
    a.respondWith({ status: 201, json: { file: { id: "DdDdDdDdDdDdDdDdDdDdDd", filename: "receipt.jpg", size: 4000,
                                                 media_type: "image/jpeg" } } },
                  { status: 200, contentType: "image/jpeg", body: "synthetic-jpeg" },
                  ok("It is a receipt for 3 items.", "s-1"));
    await a.chooseFile("receipt.jpeg", "\xff\xd8\xff synthetic");
    await a.wait(80);
    a.files("attached");
    await a.send("what is this?");
    a.snap("sent-page");
    a.files("sent");
  };

  // Images on: a refused image shows one fixed line and attaches nothing; the next success clears it.
  SCENARIOS.images_refused = async function (a) {
    await a.wait(60);
    a.respondWith({ status: 422, json: { error: "animated_image_unsupported", detail: SECRET } });
    await a.chooseFile("dance.gif", "GIF89a synthetic");
    a.snap("refused-page");
    a.files("refused");
    a.respondWith({ status: 422, json: { error: "something_new", detail: SECRET } });
    await a.chooseFile("odd.png", "\x89PNG synthetic");
    a.files("unknown-code");
    a.respondWith({ status: 201, json: { file: { id: "EeEeEeEeEeEeEeEeEeEeEe", filename: "notes.txt", size: 10,
                                                 media_type: "text/plain; charset=utf-8" } } });
    await a.chooseFile("notes.txt", "hello");
    a.files("accepted");
  };

  // A held-back edit: the reply offers a confirmation; confirming sends one fixed message with the
  // confirmation id and the file, keeps the composer's draft, and cannot be pressed twice.
  SCENARIOS.files_edit_confirm = async function (a) {
    await a.wait(60);
    a.respondWith(
      { status: 200, json: { reply: "The total would no longer add up, so I held that edit back.", session_id: "s-1",
                             files: [], confirmations: [{ id: "conf_1-x", file_id: "CcCcCcCcCcCcCcCcCcCcCc" },
                                                        { id: "<b>bad</b>", file_id: "CcCcCcCcCcCcCcCcCcCcCc" }] } },
      { status: 200, json: { reply: "Applied.", session_id: "s-1", files: [GOOD_FILE] } });
    await a.send("set the rent to 900");
    $("composer-input").value = "my unsent draft";
    var buttons = document.querySelectorAll(".edit-confirm-btn");
    steps.push({ label: "offered", buttons: buttons.length, text: text(document.querySelector(".edit-confirm")) });
    buttons[0].click();
    buttons[0].click();
    await a.wait(120);
    a.snap("confirmed-page");
    steps.push({ label: "confirmed", draft: $("composer-input").value,
                 disabled: Array.prototype.map.call(document.querySelectorAll(".edit-confirm-btn"),
                                                    function (b) { return b.disabled; }) });
  };

  // A files deployment, but this account has no access: nothing is offered.
  SCENARIOS.files_offered_no_access = async function (a) {
    await a.wait(60);
    a.snap("not-offered-page");
    a.files("not-offered");
  };

  // Reasoning modes on: Balanced by default, a chosen mode goes with the message, arrows move the choice.
  SCENARIOS.reasoning_offered = async function (a) {
    a.modes("start");
    a.respondWith(ok("Quick answer.", "s-1"), ok("Deep answer.", "s-1"));
    await a.send("first question");
    await a.clickMode("deep");
    a.modes("deep-clicked");
    await a.keyOnMode("deep", "ArrowRight");     // Wraps to Quick.
    a.modes("arrow-right");
    await a.keyOnMode("quick", "ArrowLeft");     // Back to Deep.
    a.modes("arrow-left");
    await a.send("second question");
    a.snap("sent");
  };

  // A Free account: Deep is shown as part of Premium, cannot be chosen by click or keys, and is never sent.
  SCENARIOS.reasoning_free_account = async function (a) {
    await a.wait(60);
    a.modes("start");
    await a.clickMode("deep");
    a.modes("deep-clicked");
    await a.keyOnMode("balanced", "ArrowRight");   // Skips Deep, wraps to Quick.
    a.modes("arrow-right");
    await a.keyOnMode("quick", "ArrowLeft");       // Skips Deep, back to Balanced.
    a.modes("arrow-left");
    a.respondWith(ok("Answer.", "s-1"));
    await a.send("question");
    a.snap("sent");
  };

  // Learn more, beside the Premium note: a modal dialog with fixed copy; Escape and Close both close it and
  // return focus to Learn more; choosing nothing changes the selected mode. With accounts on it also links
  // to the plan, in a new tab.
  function modesDialogState(label) {
    var dialog = $("modes-dialog");
    var modal = false;
    try { modal = dialog.open && dialog.matches(":modal"); } catch (e) { modal = null; }
    var explore = $("modes-explore");
    var rect = dialog.getBoundingClientRect();
    steps.push({
      label: label, open: dialog.open, modal: modal,
      labelledBy: dialog.getAttribute("aria-labelledby"), title: text($("modes-dialog-title")),
      terms: Array.prototype.map.call(dialog.querySelectorAll("dt"), function (e) { return text(e); }),
      tags: Array.prototype.map.call(dialog.querySelectorAll("[data-mode-tag]"), function (e) {
        return [e.getAttribute("data-mode-tag"), e.hidden ? null : text(e).trim(), e.classList.contains("is-unoffered")];
      }),
      descriptions: Array.prototype.map.call(dialog.querySelectorAll("dd"), function (e) { return text(e); }),
      note: text($("modes-dialog-note")),
      explore: explore && !explore.hidden && explore.offsetParent !== null
        ? [text(explore), explore.getAttribute("href"), explore.getAttribute("target"), explore.getAttribute("rel")] : null,
      focused: document.activeElement ? document.activeElement.id : null,
      checked: Array.prototype.map.call($("mode-picker").querySelectorAll('[aria-checked="true"]'),
                                        function (o) { return o.getAttribute("data-mode"); }),
      inView: !dialog.open || (rect.top >= -0.5 && rect.bottom <= window.innerHeight + 0.5 &&
                               rect.left >= -0.5 && rect.right <= window.innerWidth + 0.5),
      overflow: document.documentElement.scrollWidth - window.innerWidth,
      small: Array.prototype.filter.call(dialog.querySelectorAll("a, button"), function (e) {
        return e.offsetParent !== null && e.getBoundingClientRect().height < 44;
      }).map(text),
      requestCount: requests.length
    });
  }
  async function learnMore(a) {
    await a.wait(60);
    await a.clickMode("deep");
    a.modes("deep-clicked");
    modesDialogState("before");
    $("mode-learn").focus();
    $("mode-learn").click();
    await a.wait(40);
    modesDialogState("opened");
    // Escape, as the browser delivers it to a modal dialog (a cancel event, then it closes).
    var dialog = $("modes-dialog");
    if (dialog.dispatchEvent(new Event("cancel", { cancelable: true }))) dialog.close();
    await a.wait(40);
    modesDialogState("escaped");
    $("mode-learn").click();
    await a.wait(40);
    $("modes-close").click();
    await a.wait(40);
    modesDialogState("closed");
    a.respondWith(ok("Answer.", "s-1"));
    await a.send("question");
    a.snap("sent");
  }
  SCENARIOS.reasoning_learn_more = learnMore;
  SCENARIOS.reasoning_learn_more_accounts = learnMore;
  SCENARIOS.reasoning_learn_more_360x640 = learnMore;

  // A chat reply of session_ended forgets the ended session id.
  SCENARIOS.session_ended_reply = async function (a) {
    a.respondWith(ok("Answer.", "s-1"), { status: 409, json: { error: "session_ended" } }, ok("Fresh.", "s-2"));
    await a.send("question one");
    await a.send("question two");
    a.snap("ended");
    await a.send("question three");
    a.snap("after-new-send");
  };

  // ---- Spaces -----------------------------------------------------------------------
  var SPACE_A = "11111111-1111-4111-8111-111111111111";
  var SPACE_B = "22222222-2222-4222-8222-222222222222";
  var CONV_A = "33333333-3333-4333-8333-333333333333";
  var CONV_B = "44444444-4444-4444-8444-444444444444";
  var AT = "2026-10-30T12:00:00Z";

  function spacesList(items) { return { status: 200, json: { spaces: items, next_cursor: null } }; }
  function conversationsList(items) { return { status: 200, json: { conversations: items, next_cursor: null } }; }
  function savedMessage(seq, role, content) {
    return { id: "55555555-5555-4555-8555-55555555555" + seq, seq: seq, role: role, content: content, created_at: AT };
  }
  function savedExchange(content) {
    return { status: 200, json: { user_message: savedMessage(3, "user", "x"),
                                  assistant_message: savedMessage(4, "assistant", content),
                                  conversation: { id: CONV_A } } };
  }
  function openResearchPlan(a) {
    a.respondWith(
      spacesList([{ id: SPACE_A, name: "Research", conversation_count: 1, updated_at: AT },
                  { id: "../../api/chat", name: "Not an id", conversation_count: 0, updated_at: AT },
                  { id: SPACE_B, name: "<img src=x onerror=alert(1)>", conversation_count: 0, updated_at: AT }]),
      conversationsList([{ id: CONV_A, title: "Plan", message_count: 2, last_message_at: AT, updated_at: AT }]),
      { status: 200, json: { conversation: { id: CONV_A, title: "Plan" }, next_before_seq: null, messages: [
        savedMessage(1, "user", "Earlier question"),
        savedMessage(2, "assistant", "Earlier **answer** <script>alert(1)</script>")] } });
  }

  // Where the customer is and what the header offers: the address (beyond ?scenario=), the account menu,
  // and which Spaces items are marked as open now.
  function where(a, label) {
    var menu = document.querySelector(".app-header .account-menu");
    var list = $("account-menu-list");
    var dialog = $("spaces-dialog");
    a.push({
      label: label,
      address: location.search.replace(/^\?scenario=[a-z0-9_]+/, ""),
      plainLinks: document.querySelectorAll(".app-header a.account-link").length,
      menuButton: menu ? text($("account-menu-btn")) : null,
      expanded: menu ? $("account-menu-btn").getAttribute("aria-expanded") : null,
      listShown: !!list && !list.hidden,
      items: list ? Array.prototype.map.call(list.querySelectorAll(".account-menu-item"), function (e) {
        return [text(e), e.getAttribute("href")];
      }) : [],
      open: Array.prototype.map.call(document.querySelectorAll("#spaces-list .spaces-item.is-open"), function (b) {
        return [b.textContent, b.getAttribute("aria-current")];
      }),
      dialogOpen: !!(dialog && dialog.open),
      barShown: !$("space-bar").hidden,
      plan: text($("app-plan")),
      requestCount: requests.length
    });
  }

  SCENARIOS.spaces_signed_out_accounts = async function (a) {
    await a.wait(60);
    where(a, "loaded");
    a.snap("idle");
  };

  SCENARIOS.spaces_account_menu = async function (a) {
    await a.wait(60);
    where(a, "loaded");
    $("account-menu-btn").click();
    await a.wait(20);
    where(a, "menu-open");
    $("account-menu-btn").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await a.wait(20);
    where(a, "menu-escaped");
    openResearchPlan(a);
    await a.clickSpaces();
    await a.clickSpacesItem(0);
    await a.clickSpacesItem(0);
    where(a, "opened");
    // The lists mark the open Space and conversation; choosing the open conversation goes back to it.
    a.respondWith(spacesList([{ id: SPACE_A, name: "Research", conversation_count: 1, updated_at: AT },
                              { id: SPACE_B, name: "Trip", conversation_count: 0, updated_at: AT }]),
                  conversationsList([{ id: CONV_A, title: "Plan", message_count: 2, last_message_at: AT, updated_at: AT },
                                     { id: CONV_B, title: "Other", message_count: 0, last_message_at: AT, updated_at: AT }]));
    await a.clickSpaces();
    await until(function () { return document.querySelectorAll("#spaces-list .spaces-item").length === 2; }, "relisted");
    where(a, "relisted");
    await a.clickSpacesItem(0);
    await until(function () {
      return text($("spaces-dialog-title")) === "Research" && document.querySelector("#spaces-list .spaces-item");
    }, "space view");
    where(a, "space-view");
    await a.clickSpacesItem(0);
    where(a, "returned");
    // The Space bar's Conversations opens the open Space's own list directly.
    a.respondWith(conversationsList([{ id: CONV_A, title: "Plan", message_count: 2, last_message_at: AT, updated_at: AT }]));
    $("space-conversations").click();
    await a.wait(80);
    a.spaces("bar-conversations");
    $("spaces-close").click();
    await a.wait(20);
    // New conversation: one step, in the same Space.
    a.respondWith({ status: 201, json: { conversation: { id: CONV_B, title: null } } });
    $("space-new").click();
    await a.wait(80);
    where(a, "new-conversation");
    await a.leaveSpace();
    where(a, "left");
    // Sign out: one same-origin POST; a refusal says so and leaves the page as it is.
    $("account-menu-btn").click();
    a.respondWith({ status: 503, json: { error: "unavailable" } });
    $("account-menu-sign-out").click();
    await a.wait(80);
    where(a, "sign-out-refused");
    a.snap("idle");
  };

  SCENARIOS.spaces_restore_from_address = async function (a) {
    a.respondWith(spaceRecord("Research", "", 1),
      { status: 200, json: { conversation: { id: CONV_A, title: "Plan" }, next_before_seq: null, messages: [
        savedMessage(1, "user", "Earlier question"), savedMessage(2, "assistant", "Earlier answer")] } });
    await a.wait(120);
    where(a, "restored");
    a.spaces("restored-bar");
    a.snap("restored-view");
  };

  SCENARIOS.spaces_restore_refused = async function (a) {
    a.respondWith({ status: 404, json: { error: "not_found" } });
    await a.wait(120);
    where(a, "refused");
    a.snap("refused-view");
  };

  SCENARIOS.spaces_list_from_address = async function (a) {
    a.respondWith(spacesList([{ id: SPACE_A, name: "Research", conversation_count: 1, updated_at: AT }]));
    await a.wait(120);
    where(a, "listed");
    a.spaces("listed-dialog");
    a.snap("idle");
  };

  // No pile of empty conversations: an untitled Start reuses the Space's empty one, New conversation on an
  // empty conversation stays put, and a saved exchange's (automatic) title reaches the Space bar.
  SCENARIOS.spaces_no_empty_pile = async function (a) {
    await a.wait(60);
    a.respondWith(spacesList([{ id: SPACE_A, name: "Research", conversation_count: 1, updated_at: AT }]),
                  conversationsList([{ id: CONV_A, title: "New conversation", message_count: 0, last_message_at: null,
                                      updated_at: AT }]));
    await a.clickSpaces();
    await a.clickSpacesItem(0);
    await until(function () { return document.querySelector("#spaces-list .spaces-item"); }, "conversations");
    a.push({ label: "space-view", submit: text($("spaces-new-submit")), hintShown: !$("spaces-dialog-hint").hidden,
             placeholder: $("spaces-new-name").placeholder, requestCount: requests.length });
    a.respondWith({ status: 200, json: { conversation: { id: CONV_A, title: "New conversation" },
                                         next_before_seq: null, messages: [] } });
    await a.createInSpaces("");
    await until(function () { return !$("space-bar").hidden; }, "reused conversation opened");
    a.spaces("reused");
    var before = requests.length;
    $("space-new").click();
    await a.wait(60);
    a.push({ label: "new-on-empty", added: requests.length - before, focused: document.activeElement.id });
    a.respondWith({ status: 200, json: { user_message: savedMessage(1, "user", "x"),
                                         assistant_message: savedMessage(2, "assistant", "Saved reply."),
                                         conversation: { id: CONV_A, title: "Plan the product launch" } } });
    await a.send("Plan the product launch");
    await a.wait(60);
    a.spaces("titled");
    a.snap("idle");
  };

  // The account may use Spaces: open a saved conversation, send in it, leave it.
  SCENARIOS.spaces_open_send_leave = async function (a) {
    await a.wait(60);
    a.spaces("loaded");
    a.snap("idle");
    openResearchPlan(a);
    await a.clickSpaces();
    a.spaces("listed");
    await a.clickSpacesItem(0);
    a.spaces("space");
    await a.clickSpacesItem(0);
    a.spaces("opened");
    a.snap("opened");
    a.respondWith(savedExchange("Saved reply."));
    await a.send("New question");
    await a.wait(60);
    a.snap("sent");
    await a.leaveSpace();
    a.spaces("left");
    a.snap("left");
  };

  function spaceRecord(name, instructions, version) {
    return { status: 200, json: { space: { id: SPACE_A, name: name, instructions: instructions, version: version,
                                           instructions_updated_at: null, created_at: AT, updated_at: AT } } };
  }
  function manage(label) {
    var buttons = Array.prototype.map.call(document.querySelectorAll("#spaces-manage button"), text);
    var field = document.getElementById("spaces-edit-field");
    // Layout, for the phone-sized run: controls in the open dialog under a 44 px target, the page's
    // horizontal overflow, and whether the name field's placeholder fits inside it.
    var small = Array.prototype.filter.call(
      document.querySelectorAll("dialog[open] button, dialog[open] input, dialog[open] textarea"),
      function (e) { return e.offsetParent !== null && e.getBoundingClientRect().height < 44; }
    ).map(function (e) { return text(e) || e.id; });
    var input = $("spaces-new-name");
    var style = getComputedStyle(input);
    var ctx = document.createElement("canvas").getContext("2d");
    ctx.font = style.font;
    // A field that is not shown (the new-conversation field steps aside while the Space is edited) has no
    // placeholder to fit; newShown says which it is.
    var newShown = input.offsetParent !== null;
    var room = newShown ? input.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight) : Infinity;
    var toggle = $("spaces-edit-toggle");
    var shown = function (id) { return !!$(id) && $(id).offsetParent !== null; };
    var dialog = $("spaces-dialog").getBoundingClientRect();
    steps.push({ label: label, buttons: buttons, field: field ? field.value : null,
                 mode: $("spaces-dialog-panel").getAttribute("data-mode"), newShown: newShown,
                 toggle: toggle && toggle.offsetParent !== null ? text(toggle) : null,
                 groups: ["spaces-manage-label", "spaces-list-label", "spaces-new-heading"].filter(shown)
                   .map(function (id) { return text($(id)); }),
                 listShown: shown("spaces-list") || shown("spaces-list-head"),
                 closeInView: $("spaces-close").getBoundingClientRect().bottom <= window.innerHeight + 0.5,
                 dialogInView: dialog.top >= -0.5 && dialog.bottom <= window.innerHeight + 0.5,
                 title: text($("spaces-dialog-title")), status: text($("spaces-status")),
                 tone: $("spaces-status").getAttribute("data-tone"),
                 barName: text($("space-bar-name")), barShown: !$("space-bar").hidden, requestCount: requests.length,
                 viewport: window.innerWidth, overflow: document.documentElement.scrollWidth - window.innerWidth,
                 small: small, placeholderFits: ctx.measureText(input.placeholder).width <= room + 0.5 });
  }
  // Waits (up to 3 s) for what the page draws after a scripted response, instead of a fixed pause.
  async function until(find, label) {
    for (var waited = 0; waited < 3000; waited += 20) {
      var found = find();
      if (found) return found;
      await wait(20);
    }
    throw new Error("HARNESS: never appeared: " + (label || "?") + " | queue " + queue.length +
                    " | requests " + requests.map(function (r) { return (r.method || "GET") + " " + r.url; }).join(", "));
  }
  async function clickManage(name) {
    var button = await until(function () {
      return Array.prototype.filter.call(document.querySelectorAll("#spaces-manage button, #spaces-list-head button"),
                                         function (b) { return text(b) === name && !b.disabled && b.offsetParent !== null; })[0];
    }, "manage " + name);
    button.click();
    return wait(80);
  }
  async function submitEdit(value) {
    var field = await until(function () { return document.getElementById("spaces-edit-field"); }, "edit field");
    field.value = value;
    document.querySelector("#spaces-manage form").requestSubmit();
    return wait(80);
  }

  // Inside an open Space conversation: rename the Space (the bar follows), a version conflict on the
  // instructions is reported, not overwritten; saved instructions; then delete, which leaves the Space.
  SCENARIOS.spaces_manage = async function (a) {
    await a.wait(60);
    openResearchPlan(a);
    await a.clickSpaces();
    await a.clickSpacesItem(0);
    await a.clickSpacesItem(0);                     // Open "Plan" in "Research".
    await until(function () { return !$("space-bar").hidden; }, "space bar");
    a.respondWith(spacesList([{ id: SPACE_A, name: "Research", conversation_count: 1, updated_at: AT }]),
                  conversationsList([]));
    await a.clickSpaces();
    await a.clickSpacesItem(0);                     // Back in the dialog, in the open Space.
    manage("space-view");
    a.respondWith(spaceRecord("Research", "Be brief.", 3), spaceRecord("Research 2026", "Be brief.", 4));
    await clickManage("Rename");
    manage("renaming");
    await submitEdit("Research 2026");
    manage("renamed");
    a.respondWith(spaceRecord("Research 2026", "Be brief.", 4),
                  { status: 409, json: { error: "version_conflict", detail: SECRET } });
    await clickManage("Instructions");
    manage("instructions");
    await submitEdit("Cite sources.");
    manage("conflict");
    a.respondWith(spaceRecord("Research 2026", "Changed elsewhere.", 5),
                  spaceRecord("Research 2026", "Cite sources.", 6));
    await clickManage("Cancel");
    await clickManage("Instructions");
    await submitEdit("Cite sources.");
    manage("instructions-saved");
    await clickManage("Delete");
    manage("confirm-delete");
    a.respondWith({ status: 204, body: "" }, spacesList([]));
    await clickManage("Delete Space");
    await until(function () { return text($("spaces-status")) === "Space deleted."; }, "deleted status");
    manage("deleted");
    a.snap("deleted-page");
  };

  // The same management, on a 360 px phone (layout recorded at every step).
  SCENARIOS.spaces_manage_360x640 = SCENARIOS.spaces_manage;

  // Reasoning modes on a Spaces deployment: the picker stays in a Space and the chosen mode goes with the
  // message; a retry keeps the mode it was sent with, even after the picker changed.
  SCENARIOS.spaces_with_modes = async function (a) {
    await a.wait(60);
    openResearchPlan(a);
    await a.clickSpaces();
    await a.clickSpacesItem(0);
    await a.clickSpacesItem(0);
    a.modes("in-space");
    await a.clickMode("deep");
    a.respondWith({ status: 503, json: { error: "service_unavailable" } }, savedExchange("Deep answer."));
    await a.send("Hard question");
    await a.wait(60);
    await a.clickMode("quick");
    await a.clickAction("Retry");
    await a.wait(60);
    a.snap("retried");
  };

  // Editing a Space's conversations: rename one (with the version read first; the open conversation's
  // bar follows), then delete the open one, which leaves it.
  SCENARIOS.spaces_conversations_edit = async function (a) {
    await a.wait(60);
    openResearchPlan(a);
    await a.clickSpaces();
    await a.clickSpacesItem(0);
    await a.clickSpacesItem(0);                     // Open "Plan".
    await until(function () { return !$("space-bar").hidden; }, "space bar");
    a.respondWith(spacesList([{ id: SPACE_A, name: "Research", conversation_count: 2, updated_at: AT }]),
                  conversationsList([{ id: CONV_A, title: "Plan", message_count: 2, last_message_at: AT, updated_at: AT },
                                     { id: CONV_B, title: "Notes", message_count: 0, last_message_at: null,
                                       updated_at: AT }]));
    await a.clickSpaces();
    await a.clickSpacesItem(0);
    await clickManage("Edit conversations");
    steps.push({ label: "editing", mode: $("spaces-dialog-panel").getAttribute("data-mode"),
                 toggle: text($("spaces-edit-toggle")), newShown: $("spaces-new-name").offsetParent !== null,
                 rows: Array.prototype.map.call(
      document.querySelectorAll("#spaces-list .spaces-edit-row .spaces-item-name"), function (r) { return text(r); }) });
    a.respondWith({ status: 200, json: { conversation: { id: CONV_A, title: "Plan", version: 2 }, messages: [],
                                         next_before_seq: null } },
                  { status: 200, json: { conversation: { id: CONV_A, title: "Launch plan", version: 3 } } });
    document.querySelectorAll("#spaces-list .spaces-edit-row")[0].querySelector("button").click();
    var field = await until(function () { return document.getElementById("spaces-rename-conversation"); }, "rename");
    field.value = "Launch plan";
    field.form.requestSubmit();
    await until(function () { return text($("spaces-status")) === "Renamed."; }, "renamed");
    steps.push({ label: "renamed", bar: text($("space-bar-title")), rows: Array.prototype.map.call(
      document.querySelectorAll("#spaces-list .spaces-edit-row .spaces-item-name"), function (r) { return text(r); }) });
    a.respondWith({ status: 204, body: "" });
    document.querySelectorAll("#spaces-list .spaces-edit-row")[0].querySelectorAll("button")[1].click();
    var confirmDelete = await until(function () {
      return Array.prototype.filter.call(document.querySelectorAll("#spaces-list button"),
                                         function (b) { return text(b) === "Delete conversation"; })[0];
    }, "confirm delete");
    confirmDelete.click();
    await until(function () { return text($("spaces-status")) === "Conversation deleted."; }, "deleted");
    steps.push({ label: "deleted", barShown: !$("space-bar").hidden, rows: Array.prototype.map.call(
      document.querySelectorAll("#spaces-list .spaces-edit-row .spaces-item-name"), function (r) { return text(r); }) });
    a.snap("deleted-page");
  };

  // A long saved conversation: earlier messages load above, each turn keeps Copy, the cursor moves on.
  SCENARIOS.spaces_earlier_messages = async function (a) {
    await a.wait(60);
    a.respondWith(
      spacesList([{ id: SPACE_A, name: "Research", conversation_count: 1, updated_at: AT }]),
      conversationsList([{ id: CONV_A, title: "Plan", message_count: 6, last_message_at: AT, updated_at: AT }]),
      { status: 200, json: { conversation: { id: CONV_A, title: "Plan" }, next_before_seq: 5, messages: [
        savedMessage(5, "user", "Third question"), savedMessage(6, "assistant", "Third answer")] } },
      { status: 200, json: { conversation: { id: CONV_A, title: "Plan" }, next_before_seq: null, messages: [
        savedMessage(1, "user", "First question"), savedMessage(2, "assistant", "First answer"),
        savedMessage(3, "user", "Second question"), savedMessage(4, "assistant", "Second answer")] } });
    await a.clickSpaces();
    await a.clickSpacesItem(0);
    await a.clickSpacesItem(0);
    a.snap("opened");
    steps.push({ label: "earlier-shown", count: document.querySelectorAll(".spaces-earlier").length });
    document.querySelector(".spaces-earlier").click();
    await a.wait(120);
    a.snap("expanded");
    steps.push({ label: "earlier-after", count: document.querySelectorAll(".spaces-earlier").length,
                 copies: document.querySelectorAll("#thread .msg-actions").length });
  };

  // A failed send and a stopped one are retried with the same idempotency key; the next message gets
  // a new one; a saved answer has Copy and no Retry.
  SCENARIOS.spaces_retry_keeps_the_key = async function (a) {
    await a.wait(60);
    openResearchPlan(a);
    await a.clickSpaces();
    await a.clickSpacesItem(0);
    await a.clickSpacesItem(0);
    a.respondWith({ status: 503, json: { error: "service_unavailable", detail: SECRET } }, savedExchange("Saved once."),
                  { hang: true }, savedExchange("Second."));
    await a.send("Question");
    await a.wait(60);
    a.snap("failed");
    await a.clickAction("Retry");
    await a.wait(60);
    a.snap("retried");
    await a.send("Another");
    await a.stop();
    await a.wait(60);
    await a.clickAction("Retry");
    await a.wait(60);
    a.snap("after-stop");
  };

  // Create a Space, then a conversation in it: the new conversation opens without loading anything.
  SCENARIOS.spaces_create = async function (a) {
    await a.wait(60);
    a.respondWith(spacesList([]),
                  { status: 201, json: { space: { id: SPACE_B, name: "Trip", instructions: "" } } },
                  conversationsList([]),
                  { status: 201, json: { conversation: { id: CONV_B, title: null } } });
    await a.clickSpaces();
    a.spaces("empty");
    await a.createInSpaces("  Trip  ");
    a.spaces("created-space");
    await a.createInSpaces("");
    a.spaces("created-conversation");
    a.snap("idle");
  };

  // On a 360 px phone, the logo, Account-style buttons and the status icon fit in the header without
  // overlapping or overflowing.
  SCENARIOS.spaces_header_360x640 = async function (a) {
    await a.wait(80);
    var logo = document.getElementById("brand-logo").getBoundingClientRect();
    var end = document.querySelector(".app-header-end").getBoundingClientRect();
    var button = document.querySelector(".app-header .spaces-button").getBoundingClientRect();
    var status = document.getElementById("session-status");
    a.push({ label: "header", viewport: window.innerWidth, logoRight: logo.right, endLeft: end.left,
             endRight: end.right, buttonRight: button.right, scrollWidth: document.documentElement.scrollWidth,
             statusText: status.textContent.replace(/\s+/g, " ").trim(),
             statusIconShown: getComputedStyle(status.querySelector("svg")).display !== "none" });
    a.snap("idle");
  };

  // Spaces on, but this account may not use them: no button, and the chat is unchanged.
  SCENARIOS.spaces_not_offered = async function (a) {
    await a.wait(60);
    a.spaces("loaded");
    a.respondWith(ok("Answer.", "s-1"));
    await a.send("question");
    a.snap("idle");
  };

  // The save status follows only what the server confirmed: saved rows on opening, "Generating…" while a
  // send is in flight, "Saved" for a 200 carrying both stored rows, "not saved" for a named refusal, and
  // "not confirmed" for anything the page cannot know (stopped, lost, a 404, a 200 without stored rows).
  SCENARIOS.spaces_save_status = async function (a) {
    await a.wait(60);
    openResearchPlan(a);
    await a.clickSpaces();
    await a.clickSpacesItem(0);
    await a.clickSpacesItem(0);
    a.spaces("opened");
    a.respondWith({ defer: "first" });
    await a.send("First");
    a.spaces("generating");
    await a.settle("first", savedExchange("Saved one."));
    await a.wait(60);
    a.spaces("saved");
    a.respondWith({ status: 429, json: { error: "account_daily_limit_reached" } });
    await a.send("Second");
    await a.wait(60);
    a.spaces("refused");
    a.respondWith({ hang: true });
    await a.send("Third");
    await a.stop();
    await a.wait(60);
    a.spaces("stopped");
    a.respondWith(savedExchange("Saved three."));
    await a.clickAction("Retry");
    await a.wait(60);
    a.spaces("retried");
    a.respondWith({ reject: "RAW-SECRET network" });
    await a.send("Fourth");
    await a.wait(60);
    a.spaces("lost");
    a.respondWith({ status: 404, json: { error: "not_found" } });
    await a.clickAction("Retry");
    await a.wait(60);
    a.spaces("not-found");
    a.respondWith({ status: 200, json: { assistant_message: { content: "An answer with no stored rows." },
                                         conversation: { id: CONV_A } } });
    await a.clickAction("Retry");
    await a.wait(60);
    a.spaces("no-rows");
    a.respondWith({ status: 503, json: { error: "model_provider_unavailable", detail: SECRET } });
    await a.send("Fifth");
    await a.wait(60);
    a.spaces("provider-failed");
    a.snap("provider-failed-page");
    // A new, empty conversation has nothing to save yet; leaving resets the bar.
    a.respondWith({ status: 201, json: { conversation: { id: CONV_B, title: "New conversation" } } });
    $("space-new").click();
    await a.wait(80);
    a.spaces("new-empty");
    await a.leaveSpace();
    a.spaces("left");
  };

  function manySpaces(count, cursor) {
    var names = ["Research", "Recipes", "Work stuff", "Calculus", "Travel plans", "research notes", "Garden",
                 "Budget 2026", "Reading list", "Python scripts"];
    var items = [];
    for (var i = 0; i < count; i++) {
      var id = (i + 16).toString(16);
      items.push({ id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaa" + id, conversation_count: i,
                   name: i < names.length ? names[i] : "Space " + (i + 1), updated_at: AT });
    }
    return { status: 200, json: { spaces: items, next_cursor: cursor || null } };
  }
  function searchState(label) {
    var input = $("spaces-search-input");
    steps.push({
      label: label, searchShown: !$("spaces-search").hidden, value: input.value,
      placeholder: input.placeholder, type: input.type, labelled: !!document.querySelector('label[for="spaces-search-input"]'),
      hintShown: !$("spaces-dialog-hint").hidden, status: text($("spaces-status")),
      tone: $("spaces-status").getAttribute("data-tone"),
      items: Array.prototype.map.call(document.querySelectorAll("#spaces-list .spaces-item .spaces-item-name"), text),
      title: text($("spaces-dialog-title")), dialogOpen: $("spaces-dialog").open,
      height: Math.round($("spaces-dialog").getBoundingClientRect().height),
      focused: document.activeElement ? (document.activeElement.id || text(document.activeElement)) : null,
      requestCount: requests.length
    });
  }
  async function typeSearch(value) {
    var input = $("spaces-search-input");
    input.focus();
    input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return wait(20);
  }
  function key(target, name) {
    return target.dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true }));
  }

  // Search Spaces: every Space of the account is loaded (a second page, if the server has one, is followed),
  // and typing filters them on the page: case-insensitive, no request, a helpful empty state, and the
  // keyboard (Escape clears first, Down moves into the list, Enter opens the first match).
  SCENARIOS.spaces_search_1280x800 = async function (a) {
    await a.wait(60);
    var first = manySpaces(6, "cursor-PAGE_2");
    var second = { status: 200, json: { spaces: [
      { id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1", name: "Older research", conversation_count: 3, updated_at: AT },
      { id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2", name: "<b>Markup</b>", conversation_count: 0, updated_at: AT }],
      next_cursor: null } };
    a.respondWith(first, second);
    await a.clickSpaces();
    await until(function () { return document.querySelectorAll("#spaces-list .spaces-item").length === 8; }, "all spaces");
    searchState("listed");
    await typeSearch("RESEARCH");
    searchState("typed");
    await typeSearch("  rk  ");
    searchState("trimmed");
    await typeSearch("zebra");
    searchState("no-match");
    var escaped = key($("spaces-search-input"), "Escape");
    await a.wait(20);
    searchState("escape-clears");
    steps.push({ label: "escape-default", prevented: !escaped });
    await typeSearch("calc");
    key($("spaces-search-input"), "ArrowDown");
    await a.wait(20);
    searchState("arrow-down");
    key(document.activeElement, "ArrowUp");
    await a.wait(20);
    searchState("arrow-up");
    a.respondWith(conversationsList([]));
    key($("spaces-search-input"), "Enter");
    await until(function () { return text($("spaces-dialog-title")) === "Calculus"; }, "opened by Enter");
    searchState("enter-opens");
    // Back: every Space again from the server, with the search still applied and still shown.
    a.respondWith(manySpaces(6));
    await a.spacesBack();
    await until(function () { return document.querySelectorAll("#spaces-list .spaces-item").length > 0; }, "list again");
    searchState("back");
    // A new opening starts with every Space listed.
    $("spaces-close").click();
    await a.wait(40);
    a.respondWith(manySpaces(4));
    await a.clickSpaces();
    await until(function () { return document.querySelectorAll("#spaces-list .spaces-item").length === 4; }, "four spaces");
    searchState("few");
    a.snap("idle");
  };

  // Many Spaces: the dialog stays inside the window, only its list scrolls, and creating, All Spaces and
  // Close stay in view; the arrow keys move through the list.
  function longList(label) {
    var dialog = $("spaces-dialog");
    var body = $("spaces-dialog-body");
    var rect = dialog.getBoundingClientRect();
    var inView = function (el) {
      var r = el.getBoundingClientRect();
      return el.offsetParent !== null && r.top >= rect.top - 0.5 && r.bottom <= rect.bottom + 0.5 &&
             r.bottom <= window.innerHeight + 0.5 && r.top >= -0.5;
    };
    var scrollers = Array.prototype.filter.call(dialog.querySelectorAll("*"), function (el) {
      return el.scrollHeight > el.clientHeight + 1 && /auto|scroll/.test(getComputedStyle(el).overflowY);
    }).map(function (el) { return el.id || el.className; });
    steps.push({
      label: label, viewport: [window.innerWidth, window.innerHeight],
      items: document.querySelectorAll("#spaces-list .spaces-item").length,
      dialogInView: rect.top >= -0.5 && rect.bottom <= window.innerHeight + 0.5,
      dialogScrolls: dialog.scrollHeight > dialog.clientHeight + 1,
      bodyScrolls: body.scrollHeight > body.clientHeight + 1, scrollers: scrollers,
      newInView: inView($("spaces-new-name")) && inView($("spaces-new-submit")),
      closeInView: inView($("spaces-close")), searchInView: inView($("spaces-search-input")),
      titleInView: inView($("spaces-dialog-title")),
      overflow: document.documentElement.scrollWidth - window.innerWidth,
      bodyOverflowX: body.scrollWidth - body.clientWidth,
      small: Array.prototype.filter.call(
        document.querySelectorAll("dialog[open] button, dialog[open] input, dialog[open] textarea"),
        function (e) { return e.offsetParent !== null && e.getBoundingClientRect().height < 44; }
      ).map(function (e) { return text(e) || e.id; }).filter(function (v, i, all) { return all.indexOf(v) === i; }),
      focused: document.activeElement ? (document.activeElement.id || text(document.activeElement.querySelector(".spaces-item-name") || document.activeElement)) : null
    });
  }
  SCENARIOS.spaces_long_list_360x640 = async function (a) {
    await a.wait(60);
    a.respondWith(manySpaces(30));
    await a.clickSpaces();
    await until(function () { return document.querySelectorAll("#spaces-list .spaces-item").length === 30; }, "thirty spaces");
    longList("listed");
    var items = document.querySelectorAll("#spaces-list .spaces-item");
    items[0].focus();
    key(items[0], "ArrowDown");
    await a.wait(20);
    longList("arrow-down");
    key(document.activeElement, "End");
    await a.wait(20);
    longList("end");
    key(document.activeElement, "Home");
    key(document.activeElement, "ArrowUp");
    await a.wait(20);
    longList("up-to-search");
    // One Space with many conversations: the same holds, with its three parts labelled.
    var many = [];
    for (var i = 0; i < 40; i++) {
      many.push({ id: "cccccccc-cccc-4ccc-8ccc-cccccccccc" + (i + 16).toString(16), title: "Conversation " + (i + 1),
                  message_count: 2, last_message_at: AT, updated_at: AT });
    }
    a.respondWith(conversationsList(many));
    await a.clickSpacesItem(0);
    await until(function () { return document.querySelectorAll("#spaces-list .spaces-item").length === 40; }, "forty conversations");
    longList("space");
    manage("space-parts");
    a.snap("idle");
  };
  SCENARIOS.spaces_long_list_1280x800 = SCENARIOS.spaces_long_list_360x640;

  // A Space with more conversations than one page: Show more conversations reads the next page with the
  // server's cursor and adds it; nothing is left out silently, and nothing is listed twice.
  SCENARIOS.spaces_more_conversations = async function (a) {
    await a.wait(60);
    a.respondWith(
      spacesList([{ id: SPACE_A, name: "Research", conversation_count: 3, updated_at: AT }]),
      { status: 200, json: { conversations: [
        { id: CONV_A, title: "Plan", message_count: 2, last_message_at: AT, updated_at: AT },
        { id: CONV_B, title: "Notes", message_count: 4, last_message_at: AT, updated_at: AT }],
        next_cursor: "cursor-NEXT_1" } });
    await a.clickSpaces();
    await a.clickSpacesItem(0);
    await until(function () { return document.querySelector(".spaces-more-row button"); }, "show more");
    var state = function (label) {
      steps.push({ label: label,
        items: Array.prototype.map.call(document.querySelectorAll("#spaces-list .spaces-item"), text),
        more: Array.prototype.map.call(document.querySelectorAll(".spaces-more-row button"), text),
        status: text($("spaces-status")), focused: text(document.activeElement), requestCount: requests.length });
    };
    state("first-page");
    a.respondWith({ status: 200, json: { conversations: [
      { id: CONV_B, title: "Notes", message_count: 4, last_message_at: AT, updated_at: AT },
      { id: "66666666-6666-4666-8666-666666666666", title: "Oldest", message_count: 6, last_message_at: AT,
        updated_at: AT }], next_cursor: null } });
    document.querySelector(".spaces-more-row button").click();
    await until(function () { return document.querySelectorAll("#spaces-list .spaces-item").length === 3; }, "second page");
    state("second-page");
    a.snap("idle");
  };

  // ---- review corrections ---------------------------------------------------------------------------------

  // Ambiguous answers never read as "not saved". A database failure during the save (503 database_unavailable)
  // may have stored the exchange; a retry refused as busy says nothing about the attempt still running. Only
  // the same key returning the stored exchange settles either.
  SCENARIOS.spaces_save_ambiguous = async function (a) {
    await a.wait(60);
    openResearchPlan(a);
    await a.clickSpaces();
    await a.clickSpacesItem(0);
    await a.clickSpacesItem(0);
    a.respondWith({ status: 503, json: { error: "database_unavailable" } });
    await a.send("Committed, acknowledgement lost");
    await a.wait(60);
    a.spaces("commit-acknowledgement-lost");
    a.snap("commit-acknowledgement-lost-page");
    a.respondWith(savedExchange("Recovered committed answer."));
    await a.clickAction("Retry");
    await a.wait(60);
    a.spaces("commit-recovered");
    a.respondWith({ hang: true });
    await a.send("Still running on the server");
    await a.stop();
    await a.wait(60);
    a.spaces("stopped");
    a.respondWith({ status: 409, json: { error: "conversation_busy" } });
    await a.clickAction("Retry");
    await a.wait(60);
    a.spaces("retry-while-original-runs");
    a.respondWith({ status: 429, json: { error: "busy" } });
    await a.clickAction("Retry");
    await a.wait(60);
    a.spaces("retry-refused-still-open");
    a.respondWith(savedExchange("The original did save."));
    await a.clickAction("Retry");
    await a.wait(60);
    a.spaces("reconciled");
    // First attempts (nothing unknown before them): a named refusal does establish that nothing was stored;
    // an answer that speaks of something else, or an unknown code, does not.
    a.respondWith({ status: 409, json: { error: "conversation_busy" } });
    await a.send("Refused while another send holds the conversation");
    await a.wait(60);
    a.spaces("first-attempt-busy");
    a.respondWith({ status: 504, json: { error: "request_timeout" } });
    await a.send("Timed out before the save");
    await a.wait(60);
    a.spaces("first-attempt-timeout");
    a.respondWith({ status: 500, json: { error: "internal_error" } });
    await a.send("An unexpected failure");
    await a.wait(60);
    a.spaces("first-attempt-internal-error");
    a.respondWith({ status: 503, json: { error: "account_unavailable" } });
    await a.send("An account lookup failure");
    await a.wait(60);
    a.spaces("first-attempt-account-unavailable");
    a.respondWith({ status: 409, json: { error: "idempotency_conflict" } });
    await a.send("A key the server already holds");
    await a.wait(60);
    a.spaces("first-attempt-idempotency-conflict");
    a.respondWith({ status: 503, json: { error: "a_code_added_later" } });
    await a.send("A code this page does not know");
    await a.wait(60);
    a.spaces("first-attempt-unknown-code");
    a.respondWith({ status: 502, body: "<html>Bad gateway</html>", contentType: "text/html" });
    await a.send("An answer from something in between");
    await a.wait(60);
    a.spaces("first-attempt-gateway");
    a.snap("end");
  };

  // Start without a title reuses a conversation the list showed as empty. Another tab has since saved an
  // exchange in it: it is read from the server (once) and shown, with its current title, not assumed empty.
  SCENARIOS.spaces_reuse_reads_history = async function (a) {
    await a.wait(60);
    a.respondWith(
      spacesList([{ id: SPACE_A, name: "Research", conversation_count: 1, updated_at: AT }]),
      conversationsList([{ id: CONV_A, title: "New conversation", message_count: 0, last_message_at: null,
                           updated_at: AT }]));
    await a.clickSpaces();
    await a.clickSpacesItem(0);
    await until(function () { return document.querySelector("#spaces-list .spaces-item"); }, "conversations");
    a.type("An unsent draft");
    a.respondWith({ status: 200, json: {
      conversation: { id: CONV_A, title: "Question from the other tab" }, next_before_seq: 7,
      messages: [savedMessage(7, "user", "Question from the other tab"), savedMessage(8, "assistant", "Saved answer")] } });
    await a.createInSpaces("");
    await until(function () { return !$("space-bar").hidden; }, "reused conversation opened");
    a.spaces("reused-existing");
    a.push({ label: "reused-thread", earlier: document.querySelectorAll(".spaces-earlier").length,
             draft: $("composer-input").value, requestCount: requests.length });
    a.snap("reused-page");
    // A conversation this page has just created is still entered without a read.
    a.respondWith(conversationsList([{ id: CONV_A, title: "Question from the other tab", message_count: 2,
                                       last_message_at: AT, updated_at: AT }]),
                  { status: 201, json: { conversation: { id: CONV_B, title: "New conversation" } } });
    $("space-conversations").click();
    await until(function () { return document.querySelector("#spaces-list .spaces-item"); }, "conversations again");
    var before = requests.length;
    await a.createInSpaces("");
    await until(function () { return text($("space-bar-title")) === " · New conversation"; }, "new conversation");
    a.spaces("created-new");
    a.push({ label: "created-requests", added: requests.slice(before).map(function (r) {
      return (r.method || "GET") + " " + r.url; }) });
  };

  // A mode no plan has on this deployment: unavailable, and never described as part of Premium.
  async function unoffered(a) {
    await a.wait(60);
    a.modes("start");
    await a.clickMode("deep");
    a.modes("deep-clicked");
    $("mode-learn").click();
    await a.wait(40);
    modesDialogState("opened");
    $("modes-close").click();
    await a.wait(40);
    a.respondWith(ok("Answer.", "s-1"));
    await a.send("question");
    a.snap("sent");
  }
  SCENARIOS.reasoning_unoffered = unoffered;
  SCENARIOS.reasoning_premium_unstated = unoffered;

  // The keyboard's own route to the explanation: the arrow keys move among the usable modes and skip the
  // unavailable one, yet Learn more is in the page and the tab order (out of sight until focused) from the
  // moment the server's answer arrives, with no click on the unavailable mode. The real key presses are in
  // test_frontend_error_contract.py (this page only waits, for a browser driven by its keyboard).
  SCENARIOS.reasoning_keyboard = async function (a) {
    await a.wait(60);
    a.modes("start");
    var reached = [];
    document.querySelector('.mode-option[aria-checked="true"]').focus();
    for (var i = 0; i < 4; i++) {
      document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true }));
      reached.push(document.activeElement.getAttribute("data-mode"));
    }
    a.modes("after-arrows");
    a.push({ label: "arrow-path", reached: reached });
    $("mode-learn").focus();                       // What Tab does next; the real key is pressed in the test.
    a.modes("learn-focused");
    a.respondWith(ok("Answer.", "s-1"));
    await a.send("question");
    a.snap("sent");
  };
  SCENARIOS.reasoning_keyboard_live = async function (a) {
    a.snap("idle");
    await a.wait(600000);                          // Left open for the test's real key presses.
  };

  // ---- run ---------------------------------------------------------------------
  window.addEventListener("load", function () {
    var scenario = SCENARIOS[scenarioName];
    var finish = function (error) {
      window.__HARNESS_RESULT__ = {
        scenario: scenarioName,
        error: error || null,
        steps: steps,
        requests: requests,
        modeRequests: modeRequests,
        planRequests: planRequests,
        copied: copied,
        eraseTimers: eraseTimers.map(function (t) { return { cleared: t.cleared, fired: t.fired }; })
      };
    };

    if (!scenario) { finish("unknown scenario"); return; }

    Promise.resolve()
      .then(function () { return scenario(api); })
      .then(function () { finish(null); },
            function (e) { finish(String(e && e.message || e)); });
  });
})();
