import { type ReactNode, useEffect, useRef, useState } from 'react';
import { ArrowUp, ArrowRight, BookOpen, Code2, Compass, FileText, Menu, MessageSquare, Search, Sparkles, X } from 'lucide-react';
import { Link, Route, Switch, Router as WouterRouter } from 'wouter';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import renderMathInElement from 'katex/contrib/auto-render';
import 'katex/dist/katex.min.css';
import appJsUrl from './assets/app_1790308213208.js?url';
import { ErrorBoundary } from '@/components/error-boundary';
import { Toaster } from '@/components/ui/toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import NotFound from '@/pages/not-found';

const queryClient = new QueryClient();
const brandLogoUrl = `${import.meta.env.BASE_URL}assets/kalillac-ai-logo.png`;

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
          <Link href="/privacy" className="nav-link" data-testid="link-privacy-nav" onClick={close}>Privacy</Link>
          <Link href="/terms" className="nav-link" data-testid="link-terms-nav" onClick={close}>Terms</Link>
          <button type="button" className="mobile-nav-toggle" onClick={() => setOpen(!open)} aria-expanded={open} aria-controls="mobile-site-menu" aria-label={open ? 'Close navigation menu' : 'Open navigation menu'} data-testid="button-mobile-menu">
            {open ? <X size={19} /> : <Menu size={19} />}
          </button>
        </nav>
      </div>
      {open && (
        <nav id="mobile-site-menu" className="mobile-site-menu" aria-label="Mobile navigation">
          <Link href="/privacy" onClick={close} data-testid="link-mobile-privacy">Privacy</Link>
          <Link href="/terms" onClick={close} data-testid="link-mobile-terms">Terms</Link>
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
      <div className="container-wide footer-bottom"><span>© 2026 Kalillac AI</span><span>Kalillac AI can make mistakes. Verify important information.</span></div>
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

function EmbeddedChat() {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [active, setActive] = useState(false);
  useEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;
    let observer: MutationObserver | undefined;
    const watch = () => {
      observer?.disconnect();
      try {
        const document = frame.contentDocument;
        if (!document?.body) return;
        const sync = () => setActive(Boolean(document.querySelector('#thread > .msg')));
        observer = new MutationObserver(sync);
        observer.observe(document.body, { childList: true, subtree: true });
        sync();
      } catch { /* The same-origin embed is still usable without automatic resizing. */ }
    };
    frame.addEventListener('load', watch);
    if (frame.contentDocument?.readyState === 'complete') watch();
    return () => { frame.removeEventListener('load', watch); observer?.disconnect(); };
  }, []);
  return <div id="chat" className={`embedded-chat${active ? ' has-conversation' : ''}`}><iframe ref={frameRef} src={`${import.meta.env.BASE_URL}app`} title="Chat with Kalillac AI" loading="eager" /></div>;
}

