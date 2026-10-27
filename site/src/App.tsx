import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, CircleSlash, Clock, Code2, FileText, Lightbulb, Menu, MessageSquare, PenLine, Search, ShieldCheck, Smartphone, Sparkles, UserRound, X, type LucideIcon } from 'lucide-react';
import { type AroundNavHandler, Link, Route, Switch, Router as WouterRouter, useLocation } from 'wouter';
import { ErrorBoundary } from '@/components/error-boundary';
import NotFound from '@/pages/not-found';
import { updatePublicMetadata } from '@/public-identity';

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
  const toggle = useRef<HTMLButtonElement>(null);
  const close = () => setOpen(false);
  useEffect(() => {
    if (!open) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      close();
      toggle.current?.focus({ preventScroll: true });
    };
    document.addEventListener('keydown', escape);
    return () => document.removeEventListener('keydown', escape);
  }, [open]);
  return (
    <header className="site-header">
      <div className="container-wide site-header-inner">
        <Link href="/" className="brand" data-testid="link-home-brand" onClick={close} aria-label="Kalillac AI home">
          <img className="brand-logo" src={brandLogoUrl} alt="Kalillac AI" />
        </Link>
        <nav className="site-nav" aria-label="Site navigation">
          <a href="/#product" className="nav-link" data-testid="link-product-nav">Product</a>
          <Link href="/privacy" className="nav-link" data-testid="link-privacy-nav">Privacy</Link>
          <a href="/#how-it-works" className="nav-link" data-testid="link-how-nav">How it works</a>
          <button ref={toggle} type="button" className="mobile-nav-toggle" onClick={() => setOpen(!open)} aria-expanded={open} aria-controls="mobile-site-menu" aria-label={open ? 'Close navigation menu' : 'Open navigation menu'} data-testid="button-mobile-menu">
            {open ? <X size={20} aria-hidden="true" /> : <Menu size={20} aria-hidden="true" />}
          </button>
        </nav>
      </div>
      {open && (
        <nav id="mobile-site-menu" className="mobile-site-menu" aria-label="Mobile navigation">
          <a href="/#product" onClick={close} data-testid="link-mobile-product">Product</a>
          <Link href="/privacy" onClick={close} data-testid="link-mobile-privacy">Privacy</Link>
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
        <div className="footer-brand"><Link href="/" className="brand" data-testid="link-footer-home"><img className="brand-logo" src={brandLogoUrl} alt="Kalillac AI" /></Link><p>A more private, helpful internet.</p></div>
        <nav className="footer-links" aria-label="Footer navigation">
          <a href="/#product" data-testid="link-footer-product">Product</a>
          <Link href="/privacy" data-testid="link-footer-privacy">Privacy</Link>
          <a href="/#how-it-works" data-testid="link-footer-how">How it works</a>
          <Link href="/terms" data-testid="link-footer-terms">Terms</Link>
          <a href="https://www.linkedin.com/in/kalillacai" target="_blank" rel="noopener noreferrer" data-testid="link-footer-linkedin">LinkedIn</a>
        </nav>
      </div>
      <div className="container-wide footer-bottom"><span>&copy; 2026 Kalillac AI &middot; Adults 18+</span><span>Kalillac AI can make mistakes. Verify important information.</span></div>
    </footer>
  );
}

function OrbitalAssistant() {
  return <div className="orbital-wrap" aria-hidden="true"><span className="orbit-dot blue"/><span className="orbit-dot mint"/></div>;
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


/* Meaningful flow content is HTML; arrows are decorative. */
function FlowMarkers() { return <ArrowRight className="flow-arrow" size={22} aria-hidden="true"/>; }

const tools: { title: string; description: string; kind: string; icon: LucideIcon }[] = [
  { title: 'Research the current web', description: 'Find current information with sources. Brave is primary; Tavily may provide a bounded fallback or follow-up when eligible.', kind: 'research', icon: Search },
  { title: 'Write and revise', description: 'Draft, edit, and improve your writing. Get clearer, stronger versions and useful feedback.', kind: 'writing', icon: PenLine },
  { title: 'Code and debug', description: 'Build, debug, and explain code in plain language across multiple languages.', kind: 'code', icon: Code2 },
  { title: 'Learn anything', description: 'Get clear, step-by-step explanations on complex topics, from everyday questions to advanced concepts.', kind: 'learning', icon: Lightbulb },
];

function ToolIllustration({ kind, icon: Icon }: { kind: string; icon: LucideIcon }) {
  return <div className={`tool-art art-${kind}`} aria-hidden="true">
    <span className="art-orbit"/><span className="art-dot dot-one"/><span className="art-dot dot-two"/>
    <span className="art-layer"/>
    <span className="art-paper"><i/><i/><i/></span>
    <span className="art-symbol"><Icon size={kind === 'learning' ? 54 : 42} strokeWidth={1.8}/></span>
  </div>;
}

function PowerfulTools() {
  return <section className="tools-section" id="product" aria-labelledby="tools-heading" data-testid="section-tools">
    <div className="container-wide">
      <div className="section-intro"><div><span className="eyebrow">GET MORE DONE</span><h2 id="tools-heading">Powerful tools for real work.</h2></div><p>A private, flexible workspace for research, writing, coding, and learning—with real-time information when you need it.</p></div>
      <div className="tools-grid">{tools.map(tool => <article className="tool-card" key={tool.kind}><ToolIllustration kind={tool.kind} icon={tool.icon}/><h3>{tool.title}</h3><p>{tool.description}</p></article>)}</div>
    </div>
  </section>;
}

function PrivacyPromise() {
  const promises = [
    { title: 'No account required', icon: UserRound, text: 'Start a session without an account. The current product does not offer permanent account chat history.' },
    { title: 'Temporary sessions', icon: Clock, text: 'Your active conversation uses temporary server memory for context, including the facts you ask Kalillac to remember.' },
    { title: 'Clear provider disclosure', icon: ShieldCheck, text: 'OpenAI generates model answers. Relevant query text goes to the search provider used when search is needed.' },
    { title: 'Session inaccessible after refresh', icon: CircleSlash, text: "Refreshing or leaving ends this browser's access. It does not immediately delete the server-memory entry." },
  ];
  return <section className="promise-section" aria-labelledby="promise-heading" data-testid="section-promise"><div className="container-wide promise-panel">
    <span className="eyebrow">OUR PRIVACY PROMISE</span><h2 id="promise-heading">Your conversation stays temporary.</h2>
    <p className="promise-qualification">This refers to facts you ask Kalillac to remember. Current conversation context is also held temporarily; this is not a promise of complete anonymity or no third-party processing.</p>
    <div className="promise-grid">{promises.map(({ title, icon: Icon, text }) => <div className="promise-item" key={title}><span className="promise-icon" aria-hidden="true"><Icon size={25}/></span><div><h3>{title}</h3><p>{text}</p></div></div>)}</div>
  </div></section>;
}

function PrivacyFlow() {
  const stages = [
    { title: '1. You ask', icon: UserRound, text: 'Bring a question, idea, or problem. No account or profile is required.' },
    { title: '2. Temporary session', icon: FileText, text: 'Kalillac keeps current conversation context in temporary server memory. There is no permanent account chat history.' },
    { title: '3. Answer or search', icon: Sparkles, text: 'OpenAI generates model answers. Brave is primary search; Tavily may provide one bounded fallback or native evidence follow-up when eligible.' },
    { title: '4. You move on', icon: CircleSlash, text: "Refreshing or leaving ends this browser's access to the conversation. That is not immediate deletion from the server." },
  ];
  return <section className="flow-section" id="how-it-works" aria-labelledby="flow-heading" data-testid="section-flow"><div className="container-wide flow-panel">
    <div className="section-intro flow-intro"><div><span className="eyebrow eyebrow-on-dark">HOW IT WORKS</span><h2 id="flow-heading">Your conversation has boundaries.</h2></div><p>Kalillac uses AI from OpenAI and can search the current web when needed. Your conversation stays in a temporary session and isn't saved to an account.</p></div>
    <ol className="flow-stages" aria-labelledby="flow-heading" data-testid="flow-summary">{stages.map(({ title, icon: Icon, text }, index) => <li className="flow-stage" data-stage={index + 1} key={title}><span className="flow-stage-icon" aria-hidden="true"><Icon size={27}/></span><div><h3>{title}</h3><p>{text}</p></div>{index < 3 && <FlowMarkers/>}</li>)}</ol>
    <div className="provider-rail"><span className="provider-badge"><Search size={21} aria-hidden="true"/><span><strong>Brave</strong><small>Primary search provider</small></span></span><ArrowRight size={20} aria-hidden="true"/><span className="provider-badge"><Sparkles size={21} aria-hidden="true"/><span><strong>Tavily</strong><small>Fallback or follow-up when eligible</small></span></span></div>
    <p className="flow-policy">Search is used only when needed. A usable Brave response does not automatically trigger Tavily fallback. Tavily may supply one bounded fallback after an eligible Brave failure or, in native routing, one later model-requested evidence follow-up after insufficient Brave results, if Tavily has not already been attempted. Search fallback is not model fallback; there is no automatic alternate generation model. Retrieved evidence can be supplied to OpenAI for the answer.</p>
    <p className="flow-note" data-testid="flow-memory-note"><strong>Temporary session data can remain in server memory until capacity limits or a restart clear it.</strong>{' '}Ending this browser's access is not the same as deleting it from the server.</p>
  </div></section>;
}

type TranscriptTurn = { speaker: 'you' | 'kalillac'; text: string };

/* One real Kalillac session, shown in two phases. Text is verbatim from the
   session; the final response is excerpted (see the disclosure below). */
const MEMORY_SAME_SESSION: TranscriptTurn[] = [
  { speaker: 'you', text: 'Remember for me that my business name is Kalillac AI.' },
  { speaker: 'kalillac', text: 'I’ll remember that your business name is Kalillac AI for this conversation.' },
  { speaker: 'you', text: 'What is my business name?' },
  { speaker: 'kalillac', text: 'Your business name is Kalillac AI.' },
];

const MEMORY_AFTER_REFRESH: TranscriptTurn[] = [
  { speaker: 'you', text: 'Do you remember me telling you what my business name was?' },
  { speaker: 'kalillac', text: 'No. I don’t see your business name in the current session, so I can’t recover it.' },
];


function Transcript({ turns, labelledBy }: { turns: TranscriptTurn[]; labelledBy: string }) {
  return <ol className="memory-transcript" aria-labelledby={labelledBy}>{turns.map((turn, index) => <li key={index} className={`memory-msg memory-msg-${turn.speaker}`}>
    {turn.speaker === 'kalillac' && <span className="memory-avatar" aria-hidden="true"><MessageSquare size={20}/></span>}
    <div className="memory-bubble"><span className="memory-speaker">{turn.speaker === 'you' ? 'You' : 'Kalillac'}<span className="sr-only">:</span></span><p>{turn.text}</p></div>
  </li>)}</ol>;
}

function MemoryDemonstration() {
  return <section className="demos-section" id="temporary-memory" aria-labelledby="memory-heading" data-testid="section-demos"><div className="container-wide">
    <div className="section-intro demos-intro"><div><span className="eyebrow">SEE IT IN ACTION</span><h2 id="memory-heading">Temporary memory you can test yourself.</h2></div><p>Kalillac can remember information during your active conversation. Refreshing starts a new browser session that cannot reopen the previous conversation.</p></div>
    <ol className="memory-phases"><li className="memory-phase memory-phase-during" data-testid="memory-phase-during"><h3 className="memory-phase-label" id="memory-phase-during-label"><span aria-hidden="true">1</span> DURING THE SAME SESSION</h3><Transcript turns={MEMORY_SAME_SESSION} labelledBy="memory-phase-during-label"/></li><li className="memory-phase memory-phase-after" data-testid="memory-phase-after"><h3 className="memory-phase-label" id="memory-phase-after-label"><span aria-hidden="true">2</span> AFTER REFRESHING</h3><Transcript turns={MEMORY_AFTER_REFRESH} labelledBy="memory-phase-after-label"/></li></ol>
    <p className="memory-explain" data-testid="memory-explain">Refreshing ends that browser’s access to the previous temporary conversation. The old session data may remain temporarily in server memory until capacity limits or a restart clear it, but the refreshed browser cannot retrieve it.</p>
    <p className="memory-disclosure" data-testid="memory-disclosure">Real Kalillac session example. The final response was excerpted, and formatting was condensed for display.</p>
  </div></section>;
}

function SessionExplanation() {
  return (
    <section className="explain-section" id="privacy" aria-labelledby="explain-heading" data-testid="section-explain">
      <div className="container-wide">
        <h2 id="explain-heading" className="sr-only">Your session and providers in plain language</h2>
        <div className="explain-grid">
          <div className="explain-block" data-testid="explain-session">
            <span className="explain-icon" aria-hidden="true"><Clock size={24}/></span><div><h3>Your temporary session</h3>
            <p>Kalillac doesn’t require an account and doesn’t offer permanent chat history. Your conversation lasts for the active temporary session. Refreshing or leaving the page ends your browser’s access to it. Temporary session data can remain in server memory until capacity limits or a restart clear it.</p></div>
          </div>
          <div className="explain-block" data-testid="explain-providers">
            <span className="explain-icon" aria-hidden="true"><ShieldCheck size={24}/></span><div><h3>Who processes what</h3>
            <p>OpenAI generates model answers. Brave is the primary search provider when search is needed. Tavily may supply one bounded fallback after an eligible Brave failure or, in native routing, one later model-requested evidence follow-up after insufficient Brave results, if Tavily has not already been attempted. A usable Brave response does not automatically trigger Tavily fallback. Search fallback is not model fallback; there is no automatic alternate generation model. Relevant query text goes to whichever search provider is used; retrieved evidence can be supplied to OpenAI for the answer. OpenAI, Brave and Tavily process what they receive under their own policies.</p></div>
          </div>
        </div>
        <Link href="/privacy" className="text-link explain-link" data-testid="link-full-privacy">Read the full Privacy page <ArrowRight size={16} aria-hidden="true" /></Link>
      </div>
    </section>
  );
}


function MobileAppSection() {
  return <section className="mobile-app-section" aria-labelledby="mobile-app-title" data-testid="section-mobile-app"><div className="container-wide mobile-app-panel">
    <div className="app-copy"><span className="eyebrow">COMING SOON</span><h2 id="mobile-app-title">Kalillac AI app in development.</h2><p>Take Kalillac with you. A native mobile app is in progress, bringing the same private, flexible experience to your phone.</p></div>
    <div className="development-badge"><span aria-hidden="true"><Smartphone size={30}/></span><div><strong>Mobile app in progress</strong><p>No release date announced.</p></div></div>
    <div className="phone-stage" aria-hidden="true"><span className="phone-halo"/><div className="phone"><span className="phone-island"/><div className="phone-content"><img src={brandLogoUrl} alt="" className="phone-logo"/><p>Private by design.<br/>On the go.</p><span className="phone-home"/></div></div></div>
  </div></section>;
}

function HomePage() {
  const workspace = useChatWorkspace();
  const { focusChat } = workspace;
  useEffect(() => {
    const id = window.location.hash.slice(1);
    if (!id) return;
    if (id === 'chat') { focusChat(); return; }
    document.getElementById(id)?.scrollIntoView();
  }, [focusChat]);
  return <div className="site-shell"><SiteHeader/><main>
    <section className="hero" aria-labelledby="hero-title" data-testid="section-hero"><div className="hero-signal-line" aria-hidden="true"/><div className="container-wide hero-stage">
      <div className="hero-copy"><span className="eyebrow">PRIVATE BY DESIGN</span><h1 id="hero-title"><span className="h1-phrase">Think clearly.</span>{' '}<span className="h1-phrase">Write well.</span>{' '}<span className="h1-phrase">Build and debug.</span>{' '}<span className="h1-phrase h1-accent"><span>Search the</span>{' '}<span>current web.</span></span></h1><p className="hero-description">Work through difficult questions, draft and revise writing, explain and repair code, or research current information with sources—without creating an account or building a permanent chat history.</p>
        <button type="button" className="primary-link hero-action" onClick={focusChat} data-testid="button-start-session">Start a private session <ArrowRight size={18} aria-hidden="true"/></button>
        <p className="hero-under-note"><span className="trust-item"><Clock size={16} aria-hidden="true"/> Temporary sessions</span><span className="trust-item"><CircleSlash size={16} aria-hidden="true"/> No account required</span><Link href="/privacy" className="note-link trust-item"><ShieldCheck size={16} aria-hidden="true"/> Clear provider disclosure</Link></p>
      </div><div className="hero-workspace"><OrbitalAssistant/><EmbeddedChat workspace={workspace}/></div>
    </div></section>
    <PowerfulTools/><PrivacyPromise/><PrivacyFlow/><MemoryDemonstration/><SessionExplanation/><MobileAppSection/>
  </main><SiteFooter/></div>;
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
  ['overview', 'Information Kalillac processes'], ['not-persisted', 'What Kalillac does not save'], ['session-state', 'Temporary session state'], ['isolation', 'Session isolation'], ['browser', 'Browser storage'], ['visitor-cookie', 'Daily allowance cookie'], ['logging', 'Logging'], ['model-inference', 'Model inference: OpenAI'], ['tavily', 'Web search: Brave and Tavily'], ['infrastructure', 'Network infrastructure: Cloudflare'], ['cloudflare-security', 'Cloudflare security checks'], ['analytics', 'Analytics and performance measurement'], ['path', 'How a message moves'], ['security', 'Security'], ['age', 'Age'], ['retention', 'Retention in brief'], ['choices', 'What you can do'], ['changes', 'Changes'], ['source', 'About this page'], ['contact', 'Contact'],
];

function PrivacyPage() {
  return <DocLayout title="Privacy" toc={privacyToc} updated="Last updated: October 27, 2026" lede={<><p className="doc-lede">This page describes how information is handled when you use Kalillac AI at <span className="mono">kalillac.ai</span>, including the chat application.</p><p className="doc-lede">Kalillac is an AI assistant for questions, research, writing, coding, learning, and answers that may use the current web. No account or paid subscription is currently offered. This page describes the service as it is designed now. It is not a promise that the service is anonymous, that data is never stored at any layer, or that the service will never change.</p></>}>
    <DocSection id="overview" title="Information Kalillac processes"><p>To answer a message, the application handles:</p><ul><li><strong>The message you type.</strong> Currently limited to 4,000 characters.</li><li><strong>Recent turns of the open conversation.</strong> The chat interface holds the current thread in page memory and sends recent turns with each request so the assistant has context.</li><li><strong>A session identifier.</strong> The server generates an opaque identifier and uses it as the key for a temporary session entry. The chat interface keeps that identifier in page memory. It is not an account.</li><li><strong>Facts you explicitly ask Kalillac to remember during the session.</strong> Those facts are stored as short entries in that temporary session entry.</li><li><strong>Timestamps of recent web searches in the session.</strong> Used only to enforce the current limit of five searches per ten minutes.</li><li><strong>A daily allowance cookie, when the anonymous daily allowance is active.</strong> A random visitor value used only to apply the free daily limit, as described in <a href="#visitor-cookie">Daily allowance cookie</a>.</li></ul><p>Kalillac does not ask for your name, email address, phone number, or account details. The current service has no file upload and does not read documents from your device.</p><p>The systems that deliver the site, including the web server, the operating system, the hosting provider, and Cloudflare, also process ordinary request data such as IP address, date and time, and user agent.</p></DocSection>
    <DocSection id="not-persisted" title="What Kalillac does not save"><p>Kalillac does not save chat transcripts as persistent conversations in its own application database. In its own application, Kalillac does not:</p><ul><li>store chat history across visits</li><li>keep a user profile tied to you</li><li>keep search results in a Kalillac database after using them to write an answer</li></ul><p>Those statements describe Kalillac’s own application. They do not mean that no copy, log, or metadata can exist anywhere else. OpenAI, Brave, Tavily, Cloudflare, and DigitalOcean handle what they receive under their own policies.</p></DocSection>
    <DocSection id="session-state" title="Temporary session state"><p>Kalillac’s session state is an entry in the memory of the running application process. It is not written to a database.</p><p>The entry holds two things:</p><ol><li>memory facts from that session</li><li>timestamps of recent searches in that session</li></ol><p>The application keeps at most 200 session entries at once and at most 50 memory entries per session. When a new session arrives and the store is full, the oldest entry is removed.</p><h3>How this state ends</h3><ul><li>Refreshing or closing the page removes the browser’s access to that session. The chat interface does not keep the thread in saved browser storage, and a new page starts a new, empty session that cannot read the previous entry.</li><li>That does not promise immediate deletion of the server-memory entry. Active-session context may remain in server memory until eviction or service restart.</li><li>When the application process restarts, the in-memory store is gone, because it existed only in that process.</li></ul></DocSection>
    <DocSection id="isolation" title="Session isolation"><p>Each session is stored under its own identifier. A request looks up only the matching entry. There is no shared conversation buffer and no cross-session lookup, so one visitor’s memory entries are not read into another visitor’s context.</p><p>Capacity is shared. With a limit of 200 sessions, heavy traffic can evict older sessions sooner. That affects when state disappears, not who can read it.</p></DocSection>
    <DocSection id="browser" title="Browser storage"><p>The chat interface keeps the session identifier and the completed turns of the open conversation in page memory only. It does not write them to localStorage, sessionStorage, IndexedDB, cookies, or the page address.</p><p>When the anonymous daily allowance is active, Kalillac’s chat service also sets the <a href="#visitor-cookie">daily allowance cookie</a> described below. That cookie holds a random visitor value and none of your conversation.</p><p>The homepage embeds the same chat application that is served at <span className="mono">/app/</span>. Your messages and Kalillac’s responses are not passed through the page address, browser storage, or messages between browser windows. The embedded chat application sends each chat request itself, under the same temporary session behavior described above.</p><p>Kalillac’s own frontend uses your device’s fonts and does not include advertising code, browser analytics code, remote fonts, or third-party asset dependencies. Cloudflare may separately add the <a href="#cloudflare-security">security code described below</a> at its edge.</p></DocSection>
    <DocSection id="visitor-cookie" title="Daily allowance cookie"><p>The anonymous daily allowance is active in the current service.</p><p>When the anonymous daily allowance is active, Kalillac uses one strictly necessary first-party cookie, named <span className="mono">__Host-kalillac_visitor</span>, to apply the free allowance of 10 successful responses per browser per UTC day. It contains a random, opaque visitor value. It does not contain your messages, Kalillac’s responses, or the session identifier.</p><p>The chat service sets this cookie with HttpOnly, Secure, SameSite=Lax, Path=/ and no Domain attribute. It is sent only to the Kalillac host that set it and cannot be read by page scripts. Each anonymous chat response refreshes its 48-hour lifetime.</p><p>Kalillac does not store the raw visitor value in its PostgreSQL database. For each UTC day, the server derives a SHA-256 digest from the visitor value and date. The stored digest changes each UTC day and does not itself provide a stable identifier across days. The database also stores the reservation state and timestamps needed to enforce or refund the allowance.</p><p>The service is designed to retain the current and previous UTC day’s records and lazily prune older records during later anonymous traffic. Because pruning is lazy and best-effort, older records may remain longer when there is no anonymous traffic or cleanup cannot complete.</p><p>The allowance does not use an IP address, browser fingerprinting or account identity. Kalillac does not use this cookie for advertising, analytics or cross-site tracking.</p><p>Clearing or refusing this cookie causes the browser to receive a new visitor value and can reset its per-browser allowance.</p></DocSection>
    <DocSection id="logging" title="Logging"><p>The application writes operational output used to run and diagnose the service. Kalillac does not use that output as a conversation archive. This page does not claim that message content can never appear in server logs.</p><p>The web server, the operating system, Cloudflare, and the hosting provider can create their own request, operational, and security records, which can include data such as IP address, time, and user agent. Those records are governed by the policies of the systems and providers that create them.</p></DocSection>
    <DocSection id="model-inference" title="Model inference: OpenAI"><p>OpenAI may process requests sent for model inference. Kalillac does not run its model on the Kalillac server. The current production model setting is GPT-6 Luna. OpenAI is Kalillac’s only model provider, and there is no automatic fallback to another model or provider: if OpenAI cannot return a usable answer, the request ends with an error instead of being sent elsewhere.</p><p>Some requests never go to OpenAI. For example, deterministic arithmetic and some temporary session-memory requests are handled by Kalillac’s own code.</p><p>A request sent to OpenAI can include, as applicable:</p><ul><li>your current message</li><li>relevant recent turns</li><li>relevant temporary session-memory entries</li><li>the instructions Kalillac assembles for the request</li><li>retrieved web-search text, when the answer uses a search</li></ul><p>Cloudflare Workers AI and Groq are not part of the current active model-provider path.</p><p>OpenAI handles what it receives under its own terms and policies, which Kalillac does not control: <a href="https://openai.com/policies/" target="_blank" rel="noopener noreferrer">OpenAI policies</a>.</p></DocSection>
    <DocSection id="tavily" title="Web search: Brave and Tavily"><p>When web search is used, relevant query text may be sent to Brave or Tavily, depending on which provider is used. Not every message uses web search. OpenAI generates model answers; neither search provider writes Kalillac's answers.</p><p>Brave Search is the primary web search provider. Tavily may provide a bounded fallback when Brave cannot provide usable evidence, or follow-up searches to clarify or verify insufficient evidence. Search fallback and verification do not change the model provider: OpenAI remains the only model provider. A search may fail or leave information unverified.</p><p>When a search runs:</p><ul><li>Kalillac builds the query from the current request. For a follow-up question, the query can also include the earlier topic needed to understand it. The query is limited to 400 characters.</li><li>Search providers return titles, links, and text snippets that Kalillac uses as evidence.</li><li>Kalillac uses the retrieved evidence for the response. When the model writes the answer, that evidence can be sent to OpenAI as described above.</li><li>Kalillac does not keep those results in a Kalillac database.</li><li>Each session is limited to five lookups in a ten-minute window; a bounded fallback or follow-up shares that lookup's session allowance.</li></ul><p>Brave and Tavily handle what they receive under their own terms and privacy policies, which Kalillac does not control. Their retention, logging, storage and training practices are not established by Kalillac's application design. Tavily's policy is available here: <a href="https://www.tavily.com/privacy" target="_blank" rel="noopener noreferrer">Tavily Privacy Policy</a>.</p></DocSection>
    <DocSection id="infrastructure" title="Network infrastructure: Cloudflare"><p>Cloudflare provides reverse proxy, CDN, security and DNS services for <span className="mono">kalillac.ai</span>. Cloudflare is not a Kalillac model provider.</p><p>Traffic to <span className="mono">kalillac.ai</span> passes through Cloudflare before reaching Kalillac’s server. Cloudflare terminates the browser-facing HTTPS connection and forwards the request to Kalillac’s origin over a separate encrypted connection. This means Cloudflare can process request content while proxying it, including chat requests, along with ordinary network information such as IP address, request headers, requested path, browser information, and time. Cloudflare handles what it receives under its own policies.</p><p>DigitalOcean hosts Kalillac’s origin server and handles traffic and system data under its own policies.</p><p>Kalillac does not control Cloudflare’s policies: <a href="https://www.cloudflare.com/privacypolicy/" target="_blank" rel="noopener noreferrer">Cloudflare Privacy Policy</a>.</p></DocSection>
    <DocSection id="cloudflare-security" title="Cloudflare security checks"><p>Cloudflare may add security code used to detect automated or abusive traffic. When those checks run, Cloudflare may set an HttpOnly <span className="mono">cf_clearance</span> cookie showing that the browser passed a security check. Kalillac does not use this cookie to store, reconstruct, or identify prompts, responses, or conversation history.</p></DocSection>
    <DocSection id="analytics" title="Analytics and performance measurement"><p>Cloudflare Web Analytics and Real User Measurements are disabled for <span className="mono">kalillac.ai</span>. Kalillac does not load Cloudflare’s browser RUM beacon or send prompts, responses, or conversation content to that beacon.</p><p>Cloudflare still produces traffic, operational, performance, and security information from requests handled at its edge. Those infrastructure records and aggregate statistics are separate from the disabled browser RUM beacon and are handled under Cloudflare’s own policies.</p></DocSection>
    <DocSection id="path" title="How a message moves"><p>The public site and the chat application are served from the same domain. A chat request passes through Cloudflare’s network to the Kalillac server, where the application decides how to handle it.</p><ul><li><strong>Handled by Kalillac.</strong> For example, deterministic arithmetic or some temporary session-memory requests, without OpenAI, Brave or Tavily.</li><li><strong>Model response.</strong> The request goes to OpenAI.</li><li><strong>Web search.</strong> Relevant query text goes to whichever search provider is used: Brave Search is primary, and Tavily may supply fallback or verification searches as described above. Retrieved evidence can go to OpenAI when the model writes the answer. The response can include source links.</li></ul></DocSection>
    <DocSection id="security" title="Security"><p>No online service can promise that it is completely secure, or that no person or provider could ever access a request, a log, or provider-side data. Do not send passwords, API keys, credentials, payment-card numbers, or other highly sensitive secrets. Ordinary use of Kalillac does not require them.</p></DocSection>
    <DocSection id="age" title="Age"><p>Kalillac is for adults. You must be at least 18. The service is not directed to children. Kalillac does not knowingly seek information from anyone under 18. If it becomes aware that someone under 18 is using the service, it may block that access.</p><p>Use of the service is a representation that you are at least 18. Kalillac does not run a separate age-verification process.</p></DocSection>
    <DocSection id="retention" title="Retention in brief"><table><thead><tr><th>Information</th><th>Where it lives now</th><th>When it ends</th></tr></thead><tbody><tr><td>Open conversation in the chat interface</td><td>Browser page memory</td><td>The browser loses access on refresh, closing the page, or leaving it</td></tr><tr><td>Session identifier</td><td>Page memory, as the key for the server entry</td><td>Same as that page session</td></tr><tr><td>Memory facts and search timestamps</td><td>Server memory, up to 200 sessions and 50 facts</td><td>Eviction or service restart. Refreshing or closing the page does not promise immediate deletion</td></tr><tr><td>Chat transcripts</td><td>Not saved as persistent conversations in Kalillac’s application database</td><td>Not kept as chat history</td></tr><tr><td>Daily allowance cookie, when the anonymous daily allowance is active</td><td>Your browser</td><td>48 hours after the last anonymous chat response that refreshed it, or when you clear it</td></tr><tr><td>Daily allowance records: a per-day digest, reservation state and timestamps</td><td>Kalillac’s application database</td><td>Designed to keep the current and previous UTC day. Older records are pruned lazily during later anonymous traffic and may remain longer</td></tr><tr><td>OpenAI</td><td>OpenAI’s systems</td><td>OpenAI’s policies</td></tr><tr><td>Brave or Tavily, when used for search</td><td>The search provider's systems</td><td>That provider's policies</td></tr><tr><td>Cloudflare and DigitalOcean hosting records</td><td>Those providers</td><td>Their policies</td></tr></tbody></table><p>There is no Kalillac account and no Kalillac conversation archive to export or delete. That is not a claim that no record exists at a provider, or that Kalillac can find and erase every infrastructure record on request.</p></DocSection>
    <DocSection id="choices" title="What you can do"><p>You choose what to type. Do not submit information you cannot allow OpenAI, Brave, Tavily, Cloudflare, or DigitalOcean to process. You can avoid questions that need web search if you do not want query text sent to Brave or Tavily. The service does not ask for an email address and has no account settings.</p><p>When the anonymous daily allowance is active, you can clear or block its cookie in your browser. Doing so can reset your browser’s daily allowance.</p></DocSection>
    <DocSection id="changes" title="Changes"><p>Kalillac is under active development. Models, providers, limits, and session settings are operational choices, not permanent commitments. Possible later features, such as optional accounts or paid plans, are not part of the current service.</p><p>If message handling changes in a way that matters, this page will be updated to describe the new behavior.</p></DocSection>
    <DocSection id="source" title="About this page"><p>Statements about Kalillac’s own application describe its source code and design. Statements about OpenAI, Brave, Tavily, and Cloudflare are limited to what Kalillac sends them; their own policies govern what they do with it. This page is an operator description. It is not an independent privacy or security audit.</p></DocSection>
    <DocSection id="contact" title="Contact"><p>Questions and corrections can go to Kalillac AI through <a href="https://www.linkedin.com/in/kalillacai" target="_blank" rel="noopener noreferrer">LinkedIn</a>.</p></DocSection>
  </DocLayout>;
}

const termsToc: [string, string][] = [
  ['service', 'The service'], ['age', 'Who may use it'], ['support', 'No account and no paid subscription'], ['ai-limitations', 'AI responses'], ['responsibilities', 'Your responsibilities'], ['user-actions', 'Your searches and later actions'], ['boundaries', 'Acceptable use'], ['privacy', 'Privacy and other companies'], ['content', 'Prompts and output'], ['intellectual-property', 'Kalillac intellectual property'], ['availability', 'Availability and access'], ['warranties', 'Disclaimers and liability'], ['law', 'Governing law'], ['changes', 'Changes to these Terms'], ['contact', 'Contact'],
];

function TermsPage() {
  return <DocLayout title="Terms of Use" toc={termsToc} updated="Last updated: October 27, 2026" lede={<><p className="doc-lede">These Terms govern use of Kalillac AI, the service at <span className="mono">kalillac.ai</span>. Kalillac is an AI assistant for questions, research, writing, coding, learning, and answers that may use the current web.</p><p className="doc-lede">The service is currently free and does not require an account. It is only for adults age 18 and older. By using Kalillac, you agree to these Terms. If you do not agree, do not use the service.</p></>}>
    <DocSection id="service" title="The service"><p>Some requests are handled by Kalillac’s own code. OpenAI may process requests sent for model inference. When web search is used, relevant query text may be sent to Brave or Tavily, depending on which provider is used. Brave is primary; Tavily can supply the single bounded fallback or native evidence follow-up described on the Privacy page. Retrieved evidence can be supplied to OpenAI for the answer. OpenAI remains the only generation-model provider, with no automatic alternate generation model. Cloudflare provides reverse proxy, CDN, security and DNS services for the site. Cloudflare Workers AI and Groq are not part of the current active model-provider path. Current data handling is described on the <Link href="/privacy">Privacy page</Link>.</p><p>The service is under active development. Features, models, providers, limits, routing, and the interface may change. No particular model, provider, search capability, limit, or screen is guaranteed to remain available.</p></DocSection>
    <DocSection id="age" title="Who may use it"><p>You must be at least <strong>18 years old</strong>. Kalillac is not directed to children or minors. If you are under 18, do not use it.</p><p>By using Kalillac, you represent that you are at least 18. Kalillac may restrict access if it becomes aware that someone under 18 is using the service. These Terms rely on your representation. They do not describe a separate age-verification process.</p></DocSection>
    <DocSection id="support" title="No account and no paid subscription"><p>The free allowance is 10 successful responses per browser per UTC day. The service uses the host-only <span className="mono">__Host-kalillac_visitor</span> cookie and PostgreSQL usage records containing a daily SHA-256 digest, reservation state and timestamps, rather than the raw visitor value or persistent conversation history. See the <Link href="/privacy#visitor-cookie">Privacy page’s daily allowance disclosure</Link> for cookie settings and retention.</p><p>No account or paid subscription is currently offered. These Terms are posted on the site because there is no registration step.</p><p>A voluntary <a href="https://buymeacoffee.com/kalillactv7" target="_blank" rel="noopener noreferrer">Buy Me a Coffee</a> link is available if you want to support development and infrastructure. That payment is not a subscription. By itself, it does not buy priority access, guaranteed uptime, ownership, extra features, or a permanent right to use the service.</p><p>Kalillac may later offer paid features or optional accounts. If it does, those terms will be stated separately. Nothing here means a paid plan exists now.</p></DocSection>
    <DocSection id="ai-limitations" title="AI responses"><p>Responses can be inaccurate, incomplete, outdated, or misleading, including when they sound confident. Kalillac may misunderstand context, reason incorrectly, or cite material that still needs to be checked.</p><p>You decide whether to rely on a response. Verify information before you act on it, especially where a mistake could affect health, legal rights, money, safety, work, education, or another important decision.</p><p>Kalillac can discuss legal, medical, financial, technical, and other professional subjects. The responses are informational. Use of the service does not create an attorney-client, doctor-patient, fiduciary, therapist-client, or other professional relationship with Kalillac AI.</p><p>Where professional judgment matters, treat Kalillac as one source of information, not as a substitute for a qualified professional who can evaluate your situation.</p></DocSection>
    <DocSection id="responsibilities" title="Your responsibilities"><p>You control what you ask and what you do with the answer. You are responsible for your prompts, searches, and instructions, and for decisions, downloads, purchases, installations, communications, transactions, and other actions you take as a result of using the service.</p><p>You are responsible for:</p><ul><li>the content you submit</li><li>judging whether a response is accurate, lawful, safe, and suitable before you use it</li><li>how you use, share, publish, run, install, or otherwise rely on generated material</li><li>complying with laws, contracts, licenses, and other duties that apply to you</li><li>respecting other people’s rights, privacy, property, accounts, credentials, and systems</li><li>checking information when an error could have real consequences</li></ul><p>Do not submit passwords, credentials, API keys, financial account numbers, or other highly sensitive information the request does not require, or information you cannot allow OpenAI, Brave, Tavily, Cloudflare, or DigitalOcean to process.</p></DocSection>
    <DocSection id="user-actions" title="Your searches and later actions"><p>Kalillac provides information. It does not control what you search, ask, investigate, open, download, install, buy, publish, execute, send, or do after a response.</p><p>You are responsible for whether you act, and for the consequences. That includes consequences involving other websites, software, services, accounts, transactions, devices, files, people, or systems you choose to interact with.</p><p>Kalillac AI does not authorize, direct, endorse, or take responsibility for your independent conduct merely because the service discussed a subject, returned information, linked a source, or generated related text.</p><p>To the fullest extent permitted by law, you assume the risk of actions you independently choose to take based on, or after, using Kalillac. This section does not remove a responsibility the law does not allow to be removed.</p><div className="doc-callout doc-callout-amber">A response is information. It is not permission to interfere with another person’s rights, property, privacy, accounts, credentials, or computer systems.</div></DocSection>
    <DocSection id="boundaries" title="Acceptable use"><p>Kalillac is meant to engage with difficult, controversial, and technically sensitive subjects. A subject is not forbidden merely because it is uncomfortable, or because someone else’s terms of service, game rules, or competition rules would restrict it. Those outside rules do not automatically become Kalillac’s rules.</p><p>Kalillac may refuse or narrow a request when the output would materially help cause serious real-world harm to a person, or to that person’s property, finances, privacy, credentials, or computer systems, or when providing it would conflict with law or with a binding requirement on the service.</p><p>If only part of a request has that problem, the intended behavior is to limit that part and continue with the rest, rather than refuse the whole conversation. That is the intended design. It is not a warranty that every answer will be divided perfectly.</p><p>You may not use Kalillac to intentionally facilitate serious harm, including fraud, credential theft, exploitation of children, destructive compromise of systems you do not control and are not authorized to test, or planning real-world violence.</p></DocSection>
    <DocSection id="privacy" title="Privacy and other companies"><p>OpenAI is the only model provider, with GPT-6 Luna as the current production model setting and no automatic model-provider fallback. Brave Search is primary for web search; Tavily may provide fallback or verification. Cloudflare provides the reverse proxy, CDN, security and DNS services, and DigitalOcean hosts the origin server.</p><p>The <Link href="/privacy">Privacy page</Link> describes messages, session state, logging, and providers. OpenAI, Brave, Tavily, Cloudflare, and DigitalOcean operate under their own terms. Kalillac does not control them and does not promise anything on their behalf.</p></DocSection>
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
  useEffect(() => { updatePublicMetadata(location); }, [location]);

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

/* Every in-app navigation (<Link> and navigate()) passes through here. The new
   page starts at its top, or at its #fragment target when the link has one,
   instead of inheriting the previous page's scroll position. The jump is
   immediate: an inline scroll-behavior briefly overrides the site's smooth
   scrolling (not the newer 'instant' value, which older browsers reject). Browser
   Back/Forward does not pass through here, so its scroll restoration is kept. */
const jumpTo = (scroll: () => void) => {
  const root = document.documentElement;
  const previous = root.style.scrollBehavior;
  root.style.scrollBehavior = 'auto';
  // Read it back so the browser applies the override before scrolling;
  // otherwise it scrolls with the stale (smooth) computed value.
  void getComputedStyle(root).scrollBehavior;
  scroll();
  root.style.scrollBehavior = previous;
};

const startNavigationAtTop: AroundNavHandler = (navigate, to, options) => {
  navigate(to, options);
  const fragment = to.includes('#') ? decodeURIComponent(to.slice(to.indexOf('#') + 1)) : '';
  if (!fragment) {
    jumpTo(() => window.scrollTo(0, 0));
    return;
  }
  // The target section belongs to the page being rendered; scroll once it exists.
  requestAnimationFrame(() => {
    const target = document.getElementById(fragment);
    jumpTo(() => (target ? target.scrollIntoView() : window.scrollTo(0, 0)));
  });
};

function App() {
  if (IS_FRAMED) return <FramedNotice />;
  return <WouterRouter aroundNav={startNavigationAtTop}><Router /></WouterRouter>;
}

export default App;
