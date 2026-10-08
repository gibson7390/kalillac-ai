import { type CSSProperties, type MouseEvent, type ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, BookOpen, Code2, Compass, FileText, Menu, MessageSquare, Search, Sparkles, X } from 'lucide-react';
import { Link, Route, Switch, Router as WouterRouter, useLocation } from 'wouter';
import { ErrorBoundary } from '@/components/error-boundary';
import NotFound from '@/pages/not-found';
import { CHAT_PATH, isPromptSubmittedMessage, postPresentation } from './embed-protocol';

const brandLogoUrl = '/assets/kalillac-ai-logo.png';

/* The path this document was first loaded at. Inside this site, /app/ is only
   ever reached through the chat handoff's history.pushState; a real load of
   /app/ (direct visit or refresh) is the canonical chat in ../frontend. If
   this site were ever served at /app/ anyway, it must not frame itself. */
const INITIAL_PATH = window.location.pathname;
const IS_FRAMED = (() => {
  try { return window.self !== window.top; } catch { return true; }
})();

function SiteHeader({ inert, onTryKalillac }: { inert?: boolean; onTryKalillac?: (event: MouseEvent<HTMLAnchorElement>) => void }) {
  const [open, setOpen] = useState(false);
  const close = () => setOpen(false);
  const tryKalillac = (event: MouseEvent<HTMLAnchorElement>) => { close(); onTryKalillac?.(event); };
  return (
    <header className="site-header" inert={inert}>
      <div className="container-wide site-header-inner">
        <Link href="/" className="brand" data-testid="link-home-brand" onClick={close} aria-label="Kalillac AI home">
          <img className="brand-logo" src={brandLogoUrl} alt="Kalillac AI" />
        </Link>
        <nav className="site-nav" aria-label="Site navigation">
          <a href="/#product" className="nav-link" data-testid="link-product-nav">Product</a>
          <Link href="/privacy" className="nav-link" data-testid="link-privacy-nav">Privacy</Link>
          <a href="/#how-it-works" className="nav-link" data-testid="link-how-nav">How it works</a>
          <a href="/#chat" className="nav-cta" data-testid="link-try-nav" onClick={tryKalillac}>Try Kalillac</a>
          <button type="button" className="mobile-nav-toggle" onClick={() => setOpen(!open)} aria-expanded={open} aria-controls="mobile-site-menu" aria-label={open ? 'Close navigation menu' : 'Open navigation menu'} data-testid="button-mobile-menu">
            {open ? <X size={19} /> : <Menu size={19} />}
          </button>
        </nav>
      </div>
      {open && (
        <nav id="mobile-site-menu" className="mobile-site-menu" aria-label="Mobile navigation">
          <a href="/#product" onClick={close} data-testid="link-mobile-product">Product</a>
          <Link href="/privacy" onClick={close} data-testid="link-mobile-privacy">Privacy</Link>
          <a href="/#how-it-works" onClick={close} data-testid="link-mobile-how">How it works</a>
          <a href="/#chat" onClick={tryKalillac} data-testid="link-mobile-try">Try Kalillac</a>
        </nav>
      )}
    </header>
  );
}