function HomePage() {
  return (
    <div className="site-shell">
      <SiteHeader />
      <main>
        <section className="hero" aria-labelledby="hero-title">
          <div className="hero-signal-line" aria-hidden="true" />
          <div className="container-wide hero-layout">
            <div className="hero-grid">
              <div className="hero-copy">
                <span className="eyebrow"><span className="eyebrow-dot" /> MEET KALILLAC AI</span>
                <h1 id="hero-title">Ask. Write. Code.<br /><span>Explore what’s next.</span></h1>
                <p className="hero-description">Work through questions, develop ideas, write, code, and use current web information when you need it. Start right here.</p>
              </div>
              <OrbitalAssistant />
            </div>
            <EmbeddedChat />
            <p className="hero-under-note"><span className="live-dot" /> A conversation starts here <span className="note-divider">/</span> No account required</p>
          </div>
        </section>
        <section className="quick-capabilities container-wide" aria-label="Kalillac at a glance">
          <article><span className="quick-icon"><MessageSquare size={20}/></span><div><h3>Questions, unpacked</h3><p>Work through ideas and difficult topics.</p></div></article>
          <article><span className="quick-icon mint"><FileText size={20}/></span><div><h3>Words and code</h3><p>Draft, rewrite, explain, and troubleshoot.</p></div></article>
          <article><span className="quick-icon cobalt"><Search size={20}/></span><div><h3>Current when it counts</h3><p>Use web information when a question needs it.</p></div></article>
        </section>

        <section className="capabilities-section" aria-labelledby="capabilities-title">
          <div className="container-wide">
            <div className="section-heading"><div><span className="section-label">MADE FOR THE WAY YOU THINK</span><h2 className="section-title" id="capabilities-title">One place for the work<br />that doesn’t fit in a box.</h2></div><p className="section-copy">A flexible place for everyday questions and deeper work. Move between tasks in the same conversation.</p></div>
            <div className="capability-grid">
              <article className="capability-panel capability-feature"><div className="capability-icon"><Compass size={23}/></div><span className="capability-index">01 / THINK</span><h3>Make sense of<br />the complicated.</h3><p>Work through questions, concepts, and difficult topics one step at a time.</p><div className="capability-art" aria-hidden="true"><span/><span/><span/><i/></div></article>
              <article className="capability-panel"><div className="capability-icon"><FileText size={22}/></div><span className="capability-index">02 / WRITE</span><h3>Find the right words.</h3><p>Draft, rewrite, organize, summarize, and brainstorm when the blank page gets in the way.</p></article>
              <article className="capability-panel"><div className="capability-icon"><Code2 size={22}/></div><span className="capability-index">03 / BUILD</span><h3>Get unstuck in code.</h3><p>Write, explain, troubleshoot, and improve code with a conversational collaborator.</p></article>
              <article className="capability-panel capability-web"><div className="capability-icon"><Search size={22}/></div><span className="capability-index">04 / DISCOVER</span><h3>Go beyond what’s already known.</h3><p>When a question calls for up-to-date information, Kalillac can use the current web and include sources.</p><span className="web-path" aria-hidden="true"><i/><i/><i/><i/></span></article>
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

        <section className="works-section" aria-labelledby="works-title">
          <div className="container-wide works-layout">
            <div className="works-intro"><span className="section-label">THE SIGNAL, SIMPLIFIED</span><h2 className="section-title" id="works-title">How Kalillac<br/>works</h2><p className="section-copy">One conversation. A few different ways to move forward.</p></div>
            <div className="steps">
              <article className="step-card"><div className="step-top"><span className="step-number">01</span><MessageSquare size={21}/></div><h3>Ask</h3><p>Type a question or choose a starting prompt.</p></article>
              <span className="step-connector" aria-hidden="true"><ArrowRight size={17}/></span>
              <article className="step-card"><div className="step-top"><span className="step-number">02</span><Sparkles size={21}/></div><h3>Kalillac works through it</h3><p>It determines how to handle your request and returns an answer.</p></article>
              <span className="step-connector" aria-hidden="true"><ArrowRight size={17}/></span>
              <article className="step-card"><div className="step-top"><span className="step-number">03</span><BookOpen size={21}/></div><h3>Continue the conversation</h3><p>Keep working within the current temporary session.</p></article>
            </div>
          </div>
        </section>

        <section className="privacy-band" id="sessions" aria-labelledby="memory-title">
          <div className="container-wide privacy-inner">
            <div className="privacy-orbit" aria-hidden="true"><span/><span/><i/></div>
            <div><span className="section-label">A NOTE ON YOUR SESSION</span><h2 id="memory-title">A conversation for now.</h2><p>Kalillac keeps context during the active temporary session. Persistent chat history is not currently provided.</p></div>
            <Link href="/privacy" className="text-link" data-testid="link-full-privacy">How Kalillac handles data <ArrowRight size={16}/></Link>
          </div>
        </section>
      </main>
      <SiteFooter />
    </div>
  );
}

function ChatPage() {
  useEffect(() => {
    Object.assign(window, { marked, DOMPurify, renderMathInElement });
    const script = document.createElement('script');
    script.src = appJsUrl;
    script.dataset.kalillacChat = 'true';
    document.body.appendChild(script);
    return () => { script.remove(); };
  }, []);

  return <main className="chat-surface">
    <header className="chat-app-header"><Link href="/" className="brand"><img className="brand-logo" src={brandLogoUrl} alt="Kalillac AI" /></Link><span>Temporary session</span></header>
    <div className="chat-workspace">
      <div className="chat-compose-wrap">
        <form id="composer" className="native-composer">
          <label htmlFor="composer-input" className="sr-only">Message Kalillac AI</label>
          <textarea id="composer-input" rows={1} maxLength={4000} placeholder="Message Kalillac AI…" aria-label="Message Kalillac AI" />
          <button id="send-btn" type="submit" disabled aria-label="Send message" title="Send"><ArrowUp size={20} /></button>
        </form>
        <p className="chat-help">Your chat is temporary. Avoid sharing sensitive information.</p>
      </div>
      <div id="conversation" className="native-conversation">
        <div id="empty" className="native-empty">
          <p>TRY A STARTING POINT</p>
          <div className="native-hints">
            <button className="hint" type="button" data-hint-group="explain" data-prompt="Explain a concept"><Sparkles size={14}/> Explain a concept</button>
            <button className="hint" type="button" data-hint-group="code" data-prompt="Write some code"><Code2 size={14}/> Write some code</button>
            <button className="hint" type="button" data-hint-group="search" data-prompt="Search the web"><Search size={14}/> Search the web</button>
            <button className="hint" type="button" data-hint-group="write" data-prompt="Help me write"><FileText size={14}/> Help me write</button>
            <button className="hint" type="button" data-hint-group="summarize" data-prompt="Summarize a topic"><BookOpen size={14}/> Summarize a topic</button>
          </div>
        </div>
        <div id="thread" role="log" aria-label="Conversation" aria-live="polite" />
      </div>
    </div>
    <footer className="chat-app-footer"><Link href="/privacy">Privacy</Link> · <Link href="/terms">Terms</Link> · Adults 18+</footer>
  </main>;
}

