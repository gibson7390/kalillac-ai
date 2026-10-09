import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, Code2, FileText, Menu, Search, Sparkles, X } from 'lucide-react';
import { Link, Route, Switch, Router as WouterRouter, useLocation } from 'wouter';
import { ErrorBoundary } from '@/components/error-boundary';
import NotFound from '@/pages/not-found';

const brandLogoUrl = '/assets/kalillac-ai-logo.png';

// The canonical chat workspace (../frontend), served at /app/ and embedded
// here as a same-origin iframe.
const CHAT_PATH = '/app/';

// A real load of /app/ is the canonical chat in ../frontend; this site must
// never frame itself if it is ever served there.
const IS_FRAMED = (() => {
  try { return window.self !== window.top; } catch { return true; }
})();

function SiteHeader() {
  const [open, setOpen] = useState(false);
  const close = () => setOpen(false);
  return (
    <header className="site-header">
      <div className="container-wide site-header-inner">
        <Link href="/" className="brand" data-testid="link-home-brand" onClick={close} aria-label="Kalillac AI home">
          <img className="brand-logo" src={brandLogoUrl} alt="Kalillac AI" />
        </Link>
        <nav className="site-nav" aria-label="Site navigation">
          <a href="/#product" className="nav-link" data-testid="link-product-nav">Product</a>
          <a href="/#privacy" className="nav-link" data-testid="link-privacy-nav">Privacy</a>
          <a href="/#how-it-works" className="nav-link" data-testid="link-how-nav">How it works</a>
          <button type="button" className="mobile-nav-toggle" onClick={() => setOpen(!open)} aria-expanded={open} aria-controls="mobile-site-menu" aria-label={open ? 'Close navigation menu' : 'Open navigation menu'} data-testid="button-mobile-menu">
            {open ? <X size={19} /> : <Menu size={19} />}
          </button>
        </nav>
      </div>
      {open && (
        <nav id="mobile-site-menu" className="mobile-site-menu" aria-label="Mobile navigation">
          <a href="/#product" onClick={close} data-testid="link-mobile-product">Product</a>
          <a href="/#privacy" onClick={close} data-testid="link-mobile-privacy">Privacy</a>
          <a href="/#how-it-works" onClick={close} data-testid="link-mobile-how">How it works</a>
        </nav>
      )}
    </header>
  );
}

function SiteFooter() {
  return (
    <footer className="site-footer">
      <div className="container-wide footer-inner">
        <div><Link href="/" className="brand" data-testid="link-footer-home"><img className="brand-logo" src={brandLogoUrl} alt="Kalillac AI" /></Link><p>AI for the questions and work in front of you.</p></div>
        <div className="footer-links"><Link href="/privacy" data-testid="link-footer-privacy">Privacy</Link><Link href="/terms" data-testid="link-footer-terms">Terms</Link><a href="https://www.linkedin.com/in/kalillacai" target="_blank" rel="noopener noreferrer" data-testid="link-footer-linkedin">LinkedIn</a></div>
      </div>
      <div className="container-wide footer-bottom"><span>© 2026 Kalillac AI · Adults 18+</span><span>Kalillac AI can make mistakes. Verify important information.</span></div>
    </footer>
  );
}

function OrbitalAssistant() {
  return (
    <div className="orbital-wrap" aria-hidden="true">
      <div className="orbital-glow" />
      <div className="orbital">
        <span className="orbit-line" />
        <span className="orbit-line two" />
        <span className="orbit-line three" />
        <span className="orbit-line four" />
        <span className="orbit-track orbit-track-one" />
        <span className="orbit-track orbit-track-two" />
        <span className="orbit-dot blue" />
        <span className="orbit-dot mint" />
        <span className="orbit-dot white" />
        <span className="orbit-dot tiny" />
        <span className="orbital-center" />
      </div>
    </div>
  );
}

// Space kept between the sticky site header and the chat workspace when the
// CTA brings it into view.
const COMPOSER_GAP_BELOW_HEADER = 16;

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/* The homepage chat workspace: the canonical /app/ client in a same-origin
   iframe, at one stable height from the first render onward. It is a full
   conversation workspace before, during and after a conversation; nothing
   resizes, expands or navigates when a prompt is sent, and the homepage and
   the chat exchange no messages. */
function useChatWorkspace() {
  const slotRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLIFrameElement>(null);

  /* Move the workspace to just below the sticky site header when it is not
     already fully visible there -- smoothly, or instantly when reduced
     motion is requested. Never moves the page when it is already in view. */
  const bringIntoView = useCallback(() => {
    const slot = slotRef.current;
    if (!slot) return;

    const header = document.querySelector('.site-header');
    const headerBottom = header ? header.getBoundingClientRect().bottom : 0;
    const card = slot.getBoundingClientRect();
    const fullyVisible = card.top >= headerBottom && card.bottom <= window.innerHeight;

    if (!fullyVisible) {
      window.scrollTo({
        top: Math.max(0, window.scrollY + card.top - headerBottom - COMPOSER_GAP_BELOW_HEADER),
        // 'auto' (not the newer 'instant', which older browsers reject with
        // a TypeError) is immediate here: under reduced motion the site CSS
        // forces scroll-behavior: auto.
        behavior: prefersReducedMotion() ? 'auto' : 'smooth',
      });
    }
  }, []);

  /* "Start a private session" lands here: the workspace is brought into
     view, then the real message input inside the same-origin chat is focused
     with preventScroll, so focusing never causes a second, browser-generated
     jump. */
  const focusChat = useCallback(() => {
    bringIntoView();

    const frame = frameRef.current;
    let input: HTMLElement | null = null;
    try { input = frame?.contentDocument?.getElementById('composer-input') ?? null; } catch { input = null; }

    if (input) input.focus({ preventScroll: true });
    else frame?.focus({ preventScroll: true });
  }, [bringIntoView]);

  return { slotRef, frameRef, focusChat };
}

function EmbeddedChat({ workspace }: { workspace: ReturnType<typeof useChatWorkspace> }) {
  const { slotRef, frameRef } = workspace;
  return (
    <div id="chat" className="embedded-chat-slot" ref={slotRef}>
      <div className="embedded-chat" data-testid="embedded-chat">
        <iframe ref={frameRef} src={CHAT_PATH} title="Chat with Kalillac AI" loading="eager" />
      </div>
    </div>
  );
}

/* ---------------- Privacy and provider-flow diagram ----------------
   One inline SVG per layout: a horizontal drawing for wide screens and a
   vertical one for narrow screens (CSS shows exactly one, so assistive
   technology meets exactly one). Both have the same four primary stages.
   The text inside each SVG is part of the image; its accessible title and
   description point to the semantic list that follows it. */

