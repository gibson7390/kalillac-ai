/* ==========================================================================
   Kalillac AI — native chat frontend (vanilla JS)
   - Same-origin POST /api/chat
   - session_id lives ONLY in a page-memory variable. No localStorage,
     sessionStorage, IndexedDB, cookies, or any persistent store.
   - Model output is parsed by the locally vendored marked, then sanitized by
     the locally vendored DOMPurify BEFORE it touches innerHTML.
   - No analytics, no tracking, no remote fonts, no CDN.
   ========================================================================== */
(function () {
  "use strict";

  // ---- page-memory session state (destroyed on reload) --------------------
  var SESSION_ID = null;          // opaque token from the server; RAM only
  var inFlight = false;
  var controller = null;          // AbortController for the active request

  /* TURNS is the single source of truth for conversation history.
     Each entry: { user: string, assistant: string, row: HTMLElement }
     Only COMPLETED exchanges live here. The API payload is derived from it, so
     the current message is never duplicated and Retry cannot desynchronize
     what the UI shows from what the backend receives. */
  var TURNS = [];

  var conversation = document.getElementById("conversation");
  var thread = document.getElementById("thread");
  var emptyState = document.getElementById("empty");
  var form = document.getElementById("composer");
  var input = document.getElementById("composer-input");
  var actionBtn = document.getElementById("send-btn");

  // Logo fallback: start hidden so a missing asset can never flash a
  // broken-image icon. Also handle cached load/error events that may have
  // completed before app.js attached its listeners.
  var logo = document.getElementById("brand-logo");
  var brand = logo && logo.parentNode;

  function syncLogoState() {
    if (!logo) return;
    var loaded = logo.complete && logo.naturalWidth > 0;
    logo.hidden = !loaded;
    if (brand && brand.classList) {
      brand.classList.toggle("has-logo", loaded);
    }
  }

  if (logo) {
    logo.addEventListener("load", syncLogoState);
    logo.addEventListener("error", syncLogoState);
    syncLogoState();
  }

  // ---- Markdown configuration ---------------------------------------------
  if (window.marked && typeof window.marked.setOptions === "function") {
    window.marked.setOptions({
      gfm: true,
      breaks: false,
      headerIds: false,
      mangle: false
    });
  }

  // DOMPurify config: allow only presentational markup. Event handlers, style,
  // and dangerous URI schemes are all stripped.
  var PURIFY_CONFIG = {
    ALLOWED_TAGS: [
      "p", "br", "hr", "strong", "em", "del", "code", "pre",
      "h1", "h2", "h3", "h4", "h5", "h6",
      "ul", "ol", "li", "blockquote",
      "a", "table", "thead", "tbody", "tr", "th", "td", "span"
    ],
    ALLOWED_ATTR: ["href", "title", "class"],
    ALLOW_DATA_ATTR: false,
    ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i,
    FORBID_TAGS: ["script", "style", "iframe", "object", "embed", "form", "input"],
    FORBID_ATTR: ["style", "onerror", "onload", "onclick", "onmouseover"]
  };

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  /*
   * Preserve supported LaTeX delimiters before Marked sees them.
   *
   * Marked treats \(, \), \[ and \] as Markdown escapes. Without this
   * protection a valid model response such as
   *
   *   \(\lim_{x\to4} f(x)=-2\)
   *
   * reaches the browser as visible LaTeX instead of mathematics.
   *
   * We intentionally support:
   *   \( ... \)    inline math
   *   \[ ... \]    display math
   *   $$ ... $$    display math
   *
   * Single-dollar $...$ math is deliberately NOT enabled because ordinary
   * currency such as "$20 to $30" must remain ordinary text.
   */
  function makeMathPlaceholderPrefix() {
    var suffix = "";

    if (
      window.crypto &&
      typeof window.crypto.getRandomValues === "function"
    ) {
      var words = new Uint32Array(4);
      window.crypto.getRandomValues(words);

      for (var i = 0; i < words.length; i++) {
        suffix += words[i].toString(16).padStart(8, "0");
      }
    } else {
      // Collision-avoidance fallback only. This value is not a credential,
      // session identifier, or security boundary.
      suffix =
        Date.now().toString(36) +
        Math.random().toString(36).slice(2);
    }

    return "KALILLACMATH" + suffix + "P";
  }

  function protectMathForMarkdown(text) {
    var source = String(text == null ? "" : text);
    var items = [];
    var prefix = makeMathPlaceholderPrefix();

    function stash(match) {
      var token =
        prefix +
        String(items.length) +
        "END";

      items.push(match);
      return token;
    }

    // Protect display forms first, then inline form.
    source = source.replace(/\\\[[\s\S]*?\\\]/g, stash);
    source = source.replace(/\$\$[\s\S]*?\$\$/g, stash);
    source = source.replace(/\\\([\s\S]*?\\\)/g, stash);

    return {
      text: source,
      items: items,
      prefix: prefix
    };
  }

  /*
   * Restore protected mathematics as text nodes, never by concatenating
   * unsanitized model output back into an HTML string.
   */
  function restoreMathPlaceholders(container, items, prefix) {
    if (!container || !items || !items.length || !prefix) return;

    var walker = document.createTreeWalker(
      container,
      NodeFilter.SHOW_TEXT
    );

    var nodes = [];
    while (walker.nextNode()) {
      nodes.push(walker.currentNode);
    }

    var escapedPrefix = prefix.replace(
      /[.*+?^${}()|[\]\\]/g,
      "\\$&"
    );

    var tokenPattern = new RegExp(
      escapedPrefix + "(\\d+)END",
      "g"
    );

    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var value = node.nodeValue || "";

      tokenPattern.lastIndex = 0;

      if (!tokenPattern.test(value)) continue;

      tokenPattern.lastIndex = 0;

      var fragment = document.createDocumentFragment();
      var lastIndex = 0;
      var match;

      while ((match = tokenPattern.exec(value)) !== null) {
        if (match.index > lastIndex) {
          fragment.appendChild(
            document.createTextNode(
              value.slice(lastIndex, match.index)
            )
          );
        }

        var itemIndex = Number(match[1]);

        fragment.appendChild(
          document.createTextNode(
            itemIndex >= 0 && itemIndex < items.length
              ? items[itemIndex]
              : match[0]
          )
        );

        lastIndex = tokenPattern.lastIndex;
      }

      if (lastIndex < value.length) {
        fragment.appendChild(
          document.createTextNode(value.slice(lastIndex))
        );
      }

      if (node.parentNode) {
        node.parentNode.replaceChild(fragment, node);
      }
    }
  }

  /*
   * KaTeX operates only after Markdown HTML has passed through DOMPurify.
   * trust:false is explicit: model text is never trusted to enable KaTeX
   * commands that can inject arbitrary HTML, attributes, URLs, or resources.
   *
   * pre/code are ignored so literal LaTeX inside code examples stays literal.
   */
  function renderMathSafe(container) {
    if (
      !container ||
      typeof window.renderMathInElement !== "function"
    ) {
      return;
    }

    try {
      window.renderMathInElement(container, {
        delimiters: [
          { left: "\\[", right: "\\]", display: true },
          { left: "$$", right: "$$", display: true },
          { left: "\\(", right: "\\)", display: false }
        ],
        ignoredTags: [
          "script",
          "noscript",
          "style",
          "textarea",
          "pre",
          "code",
          "option"
        ],
        throwOnError: false,
        trust: false,
        strict: "warn"
      });
    } catch (e) {
      // Rendering failure must never destroy the assistant answer.
      // The original LaTeX remains visible as ordinary text.
    }
  }

  function renderMarkdownSafe(text) {
    var rawHtml;
    try {
      rawHtml = window.marked.parse(String(text == null ? "" : text));
    } catch (e) {
      rawHtml = escapeHtml(String(text == null ? "" : text));
    }
    return window.DOMPurify.sanitize(rawHtml, PURIFY_CONFIG);
  }

  // Wrap every table in a horizontal-scroll container so wide tables scroll
  // inside their own box instead of widening the page.
  function wrapTables(container) {
    var tables = container.querySelectorAll("table");
    for (var i = 0; i < tables.length; i++) {
      var t = tables[i];
      if (t.parentNode && t.parentNode.classList &&
          t.parentNode.classList.contains("table-wrap")) continue;
      var wrap = document.createElement("div");
      wrap.className = "table-wrap";
      t.parentNode.insertBefore(wrap, t);
      wrap.appendChild(t);
    }
  }

  function hardenLinks(container) {
    var links = container.querySelectorAll("a[href]");
    for (var i = 0; i < links.length; i++) {
      links[i].setAttribute("target", "_blank");
      links[i].setAttribute("rel", "noopener noreferrer");
    }
  }

  // ---- icons ---------------------------------------------------------------
  var ICON_COPY =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="9" y="9" width="12" height="12" rx="2"/>' +
    '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';

  var ICON_CHECK =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M20 6L9 17l-5-5"/></svg>';

  var ICON_RETRY =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/></svg>';

  // ---- DOM builders --------------------------------------------------------
  function hideEmptyState() {
    if (emptyState && emptyState.parentNode) {
      emptyState.parentNode.removeChild(emptyState);
      emptyState = null;
    }
    conversation.classList.remove("is-empty");
  }

  function addUserMessage(text) {
    hideEmptyState();
    var row = document.createElement("div");
    row.className = "msg msg-user";

    var bubble = document.createElement("div");
    bubble.className = "bubble";
    // User text is NEVER rendered as HTML.
    bubble.style.whiteSpace = "pre-wrap";
    bubble.style.overflowWrap = "anywhere";
    bubble.textContent = text;

    row.appendChild(bubble);
    thread.appendChild(row);
    scrollToBottom();
    return row;
  }

  function addAssistantShell() {
    hideEmptyState();
    var row = document.createElement("div");
    row.className = "msg msg-assistant";

    var bubble = document.createElement("div");
    bubble.className = "bubble";

    var roleLabel = document.createElement("div");
    roleLabel.className = "msg-role";
    roleLabel.textContent = "Kalillac";
    bubble.appendChild(roleLabel);

    var body = document.createElement("div");
    body.className = "md";
    bubble.appendChild(body);

    row.appendChild(bubble);
    thread.appendChild(row);
    scrollToBottom();
    return { row: row, bubble: bubble, body: body };
  }

  function setThinking(shell) {
    shell.body.innerHTML =
      '<div class="thinking"><span>Kalillac is thinking</span>' +
      '<span class="dots"><span></span><span></span><span></span></span></div>';
    scrollToBottom();
  }

  function renderAssistantText(shell, text) {
    var protectedMath = protectMathForMarkdown(text);

    // Markdown-generated HTML is sanitized before entering the DOM.
    shell.body.innerHTML = renderMarkdownSafe(protectedMath.text);

    // Restore only text, never unsanitized HTML, then let trusted local KaTeX
    // render supported mathematics.
    restoreMathPlaceholders(
      shell.body,
      protectedMath.items,
      protectedMath.prefix
    );
    renderMathSafe(shell.body);

    wrapTables(shell.body);
    hardenLinks(shell.body);
  }

  function showNote(shell, message, kind) {
    shell.body.innerHTML = "";
    var note = document.createElement("div");
    note.className = kind === "error" ? "error-note" : "notice-note";
    note.textContent = message;
    shell.body.appendChild(note);
    scrollToBottom();
  }

  /* Copy + Retry live under a COMPLETED assistant answer only. They are added
     after the reply lands, never on a thinking or error row. */
  function addActions(shell, turnIndex) {
    var bar = document.createElement("div");
    bar.className = "msg-actions";

    var copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "msg-action";
    copyBtn.setAttribute("aria-label", "Copy response");
    copyBtn.innerHTML = ICON_COPY + "<span>Copy</span>";
    copyBtn.addEventListener("click", function () {
      copyText(TURNS[turnIndex] ? TURNS[turnIndex].assistant : "", copyBtn);
    });

    var retryBtn = document.createElement("button");
    retryBtn.type = "button";
    retryBtn.className = "msg-action";
    retryBtn.setAttribute("aria-label", "Retry this response");
    retryBtn.innerHTML = ICON_RETRY + "<span>Retry</span>";
    retryBtn.addEventListener("click", function () { retryTurn(turnIndex); });

    bar.appendChild(copyBtn);
    bar.appendChild(retryBtn);
    shell.row.appendChild(bar);
    return bar;
  }

  /* Clipboard API needs a secure context. Fall back to a temporary textarea +
     execCommand so copy still works over plain http on a LAN/staging host. */
  function copyText(text, btn) {
    function done() {
      var original = btn.innerHTML;
      btn.classList.add("is-done");
      btn.innerHTML = ICON_CHECK + "<span>Copied</span>";
      window.setTimeout(function () {
        btn.classList.remove("is-done");
        btn.innerHTML = original;
      }, 1600);
    }
    function fallback() {
      try {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
        done();
      } catch (e) { /* clipboard unavailable; fail quietly */ }
    }

    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  }

  // ---- scroll management ---------------------------------------------------
  var stickToBottom = true;

  function atBottom() {
    return conversation.scrollHeight - conversation.scrollTop -
           conversation.clientHeight < 80;
  }

  function scrollToBottom(force) {
    if (!force && !stickToBottom) return;
    window.requestAnimationFrame(function () {
      conversation.scrollTop = conversation.scrollHeight;
    });
  }

  conversation.addEventListener("scroll", function () {
    stickToBottom = atBottom();
  });

  // ---- busy / send / stop state -------------------------------------------
  function setBusy(busy) {
    inFlight = busy;
    actionBtn.classList.toggle("is-stop", busy);
    actionBtn.setAttribute("aria-label", busy ? "Stop generating" : "Send message");
    actionBtn.title = busy ? "Stop" : "Send";
    actionBtn.disabled = false;                 // Stop must stay clickable
    if (!busy) syncSendEnabled();
    input.readOnly = busy;                      // readOnly, not disabled, so the
                                                // iOS keyboard does not dismiss
  }

  function syncSendEnabled() {
    if (inFlight) return;
    actionBtn.disabled = input.value.trim().length === 0;
  }

  // ---- history payload -----------------------------------------------------
  /* Derived from completed TURNS only. The current message is sent separately
     as `message`, so it is never present twice. */
  function buildHistory() {
    var out = [];
    for (var i = 0; i < TURNS.length; i++) {
      out.push({ role: "user", content: TURNS[i].user });
      out.push({ role: "assistant", content: TURNS[i].assistant });
    }
    return out;
  }

  // ---- core request --------------------------------------------------------
  /* history is captured BEFORE the request so a retry replays the exact
     conversation state that preceded the turn being regenerated. */
  function requestReply(text, history, shell, userRow) {
    setBusy(true);
    setThinking(shell);

    controller = new AbortController();

    var payload = {
      message: text,
      history: history,
      session_id: SESSION_ID
    };

    // Same-origin: the frontend is served at /app/ and the API at /api/, so no
    // CORS is involved and no credentials/cookies are sent.
    return fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal
    })
      .then(function (res) {
        return res.json().catch(function () { return {}; })
          .then(function (data) {
            return { ok: res.ok, status: res.status, data: data };
          });
      })
      .then(function (r) {
        if (r.ok && r.data && typeof r.data.reply === "string") {
          if (r.data.session_id) SESSION_ID = r.data.session_id;
          renderAssistantText(shell, r.data.reply);

          // Commit the completed exchange only now.
          var index = TURNS.length;
          TURNS.push({
            user: text,
            assistant: r.data.reply,
            userRow: userRow,
            row: shell.row
          });
          addActions(shell, index);
          scrollToBottom();
          return true;
        }
        showNote(shell, mapError(r.status, r.data && r.data.error), "error");
        return false;
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") {
          /* Cancelled by the user. The browser request is aborted immediately.
             NOTE: /api/chat is a non-streaming provider call, so aborting the
             browser request does not guarantee the upstream model computation
             stops at that instant; the server may still finish generating and
             discard the result. This is an honest description of the current
             backend, not a claim of upstream cancellation. */
          showNote(shell, "Stopped.", "notice");
          // No turn is recorded, so history stays consistent for the next send.
          return false;
        }
        showNote(shell,
          "Kalillac couldn't reach the server. Check your connection and try again.",
          "error");
        return false;
      })
      .then(function (result) {
        controller = null;
        setBusy(false);
        return result;
      });
  }

  function mapError(status, code) {
    if (code === "empty_message") return "Please type a message.";
    if (code === "message_too_long")
      return "That message is too long. Please shorten it and try again.";
    if (code === "history_too_long")
      return "This conversation is very long. Reload the page to start a fresh session.";
    if (code === "model_provider_unavailable" && status === 503)
      return "Kalillac's AI model provider is temporarily unavailable. Please try again shortly.";
    if (code === "busy" || status === 429)
      return "Kalillac is handling several requests right now. Please try again in a moment.";
    return "Something went wrong on Kalillac's end. Please try again.";
  }

  // ---- send ----------------------------------------------------------------
  function send() {
    var text = input.value.trim();
    if (!text || inFlight) return;

    // Snapshot history BEFORE this turn: prior completed exchanges only.
    var history = buildHistory();

    var userRow = addUserMessage(text);
    input.value = "";
    autoGrow();
    syncSendEnabled();

    stickToBottom = true;
    var shell = addAssistantShell();
    requestReply(text, history, shell, userRow).then(function () {
      if (!inFlight) input.focus();
    });
  }

  /* Retry regenerates the answer for TURN index i.
     Everything from turn i onward is removed from BOTH the DOM and TURNS, then
     the original user message is resent with the history that preceded it. The
     user message is not re-appended to the DOM, so it is never duplicated, and
     buildHistory() stays exactly in step with what is on screen. */
  function retryTurn(index) {
    if (inFlight) return;
    var turn = TURNS[index];
    if (!turn || !turn.userRow) return;

    /* Retry may target any completed answer. Keep the selected user message,
       remove everything after it (selected answer, later turns, and any later
       stopped/error rows), then rebuild history from completed prior turns. */
    var node = turn.userRow.nextSibling;
    while (node) {
      var next = node.nextSibling;
      thread.removeChild(node);
      node = next;
    }

    TURNS.length = index;

    var history = buildHistory();
    stickToBottom = true;
    var shell = addAssistantShell();
    requestReply(turn.user, history, shell, turn.userRow).then(function () {
      if (!inFlight) input.focus();
    });
  }

  function stop() {
    if (controller) {
      try { controller.abort(); } catch (e) { /* already settled */ }
    }
  }

  // ---- composer behavior ---------------------------------------------------
  function autoGrow() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 168) + "px";
  }

  input.addEventListener("input", function () {
    autoGrow();
    syncSendEnabled();
  });

  input.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || e.shiftKey) return;
    // On touch keyboards Enter should insert a newline; the Send button sends.
    var coarse = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
    if (coarse) return;
    e.preventDefault();
    if (!inFlight) send();
  });

  actionBtn.addEventListener("click", function (e) {
    e.preventDefault();
    if (inFlight) stop();
    else send();
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (!inFlight) send();
  });

  // Empty-state starter chips. Each chip draws from its own prompt pool and
  // never repeats the same prompt twice in a row. Clicking only fills the
  // composer; the user can edit before sending.
  var HINT_PROMPTS = {
    explain: [
      "Explain how HTTPS certificate validation works.",
      "Explain what happens during a DNS lookup.",
      "Explain the difference between hashing and encryption.",
      "Explain how a reverse proxy works.",
      "Explain public-key cryptography in plain English.",
      "Explain the difference between RAM and persistent storage."
    ],
    code: [
      "Write Python that extracts IOCs from a log.",
      "Write Bash that shows listening ports and owning processes.",
      "Write PowerShell that lists established TCP connections and process names.",
      "Write Python that parses Nmap XML into CSV.",
      "Write a Bash script that checks ports 22, 80, 443, and 445 on a list of hosts.",
      "Write Python that deduplicates SHA-256 hashes from a text file."
    ],
    search: [
      "Find and summarize the latest major cybersecurity developments.",
      "Find the latest major open-source AI model releases.",
      "Find recent Linux security advisories and summarize the important ones.",
      "Find the current official Nmap documentation and summarize recent changes.",
      "Find the latest major browser privacy developments.",
      "Find today's important AI and cybersecurity news."
    ]
  };

  var lastHintIndex = Object.create(null);

  function chooseHintPrompt(group, fallback) {
    var pool = HINT_PROMPTS[group];
    if (!pool || pool.length === 0) return fallback || "";
    if (pool.length === 1) return pool[0];

    var index;
    do {
      index = Math.floor(Math.random() * pool.length);
    } while (index === lastHintIndex[group]);

    lastHintIndex[group] = index;
    return pool[index];
  }

  var hints = document.querySelectorAll(".hint");

  // Quietly guide the eye across the three starters while the composer is
  // untouched. Hover pauses the guide so only the hovered chip is highlighted.
  // Leaving the chips resumes from the same guide position. Typing or choosing
  // a starter dismisses the guide permanently for this page session.
  var hintGuideTimer = null;
  var hintGuideIndex = 0;
  var hintGuideDismissed = false;

  function clearHintGuide() {
    for (var i = 0; i < hints.length; i++) {
      hints[i].classList.remove("hint-guide-active");
    }
  }

  function clearHintGuideTimer() {
    if (hintGuideTimer !== null) {
      window.clearInterval(hintGuideTimer);
      hintGuideTimer = null;
    }
  }

  function stopHintGuide() {
    hintGuideDismissed = true;
    clearHintGuideTimer();
    clearHintGuide();
  }

  function pauseHintGuide() {
    if (hintGuideDismissed) return;
    clearHintGuideTimer();
    clearHintGuide();
  }

  function showHintGuide(index) {
    clearHintGuide();
    if (hints[index]) {
      hints[index].classList.add("hint-guide-active");
    }
  }

  function runHintGuide() {
    if (hintGuideDismissed || !hints.length) return;

    if (window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      return;
    }

    showHintGuide(hintGuideIndex);

    clearHintGuideTimer();
    hintGuideTimer = window.setInterval(function () {
      hintGuideIndex = (hintGuideIndex + 1) % hints.length;
      showHintGuide(hintGuideIndex);
    }, 1400);
  }

  function startHintGuide() {
    hintGuideDismissed = false;
    hintGuideIndex = 0;
    runHintGuide();
  }

  function resumeHintGuideAfterHover() {
    // mouseleave fires before mouseenter while moving between chips. Wait one
    // event turn and resume only if the pointer is no longer over ANY chip.
    window.setTimeout(function () {
      if (hintGuideDismissed) return;

      for (var i = 0; i < hints.length; i++) {
        if (hints[i].matches(":hover")) return;
      }

      runHintGuide();
    }, 0);
  }

  for (var h = 0; h < hints.length; h++) {
    hints[h].addEventListener("mouseenter", pauseHintGuide);
    hints[h].addEventListener("mouseleave", resumeHintGuideAfterHover);

    hints[h].addEventListener("click", function () {
      stopHintGuide();

      var group = this.getAttribute("data-hint-group") || "";
      var fallback = this.getAttribute("data-prompt") || "";
      input.value = chooseHintPrompt(group, fallback);
      autoGrow();
      syncSendEnabled();
      input.focus();
    });
  }

  input.addEventListener("input", function () {
    if (input.value.trim().length > 0) {
      stopHintGuide();
    }
  });

  startHintGuide();

  /* iOS: the software keyboard shrinks the visual viewport rather than the
     layout viewport, so track it and keep the newest message in view. */
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", function () {
      if (stickToBottom) scrollToBottom(true);
    });
  }

  /* Same-origin embed detection. The homepage frames this app at /app/; when
     it does, the surrounding page already provides branding and the
     Privacy/Terms links, so the duplicate chrome is suppressed via CSS. */
  var isEmbedded = false;
  try { isEmbedded = window.self !== window.top; } catch (e) { isEmbedded = true; }
  if (isEmbedded) document.body.classList.add("is-embedded");

  if (emptyState) conversation.classList.add("is-empty");
  syncSendEnabled();
  input.focus();
})();
