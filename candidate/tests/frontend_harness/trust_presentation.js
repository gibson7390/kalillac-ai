/* Executable harness for the chat's trust presentation: the accuracy notice
   beneath the composer and the presentation of the server's source list.

   Loaded by test_frontend_trust_presentation.py into the real
   frontend/index.html, before any other script, so it can replace
   window.fetch before app.js runs. It drives the page only through its real
   DOM controls and records what the page shows. Responses come from
   /__trust__/fixtures.json, which the test builds with the backend's own
   source_trust functions -- each a reply plus, for a genuine footer, the
   server's source_list -- so the page renders exactly what the server sends.

   Two modes:
   - /app/?scenario=<name>: run one scenario against the real app.
   - /__trust__/runner?scenarios=a,b,...: run each scenario in a fresh
     same-origin iframe sized by the name's _<W>x<H> suffix (default
     1280x800) and write every result, as JSON, into <pre id="results">.

   No request leaves the page: fetch is replaced before app.js loads. */
(function () {
  "use strict";

  function wait(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function frameSize(name) {
    var match = /_(\d+)x(\d+)$/.exec(name);
    return match ? { w: +match[1], h: +match[2] } : { w: 1280, h: 800 };
  }

  // ---- runner page -----------------------------------------------------------
  if (location.pathname.indexOf("/__trust__/runner") === 0) {
    var names = (new URLSearchParams(location.search).get("scenarios") || "")
      .split(",").filter(Boolean);

    window.addEventListener("load", function () {
      var results = {};
      var chain = Promise.resolve();

      names.forEach(function (name) {
        chain = chain.then(function () {
          return new Promise(function (resolve) {
            var size = frameSize(name);
            var frame = document.createElement("iframe");
            frame.style.border = "0";
            frame.style.display = "block";
            frame.style.width = size.w + "px";
            frame.style.height = size.h + "px";
            frame.src = "/app/?scenario=" + encodeURIComponent(name);
            document.body.appendChild(frame);
            var waited = 0;

            (function poll() {
              var win = frame.contentWindow;
              var done = win && win.__TRUST_RESULT__;
              if (done || waited >= 20000) {
                results[name] = done || { error: "timeout" };
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
    });
    return;
  }

  // ---- scenario page -------------------------------------------------------------
  var params = new URLSearchParams(location.search);
  var scenarioName = params.get("scenario") || "";
  var fixtureName = params.get("fixture") || "";

  // Synchronous, so the fixtures exist before app.js runs. Loopback only.
  var FIXTURES = {};
  try {
    var xhr = new XMLHttpRequest();
    xhr.open("GET", "/__trust__/fixtures.json", false);
    xhr.send();
    FIXTURES = JSON.parse(xhr.responseText);
  } catch (e) { FIXTURES = {}; }

  var queue = [];
  var requests = [];

  window.fetch = function (url, init) {
    var record = { url: String(url), method: init && init.method };
    try { record.body = JSON.parse(init.body); } catch (e) { record.body = null; }
    requests.push(record);
    var spec = queue.shift();
    if (!spec) return Promise.reject(new TypeError("HARNESS: no scripted response"));
    var body = JSON.stringify(spec.json);
    return Promise.resolve(new Response(body, {
      status: spec.status || 200, headers: { "Content-Type": "application/json" }
    }));
  };

  var copied = [];
  try {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: function (value) { copied.push(String(value)); return Promise.resolve(); } }
    });
  } catch (e) { /* leave the real clipboard */ }

  // Records every attempt to run script or load a resource from rendered text.
  window.__TRUST_EXECUTED__ = [];

  function $(id) { return document.getElementById(id); }

  function rect(el) {
    if (!el) return null;
    var r = el.getBoundingClientRect();
    return { top: r.top, left: r.left, bottom: r.bottom, right: r.right, width: r.width, height: r.height };
  }

  function styleOf(el, names) {
    var cs = getComputedStyle(el);
    var out = {};
    names.forEach(function (n) { out[n] = cs.getPropertyValue(n); });
    return out;
  }

  // The first non-transparent background behind an element.
  function backgroundBehind(el) {
    for (var node = el; node && node.nodeType === 1; node = node.parentElement) {
      var bg = getComputedStyle(node).backgroundColor;
      if (bg && bg !== "transparent" && !/rgba\(.*,\s*0\)$/.test(bg)) return bg;
    }
    return "rgb(255, 255, 255)";
  }

  function noticeState() {
    var notice = $("accuracy-notice");
    var input = $("composer-input");
    var limit = $("limit-notice");
    return {
      exists: !!notice,
      text: notice ? notice.textContent : null,
      tag: notice ? notice.tagName : null,
      rect: rect(notice),
      style: notice ? styleOf(notice, ["color", "font-size", "display", "visibility", "opacity",
                                        "line-height", "text-align"]) : null,
      background: notice ? backgroundBehind(notice) : null,
      ariaHidden: notice ? notice.closest("[aria-hidden='true'],[inert],[hidden]") !== null : null,
      focusable: notice ? notice.tabIndex >= 0 : null,
      describedBy: input ? input.getAttribute("aria-describedby") : null,
      afterComposer: notice ? !!($("composer").compareDocumentPosition(notice) & Node.DOCUMENT_POSITION_FOLLOWING) : null,
      composer: rect($("composer-shell")),
      input: rect(input),
      send: rect($("send-btn")),
      erase: rect($("erase-btn")),
      footer: rect(document.querySelector(".app-footer")),
      limitNotice: limit && !limit.hidden ? rect(limit) : null,
      limitText: limit && !limit.hidden ? limit.textContent : null,
      viewport: { width: window.innerWidth, height: window.innerHeight },
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth
    };
  }

  function assistantRows() {
    return document.querySelectorAll("#thread .msg-assistant");
  }

  function messageState(row) {
    var body = row.querySelector(".md");
    var bubble = row.querySelector(".bubble");
    var list = body.querySelector(".source-list");
    var anchors = Array.prototype.map.call(body.querySelectorAll("a"), function (a) {
      return {
        href: a.getAttribute("href"),
        text: a.textContent,
        inSourceList: !!a.closest(".source-list"),
        target: a.getAttribute("target"),
        rel: a.getAttribute("rel")
      };
    });
    var handlers = [];
    Array.prototype.forEach.call(body.querySelectorAll("*"), function (el) {
      Array.prototype.forEach.call(el.attributes, function (attr) {
        if (/^on/i.test(attr.name) || attr.name === "style" || attr.name === "srcdoc") {
          handlers.push(el.tagName + "@" + attr.name);
        }
      });
    });
    var state = {
      text: body.textContent,
      html: body.innerHTML,
      anchors: anchors,
      handlers: handlers,
      forbidden: body.querySelectorAll("img, script, iframe, object, embed, form, input, svg, style, video, audio").length,
      bubble: rect(bubble),
      list: null
    };
    if (list) {
      var label = list.querySelector(".source-list-label");
      var ul = list.querySelector("ul");
      state.list = {
        kind: list.getAttribute("data-source-kind"),
        className: list.className,
        isLastBlock: body.lastElementChild === list,
        label: label ? label.textContent : null,
        labelId: label ? label.id : null,
        labelledBy: ul ? ul.getAttribute("aria-labelledby") : null,
        labelStyle: label ? styleOf(label, ["color", "font-size", "font-weight"]) : null,
        rect: rect(list),
        items: Array.prototype.map.call(list.querySelectorAll("li"), function (li) {
          var a = li.querySelector("a");
          var title = li.querySelector(".source-title");
          var host = li.querySelector(".source-host");
          return {
            href: a ? a.getAttribute("href") : null,
            linkText: a ? a.textContent : null,
            target: a ? a.getAttribute("target") : null,
            rel: a ? a.getAttribute("rel") : null,
            tabIndex: a ? a.tabIndex : null,
            title: title ? title.textContent : null,
            host: host ? host.textContent : null,
            children: a ? Array.prototype.map.call(a.children, function (c) { return c.className; }) : [],
            linkRect: rect(a),
            titleRect: rect(title),
            hostRect: rect(host),
            titleClamped: title ? title.scrollHeight > title.clientHeight + 1 : null,
            hostOverflows: host ? host.scrollWidth > host.clientWidth + 1 : null,
            linkStyle: a ? styleOf(a, ["background-color", "border-left-width", "border-left-color",
                                        "text-decoration-line", "display"]) : null,
            titleStyle: title ? styleOf(title, ["color", "font-size", "font-weight"]) : null,
            hostStyle: host ? styleOf(host, ["color", "font-size"]) : null,
            linkBackground: a ? backgroundBehind(a) : null
          };
        })
      };
    }
    return state;
  }

  function focusState(link) {
    link.focus();
    var cs = getComputedStyle(link);
    var out = {
      active: document.activeElement === link,
      focusVisible: link.matches(":focus-visible"),
      outlineStyle: cs.outlineStyle,
      outlineWidth: cs.outlineWidth,
      outlineColor: cs.outlineColor
    };
    link.blur();
    return out;
  }

  var steps = [];

  var api = {
    reply: function (name) {
      if (!(name in FIXTURES)) throw new Error("no fixture " + name);
      var json = { session_id: "s-trust" };
      Object.keys(FIXTURES[name]).forEach(function (k) { json[k] = FIXTURES[name][k]; });
      queue.push({ status: 200, json: json });
    },
    unavailable: function () {
      queue.push({ status: 503, json: { error: "service_unavailable" } });
    },
    clickRetry: async function () {
      var buttons = document.querySelectorAll("#thread .msg-action, #thread .retry-btn, #thread button");
      for (var i = buttons.length - 1; i >= 0; i--) {
        var label = buttons[i].querySelector("span");
        if (label && label.textContent === "Retry") {
          buttons[i].click();
          for (var j = 0; j < 200; j++) {
            await wait(20);
            if (!$("send-btn").classList.contains("is-stop") && !document.querySelector("#thread .thinking")) break;
          }
          await wait(50);
          return;
        }
      }
      throw new Error("no Retry control");
    },
    dailyLimit: function () {
      queue.push({ status: 429, json: { error: "anonymous_daily_limit_reached" } });
    },
    send: async function (text) {
      var input = $("composer-input");
      input.value = text;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      $("send-btn").click();
      for (var i = 0; i < 200; i++) {
        await wait(20);
        var rows = assistantRows();
        var last = rows[rows.length - 1];
        if (last && !last.querySelector(".thinking") && !$("send-btn").classList.contains("is-stop")) break;
      }
      await wait(50);
    },
    snap: function (label, extra) {
      var rows = assistantRows();
      var snap = {
        label: label,
        notice: noticeState(),
        messages: Array.prototype.map.call(rows, messageState),
        executed: window.__TRUST_EXECUTED__.slice()
      };
      if (extra) Object.keys(extra).forEach(function (k) { snap[k] = extra[k]; });
      steps.push(snap);
      return snap;
    },
    focusFirstSource: function () {
      var link = document.querySelector("#thread .source-link");
      return link ? focusState(link) : null;
    },
    copyLast: async function () {
      var buttons = document.querySelectorAll("#thread .msg-action");
      for (var i = buttons.length - 1; i >= 0; i--) {
        var label = buttons[i].querySelector("span");
        if (label && label.textContent === "Copy") { buttons[i].click(); await wait(50); return; }
      }
      throw new Error("no Copy action");
    },
    closeLimitDialog: async function () { $("limit-dialog-ok").click(); await wait(50); }
  };

  // ---- scenarios -------------------------------------------------------------------
  var SCENARIOS = {};

  // One reply per fixture, rendered and measured at the frame's size.
  function single(fixture) {
    return async function (a) {
      a.reply(fixture);
      await a.send("question");
      a.snap("rendered", { focus: a.focusFirstSource() });
    };
  }

  // Viewport scenarios: the empty workspace, then a sourced answer.
  async function layout(a) {
    a.snap("empty");
    a.reply("sources_selected");
    await a.send("question");
    a.reply("search_results_long");
    await a.send("another question");
    a.snap("conversation", { focus: a.focusFirstSource() });
  }

  // The daily limit: the dialog closes, the limit note sits above the composer
  // and the accuracy notice stays beneath it.
  async function dailyLimit(a) {
    a.reply("plain_answer");
    await a.send("question");
    a.dailyLimit();
    var input = $("composer-input");
    input.value = "refused question";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    $("send-btn").click();
    await wait(200);
    await a.closeLimitDialog();
    a.snap("paused");
  }

  // A failed send retried: the successful response's metadata styles the
  // list. Retrying that answer with a response that has none leaves it plain.
  async function retryWithMetadata(a) {
    a.unavailable();
    await a.send("question");
    a.reply("sources_selected");
    await a.clickRetry();
    a.snap("after_retry");
    a.reply("fallback_unsigned_sources");
    await a.clickRetry();
    a.snap("after_plain_retry");
  }

  // History and Copy keep the server's reply text exactly.
  async function preservation(a) {
    a.reply("sources_selected");
    await a.send("first");
    await a.copyLast();
    a.reply("plain_answer");
    await a.send("second");
    a.snap("after");
  }

  var VIEWPORTS = ["1280x800", "1024x768", "768x1024", "640x400", "390x844", "360x640", "320x568",
                   "640x360", "360x300"];
  VIEWPORTS.forEach(function (size) { SCENARIOS["layout_" + size] = layout; });
  ["1280x800", "360x640", "320x568"].forEach(function (size) {
    SCENARIOS["daily_limit_" + size] = dailyLimit;
  });
  SCENARIOS.preservation = preservation;
  SCENARIOS.retry_with_metadata = retryWithMetadata;
  // The empty workspace only (visual evidence).
  SCENARIOS.empty = async function (a) { a.snap("empty"); };
  Object.keys(FIXTURES).forEach(function (name) {
    SCENARIOS["fixture_" + name] = single(name);
    SCENARIOS["fixture_" + name + "_360x640"] = single(name);
  });
  // A single fixture rendered on its own (visual evidence).
  if (fixtureName) SCENARIOS.visual = single(fixtureName);

  window.addEventListener("load", function () {
    var scenario = SCENARIOS[scenarioName];
    var finish = function (error) {
      window.__TRUST_RESULT__ = {
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
      .then(function () { finish(null); }, function (e) { finish(String(e && e.message || e)); });
  });
})();