function SiteFooter({ inert }: { inert?: boolean }) {
  return (
    <footer className="site-footer" inert={inert}>
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

/* The homepage chat card and its handoff into the full /app/ workspace.

   The card is the canonical /app/ client in a same-origin iframe, shown at a
   FIXED height: it never grows with the conversation. When the framed chat
   reports that a valid prompt is being submitted (embed-protocol.ts), the
   card expands to the full viewport, the URL becomes /app/ through
   history.pushState, and the iframe is told its new presentation. The iframe
   is never navigated, re-parented or remounted, so the prompt it is already
   sending and the reply stay in place. Back (popstate to a non-/app/ entry)
   returns to the card; it sends nothing to the chat except the presentation
   message, so no prompt is ever resent. */
function useChatHandoff() {
  const slotRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [expanded, setExpanded] = useState(() => window.location.pathname === CHAT_PATH);
  const [fromClip, setFromClip] = useState<string | null>(null);

  const present = useCallback((next: boolean) => {
    if (next) {
      // Reveal from the card's current on-screen rectangle.
      const slot = slotRef.current;
      if (slot) {
        const r = slot.getBoundingClientRect();
        const right = Math.max(0, window.innerWidth - r.right);
        const bottom = Math.max(0, window.innerHeight - r.bottom);
        setFromClip(`inset(${Math.max(0, r.top)}px ${right}px ${bottom}px ${Math.max(0, r.left)}px round 21px)`);
      }
    }
    setExpanded(next);
    postPresentation(frameRef.current?.contentWindow ?? null, window.location.origin, next);
  }, []);

  useEffect(() => {
    const origin = window.location.origin;
    const frame = frameRef.current;

    const onMessage = (event: MessageEvent) => {
      if (!isPromptSubmittedMessage(event, frameRef.current?.contentWindow ?? null, origin)) return;
      if (window.location.pathname !== CHAT_PATH) {
        window.history.pushState({ kalillac: 'chat' }, '', CHAT_PATH);
      }
      present(true);
    };

    const onPopState = () => present(window.location.pathname === CHAT_PATH);

    // A (re)loaded chat document learns the current presentation.
    const onFrameLoad = () => postPresentation(frame?.contentWindow ?? null, origin, window.location.pathname === CHAT_PATH);

    window.addEventListener('message', onMessage);
    window.addEventListener('popstate', onPopState);
    frame?.addEventListener('load', onFrameLoad);

    return () => {
      window.removeEventListener('message', onMessage);
      window.removeEventListener('popstate', onPopState);
      frame?.removeEventListener('load', onFrameLoad);
    };
  }, [present]);

  // Only the expanded chat scrolls while it covers the page.
  useEffect(() => {
    document.documentElement.classList.toggle('chat-expanded', expanded);
    return () => document.documentElement.classList.remove('chat-expanded');
  }, [expanded]);

  const focusChat = useCallback(() => {
    slotRef.current?.scrollIntoView({ block: 'center' });
    frameRef.current?.focus();
  }, []);

  return { slotRef, frameRef, expanded, fromClip, focusChat };
}

function EmbeddedChat({ handoff }: { handoff: ReturnType<typeof useChatHandoff> }) {
  const { slotRef, frameRef, expanded, fromClip } = handoff;
  const style = fromClip ? ({ '--chat-from-clip': fromClip } as CSSProperties) : undefined;
  return (
    <div id="chat" className="embedded-chat-slot" ref={slotRef}>
      <div className={`embedded-chat${expanded ? ' is-expanded' : ''}`} style={style} data-testid="embedded-chat">
        <iframe ref={frameRef} src={CHAT_PATH} title="Chat with Kalillac AI" loading="eager" />
      </div>
    </div>
  );
}

function HomePage() {
  const handoff = useChatHandoff();
  const { expanded, focusChat } = handoff;

  // Section links (/#product, /#chat ...) arriving from another page.
  useEffect(() => {
    const id = window.location.hash.slice(1);
    if (!id) return;
    if (id === 'chat') { focusChat(); return; }
    document.getElementById(id)?.scrollIntoView();
  }, [focusChat]);

  const tryKalillac = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    focusChat();
  };

  return (
    <div className="site-shell">
      <SiteHeader inert={expanded} onTryKalillac={tryKalillac} />
      <main>
        <section className="hero" aria-labelledby="hero-title">
          <div className="hero-signal-line" aria-hidden="true" />
          <div className="container-wide hero-layout">
            <div className="hero-grid" inert={expanded}>
              <div className="hero-copy">
                <span className="eyebrow"><span className="eyebrow-dot" /> MEET KALILLAC AI</span>
                <h1 id="hero-title">Private by design.<br /><span>Powerful when it matters.</span></h1>
                <p className="hero-description">Ask questions, develop ideas, write and troubleshoot code, or search the current web—without creating an account or building a permanent chat history.</p>
                <button type="button" className="primary-link hero-action" onClick={focusChat} data-testid="button-start-session">Start a private session <ArrowRight size={16} /></button>
              </div>
              <OrbitalAssistant />
            </div>
            <EmbeddedChat handoff={handoff} />
            <p className="hero-under-note" inert={expanded}><span className="live-dot" /> Temporary sessions <span className="note-divider">·</span> No account required <span className="note-divider">·</span> <Link href="/privacy" className="note-link">Clear provider disclosure</Link></p>
          </div>
        </section>
        <div inert={expanded}>
          <section className="quick-capabilities container-wide" aria-label="Kalillac at a glance">
            <article><span className="quick-icon"><MessageSquare size={20}/></span><div><h3>Questions, unpacked</h3><p>Work through ideas and difficult topics.</p></div></article>
            <article><span className="quick-icon mint"><FileText size={20}/></span><div><h3>Words and code</h3><p>Draft, rewrite, explain, and troubleshoot.</p></div></article>
            <article><span className="quick-icon cobalt"><Search size={20}/></span><div><h3>Current when it counts</h3><p>Use web information when a question needs it.</p></div></article>
          </section>

          <section className="capabilities-section" id="product" aria-labelledby="capabilities-title">
            <div className="container-wide">
              <div className="section-heading"><div><span className="section-label">MADE FOR THE WAY YOU THINK</span><h2 className="section-title" id="capabilities-title">One place for the work<br />that doesn’t fit in a box.</h2></div><p className="section-copy">A flexible place for everyday questions and deeper work. Move between tasks in the same conversation.</p></div>
              <div className="capability-grid">
                <article className="capability-panel capability-feature"><div className="capability-icon"><Compass size={23}/></div><span className="capability-index">01 / THINK</span><h3>Make sense of<br />the complicated.</h3><p>Work through questions, concepts, and difficult topics one step at a time.</p><div className="capability-art" aria-hidden="true"><span/><span/><span/><i/></div></article>
                <article className="capability-panel"><div className="capability-icon"><FileText size={22}/></div><span className="capability-index">02 / WRITE</span><h3>Find the right words.</h3><p>Draft, rewrite, organize, summarize, and brainstorm when the blank page gets in the way.</p></article>
                <article className="capability-panel"><div className="capability-icon"><Code2 size={22}/></div><span className="capability-index">03 / BUILD</span><h3>Get unstuck in code.</h3><p>Write, explain, troubleshoot, and improve code with a conversational collaborator.</p></article>
                <article className="capability-panel capability-web"><div className="capability-icon"><Search size={22}/></div><span className="capability-index">04 / DISCOVER</span><h3>Go beyond what’s already known.</h3><p>When a question calls for up-to-date information, Kalillac can search the current web and include sources.</p><span className="web-path" aria-hidden="true"><i/><i/><i/><i/></span></article>
              </div>
            </div>
          </section>

          <section className="mobile-app-section" aria-labelledby="mobile-app-title">
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

          <section className="works-section" id="how-it-works" aria-labelledby="works-title">
            <div className="container-wide works-layout">
              <div className="works-intro"><span className="section-label">THE SIGNAL, SIMPLIFIED</span><h2 className="section-title" id="works-title">How Kalillac<br/>works</h2><p className="section-copy">One temporary conversation. Clear about who processes what.</p></div>
              <div className="steps">
                <article className="step-card"><div className="step-top"><span className="step-number">01</span><MessageSquare size={21}/></div><h3>Ask</h3><p>Type a question or choose a starting prompt. No account is needed.</p></article>
                <span className="step-connector" aria-hidden="true"><ArrowRight size={17}/></span>
                <article className="step-card"><div className="step-top"><span className="step-number">02</span><Sparkles size={21}/></div><h3>Kalillac works through it</h3><p>Model responses come from OpenAI. When a question needs the current web, the search runs through Tavily.</p></article>
                <span className="step-connector" aria-hidden="true"><ArrowRight size={17}/></span>
                <article className="step-card"><div className="step-top"><span className="step-number">03</span><BookOpen size={21}/></div><h3>Continue, then move on</h3><p>Keep working in the current temporary session. Refreshing or leaving the page ends your browser’s access to it.</p></article>
              </div>
            </div>
          </section>

          <section className="privacy-band" id="sessions" aria-labelledby="memory-title">
            <div className="container-wide privacy-inner">
              <div className="privacy-orbit" aria-hidden="true"><span/><span/><i/></div>
              <div><span className="section-label">A NOTE ON YOUR SESSION</span><h2 id="memory-title">A conversation for now.</h2><p>Kalillac keeps context for the active temporary session and does not offer a saved chat history. Refreshing or leaving the page ends your browser’s access to the conversation. Temporary session data can remain in server memory until capacity limits or a restart clear it.</p></div>
              <Link href="/privacy" className="text-link" data-testid="link-full-privacy">How Kalillac handles data <ArrowRight size={16}/></Link>
            </div>
          </section>
        </div>
      </main>
      <SiteFooter inert={expanded} />
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
  ['overview', 'Information Kalillac processes'], ['not-persisted', 'What Kalillac does not save'], ['session-state', 'Temporary session state'], ['isolation', 'Session isolation'], ['browser', 'Browser storage'], ['logging', 'Logging'], ['model-inference', 'Model inference: OpenAI'], ['tavily', 'Web search: Tavily'], ['infrastructure', 'Network infrastructure: Cloudflare'], ['path', 'How a message moves'], ['security', 'Security'], ['age', 'Age'], ['retention', 'Retention in brief'], ['choices', 'What you can do'], ['changes', 'Changes'], ['source', 'About this page'], ['contact', 'Contact'],
];

function PrivacyPage() {
  return <DocLayout title="Privacy" toc={privacyToc} updated="Last updated: October 8, 2026" lede={<><p className="doc-lede">This page describes how information is handled when you use Kalillac AI at <span className="mono">kalillac.com</span>, including the chat application.</p><p className="doc-lede">Kalillac is an AI assistant for questions, research, writing, coding, learning, and answers that may use the current web. No account or paid subscription is currently offered. This page describes the service as it is designed now. It is not a promise that the service is anonymous, that data is never stored at any layer, or that the service will never change.</p></>}>
    <DocSection id="overview" title="Information Kalillac processes"><p>To answer a message, the application handles:</p><ul><li><strong>The message you type.</strong> Currently limited to 4,000 characters.</li><li><strong>Recent turns of the open conversation.</strong> The chat interface holds the current thread in page memory and sends recent turns with each request so the assistant has context.</li><li><strong>A session identifier.</strong> The server generates an opaque identifier and uses it as the key for a temporary session entry. The chat interface keeps that identifier in page memory. It is not an account.</li><li><strong>Facts you explicitly ask Kalillac to remember during the session.</strong> Those facts are stored as short entries in that temporary session entry.</li><li><strong>Timestamps of recent web searches in the session.</strong> Used only to enforce the current limit of five searches per ten minutes.</li></ul><p>Kalillac does not ask for your name, email address, phone number, or account details. The current service has no file upload and does not read documents from your device.</p><p>The systems that deliver the site, including the web server, the operating system, the hosting provider, and Cloudflare, also process ordinary request data such as IP address, date and time, and user agent.</p></DocSection>
    <DocSection id="not-persisted" title="What Kalillac does not save"><p>Kalillac does not save chat transcripts as persistent conversations in its own application database. In its own application, Kalillac does not:</p><ul><li>store chat history across visits</li><li>keep a user profile tied to you</li><li>keep search results in a Kalillac database after using them to write an answer</li></ul><p>Those statements describe Kalillac’s own application. They do not mean that no copy, log, or metadata can exist anywhere else. OpenAI, Tavily, Cloudflare, and the hosting provider handle what they receive under their own policies.</p></DocSection>
    <DocSection id="session-state" title="Temporary session state"><p>Kalillac’s session state is an entry in the memory of the running application process. It is not written to a database.</p><p>The entry holds two things:</p><ol><li>memory facts from that session</li><li>timestamps of recent searches in that session</li></ol><p>The application keeps at most 200 session entries at once and at most 50 memory entries per session. When a new session arrives and the store is full, the oldest entry is removed.</p><h3>How this state ends</h3><ul><li>Refreshing or closing the page removes the browser’s access to that session. The chat interface does not keep the thread in saved browser storage, and a new page starts a new, empty session that cannot read the previous entry.</li><li>That does not promise immediate deletion of the server-memory entry. Active-session context may remain in server memory until eviction or service restart.</li><li>When the application process restarts, the in-memory store is gone, because it existed only in that process.</li></ul></DocSection>
    <DocSection id="isolation" title="Session isolation"><p>Each session is stored under its own identifier. A request looks up only the matching entry. There is no shared conversation buffer and no cross-session lookup, so one visitor’s memory entries are not read into another visitor’s context.</p><p>Capacity is shared. With a limit of 200 sessions, heavy traffic can evict older sessions sooner. That affects when state disappears, not who can read it.</p></DocSection>
    <DocSection id="browser" title="Browser storage"><p>The chat interface keeps the session identifier and the completed turns of the open conversation in page memory only. It does not write them to localStorage, sessionStorage, IndexedDB, cookies, or the page address.</p><p>When you start a chat from the homepage, the homepage and the chat exchange only a signal that the chat should open full-screen. Your message is not copied into the page address, browser storage, or a cookie to make that transition.</p><p>The site and the chat interface use your device’s own fonts and load no fonts, stylesheets, or scripts from other websites. They do not include advertising or analytics scripts.</p></DocSection>
    <DocSection id="logging" title="Logging"><p>The application writes operational output used to run and diagnose the service. Kalillac does not use that output as a conversation archive. This page does not claim that message content can never appear in server logs.</p><p>The web server, the operating system, Cloudflare, and the hosting provider can create their own request, operational, and security records, which can include data such as IP address, time, and user agent. Those records are governed by the policies of the systems and providers that create them.</p></DocSection>
    <DocSection id="model-inference" title="Model inference: OpenAI"><p>OpenAI may process requests sent for model inference. Kalillac does not run its model on the Kalillac server. OpenAI is Kalillac’s only model provider, and there is no automatic fallback to another model or provider: if OpenAI cannot return a usable answer, the request ends with an error instead of being sent elsewhere.</p><p>Some requests never go to OpenAI. For example, deterministic arithmetic and some temporary session-memory requests are handled by Kalillac’s own code.</p><p>A request sent to OpenAI can include, as applicable:</p><ul><li>your current message</li><li>relevant recent turns</li><li>relevant temporary session-memory entries</li><li>the instructions Kalillac assembles for the request</li><li>retrieved web-search text, when the answer uses a search</li></ul><p>Cloudflare Workers AI and Groq are not part of the current active model-provider path.</p><p>OpenAI handles what it receives under its own terms and policies, which Kalillac does not control: <a href="https://openai.com/policies/" target="_blank" rel="noopener noreferrer">OpenAI policies</a>.</p></DocSection>
    <DocSection id="tavily" title="Web search: Tavily"><p>When web search is used, relevant query text may be sent to Tavily. Most messages do not use web search, and Tavily does not write Kalillac’s answers.</p><p>When a search runs:</p><ul><li>Kalillac builds the query from the current request. For a follow-up question, the query can also include the earlier topic needed to understand it. The query sent to Tavily is limited to 400 characters.</li><li>Tavily returns up to four titles, links, and text snippets. When a request is about a specific public web page, Kalillac can also ask Tavily for that page’s text.</li><li>Kalillac uses those results for the response. When the model writes the answer, the results are sent to OpenAI as described above.</li><li>Kalillac does not keep those results in a Kalillac database.</li><li>Each session is limited to five searches in a ten-minute window.</li></ul><p>Tavily handles what it receives under its own terms and privacy policy, which Kalillac does not control: <a href="https://www.tavily.com/privacy" target="_blank" rel="noopener noreferrer">Tavily Privacy Policy</a>.</p></DocSection>
    <DocSection id="infrastructure" title="Network infrastructure: Cloudflare"><p>Cloudflare provides edge delivery and security for <span className="mono">kalillac.com</span> and may process network metadata, such as IP address and request details, for traffic to the site, including chat requests. Cloudflare is not a Kalillac model provider.</p><p>Kalillac’s server runs on a hosting provider’s infrastructure, which handles traffic and system data under its own policies.</p><p>Cloudflare handles what it receives under its own policies, which Kalillac does not control: <a href="https://www.cloudflare.com/privacypolicy/" target="_blank" rel="noopener noreferrer">Cloudflare Privacy Policy</a>.</p></DocSection>
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

  // "/" and an in-page handoff to /app/ render the SAME HomePage element in
  // the same position, so the chat iframe is never remounted by the URL
  // change. A real load at /app/ is not this site's page.
  const isHome = location === '/' || (location === CHAT_PATH && INITIAL_PATH !== CHAT_PATH);

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
