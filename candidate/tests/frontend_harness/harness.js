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
            // A phone-sized viewport for the scenario that measures layout.
            if (/_360x640$/.test(name)) {
              frame.style.border = "0";
              frame.style.width = "360px";
              frame.style.height = "640px";
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

    var headers = { "Content-Type": spec.contentType || "application/json" };
    var response;

    if (spec.bodyError) {
      var stream = new ReadableStream({
        start: function (c) { c.error(new TypeError("RAW-STREAM-SECRET body failed")); }
      });
      response = new Response(stream, { status: spec.status || 200, headers: headers });
    } else {
      var body = spec.json !== undefined ? JSON.stringify(spec.json) : (spec.body || "");
      response = new Response(body, { status: spec.status || 200, headers: headers });
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

  window.fetch = function (url, init) {
    var record = { url: String(url), method: init && init.method };
    try { record.body = JSON.parse(init.body); } catch (e) { record.body = null; }
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
      limitDialog: dialogState()
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
    respondWith: function () {
      for (var i = 0; i < arguments.length; i++) queue.push(arguments[i]);
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
    request_timeout: 504, internal_error: 500
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

  // The anonymous daily allowance is used up (with server text that must never
  // be shown).
  var DAILY_LIMIT = {
    status: 429,
    json: { error: "anonymous_daily_limit_reached", detail: SECRET, message: SECRET }
  };

  SCENARIOS.code_anonymous_daily_limit_reached = async function (a) {
    a.respondWith(DAILY_LIMIT, ok("Fine."));
    await a.send("first question");
    a.snap("failed");
    await a.clickDialogOk();
    a.snap("closed");
    await a.send("next question");
    a.snap("after-new-send");
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

  SCENARIOS.daily_limit_reopen = async function (a) {
    a.respondWith(DAILY_LIMIT, DAILY_LIMIT);
    await a.send("first question");
    await a.clickDialogOk();
    a.snap("first-closed");
    await a.send("second question");
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

  // ---- run ---------------------------------------------------------------------
  window.addEventListener("load", function () {
    var scenario = SCENARIOS[scenarioName];
    var finish = function (error) {
      window.__HARNESS_RESULT__ = {
        scenario: scenarioName,
        error: error || null,
        steps: steps,
        requests: requests,
        copied: copied
      };
    };

    if (!scenario) { finish("unknown scenario"); return; }

    Promise.resolve()
      .then(function () { return scenario(api); })
      .then(function () { finish(null); },
            function (e) { finish(String(e && e.message || e)); });
  });
})();