type DocSectionProps = { id: string; title: string; children: ReactNode };
function DocSection({ id, title, children }: DocSectionProps) {
  return <section className="doc-section" id={id}><h2>{title}</h2>{children}</section>;
}

function DocLayout({ title, lede, children, toc }: { title: string; lede: ReactNode; children: ReactNode; toc: [string, string][] }) {
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
          <p className="doc-updated">{title === 'Privacy' ? 'Last reviewed: September 24, 2026' : 'Effective and last reviewed: September 24, 2026'}</p>
        </div>
      </main>
      <SiteFooter />
    </div>
  );
}

const privacyToc: [string, string][] = [
  ['overview', 'Information Kalillac processes'], ['not-persisted', 'What Kalillac’s application does not keep'], ['session-state', 'Temporary session state'], ['isolation', 'Session isolation'], ['browser', 'Browser storage'], ['logging', 'Logging'], ['groq', 'Model inference'], ['tavily', 'Web search'], ['path', 'How a message moves'], ['security', 'Security'], ['age', 'Age'], ['retention', 'Retention in brief'], ['choices', 'What you can do'], ['changes', 'Changes'], ['source', 'How this page is checked'], ['contact', 'Contact'],
];

function PrivacyPage() {
  return <DocLayout title="Privacy" toc={privacyToc} lede={<><p className="doc-lede">This page describes how information is handled when you use Kalillac AI at <span className="mono">kalillac.com</span>, including the chat application.</p><p className="doc-lede">Kalillac is an AI assistant for questions, research, writing, coding, learning, and answers that may use the live web. It is currently free and does not require an account. This page describes the deployment that is running now. It is not a promise that the service is anonymous, that data is never stored at any layer, or that the configuration will never change.</p></>}>
    <DocSection id="overview" title="Information Kalillac processes"><p>To answer a message, the application handles:</p><ul><li><strong>The message you type.</strong> Currently limited to 4,000 characters.</li><li><strong>Recent turns of the open conversation.</strong> The chat interface holds the current thread in page memory and sends recent turns with the request so the assistant has context. That thread is not written to a Kalillac conversation database.</li><li><strong>A session identifier.</strong> The server generates an opaque identifier and uses it as the key for a temporary session entry. The chat interface keeps that identifier in page memory. It is not an account.</li><li><strong>Facts you explicitly ask Kalillac to remember during the session.</strong> Those facts are stored as short entries in that temporary session entry.</li><li><strong>Timestamps of recent web searches in the session.</strong> Used only to enforce the current limit of five searches per ten minutes.</li></ul><p>Kalillac does not ask for your name, email address, phone number, or account details. The current service has no file upload and does not read documents from your device.</p><p>While a request is being handled, the message, recent turns, and any retrieved search text are used to produce the response. They are not added to the session entry described below, and they are not written to a Kalillac conversation database or file.</p><p>The systems that deliver the site, including the web server, operating system, host, and network providers, also process ordinary request data such as IP address, date and time, and user agent. That infrastructure handling is described under Logging. It is not Kalillac’s conversation store.</p></DocSection>
    <DocSection id="not-persisted" title="What Kalillac’s application does not keep"><p>In the current application code there is no conversation database, no conversation file, and no cache directory for chats. In that application storage, Kalillac does not:</p><ul><li>save your conversation to disk</li><li>store chat history across visits</li><li>keep a user profile or a behavioral record tied to you</li><li>retain model responses after returning them to you</li><li>keep search results in a Kalillac search-results database after using them to write an answer</li><li>use your messages to train or fine-tune a Kalillac model</li><li>sell your conversation to advertisers</li></ul><p>Those statements cover Kalillac’s own application storage. They do not mean that no copy, log, or metadata can exist anywhere else. Hosting and network providers, and the model and search providers below, may keep their own operational and security records under their own rules.</p></DocSection>
    <DocSection id="session-state" title="Temporary session state"><p>Kalillac’s custom session state is an entry in a Python dictionary in the running application process. It is held in RAM only. It is not written to a persistent conversation database.</p><p>The entry holds two things:</p><ol><li>memory facts from that session</li><li>timestamps of recent searches in that session</li></ol><p>The application keeps at most 200 session entries at once and at most 50 memory entries per session. When a new session arrives and the store is full, the oldest entry is removed. Removing an entry removes that session’s state.</p><h3>How this state ends</h3><ul><li>Refreshing the page, or opening a new tab, creates a new session identifier. The new session starts empty. It cannot read the previous entry. The previous thread is also gone from the browser, because the chat interface does not keep it in saved browser storage.</li><li>Losing access is not the same as deletion. The old server entry <strong>remains in RAM</strong> until capacity eviction or until the application process restarts.</li><li>Closing the browser does not delete the server-side entry.</li><li>When the application process restarts, the in-memory store is gone, because it existed only in that process.</li></ul></DocSection>
    <DocSection id="isolation" title="Session isolation"><p>Each session is stored under its own identifier. A request looks up only the matching entry. There is no shared conversation buffer and no cross-session lookup, so one visitor’s memory entries are not read into another visitor’s context.</p><p>Capacity is shared. With a limit of 200 sessions, heavy traffic can evict older sessions sooner. That affects when state disappears, not who can read it.</p></DocSection>
    <DocSection id="browser" title="Browser storage"><p>The chat interface keeps the session identifier and the completed turns of the open conversation in page memory only. It does not write them to localStorage, sessionStorage, IndexedDB, or cookies.</p><p>Kalillac does not intentionally include advertising or analytics scripts in the chat interface.</p><p>This does not claim that every system between your browser and the application is incapable of setting a cookie or writing a request log. It describes the chat application’s own storage.</p></DocSection>
    <DocSection id="logging" title="Logging"><h3>Debug logging</h3><p>The application has a debug mode that, when enabled, can write routing detail to the service log, including the raw text of the message being routed. One environment switch controls it. In the current production configuration that switch is <strong>off</strong>. While it is off, that function returns without writing, so message text does not reach the log through this path.</p><p>That is not a claim that nothing is ever logged.</p><h3>Operational logging</h3><p>Apart from the disabled debug path, the application writes limited operational output used to run and diagnose the service. Current examples include the startup message, provider-fallback status, search-failure notices, and, where an application error is recorded, the exception class rather than the full text of a provider exception. Fallback handling can also record which inference path succeeded or failed and related status information. That output is not a conversation archive. It is also not an empty log.</p><h3>Infrastructure</h3><p>Uvicorn/FastAPI, systemd, Nginx, the operating system, and hosting and network providers can create their own startup, request, warning, operational, and security records, which can include data such as IP address, time, and user agent. Those records sit outside Kalillac’s session store and are controlled by the systems that create them. This page is not a complete inventory of every record those systems can emit.</p></DocSection>
    <DocSection id="groq" title="Model inference"><p>Kalillac does not run its primary model on the Kalillac server. When a response needs a model, the current chain is:</p><ol><li><strong>Primary:</strong> Groq, model <span className="mono">openai/gpt-oss-120b</span></li><li><strong>First fallback,</strong> if Kalillac’s fallback logic is triggered: Cloudflare Workers AI, model <span className="mono">@cf/openai/gpt-oss-120b</span></li><li><strong>Final attempt,</strong> if that fallback does not return a usable response: Groq, model <span className="mono">openai/gpt-oss-20b</span></li></ol><p>Some requests never go to a model provider. Deterministic arithmetic, saving a session-memory fact, and a direct answer already available from temporary session state can be handled by Kalillac without Groq or Cloudflare Workers AI.</p><p>An inference request can include, as applicable:</p><ul><li>your current message</li><li>relevant recent turns</li><li>relevant temporary session-memory entries</li><li>the route instructions Kalillac assembles</li><li>retrieved web-search text, when a model writes the answer after a search</li></ul><p>Groq and Cloudflare process that information under their own terms. Kalillac does not control them and does not make promises on their behalf. Models and fallback order are current settings, not a permanent architecture.</p><h3>Groq</h3><p>Groq’s documentation is the authority for data Groq receives. Groq distinguishes customer data from usage metadata. Its current documentation says inference customer data is not retained by default, except for features that require retention in order to function, and except when inputs and outputs are temporarily logged to troubleshoot reliability problems or investigate suspected abuse. Groq states that those temporary logs are kept for up to 30 days unless the law requires longer. Customers can turn that reliability and abuse-monitoring retention off with Zero Data Retention. Groq says usage metadata is always collected, is retained, and does not contain customer inputs or outputs.</p><div className="doc-callout"><strong>Kalillac’s Groq organization has Zero Data Retention enabled.</strong> Groq documents that, with Zero Data Retention enabled, it does not retain customer data for system reliability and abuse monitoring. That does not mean Groq collects no metadata. Usage metadata is still collected and, according to Groq, does not contain customer inputs or outputs. Groq’s current documentation governs data after it reaches Groq.</div><p>Sources: <a href="https://console.groq.com/docs/your-data" target="_blank" rel="noopener noreferrer">Your Data in GroqCloud</a>, <a href="https://console.groq.com/docs/legal/services-agreement" target="_blank" rel="noopener noreferrer">Groq Services Agreement</a>, <a href="https://groq.com/privacy-policy" target="_blank" rel="noopener noreferrer">Groq Privacy Policy</a>.</p><h3>Cloudflare Workers AI</h3><p>Cloudflare is the first cross-provider fallback, not the last step. If Workers AI cannot return a usable response, Kalillac can still make the final attempt on the smaller Groq model.</p><p>Cloudflare describes Workers AI inputs, outputs, embeddings, and training data as Customer Content. Cloudflare states that it does not make that Customer Content available to other Cloudflare customers, and that it does not use it to train Workers AI models or to improve Cloudflare or third-party services unless it has explicit consent. Cloudflare also states that Customer Content may be stored if a customer uses a Cloudflare storage service together with Workers AI. Kalillac’s current use is an inference fallback, not a Kalillac conversation archive. Cloudflare’s current policies govern data after it reaches Workers AI.</p><p>Sources: <a href="https://developers.cloudflare.com/workers-ai/platform/data-usage/" target="_blank" rel="noopener noreferrer">Cloudflare Workers AI data usage</a>, <a href="https://www.cloudflare.com/privacypolicy/" target="_blank" rel="noopener noreferrer">Cloudflare Privacy Policy</a>.</p></DocSection>
    <DocSection id="tavily" title="Web search"><p>Live web search uses Tavily when Kalillac’s router decides that current, externally checked, or site-specific information is needed. Most messages do not use Tavily.</p><p>When a search runs:</p><ul><li>Kalillac builds the query from the current request. For a factual follow-up, the query can also include the prior topic or named subject needed to resolve that follow-up. The query sent to Tavily is capped at 400 characters.</li><li>Tavily returns up to four titles, links, and text snippets.</li><li>Kalillac uses those results for the response. If a model must write the answer, the results are sent through the inference chain above.</li><li>Kalillac does not put those results in a persistent Kalillac search-results database.</li><li>Each session is limited to five searches in a ten-minute window.</li></ul><p>Tavily’s public materials do not all describe the same practice.</p><ul><li>Some Tavily product and FAQ language describes zero data retention. A Tavily documentation page, summarizing what it calls a Tavily Search Privacy Notice, says the search service does not collect personal information about you, your device, or your searches, and does not share data that could be used to profile or track you.</li><li>Tavily’s privacy policy says it collects query data in order to retrieve results, may use portions of query data to improve responses to future queries unless a contract says otherwise, retains personal information as needed to provide and improve the services, and may share query data with a third-party search index when its own index cannot retrieve the requested content.</li><li>Tavily’s platform terms, last updated May 4, 2026, include a broad license to process customer input to provide and improve the services, and separate terms for Tavily AI features that refer to retention and model improvement by Tavily and its providers.</li></ul><div className="doc-callout doc-callout-amber">Kalillac does not control Tavily and does not treat any one of those statements as a complete description of the others. This page does not claim that a search query is never retained, never reused, or never passed to another index. After a query is sent, Tavily’s current terms and privacy policy govern it.</div><p>If you do not want a question sent to an outside search provider, do not ask for information that requires live or externally verified web results.</p><p>Sources: <a href="https://www.tavily.com/privacy" target="_blank" rel="noopener noreferrer">Tavily Privacy Policy</a>, <a href="https://www.tavily.com/terms" target="_blank" rel="noopener noreferrer">Tavily Platform Terms</a>, <a href="https://docs.tavily.com/faq/faq" target="_blank" rel="noopener noreferrer">Tavily documentation FAQ</a>.</p></DocSection>
    <DocSection id="path" title="How a message moves"><p>The public site and the chat application are served from the same domain. The browser sends the chat request over HTTPS. The Kalillac server receives it, and a router selects a path.</p><ul><li><strong>Handled on Kalillac.</strong> Arithmetic, saving a temporary memory fact, or answering from temporary session state, without a model provider or Tavily.</li><li><strong>Model response.</strong> The Groq, Cloudflare Workers AI, and smaller-Groq chain above.</li><li><strong>Live search.</strong> The query goes to Tavily. If a model writes the answer, the results go through that same chain. The response can include the source links.</li></ul><p>The FastAPI/Uvicorn chat application listens only on the server’s local interface at <span className="mono">127.0.0.1:8001</span>. It is reached through the web server. It is not published directly on that port.</p></DocSection>
    <DocSection id="security" title="Security"><p>Chat requests are sent over HTTPS. The chat process is bound to the local interface and reached through the web server. Production debug logging of message text is switched off.</p><p>Those controls are not a promise that the service is completely secure, or that no person or provider could ever access a request, a log, or provider-side data. Do not send passwords, API keys, credentials, payment-card numbers, or other highly sensitive secrets. Ordinary use of Kalillac does not require them.</p></DocSection>
    <DocSection id="age" title="Age"><p>Kalillac is for adults. You must be at least 18. The service is not directed to children. Kalillac does not knowingly seek information from anyone under 18. If it becomes aware that someone under 18 is using the service, it may block that access.</p><p>Use of the service is a representation that you are at least 18. These terms do not describe a separate age-verification process.</p></DocSection>
    <DocSection id="retention" title="Retention in brief"><table><thead><tr><th>Information</th><th>Where it lives now</th><th>When it ends</th></tr></thead><tbody><tr><td>Open conversation in the chat interface</td><td>Browser page memory</td><td>Refresh, a new tab, or leaving the page</td></tr><tr><td>Session identifier</td><td>Page memory, as the key for the server entry</td><td>Same as that page session</td></tr><tr><td>Memory facts and search timestamps</td><td>Server RAM, up to 200 sessions and 50 facts</td><td>Capacity eviction or process restart. Closing the browser does not delete the entry</td></tr><tr><td>Conversation on Kalillac’s servers</td><td>Not written to a database or file</td><td>Not kept as chat history. The request exists while it is being processed</td></tr><tr><td>Groq and Cloudflare</td><td>Their systems</td><td>Their policies. Kalillac’s Groq organization has Zero Data Retention enabled</td></tr><tr><td>Tavily</td><td>Tavily’s systems</td><td>Tavily’s terms and privacy policy, which are not identical to its zero-retention marketing language</td></tr><tr><td>Host and network logs</td><td>Those providers</td><td>Their retention, which Kalillac does not control</td></tr></tbody></table><p>There is no Kalillac account and no Kalillac conversation archive to export or delete. That is not a claim that no record exists at a host, network, or provider, or that Kalillac can find and erase every infrastructure log on request.</p></DocSection>
    <DocSection id="choices" title="What you can do"><p>You choose what to type. You can avoid live-search questions if you do not want a query sent to Tavily, and you can avoid putting secrets in the chat. The service does not ask for an email address and has no account settings for history or marketing.</p></DocSection>
    <DocSection id="changes" title="Changes"><p>Kalillac is under active development. Models, providers, limits, the debug switch, session limits, and search limits are operational settings, not permanent commitments. Later features, including accounts or paid options, are not the current service.</p><p>If message handling changes in a way that matters, this page will be updated to describe what is actually deployed, rather than left describing the old behavior.</p></DocSection>
    <DocSection id="source" title="How this page is checked"><p>Statements about Kalillac’s own application are checked against the application source and the production configuration. Statements about Groq, Cloudflare, and Tavily follow those companies’ published materials, which can change. This page is an operator description. It is not an independent privacy or security audit. Kalillac can describe privacy-relevant architecture without publishing proprietary routing and product logic.</p></DocSection>
    <DocSection id="contact" title="Contact"><p>Questions and corrections can go to Kalillac AI through <a href="https://www.linkedin.com/in/kalillacai" target="_blank" rel="noopener noreferrer">LinkedIn</a>.</p><p>Questions about what Groq, Cloudflare Workers AI, or Tavily do with data they receive should be answered from those companies’ policies, not from this page.</p></DocSection>
  </DocLayout>;
}

