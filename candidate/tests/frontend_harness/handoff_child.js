/* Chat-side instrumentation for the homepage handoff browser test.

   Loaded by test_homepage_handoff.py into the real frontend/index.html,
   before app.js. It never touches app.js internals:
   - replaces fetch with a scripted /api/chat (300 ms per reply; a message
     longer than 4,000 characters gets the backend's 422 message_too_long);
   - records every attempted browser-storage or cookie write;
   - when the chat is a top-level page (a direct visit or a refresh of
     /app/), checks that it starts empty and standalone, sends one message,
     and reports the result to the loopback test server. */
(function () {
  "use strict";

  var realFetch = window.fetch.bind(window);
  var state = window.__HANDOFF_CHILD__ = {
    requests: [], writes: [], received: [], loadedAt: Date.now()
  };

  instrumentWrites(window, state.writes);

  // Every message this window receives, so each rejection check can prove
  // its message was actually delivered.
  window.addEventListener("message", function (event) {
    var data = event.data;
    state.received.push({
      origin: event.origin,
      fromParent: event.source === window.parent && window.parent !== window,
      fromSelf: event.source === window,
      type: data && typeof data === "object" ? data.type : null
    });
  });

  var replies = 0;

  window.fetch = function (url, init) {
    var body = null;
    try { body = JSON.parse(init.body); } catch (e) { body = null; }
    state.requests.push({ url: String(url), method: init && init.method, body: body });

    if (String(url) !== "/api/chat") {
      return Promise.reject(new TypeError("HANDOFF: unexpected request"));
    }

    var tooLong = body && typeof body.message === "string" && body.message.length > 4000;
    replies++;
    var status = tooLong ? 422 : 200;
    var json = tooLong ? { error: "message_too_long" }
                       : { reply: "Reply " + replies + ".", session_id: "s-1" };

    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () {
        resolve(new Response(JSON.stringify(json), {
          status: status, headers: { "Content-Type": "application/json" }
        }));
      }, 300);

      if (init && init.signal) {
        init.signal.addEventListener("abort", function () {
          clearTimeout(timer);
          reject(new DOMException("aborted", "AbortError"));
        });
      }
    });
  };

  function instrumentWrites(win, log) {
    try {
      ["setItem", "removeItem", "clear"].forEach(function (name) {
        var original = win.Storage.prototype[name];
        win.Storage.prototype[name] = function () {
          log.push("Storage." + name);
          return original.apply(this, arguments);
        };
      });
    } catch (e) { log.push("instrument-storage-failed"); }

    try {
      var cookie = Object.getOwnPropertyDescriptor(win.Document.prototype, "cookie");
      Object.defineProperty(win.Document.prototype, "cookie", {
        configurable: true,
        get: function () { return cookie.get.call(this); },
        set: function (value) { log.push("document.cookie"); cookie.set.call(this, value); }
      });
    } catch (e) { log.push("instrument-cookie-failed"); }

    try {
      ["open", "deleteDatabase"].forEach(function (name) {
        var original = win.IDBFactory.prototype[name];
        win.IDBFactory.prototype[name] = function () {
          log.push("indexedDB." + name);
          return original.apply(this, arguments);
        };
      });
    } catch (e) { log.push("instrument-indexeddb-failed"); }

    try {
      if (win.CookieStore) {
        ["set", "delete"].forEach(function (name) {
          var original = win.CookieStore.prototype[name];
          win.CookieStore.prototype[name] = function () {
            log.push("cookieStore." + name);
            return original.apply(this, arguments);
          };
        });
      }
    } catch (e) { log.push("instrument-cookiestore-failed"); }
  }

  var topLevel;
  try { topLevel = window.self === window.top; } catch (e) { topLevel = false; }
  if (!topLevel) return;

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

  function snapshot() {
    var empty = document.getElementById("empty");
    return {
      path: location.pathname, search: location.search, hash: location.hash,
      embedded: document.body.classList.contains("is-embedded"),
      expanded: document.body.classList.contains("is-expanded"),
      headerShown: getComputedStyle(document.querySelector(".app-header")).display !== "none",
      emptyShown: !!(empty && empty.isConnected),
      conversationEmpty: document.getElementById("conversation").classList.contains("is-empty"),
      users: Array.prototype.map.call(
        document.querySelectorAll("#thread .msg-user .bubble"), function (b) { return b.textContent; }),
      requests: state.requests.slice()
    };
  }

  window.addEventListener("load", async function () {
    var report = { phase: "standalone", errors: [] };
    try {
      await wait(200);
      report.before = snapshot();

      var input = document.getElementById("composer-input");
      input.value = "after refresh";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      document.getElementById("send-btn").click();
      await wait(600);

      report.after = snapshot();
      report.writes = state.writes.slice();
      report.localStorageLength = localStorage.length;
      report.sessionStorageLength = sessionStorage.length;
      report.cookie = document.cookie;
      report.databases = await databaseNames(window);
    } catch (e) {
      report.errors.push(String(e && e.message || e));
    }

    await realFetch("/__handoff__/report", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(report)
    });
    document.body.setAttribute("data-handoff-reported", "true");
  });
})();
