/* ==========================================================================
   Kalillac AI — native chat frontend (vanilla JS)
   - Same-origin POST /api/chat
   - session_id lives ONLY in a page-memory variable. This script never reads
     or writes localStorage, IndexedDB, cookies, or any persistent store. Its
     only sessionStorage use is the homepage's one-time handoff (below): it
     removes the homepage's envelope -- except one too old for it to take,
     which it leaves, and one it could not show it took, which it puts back
     unchanged -- and keeps a bounded list of recently used random
     identifiers, never a message of its own.
   - Model output is parsed by the locally vendored marked, then sanitized by
     the locally vendored DOMPurify BEFORE it touches innerHTML. Fenced code
     with a known language label is then syntax-highlighted from its text by
     the locally vendored highlight.js (re-sanitized to plain token spans).
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

  /* Bumped by End & erase. A request started before it belongs to the erased
     conversation: when it settles, nothing it carries is shown, kept or
     restored, and it no longer owns the Send/Stop control. */
  var conversationEpoch = 0;

  /* End & erase requests, per opaque session id: { attempt, state } where
     state is "pending", "removed", "not_found" or "failed". Only an id's
     latest attempt may record its result. The map holds just the ids of the
     current erase round and empties once every one is confirmed, so it never
     accumulates. Never any content. */
  var eraseAttempts = {};
  var eraseAttemptSeq = 0;

  var conversation = document.getElementById("conversation");
  var thread = document.getElementById("thread");
  var emptyState = document.getElementById("empty");
  var form = document.getElementById("composer");
  var input = document.getElementById("composer-input");
  var actionBtn = document.getElementById("send-btn");

  // Logo fallback: the logo is shown from the first frame (its size is
  // reserved, so nothing shifts) and only a failed load swaps in the wordmark,
  // never a broken-image icon. Also handle cached load/error events that may
  // have completed before app.js attached its listeners.
  var logo = document.getElementById("brand-logo");
  var brand = logo && logo.parentNode;

  function syncLogoState() {
    if (!logo) return;
    var failed = logo.complete && logo.naturalWidth === 0;
    logo.hidden = failed;
    if (brand && brand.classList) {
      brand.classList.toggle("has-logo", !failed);
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

    // Match math only outside code, including multi-backtick spans and
    // tilde fences. Never pair delimiters across an actual code block.
    source = mapOutsideCode(source, function (part) {
      part = part.replace(/\\\[[\s\S]*?\\\]/g, stash);
      part = part.replace(/\$\$[\s\S]*?\$\$/g, stash);
      part = part.replace(/\\\([\s\S]*?\\\)/g, stash);
      return protectUnfinishedMath(part, stash, prefix);
    });

    return {
      text: source,
      items: items,
      prefix: prefix
    };
  }

  /*
   * A reply cut off inside mathematics ends with an opening delimiter that
   * has no closer, and the continuation can begin with a closer that has no
   * opener. After complete pairs are protected, keep such a fragment as
   * literal text: Marked would otherwise drop its backslashes, and it is
   * never closed or completed here. Fenced and inline code are left alone.
   */
  function mapOutsideCode(source, transform) {
    function mapped(part) {
      // A placeholder must not consume the line break that introduces the
      // next code fence, or Markdown would stop recognizing that fence.
      var suffix = /\n+$/.exec(part);
      var end = suffix ? part.length - suffix[0].length : part.length;
      return transform(part.slice(0, end)) + part.slice(end);
    }
    var boundaries = /^[ \t]*(`{3,}|~{3,})[^\n]*(?:\n|$)|(`+)/gm;
    var result = "";
    var last = 0;
    var match;
    while ((match = boundaries.exec(source)) !== null) {
      var marker = match[1] || match[2];
      var end;
      if (match[1]) {
        var closeFence = new RegExp("^[ \\t]*" + marker[0] + "{" + marker.length + ",}[ \\t]*(?:\\n|$)", "gm");
        closeFence.lastIndex = boundaries.lastIndex;
        var close = closeFence.exec(source);
        end = close ? closeFence.lastIndex : source.length;
      } else {
        var closeSpan = new RegExp("(^|[^`])`{" + marker.length + "}(?!`)", "g");
        closeSpan.lastIndex = boundaries.lastIndex;
        var span = closeSpan.exec(source);
        if (!span) continue;
        end = closeSpan.lastIndex;
      }
      result += mapped(source.slice(last, match.index)) + source.slice(match.index, end);
      last = end;
      boundaries.lastIndex = end;
    }
    return result + mapped(source.slice(last));
  }

  function protectUnfinishedMath(source, stash, prefix) {
    // Complete expressions already have placeholders. Leave an unmatched
    // opener literal, without manufacturing a closer or missing content.
    var opener = source.search(/\\\[|\\\(|\$\$/);
    if (opener !== -1) {
      var tail = source.slice(opener);
      var nextMath = tail.indexOf(prefix);
      var end = nextMath === -1 ? source.length : opener + nextMath;
      source = source.slice(0, opener) + stash(source.slice(opener, end)) + source.slice(end);
    }
    // Preserve a leading orphan fragment, without consuming completed math.
    var closer = /\\\]|\\\)/.exec(source);
    if (closer) {
      var head = source.slice(0, closer.index + closer[0].length);
      if (head.indexOf(prefix) === -1) {
        source = stash(head) + source.slice(head.length);
      } else {
        source = source.slice(0, closer.index) + stash(closer[0]) + source.slice(closer.index + closer[0].length);
      }
    }
    return source;
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
   * The server's source list (presentSourceList) is ignored too: its titles
   * are literal search-result text, never mathematics.
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
        ignoredClasses: ["source-list"],
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

  /* ---- the server's source list: presentation only ------------------------
     After a web search the server appends one list as the reply's last two
     blocks: a paragraph that is exactly **Sources** or **Search results**,
     then a bullet list with one link per entry (source_footer in
     candidate/kalillac_routing/source_trust.py). The server alone decides
     which results are listed, validates every address and limits every link
     in the answer to that list; titles arrive as literal text.

     Markdown alone cannot show who wrote a list: a model can write the same
     shape. So the server also reports the footer it appended, outside the
     reply text, as the response's source_list: { kind: "sources" |
     "search_results", count: N }, computed by application code and sent only
     while the reply still ends with exactly that footer. Without it, or when
     the displayed list's label or entry count disagrees with it, nothing is
     restyled and the reply keeps its ordinary Markdown presentation.

     This step only restyles that final list, after DOMPurify, and only when
     the sanitized DOM has exactly that shape. It reads each link's sanitized
     href and visible text and builds new nodes with textContent, so no title
     is ever parsed as HTML. It never makes anything a link that was not
     already one, never adds, drops, merges or reorders an entry, and never
     changes an address. Anything else -- including a list that is not the
     last block or an entry with extra text or markup -- is left exactly as
     rendered. Copy and history keep the original reply text. */
  // Footer label -> the server's source_list kind and this page's class suffix.
  var SOURCE_LIST_KINDS = {
    "Sources": { kind: "sources", css: "sources" },
    "Search results": { kind: "search_results", css: "search-results" }
  };
  var SOURCE_LIST_MAX = 100;
  var sourceListCount = 0;

  // The response's source_list, if it is exactly the server's shape; else null.
  function sourceListMeta(data) {
    var meta = data && data.source_list;
    if (!meta || typeof meta !== "object") return null;
    if (meta.kind !== "sources" && meta.kind !== "search_results") return null;
    var count = meta.count;
    if (typeof count !== "number" || Math.floor(count) !== count || count < 1 || count > SOURCE_LIST_MAX) return null;
    return { kind: meta.kind, count: count };
  }

  function onlyWhitespaceText(node) {
    return node.nodeType === 3 && !/\S/.test(node.nodeValue);
  }

  // The hostname a reader recognizes. The browser's own URL parser gives
  // the host exactly as the address bar would (IDNs stay in their ASCII
  // form); only a leading "www." is dropped. Never shortened.
  function sourceHost(href) {
    var parsed;
    try { parsed = new URL(href); } catch (e) { return null; }
    if ((parsed.protocol !== "http:" && parsed.protocol !== "https:") || !parsed.hostname) return null;
    return parsed.hostname.replace(/^www\./i, "");
  }

  function sourceListEntries(list) {
    var entries = [];
    var nodes = list.childNodes;
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (onlyWhitespaceText(node)) continue;
      if (node.nodeType !== 1 || node.tagName !== "LI") return null;
      var link = null;
      for (var j = 0; j < node.childNodes.length; j++) {
        var part = node.childNodes[j];
        if (onlyWhitespaceText(part)) continue;
        if (link || part.nodeType !== 1 || part.tagName !== "A") return null;
        link = part;
      }
      if (!link || link.children.length) return null;
      var href = link.getAttribute("href");
      var title = (link.textContent || "").trim();
      var host = href ? sourceHost(href) : null;
      if (!host || !title) return null;
      entries.push({ href: href, title: title, host: host });
    }
    return entries.length ? entries : null;
  }

  function presentSourceList(container, meta) {
    if (!meta) return;
    var blocks = container.children;
    if (blocks.length < 2) return;
    var heading = blocks[blocks.length - 2];
    var list = blocks[blocks.length - 1];
    if (heading.tagName !== "P" || list.tagName !== "UL") return;
    if (heading.childNodes.length !== 1) return;
    var strong = heading.firstChild;
    if (strong.nodeType !== 1 || strong.tagName !== "STRONG" || strong.children.length) return;
    var label = strong.textContent;
    if (!Object.prototype.hasOwnProperty.call(SOURCE_LIST_KINDS, label)) return;
    if (SOURCE_LIST_KINDS[label].kind !== meta.kind) return;
    var entries = sourceListEntries(list);
    if (!entries || entries.length !== meta.count) return;

    sourceListCount += 1;
    var labelId = "source-list-label-" + sourceListCount;
    var kind = SOURCE_LIST_KINDS[label].css;

    var wrap = document.createElement("div");
    wrap.className = "source-list source-list-" + kind;
    wrap.setAttribute("data-source-kind", kind);

    var caption = document.createElement("p");
    caption.className = "source-list-label";
    caption.id = labelId;
    caption.textContent = label;
    wrap.appendChild(caption);

    var items = document.createElement("ul");
    items.className = "source-list-items";
    items.setAttribute("aria-labelledby", labelId);

    for (var i = 0; i < entries.length; i++) {
      var item = document.createElement("li");
      item.className = "source-item";
      var link = document.createElement("a");
      link.className = "source-link";
      link.setAttribute("href", entries[i].href);
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");

      var title = document.createElement("span");
      title.className = "source-title";
      title.textContent = entries[i].title;
      var host = document.createElement("span");
      host.className = "source-host";
      host.textContent = entries[i].host;
      var hint = document.createElement("span");
      hint.className = "sr-only";
      hint.textContent = " (opens in a new tab)";

      link.appendChild(title);
      link.appendChild(host);
      link.appendChild(hint);
      item.appendChild(link);
      items.appendChild(item);
    }
    wrap.appendChild(items);

    container.replaceChild(wrap, list);
    container.removeChild(heading);
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
    placeStarter();
  }

  function showEmptyState() {
    if (emptyState && !emptyState.parentNode) {
      thread.appendChild(emptyState);
    }
    conversation.classList.add("is-empty");
    placeStarter();
  }

  /* The example prompts belong to the empty state. They sit in the dock after
     the composer, so the DOM -- and with it the keyboard and screen-reader
     order -- is heading, composer, then the examples, exactly as they appear.
     Once a conversation starts they are detached. */
  var starter = document.getElementById("starter");
  var starterDock = document.getElementById("starter-dock");

  function placeStarter() {
    if (!starter || !emptyState) return;
    var home = starterDock || emptyState;
    if (!emptyState.parentNode) {
      if (starter.parentNode) starter.parentNode.removeChild(starter);
    } else if (starter.parentNode !== home) {
      home.appendChild(starter);
    }
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

  function renderAssistantText(shell, text, sourceList) {
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
    // Before KaTeX, which skips the presented list: titles stay literal text.
    presentSourceList(shell.body, sourceList);
    renderMathSafe(shell.body);

    wrapTables(shell.body);
    hardenLinks(shell.body);
    highlightCode(shell.body);
    addCodeCopy(shell.body);
  }

  /* Each fenced block gets its own header: the language the fence named (if
     any) and a Copy control for that block alone. It copies the block's text
     content exactly as the reply carried it, without the final line break;
     highlighting never changes that text. The header is built with
     createElement and textContent, so nothing from the reply is parsed as
     HTML here. Inline code gets no control, and the answer's own Copy still
     copies the whole stored reply. */
  var CODE_LABEL_MAX = 24;

  function addCodeCopy(container) {
    if (!container) return;
    var blocks = container.querySelectorAll("pre > code");
    for (var i = 0; i < blocks.length; i++) {
      var code = blocks[i];
      var pre = code.parentNode;
      var parent = pre.parentNode;
      if (!parent || (parent.classList && parent.classList.contains("code-block"))) continue;

      var wrap = document.createElement("div");
      wrap.className = "code-block";
      var bar = document.createElement("div");
      bar.className = "code-bar";

      var match = CODE_LANGUAGE_RE.exec(code.className || "");
      var label = document.createElement("span");
      label.className = "code-lang";
      label.textContent = match ? match[1].slice(0, CODE_LABEL_MAX) : "";

      var button = document.createElement("button");
      button.type = "button";
      button.className = "code-copy";
      button.setAttribute("aria-label", match ? "Copy " + label.textContent + " code" : "Copy code");
      button.innerHTML = ICON_COPY + "<span>Copy</span>";
      button.addEventListener("click", copyCodeHandler(code, button));

      bar.appendChild(label);
      bar.appendChild(button);
      parent.insertBefore(wrap, pre);
      wrap.appendChild(bar);
      wrap.appendChild(pre);
    }
  }

  function copyCodeHandler(code, button) {
    return function () {
      copyText((code.textContent || "").replace(/\n$/, ""), button);
    };
  }

  /* Syntax highlighting for fenced code, presentation only. It runs on the
     already-sanitized DOM and starts from each block's text content, never
     its markup, so model text is never parsed as HTML. highlight.js escapes
     that text into token spans; the result is sanitized again to spans with
     class attributes only, and kept only if its text equals the original
     exactly. Unlabeled fences, unknown labels and inline code stay plain.
     Copy uses the stored reply text, so token markup is never copied. */
  var CODE_LANGUAGE_RE = /(?:^|\s)language-([\w#+.-]+)(?:\s|$)/;
  var HIGHLIGHT_PURIFY_CONFIG = {
    ALLOWED_TAGS: ["span"],
    ALLOWED_ATTR: ["class"],
    ALLOW_DATA_ATTR: false
  };

  function highlightCode(container) {
    var hljs = window.hljs;
    if (!container || !hljs || typeof hljs.highlight !== "function" ||
        typeof hljs.getLanguage !== "function" || !window.DOMPurify) {
      return;
    }

    var blocks = container.querySelectorAll("pre > code");
    for (var i = 0; i < blocks.length; i++) {
      var code = blocks[i];
      var match = CODE_LANGUAGE_RE.exec(code.className || "");
      if (!match || !hljs.getLanguage(match[1])) continue;

      var source = code.textContent;
      try {
        var tokens = hljs.highlight(source, { language: match[1], ignoreIllegals: true }).value;
        code.innerHTML = window.DOMPurify.sanitize(tokens, HIGHLIGHT_PURIFY_CONFIG);
        if (code.textContent === source) {
          code.classList.add("hljs");
        } else {
          code.textContent = source;
        }
      } catch (e) {
        code.textContent = source;
      }
    }
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
    // A saved Space answer is already stored: regenerating it here would leave the screen and the
    // saved conversation different, so it has Copy only.
    if (!SPACE) bar.appendChild(retryBtn);
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

  // ---- send authority ------------------------------------------------------
  /* The one source of truth for whether this page may send what is in the
     message box, or clear or replace it. It is withheld only while the box
     holds a homepage message this page could not show it took (see the
     handoff at the end of this file): the homepage may still own that message
     and give it back, so here it stays read-only, for selecting and copying,
     and nothing sends, clears or replaces it. Every request is built by
     requestReply(), and it and every way into it -- send(), which Enter,
     Send and the form all call, retryExchange() and retryTurn() -- refuse
     without authority, as do End & erase and the example prompts. Nothing
     grants it back in this page: no timer, deadline, navigation, cache
     restore or later showing. It is kept on the form itself, so any other
     copy of this script in the document reads the same answer. */
  function hasAuthority() {
    return form.getAttribute("data-authority") !== "withheld";
  }

  // ---- busy / send / stop state -------------------------------------------
  function setBusy(busy) {
    inFlight = busy;
    actionBtn.classList.toggle("is-stop", busy);
    actionBtn.setAttribute("aria-label", busy ? "Stop generating" : "Send message");
    actionBtn.title = busy ? "Stop" : "Send";
    actionBtn.disabled = false;                 // Stop must stay clickable
    if (!busy) syncSendEnabled();
    input.readOnly = busy || !hasAuthority();   // readOnly, not disabled, so the
                                                // iOS keyboard does not dismiss
    syncEraseEnabled();
  }

  function syncSendEnabled() {
    syncEraseEnabled();
    if (inFlight) return;
    if (!hasAuthority()) {
      actionBtn.disabled = true;
      return;
    }
    actionBtn.disabled = limitHeld || input.value.trim().length === 0;
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
  var COPY_DAILY_LIMIT = "You’ve used today’s 10 free responses.";

  var OUTCOME_UNREACHABLE = { message: COPY_UNREACHABLE, kind: "error", action: "retry" };
  var OUTCOME_INCOMPLETE = { message: COPY_INCOMPLETE, kind: "error", action: "retry" };
  var OUTCOME_BUSY = { message: COPY_BUSY, kind: "error", action: "retry" };
  var OUTCOME_STOPPED = { message: COPY_STOPPED, kind: "notice", action: "retry" };
  /* The anonymous daily allowance is used up: a neutral notice, no Retry, and
     the fixed daily-limit dialog. */
  var OUTCOME_DAILY_LIMIT = { message: COPY_DAILY_LIMIT, kind: "notice", action: null, dialog: true };
  /* The conversation's session was ended (End & erase, possibly elsewhere): the
     next message starts a new session. */
  /* A signed-in account's own daily limit (a 429 only). No number is shown: the
     allowances are the deployment's, and the day is the UTC day. */
  var OUTCOME_ACCOUNT_DAILY_LIMIT = {
    message: "You’ve reached today’s message limit for this account. " +
             "It resets at 12:00 AM UTC.",
    kind: "notice", action: null
  };
  var OUTCOME_SESSION_ENDED = {
    message: "This conversation was ended. Your next message starts a new one.",
    kind: "notice", action: null
  };

  var OUTCOME_FILE_FAILED = {
    message: "Kalillac couldn't finish working on that file. Please try again.", kind: "error", action: "retry"
  };

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
    },
    anonymous_daily_limit_reached: OUTCOME_DAILY_LIMIT,
    daily_limit_reached: OUTCOME_ACCOUNT_DAILY_LIMIT,
    session_ended: OUTCOME_SESSION_ENDED,
    // The account itself was refused (V1 account security): no Retry.
    account_unavailable: { message: "This account isn’t available right now.", kind: "error", action: null },
    unsupported_media_type: { message: COPY_UNREADABLE, kind: "error", action: "retry" },
    // A reasoning mode the account's tier may not use (never silently downgraded).
    reasoning_mode_not_allowed: {
      message: "That reasoning mode isn't available for this account.", kind: "error", action: null
    },
    // Files (a files deployment only).
    files_require_premium: { message: "Files aren't available for this account.", kind: "error", action: null },
    // The cookie's session is not live (signed out, expired or ended elsewhere): chat, files, Spaces.
    not_authenticated: { message: "Sign in to continue.", kind: "error", action: null },
    file_not_found: {
      message: "An attached file is no longer available. Remove it and try again.", kind: "error", action: null
    },
    // Images (an images deployment): more than one message shows, or a stored image that cannot be used.
    too_many_images: { message: "Attach at most 4 images to one message.", kind: "error", action: null },
    image_unavailable: {
      message: "An attached image is no longer available. Remove it and try again.", kind: "error", action: null
    },
    file_assistant_unavailable: OUTCOME_FILE_FAILED,
    model_output_unusable: OUTCOME_FILE_FAILED,
    model_output_incomplete: OUTCOME_FILE_FAILED,
    model_refused: OUTCOME_FILE_FAILED,
    tool_limit_exceeded: OUTCOME_FILE_FAILED
  };

  /* An HTTP response arrived but was not a usable reply. A recognized fixed
     code wins; a bare 429 is busy; anything else could not be completed. */
  function outcomeForResponse(status, data) {
    var code = data && typeof data.error === "string" ? data.error : null;

    // The daily limit is only ever a 429; the same code on any other status
    // is not a usable outcome.
    if ((code === "anonymous_daily_limit_reached" || code === "daily_limit_reached") && status !== 429) {
      code = null;
    }

    if (code && Object.prototype.hasOwnProperty.call(ERROR_OUTCOMES, code)) {
      return ERROR_OUTCOMES[code];
    }

    if (status === 429) return OUTCOME_BUSY;
    return OUTCOME_INCOMPLETE;
  }

  function isAbort(err) {
    return !!err && err.name === "AbortError";
  }

  // ---- daily-limit dialog ----------------------------------------------------
  /* Its copy is fixed markup in index.html; nothing from the response is shown
     in it. The native modal dialog keeps keyboard focus inside itself and makes
     the page behind it inert while open. Got it, Escape (the cancel event) and
     a click on the backdrop all close it through closeLimitDialog(), which
     returns focus to the composer. Without showModal() only the inline thread
     note is shown. */
  var limitDialog = document.getElementById("limit-dialog");
  var limitDialogOk = document.getElementById("limit-dialog-ok");
  var limitNotice = document.getElementById("limit-notice");

  function limitDialogOpen() {
    return !!(limitDialog && limitDialog.open);
  }

  /* Returns whether the modal dialog opened. */
  function openLimitDialog() {
    if (!limitDialog || !limitDialogOk || typeof limitDialog.showModal !== "function") return false;
    try {
      if (!limitDialog.open) limitDialog.showModal();
      limitDialogOk.focus();
      return true;
    } catch (e) {
      // A dialog that cannot open leaves the composer note as the only notice.
      return false;
    }
  }

  function closeLimitDialog() {
    if (limitDialog && limitDialog.open) limitDialog.close();
    showLimitNotice();
    input.focus({ preventScroll: true });
  }

  /* The allowance hold. The server's 429 is the only authority: this page
     never counts responses. After it, sending pauses on this page -- the
     composer stays editable -- until 00:00 UTC, the documented reset of the
     anonymous allowance, read from this device's clock. A wrong clock only
     moves when the pause lifts; the server still decides, and a refusal after
     that simply shows this flow again. A reload also lifts it. */
  var DAY_MS = 86400000;
  var limitHeld = false;
  var limitHoldTimer = null;

  function msUntilUtcReset() {
    return DAY_MS - (Date.now() % DAY_MS) + 1000;
  }

  function holdForDailyLimit() {
    limitHeld = true;
    if (limitHoldTimer !== null) clearTimeout(limitHoldTimer);
    limitHoldTimer = setTimeout(releaseDailyLimit, msUntilUtcReset());
    actionBtn.setAttribute("aria-describedby", "limit-notice");
    syncSendEnabled();
  }

  function releaseDailyLimit() {
    limitHeld = false;
    limitHoldTimer = null;
    if (limitNotice) limitNotice.hidden = true;
    actionBtn.removeAttribute("aria-describedby");
    syncSendEnabled();
  }

  function showLimitNotice() {
    if (limitNotice && limitHeld) limitNotice.hidden = false;
  }

  /* A refused prompt was never sent as a turn: its optimistic rows are
     removed (the empty state returns if nothing else is on screen), its text
     goes back into the composer, and one dialog explains why. */
  function rejectForDailyLimit(exchange) {
    var rows = [exchange.userRow, exchange.shell.row];
    for (var i = 0; i < rows.length; i++) {
      if (rows[i] && rows[i].parentNode) rows[i].parentNode.removeChild(rows[i]);
    }
    if (failedExchange === exchange) failedExchange = null;
    if (!thread.querySelector(".msg")) showEmptyState();

    restoreDraft(exchange);
    holdForDailyLimit();
    if (!openLimitDialog()) showLimitNotice();
  }

  /* After a send settles, the composer takes focus again unless the
     daily-limit dialog holds it. */
  function focusComposer() {
    if (!inFlight && !limitDialogOpen()) input.focus({ preventScroll: true });
  }

  if (limitDialog && limitDialogOk) {
    limitDialogOk.addEventListener("click", closeLimitDialog);
    limitDialog.addEventListener("cancel", function (event) {
      event.preventDefault();
      closeLimitDialog();
    });
    // The panel fills the dialog box, so only a backdrop click targets the
    // dialog element itself.
    limitDialog.addEventListener("click", function (event) {
      if (event.target === limitDialog) closeLimitDialog();
    });
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
    // The daily limit is not an answer: no Kalillac row, no Retry.
    if (outcome.dialog) {
      rejectForDailyLimit(exchange);
      return;
    }
    clearActions(exchange.shell);
    showNote(exchange.shell, outcome.message, outcome.kind);

    if (outcome.action === "retry") {
      addFailureAction(exchange.shell, "retry", function () { retryExchange(exchange); });
    } else if (outcome.action === "new-conversation") {
      addFailureAction(exchange.shell, "new-conversation", startNewConversation);
    }

    if (outcome === OUTCOME_SESSION_ENDED) SESSION_ID = null;

    failedExchange = exchange;
    restoreDraft(exchange);
  }

  /* --- Files ------------------------------------------------------------
     Offered only when GET /api/files answers 200: files are enabled and this
     signed-in account has access. A reply may carry files Kalillac made; they
     are shown as download links built with DOM APIs (never HTML), each id
     checked against the server's format. Attachments are uploaded as the raw
     file body and sent with the next message as file_ids. */
  var FILE_ID_RE = /^[A-Za-z0-9_-]{22}$/;
  var MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
  var FILE_KINDS = {
    "application/pdf": "PDF",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint",
    "text/csv; charset=utf-8": "CSV",
    "text/markdown; charset=utf-8": "Markdown",
    "text/plain; charset=utf-8": "Text",
    "application/json": "JSON",
    "image/png": "PNG image",
    "image/jpeg": "JPEG image"
  };
  /* Images (a files deployment that also marks <html data-kalillac-images="on">): photos and
     screenshots can be attached too. The server keeps only its own clean copy of each; the
     attachment shows that copy as a small preview, fetched as a blob, never the file the
     visitor chose. */
  var IMAGES_OFFERED = document.documentElement.getAttribute("data-kalillac-images") === "on";
  var IMAGE_MEDIA = { "image/png": true, "image/jpeg": true };
  // A refused upload gets one fixed line; nothing from the response is shown.
  var UPLOAD_REFUSALS = {
    file_too_large: "That file is larger than 10 MB.",
    image_too_large: "That image is too large. Try a smaller one.",
    animated_image_unsupported: "Animated images can’t be attached. Try a still image.",
    unreadable_image: "That image couldn’t be read.",
    unsupported_type: "That type of file can’t be attached.",
    type_mismatch: "That file’s name doesn’t match its contents.",
    file_count_quota_exceeded: "Your files are full. Delete some to attach more.",
    storage_quota_exceeded: "Your files are full. Delete some to attach more."
  };
  var UPLOAD_FAILED = "Kalillac couldn’t attach that file. Please try again.";
  var attachmentList = document.getElementById("attachments");
  var attachStatus = document.getElementById("attach-status");
  // The attach button and its file chooser exist only on a files deployment: the page itself carries
  // neither, so an ordinary visitor's page exposes no upload control at all. The button stays hidden
  // until the server says this account may use files.
  var attachBtn = null;
  var attachInput = null;
  var attachTemplate = document.getElementById("attach-template");
  if (document.documentElement.getAttribute("data-kalillac-files") === "on" && attachTemplate
      && attachTemplate.content && attachTemplate.content.firstElementChild) {
    attachBtn = attachTemplate.content.firstElementChild.cloneNode(true);
    attachTemplate.parentNode.insertBefore(attachBtn, attachTemplate);
    attachInput = document.createElement("input");
    attachInput.type = "file";
    attachInput.id = "attach-input";
    attachInput.hidden = true;
    attachInput.accept = ".docx,.xlsx,.pptx,.pdf,.txt,.md,.csv,.json" +
      (IMAGES_OFFERED ? ",.png,.jpg,.jpeg,.webp,.gif" : "");
    attachBtn.parentNode.insertBefore(attachInput, attachBtn.nextSibling);
  }
  var ATTACHED = [];   // [{id, filename, thumb}] for the next message; thumb: a blob URL or null

  function clearAttached() {
    ATTACHED.forEach(function (f) { if (f.thumb) URL.revokeObjectURL(f.thumb); });
    ATTACHED = [];
    renderAttachments();
  }

  function showAttachStatus(message) {
    if (!attachStatus) return;
    attachStatus.textContent = message || "";
    attachStatus.hidden = !message;
  }

  // The stored copy of an attached image, as a preview. Only a PNG or JPEG blob is shown.
  function loadThumb(entry) {
    fetch("/api/files/" + encodeURIComponent(entry.id), { method: "GET" })
      .then(function (res) { return res.ok ? res.blob() : null; })
      .then(function (blob) {
        if (!blob || !IMAGE_MEDIA[blob.type] || ATTACHED.indexOf(entry) === -1) return;
        entry.thumb = URL.createObjectURL(blob);
        renderAttachments();
      }, function () {});
  }

  function fileSize(bytes) {
    if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    return Math.max(1, Math.round(bytes / 1024)) + " KB";
  }

  function filesFrom(data) {
    if (!data || !Array.isArray(data.files)) return [];
    return data.files.filter(function (f) {
      return f && typeof f.id === "string" && FILE_ID_RE.test(f.id) &&
        typeof f.filename === "string" && f.filename && typeof f.size === "number";
    });
  }

  function renderFiles(shell, files) {
    if (!files.length) return;
    var list = document.createElement("ul");
    list.className = "file-list";
    list.setAttribute("aria-label", "Files from Kalillac");
    files.forEach(function (f) {
      var item = document.createElement("li");
      var link = document.createElement("a");
      link.className = "file-chip";
      link.href = "/api/files/" + encodeURIComponent(f.id);
      link.setAttribute("download", f.filename);
      var name = document.createElement("span");
      name.className = "file-name";
      name.textContent = f.filename;
      var meta = document.createElement("span");
      meta.className = "file-meta";
      meta.textContent = (FILE_KINDS[f.media_type] || "File") + " · " + fileSize(f.size);
      link.appendChild(name);
      link.appendChild(meta);
      item.appendChild(link);
      list.appendChild(item);
    });
    shell.bubble.appendChild(list);
  }

  function renderAttachments() {
    if (!attachmentList) return;
    while (attachmentList.firstChild) attachmentList.removeChild(attachmentList.firstChild);
    ATTACHED.forEach(function (f, index) {
      var item = document.createElement("li");
      item.className = "attachment";
      if (f.thumb) {
        var preview = document.createElement("img");
        preview.className = "attachment-thumb";
        preview.src = f.thumb;
        preview.alt = "";                 // The name beside it says what it is.
        item.appendChild(preview);
      }
      var name = document.createElement("span");
      name.textContent = f.filename;
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "attachment-remove";
      remove.setAttribute("aria-label", "Remove " + f.filename);
      remove.textContent = "×";
      remove.addEventListener("click", function () {
        var removed = ATTACHED.splice(index, 1)[0];
        if (removed && removed.thumb) URL.revokeObjectURL(removed.thumb);
        renderAttachments();
      });
      item.appendChild(name);
      item.appendChild(remove);
      attachmentList.appendChild(item);
    });
    attachmentList.hidden = ATTACHED.length === 0;
    // Files are not offered inside a Space, so an attachment always belongs to a temporary conversation:
    // say plainly that the file itself is kept in the account.
    var note = document.getElementById("attach-note");
    if (note) note.hidden = ATTACHED.length === 0;
  }

  /* --- Account link ------------------------------------------------------
     A deployment with accounts on marks the page
     (<html data-kalillac-accounts="on">); only then does the header link to
     the account pages, where the plan, billing and security settings live. It
     is plain navigation: this page makes no request for it and learns nothing
     about the visitor. On such a deployment the two plan refusals also say
     where the plan is shown. */
  var ACCOUNTS_OFFERED = document.documentElement.getAttribute("data-kalillac-accounts") === "on";
  var accountTemplate = document.getElementById("account-link-template");
  if (ACCOUNTS_OFFERED && accountTemplate && accountTemplate.content &&
      accountTemplate.content.firstElementChild) {
    accountTemplate.parentNode.insertBefore(
      accountTemplate.content.firstElementChild.cloneNode(true), accountTemplate);
  }
  if (ACCOUNTS_OFFERED) {
    ERROR_OUTCOMES.reasoning_mode_not_allowed = {
      message: "That reasoning mode isn't part of this account's plan. Account shows your plan.",
      kind: "error", action: null
    };
    ERROR_OUTCOMES.files_require_premium = {
      message: "Files aren't part of this account's plan. Account shows your plan.",
      kind: "error", action: null
    };
  }

  /* --- Reasoning modes ---------------------------------------------------
     A deployment with reasoning modes on marks the page
     (<html data-kalillac-reasoning="on">); only then is the picker shown and a
     mode sent. The server decides what each mode means and who may use it; a
     refusal arrives as the fixed reasoning_mode_not_allowed copy. The choice
     lives only as long as the page: nothing is stored in the browser. */
  var modePicker = document.getElementById("mode-picker");
  var REASONING_OFFERED = document.documentElement.getAttribute("data-kalillac-reasoning") === "on";
  var reasoningMode = null;

  function modeOptions() {
    return modePicker ? Array.prototype.slice.call(modePicker.querySelectorAll(".mode-option")) : [];
  }

  function selectMode(mode, focus) {
    var options = modeOptions();
    var chosen = options.filter(function (o) { return o.getAttribute("data-mode") === mode; })[0];
    if (!chosen) return;
    options.forEach(function (o) {
      var on = o === chosen;
      o.setAttribute("aria-checked", on ? "true" : "false");
      o.tabIndex = on ? 0 : -1;
    });
    reasoningMode = mode;
    if (focus) chosen.focus();
  }

  if (REASONING_OFFERED && modePicker) {
    modePicker.hidden = false;
    selectMode("balanced", false);
    modeOptions().forEach(function (option, index, all) {
      option.addEventListener("click", function () { selectMode(option.getAttribute("data-mode"), false); });
      option.addEventListener("keydown", function (event) {
        var step = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 :
                   event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 0;
        if (!step) return;
        event.preventDefault();
        var next = all[(index + step + all.length) % all.length];
        selectMode(next.getAttribute("data-mode"), true);
      });
    });
  }

  function uploadFile(file) {
    if (ATTACHED.length >= 5) return;
    if (file.size > MAX_UPLOAD_BYTES) {
      showAttachStatus(UPLOAD_REFUSALS.file_too_large);
      return;
    }
    showAttachStatus("");
    attachBtn.disabled = true;
    fetch("/api/files", {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Kalillac-Filename": encodeURIComponent(file.name)
      },
      body: file
    })
      .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
      .then(function (result) {
        var f = result.ok && result.data && result.data.file;
        if (f && typeof f.id === "string" && FILE_ID_RE.test(f.id)) {
          var entry = { id: f.id, filename: typeof f.filename === "string" ? f.filename : file.name, thumb: null };
          ATTACHED.push(entry);
          renderAttachments();
          if (IMAGES_OFFERED && IMAGE_MEDIA[f.media_type]) loadThumb(entry);
          return;
        }
        var code = result.data && typeof result.data.error === "string" ? result.data.error : null;
        showAttachStatus(code && Object.prototype.hasOwnProperty.call(UPLOAD_REFUSALS, code)
          ? UPLOAD_REFUSALS[code] : UPLOAD_FAILED);
      }, function () { showAttachStatus(UPLOAD_FAILED); })
      .then(function () { attachBtn.disabled = false; });
  }

  // A deployment with files enabled marks the page (<html data-kalillac-files="on">). Without the mark
  // nothing here runs and no request is made, so every other visitor's page behaves exactly as before.
  var FILES_OFFERED = document.documentElement.getAttribute("data-kalillac-files") === "on";
  var FILES_KEPT_NOTE = "Files you attach or Kalillac creates are stored separately in your account and stay available for 30 days.";
  if (FILES_OFFERED && attachBtn && attachInput && attachmentList) {
    attachBtn.addEventListener("click", function () { attachInput.click(); });
    attachInput.addEventListener("change", function () {
      if (attachInput.files && attachInput.files[0]) uploadFile(attachInput.files[0]);
      attachInput.value = "";
    });
    // Files are offered only to an account the server says may use them.
    fetch("/api/files", { method: "GET" }).then(function (res) {
      if (res.status !== 200) return;
      attachBtn.hidden = false;
      // This account keeps files: the temporary session's description says where they go.
      var status = document.getElementById("session-status");
      if (status) status.title += " " + FILES_KEPT_NOTE;
    }, function () {});
  }

  /* Render the reply and its actions first; only a fully rendered answer
     becomes a completed turn. */
  /* Exact-edit confirmations (files): when the file assistant holds back an edit because a total would
     no longer add up, the reply carries single-use confirmation ids the server issued for exactly that
     edit. The customer may confirm one here; it goes with one fixed message and the file, and the server
     applies the edit only if it is exactly the one confirmed. */
  var CONFIRM_TEXT = "Apply that edit exactly as I asked.";
  var pendingConfirmation = null;   // {id, fileId}, taken by the next request only

  function confirmationsFrom(data) {
    if (!data || !Array.isArray(data.confirmations)) return [];
    return data.confirmations.filter(function (c) {
      return c && typeof c.id === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(c.id) &&
        typeof c.file_id === "string" && FILE_ID_RE.test(c.file_id);
    }).slice(0, 4);
  }

  function renderConfirmations(shell, confirmations) {
    if (!confirmations || !confirmations.length) return;
    var box = document.createElement("div");
    box.className = "edit-confirm";
    var note = document.createElement("p");
    note.textContent = "This edit was held back because a total would no longer add up.";
    box.appendChild(note);
    confirmations.forEach(function (c) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "edit-confirm-btn";
      button.textContent = "Apply exactly as asked";
      button.addEventListener("click", function () {
        if (inFlight || limitHeld || !hasAuthority()) return;
        Array.prototype.forEach.call(box.querySelectorAll("button"), function (b) { b.disabled = true; });
        pendingConfirmation = { id: c.id, fileId: c.file_id };
        sendFixed(CONFIRM_TEXT);
      });
      box.appendChild(button);
    });
    shell.bubble.appendChild(box);
  }

  function completeExchange(exchange, reply, sessionId, sourceList, files, confirmations) {
    var index = TURNS.length;

    try {
      renderAssistantText(exchange.shell, reply, sourceList);
      renderFiles(exchange.shell, files || []);
      renderConfirmations(exchange.shell, confirmations);
      if (exchange.fileIds && exchange.fileIds.length) {
        clearAttached();
      }
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
      if (exchange.epoch !== conversationEpoch) return;   // erased meanwhile
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

      completeExchange(exchange, reply, data.session_id, sourceListMeta(data), filesFrom(data),
                       FILES_OFFERED ? confirmationsFrom(data) : []);
    }, function (err) {
      if (exchange.epoch !== conversationEpoch) return;   // erased meanwhile
      // The response arrived but its body could not be read.
      failExchange(exchange, isAbort(err) ? OUTCOME_STOPPED : OUTCOME_INCOMPLETE);
    });
  }

  function requestReply(exchange) {
    if (!hasAuthority()) return Promise.resolve();   // never a request without authority
    if (SPACE) return requestSpaceReply(exchange);
    exchange.epoch = conversationEpoch;
    setBusy(true);
    clearActions(exchange.shell);
    setThinking(exchange.shell);

    controller = new AbortController();

    var payload = {
      message: exchange.text,
      history: exchange.history,
      session_id: SESSION_ID
    };
    // A confirmation belongs to the exchange it was given for; a retry of that exchange keeps it.
    if (exchange.confirmation === undefined) {
      exchange.confirmation = pendingConfirmation;
      pendingConfirmation = null;
    }
    // Attachments go with this message; they are cleared once its reply is shown.
    exchange.fileIds = ATTACHED.map(function (f) { return f.id; });
    if (exchange.confirmation) {
      if (exchange.fileIds.indexOf(exchange.confirmation.fileId) === -1) {
        exchange.fileIds.push(exchange.confirmation.fileId);
      }
      payload.file_confirmations = [exchange.confirmation.id];
    }
    if (exchange.fileIds.length) payload.file_ids = exchange.fileIds;
    if (reasoningMode) payload.reasoning_mode = reasoningMode;

    // Same-origin: the frontend is served at /app/ and the API at /api/, so no
    // CORS is involved. fetch's default credentials mode ("same-origin") lets
    // the browser attach same-origin cookies (the server's HttpOnly daily
    // allowance cookie, when that feature is on); this script never reads or
    // writes cookies.
    return fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal
    })
      .then(function (res) {
        if (exchange.epoch !== conversationEpoch) return;   // erased meanwhile
        return handleResponse(exchange, res);
      }, function (err) {
        if (exchange.epoch !== conversationEpoch) return;   // erased meanwhile
        /* No HTTP response was received. A user Stop is "Stopped."; only a
           genuine fetch rejection means the server could not be reached.
           NOTE: /api/chat is a non-streaming provider call, so aborting the
           browser request does not guarantee the upstream model computation
           stops at that instant; the server may still finish generating and
           discard the result. */
        failExchange(exchange, isAbort(err) ? OUTCOME_STOPPED : OUTCOME_UNREACHABLE);
      })
      .then(null, function () {
        if (exchange.epoch !== conversationEpoch) return;   // erased meanwhile
        // A failure after the response arrived (processing or rendering).
        failExchange(exchange, OUTCOME_INCOMPLETE);
      })
      .then(function () {
        // After End & erase this request no longer owns the controls.
        if (exchange.epoch !== conversationEpoch) return;
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
    if (!hasAuthority()) return;     // before anything in the page changes
    sendText(input.value.trim(), false);
  }

  /* A message the page sends for the customer (a confirmation): the composer and its draft are left
     exactly as they are. Like every way into a request, it refuses without the send authority. */
  function sendFixed(text) {
    if (!hasAuthority()) {
      pendingConfirmation = null;
      return;
    }
    sendText(text, true);
  }

  function sendText(text, fixed) {
    if (fixed && (inFlight || limitHeld)) pendingConfirmation = null;   // Never left for a later message.
    // Paused by the daily limit: Enter, Send and touch all end here.
    if (!text || inFlight || limitHeld) return;

    // A deliberate new message replaces any failed exchange still on screen.
    discardFailedExchange();
    dismissSettledEraseReceipt();
    hideHandoffNotice();

    // Snapshot history BEFORE this turn: prior completed exchanges only.
    var history = buildHistory();

    var userRow = addUserMessage(text);
    if (!fixed) {
      input.value = "";
      autoGrow();
      syncSendEnabled();
    }

    stickToBottom = true;
    var shell = addAssistantShell();
    requestReply(newExchange(text, history, userRow, shell)).then(focusComposer);
  }

  /* Retry a failed or stopped exchange: the same text, the same pre-send
     history and the same rows. Nothing new is added to the thread. */
  function retryExchange(exchange) {
    if (!hasAuthority()) return;
    if (inFlight || limitHeld || failedExchange !== exchange) return;

    /* Ownership is not renewed here: the exchange may restore its text again
       only if this reclaim emptied the composer itself, which leaves the
       revision unchanged. A user edit since the restore -- including clearing
       the composer -- has advanced the revision and is kept. */
    reclaimDraft(exchange);

    stickToBottom = true;
    requestReply(exchange).then(focusComposer);
  }

  /* Retry regenerates the answer for TURN index i.
     Everything from turn i onward is removed from BOTH the DOM and TURNS, then
     the original user message is resent with the history that preceded it. The
     user message is not re-appended to the DOM, so it is never duplicated, and
     buildHistory() stays exactly in step with what is on screen. */
  function retryTurn(index) {
    if (!hasAuthority()) return;
    if (inFlight || limitHeld) return;
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
    requestReply(newExchange(turn.user, history, turn.userRow, shell)).then(focusComposer);
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

  // ---- Spaces (saved conversations) ------------------------------------------------
  /* A deployment with Spaces on marks the page (<html data-kalillac-spaces="on">). Only then does
     the page ask, once, whether this browser's account may use Spaces (GET /api/spaces); only if it
     may is the Spaces button shown. Without the mark nothing here runs and nothing is asked.

     A conversation opened from a Space is saved to the account. While one is open: the bar above the
     thread says so and the "Temporary session" status is hidden (it would be untrue); End & erase,
     which is for the temporary session, is hidden; files are not offered (a Space message carries
     none); a reasoning mode, when offered, goes with the message. Each message is sent to that
     conversation with an idempotency key; a
     retry of a failed or stopped send reuses it, so the server can never save the same message
     twice. Leaving returns to a new temporary conversation. Names and messages from the server are
     only ever set as text, or rendered through the same sanitizer as any reply. */
  var SPACES_OFFERED = document.documentElement.getAttribute("data-kalillac-spaces") === "on";
  var SPACE = null;            // {space: {id, name}, conversation: {id, title}, restore} while one is open
  var SPACE_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  var SPACE_NAME_MAX = 80;
  var SPACE_TITLE_MAX = 120;
  var SPACE_PAGE_MAX = 50;
  var SPACE_MESSAGES_MAX = 100;
  var COPY_SPACES_FAILED = "Kalillac couldn't load your Spaces. Please try again.";
  var COPY_SPACES_CREATE_FAILED = "Kalillac couldn't create that. Please check the name and try again.";
  var COPY_SPACES_EMPTY = "No Spaces yet. Create one below.";
  var COPY_CONVERSATIONS_EMPTY = "No conversations in this Space yet. Start one below.";
  var spacesBtn = null;
  var spacesTemplate = document.getElementById("spaces-button-template");
  var spacesDialog = document.getElementById("spaces-dialog");
  var spacesTitle = document.getElementById("spaces-dialog-title");
  var spacesStatus = document.getElementById("spaces-status");
  var spacesList = document.getElementById("spaces-list");
  var spacesNew = document.getElementById("spaces-new");
  var spacesNewName = document.getElementById("spaces-new-name");
  var spacesNewLabel = document.getElementById("spaces-new-label");
  var spacesBack = document.getElementById("spaces-back");
  var spacesClose = document.getElementById("spaces-close");
  var spaceBar = document.getElementById("space-bar");
  var spaceBarName = document.getElementById("space-bar-name");
  var spaceBarTitle = document.getElementById("space-bar-title");
  var spaceLeave = document.getElementById("space-leave");
  var sessionStatus = document.getElementById("session-status");
  var spacesView = null;       // {kind: "spaces"} or {kind: "space", space}; replaced on every change

  function newRequestId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    var b = new Uint8Array(16);
    window.crypto.getRandomValues(b);
    b[6] = (b[6] & 15) | 64;
    b[8] = (b[8] & 63) | 128;
    var h = Array.prototype.map.call(b, function (x) { return (x + 256).toString(16).slice(1); }).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }

  function spacesRequest(method, url, body) {
    var init = { method: method };
    if (body !== undefined) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    return fetch(url, init).then(function (res) {
      return res.text().then(function (text) {
        return { status: res.status, ok: res.ok, data: parseJson(text) };
      });
    });
  }

  function validName(value, max) {
    return typeof value === "string" && value.length > 0 && value.length <= max;
  }

  function spaceUrl(spaceId, rest) {
    return "/api/spaces/" + spaceId + (rest || "");
  }

  // A failure (one of the fixed failure messages) is marked so it reads as a problem.
  function setSpacesStatus(text) {
    spacesStatus.textContent = text || "";
    var problem = text === COPY_SPACES_FAILED || text === COPY_SPACES_CREATE_FAILED
      || text === COPY_SPACE_SAVE_FAILED || text === COPY_SPACE_CHANGED;
    if (problem) spacesStatus.setAttribute("data-tone", "problem");
    else spacesStatus.removeAttribute("data-tone");
  }

  function clearSpacesList() {
    while (spacesList.firstChild) spacesList.removeChild(spacesList.firstChild);
  }

  function addSpacesItem(name, detail, onChoose) {
    var item = document.createElement("li");
    var button = document.createElement("button");
    button.type = "button";
    button.className = "spaces-item";
    var label = document.createElement("span");
    label.className = "spaces-item-name";
    label.textContent = name;
    button.appendChild(label);
    if (detail) {
      var extra = document.createElement("span");
      extra.className = "spaces-item-detail";
      extra.textContent = detail;
      button.appendChild(extra);
    }
    button.addEventListener("click", onChoose);
    item.appendChild(button);
    spacesList.appendChild(item);
  }

  function countLabel(count, one, many) {
    if (typeof count !== "number" || count < 0 || Math.floor(count) !== count) return "";
    return count + " " + (count === 1 ? one : many);
  }

  function showSpacesList() {
    var view = spacesView = { kind: "spaces" };
    spacesTitle.textContent = "Spaces";
    spacesBack.hidden = true;
    clearManage();
    spacesNewLabel.textContent = "New Space name";
    spacesNewName.placeholder = "New Space name";
    spacesNewName.maxLength = SPACE_NAME_MAX;
    clearSpacesList();
    setSpacesStatus("Loading…");
    return spacesRequest("GET", "/api/spaces?limit=" + SPACE_PAGE_MAX).then(function (r) {
      if (spacesView !== view) return;
      if (r.status !== 200 || !r.data || !Array.isArray(r.data.spaces)) {
        setSpacesStatus(COPY_SPACES_FAILED);
        return;
      }
      var shown = 0;
      r.data.spaces.forEach(function (space) {
        if (!space || !SPACE_ID_RE.test(space.id) || !validName(space.name, SPACE_NAME_MAX)) return;
        shown += 1;
        addSpacesItem(space.name, countLabel(space.conversation_count, "conversation", "conversations"),
          function () { showSpace({ id: space.id, name: space.name }); });
      });
      setSpacesStatus(shown ? "" : COPY_SPACES_EMPTY);
    }, function () {
      if (spacesView === view) setSpacesStatus(COPY_SPACES_FAILED);
    });
  }

  function showSpace(space) {
    var view = spacesView = { kind: "space", space: space };
    spacesTitle.textContent = space.name;
    spacesBack.hidden = false;
    spacesNewLabel.textContent = "New conversation title (optional)";
    spacesNewName.placeholder = "Conversation title (optional)";   // Fits a 360 px phone in full.
    spacesNewName.maxLength = SPACE_TITLE_MAX;
    spacesNewName.value = "";
    clearSpacesList();
    manageBar(view);
    setSpacesStatus("Loading…");
    return spacesRequest("GET", spaceUrl(space.id, "/conversations?limit=" + SPACE_PAGE_MAX)).then(function (r) {
      if (spacesView !== view) return;
      if (r.status !== 200 || !r.data || !Array.isArray(r.data.conversations)) {
        setSpacesStatus(COPY_SPACES_FAILED);
        return;
      }
      view.conversations = [];
      r.data.conversations.forEach(function (c) {
        if (!c || !SPACE_ID_RE.test(c.id)) return;
        view.conversations.push({ id: c.id, title: validName(c.title, SPACE_TITLE_MAX) ? c.title : null,
                                  message_count: c.message_count });
      });
      renderConversations(view);
    }, function () {
      if (spacesView === view) setSpacesStatus(COPY_SPACES_FAILED);
    });
  }

  /* Managing the open Space (in the dialog's Space view): rename it, set its instructions, delete it.
     Each change carries the version the page last read; a change made elsewhere meanwhile is refused
     by the server (version_conflict) and the page says so instead of overwriting it. */
  var SPACE_INSTRUCTIONS_MAX = 8000;
  var COPY_SPACE_SAVE_FAILED = "Kalillac couldn't save that. Please try again.";
  var COPY_SPACE_CHANGED = "This Space was changed somewhere else. Open it again to see the latest.";
  var spacesManage = null;

  function spacesButton(label, className, onClick) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    button.addEventListener("click", onClick);
    return button;
  }

  function clearManage() {
    if (!spacesManage) return;
    while (spacesManage.firstChild) spacesManage.removeChild(spacesManage.firstChild);
    spacesManage.hidden = true;
  }

  function manageBar(view) {
    if (!spacesManage) {
      spacesManage = document.createElement("div");
      spacesManage.className = "spaces-manage";
      spacesManage.id = "spaces-manage";
      spacesList.parentNode.insertBefore(spacesManage, spacesList);
    }
    clearManage();
    spacesManage.hidden = false;
    spacesManage.appendChild(spacesButton("Rename", "spaces-manage-btn", function () { editSpace(view, "name"); }));
    spacesManage.appendChild(spacesButton("Instructions", "spaces-manage-btn",
      function () { editSpace(view, "instructions"); }));
    spacesManage.appendChild(spacesButton("Delete", "spaces-manage-btn spaces-danger",
      function () { confirmDeleteSpace(view); }));
    spacesManage.appendChild(spacesButton(view.editing ? "Done" : "Edit conversations", "spaces-manage-btn",
      function () {
        view.editing = !view.editing;
        manageBar(view);
        renderConversations(view);
      }));
  }

  /* The open Space's conversations: opened by a click, or, while editing, renamed or deleted in place.
     A rename reads the conversation first and sends the version it read, like a Space rename. */
  function renderConversations(view) {
    if (spacesView !== view || !view.conversations) return;
    clearSpacesList();
    view.conversations.forEach(function (c) {
      if (!view.editing) {
        addSpacesItem(c.title || "Untitled conversation", countLabel(c.message_count, "message", "messages"),
          function () { openSpaceConversation(view.space, { id: c.id, title: c.title }, true); });
        return;
      }
      var item = document.createElement("li");
      item.className = "spaces-edit-row";
      var name = document.createElement("span");
      name.className = "spaces-item-name";
      name.textContent = c.title || "Untitled conversation";
      item.appendChild(name);
      item.appendChild(spacesButton("Rename", "spaces-manage-btn", function () { renameConversation(view, c, item); }));
      item.appendChild(spacesButton("Delete", "spaces-manage-btn spaces-danger",
        function () { confirmDeleteConversation(view, c, item); }));
      spacesList.appendChild(item);
    });
    setSpacesStatus(view.conversations.length ? "" : COPY_CONVERSATIONS_EMPTY);
  }

  function conversationUrl(view, c) {
    return spaceUrl(view.space.id, "/conversations/" + c.id);
  }

  function renameConversation(view, c, item) {
    while (item.firstChild) item.removeChild(item.firstChild);
    var form = document.createElement("form");
    form.className = "spaces-edit spaces-edit-inline";
    var label = document.createElement("label");
    label.className = "sr-only";
    label.htmlFor = "spaces-rename-conversation";
    label.textContent = "Conversation title";
    var box = document.createElement("input");
    box.id = "spaces-rename-conversation";
    box.className = "spaces-edit-field";
    box.maxLength = SPACE_TITLE_MAX;
    box.value = c.title || "";
    var actions = document.createElement("div");
    actions.className = "spaces-edit-actions";
    var save = document.createElement("button");
    save.type = "submit";
    save.className = "spaces-manage-btn spaces-primary";
    save.textContent = "Save";
    actions.appendChild(save);
    actions.appendChild(spacesButton("Cancel", "spaces-manage-btn", function () { renderConversations(view); }));
    form.appendChild(label);
    form.appendChild(box);
    form.appendChild(actions);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var title = box.value.trim();
      if (!title) { box.focus(); return; }
      save.disabled = true;
      setSpacesStatus("Saving…");
      spacesRequest("GET", conversationUrl(view, c) + "?limit=1").then(function (r) {
        var current = r.data && r.data.conversation;
        if (spacesView !== view) return null;
        if (r.status !== 200 || !current || current.id !== c.id || typeof current.version !== "number") {
          throw new Error("read");
        }
        return spacesRequest("PATCH", conversationUrl(view, c), { title: title, expected_version: current.version });
      }).then(function (r) {
        if (!r || spacesView !== view) return;
        var saved = r.data && r.data.conversation;
        if (r.status === 200 && saved && saved.id === c.id && validName(saved.title, SPACE_TITLE_MAX)) {
          c.title = saved.title;
          if (SPACE && SPACE.conversation.id === c.id) {
            SPACE.conversation.title = saved.title;
            spaceBarTitle.textContent = " · " + saved.title;
          }
          renderConversations(view);
          setSpacesStatus("Renamed.");
          return;
        }
        save.disabled = false;
        setSpacesStatus(r.data && r.data.error === "version_conflict" ? COPY_SPACE_CHANGED : COPY_SPACE_SAVE_FAILED);
      }).then(null, function () {
        if (spacesView !== view) return;
        save.disabled = false;
        setSpacesStatus(COPY_SPACE_SAVE_FAILED);
      });
    });
    item.appendChild(form);
    box.focus();
  }

  function confirmDeleteConversation(view, c, item) {
    while (item.firstChild) item.removeChild(item.firstChild);
    var note = document.createElement("span");
    note.className = "spaces-confirm";
    note.textContent = "Delete “" + (c.title || "Untitled conversation") + "”? This can’t be undone.";
    item.appendChild(note);
    var remove = spacesButton("Delete conversation", "spaces-manage-btn spaces-danger", function () {
      if (inFlight) return;
      remove.disabled = true;
      setSpacesStatus("Deleting…");
      spacesRequest("DELETE", conversationUrl(view, c)).then(function (r) {
        if (spacesView !== view) return;
        if (r.status !== 204) {
          remove.disabled = false;
          setSpacesStatus(COPY_SPACE_SAVE_FAILED);
          return;
        }
        if (SPACE && SPACE.conversation.id === c.id) leaveSpace();
        view.conversations = view.conversations.filter(function (other) { return other !== c; });
        renderConversations(view);
        setSpacesStatus("Conversation deleted.");
      }, function () {
        if (spacesView !== view) return;
        remove.disabled = false;
        setSpacesStatus(COPY_SPACE_SAVE_FAILED);
      });
    });
    item.appendChild(remove);
    item.appendChild(spacesButton("Cancel", "spaces-manage-btn", function () { renderConversations(view); }));
  }

  // The Space as the server has it now (name, instructions, version), or null.
  function readSpace(view) {
    return spacesRequest("GET", spaceUrl(view.space.id)).then(function (r) {
      var s = r.data && r.data.space;
      if (spacesView !== view || r.status !== 200 || !s || s.id !== view.space.id || !validName(s.name, SPACE_NAME_MAX)
          || typeof s.instructions !== "string" || typeof s.version !== "number") return null;
      return s;
    }, function () { return null; });
  }

  function editSpace(view, field) {
    setSpacesStatus("Loading…");
    readSpace(view).then(function (s) {
      if (spacesView !== view) return;
      if (!s) { setSpacesStatus(COPY_SPACES_FAILED); return; }
      setSpacesStatus("");
      clearManage();
      spacesManage.hidden = false;
      var form = document.createElement("form");
      form.className = "spaces-edit";
      var label = document.createElement("label");
      label.className = "spaces-edit-label";
      label.htmlFor = "spaces-edit-field";
      label.textContent = field === "name" ? "Space name" : "Instructions for every conversation in this Space";
      var box = document.createElement(field === "name" ? "input" : "textarea");
      box.id = "spaces-edit-field";
      box.className = "spaces-edit-field";
      box.maxLength = field === "name" ? SPACE_NAME_MAX : SPACE_INSTRUCTIONS_MAX;
      box.value = field === "name" ? s.name : s.instructions;
      if (field !== "name") box.rows = 6;
      var actions = document.createElement("div");
      actions.className = "spaces-edit-actions";
      var save = document.createElement("button");
      save.type = "submit";
      save.className = "spaces-manage-btn spaces-primary";
      save.textContent = "Save";
      actions.appendChild(save);
      actions.appendChild(spacesButton("Cancel", "spaces-manage-btn", function () { manageBar(view); }));
      form.appendChild(label);
      form.appendChild(box);
      form.appendChild(actions);
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var value = field === "name" ? box.value.trim() : box.value;
        if (field === "name" && !value) { box.focus(); return; }
        save.disabled = true;
        setSpacesStatus("Saving…");
        var request = field === "name"
          ? spacesRequest("PATCH", spaceUrl(view.space.id), { name: value, expected_version: s.version })
          : spacesRequest("PUT", spaceUrl(view.space.id, "/instructions"),
                          { instructions: value, expected_version: s.version });
        request.then(function (r) {
          if (spacesView !== view) return;
          var saved = r.data && r.data.space;
          if (r.status === 200 && saved && saved.id === view.space.id && validName(saved.name, SPACE_NAME_MAX)) {
            view.space.name = saved.name;
            spacesTitle.textContent = saved.name;
            if (SPACE && SPACE.space.id === saved.id) {
              SPACE.space.name = saved.name;
              spaceBarName.textContent = saved.name;
            }
            manageBar(view);
            setSpacesStatus(field === "name" ? "Renamed." : "Instructions saved.");
            return;
          }
          save.disabled = false;
          setSpacesStatus(r.data && r.data.error === "version_conflict" ? COPY_SPACE_CHANGED : COPY_SPACE_SAVE_FAILED);
        }, function () {
          if (spacesView !== view) return;
          save.disabled = false;
          setSpacesStatus(COPY_SPACE_SAVE_FAILED);
        });
      });
      spacesManage.appendChild(form);
      box.focus();
    });
  }

  function confirmDeleteSpace(view) {
    clearManage();
    spacesManage.hidden = false;
    var note = document.createElement("p");
    note.className = "spaces-confirm";
    note.textContent = "Delete this Space and all its conversations? This can’t be undone.";
    spacesManage.appendChild(note);
    var remove = spacesButton("Delete Space", "spaces-manage-btn spaces-danger", function () {
      if (inFlight) return;
      remove.disabled = true;
      setSpacesStatus("Deleting…");
      spacesRequest("DELETE", spaceUrl(view.space.id)).then(function (r) {
        if (spacesView !== view) return;
        if (r.status !== 204) {
          remove.disabled = false;
          setSpacesStatus(COPY_SPACE_SAVE_FAILED);
          return;
        }
        if (SPACE && SPACE.space.id === view.space.id) leaveSpace();
        showSpacesList().then(function () { setSpacesStatus("Space deleted."); });
      }, function () {
        if (spacesView !== view) return;
        remove.disabled = false;
        setSpacesStatus(COPY_SPACE_SAVE_FAILED);
      });
    });
    spacesManage.appendChild(remove);
    spacesManage.appendChild(spacesButton("Cancel", "spaces-manage-btn", function () { manageBar(view); }));
  }

  function createInSpaces(event) {
    event.preventDefault();
    var view = spacesView;
    if (!view) return;
    var value = spacesNewName.value.trim();
    if (view.kind === "spaces") {
      if (!value) { spacesNewName.focus(); return; }
      setSpacesStatus("Creating…");
      spacesRequest("POST", "/api/spaces", { name: value, instructions: "" }).then(function (r) {
        if (spacesView !== view) return;
        var space = r.data && r.data.space;
        if (r.status !== 201 || !space || !SPACE_ID_RE.test(space.id) || !validName(space.name, SPACE_NAME_MAX)) {
          setSpacesStatus(COPY_SPACES_CREATE_FAILED);
          return;
        }
        spacesNewName.value = "";
        showSpace({ id: space.id, name: space.name });
      }, function () {
        if (spacesView === view) setSpacesStatus(COPY_SPACES_CREATE_FAILED);
      });
      return;
    }
    setSpacesStatus("Creating…");
    spacesRequest("POST", spaceUrl(view.space.id, "/conversations"), value ? { title: value } : {}).then(function (r) {
      if (spacesView !== view) return;
      var c = r.data && r.data.conversation;
      if (r.status !== 201 || !c || !SPACE_ID_RE.test(c.id)) {
        setSpacesStatus(COPY_SPACES_CREATE_FAILED);
        return;
      }
      spacesNewName.value = "";
      // A new conversation has no messages: it opens without another request.
      openSpaceConversation(view.space, { id: c.id, title: validName(c.title, SPACE_TITLE_MAX) ? c.title : null }, false);
    }, function () {
      if (spacesView === view) setSpacesStatus(COPY_SPACES_CREATE_FAILED);
    });
  }

  function openSpaceConversation(space, conversation, load) {
    if (inFlight) return;
    var view = spacesView;
    if (!load) {
      enterSpace(space, conversation, []);
      return;
    }
    setSpacesStatus("Opening…");
    spacesRequest("GET", spaceUrl(space.id, "/conversations/" + conversation.id + "?limit=" + SPACE_MESSAGES_MAX))
      .then(function (r) {
        if (spacesView !== view || inFlight) return;
        if (r.status !== 200 || !r.data || !Array.isArray(r.data.messages)) {
          setSpacesStatus(COPY_SPACES_FAILED);
          return;
        }
        enterSpace(space, conversation, r.data.messages, earlierCursor(r.data.next_before_seq));
      }, function () {
        if (spacesView === view) setSpacesStatus(COPY_SPACES_FAILED);
      });
  }

  function closeSpacesDialog() {
    spacesView = null;
    if (spacesDialog.open) {
      if (typeof spacesDialog.close === "function") spacesDialog.close();
      else spacesDialog.removeAttribute("open");
    }
  }

  function openSpacesDialog() {
    if (inFlight) return;
    if (typeof spacesDialog.showModal === "function") spacesDialog.showModal();
    else spacesDialog.setAttribute("open", "");
    showSpacesList();
  }

  // A server cursor for the page before the shown messages: a positive whole number, else none.
  function earlierCursor(value) {
    return typeof value === "number" && value > 0 && Math.floor(value) === value ? value : null;
  }

  // Saved messages, oldest first, as completed turns (Copy only).
  function renderSaved(messages) {
    var pending = null;
    for (var i = 0; i < messages.length; i++) {
      var m = messages[i];
      if (!m || typeof m.content !== "string") continue;
      if (m.role === "user") {
        pending = { text: m.content, row: addUserMessage(m.content) };
      } else if (m.role === "assistant" && pending) {
        var shell = addAssistantShell();
        renderAssistantText(shell, m.content, null);
        addActions(shell, TURNS.length);
        TURNS.push({ user: pending.text, assistant: m.content, userRow: pending.row, row: shell.row });
        pending = null;
      }
    }
  }

  // "Show earlier messages" above the thread while the server has older ones.
  function showEarlierControl() {
    if (!SPACE || SPACE.earlier === null) return;
    var row = document.createElement("div");
    row.className = "spaces-earlier-row";
    row.appendChild(spacesButton("Show earlier messages", "spaces-earlier", loadEarlier));
    thread.insertBefore(row, thread.firstChild);
  }

  /* The page before the oldest shown message, placed above it. The thread is drawn again from what was
     saved (the earlier page, then every turn on screen), so each turn keeps its own Copy. */
  function loadEarlier(event) {
    var open = SPACE;
    if (!open || open.earlier === null || inFlight) return;
    var button = event && event.currentTarget;
    if (button) button.disabled = true;
    spacesRequest("GET", spaceUrl(open.space.id, "/conversations/" + open.conversation.id +
                  "?limit=" + SPACE_MESSAGES_MAX + "&before_seq=" + open.earlier)).then(function (r) {
      if (SPACE !== open || inFlight) return;
      if (r.status !== 200 || !r.data || !Array.isArray(r.data.messages)) {
        if (button) {
          button.disabled = false;
          button.textContent = "Couldn’t load earlier messages. Try again";
        }
        return;
      }
      var shown = [];
      TURNS.forEach(function (t) {
        shown.push({ role: "user", content: t.user });
        shown.push({ role: "assistant", content: t.assistant });
      });
      discardFailedExchange();
      TURNS.length = 0;
      while (thread.firstChild) thread.removeChild(thread.firstChild);
      open.earlier = earlierCursor(r.data.next_before_seq);
      stickToBottom = false;              // Reading back: drawing the thread must not jump to its end,
      renderSaved(r.data.messages.concat(shown));
      showEarlierControl();
      window.requestAnimationFrame(function () {   // and the earlier messages are what is shown.
        if (SPACE === open) conversation.scrollTop = 0;
      });
    }, function () {
      if (button && SPACE === open) button.disabled = false;
    });
  }

  function enterSpace(space, conversation, messages, earlier) {
    var restore = SPACE ? SPACE.restore : {
      picker: modePicker ? modePicker.hidden : null,
      attach: attachBtn ? attachBtn.hidden : null
    };
    SPACE = null;
    startNewConversation();          // The temporary conversation leaves the screen (as New conversation).
    SPACE = { space: space, conversation: conversation, restore: restore, earlier: earlier || null };
    spaceBarName.textContent = space.name;
    spaceBarTitle.textContent = conversation.title ? " · " + conversation.title : "";
    spaceBar.hidden = false;
    if (sessionStatus) sessionStatus.hidden = true;
    if (eraseBtn) eraseBtn.hidden = true;
    if (attachBtn) attachBtn.hidden = true;
    clearAttached();
    showAttachStatus("");

    renderSaved(messages);
    showEarlierControl();
    closeSpacesDialog();
    stickToBottom = true;
    scrollToBottom(true);
    input.focus();
  }

  function leaveSpace() {
    if (!SPACE || inFlight) return;
    var restore = SPACE.restore;
    SPACE = null;
    spaceBar.hidden = true;
    spaceBarName.textContent = "";
    spaceBarTitle.textContent = "";
    if (sessionStatus) sessionStatus.hidden = false;
    if (eraseBtn) eraseBtn.hidden = false;
    if (modePicker && restore.picker !== null) modePicker.hidden = restore.picker;
    if (attachBtn && restore.attach !== null) attachBtn.hidden = restore.attach;
    startNewConversation();
  }

  function requestSpaceReply(exchange) {
    var open = SPACE;
    exchange.epoch = conversationEpoch;
    // The idempotency key: the same for every retry of this message, new for the next one.
    if (!exchange.requestId) exchange.requestId = newRequestId();
    // The reasoning mode chosen when the message was first sent; a retry keeps it with its key.
    if (exchange.mode === undefined) exchange.mode = reasoningMode;
    var spaceBody = { message: exchange.text, client_request_id: exchange.requestId };
    if (exchange.mode) spaceBody.reasoning_mode = exchange.mode;
    setBusy(true);
    clearActions(exchange.shell);
    setThinking(exchange.shell);
    controller = new AbortController();
    var stale = function () { return exchange.epoch !== conversationEpoch || SPACE !== open; };

    return fetch(spaceUrl(open.space.id, "/conversations/" + open.conversation.id + "/messages"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(spaceBody),
      signal: controller.signal
    })
      .then(function (res) {
        if (stale()) return;
        return res.text().then(function (body) {
          if (stale()) return;
          var data = parseJson(body);
          if (!res.ok) {
            failExchange(exchange, outcomeForResponse(res.status, data));
            return;
          }
          var saved = data && data.assistant_message;
          var reply = saved && typeof saved.content === "string" ? saved.content : "";
          if ((data && typeof data.error === "string") || !reply.trim()) {
            failExchange(exchange, OUTCOME_INCOMPLETE);
            return;
          }
          completeExchange(exchange, reply, null, null, []);
        }, function (err) {
          if (!stale()) failExchange(exchange, isAbort(err) ? OUTCOME_STOPPED : OUTCOME_INCOMPLETE);
        });
      }, function (err) {
        // Stopping does not stop the server from saving; a retry with the same key shows what it saved.
        if (!stale()) failExchange(exchange, isAbort(err) ? OUTCOME_STOPPED : OUTCOME_UNREACHABLE);
      })
      .then(null, function () {
        if (!stale()) failExchange(exchange, OUTCOME_INCOMPLETE);
      })
      .then(function () {
        if (exchange.epoch !== conversationEpoch) return;
        controller = null;
        setBusy(false);
        autoGrow();
      });
  }

  if (SPACES_OFFERED && spacesTemplate && spacesTemplate.content && spacesTemplate.content.firstElementChild &&
      spacesDialog && spaceBar) {
    spacesNew.addEventListener("submit", createInSpaces);
    spacesBack.addEventListener("click", function () { showSpacesList(); });
    spacesClose.addEventListener("click", closeSpacesDialog);
    spacesDialog.addEventListener("close", function () {
      // The close event arrives after the dialog closed; when it was opened again meanwhile, this late
      // event belongs to the earlier opening and must not end the new one.
      if (spacesDialog.open) return;
      spacesView = null;
      if (spacesBtn) spacesBtn.focus();
    });
    spaceLeave.addEventListener("click", leaveSpace);
    // The one question asked on load: may this account use Spaces? Anything but a list is "no".
    spacesRequest("GET", "/api/spaces?limit=1").then(function (r) {
      if (r.status !== 200 || !r.data || !Array.isArray(r.data.spaces)) return;
      spacesBtn = spacesTemplate.content.firstElementChild.cloneNode(true);
      spacesTemplate.parentNode.insertBefore(spacesBtn, spacesTemplate);
      spacesBtn.addEventListener("click", openSpacesDialog);
    }, function () { /* not offered */ });
  }

  // ---- End & erase ---------------------------------------------------------------
  /* Ends the conversation on this device and asks the server to remove its
     temporary session memory. Confirmation is a native modal dialog with fixed
     copy (index.html). On confirmation: abort the browser request, clear every
     turn, the failed exchange, the rendered thread, the session id and the
     composer draft, then -- only when a session id existed -- send a separate
     request to /api/chat/session/end for it (and ask again for any earlier id
     whose removal is still unconfirmed). Cookies (the daily-allowance cookie or
     an account cookie) are never touched. The receipt below states what was
     and was not erased; it never shows server text. */
  var eraseBtn = document.getElementById("erase-btn");
  var eraseDialog = document.getElementById("erase-dialog");
  var eraseConfirm = document.getElementById("erase-dialog-confirm");
  var eraseCancel = document.getElementById("erase-dialog-cancel");
  var eraseReceipt = document.getElementById("erase-receipt");
  var eraseReceiptText = document.getElementById("erase-receipt-text");
  var eraseReceiptNote = document.getElementById("erase-receipt-note");
  var eraseRetry = document.getElementById("erase-retry");

  var RECEIPT_REMOVED = "Ended on this device. Kalillac removed this session’s temporary conversation memory. Any active response was asked to stop.";
  var RECEIPT_NOT_FOUND = "Ended on this device. Kalillac found no temporary session memory to remove.";
  var RECEIPT_LIMITS = "Information already sent to AI or search providers, required allowance records, and operational or security logs are not erased by this action. Provider processing may not stop immediately.";
  var RECEIPT_FAILED = "Cleared from this device, but Kalillac could not confirm removal of its temporary server memory.";
  var RECEIPT_LOCAL = "Cleared from this device. No Kalillac session had been confirmed for this conversation.";
  var ERASE_CONFIRM_FALLBACK = "End and erase this conversation? This clears it from this device " +
                               "and asks Kalillac to remove its temporary memory of this session. " +
                               "This can’t be undone.";

  function hasErasable() {
    return inFlight || TURNS.length > 0 || !!failedExchange || !!SESSION_ID ||
           input.value !== "" || !!thread.querySelector(".msg");
  }

  // End & erase would empty the message box, so it needs the send authority too.
  function syncEraseEnabled() {
    if (eraseBtn) eraseBtn.disabled = !hasAuthority() || !hasErasable();
  }

  /* The notice leaves on its own about RECEIPT_SHOW_MS after it appears,
     counted in RECEIPT_TICK_MS steps that do not count while the pointer is
     over it or focus is inside it, so reading or reaching Dismiss never loses
     it. An unconfirmed removal (with Retry erase) stays until dismissed or
     retried. Dismiss closes it at any time. It leaves with a short fade, or at
     once under reduced motion, and takes no space afterwards. */
  var eraseReceiptDismiss = document.getElementById("erase-receipt-dismiss");
  var RECEIPT_SHOW_MS = 12000;
  var RECEIPT_TICK_MS = 250;
  var RECEIPT_FADE_MS = 160;
  var receiptLeft = 0;
  var receiptTicker = null;
  var receiptHeld = false;
  var receiptFadeTimer = null;

  function stopReceiptTicker() {
    if (receiptTicker !== null) {
      window.clearInterval(receiptTicker);
      receiptTicker = null;
    }
  }

  function receiptHolding() {
    return receiptHeld || eraseReceipt.contains(document.activeElement);
  }

  function hideEraseReceipt() {
    if (!eraseReceipt || eraseReceipt.hidden) return;
    stopReceiptTicker();
    var hadFocus = eraseReceipt.contains(document.activeElement);
    var reduce = window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    function finish() {
      receiptFadeTimer = null;
      eraseReceipt.hidden = true;
      eraseReceipt.classList.remove("is-leaving");
      if (hadFocus) input.focus({ preventScroll: true });
    }

    if (reduce) {
      finish();
      return;
    }
    eraseReceipt.classList.add("is-leaving");
    receiptFadeTimer = window.setTimeout(finish, RECEIPT_FADE_MS);
  }

  function showEraseReceipt(text, withLimits, retry) {
    if (!eraseReceipt) return;
    stopReceiptTicker();
    if (receiptFadeTimer !== null) {
      window.clearTimeout(receiptFadeTimer);
      receiptFadeTimer = null;
    }
    eraseReceipt.classList.remove("is-leaving");
    eraseReceiptText.textContent = text;
    eraseReceiptNote.textContent = withLimits ? RECEIPT_LIMITS : "";
    eraseReceiptNote.hidden = !withLimits;
    eraseRetry.hidden = !retry;
    eraseRetry.disabled = false;
    eraseReceipt.classList.toggle("is-failed", !!retry);
    eraseReceipt.hidden = false;

    if (retry) return;                // stays until dismissed or retried
    receiptLeft = RECEIPT_SHOW_MS;
    receiptTicker = window.setInterval(function () {
      if (receiptHolding()) return;
      receiptLeft -= RECEIPT_TICK_MS;
      if (receiptLeft <= 0) hideEraseReceipt();
    }, RECEIPT_TICK_MS);
  }

  if (eraseReceipt) {
    eraseReceipt.addEventListener("mouseenter", function () { receiptHeld = true; });
    eraseReceipt.addEventListener("mouseleave", function () { receiptHeld = false; });
  }
  if (eraseReceiptDismiss) eraseReceiptDismiss.addEventListener("click", hideEraseReceipt);

  function eraseIdsIn(state) {
    return Object.keys(eraseAttempts).filter(function (id) {
      return eraseAttempts[id].state === state;
    });
  }

  /* A confirmed or local-only receipt makes way for the next conversation; an
     unconfirmed removal stays on screen with Retry erase until it succeeds. */
  function dismissSettledEraseReceipt() {
    if (eraseReceipt && !eraseReceipt.hidden && Object.keys(eraseAttempts).length === 0) {
      stopReceiptTicker();
      eraseReceipt.hidden = true;
    }
  }

  /* One separate POST per id with its own AbortController: never the chat
     request's controller or signal. keepalive lets it outlive a page close
     where the browser allows; it does not guarantee delivery. After
     ERASE_TIMEOUT_MS the request is aborted and counts as unconfirmed. The
     timer is cleared however the request settles. Resolves "removed",
     "not_found" or null (unconfirmed). */
  var ERASE_TIMEOUT_MS = 10000;

  function postEraseRequest(sessionId) {
    return new Promise(function (resolve) {
      var eraseAbort = new AbortController();
      var settled = false;
      var timer = setTimeout(function () {
        eraseAbort.abort();
        finish(null);
      }, ERASE_TIMEOUT_MS);

      function finish(state) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(state);
      }

      fetch("/api/chat/session/end", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
        keepalive: true,
        signal: eraseAbort.signal
      })
        .then(function (res) {
          return res.text().then(function (body) {
            var data = parseJson(body);
            if (!res.ok || !data || data.ended !== true) return null;
            var state = data.kalillac_session_state;
            return state === "removed" || state === "not_found" ? state : null;
          });
        })
        .then(finish, function () { finish(null); });
    });
  }

  /* The receipt reflects the whole erase round once nothing in it is pending:
     any unconfirmed id gets the failure receipt and Retry erase; otherwise
     "removed" if any id was removed, else "not found". A fully confirmed
     round is then forgotten. */
  function renderEraseRound() {
    if (eraseIdsIn("pending").length) return;

    if (eraseIdsIn("failed").length) {
      showEraseReceipt(RECEIPT_FAILED, false, true);
      return;
    }

    var removed = eraseIdsIn("removed").length > 0;
    eraseAttempts = {};
    showEraseReceipt(removed ? RECEIPT_REMOVED : RECEIPT_NOT_FOUND, true, false);
  }

  /* Starts a new attempt for each id. Only an id's latest attempt may record
     its result, so an older completion can never overwrite a newer one, and
     ids completing out of order each keep their own result. */
  function requestErase(ids) {
    if (eraseRetry) eraseRetry.disabled = true;

    ids.forEach(function (id) {
      var attempt = ++eraseAttemptSeq;
      eraseAttempts[id] = { attempt: attempt, state: "pending" };

      postEraseRequest(id).then(function (state) {
        var entry = eraseAttempts[id];
        if (!entry || entry.attempt !== attempt) return;   // superseded or forgotten
        entry.state = state || "failed";
        renderEraseRound();
      });
    });
  }

  function endAndErase() {
    if (!hasAuthority()) return;
    // The opaque id is the only thing kept, and only for the request below.
    var sessionId = SESSION_ID;
    var requestWasActive = inFlight;

    conversationEpoch++;
    if (controller) {
      try { controller.abort(); } catch (e) { /* already settled */ }
    }
    controller = null;

    TURNS.length = 0;
    SESSION_ID = null;
    failedExchange = null;
    while (thread.firstChild) {
      thread.removeChild(thread.firstChild);
    }
    showEmptyState();

    input.value = "";
    draftRevision++;
    stickToBottom = true;
    setBusy(false);
    autoGrow();
    syncSendEnabled();
    input.focus({ preventScroll: true });

    if (sessionId) {
      // Earlier unconfirmed ids are asked again; pending ones keep their
      // in-flight attempt.
      var ids = eraseIdsIn("failed");
      if (ids.indexOf(sessionId) === -1) ids.push(sessionId);
      requestErase(ids);
    } else if (Object.keys(eraseAttempts).length === 0) {
      // No server session was ever confirmed to this page: nothing to ask the
      // server to remove. An earlier erase still in progress or unconfirmed
      // keeps its own receipt.
      showEraseReceipt(RECEIPT_LOCAL, requestWasActive, false);
    }
  }

  function eraseDialogOpen() {
    return !!(eraseDialog && eraseDialog.open);
  }

  function closeEraseDialog() {
    if (eraseDialogOpen()) eraseDialog.close();
    if (eraseBtn && !eraseBtn.disabled) eraseBtn.focus({ preventScroll: true });
  }

  function openEraseDialog() {
    if (!hasAuthority() || !hasErasable()) return;

    if (eraseDialog && eraseCancel && typeof eraseDialog.showModal === "function") {
      try {
        // The opener holds focus, so closing the dialog returns focus to it.
        if (eraseBtn) eraseBtn.focus({ preventScroll: true });
        if (!eraseDialog.open) eraseDialog.showModal();
        eraseCancel.focus();   // the safe choice has focus first
        return;
      } catch (e) {
        // Fall through to the browser's own confirmation.
      }
    }

    if (window.confirm(ERASE_CONFIRM_FALLBACK)) endAndErase();
  }

  if (eraseBtn) eraseBtn.addEventListener("click", openEraseDialog);

  if (eraseDialog && eraseConfirm && eraseCancel) {
    // Only a confirmation made while the dialog is open erases, so a repeated
    // click or key press can erase -- and ask the server -- only once.
    eraseConfirm.addEventListener("click", function () {
      if (!eraseDialogOpen()) return;
      eraseDialog.close();
      endAndErase();
    });
    eraseCancel.addEventListener("click", closeEraseDialog);
    // Tab and Shift+Tab cycle between the dialog's two buttons.
    eraseDialog.addEventListener("keydown", function (event) {
      if (event.key !== "Tab") return;
      var target = event.shiftKey ? eraseCancel : eraseConfirm;
      var other = target === eraseCancel ? eraseConfirm : eraseCancel;
      if (document.activeElement === target || !eraseDialog.contains(document.activeElement)) {
        event.preventDefault();
        other.focus();
      }
    });
    eraseDialog.addEventListener("cancel", function (event) {
      event.preventDefault();
      closeEraseDialog();
    });
    // The panel fills the dialog box, so only a backdrop click targets the
    // dialog element itself.
    eraseDialog.addEventListener("click", function (event) {
      if (event.target === eraseDialog) closeEraseDialog();
    });
  }

  if (eraseRetry) {
    eraseRetry.addEventListener("click", function () {
      var failed = eraseIdsIn("failed");
      if (failed.length) requestErase(failed);
    });
  }

  syncEraseEnabled();

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

  // Empty-state example prompts. Clicking one puts exactly its own text in
  // the composer; it never sends. The visitor can edit before sending.
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
      if (!hasAuthority()) return;     // it would replace a message kept for copying
      stopHintGuide();

      input.value = this.getAttribute("data-prompt") || "";
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

  /* ---- the homepage's one-time handoff -------------------------------------
     The homepage stores one envelope in this tab's sessionStorage when its
     Send is pressed: { v: 1, draft, nonce, createdAt } -- the trimmed draft, a
     random 32-hex-digit nonce and the creation time. It is read once, when
     this page loads, and removed BEFORE anything else is checked or sent --
     except a well-formed envelope HANDOFF_MAX_AGE_MS old or older, which this
     page can never take: that is left exactly where it is, so the homepage
     can still give the message back. A direct visit finds nothing and
     touches nothing. The key, shape and limits match site/src/App.tsx.

     Only a well-formed envelope whose nonce this tab has not taken before is
     used, and it is sent only when all of this holds:
     - it is less than HANDOFF_SEND_MS old. The homepage gives the draft back
       after 10 seconds without this page, and that is later than this
       deadline, so once the homepage has given a draft back this page can no
       longer send it -- whether or not the homepage could withdraw it, and
       without depending on the homepage's timer;
     - it was removed, and its nonce was recorded in the ledger and read back.
     A well-formed envelope that fails any of these (but is not stale, from the
     future or already taken) is placed in the message box, unsent, with a
     notice. Nothing else is shown or sent -- including the homepage's
     withdrawn marker, { v: 1, withdrawn: nonce }, which it leaves in place of
     an envelope it took back but could not remove.

     Taking a message makes it this page's for good, so it is taken only in a
     way the homepage can see: its nonce is recorded in the ledger (which the
     homepage reads first, on every showing), or its envelope is removed and
     confirmed gone. When neither is verified, this page does not own it:
     - removal certainly failed: the message is left to the homepage,
       untouched, and only a notice says so -- the homepage still sees an
       envelope nobody took, and gives it back;
     - removal could not be confirmed: the envelope may still be there, or
       may be gone. It is put back, so the homepage can still give the
       message back; and the draft is kept here too, but read-only and never
       sendable: the send authority (see hasAuthority) is withheld for the
       rest of this page's life, and a notice says ownership could not be
       confirmed.

     The ledger, { v: 1, entries: [{ n, x }] } under HANDOFF_LEDGER_KEY, holds
     the nonce of every envelope taken in the last HANDOFF_RETAIN_MS -- never a
     message. An entry is dropped only once its envelope could not be accepted
     even after a clock correction of HANDOFF_SKEW_MS; a full list of live
     entries is never trimmed. A ledger that cannot be read, is malformed or
     full, or cannot be written and read back sends nothing. */
  var HANDOFF_KEY = "kalillac.handoff";
  var HANDOFF_LEDGER_KEY = "kalillac.handoff.ledger";
  var HANDOFF_SEND_MS = 8000;
  var HANDOFF_MAX_AGE_MS = 60000;
  var HANDOFF_SKEW_MS = 5000;
  var HANDOFF_RETAIN_MS = HANDOFF_MAX_AGE_MS + HANDOFF_SKEW_MS;
  var HANDOFF_LEDGER_MAX = 32;
  var HANDOFF_MAX_CHARS = 4000;
  var COPY_HANDOFF_LATE = "The chat took too long to open, so your message wasn’t sent. " +
    "It’s in the message box. Press Send to send it.";
  var COPY_HANDOFF_UNCHECKED = "The chat didn’t send your message automatically, because this " +
    "browser’s storage didn’t allow its check against sending a message twice. It’s in the " +
    "message box. Press Send to send it.";
  var COPY_HANDOFF_LEFT = "Your message wasn’t sent, and it is still on the homepage: this browser’s " +
    "storage didn’t let the chat take it. Go back to the homepage to send it from there.";
  // These two are true whatever became of the message before: unsent, sending, answered or failed.
  var COPY_HANDOFF_UNVERIFIED = "Kalillac couldn’t verify this message’s status. To prevent it from " +
    "being sent twice, it remains read-only. You can select and copy it.";
  var COPY_HANDOFF_LEFT_UNVERIFIED = "Kalillac couldn’t verify this message’s status, so this chat " +
    "didn’t take it. If the homepage still has it, you can send it from there.";
  var handoffNotice = document.getElementById("handoff-notice");
  var handoffNoticeText = document.getElementById("handoff-notice-text");

  function isNonce(value) {
    return typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
  }

  // The ledger's entries, or null when it cannot be read or is not exactly the expected shape.
  function readLedger(store) {
    var raw, ledger;
    try { raw = store.getItem(HANDOFF_LEDGER_KEY); } catch (e) { return null; }
    if (raw === null) return [];
    try { ledger = JSON.parse(raw); } catch (e) { return null; }
    if (!ledger || typeof ledger !== "object" || Array.isArray(ledger) ||
        Object.keys(ledger).sort().join() !== "entries,v" || ledger.v !== 1 ||
        !Array.isArray(ledger.entries) || ledger.entries.length > HANDOFF_LEDGER_MAX) return null;
    var seen = {};
    for (var i = 0; i < ledger.entries.length; i++) {
      var entry = ledger.entries[i];
      if (!entry || typeof entry !== "object" || Array.isArray(entry) ||
          Object.keys(entry).sort().join() !== "n,x" || !isNonce(entry.n) ||
          !Number.isSafeInteger(entry.x) || entry.x <= 0 || seen[entry.n]) return null;
      seen[entry.n] = true;
    }
    return ledger.entries;
  }

  // Records a nonce and reads it back; false when the list is full of live entries or storage fails.
  function recordNonce(store, entries, nonce, expires, now) {
    var kept = entries.filter(function (entry) { return entry.x > now; });
    if (kept.length >= HANDOFF_LEDGER_MAX) return false;
    kept.push({ n: nonce, x: expires });
    var serialized = JSON.stringify({ v: 1, entries: kept });
    try {
      store.setItem(HANDOFF_LEDGER_KEY, serialized);
      return store.getItem(HANDOFF_LEDGER_KEY) === serialized;
    } catch (e) {
      return false;
    }
  }

  /* A well-formed envelope -- exactly its four fields, this version, a nonce,
     a whole-millisecond time and a non-empty draft within the limit -- or null. */
  function parseEnvelope(raw) {
    var envelope;
    try { envelope = JSON.parse(raw); } catch (e) { return null; }
    if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) return null;
    if (Object.keys(envelope).sort().join() !== "createdAt,draft,nonce,v") return null;
    if (envelope.v !== 1 || typeof envelope.draft !== "string" || !isNonce(envelope.nonce) ||
        !Number.isSafeInteger(envelope.createdAt)) return null;
    if (!envelope.draft.trim() || Array.from(envelope.draft).length > HANDOFF_MAX_CHARS) return null;
    return envelope;
  }

  /* A message this page could not show it took, and whose removal it could
     not confirm: the envelope is put back exactly as it was -- harmless if it
     was never removed -- so the homepage can still give the message back once
     this page is gone; and this page keeps a read-only copy it never sends.
     It may still be the homepage's. */
  function unverifiedHandoff(store, raw, envelope) {
    try { store.setItem(HANDOFF_KEY, raw); } catch (e) { /* then this page's copy is the only one */ }
    return { draft: envelope.draft, notice: COPY_HANDOFF_UNVERIFIED, authority: "withheld" };
  }

  /* { draft, notice[, authority] }: notice is null only when the draft may be
     sent now; draft is null when the message was left to the homepage; and
     authority is "withheld" when this page could not show it took it. */
  function takeHandoff() {
    var store, raw;
    try {
      store = window.sessionStorage;
      raw = store.getItem(HANDOFF_KEY);
    } catch (e) {
      return null;
    }
    if (raw === null) return null;
    var envelope = parseEnvelope(raw);
    var now = Date.now();
    // Too old for this page ever to take, so not this page's to remove: it is left exactly
    // where it is, and the homepage can still give the message back.
    if (envelope !== null && now - envelope.createdAt >= HANDOFF_MAX_AGE_MS) return null;
    var removal = "kept";   // "removed", "kept" (certainly still there) or "unchecked"
    try {
      store.removeItem(HANDOFF_KEY);
      try {
        removal = store.getItem(HANDOFF_KEY) === null ? "removed" : "kept";
      } catch (e) {
        removal = "unchecked";
      }
    } catch (e) {
      removal = "kept";
    }

    if (envelope === null) return null;
    var age = now - envelope.createdAt;
    if (age < -HANDOFF_SKEW_MS || age >= HANDOFF_MAX_AGE_MS) return null;

    var left = { draft: null, notice: COPY_HANDOFF_LEFT };
    var entries = readLedger(store);
    if (entries === null) {
      if (removal === "unchecked") return unverifiedHandoff(store, raw, envelope);
      // Without the ledger, an earlier send of this very envelope cannot be ruled out: say nothing of its past.
      if (removal === "kept") return { draft: null, notice: COPY_HANDOFF_LEFT_UNVERIFIED };
      return { draft: envelope.draft, notice: COPY_HANDOFF_UNCHECKED };
    }
    for (var i = 0; i < entries.length; i++) {
      if (entries[i].n === envelope.nonce) return null;   // taken before: never sent or shown again
    }
    var recorded = recordNonce(store, entries, envelope.nonce, envelope.createdAt + HANDOFF_RETAIN_MS, now);
    if (!recorded && removal === "kept") return left;     // nothing would show the homepage it was taken
    if (!recorded && removal === "unchecked") return unverifiedHandoff(store, raw, envelope);
    if (age >= HANDOFF_SEND_MS) return { draft: envelope.draft, notice: COPY_HANDOFF_LATE };
    if (removal !== "removed" || !recorded) return { draft: envelope.draft, notice: COPY_HANDOFF_UNCHECKED };
    return { draft: envelope.draft, notice: null };
  }

  function hideHandoffNotice() {
    if (handoffNotice) handoffNotice.hidden = true;
  }

  /* For the rest of this page's life: the message box is read-only, and
     Send, End & erase and the example prompts are disabled -- the visible
     side of the refusals in requestReply() and every way into it. */
  function withholdAuthority() {
    form.setAttribute("data-authority", "withheld");
    input.readOnly = true;
    for (var i = 0; i < hints.length; i++) hints[i].disabled = true;
    syncSendEnabled();
  }

  if (emptyState) conversation.classList.add("is-empty");
  placeStarter();
  syncSendEnabled();

  var handoff = takeHandoff();
  if (handoff !== null && handoff.draft !== null) {
    // The homepage's draft becomes this conversation's first message, once.
    // It is shown only as text (addUserMessage) and goes through the same
    // send() as any typed message -- or, when it may not be sent now, waits
    // in the message box with a notice -- read-only, when this page could
    // not show it took it.
    stopHintGuide();
    if (handoff.authority === "withheld") withholdAuthority();
    input.value = handoff.draft;
    draftRevision++;
    autoGrow();
    syncSendEnabled();
  }
  if (handoff !== null) {
    if (handoff.notice === null) {
      send();
    } else if (handoffNotice && handoffNoticeText) {
      handoffNotice.hidden = false;
      handoffNoticeText.textContent = handoff.notice;
    }
  }
  input.focus();
})();