const termsToc: [string, string][] = [
  ['service', 'The service'], ['age', 'Who may use it'], ['support', 'No account and no paid plan today'], ['ai-limitations', 'AI responses'], ['responsibilities', 'Your responsibilities'], ['user-actions', 'Your searches and later actions'], ['boundaries', 'Acceptable use'], ['privacy', 'Privacy and other companies'], ['content', 'Prompts and output'], ['intellectual-property', 'Kalillac intellectual property'], ['availability', 'Availability and access'], ['warranties', 'Disclaimers and liability'], ['law', 'Governing law'], ['changes', 'Changes to these Terms'], ['contact', 'Contact'],
];

function TermsPage() {
  return <DocLayout title="Terms of Use" toc={termsToc} lede={<><p className="doc-lede">These Terms govern use of Kalillac AI, the service at <span className="mono">kalillac.com</span>. Kalillac is an AI assistant for questions, research, writing, coding, learning, and answers that may use the live web.</p><p className="doc-lede">The service is currently free and does not require an account. It is only for adults age 18 and older. By using Kalillac, you agree to these Terms. If you do not agree, do not use the service.</p></>}>
    <DocSection id="service" title="The service"><p>Kalillac routes requests in more than one way. Some are handled by Kalillac. Some require a model, currently through Groq, with Cloudflare Workers AI as the first fallback and a smaller Groq model as the last inference attempt. Some use Tavily when the answer needs the live web. Current data handling is described on the <Link href="/privacy">Privacy page</Link>.</p><p>The service is under active development. Features, models, providers, limits, routing, and the interface may change. No particular model, provider, search capability, limit, or screen is guaranteed to remain available.</p></DocSection>
    <DocSection id="age" title="Who may use it"><p>You must be at least <strong>18 years old</strong>. Kalillac is not directed to children or minors. If you are under 18, do not use it.</p><p>By using Kalillac, you represent that you are at least 18. Kalillac may restrict access if it becomes aware that someone under 18 is using the service. These Terms rely on your representation. They do not describe a separate age-verification process.</p></DocSection>
    <DocSection id="support" title="No account and no paid plan today"><p>Kalillac does not currently require an account, login, or subscription. These Terms are posted on the site because there is no registration step.</p><p>A voluntary <a href="https://buymeacoffee.com/kalillactv7" target="_blank" rel="noopener noreferrer">Buy Me a Coffee</a> link is available if you want to support development and infrastructure. That payment is not a subscription. By itself, it does not buy priority access, guaranteed uptime, ownership, extra features, or a permanent right to use the service.</p><p>Kalillac may later offer paid features or optional accounts. If it does, those terms will be stated separately. Nothing here means a paid plan exists now.</p></DocSection>
    <DocSection id="ai-limitations" title="AI responses"><p>Responses can be inaccurate, incomplete, outdated, or misleading, including when they sound confident. Kalillac may misunderstand context, reason incorrectly, or cite material that still needs to be checked.</p><p>You decide whether to rely on a response. Verify information before you act on it, especially where a mistake could affect health, legal rights, money, safety, work, education, or another important decision.</p><p>Kalillac can discuss legal, medical, financial, technical, and other professional subjects. The responses are informational. Use of the service does not create an attorney-client, doctor-patient, fiduciary, therapist-client, or other professional relationship with Kalillac AI.</p><p>Where professional judgment matters, treat Kalillac as one source of information, not as a substitute for a qualified professional who can evaluate your situation.</p></DocSection>
    <DocSection id="responsibilities" title="Your responsibilities"><p>You control what you ask and what you do with the answer. You are responsible for your prompts, searches, and instructions, and for decisions, downloads, purchases, installations, communications, transactions, and other actions you take as a result of using the service.</p><p>You are responsible for:</p><ul><li>the content you submit</li><li>judging whether a response is accurate, lawful, safe, and suitable before you use it</li><li>how you use, share, publish, run, install, or otherwise rely on generated material</li><li>complying with laws, contracts, licenses, and other duties that apply to you</li><li>respecting other people’s rights, privacy, property, accounts, credentials, and systems</li><li>checking information when an error could have real consequences</li></ul><p>Do not submit passwords, credentials, API keys, financial account numbers, or other highly sensitive information the request does not require.</p></DocSection>
    <DocSection id="user-actions" title="Your searches and later actions"><p>Kalillac provides information. It does not control what you search, ask, investigate, open, download, install, buy, publish, execute, send, or do after a response.</p><p>You are responsible for whether you act, and for the consequences. That includes consequences involving other websites, software, services, accounts, transactions, devices, files, people, or systems you choose to interact with.</p><p>Kalillac AI does not authorize, direct, endorse, or take responsibility for your independent conduct merely because the service discussed a subject, returned information, linked a source, or generated related text.</p><p>To the fullest extent permitted by law, you assume the risk of actions you independently choose to take based on, or after, using Kalillac. This section does not remove a responsibility the law does not allow to be removed.</p><div className="doc-callout doc-callout-amber">A response is information. It is not permission to interfere with another person’s rights, property, privacy, accounts, credentials, or computer systems.</div></DocSection>
    <DocSection id="boundaries" title="Acceptable use"><p>Kalillac is meant to engage with difficult, controversial, and technically sensitive subjects. A subject is not forbidden merely because it is uncomfortable, or because someone else’s terms of service, game rules, or competition rules would restrict it. Those outside rules do not automatically become Kalillac’s rules.</p><p>Kalillac may refuse or narrow a request when the output would materially help cause serious real-world harm to a person, or to that person’s property, finances, privacy, credentials, or computer systems, or when providing it would conflict with law or with a binding requirement on the service.</p><p>If only part of a request has that problem, the intended behavior is to limit that part and continue with the rest, rather than refuse the whole conversation. That is the intended design. It is not a warranty that every answer will be divided perfectly.</p><p>You may not use Kalillac to intentionally facilitate serious harm, including fraud, credential theft, exploitation of children, destructive compromise of systems you do not control and are not authorized to test, or planning real-world violence.</p></DocSection>
    <DocSection id="privacy" title="Privacy and other companies"><p>The <Link href="/privacy">Privacy page</Link> describes messages, session state, logging, and providers. Groq, Cloudflare Workers AI, Tavily, hosting providers, network providers, and other infrastructure providers operate under their own terms. Kalillac does not control them and does not promise anything on their behalf.</p></DocSection>
    <DocSection id="content" title="Prompts and output"><p>As between you and Kalillac, Kalillac does not claim ownership of the text you submit. You give Kalillac the limited permission required to process that content so it can respond and operate the service as the Privacy page describes.</p><p>Kalillac does not promise that output is unique, copyrightable, accurate, non-infringing, or suitable for commercial use. Other people may receive similar or identical output. Rights in AI-generated material depend on the law, the source material, and third-party terms.</p><p>You are responsible for having the rights you need before you publish, sell, distribute, or otherwise rely on generated material.</p></DocSection>
    <DocSection id="intellectual-property" title="Kalillac intellectual property"><p>Unless stated otherwise, rights to the Kalillac name, branding, logo, original site design, original written content, and proprietary application code are reserved or used with permission.</p><p>Open-source libraries, third-party models, third-party services, trademarks, and other third-party materials stay under their own licenses and owners.</p><p>These Terms do not transfer Kalillac’s branding, private source code, credentials, system configuration, or other proprietary materials.</p></DocSection>
    <DocSection id="availability" title="Availability and access"><p>Kalillac may be unavailable, slow, rate-limited, changed, suspended, or discontinued. Advance notice is not guaranteed.</p><p>Kalillac may limit or block access when reasonably necessary to protect the service, respond to abuse, meet a legal duty, keep the system stable, or enforce these Terms. There is no user account to close. The action is a limit on access to the service.</p></DocSection>
    <DocSection id="warranties" title="Disclaimers and liability"><p>To the fullest extent permitted by law, Kalillac is provided “as is” and “as available,” without warranties that it will be uninterrupted, error-free, secure, accurate, current, complete, or fit for a particular purpose.</p><p>You are responsible for evaluating information before you rely on it, and for what you independently choose to do. Kalillac AI is not responsible for losses, penalties, bans, account actions, damages, injuries, disputes, security incidents, legal or financial consequences, data loss, or other outcomes caused by your own conduct, or by your decision to follow, run, publish, install, buy, transmit, or otherwise act on generated information, except where the law imposes a responsibility that cannot be disclaimed.</p><p>To the fullest extent permitted by law, Kalillac AI is not liable for indirect, incidental, special, consequential, exemplary, or similar losses arising from use of the service, inability to use it, reliance on it, interactions with third-party sites, services, software, or content, or actions taken independently after using Kalillac.</p><p>These Terms do not exclude or limit liability that cannot lawfully be excluded or limited. Mandatory consumer rights still apply where they apply.</p></DocSection>
    <DocSection id="law" title="Governing law"><p>These Terms are governed by the laws of the State of Indiana, without regard to conflict-of-law rules, except where federal law or a mandatory consumer protection says otherwise.</p><p>These Terms do not waive a right that applicable law does not allow you to waive.</p></DocSection>
    <DocSection id="changes" title="Changes to these Terms"><p>Kalillac may update these Terms. The new version will be posted here with a revised date.</p><p>A material change should be stated openly. A material change in privacy or data handling should also be reflected on the Privacy page, so the public description matches the service that is running.</p><p>Continued use after updated Terms are posted means those Terms apply to later use, subject to applicable law.</p></DocSection>
    <DocSection id="contact" title="Contact"><p>Questions about these Terms can be sent to Kalillac AI through <a href="https://www.linkedin.com/in/kalillacai" target="_blank" rel="noopener noreferrer">LinkedIn</a>.</p></DocSection>
  </DocLayout>;
}

function Router() {
  return <ErrorBoundary resetKey={window.location.pathname}><Switch><Route path="/" component={HomePage} /><Route path="/app" component={ChatPage} /><Route path="/privacy" component={PrivacyPage} /><Route path="/terms" component={TermsPage} /><Route component={NotFound} /></Switch></ErrorBoundary>;
}

function App() {
  return <QueryClientProvider client={queryClient}><TooltipProvider><WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, '')}><Router /></WouterRouter><Toaster /></TooltipProvider></QueryClientProvider>;
}

export default App;