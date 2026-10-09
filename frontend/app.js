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

  /* The one exchange whose send did not complete (failed or stopped), or null.
     { text, history, userRow, shell, draftRevision, outcome }. It never enters
     TURNS; it stays on screen until it is retried, a new message is sent, or
     the conversation is reset. */
  var failedExchange = null;

  /* Bumped by every user edit of the composer (typing, paste, cut, a starter
     chip). Programmatic clears and restores do not bump it, so a submitted
     message is restored only while the composer is still the one its send
     emptied. */
  var draftRevision = 0;

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

  var ICON_NEW =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M12 5v14"/><path d="M5 12h14"/></svg>';

  // ---- DOM builders --------------------------------------------------------
  /* The empty-state node is detached, not discarded, so a new conversation
     can show it again with its starter chips still wired. */
  function hideEmptyState() {
    if (emptyState && emptyState.parentNode) {
      emptyState.parentNode.removeChild(emptyState);
    }
    conversation.classList.remove("is-empty");
  }

  function showEmptyState() {
    if (emptyState && !emptyState.parentNode) {
      thread.appendChild(emptyState);
    }
    conversation.classList.add("is-empty");
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

  function clearActions(shell) {
    var bars = shell.row.querySelectorAll(".msg-actions");
    for (var i = 0; i < bars.length; i++) {
      bars[i].parentNode.removeChild(bars[i]);
    }
  }

  /* The single action under a failed or stopped exchange: Retry, or New
     conversation when the conversation itself is too long. Always visible,
     since there is no answer to hover over. */
  function addFailureAction(shell, action, onClick) {
    var bar = document.createElement("div");
    bar.className = "msg-actions is-visible";

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "msg-action";

    if (action === "new-conversation") {
      btn.setAttribute("aria-label", "Start a new conversation");
      btn.innerHTML = ICON_NEW + "<span>New conversation</span>";
    } else {
      btn.setAttribute("aria-label", "Retry this message");
      btn.innerHTML = ICON_RETRY + "<span>Retry</span>";
    }

    btn.addEventListener("click", onClick);
    bar.appendChild(btn);
    shell.row.appendChild(bar);
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

  // ---- outcomes --------------------------------------------------------------
  /* Every unsuccessful send ends in exactly one of these fixed outcomes. The
     backend's fixed error code selects it; no other response text is ever
     shown. action: "retry", "new-conversation" or null. */
  var COPY_UNREACHABLE =
    "Kalillac couldn't reach the server. Check your connection and try again.";
  var COPY_INCOMPLETE = "Kalillac couldn't complete the request. Please try again.";
  var COPY_UNREADABLE = "Kalillac couldn't read that request. Please try again.";
  var COPY_BUSY = "Kalillac is busy right now. Please try again in a moment.";
  var COPY_STOPPED = "Stopped.";

  var OUTCOME_UNREACHABLE = { message: COPY_UNREACHABLE, kind: "error", action: "retry" };
  var OUTCOME_INCOMPLETE = { message: COPY_INCOMPLETE, kind: "error", action: "retry" };
  var OUTCOME_BUSY = { message: COPY_BUSY, kind: "error", action: "retry" };
  var OUTCOME_STOPPED = { message: COPY_STOPPED, kind: "notice", action: "retry" };

  var ERROR_OUTCOMES = {
    invalid_json: { message: COPY_UNREADABLE, kind: "error", action: "retry" },
    invalid_body: { message: COPY_UNREADABLE, kind: "error", action: "retry" },
    invalid_request: { message: COPY_UNREADABLE, kind: "error", action: "retry" },
    empty_message: {
      message: "Please type a message.", kind: "error", action: null
    },
    message_too_long: {
      message: "That message is too long. Please shorten it and try again.",
      kind: "error", action: null
    },
    history_too_long: {
      message: "This conversation is too long to continue. " +
               "Start a new conversation to keep going.",
      kind: "error", action: "new-conversation"
    },
    processing_limit_reached: {
      message: "This request needed more processing than Kalillac allows for one " +
               "message. Try simplifying it or splitting it up.",
      kind: "error", action: null
    },
    busy: OUTCOME_BUSY,
    request_cancelled: OUTCOME_STOPPED,
    service_unavailable: {
      message: "Kalillac is temporarily unavailable. Please try again shortly.",
      kind: "error", action: "retry"
    },
    model_provider_unavailable: {
      message: "Kalillac couldn't get an answer from its AI model just now. " +
               "Please try again shortly.",
      kind: "error", action: "retry"
    },
    request_timeout: {
      message: "That took too long and was stopped. Please try again, " +
               "or try a simpler request.",
      kind: "error", action: "retry"
    },
    internal_error: {
      message: "Something went wrong on Kalillac's end. Please try again.",
      kind: "error", action: "retry"
    }
  };

  /* An HTTP response arrived but was not a usable reply. A recognized fixed
     code wins; a bare 429 is busy; anything else could not be completed. */
  function outcomeForResponse(status, data) {
    var code = data && typeof data.error === "string" ? data.error : null;

    if (code && Object.prototype.hasOwnProperty.call(ERROR_OUTCOMES, code)) {
      return ERROR_OUTCOMES[code];
    }

    if (status === 429) return OUTCOME_BUSY;
    return OUTCOME_INCOMPLETE;
  }

  function isAbort(err) {
    return !!err && err.name === "AbortError";
  }

  function parseJson(body) {
    try { return JSON.parse(body); } catch (e) { return null; }
  }

  // ---- composer ownership -----------------------------------------------------
  /* Put an unsuccessful exchange's text back in the composer only while the
     composer is still the empty one its send left behind. Newer text is never
     overwritten. */
  function restoreDraft(exchange) {
    if (input.value !== "" || draftRevision !== exchange.draftRevision) return;
    input.value = exchange.text;
    autoGrow();
    syncSendEnabled();
  }

  /* A Retry takes its own restored text back out of the composer, but only if
     the user has not changed it since it was restored. */
  function reclaimDraft(exchange) {
    if (input.value !== exchange.text || draftRevision !== exchange.draftRevision) return;
    input.value = "";
    autoGrow();
    syncSendEnabled();
  }

  // ---- core request --------------------------------------------------------
  /* One exchange: the submitted text, the completed-history snapshot taken
     before it was sent, its user row and its assistant shell. Retrying it
     reuses all four. */
  function newExchange(text, history, userRow, shell) {
    return {
      text: text,
      history: history,
      userRow: userRow,
      shell: shell,
      draftRevision: draftRevision,
      outcome: null
    };
  }

  function failExchange(exchange, outcome) {
    exchange.outcome = outcome;
    clearActions(exchange.shell);
    showNote(exchange.shell, outcome.message, outcome.kind);

    if (outcome.action === "retry") {
      addFailureAction(exchange.shell, "retry", function () { retryExchange(exchange); });
    } else if (outcome.action === "new-conversation") {
      addFailureAction(exchange.shell, "new-conversation", startNewConversation);
    }

    failedExchange = exchange;
    restoreDraft(exchange);
  }

  /* Render the reply and its actions first; only a fully rendered answer
     becomes a completed turn. */
  function completeExchange(exchange, reply, sessionId) {
    var index = TURNS.length;

    try {
      renderAssistantText(exchange.shell, reply);
      clearActions(exchange.shell);
      addActions(exchange.shell, index);
    } catch (e) {
      failExchange(exchange, OUTCOME_INCOMPLETE);
      return;
    }

    if (typeof sessionId === "string" && sessionId) SESSION_ID = sessionId;

    TURNS.push({
      user: exchange.text,
      assistant: reply,
      userRow: exchange.userRow,
      row: exchange.shell.row
    });

    if (failedExchange === exchange) failedExchange = null;
    scrollToBottom();
  }

  function handleResponse(exchange, res) {
    return res.text().then(function (body) {
      var data = parseJson(body);

      if (!res.ok) {
        failExchange(exchange, outcomeForResponse(res.status, data));
        return;
      }

      // A 2xx that also carries an error code is ambiguous, not a reply.
      if (data && typeof data.error === "string") {
        failExchange(exchange, OUTCOME_INCOMPLETE);
        return;
      }

      var reply = data && typeof data.reply === "string" ? data.reply : "";

      if (!reply.trim()) {
        failExchange(exchange, OUTCOME_INCOMPLETE);
        return;
      }

      completeExchange(exchange, reply, data.session_id);
    }, function (err) {
      // The response arrived but its body could not be read.
      failExchange(exchange, isAbort(err) ? OUTCOME_STOPPED : OUTCOME_INCOMPLETE);
    });
  }

  function requestReply(exchange) {
    setBusy(true);
    clearActions(exchange.shell);
    setThinking(exchange.shell);

    controller = new AbortController();

    var payload = {
      message: exchange.text,
      history: exchange.history,
      session_id: SESSION_ID
    };

    // Same-origin: the frontend is served at /app/ and the API at /api/, so no
    // CORS is involved. fetch's default credentials mode ("same-origin") lets
    // the browser attach any same-origin cookie; this app never sets or reads
    // one.
    return fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal
    })
      .then(function (res) {
        return handleResponse(exchange, res);
      }, function (err) {
        /* No HTTP response was received. A user Stop is "Stopped."; only a
           genuine fetch rejection means the server could not be reached.
           NOTE: /api/chat is a non-streaming provider call, so aborting the
           browser request does not guarantee the upstream model computation
           stops at that instant; the server may still finish generating and
           discard the result. */
        failExchange(exchange, isAbort(err) ? OUTCOME_STOPPED : OUTCOME_UNREACHABLE);
      })
      .then(null, function () {
        // A failure after the response arrived (processing or rendering).
        failExchange(exchange, OUTCOME_INCOMPLETE);
      })
      .then(function () {
        controller = null;
        setBusy(false);
        autoGrow();
      });
  }

  /* Remove the failed exchange's rows; it never entered TURNS. */
  function discardFailedExchange() {
    if (!failedExchange) return;

    var rows = [failedExchange.userRow, failedExchange.shell.row];
    for (var i = 0; i < rows.length; i++) {
      if (rows[i] && rows[i].parentNode) rows[i].parentNode.removeChild(rows[i]);
    }

    failedExchange = null;
  }

  // ---- send ----------------------------------------------------------------
  function send() {
    var text = input.value.trim();
    if (!text || inFlight) return;

    // A deliberate new message replaces any failed exchange still on screen.
    discardFailedExchange();

    // Snapshot history BEFORE this turn: prior completed exchanges only.
    var history = buildHistory();

    var userRow = addUserMessage(text);
    input.value = "";
    autoGrow();
    syncSendEnabled();

    stickToBottom = true;
    var shell = addAssistantShell();
    requestReply(newExchange(text, history, userRow, shell)).then(function () {
      if (!inFlight) input.focus({ preventScroll: true });
    });
  }

  /* Retry a failed or stopped exchange: the same text, the same pre-send
     history and the same rows. Nothing new is added to the thread. */
  function retryExchange(exchange) {
    if (inFlight || failedExchange !== exchange) return;

    /* Ownership is not renewed here: the exchange may restore its text again
       only if this reclaim emptied the composer itself, which leaves the
       revision unchanged. A user edit since the restore -- including clearing
       the composer -- has advanced the revision and is kept. */
    reclaimDraft(exchange);

    stickToBottom = true;
    requestReply(exchange).then(function () {
      if (!inFlight) input.focus({ preventScroll: true });
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
    failedExchange = null;

    var history = buildHistory();
    stickToBottom = true;
    var shell = addAssistantShell();
    requestReply(newExchange(turn.user, history, turn.userRow, shell)).then(function () {
      if (!inFlight) input.focus({ preventScroll: true });
    });
  }

  /* Start a new conversation in place: forget every completed turn and the
     server session, remove all rendered messages and show the empty state
     again. The composer draft is kept. No request is sent; the next message
     starts a new server session because no session id is sent. */
  function startNewConversation() {
    if (inFlight) return;

    TURNS.length = 0;
    SESSION_ID = null;
    failedExchange = null;
    controller = null;

    while (thread.firstChild) {
      thread.removeChild(thread.firstChild);
    }
    showEmptyState();

    stickToBottom = true;
    setBusy(false);
    autoGrow();
    syncSendEnabled();
    input.focus();
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
    // Any user edit (typing, paste, cut) makes this a newer draft.
    draftRevision++;
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

  /* The composer box's own padding is part of the typing area: pressing on
     it -- not on the field, Send, or any other control -- focuses the message
     field without scrolling the page. The default is prevented only for those
     padding presses, so focus never leaves the field and no stray selection
     starts. The field and every control keep their native behavior; nothing
     is sent and the text is not changed. */
  var composerShell = document.getElementById("composer-shell");
  var COMPOSER_CONTROLS = 'button, a[href], input, textarea, select, label, ' +
                          '[contenteditable]:not([contenteditable="false"])';

  composerShell.addEventListener("mousedown", function (e) {
    if (e.button !== 0) return;
    var target = e.target;
    if (target && target.closest && target.closest(COMPOSER_CONTROLS)) return;
    e.preventDefault();
    input.focus({ preventScroll: true });
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
      draftRevision++;   // the user chose new composer text
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
  // Standalone, the composer takes focus at once. Framed by the homepage it
  // does not: focusing it on load would scroll the homepage down to the chat
  // and pull keyboard focus away from the page the visitor just opened. The
  // homepage's CTAs focus it deliberately.
  if (!isEmbedded) input.focus();
})();
