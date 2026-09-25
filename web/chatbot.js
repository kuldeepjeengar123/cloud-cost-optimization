/**
 * Floating chat widget — bottom-right popup.
 *
 * Talks to POST /api/chat/stream (SSE: meta | delta | done | error) and
 * GET /api/chat/status (see server.py / src/chat/answer.py). Two modes,
 * decided by the server:
 *   "info" — no pipeline run has completed yet (or one is running): the
 *            assistant can only explain the platform/workflow itself.
 *   "rag"  — a run has completed: answers are grounded in that run's
 *            insights file, with the same platform-only scope enforced
 *            server-side via negative prompting.
 * The answer renders token-by-token as "delta" events arrive, instead of
 * waiting for the full completion.
 * User/system messages are set via textContent. Bot messages (LLM output,
 * untrusted) are rendered as markdown through Shared.renderMarkdown(), which
 * HTML-escapes the raw text before layering any markup on top — see
 * shared.js — so no live tag/attribute from the model can reach the DOM.
 * Conversations are kept per "context" (see setContext()), each with its own
 * message history, session id, and greeting/backend role (ROLE_CONFIG), so a
 * host page with distinct roles (e.g. the FinOps prototype's Employee vs RE
 * Team dashboards) gets a role-appropriate chat that doesn't leak history
 * (or framing) into the other role. The backend role is sent as "role" on
 * every request and steers how src/chat/answer.py frames its answer. The
 * widget instance is exposed as window.PlatformChat for a host page to call
 * setContext() on.
 */