function FlowMarkers({ suffix }: { suffix: string }) {
  return (
    <defs>
      <marker id={`flow-arrow-${suffix}`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0 0 L10 5 L0 10 z" fill="#5b93ff" />
      </marker>
      <marker id={`flow-arrow-teal-${suffix}`} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0 0 L10 5 L0 10 z" fill="#35c9a7" />
      </marker>
    </defs>
  );
}

function BrowserIcon({ x, y }: { x: number; y: number }) {
  return (
    <g className="flow-icon" transform={`translate(${x} ${y})`}>
      <circle r="36" className="flow-node" />
      <rect x="-19" y="-14" width="38" height="28" rx="5" className="flow-glyph" />
      <line x1="-19" y1="-6" x2="19" y2="-6" className="flow-glyph" />
      <circle cx="-13" cy="-10" r="1.6" className="flow-glyph-fill" />
      <circle cx="-8" cy="-10" r="1.6" className="flow-glyph-fill" />
      <line x1="-11" y1="3" x2="9" y2="3" className="flow-glyph-soft" />
      <line x1="-11" y1="8" x2="3" y2="8" className="flow-glyph-soft" />
    </g>
  );
}

function MemoryIcon({ x, y }: { x: number; y: number }) {
  return (
    <g className="flow-icon" transform={`translate(${x} ${y})`}>
      <circle r="36" className="flow-node" />
      <rect x="-16" y="-15" width="32" height="8" rx="3" className="flow-glyph" />
      <rect x="-16" y="-4" width="32" height="8" rx="3" className="flow-glyph" />
      <rect x="-16" y="7" width="32" height="8" rx="3" className="flow-glyph flow-glyph-temporary" />
      <circle cx="21" cy="-17" r="6" className="flow-glyph-teal" />
    </g>
  );
}

function ModelIcon({ x, y }: { x: number; y: number }) {
  return (
    <g className="flow-icon" transform={`translate(${x} ${y})`}>
      <circle r="36" className="flow-node" />
      <path d="M0 -17 L15 -8.5 L15 8.5 L0 17 L-15 8.5 L-15 -8.5 Z" className="flow-glyph" />
      <circle r="4.5" className="flow-glyph-fill" />
      <line x1="0" y1="-4.5" x2="0" y2="-17" className="flow-glyph-soft" />
      <line x1="3.9" y1="2.3" x2="15" y2="8.5" className="flow-glyph-soft" />
      <line x1="-3.9" y1="2.3" x2="-15" y2="8.5" className="flow-glyph-soft" />
    </g>
  );
}

function SearchIcon({ x, y }: { x: number; y: number }) {
  return (
    <g className="flow-icon" transform={`translate(${x} ${y})`}>
      <circle r="24" className="flow-node flow-node-teal" />
      <circle r="11" className="flow-glyph-teal-line" />
      <ellipse rx="5" ry="11" className="flow-glyph-teal-line" />
      <line x1="-11" y1="0" x2="11" y2="0" className="flow-glyph-teal-line" />
    </g>
  );
}

function ExitIcon({ x, y }: { x: number; y: number }) {
  return (
    <g className="flow-icon" transform={`translate(${x} ${y})`}>
      <circle r="36" className="flow-node flow-node-quiet" />
      <path d="M-4 -16 H-16 V16 H-4" className="flow-glyph" />
      <line x1="-6" y1="0" x2="16" y2="0" className="flow-glyph" />
      <path d="M9 -7 L16 0 L9 7" className="flow-glyph" />
    </g>
  );
}

function ReturnIcon({ x, y }: { x: number; y: number }) {
  return (
    <g className="flow-icon" transform={`translate(${x} ${y})`}>
      <circle r="18" className="flow-node" />
      <path d="M8 -6 H-4 A6 6 0 0 0 -4 6 H6" className="flow-glyph" />
      <path d="M-8 -10 L-12 -6 L-8 -2" className="flow-glyph" transform="translate(16 0) scale(-1 1)" />
    </g>
  );
}

function FlowDiagramWide() {
  return (
    <svg className="flow-svg flow-svg-wide" viewBox="0 0 1120 420" role="img" aria-labelledby="flow-title-wide" aria-describedby="flow-desc-wide" data-testid="flow-svg-wide">
      <title id="flow-title-wide">How a Kalillac conversation moves</title>
      <desc id="flow-desc-wide">A diagram with four stages: you ask, temporary session, answer or search, and you move on. The stages are described in the list that follows.</desc>
      <FlowMarkers suffix="wide" />

      {/* Answer path: you -> temporary session -> answer. */}
      <line x1="188" y1="130" x2="390" y2="130" className="flow-path" markerEnd="url(#flow-arrow-wide)" />
      <line x1="468" y1="130" x2="670" y2="130" className="flow-path" markerEnd="url(#flow-arrow-wide)" />
      {/* The result returns through Kalillac to you. */}
      <path d="M710 92 C710 48 600 36 430 36 C260 36 150 48 150 90" className="flow-path flow-path-return" markerEnd="url(#flow-arrow-wide)" />
      <text x="430" y="26" textAnchor="middle" className="flow-label">Answers return to you through Kalillac</text>
      {/* Web search: a separate, conditional branch. */}
      <path d="M744 146 C800 158 834 196 834 250 L834 296" className="flow-path-search" markerStart="url(#flow-arrow-teal-wide)" markerEnd="url(#flow-arrow-teal-wide)" />
      {/* Later, the visitor moves on. */}
      <line x1="748" y1="130" x2="950" y2="130" className="flow-path-later" />
      <text x="850" y="118" textAnchor="middle" className="flow-label flow-label-quiet">Later</text>

      <g className="flow-stage" data-stage="1">
        <BrowserIcon x={150} y={130} />
        <text x="150" y="198" textAnchor="middle" className="flow-stage-title">YOU ASK</text>
        <text x="150" y="226" textAnchor="middle" className="flow-stage-body">No account or</text>
        <text x="150" y="248" textAnchor="middle" className="flow-stage-body">profile required.</text>
      </g>
      <g className="flow-stage" data-stage="2">
        <MemoryIcon x={430} y={130} />
        <text x="430" y="198" textAnchor="middle" className="flow-stage-title">TEMPORARY SESSION</text>
        <text x="430" y="226" textAnchor="middle" className="flow-stage-body">Kalillac keeps the current</text>
        <text x="430" y="248" textAnchor="middle" className="flow-stage-body">conversation context</text>
        <text x="430" y="270" textAnchor="middle" className="flow-stage-body">in server memory.</text>
      </g>
      <g className="flow-stage" data-stage="3">
        <ModelIcon x={710} y={130} />
        <text x="710" y="198" textAnchor="middle" className="flow-stage-title">ANSWER OR SEARCH</text>
        <text x="710" y="226" textAnchor="middle" className="flow-stage-body">OpenAI produces</text>
        <text x="710" y="248" textAnchor="middle" className="flow-stage-body">the answer.</text>
        <SearchIcon x={834} y={326} />
        <text x="872" y="320" className="flow-stage-body flow-search-text">Tavily searches when current</text>
        <text x="872" y="342" className="flow-stage-body flow-search-text">web information is needed.</text>
      </g>
      <g className="flow-stage" data-stage="4">
        <ExitIcon x={990} y={130} />
        <text x="990" y="198" textAnchor="middle" className="flow-stage-title">YOU MOVE ON</text>
        <text x="990" y="226" textAnchor="middle" className="flow-stage-body">Refreshing or leaving ends</text>
        <text x="990" y="248" textAnchor="middle" className="flow-stage-body">this browser’s access to</text>
        <text x="990" y="270" textAnchor="middle" className="flow-stage-body">the conversation.</text>
      </g>

      <g className="flow-legend" transform="translate(40 392)">
        <line x1="0" y1="0" x2="34" y2="0" className="flow-path" />
        <text x="44" y="5" className="flow-legend-text">Answer path</text>
        <line x1="170" y1="0" x2="204" y2="0" className="flow-path-search" />
        <text x="214" y="5" className="flow-legend-text">Web search, only when needed</text>
      </g>
    </svg>
  );
}

function FlowDiagramNarrow() {
  return (
    <svg className="flow-svg flow-svg-narrow" viewBox="0 0 360 920" role="img" aria-labelledby="flow-title-narrow" aria-describedby="flow-desc-narrow" data-testid="flow-svg-narrow">
      <title id="flow-title-narrow">How a Kalillac conversation moves</title>
      <desc id="flow-desc-narrow">A diagram with four stages: you ask, temporary session, answer or search, and you move on. The stages are described in the list that follows.</desc>
      <FlowMarkers suffix="narrow" />

      <line x1="48" y1="98" x2="48" y2="176" className="flow-path" markerEnd="url(#flow-arrow-narrow)" />
      <line x1="48" y1="258" x2="48" y2="366" className="flow-path" markerEnd="url(#flow-arrow-narrow)" />
      <path d="M70 434 C96 462 108 486 108 520" className="flow-path-search" markerStart="url(#flow-arrow-teal-narrow)" markerEnd="url(#flow-arrow-teal-narrow)" />
      <line x1="48" y1="448" x2="48" y2="620" className="flow-path flow-path-return" markerEnd="url(#flow-arrow-narrow)" />
      <line x1="48" y1="676" x2="48" y2="738" className="flow-path-later" />

      <g className="flow-stage" data-stage="1">
        <BrowserIcon x={48} y={60} />
        <text x="100" y="50" className="flow-stage-title">YOU ASK</text>
        <text x="100" y="74" className="flow-stage-body">No account or profile</text>
        <text x="100" y="94" className="flow-stage-body">required.</text>
      </g>
      <g className="flow-stage" data-stage="2">
        <MemoryIcon x={48} y={220} />
        <text x="100" y="206" className="flow-stage-title">TEMPORARY SESSION</text>
        <text x="100" y="230" className="flow-stage-body">Kalillac keeps the current</text>
        <text x="100" y="250" className="flow-stage-body">conversation context in</text>
        <text x="100" y="270" className="flow-stage-body">server memory.</text>
      </g>
      <g className="flow-stage" data-stage="3">
        <ModelIcon x={48} y={410} />
        <text x="100" y="398" className="flow-stage-title">ANSWER OR SEARCH</text>
        <text x="100" y="422" className="flow-stage-body">OpenAI produces the answer.</text>
        <SearchIcon x={128} y={548} />
        <text x="164" y="534" className="flow-stage-body flow-search-text">Tavily searches when</text>
        <text x="164" y="554" className="flow-stage-body flow-search-text">current web information</text>
        <text x="164" y="574" className="flow-stage-body flow-search-text">is needed.</text>
      </g>
      <ReturnIcon x={48} y={648} />
      <text x="80" y="644" className="flow-label">Answers return to you</text>
      <text x="80" y="664" className="flow-label">through Kalillac.</text>
      <g className="flow-stage" data-stage="4">
        <ExitIcon x={48} y={776} />
        <text x="100" y="764" className="flow-stage-title">YOU MOVE ON</text>
        <text x="100" y="788" className="flow-stage-body">Refreshing or leaving ends</text>
        <text x="100" y="808" className="flow-stage-body">this browser’s access to</text>
        <text x="100" y="828" className="flow-stage-body">the conversation.</text>
      </g>

      <g className="flow-legend" transform="translate(20 876)">
        <line x1="0" y1="0" x2="30" y2="0" className="flow-path" />
        <text x="40" y="5" className="flow-legend-text">Answer path</text>
        <line x1="0" y1="26" x2="30" y2="26" className="flow-path-search" />
        <text x="40" y="31" className="flow-legend-text">Web search, only when needed</text>
      </g>
    </svg>
  );
}

function PrivacyFlow() {
  return (
    <section className="flow-section" id="how-it-works" aria-labelledby="flow-heading" data-testid="section-flow">
      <div className="container-wide">
        <div className="flow-panel">
          <div className="flow-intro">
            <span className="eyebrow eyebrow-on-dark">PRIVATE BY STRUCTURE</span>
            <h2 id="flow-heading">Your conversation has boundaries.</h2>
            <p>No account. No permanent chat history. Clear provider roles.</p>
          </div>
          <figure className="flow-figure">
            <FlowDiagramWide />
            <FlowDiagramNarrow />
            <figcaption className="sr-only">
              <ol data-testid="flow-summary">
                <li>You ask. No account or profile is required, and Kalillac does not provide permanent chat history.</li>
                <li>Temporary session. Kalillac keeps the current conversation context in server memory for the active temporary session.</li>
                <li>Answer or search. OpenAI produces the answer. Tavily searches when current web information is needed. The answer returns to you through Kalillac.</li>
                <li>You move on. Refreshing or leaving ends this browser’s access to the conversation. That is not immediate deletion from the server.</li>
              </ol>
            </figcaption>
          </figure>
          <p className="flow-note" data-testid="flow-memory-note">
            <strong>Temporary session data can remain in server memory until capacity limits or a restart clear it.</strong>{' '}
            Ending this browser’s access is not the same as deleting it from the server.
          </p>
        </div>
      </div>
    </section>
  );
}

/* ---------------- See Kalillac at work ----------------
   Four illustrative product fragments (not live output and not
   testimonials), each composed differently. */

function DemoThink() {
  return (
    <article className="demo demo-think" data-testid="demo-think" aria-labelledby="demo-think-title">
      <header className="demo-head"><span className="demo-kicker">01 · Think</span><h3 id="demo-think-title">Turn a tangled question into a clear way to decide.</h3></header>
      <div className="demo-surface">
        <p className="demo-ask">I’ve been offered a team-lead role at a smaller company. More pay, less stability. I keep going back and forth.</p>
        <div className="think-answer">
          <div className="think-row think-decision"><span className="think-label">The real decision</span><p>Whether you want to trade stability for faster growth over the next two years — not only whether this offer pays more.</p></div>
          <div className="think-columns">
            <div className="think-row"><span className="think-label">What you know</span><ul><li>The salary increase is confirmed.</li><li>You would lead a team of five.</li></ul></div>
            <div className="think-row"><span className="think-label">What you’re assuming</span><ul><li>The company will still be funded in two years.</li><li>Leading people will suit you.</li></ul></div>
          </div>
          <div className="think-row"><span className="think-label">Tradeoffs</span>
            <table className="think-table">
              <thead><tr><th scope="col"><span className="sr-only">Factor</span></th><th scope="col">New role</th><th scope="col">Stay</th></tr></thead>
              <tbody>
                <tr><th scope="row">Growth</th><td>Faster, broader</td><td>Steady, deeper</td></tr>
                <tr><th scope="row">Stability</th><td>Lower</td><td>Higher</td></tr>
                <tr><th scope="row">New skill</th><td>Managing people</td><td>Technical depth</td></tr>
              </tbody>
            </table>
          </div>
          <div className="think-row think-next"><span className="think-label">A useful next step</span><p>Ask the founder how long the current funding lasts. If the answer changes your assumptions, the decision gets easier. The choice stays yours.</p></div>
        </div>
      </div>
    </article>
  );
}

function DemoWrite() {
  return (
    <article className="demo demo-write" data-testid="demo-write" aria-labelledby="demo-write-title">
      <header className="demo-head"><span className="demo-kicker">02 · Write</span><h3 id="demo-write-title">Clearer writing that still sounds like you.</h3></header>
      <div className="write-pair">
        <div className="write-col write-before"><span className="write-tag">Your draft</span><p>I am writing to let you know that the project is going to be delayed because there were some issues with the vendor that we didn’t really know about until last week, so the new date is probably going to be the end of next month.</p></div>
        <div className="write-col write-after"><span className="write-tag">Revised</span><p>The project is delayed. Last week we learned about vendor issues we hadn’t known of, and we now expect to finish at the end of next month.</p></div>
      </div>
      <ul className="write-notes">
        <li><strong>Leads with the news</strong> instead of the preamble.</li>
        <li><strong>Keeps your uncertainty</strong> — “we now expect,” not a promise.</li>
        <li><strong>Same plain, direct voice</strong>, with the filler removed.</li>
      </ul>
    </article>
  );
}

function DemoCode() {
  return (
    <article className="demo demo-code" data-testid="demo-code" aria-labelledby="demo-code-title">
      <header className="demo-head"><span className="demo-kicker">03 · Build and debug</span><h3 id="demo-code-title">See why it breaks, then fix it.</h3></header>
      <p className="demo-ask">Why does my list keep growing between calls?</p>
      <div className="code-block code-broken" aria-label="Original code">
        <span className="code-tag">Before</span>
        <pre><code>{`def add_tag(tag, tags=[]):
    tags.append(tag)
    return tags

add_tag("draft")   # ['draft']
add_tag("final")   # ['draft', 'final']`}</code></pre>
      </div>
      <p className="code-diagnosis"><strong>Why it fails:</strong> Python creates the default list once, when the function is defined, so every call that omits <code>tags</code> appends to the same list.</p>
      <div className="code-block code-fixed" aria-label="Repaired code">
        <span className="code-tag">After</span>
        <pre><code>{`def add_tag(tag, tags=None):
    if tags is None:
        tags = []
    tags.append(tag)
    return tags`}</code></pre>
      </div>
      <p className="code-why"><strong>Why the fix works:</strong> <code>None</code> is a safe default, and a new list is created on each call unless you pass one in.</p>
    </article>
  );
}

function DemoSearch() {
  return (
    <article className="demo demo-search" data-testid="demo-search" aria-labelledby="demo-search-title">
      <header className="demo-head"><span className="demo-kicker">04 · Search the current web</span><h3 id="demo-search-title">Current answers with their sources in view.</h3></header>
      <div className="demo-surface search-surface">
        <p className="demo-ask">Is the open-source library we depend on still actively maintained?</p>
        <div className="search-answer">
          <span className="search-badge"><Search size={14} aria-hidden="true" /> Searched the current web</span>
          <p>It appears to be actively maintained: the project’s release notes and repository activity show ongoing releases, and maintainers are responding to new issues. Check the changelog for breaking changes before you upgrade.</p>
        </div>
        <div className="search-sources" aria-label="Sources">
          <span className="search-sources-label">Sources</span>
          <ul>
            <li><span className="source-chip">Project repository</span></li>
            <li><span className="source-chip">Release notes</span></li>
            <li><span className="source-chip">Issue tracker</span></li>
          </ul>
        </div>
        <p className="search-caption">An example of how sourced answers appear. Real answers link the pages that were actually searched.</p>
      </div>
    </article>
  );
}

function Demonstrations() {
  return (
    <section className="demos-section" id="product" aria-labelledby="demos-heading" data-testid="section-demos">
      <div className="container-wide">
        <div className="demos-intro">
          <span className="eyebrow">THE WORK</span>
          <h2 id="demos-heading">See Kalillac at work.</h2>
          <p>Illustrative examples of the kinds of help you can ask for.</p>
        </div>
        <div className="demos-grid">
          <DemoThink />
          <DemoWrite />
          <DemoCode />
          <DemoSearch />
        </div>
      </div>
    </section>
  );
}

function SessionExplanation() {
  return (
    <section className="explain-section" id="privacy" aria-labelledby="explain-heading" data-testid="section-explain">
      <div className="container-wide">
        <h2 id="explain-heading" className="sr-only">Your session and providers in plain language</h2>
        <div className="explain-grid">
          <div className="explain-block" data-testid="explain-session">
            <h3>Your temporary session</h3>
            <p>Kalillac doesn’t require an account and doesn’t offer permanent chat history. Your conversation lasts for the active temporary session. Refreshing or leaving the page ends your browser’s access to it. Temporary session data can remain in server memory until capacity limits or a restart clear it.</p>
          </div>
          <div className="explain-block" data-testid="explain-providers">
            <h3>Who processes what</h3>
            <p>OpenAI produces the answer. Tavily searches when a search is needed. Both process what Kalillac sends them under their own policies.</p>
          </div>
        </div>
        <Link href="/privacy" className="text-link explain-link" data-testid="link-full-privacy">Read the full Privacy page <ArrowRight size={16} aria-hidden="true" /></Link>
      </div>
    </section>
  );
}

function MobileAppSection() {
  return (
    <section className="mobile-app-section" aria-labelledby="mobile-app-title" data-testid="section-mobile-app">
      <div className="container-wide mobile-app-panel">
        <div className="app-copy">
          <span className="section-label">ON THE GO <span className="label-rule"/></span>
          <h2 id="mobile-app-title">Kalillac AI app<br/><span>in development.</span></h2>
          <p>We’re building a dedicated Kalillac mobile experience for questions, writing, coding, and ideas wherever you are.</p>
          <div className="development-badge"><span className="badge-mark"><Sparkles size={17}/></span><span><strong>Mobile app in progress</strong><small>Coming soon · No release date announced</small></span></div>
        </div>
        <div className="phone-stage" aria-label="Illustration of the Kalillac mobile app in development">
          <div className="phone-halo"/><div className="phone-halo second"/>
          <div className="phone">
            <div className="phone-island"/><div className="phone-content"><div className="phone-head"><strong>Kalillac <span>AI</span></strong><span className="phone-head-mark">✦</span></div><small>Your workspace, wherever you are.</small><div className="phone-input">Ask Kalillac anything… <span>↑</span></div><div className="phone-card"><Search size={15}/><div><strong>Research</strong><small>Explore what matters</small></div><ArrowRight size={13}/></div><div className="phone-card"><Code2 size={15}/><div><strong>Code</strong><small>Work through a problem</small></div><ArrowRight size={13}/></div><div className="phone-card"><FileText size={15}/><div><strong>Writing</strong><small>Find your next sentence</small></div><ArrowRight size={13}/></div></div>
          </div>
        </div>
      </div>
    </section>
  );
}

function HomePage() {
  const workspace = useChatWorkspace();
  const { focusChat } = workspace;

  // Section links (/#product, /#privacy, /#how-it-works, /#chat) arriving
  // from another page.
  useEffect(() => {
    const id = window.location.hash.slice(1);
    if (!id) return;
    if (id === 'chat') { focusChat(); return; }
    document.getElementById(id)?.scrollIntoView();
  }, [focusChat]);

  return (
    <div className="site-shell">
      <SiteHeader />
      <main>
        <section className="hero" aria-labelledby="hero-title" data-testid="section-hero">
          <div className="hero-signal-line" aria-hidden="true" />
          <div className="container-wide hero-stage">
            <div className="hero-copy">
              <span className="eyebrow">PRIVATE BY DESIGN</span>
              <h1 id="hero-title"><span className="h1-phrase">Think clearly.</span>{' '}<span className="h1-phrase">Write well.</span>{' '}<span className="h1-phrase">Build and debug.</span>{' '}<span className="h1-phrase h1-accent">Search the current web.</span></h1>
              <p className="hero-description">Work through difficult questions, draft and revise writing, explain and repair code, or research current information with sources—without creating an account or building a permanent chat history.</p>
              <button type="button" className="primary-link hero-action" onClick={focusChat} data-testid="button-start-session">Start a private session <ArrowRight size={16} aria-hidden="true" /></button>
              <p className="hero-under-note"><span className="trust-item"><span className="live-dot" /> Temporary sessions</span> <span className="note-divider" aria-hidden="true">·</span> <span className="trust-item">No account required</span> <span className="note-divider" aria-hidden="true">·</span> <Link href="/privacy" className="note-link trust-item">Clear provider disclosure</Link></p>
            </div>
            <div className="hero-workspace">
              <OrbitalAssistant />
              <EmbeddedChat workspace={workspace} />
            </div>
          </div>
        </section>
        <PrivacyFlow />
        <Demonstrations />
        <SessionExplanation />
        <MobileAppSection />
      </main>
      <SiteFooter />
    </div>
  );
}

type DocSectionProps = { id: string; title: string; children: ReactNode };
function DocSection({ id, title, children }: DocSectionProps) {
  return <section className="doc-section" id={id}><h2>{title}</h2>{children}</section>;
}

function DocLayout({ title, lede, children, toc, updated }: { title: string; lede: ReactNode; children: ReactNode; toc: [string, string][]; updated: string }) {
  return (
    <div className="site-shell">
      <SiteHeader />
      <main className="doc">
        <div className="doc-inner">
          <Link href="/" className="doc-home" data-testid="link-doc-home">← Home</Link>
          <h1 className="doc-title">{title}</h1>
          {lede}
          <nav className="doc-toc" aria-label="On this page"><h2>On this page</h2><ol>{toc.map(([id, label]) => <li key={id}><a href={`#${id}`}>{label}</a></li>)}</ol></nav>
          {children}
          <p className="doc-updated">{updated}</p>
        </div>
      </main>
      <SiteFooter />
    </div>
  );
}

const privacyToc: [string, string][] = [
  ['overview', 'Information Kalillac processes'], ['not-persisted', 'What Kalillac does not save'], ['session-state', 'Temporary session state'], ['isolation', 'Session isolation'], ['browser', 'Browser storage'], ['logging', 'Logging'], ['model-inference', 'Model inference: OpenAI'], ['tavily', 'Web search: Tavily'], ['infrastructure', 'Network infrastructure: Cloudflare'], ['cloudflare-security', 'Cloudflare security checks'], ['analytics', 'Analytics and performance measurement'], ['path', 'How a message moves'], ['security', 'Security'], ['age', 'Age'], ['retention', 'Retention in brief'], ['choices', 'What you can do'], ['changes', 'Changes'], ['source', 'About this page'], ['contact', 'Contact'],
];

function PrivacyPage() {
  return <DocLayout title="Privacy" toc={privacyToc} updated="Last updated: October 9, 2026" lede={<><p className="doc-lede">This page describes how information is handled when you use Kalillac AI at <span className="mono">kalillac.com</span>, including the chat application.</p><p className="doc-lede">Kalillac is an AI assistant for questions, research, writing, coding, learning, and answers that may use the current web. No account or paid subscription is currently offered. This page describes the service as it is designed now. It is not a promise that the service is anonymous, that data is never stored at any layer, or that the service will never change.</p></>}>
    <DocSection id="overview" title="Information Kalillac processes"><p>To answer a message, the application handles:</p><ul><li><strong>The message you type.</strong> Currently limited to 4,000 characters.</li><li><strong>Recent turns of the open conversation.</strong> The chat interface holds the current thread in page memory and sends recent turns with each request so the assistant has context.</li><li><strong>A session identifier.</strong> The server generates an opaque identifier and uses it as the key for a temporary session entry. The chat interface keeps that identifier in page memory. It is not an account.</li><li><strong>Facts you explicitly ask Kalillac to remember during the session.</strong> Those facts are stored as short entries in that temporary session entry.</li><li><strong>Timestamps of recent web searches in the session.</strong> Used only to enforce the current limit of five searches per ten minutes.</li></ul><p>Kalillac does not ask for your name, email address, phone number, or account details. The current service has no file upload and does not read documents from your device.</p><p>The systems that deliver the site, including the web server, the operating system, the hosting provider, and Cloudflare, also process ordinary request data such as IP address, date and time, and user agent.</p></DocSection>
    <DocSection id="not-persisted" title="What Kalillac does not save"><p>Kalillac does not save chat transcripts as persistent conversations in its own application database. In its own application, Kalillac does not:</p><ul><li>store chat history across visits</li><li>keep a user profile tied to you</li><li>keep search results in a Kalillac database after using them to write an answer</li></ul><p>Those statements describe Kalillac’s own application. They do not mean that no copy, log, or metadata can exist anywhere else. OpenAI, Tavily, Cloudflare, and the hosting provider handle what they receive under their own policies.</p></DocSection>
    <DocSection id="session-state" title="Temporary session state"><p>Kalillac’s session state is an entry in the memory of the running application process. It is not written to a database.</p><p>The entry holds two things:</p><ol><li>memory facts from that session</li><li>timestamps of recent searches in that session</li></ol><p>The application keeps at most 200 session entries at once and at most 50 memory entries per session. When a new session arrives and the store is full, the oldest entry is removed.</p><h3>How this state ends</h3><ul><li>Refreshing or closing the page removes the browser’s access to that session. The chat interface does not keep the thread in saved browser storage, and a new page starts a new, empty session that cannot read the previous entry.</li><li>That does not promise immediate deletion of the server-memory entry. Active-session context may remain in server memory until eviction or service restart.</li><li>When the application process restarts, the in-memory store is gone, because it existed only in that process.</li></ul></DocSection>
    <DocSection id="isolation" title="Session isolation"><p>Each session is stored under its own identifier. A request looks up only the matching entry. There is no shared conversation buffer and no cross-session lookup, so one visitor’s memory entries are not read into another visitor’s context.</p><p>Capacity is shared. With a limit of 200 sessions, heavy traffic can evict older sessions sooner. That affects when state disappears, not who can read it.</p></DocSection>
    <DocSection id="browser" title="Browser storage"><p>The chat interface keeps the session identifier and the completed turns of the open conversation in page memory only. It does not write them to localStorage, sessionStorage, IndexedDB, cookies, or the page address.</p><p>The homepage embeds the same chat application that is served at <span className="mono">/app/</span>. Your messages and Kalillac’s responses are not passed through the page address, browser storage, or messages between browser windows. The embedded chat application sends each chat request itself, under the same temporary session behavior described above.</p><p>Kalillac’s own frontend uses your device’s fonts and does not include advertising code, browser analytics code, remote fonts, or third-party asset dependencies. Cloudflare may separately add the <a href="#cloudflare-security">security code described below</a> at its edge.</p></DocSection>
    <DocSection id="logging" title="Logging"><p>The application writes operational output used to run and diagnose the service. Kalillac does not use that output as a conversation archive. This page does not claim that message content can never appear in server logs.</p><p>The web server, the operating system, Cloudflare, and the hosting provider can create their own request, operational, and security records, which can include data such as IP address, time, and user agent. Those records are governed by the policies of the systems and providers that create them.</p></DocSection>
    <DocSection id="model-inference" title="Model inference: OpenAI"><p>OpenAI may process requests sent for model inference. Kalillac does not run its model on the Kalillac server. OpenAI is Kalillac’s only model provider, and there is no automatic fallback to another model or provider: if OpenAI cannot return a usable answer, the request ends with an error instead of being sent elsewhere.</p><p>Some requests never go to OpenAI. For example, deterministic arithmetic and some temporary session-memory requests are handled by Kalillac’s own code.</p><p>A request sent to OpenAI can include, as applicable:</p><ul><li>your current message</li><li>relevant recent turns</li><li>relevant temporary session-memory entries</li><li>the instructions Kalillac assembles for the request</li><li>retrieved web-search text, when the answer uses a search</li></ul><p>Cloudflare Workers AI and Groq are not part of the current active model-provider path.</p><p>OpenAI handles what it receives under its own terms and policies, which Kalillac does not control: <a href="https://openai.com/policies/" target="_blank" rel="noopener noreferrer">OpenAI policies</a>.</p></DocSection>
    <DocSection id="tavily" title="Web search: Tavily"><p>When web search is used, relevant query text may be sent to Tavily. Most messages do not use web search, and Tavily does not write Kalillac’s answers.</p><p>When a search runs:</p><ul><li>Kalillac builds the query from the current request. For a follow-up question, the query can also include the earlier topic needed to understand it. The query sent to Tavily is limited to 400 characters.</li><li>Tavily returns up to four titles, links, and text snippets. When a request is about a specific public web page, Kalillac can also ask Tavily for that page’s text.</li><li>Kalillac uses those results for the response. When the model writes the answer, the results are sent to OpenAI as described above.</li><li>Kalillac does not keep those results in a Kalillac database.</li><li>Each session is limited to five searches in a ten-minute window.</li></ul><p>Tavily handles what it receives under its own terms and privacy policy, which Kalillac does not control: <a href="https://www.tavily.com/privacy" target="_blank" rel="noopener noreferrer">Tavily Privacy Policy</a>.</p></DocSection>
    <DocSection id="infrastructure" title="Network infrastructure: Cloudflare"><p>Cloudflare provides edge delivery and security for <span className="mono">kalillac.com</span>. Cloudflare is not a Kalillac model provider.</p><p>Traffic to <span className="mono">kalillac.com</span> passes through Cloudflare before reaching Kalillac’s server. Cloudflare terminates the browser-facing HTTPS connection and forwards the request to Kalillac’s origin over a separate encrypted connection. This means Cloudflare can process request content while proxying it, including chat requests, along with ordinary network information such as IP address, request headers, requested path, browser information, and time. Cloudflare handles what it receives under its own policies.</p><p>Kalillac’s server runs on a hosting provider’s infrastructure, which handles traffic and system data under its own policies.</p><p>Kalillac does not control Cloudflare’s policies: <a href="https://www.cloudflare.com/privacypolicy/" target="_blank" rel="noopener noreferrer">Cloudflare Privacy Policy</a>.</p></DocSection>
    <DocSection id="cloudflare-security" title="Cloudflare security checks"><p>Cloudflare may add security code used to detect automated or abusive traffic. When those checks run, Cloudflare may set an HttpOnly <span className="mono">cf_clearance</span> cookie showing that the browser passed a security check. Kalillac does not use this cookie to store, reconstruct, or identify prompts, responses, or conversation history.</p></DocSection>
    <DocSection id="analytics" title="Analytics and performance measurement"><p>Cloudflare Web Analytics and Real User Measurements are disabled for <span className="mono">kalillac.com</span>. Kalillac does not load Cloudflare’s browser RUM beacon or send prompts, responses, or conversation content to that beacon.</p><p>Cloudflare still produces traffic, operational, performance, and security information from requests handled at its edge. Those infrastructure records and aggregate statistics are separate from the disabled browser RUM beacon and are handled under Cloudflare’s own policies.</p></DocSection>
    <DocSection id="path" title="How a message moves"><p>The public site and the chat application are served from the same domain. A chat request passes through Cloudflare’s network to the Kalillac server, where the application decides how to handle it.</p><ul><li><strong>Handled by Kalillac.</strong> For example, deterministic arithmetic or some temporary session-memory requests, without OpenAI or Tavily.</li><li><strong>Model response.</strong> The request goes to OpenAI.</li><li><strong>Web search.</strong> Relevant query text goes to Tavily, and the results go to OpenAI when the model writes the answer. The response can include source links.</li></ul></DocSection>
    <DocSection id="security" title="Security"><p>No online service can promise that it is completely secure, or that no person or provider could ever access a request, a log, or provider-side data. Do not send passwords, API keys, credentials, payment-card numbers, or other highly sensitive secrets. Ordinary use of Kalillac does not require them.</p></DocSection>
    <DocSection id="age" title="Age"><p>Kalillac is for adults. You must be at least 18. The service is not directed to children. Kalillac does not knowingly seek information from anyone under 18. If it becomes aware that someone under 18 is using the service, it may block that access.</p><p>Use of the service is a representation that you are at least 18. Kalillac does not run a separate age-verification process.</p></DocSection>
    <DocSection id="retention" title="Retention in brief"><table><thead><tr><th>Information</th><th>Where it lives now</th><th>When it ends</th></tr></thead><tbody><tr><td>Open conversation in the chat interface</td><td>Browser page memory</td><td>The browser loses access on refresh, closing the page, or leaving it</td></tr><tr><td>Session identifier</td><td>Page memory, as the key for the server entry</td><td>Same as that page session</td></tr><tr><td>Memory facts and search timestamps</td><td>Server memory, up to 200 sessions and 50 facts</td><td>Eviction or service restart. Refreshing or closing the page does not promise immediate deletion</td></tr><tr><td>Chat transcripts</td><td>Not saved as persistent conversations in Kalillac’s application database</td><td>Not kept as chat history</td></tr><tr><td>OpenAI</td><td>OpenAI’s systems</td><td>OpenAI’s policies</td></tr><tr><td>Tavily, when a search runs</td><td>Tavily’s systems</td><td>Tavily’s policies</td></tr><tr><td>Cloudflare and hosting records</td><td>Those providers</td><td>Their policies</td></tr></tbody></table><p>There is no Kalillac account and no Kalillac conversation archive to export or delete. That is not a claim that no record exists at a provider, or that Kalillac can find and erase every infrastructure record on request.</p></DocSection>
    <DocSection id="choices" title="What you can do"><p>You choose what to type. Do not submit information you cannot allow OpenAI, Tavily, or Cloudflare to process. You can avoid questions that need web search if you do not want query text sent to Tavily. The service does not ask for an email address and has no account settings.</p></DocSection>
    <DocSection id="changes" title="Changes"><p>Kalillac is under active development. Models, providers, limits, and session settings are operational choices, not permanent commitments. Possible later features, such as optional accounts or paid plans, are not part of the current service.</p><p>If message handling changes in a way that matters, this page will be updated to describe the new behavior.</p></DocSection>
    <DocSection id="source" title="About this page"><p>Statements about Kalillac’s own application describe its source code and design. Statements about OpenAI, Tavily, and Cloudflare are limited to what Kalillac sends them; their own policies govern what they do with it. This page is an operator description. It is not an independent privacy or security audit.</p></DocSection>
    <DocSection id="contact" title="Contact"><p>Questions and corrections can go to Kalillac AI through <a href="https://www.linkedin.com/in/kalillacai" target="_blank" rel="noopener noreferrer">LinkedIn</a>.</p></DocSection>
  </DocLayout>;
}

const termsToc: [string, string][] = [
  ['service', 'The service'], ['age', 'Who may use it'], ['support', 'No account and no paid subscription'], ['ai-limitations', 'AI responses'], ['responsibilities', 'Your responsibilities'], ['user-actions', 'Your searches and later actions'], ['boundaries', 'Acceptable use'], ['privacy', 'Privacy and other companies'], ['content', 'Prompts and output'], ['intellectual-property', 'Kalillac intellectual property'], ['availability', 'Availability and access'], ['warranties', 'Disclaimers and liability'], ['law', 'Governing law'], ['changes', 'Changes to these Terms'], ['contact', 'Contact'],
];

function TermsPage() {
  return <DocLayout title="Terms of Use" toc={termsToc} updated="Last updated: October 8, 2026" lede={<><p className="doc-lede">These Terms govern use of Kalillac AI, the service at <span className="mono">kalillac.com</span>. Kalillac is an AI assistant for questions, research, writing, coding, learning, and answers that may use the current web.</p><p className="doc-lede">The service is currently free and does not require an account. It is only for adults age 18 and older. By using Kalillac, you agree to these Terms. If you do not agree, do not use the service.</p></>}>
    <DocSection id="service" title="The service"><p>Some requests are handled by Kalillac’s own code. OpenAI may process requests sent for model inference. When web search is used, relevant query text may be sent to Tavily. Cloudflare provides edge delivery and security for the site. Cloudflare Workers AI and Groq are not part of the current active model-provider path. Current data handling is described on the <Link href="/privacy">Privacy page</Link>.</p><p>The service is under active development. Features, models, providers, limits, routing, and the interface may change. No particular model, provider, search capability, limit, or screen is guaranteed to remain available.</p></DocSection>
    <DocSection id="age" title="Who may use it"><p>You must be at least <strong>18 years old</strong>. Kalillac is not directed to children or minors. If you are under 18, do not use it.</p><p>By using Kalillac, you represent that you are at least 18. Kalillac may restrict access if it becomes aware that someone under 18 is using the service. These Terms rely on your representation. They do not describe a separate age-verification process.</p></DocSection>
    <DocSection id="support" title="No account and no paid subscription"><p>No account or paid subscription is currently offered. These Terms are posted on the site because there is no registration step.</p><p>A voluntary <a href="https://buymeacoffee.com/kalillactv7" target="_blank" rel="noopener noreferrer">Buy Me a Coffee</a> link is available if you want to support development and infrastructure. That payment is not a subscription. By itself, it does not buy priority access, guaranteed uptime, ownership, extra features, or a permanent right to use the service.</p><p>Kalillac may later offer paid features or optional accounts. If it does, those terms will be stated separately. Nothing here means a paid plan exists now.</p></DocSection>
    <DocSection id="ai-limitations" title="AI responses"><p>Responses can be inaccurate, incomplete, outdated, or misleading, including when they sound confident. Kalillac may misunderstand context, reason incorrectly, or cite material that still needs to be checked.</p><p>You decide whether to rely on a response. Verify information before you act on it, especially where a mistake could affect health, legal rights, money, safety, work, education, or another important decision.</p><p>Kalillac can discuss legal, medical, financial, technical, and other professional subjects. The responses are informational. Use of the service does not create an attorney-client, doctor-patient, fiduciary, therapist-client, or other professional relationship with Kalillac AI.</p><p>Where professional judgment matters, treat Kalillac as one source of information, not as a substitute for a qualified professional who can evaluate your situation.</p></DocSection>
    <DocSection id="responsibilities" title="Your responsibilities"><p>You control what you ask and what you do with the answer. You are responsible for your prompts, searches, and instructions, and for decisions, downloads, purchases, installations, communications, transactions, and other actions you take as a result of using the service.</p><p>You are responsible for:</p><ul><li>the content you submit</li><li>judging whether a response is accurate, lawful, safe, and suitable before you use it</li><li>how you use, share, publish, run, install, or otherwise rely on generated material</li><li>complying with laws, contracts, licenses, and other duties that apply to you</li><li>respecting other people’s rights, privacy, property, accounts, credentials, and systems</li><li>checking information when an error could have real consequences</li></ul><p>Do not submit passwords, credentials, API keys, financial account numbers, or other highly sensitive information the request does not require, or information you cannot allow OpenAI, Tavily, or Cloudflare to process.</p></DocSection>
    <DocSection id="user-actions" title="Your searches and later actions"><p>Kalillac provides information. It does not control what you search, ask, investigate, open, download, install, buy, publish, execute, send, or do after a response.</p><p>You are responsible for whether you act, and for the consequences. That includes consequences involving other websites, software, services, accounts, transactions, devices, files, people, or systems you choose to interact with.</p><p>Kalillac AI does not authorize, direct, endorse, or take responsibility for your independent conduct merely because the service discussed a subject, returned information, linked a source, or generated related text.</p><p>To the fullest extent permitted by law, you assume the risk of actions you independently choose to take based on, or after, using Kalillac. This section does not remove a responsibility the law does not allow to be removed.</p><div className="doc-callout doc-callout-amber">A response is information. It is not permission to interfere with another person’s rights, property, privacy, accounts, credentials, or computer systems.</div></DocSection>
    <DocSection id="boundaries" title="Acceptable use"><p>Kalillac is meant to engage with difficult, controversial, and technically sensitive subjects. A subject is not forbidden merely because it is uncomfortable, or because someone else’s terms of service, game rules, or competition rules would restrict it. Those outside rules do not automatically become Kalillac’s rules.</p><p>Kalillac may refuse or narrow a request when the output would materially help cause serious real-world harm to a person, or to that person’s property, finances, privacy, credentials, or computer systems, or when providing it would conflict with law or with a binding requirement on the service.</p><p>If only part of a request has that problem, the intended behavior is to limit that part and continue with the rest, rather than refuse the whole conversation. That is the intended design. It is not a warranty that every answer will be divided perfectly.</p><p>You may not use Kalillac to intentionally facilitate serious harm, including fraud, credential theft, exploitation of children, destructive compromise of systems you do not control and are not authorized to test, or planning real-world violence.</p></DocSection>
    <DocSection id="privacy" title="Privacy and other companies"><p>The <Link href="/privacy">Privacy page</Link> describes messages, session state, logging, and providers. OpenAI, Tavily, Cloudflare, and the hosting provider operate under their own terms. Kalillac does not control them and does not promise anything on their behalf.</p></DocSection>
    <DocSection id="content" title="Prompts and output"><p>As between you and Kalillac, Kalillac does not claim ownership of the text you submit. You give Kalillac the limited permission required to process that content so it can respond and operate the service as the Privacy page describes.</p><p>Kalillac does not promise that output is unique, copyrightable, accurate, non-infringing, or suitable for commercial use. Other people may receive similar or identical output. Rights in AI-generated material depend on the law, the source material, and third-party terms.</p><p>You are responsible for having the rights you need before you publish, sell, distribute, or otherwise rely on generated material.</p></DocSection>
    <DocSection id="intellectual-property" title="Kalillac intellectual property"><p>Unless stated otherwise, rights to the Kalillac name, branding, logo, original site design, original written content, and proprietary application code are reserved or used with permission.</p><p>Open-source libraries, third-party models, third-party services, trademarks, and other third-party materials stay under their own licenses and owners.</p><p>These Terms do not transfer Kalillac’s branding, private source code, credentials, system configuration, or other proprietary materials.</p></DocSection>
    <DocSection id="availability" title="Availability and access"><p>Kalillac may be unavailable, slow, rate-limited, changed, suspended, or discontinued. Advance notice is not guaranteed.</p><p>Kalillac may limit or block access when reasonably necessary to protect the service, respond to abuse, meet a legal duty, keep the system stable, or enforce these Terms. There is no user account to close. The action is a limit on access to the service.</p></DocSection>
    <DocSection id="warranties" title="Disclaimers and liability"><p>To the fullest extent permitted by law, Kalillac is provided “as is” and “as available,” without warranties that it will be uninterrupted, error-free, secure, accurate, current, complete, or fit for a particular purpose.</p><p>You are responsible for evaluating information before you rely on it, and for what you independently choose to do. Kalillac AI is not responsible for losses, penalties, bans, account actions, damages, injuries, disputes, security incidents, legal or financial consequences, data loss, or other outcomes caused by your own conduct, or by your decision to follow, run, publish, install, buy, transmit, or otherwise act on generated information, except where the law imposes a responsibility that cannot be disclaimed.</p><p>To the fullest extent permitted by law, Kalillac AI is not liable for indirect, incidental, special, consequential, exemplary, or similar losses arising from use of the service, inability to use it, reliance on it, interactions with third-party sites, services, software, or content, or actions taken independently after using Kalillac.</p><p>These Terms do not exclude or limit liability that cannot lawfully be excluded or limited. Mandatory consumer rights still apply where they apply.</p></DocSection>
    <DocSection id="law" title="Governing law"><p>These Terms are governed by the laws of the State of Indiana, without regard to conflict-of-law rules, except where federal law or a mandatory consumer protection says otherwise.</p><p>These Terms do not waive a right that applicable law does not allow you to waive.</p></DocSection>
    <DocSection id="changes" title="Changes to these Terms"><p>Kalillac may update these Terms. The new version will be posted here with a revised date.</p><p>A material change should be stated openly. A material change in privacy or data handling should also be reflected on the Privacy page, so the public description matches the service that is running.</p><p>Continued use after updated Terms are posted means those Terms apply to later use, subject to applicable law.</p></DocSection>
    <DocSection id="contact" title="Contact"><p>Questions about these Terms can be sent to Kalillac AI through <a href="https://www.linkedin.com/in/kalillacai" target="_blank" rel="noopener noreferrer">LinkedIn</a>.</p></DocSection>
  </DocLayout>;
}

function FramedNotice() {
  return <main className="not-found"><p>This page cannot be displayed inside another page.</p></main>;
}

function Router() {
  const [location] = useLocation();

  const isHome = location === '/';

  return (
    <ErrorBoundary resetKey={location}>
      {isHome ? <HomePage /> : (
        <Switch>
          <Route path="/privacy" component={PrivacyPage} />
          <Route path="/terms" component={TermsPage} />
          <Route component={NotFound} />
        </Switch>
      )}
    </ErrorBoundary>
  );
}

function App() {
  if (IS_FRAMED) return <FramedNotice />;
  return <WouterRouter><Router /></WouterRouter>;
}

export default App;
