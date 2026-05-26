(() => {
  "use strict";

  const API_URL = window.AHEAD_API_URL || "/api/crucial-news";
  const SCRIPT_URL = window.AHEAD_SCRIPT_URL || "/api/podcast-script";
  const AUDIO_URL = window.AHEAD_AUDIO_URL || "/api/podcast-audio";
  const LOCAL_TTS_URL = window.AHEAD_LOCAL_TTS_URL || "/api/local-tts";
  const MARK_READ_URL = window.AHEAD_MARK_READ_URL || "/api/mark-read";
  const VOICE_STATUS_URL = window.AHEAD_VOICE_STATUS_URL || "/api/voice-status";
  const VOICE_KEY_URL = window.AHEAD_VOICE_KEY_URL || "/api/voice-key";

  const state = {
    articles: [],
    status: "idle",
    message: "",
    progressStep: 0,
    window: null,
    diagnostics: null,
    generatedAt: null,
    candidatePoolCount: 0,
    seenArticleIds: new Set(),
    podcastScript: { en: "" },
    podcastMode: "premium",
    speaking: false,
    audio: null,
    freeVoice: null,
    freeRate: 0.92,
    freeChunks: [],
    freeChunkIndex: 0,
    freePaused: false,
    deviceSpeaking: false,
    voiceStatus: null,
    voiceBusy: false,
    voiceError: "",
  };

  const steps = [
    "Pulling trusted sources",
    "Filtering low-value news",
    "Scoring macro impact",
    "Preparing briefing",
  ];

  const css = `
    :root {
      --ink: #101828;
      --muted: #667085;
      --line: #d8dee8;
      --panel: #ffffff;
      --wash: #f3f6fa;
      --nav: #101820;
      --accent: #0f766e;
      --accent-2: #b45309;
      --gold: #c58b18;
      --danger: #991b1b;
      --soft: #edf5f4;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background: var(--wash);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    button { font: inherit; }

    .ahead-shell {
      display: grid;
      grid-template-columns: 260px minmax(0, 1fr);
      min-height: 100vh;
    }

    .ahead-sidebar {
      background: var(--nav);
      color: #e5edf5;
      padding: 26px 18px;
      display: flex;
      flex-direction: column;
      gap: 26px;
    }

    .ahead-brand { display: grid; gap: 6px; }
    .ahead-brand strong { font-size: 25px; letter-spacing: .08em; }
    .ahead-brand span { color: #9fb0c3; font-size: 12px; line-height: 1.55; }

    .ahead-nav { display: grid; gap: 8px; }
    .ahead-nav a {
      color: #d9e4ee;
      text-decoration: none;
      font-size: 14px;
      font-weight: 900;
      padding: 12px 13px;
      border-radius: 8px;
      background: rgba(255,255,255,.08);
      border: 1px solid rgba(255,255,255,.08);
    }
    .ahead-nav a.active { background: #fff; color: #111827; }

    .sidebar-note {
      margin-top: auto;
      border-top: 1px solid rgba(255,255,255,.12);
      padding-top: 18px;
      color: #aebccd;
      font-size: 12px;
      line-height: 1.55;
    }

    .ahead-main {
      padding: 28px;
      display: grid;
      gap: 20px;
      align-content: start;
    }

    .ahead-header {
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: end;
      border-bottom: 1px solid var(--line);
      padding-bottom: 18px;
    }

    .ahead-kicker {
      margin: 0 0 8px;
      color: var(--accent);
      font-size: 12px;
      font-weight: 900;
      letter-spacing: .13em;
      text-transform: uppercase;
    }

    h1 {
      margin: 0;
      font-size: clamp(30px, 4vw, 48px);
      line-height: 1;
      letter-spacing: 0;
    }

    .ahead-subtitle {
      margin: 12px 0 0;
      color: var(--muted);
      font-size: 15px;
      max-width: 790px;
      line-height: 1.65;
    }

    .window-pill {
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
      color: #334155;
      font-size: 13px;
      font-weight: 900;
      min-width: 244px;
      text-align: right;
      box-shadow: 0 8px 24px rgba(15, 23, 42, .06);
    }

    .hero-card, .status-card, .podcast-panel {
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 12px 30px rgba(15, 23, 42, .055);
    }

    .hero-card {
      min-height: 340px;
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(260px, .8fr);
      gap: 22px;
      align-items: center;
      padding: 32px;
    }

    .hero-card h2 {
      margin: 0;
      font-size: clamp(24px, 3vw, 36px);
      line-height: 1.12;
      letter-spacing: 0;
    }

    .hero-card p {
      margin: 14px 0 0;
      color: #475467;
      line-height: 1.65;
      max-width: 680px;
    }

    .primary-button, .secondary-button, .voice-button {
      border: 1px solid transparent;
      border-radius: 8px;
      min-height: 42px;
      padding: 0 16px;
      font-weight: 900;
      cursor: pointer;
      transition: transform .16s ease, box-shadow .16s ease, background .16s ease, border-color .16s ease;
    }

    .primary-button {
      background: var(--accent);
      color: #fff;
      margin-top: 22px;
      min-width: 210px;
    }

    .primary-button:hover { background: #0b5f59; }
    .primary-button:active, .secondary-button:active, .voice-button:active { transform: translateY(2px) scale(.985); }
    .primary-button:disabled, .secondary-button:disabled, .voice-button:disabled {
      opacity: .5;
      cursor: not-allowed;
    }

    .secondary-button, .voice-button {
      background: #fff;
      border-color: #b8c4d2;
      color: #18212f;
    }

    .voice-button.active {
      background: var(--soft);
      border-color: var(--accent);
      color: var(--accent);
    }

    .touch-button {
      min-height: 58px;
      min-width: 210px;
      border-radius: 14px;
      border: 1px solid #0b5f59;
      color: #fff;
      background: linear-gradient(135deg, #0f766e, #0b5f59);
      box-shadow: 0 14px 30px rgba(15, 118, 110, .28), inset 0 1px 0 rgba(255,255,255,.22);
      position: relative;
      overflow: hidden;
    }

    .touch-button::after {
      content: "";
      position: absolute;
      inset: 0;
      background: radial-gradient(circle at center, rgba(255,255,255,.34), transparent 45%);
      opacity: 0;
      transform: scale(.4);
      transition: opacity .2s ease, transform .2s ease;
    }

    .touch-button:hover {
      transform: translateY(-2px);
      box-shadow: 0 18px 38px rgba(15, 118, 110, .34), inset 0 1px 0 rgba(255,255,255,.28);
    }

    .touch-button:active {
      transform: translateY(2px) scale(.975);
      box-shadow: 0 8px 18px rgba(15, 118, 110, .24), inset 0 3px 8px rgba(0,0,0,.18);
    }

    .touch-button:active::after {
      opacity: 1;
      transform: scale(1.4);
    }

    .touch-button.loading {
      pointer-events: none;
      background: linear-gradient(135deg, #64748b, #334155);
      border-color: #334155;
    }

    .touch-button.loading span::after {
      content: "";
      display: inline-block;
      width: 12px;
      height: 12px;
      border: 2px solid rgba(255,255,255,.5);
      border-top-color: #fff;
      border-radius: 999px;
      margin-left: 10px;
      vertical-align: -2px;
      animation: spin .8s linear infinite;
    }

    .signal-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }

    .signal {
      background: #f7fafc;
      border: 1px solid #e1e7ef;
      border-radius: 8px;
      padding: 14px;
    }

    .signal strong {
      display: block;
      color: #1d2939;
      font-size: 14px;
    }

    .signal span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
      margin-top: 5px;
    }

    .status-card {
      min-height: 340px;
      display: grid;
      place-items: center;
      text-align: center;
      padding: 32px;
    }

    .loader {
      width: 54px;
      height: 54px;
      border-radius: 50%;
      border: 5px solid #dbe4ee;
      border-top-color: var(--accent);
      animation: spin .85s linear infinite;
      margin: 0 auto 20px;
    }

    @keyframes spin { to { transform: rotate(360deg); } }

    .status-card h2 { margin: 0 0 8px; font-size: 22px; }
    .status-card p { margin: 0; color: var(--muted); line-height: 1.55; }

    .step-list {
      display: grid;
      gap: 8px;
      margin-top: 22px;
      min-width: min(420px, 80vw);
    }

    .step {
      display: flex;
      align-items: center;
      gap: 10px;
      text-align: left;
      color: #667085;
      background: #f8fafc;
      border: 1px solid #e2e8f0;
      border-radius: 8px;
      padding: 10px 12px;
      font-weight: 800;
      font-size: 13px;
    }

    .step-dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: #cbd5e1;
      flex: 0 0 auto;
    }

    .step.active {
      color: #0f5f58;
      border-color: #a6d2cc;
      background: #eef8f6;
    }

    .step.active .step-dot { background: var(--accent); }

    .toolbar {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
    }

    .countline { color: #475569; font-size: 13px; font-weight: 900; }

    .podcast-panel {
      padding: 18px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      align-items: center;
    }

    .podcast-panel h2 {
      margin: 0;
      font-size: 20px;
    }

    .podcast-panel p {
      margin: 7px 0 0;
      color: var(--muted);
      line-height: 1.55;
      font-size: 13px;
    }

    .voice-controls { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 9px; }

    .voice-status {
      grid-column: 1 / -1;
      color: #475467;
      font-size: 12px;
      font-weight: 800;
      border-top: 1px solid #edf1f6;
      padding-top: 12px;
    }

    .news-list { display: grid; gap: 14px; }

    .news-card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      display: grid;
      grid-template-columns: 82px minmax(0, 1fr);
      gap: 18px;
      box-shadow: 0 10px 28px rgba(15, 23, 42, .055);
    }

    .rank-number {
      height: 72px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      background: #111827;
      color: #fff;
      border: 1px solid #111827;
    }

    .rank-number strong { display: block; font-size: 30px; line-height: 1; text-align: center; }
    .rank-number span {
      display: block;
      margin-top: 5px;
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: .08em;
      font-weight: 900;
    }

    .news-meta { color: var(--muted); font-size: 13px; font-weight: 900; margin-bottom: 7px; }
    .news-card h2 { margin: 0; font-size: 21px; line-height: 1.25; letter-spacing: 0; }
    .news-card h2 a { color: var(--ink); text-decoration: none; }
    .news-card h2 a:hover { color: var(--accent); }

    .story-template {
      display: grid;
      gap: 10px;
      margin-top: 13px;
    }

    .template-section {
      padding: 13px 14px;
      background: #f7fafc;
      border-left: 3px solid var(--accent-2);
      color: #1f2937;
      line-height: 1.65;
      font-size: 14px;
    }

    .template-section strong {
      display: block;
      color: #111827;
      font-size: 12px;
      letter-spacing: .08em;
      margin-bottom: 5px;
      text-transform: uppercase;
    }

    .tag-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 13px; }
    .tag {
      border: 1px solid #cbd5e1;
      color: #475569;
      background: #fff;
      border-radius: 999px;
      padding: 5px 9px;
      font-size: 12px;
      font-weight: 900;
    }

    @media (max-width: 900px) {
      .ahead-shell, .hero-card { grid-template-columns: 1fr; }
      .ahead-sidebar { position: static; gap: 16px; }
      .ahead-header, .toolbar, .podcast-panel { display: grid; align-items: start; }
      .window-pill { text-align: left; min-width: 0; }
      .news-card { grid-template-columns: 1fr; }
      .rank-number { width: 82px; }
      .ahead-main { padding: 20px; }
      .voice-controls { justify-content: flex-start; }
    }
  `;

  const monthDayYear = new Intl.DateTimeFormat("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
  });

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function renderShell() {
    document.title = "AHEAD A.I. | Top 10 Crucial News";
    document.body.innerHTML = `
      <style>${css}</style>
      <div class="ahead-shell">
        <aside class="ahead-sidebar">
          <div class="ahead-brand">
            <strong>AHEAD A.I.</strong>
            <span>Daily intelligence for professionals tracking business, automation, finance, and the new era of AI.</span>
          </div>
          <nav class="ahead-nav" aria-label="Primary navigation">
            <a class="active" href="#crucial-news">Crucial News</a>
          </nav>
          <div class="sidebar-note">
            Generate a ranked briefing from trusted sources. Promotional and low-value stories are filtered before scoring.
          </div>
        </aside>
        <main class="ahead-main">
          <header class="ahead-header">
            <div>
              <p class="ahead-kicker">Business Intelligence Briefing</p>
              <h1>Top 10 Crucial News</h1>
              <p class="ahead-subtitle">
                Pull the most critical business, finance, automation, AI, jobs, and India-impact stories from the last 48 hours. AHEAD A.I. scans trusted sources, rejects noise, and prepares a plain-language briefing.
              </p>
            </div>
            <div class="window-pill" id="windowPill">Waiting to generate</div>
          </header>
          <section id="content"></section>
        </main>
      </div>
    `;
  }

  function setWindowLabel(payload) {
    const pill = document.getElementById("windowPill");
    if (!pill) return;
    if (payload?.window?.start && payload?.window?.end) {
      const start = monthDayYear.format(new Date(payload.window.start));
      const end = monthDayYear.format(new Date(payload.window.end));
      pill.textContent = `${start} to ${end}`;
      return;
    }
    pill.textContent = "Last 48 hours";
  }

  function renderIdle() {
    document.getElementById("content").innerHTML = `
      <div class="hero-card">
        <div>
          <h2>Generate only the 10 stories that can actually change decisions.</h2>
          <p>
            This is not a feed dump. The engine searches trusted daily sources, blocks weak stories, scores each item out of 5, and ranks what matters for work, markets, investment, automation, AI, and India.
          </p>
          <button class="primary-button" id="generateButton">Generate Top 10</button>
        </div>
        <div class="signal-grid" aria-label="Ranking signals">
          <div class="signal"><strong>Business impact</strong><span>Investments, financing, markets, restructuring, supply chains.</span></div>
          <div class="signal"><strong>Automation shift</strong><span>AI model updates, large-company AI, chips, data centers, useful work output.</span></div>
          <div class="signal"><strong>Workforce reality</strong><span>Hiring, layoffs, skills, workplace redesign, job transformation.</span></div>
          <div class="signal"><strong>Policy and trust</strong><span>Reliable sources, regulation, central banks, verified institutions.</span></div>
        </div>
      </div>
    `;
    document.getElementById("generateButton").addEventListener("click", loadNews);
  }

  function renderLoading() {
    const stepHtml = steps.map((step, index) => `
      <div class="step ${index <= state.progressStep ? "active" : ""}">
        <span class="step-dot"></span>
        <span>${escapeHtml(step)}</span>
      </div>
    `).join("");
    document.getElementById("content").innerHTML = `
      <div class="status-card" role="status" aria-live="polite">
        <div>
          <div class="loader" aria-hidden="true"></div>
          <h2>Analyzing the full macro bucket</h2>
          <p>${escapeHtml(state.message)}</p>
          <div class="step-list">${stepHtml}</div>
        </div>
      </div>
    `;
  }

  function renderError() {
    document.getElementById("content").innerHTML = `
      <div class="status-card" role="alert">
        <div>
          <h2>News engine unavailable</h2>
          <p>${escapeHtml(state.message)}</p>
          <p style="margin-top:14px"><button class="primary-button" id="retryButton">Generate Again</button></p>
        </div>
      </div>
    `;
    document.getElementById("retryButton").addEventListener("click", loadNews);
  }

  function podcastPanel() {
    const hasArticles = state.articles.length > 0;
    const status = state.deviceSpeaking
      ? `Reading full briefing locally${state.freeVoice ? ` with ${state.freeVoice.name}` : ""}.`
      : state.voiceError
      ? state.voiceError
      : state.podcastMode === "openai"
      ? "Using premium generated audio. Voice is AI-generated."
      : state.voiceStatus?.kokoro_configured
        ? "Kokoro local neural voice is ready. No API key needed."
      : state.voiceStatus?.gemini_configured
        ? "Kokoro is unavailable here. Gemini premium voice is configured."
        : state.voiceStatus?.openai_configured
          ? "Kokoro is unavailable here. OpenAI premium voice is configured."
          : "Kokoro will be tried first; device voice is the emergency fallback.";
    const buttonLabel = state.voiceBusy
      ? "Preparing audio"
      : state.deviceSpeaking && state.freePaused
        ? "Resume Audio"
      : state.deviceSpeaking
        ? "Pause Audio"
      : state.audio && !state.audio.paused
        ? "Pause Audio"
        : state.audio && state.audio.paused
          ? "Resume Audio"
          : "Play Briefing";
    return `
      <section class="podcast-panel" aria-label="Podcast playback">
        <div>
          <h2>Podcast Briefing</h2>
          <p>Tap once to read the full top-10 briefing with meaning, what happened, why it matters, and Hinglish summary.</p>
        </div>
        <div class="voice-controls">
          <button class="voice-button touch-button ${state.voiceBusy ? "loading" : ""}" id="audioToggle" ${hasArticles ? "" : "disabled"}><span>${escapeHtml(buttonLabel)}</span></button>
        </div>
        <div class="voice-status" id="voiceStatus">${escapeHtml(status)}</div>
      </section>
    `;
  }

  function buildFullReaderScript() {
    const lines = [
      "AHEAD A.I. full news reader.",
      "I will read each story with meaning, what happened, why it matters, and the Hinglish summary.",
    ];
    state.articles.slice(0, 10).forEach((article, index) => {
      lines.push(`Story ${index + 1}. ${article.title || "Important story"}.`);
      lines.push(`Source: ${article.source || "trusted source"}.`);
      lines.push(`Meaning. ${article.context || article.background || ""}`);
      lines.push(`What happened. ${article.what_happened || article.short_explanation || article.summary || ""}`);
      lines.push(`Why it matters. ${article.why_it_matters || ""}`);
      lines.push(`Hinglish summary. ${article.hinglish_summary || ""}`);
    });
    lines.push("That is the full AHEAD A.I. news briefing.");
    return lines.join(" ").replace(/\s+/g, " ").trim();
  }

  function renderArticles() {
    const cards = state.articles.map((article, index) => {
      const tags = (article.tags || []).slice(0, 4).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
      return `
        <article class="news-card" id="story-${index + 1}">
          <div class="rank-number" aria-label="Story rank ${index + 1}">
            <div><strong>${index + 1}</strong><span>Rank</span></div>
          </div>
          <div>
            <div class="news-meta">${escapeHtml(article.metadata)} - <a href="${escapeHtml(article.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(article.source)}</a></div>
            <h2><a href="${escapeHtml(article.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(article.title)}</a></h2>
            <div class="story-template">
              <div class="template-section"><strong>Meaning</strong>${escapeHtml(article.context || article.background || "This means the story may affect business, money, jobs, or technology decisions.")}</div>
              <div class="template-section"><strong>What Happened</strong>${escapeHtml(article.what_happened || article.short_explanation || article.summary)}</div>
              <div class="template-section"><strong>Why It Matters</strong>${escapeHtml(article.why_it_matters || article.full_context)}</div>
              <div class="template-section"><strong>Hinglish Summary</strong>${escapeHtml(article.hinglish_summary || "Yeh news business aur future of work ke liye important signal hai.")}</div>
            </div>
            <div class="tag-row">${tags}</div>
          </div>
        </article>
      `;
    }).join("");

    const collected = state.diagnostics?.collected ? `${state.diagnostics.collected} collected` : "trusted sources scanned";
    const pool = state.candidatePoolCount || state.diagnostics?.candidate_pool_count || 0;
    const generated = state.generatedAt ? ` - live pull ${monthDayYear.format(new Date(state.generatedAt))}` : "";
    document.getElementById("content").innerHTML = `
      ${podcastPanel()}
      <div class="toolbar">
        <div class="countline">Top ${state.articles.length} crucial stories - ${escapeHtml(collected)} - ${pool} strong candidates${escapeHtml(generated)}</div>
        <button class="secondary-button" id="refreshButton">Regenerate Top 10</button>
      </div>
      <section class="news-list" id="crucial-news">${cards}</section>
    `;
    document.getElementById("refreshButton").addEventListener("click", loadNews);
    document.getElementById("audioToggle").addEventListener("click", toggleAudio);
  }

  function render() {
    if (state.status === "idle") renderIdle();
    if (state.status === "loading") renderLoading();
    if (state.status === "error") renderError();
    if (state.status === "ready") renderArticles();
  }

  function startProgressTicker() {
    state.progressStep = 0;
    return window.setInterval(() => {
      state.progressStep = Math.min(steps.length - 1, state.progressStep + 1);
      if (state.status === "loading") renderLoading();
    }, 1700);
  }

  async function loadNews() {
    stopPodcast();
      state.status = "loading";
      state.voiceError = "";
    state.message = "Scanning reliable sources and ranking only business-critical automation, finance, and AI developments.";
    state.articles = [];
    state.podcastScript = { en: "" };
    state.generatedAt = null;
    state.candidatePoolCount = 0;
    setWindowLabel(null);
    render();
    const ticker = startProgressTicker();

    try {
      const requestUrl = new URL(API_URL, window.location.origin);
      requestUrl.searchParams.set("refresh", `${Date.now()}-${Math.random().toString(16).slice(2)}`);
      if (state.seenArticleIds.size) {
        requestUrl.searchParams.set("seen", Array.from(state.seenArticleIds).slice(-80).join(","));
      }
      const response = await fetch(requestUrl.toString(), {
        headers: { "Accept": "application/json" },
        cache: "no-store",
      });

      if (!response.ok) throw new Error(`The backend returned ${response.status}.`);

      const payload = await response.json();
      state.window = payload.window || null;
      state.diagnostics = payload.diagnostics || null;
      state.generatedAt = payload.generated_at || null;
      state.candidatePoolCount = Number(payload.candidate_pool_count || payload.diagnostics?.candidate_pool_count || 0);
      state.articles = Array.isArray(payload.articles) ? payload.articles.slice(0, 10) : [];

      if (state.articles.length !== 10) {
        throw new Error(`The engine returned ${state.articles.length} qualified stories. It needs 10 to build the briefing.`);
      }

      setWindowLabel(payload);
      state.articles.forEach((article) => {
        if (article.id) state.seenArticleIds.add(article.id);
      });
      state.status = "ready";
      render();
    } catch (error) {
      state.status = "error";
      state.message = `${error.message} Confirm the backend is running, then try Generate Top 10 again.`;
      render();
    } finally {
      window.clearInterval(ticker);
    }
  }

  async function getPodcastScript(language) {
    if (state.podcastScript[language]) return state.podcastScript[language];
    const response = await fetch(SCRIPT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ language, articles: state.articles }),
    });
    if (!response.ok) throw new Error(`Podcast script failed with ${response.status}.`);
    const payload = await response.json();
    state.podcastScript[language] = payload.script || "";
    return state.podcastScript[language];
  }

  function updateVoiceStatus(message) {
    const el = document.getElementById("voiceStatus");
    if (el) el.textContent = message;
  }

  async function refreshVoiceStatus() {
    try {
      const response = await fetch(VOICE_STATUS_URL, { headers: { "Accept": "application/json" }, cache: "no-store" });
      if (response.ok) state.voiceStatus = await response.json();
    } catch {
      state.voiceStatus = null;
    }
  }

  function chunkForSpeech(text) {
    const sentences = String(text || "").match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [];
    const chunks = [];
    let current = "";
    sentences.forEach((sentence) => {
      const next = `${current} ${sentence}`.trim();
      if (next.length > 420 && current) {
        chunks.push(current);
        current = sentence.trim();
      } else {
        current = next;
      }
    });
    if (current) chunks.push(current);
    return chunks.length ? chunks : [String(text || "")];
  }

  function pickDeviceVoice() {
    const voices = window.speechSynthesis?.getVoices?.() || [];
    const preferred = [
      /natural/i,
      /online/i,
      /microsoft aria/i,
      /microsoft andrew/i,
      /microsoft ava/i,
      /microsoft emma/i,
      /microsoft ravi/i,
      /\bravi\b/i,
      /english \(india\)/i,
      /en-in/i,
      /microsoft david/i,
      /google uk english/i,
      /google us english/i,
    ];
    for (const pattern of preferred) {
      const found = voices.find((voice) => pattern.test(`${voice.name} ${voice.lang}`));
      if (found) return found;
    }
    return voices.find((voice) => /^en/i.test(voice.lang)) || voices[0] || null;
  }

  function speakNextDeviceChunk() {
    if (!window.speechSynthesis || state.freeChunkIndex >= state.freeChunks.length) {
      state.deviceSpeaking = false;
      updateVoiceStatus("Podcast finished.");
      markCurrentArticlesRead();
      renderArticles();
      return;
    }
    const utterance = new SpeechSynthesisUtterance(state.freeChunks[state.freeChunkIndex]);
    utterance.voice = state.freeVoice || pickDeviceVoice();
    utterance.lang = utterance.voice?.lang || "en-IN";
    utterance.rate = 0.88;
    utterance.pitch = 0.98;
    utterance.onend = () => {
      state.freeChunkIndex += 1;
      speakNextDeviceChunk();
    };
    utterance.onerror = () => {
      state.deviceSpeaking = false;
      updateVoiceStatus("Device voice failed on this browser.");
      renderArticles();
    };
    window.speechSynthesis.speak(utterance);
  }

  async function playDeviceVoice(script, reason = "") {
    if (!window.speechSynthesis) {
      throw new Error("No premium voice is available, and this browser does not support device speech.");
    }
    window.speechSynthesis.cancel();
    state.freeVoice = pickDeviceVoice();
    state.freeChunks = chunkForSpeech(script);
    state.freeChunkIndex = 0;
    state.deviceSpeaking = true;
    state.freePaused = false;
    updateVoiceStatus(
      `${reason ? `${reason} ` : ""}Reading full briefing with local voice${state.freeVoice ? `: ${state.freeVoice.name}` : ""}.`
    );
    renderArticles();
    speakNextDeviceChunk();
  }

  async function saveGeminiKey() {
    const input = document.getElementById("geminiKeyInput");
    const apiKey = input?.value?.trim() || "";
    if (apiKey.length < 12) {
      updateVoiceStatus("Paste a valid Gemini API key first.");
      return;
    }
    updateVoiceStatus("Saving Gemini key for this local session...");
    try {
      const response = await fetch(VOICE_KEY_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ provider: "gemini", api_key: apiKey }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `Save failed with ${response.status}.`);
      input.value = "";
      await refreshVoiceStatus();
      updateVoiceStatus("Gemini key saved. Now tap Play Briefing.");
      if (state.status === "ready") renderArticles();
    } catch (error) {
      updateVoiceStatus(error.message);
    }
  }

  async function tryPremiumAudio(language, script) {
    const response = await fetch(AUDIO_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "audio/mpeg,audio/wav,application/json" },
      body: JSON.stringify({ language, script }),
    });
    if (!response.ok) {
      let message = `Premium audio failed with ${response.status}.`;
      try {
        const payload = await response.json();
        message = payload.detail || payload.error || message;
      } catch {}
      throw new Error(message);
    }
    const blob = await response.blob();
    stopPodcast();
    const url = URL.createObjectURL(blob);
    state.audio = new Audio(url);
    state.podcastMode = "premium";
    state.audio.onended = () => {
      updateVoiceStatus("Podcast finished.");
      state.audio = null;
      renderArticles();
    };
    await state.audio.play();
    const provider = response.headers.get("X-AHEAD-Voice-Provider") || "premium AI voice";
    updateVoiceStatus(`Playing ${provider}. Voice is AI-generated.`);
    return true;
  }

  async function tryLocalTts(script) {
    const response = await fetch(LOCAL_TTS_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "audio/wav,application/json" },
      body: JSON.stringify({ text: script }),
    });
    if (!response.ok) {
      let message = `Local neural voice failed with ${response.status}.`;
      try {
        const payload = await response.json();
        message = payload.detail || payload.error || message;
      } catch {}
      throw new Error(message);
    }
    const blob = await response.blob();
    stopPodcast();
    const url = URL.createObjectURL(blob);
    state.audio = new Audio(url);
    state.podcastMode = "kokoro";
    state.audio.onended = () => {
      updateVoiceStatus("Podcast finished.");
      markCurrentArticlesRead();
      state.audio = null;
      renderArticles();
    };
    await state.audio.play();
    const provider = response.headers.get("X-AHEAD-Voice-Provider") || "Kokoro local neural voice";
    updateVoiceStatus(`Playing ${provider}.`);
    return true;
  }

  async function markCurrentArticlesRead() {
    const ids = state.articles.map((article) => article.id).filter(Boolean);
    if (!ids.length) return;
    try {
      await fetch(MARK_READ_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ article_ids: ids }),
      });
    } catch {}
  }

  async function toggleAudio() {
    if (state.voiceBusy) return;
    if (state.deviceSpeaking) {
      if (state.freePaused) {
        window.speechSynthesis.resume();
        state.freePaused = false;
        updateVoiceStatus("Device voice resumed.");
      } else {
        window.speechSynthesis.pause();
        state.freePaused = true;
        updateVoiceStatus("Device voice paused.");
      }
      renderArticles();
      return;
    }
    if (state.audio) {
      if (state.audio.paused) {
        await state.audio.play();
        updateVoiceStatus("Audio resumed.");
      } else {
        state.audio.pause();
        updateVoiceStatus("Audio paused.");
      }
      renderArticles();
      return;
    }
    await playPodcast("en");
  }

  async function playPodcast(language) {
    try {
      state.voiceBusy = true;
      state.voiceError = "";
      renderArticles();
      const script = buildFullReaderScript();
      await refreshVoiceStatus();
      updateVoiceStatus("Preparing Kokoro local neural voice...");
      try {
        await tryLocalTts(script);
      } catch (localError) {
        if (state.voiceStatus?.gemini_configured || state.voiceStatus?.openai_configured) {
          updateVoiceStatus("Kokoro unavailable. Trying premium API voice...");
          try {
            await tryPremiumAudio(language, script);
            return;
          } catch (premiumError) {
            await playDeviceVoice(script, `Kokoro and premium voice failed. ${premiumError.message}`);
            return;
          }
        }
        await playDeviceVoice(script, `Kokoro unavailable. ${localError.message}`);
      }
    } catch (error) {
      state.voiceError = error.message;
      updateVoiceStatus(state.voiceError);
    } finally {
      state.voiceBusy = false;
      renderArticles();
    }
  }

  function stopPodcast() {
    if (state.audio) {
      state.audio.pause();
      state.audio.currentTime = 0;
      state.audio = null;
    }
    state.freeChunks = [];
    state.freeChunkIndex = 0;
    state.freePaused = false;
    state.deviceSpeaking = false;
    if (window.speechSynthesis) window.speechSynthesis.cancel();
  }

  renderShell();
  refreshVoiceStatus().finally(render);
})();