(function () {
  "use strict";

  const STATUS_POLL_MS = 8000;

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "cbw-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
  }

  /** Fetch-based SSE parser (fetch's ReadableStream, not EventSource, since
   * EventSource can't send a POST body). Calls onEvent(event, data) for each
   * frame; stops once a "done" or "error" event is seen or the body ends. */
  async function streamSSE(body, onEvent) {
    const reader = body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    let finished = false;

    while (!finished) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });

      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);

        let event = "message";
        const dataLines = [];
        for (const line of chunk.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
        }
        const raw = dataLines.join("\n");
        let data = {};
        try { data = raw ? JSON.parse(raw) : {}; } catch (_e) { data = { raw }; }
        onEvent(event, data);

        if (event === "done" || event === "error") {
          finished = true;
          try { await reader.cancel(); } catch (_e) { /* ignore */ }
          break;
        }
      }
    }
  }

  function sessionId(contextKey) {
    const storageKey = "cbw_session_id_" + contextKey;
    let id = sessionStorage.getItem(storageKey);
    if (!id) {
      id = uuid();
      sessionStorage.setItem(storageKey, id);
    }
    return id;
  }

  // Per-context framing: which backend "role" this context maps to (see
  // src/chat/answer.py's ROLE_CONTEXT, which steers how the LLM frames its
  // answer) and the role-appropriate greeting for each pipeline state. Every
  // context greets the first time its panel is opened — just with text and
  // an API role suited to what that context actually does on the dashboard.
  const ROLE_CONFIG = {
    default: {
      apiRole: "employee",
      greetInfo: "Hello! I'm the platform assistant. No analysis has been run yet, so for now I can explain what this tool does, how the pipeline works, and the approval workflow. Run an analysis above to unlock questions about your actual AWS costs.",
      greetRag: "Hello! The latest analysis is ready — ask me about the cost data, charts or recommendations, or about how this platform works.",
      greetRunning: "Hello! An analysis is currently running. Meanwhile, ask me anything about this platform or how the pipeline works.",
    },
    emp: {
      apiRole: "employee",
      greetInfo: "Hello! I'm your platform assistant. No analysis has been run yet, so for now I can explain what this tool does, how the pipeline works, and the approval workflow. Run an analysis above to unlock questions about your actual AWS costs.",
      greetRag: "Hello! The latest analysis is ready — ask me about the cost data, the charts, or the recommendations you can raise for approval.",
      greetRunning: "Hello! An analysis is currently running. Meanwhile, ask me anything about this platform or how the pipeline works.",
    },
    re: {
      apiRole: "re_team",
      greetInfo: "Hello! I'm here to help the RE team review requests. No analysis has completed yet, so for now I can explain the approval workflow and how to review a request — ask me about that, or check back once a run finishes.",
      greetRag: "Hello! Ask me about the approval queue — blast radius, which requests touch production, the savings at stake, or the cost data behind any recommendation you're reviewing.",
      greetRunning: "Hello! An analysis is currently running, so the queue may still change. Meanwhile, ask me about the approval workflow or how to review a request.",
    },
  };
  function roleConfig(contextKey) {
    return ROLE_CONFIG[contextKey] || ROLE_CONFIG.default;
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const child of children) {
      if (child == null) continue;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    }
    return node;
  }

  const MODE_LABEL = {
    info: "General info",
    rag: "Live data ready",
  };

  class ChatWidget {
    constructor() {
      this.mode = "info";
      this.pipelineRunning = false;
      this.opened = false;
      this.contexts = {};
      this.contextKey = "default";
      this._buildDom();
      this._pollStatus();
      setInterval(() => this._pollStatus(), STATUS_POLL_MS);
    }

    /** Per-context state: independent message history + backend session id. */
    _store(key) {
      if (!this.contexts[key]) {
        this.contexts[key] = { sessionId: sessionId(key), messages: [], greeted: false };
      }
      return this.contexts[key];
    }

    /** "Start new chat" — wipes the CURRENT context's history and backend
     * session id (a fresh one is generated and persisted) and re-greets, as
     * if this context had never been opened. Only the active context is
     * affected; the other one (Employee vs RE Team) keeps its own history. */
    _newChat() {
      const key = this.contextKey;
      const freshId = uuid();
      sessionStorage.setItem("cbw_session_id_" + key, freshId);
      this.contexts[key] = { sessionId: freshId, messages: [], greeted: false };
      this.messagesEl.innerHTML = "";
      this._maybeGreet();
    }

    /** Switch which context's history is shown. Called by a host page when
     * its role/tab changes (e.g. setRole() on the FinOps prototype). A
     * context not seen before starts empty — nothing is shared across
     * contexts, including the backend session id used for RAG memory. */
    setContext(key) {
      const next = key || "default";
      if (next === this.contextKey) return;
      this.contextKey = next;
      this._renderContext();
      // The panel may already be open (a host's role/tab switch doesn't
      // close it) — greet the newly-active context right away in that case,
      // same as _toggle() does when the panel is opened fresh onto it.
      if (this.opened) this._maybeGreet();
    }

    _renderContext() {
      this.messagesEl.innerHTML = "";
      const store = this._store(this.contextKey);
      for (const m of store.messages) this._addMessage(m.kind, m.text, m.citations);
      this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
    }

    _maybeGreet() {
      if (!this._store(this.contextKey).greeted) this._greet();
    }

    _buildDom() {
      this.messagesEl = el("div", { id: "cbw-messages" });
      this.modeEl = el("div", { class: "cbw-mode" },
        el("span", { class: "cbw-pip" }), el("span", { id: "cbw-mode-text" }, "Connecting…"));

      this.input = el("textarea", {
        id: "cbw-input", rows: "1",
        placeholder: "Ask about the platform or your AWS costs…",
        onkeydown: (ev) => {
          if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); this._send(); }
        },
      });
      this.sendBtn = el("button", { id: "cbw-send", type: "submit" }, "Send");

      const form = el("form", { id: "cbw-form", onsubmit: (ev) => { ev.preventDefault(); this._send(); } },
        this.input, this.sendBtn);

      const panel = el("div", { id: "cbw-panel", hidden: "" },
        el("div", { id: "cbw-head" },
          el("div", { id: "cbw-head-icon", "aria-hidden": "true" }, this._brandIcon()),
          el("div", {},
            el("div", { class: "cbw-title" }, "Platform Assistant"),
            this.modeEl),
          el("button", { id: "cbw-newchat", type: "button", "aria-label": "Start new chat", title: "Start new chat", onclick: () => this._newChat() }, this._newChatIcon()),
          el("button", { id: "cbw-close", type: "button", "aria-label": "Close chat", onclick: () => this._toggle(false) }, "✕")),
        this.messagesEl,
        form);
      this.panel = panel;

      const launcher = el("button", {
        id: "cbw-launcher", type: "button", "aria-label": "Open chat assistant",
        onclick: () => this._toggle(),
      }, this._icon(), el("span", { class: "cbw-dot", "aria-hidden": "true" }));

      const root = el("div", { id: "cbw-root" }, panel, el("div", { id: "cbw-launcher-wrap" }, launcher));
      document.body.appendChild(root);
    }

    _brandIcon() {
      // Same mark as the dashboard topbar's brand-mark square, so the popup
      // header reads as part of the same product, not a bolted-on widget.
      const ns = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(ns, "svg");
      svg.setAttribute("width", "18"); svg.setAttribute("height", "18");
      svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("fill", "none");
      svg.setAttribute("stroke", "#fff"); svg.setAttribute("stroke-width", "2");
      svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round");
      const p1 = document.createElementNS(ns, "path"); p1.setAttribute("d", "M3 3v18h18");
      const p2 = document.createElementNS(ns, "path"); p2.setAttribute("d", "M7 15l4-5 3 3 5-7");
      svg.appendChild(p1); svg.appendChild(p2);
      return svg;
    }

    _newChatIcon() {
      // "Compose" pencil-in-square, the conventional "start a new
      // conversation" glyph — distinct from the brand mark and the close ✕.
      const ns = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(ns, "svg");
      svg.setAttribute("width", "15"); svg.setAttribute("height", "15");
      svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("fill", "none");
      svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "2");
      svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round");
      const p1 = document.createElementNS(ns, "path");
      p1.setAttribute("d", "M12 20h9");
      const p2 = document.createElementNS(ns, "path");
      p2.setAttribute("d", "M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z");
      svg.appendChild(p1); svg.appendChild(p2);
      return svg;
    }

    _icon() {
      const ns = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(ns, "svg");
      svg.setAttribute("width", "24"); svg.setAttribute("height", "24");
      svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("fill", "none");
      svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "2");
      svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round");
      const path = document.createElementNS(ns, "path");
      path.setAttribute("d", "M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z");
      svg.appendChild(path);
      return svg;
    }

    _toggle(force) {
      this.opened = typeof force === "boolean" ? force : !this.opened;
      this.panel.hidden = !this.opened;
      if (this.opened) {
        this._pollStatus();
        this._maybeGreet();
        this.input.focus();
      }
    }

    _setModeUI() {
      this.modeEl.classList.remove("rag", "running");
      let text = MODE_LABEL[this.mode] || "General info";
      if (this.pipelineRunning) { this.modeEl.classList.add("running"); text = "Analysis running…"; }
      else if (this.mode === "rag") { this.modeEl.classList.add("rag"); }
      this.modeEl.querySelector("#cbw-mode-text").textContent = text;
    }

    async _pollStatus() {
      try {
        const res = await fetch("/api/chat/status");
        if (!res.ok) return;
        const data = await res.json();
        const prevMode = this.mode;
        this.mode = data.mode || "info";
        this.pipelineRunning = !!data.pipeline_running;
        this._setModeUI();
        const store = this._store(this.contextKey);
        if (this.opened && prevMode === "info" && this.mode === "rag" && store.greeted) {
          const text = "The latest analysis just finished — I can now answer questions grounded in that run's data.";
          this._addMessage("system", text);
          store.messages.push({ kind: "system", text, citations: null });
        }
      } catch (_err) {
        this.modeEl.querySelector("#cbw-mode-text").textContent = "Offline";
      }
    }

    _greet() {
      const store = this._store(this.contextKey);
      store.greeted = true;
      const cfg = roleConfig(this.contextKey);
      const greeting = this.pipelineRunning ? cfg.greetRunning : this.mode === "rag" ? cfg.greetRag : cfg.greetInfo;
      this._addMessage("bot", greeting);
      store.messages.push({ kind: "bot", text: greeting, citations: null });
    }

    _addMessage(kind, text, citations) {
      const isBot = kind.split(" ")[0] === "bot";
      const msg = el("div", { class: `cbw-msg ${kind}` });
      if (isBot) msg.innerHTML = window.Shared.renderMarkdown(text);
      else msg.textContent = text;
      if (citations && citations.length) {
        const cites = el("div", { class: "cbw-cites" }, "Sources: ");
        for (const c of citations) cites.appendChild(el("span", {}, c));
        msg.appendChild(cites);
      }
      this.messagesEl.appendChild(msg);
      this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
      return msg;
    }

    /** The "waiting for a reply" bubble: an animated three-dot indicator
     * instead of static "Thinking…" text. Never persisted to a context's
     * store — the same bubble node is mutated in place (classList/innerHTML)
     * once the real answer starts arriving, in _send() below. */
    _addTypingBubble() {
      const msg = el("div", { class: "cbw-msg bot pending" },
        el("span", { class: "cbw-typing-label" }, "Thinking"),
        el("span", { class: "cbw-typing", "aria-hidden": "true" },
          el("span", { class: "cbw-typing-dot" }),
          el("span", { class: "cbw-typing-dot" }),
          el("span", { class: "cbw-typing-dot" })));
      this.messagesEl.appendChild(msg);
      this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
      return msg;
    }

    async _send() {
      const question = this.input.value.trim();
      if (!question) return;
      const store = this._store(this.contextKey);
      const apiRole = roleConfig(this.contextKey).apiRole;
      this.input.value = "";
      this.sendBtn.disabled = true;
      this._addMessage("user", question);
      store.messages.push({ kind: "user", text: question, citations: null });
      const bubble = this._addTypingBubble();
      let full = "";
      let citations = null;
      let gotDelta = false;

      try {
        const res = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question, session_id: store.sessionId, role: apiRole }),
        });
        if (!res.ok || !res.body) throw new Error("HTTP " + res.status);

        await streamSSE(res.body, (event, data) => {
          if (event === "meta") {
            this.mode = data.mode || this.mode;
            this.pipelineRunning = !!data.pipeline_running;
            this._setModeUI();
          } else if (event === "delta") {
            if (!gotDelta) { bubble.classList.remove("pending"); gotDelta = true; }
            full += data.text || "";
            bubble.innerHTML = window.Shared.renderMarkdown(full);
            this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
          } else if (event === "done") {
            full = data.answer || full;
            citations = (data.citations && data.citations.length) ? data.citations : null;
            bubble.classList.remove("pending");
            bubble.innerHTML = window.Shared.renderMarkdown(full);
            if (citations) {
              const cites = el("div", { class: "cbw-cites" }, "Sources: ");
              for (const c of citations) cites.appendChild(el("span", {}, c));
              bubble.appendChild(cites);
            }
          } else if (event === "error") {
            bubble.classList.remove("pending");
            full = full || data.message || "Something went wrong. Please try again.";
            bubble.textContent = full;
          }
        });
      } catch (_err) {
        bubble.classList.remove("pending");
        full = full || "Could not reach the assistant right now. Please try again.";
        bubble.textContent = full;
      } finally {
        this.sendBtn.disabled = false;
        store.messages.push({ kind: "bot", text: full, citations });
      }
    }
  }

  function init() {
    window.PlatformChat = new ChatWidget();
  }

  // Only needs document.body to exist (for appendChild), not full page load —
  // waiting on DOMContentLoaded would run after a host page's own inline
  // script, which may call setContext() on window.PlatformChat as soon as it
  // sets its initial role/tab.
  if (document.body) {
    init();
  } else {
    document.addEventListener("DOMContentLoaded", init);
  }
})();
