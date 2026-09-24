// ──────────────────────────────────────────────
//  CONFIG
// ──────────────────────────────────────────────
const OLLAMA_URL = 'http://127.0.0.1:11434';
const BACKEND_URL = 'http://127.0.0.1:8000';
const STARTER_QUESTIONS = [
  'What are IIBX trading hours?',
  'How do I register as a member?',
  'What commodities are traded on IIBX?',
  'What are the margin requirements?'
];

// ──────────────────────────────────────────────
//  STATE
// ──────────────────────────────────────────────
let conversationHistory = [];   // [{role, content}] — sent to backend, also used for export
let messageLog = [];            // [{role, content, timestamp}] — full record, used for export
let isGenerating = false;
let abortController = null;
let latestRetryBtn = null;      // reference to the retry button in the most recent response
let latestUserDiv = null;       // reference to the most recent user bubble (for edit)

// ──────────────────────────────────────────────
//  UI HELPERS
// ──────────────────────────────────────────────
function setGeneratingState(generating) {
  isGenerating = generating;
  document.getElementById('send-btn').disabled = generating;
  document.getElementById('send-btn').style.display = generating ? 'none' : 'flex';
  document.getElementById('stop-btn').style.display = generating ? 'flex' : 'none';
}

function stopGeneration() {
  if (abortController) abortController.abort();
}

function toggleSidebar() {
  document.querySelector('.sidebar').classList.toggle('collapsed');
}

function getModel() {
  return document.getElementById('model-select').value;
}

function removeWelcome() {
  const w = document.getElementById('welcome-card');
  if (w) w.remove();
}

function welcomeCardHTML() {
  const chips = STARTER_QUESTIONS
    .map(q => `<div class="chip" onclick="ask('${q.replace(/'/g, "\\'")}')">${q}</div>`)
    .join('\n          ');
  return `
    <div class="welcome" id="welcome-card">
      <div class="big-icon">🪙</div>
      <h2>Ask me anything about IIBX</h2>
      <p>I'm your dedicated assistant for the India International Bullion Exchange — covering trading, regulation, participants, GIFT City, BDRs, CEPA, and more.</p>
      <div class="chips">
          ${chips}
      </div>
    </div>`;
}

function appendMessage(role, text, docAttachments = null) {
  const messages = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  div.innerHTML = `
    <div class="avatar">${role === 'user' ? '👤' : 'AI'}</div>
    <div class="bubble">${renderMarkdown(text)}</div>`;

  if (role === 'user') {
    // If this message has doc attachments, inject pills above the bubble text
    if (docAttachments && docAttachments.length) {
      const bubble = div.querySelector('.bubble');
      const pillsContainer = document.createElement('div');
      pillsContainer.className = 'msg-doc-pills';
      for (const doc of docAttachments) {
        const pillEl = document.createElement('div');
        pillEl.className = 'msg-doc-pill';
        pillEl.innerHTML = `
          <div class="msg-doc-pill-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="14" height="14">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
            </svg>
          </div>
          <div class="msg-doc-pill-info">
            <span class="msg-doc-pill-name">${doc.filename}</span>
            <span class="msg-doc-pill-type">${doc.ext}</span>
          </div>`;
        pillsContainer.appendChild(pillEl);
      }
      bubble.insertBefore(pillsContainer, bubble.firstChild);
    }

    // Hide edit button on previous latest user bubble
    if (latestUserDiv) {
      const prev = latestUserDiv.querySelector('.edit-btn');
      if (prev) prev.style.display = 'none';
    }
    // Add edit button to this bubble
    const editBtn = document.createElement('button');
    editBtn.className = 'edit-btn';
    editBtn.title = 'Edit question';
    editBtn.textContent = '✏️';
    editBtn.addEventListener('click', () => editUserMessage(div, text));
    div.appendChild(editBtn);
    latestUserDiv = div;
  }

  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
  return div;
}

function appendTypingIndicator() {
  const messages = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = 'msg bot';
  div.id = 'typing-msg';
  div.innerHTML = `
    <div class="avatar">AI</div>
    <div class="bubble"><div class="typing-indicator"><span></span><span></span><span></span></div></div>`;
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
  return div;
}

// ── Edit the latest user message and re-send ──
function editUserMessage(userDiv, originalText) {
  if (isGenerating) return;

  const bubble = userDiv.querySelector('.bubble');
  const editBtn = userDiv.querySelector('.edit-btn');
  const reaskBtn = userDiv.querySelector('.reask-btn');

  // Hide both edit and reask buttons while editing
  if (editBtn) editBtn.style.display = 'none';
  if (reaskBtn) reaskBtn.style.display = 'none';
  const textarea = document.createElement('textarea');
  textarea.className = 'edit-textarea';
  textarea.value = originalText;
  bubble.innerHTML = '';
  bubble.appendChild(textarea);
  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);

  // Hide the edit button while editing
  if (editBtn) editBtn.style.display = 'none';

  // Action buttons
  const actions = document.createElement('div');
  actions.className = 'edit-actions';
  actions.innerHTML = `
    <button class="edit-confirm-btn">Send</button>
    <button class="edit-cancel-btn">Cancel</button>`;
  userDiv.appendChild(actions);

  actions.querySelector('.edit-cancel-btn').addEventListener('click', () => {
    bubble.innerHTML = renderMarkdown(originalText);
    actions.remove();
    if (editBtn) editBtn.style.display = '';
    if (reaskBtn) reaskBtn.style.display = '';
  });

  actions.querySelector('.edit-confirm-btn').addEventListener('click', () => {
    const newText = textarea.value.trim();
    if (!newText) return;
    actions.remove();

    // Remove this user bubble and everything after it from DOM
    const messagesEl = document.getElementById('messages');
    const allNodes = Array.from(messagesEl.children);
    const idx = allNodes.indexOf(userDiv);
    allNodes.slice(idx).forEach(n => n.remove());

    // Roll back history and log to before this user message
    // Find how many messages were after this point
    const userMsgIndex = messageLog.findIndex(
      m => m.role === 'user' && m.content === originalText
    );
    if (userMsgIndex !== -1) {
      messageLog.splice(userMsgIndex);
      conversationHistory.splice(userMsgIndex);
    }
    latestUserDiv = null;
    latestRetryBtn = null;

    // Re-send with the edited text
    document.getElementById('user-input').value = newText;
    sendMessage();
  });

  // Also allow Ctrl+Enter to confirm
  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      actions.querySelector('.edit-confirm-btn').click();
    }
    if (e.key === 'Escape') {
      actions.querySelector('.edit-cancel-btn').click();
    }
  });
}

function renderMarkdown(text) {
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h3>$1</h3>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>)/gs, m => `<ul>${m}</ul>`)
    .replace(/\n\n/g, '</p><p>')
    .replace(/\n/g, '<br>')
    .replace(/^(.)/, '<p>$1').replace(/(.)$/, '$1</p>');
}

// ── Source citation + feedback rendering (appended after a bot reply finishes) ──
function appendResponseFooter(botDiv, { sources, blocked, userQuery, assistantText }) {
  const footer = document.createElement('div');
  footer.className = 'response-footer';

  if (sources && sources.length && !blocked) {
    const sourceText = sources
      .map(s => {
        const icon = s.source_type === 'pdf' ? '📄' : '🌐';
        return `${icon} ${s.document}, ${s.section}`;
      })
      .join(' · ');
    const sourceTag = document.createElement('div');
    sourceTag.className = 'source-tag';
    sourceTag.textContent = `Source: ${sourceText}`;
    footer.appendChild(sourceTag);
  }

  if (!blocked) {
    const feedbackRow = document.createElement('div');
    feedbackRow.className = 'feedback-row';
    feedbackRow.innerHTML = `
      <button class="feedback-btn" data-rating="up" title="Helpful">👍</button>
      <button class="feedback-btn" data-rating="down" title="Not helpful">👎</button>
      <button class="feedback-btn retry-btn" title="Regenerate answer">↺</button>
      <button class="feedback-btn copy-btn" title="Copy answer">⎘</button>`;
    feedbackRow.querySelectorAll('.feedback-btn[data-rating]').forEach(btn => {
      btn.addEventListener('click', () => {
        if (btn.classList.contains('sent')) return;
        feedbackRow.querySelectorAll('.feedback-btn[data-rating]').forEach(b => b.disabled = true);
        btn.classList.add('sent', btn.dataset.rating === 'up' ? 'sent-up' : 'sent-down');
        submitFeedback(userQuery, assistantText, btn.dataset.rating, sources || []);
      });
    });
    const retryBtn = feedbackRow.querySelector('.retry-btn');

    // Hide the previous response's retry button — only the latest should have it
    if (latestRetryBtn) latestRetryBtn.style.display = 'none';
    latestRetryBtn = retryBtn;

    retryBtn.addEventListener('click', () => {
      retryLastMessage(botDiv, userQuery);
    });

    feedbackRow.querySelector('.copy-btn').addEventListener('click', (e) => {
      navigator.clipboard.writeText(assistantText).then(() => {
        const btn = e.currentTarget;
        btn.textContent = '✓';
        btn.classList.add('sent-up');
        setTimeout(() => { btn.textContent = '⎘'; btn.classList.remove('sent-up'); }, 1500);
      });
    });

    footer.appendChild(feedbackRow);
  }

  botDiv.appendChild(footer);
}

async function submitFeedback(query, response, rating, sources) {
  try {
    await fetch(`${BACKEND_URL}/api/feedback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, response, rating, sources })
    });
  } catch (e) {
    console.log('[feedback]', { query, response, rating, sources, error: String(e) });
  }
}

// ── Retry: remove the last bot turn and regenerate ──
async function retryLastMessage(oldBotDiv, userQuery) {
  if (isGenerating) return;

  // Strip the last assistant entry from both history and log
  if (conversationHistory.length && conversationHistory[conversationHistory.length - 1].role === 'assistant') {
    conversationHistory.pop();
  }
  if (messageLog.length && messageLog[messageLog.length - 1].role !== 'user') {
    messageLog.pop();
  }

  // Remember where in the DOM to insert the new bubble (right where the old one was)
  const insertAfter = oldBotDiv.previousSibling;
  const messagesEl = document.getElementById('messages');
  oldBotDiv.remove();

  // History to send = everything up to (but not including) the user turn we're retrying
  const historyForRequest = conversationHistory.slice(0, -1);

  setGeneratingState(true);
  abortController = new AbortController();

  // Insert typing indicator at the correct position
  const typingDiv = document.createElement('div');
  typingDiv.className = 'msg bot';
  typingDiv.id = 'typing-msg';
  typingDiv.innerHTML = `
    <div class="avatar">AI</div>
    <div class="bubble"><div class="typing-indicator"><span></span><span></span><span></span></div></div>`;
  if (insertAfter) {
    insertAfter.after(typingDiv);
  } else {
    messagesEl.prepend(typingDiv);
  }

  let fullText = '';
  let sources = [];
  let blocked = false;
  let completed = false;

  try {
    const response = await fetch(`${BACKEND_URL}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: abortController.signal,
      body: JSON.stringify({
        model: getModel(),
        query: userQuery,
        history: historyForRequest,
        top_k: 6,
        doc_session_ids: activeDocs.length ? activeDocs.map(d => d.sessionId) : null,
      })
    });

    if (!response.ok) throw new Error(`Backend error: ${response.status}`);

    let botDiv = null;
    let bubble = null;

    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value } = await reader.read();
      if (done) { completed = true; break; }
      if (abortController.signal.aborted) break;
      const chunk = decoder.decode(value);
      const lines = chunk.split('\n').filter(l => l.trim());
      for (const line of lines) {
        try {
          const parsed = JSON.parse(line);
          if (parsed.message?.content) {
            if (!botDiv) {
              // Replace typing indicator with real bubble at the same position
              const newDiv = document.createElement('div');
              newDiv.className = 'msg bot';
              newDiv.innerHTML = `<div class="avatar">AI</div><div class="bubble"></div>`;
              typingDiv.replaceWith(newDiv);
              botDiv = newDiv;
              bubble = botDiv.querySelector('.bubble');
            }
            fullText += parsed.message.content;
            bubble.innerHTML = renderMarkdown(fullText);
            const nearBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
            if (nearBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
          }
          if (parsed.done) {
            sources = parsed.sources || [];
            blocked = !!parsed.blocked;
          }
        } catch (e) {}
      }
    }

    if (completed && fullText && botDiv) {
      conversationHistory.push({ role: 'assistant', content: fullText });
      messageLog.push({ role: 'assistant', content: fullText, timestamp: new Date(), sources });
      appendResponseFooter(botDiv, { sources, blocked, userQuery, assistantText: fullText });
      saveSession();
    }

  } catch (err) {
    typingDiv?.remove();
    if (err.name !== 'AbortError') {
      appendMessage('bot', `⚠️ **Error:** ${err.message}`);
    }
  } finally {
    abortController = null;
    setGeneratingState(false);
  }
}

// ──────────────────────────────────────────────
const SESSION_KEY = 'iibx_chat_session';

function saveSession() {
  if (messageLog.length === 0) return;
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify({
      conversationHistory,
      messageLog: messageLog.map(m => ({
        ...m,
        timestamp: m.timestamp instanceof Date ? m.timestamp.toISOString() : m.timestamp
      }))
    }));
  } catch (e) { /* storage full or unavailable — silently skip */ }
}

function loadSavedSession() {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed.messageLog || parsed.messageLog.length === 0) return null;
    // Rehydrate timestamps
    parsed.messageLog = parsed.messageLog.map(m => ({
      ...m,
      timestamp: new Date(m.timestamp)
    }));
    return parsed;
  } catch (e) { return null; }
}

function restoreSession() {
  const saved = loadSavedSession();
  if (!saved) return;
  conversationHistory = saved.conversationHistory;
  messageLog = saved.messageLog;
  latestRetryBtn = null;
  latestUserDiv = null;

  // Re-render messages from the log
  removeWelcome();
  const container = document.getElementById('messages');
  container.innerHTML = '';
  for (const m of messageLog) {
    const div = appendMessage(m.role === 'user' ? 'user' : 'bot', m.content);
    if (m.role !== 'user' && m.sources !== undefined) {
      appendResponseFooter(div, {
        sources: m.sources || [],
        blocked: false,
        userQuery: '',
        assistantText: m.content
      });
    }
  }
  document.getElementById('restore-banner').style.display = 'none';
}

function dismissRestore() {
  localStorage.removeItem(SESSION_KEY);
  document.getElementById('restore-banner').style.display = 'none';
}

function checkRestoreBanner() {
  const saved = loadSavedSession();
  if (saved) {
    document.getElementById('restore-banner').style.display = 'flex';
  }
}


async function checkOllama() {
  const dot = document.getElementById('status-dot');
  const txt = document.getElementById('status-text');
  dot.className = 'status-dot loading';
  txt.textContent = 'Connecting…';
  try {
    const res = await fetch(`${OLLAMA_URL}/api/tags`, { signal: AbortSignal.timeout(3000) });
    if (res.ok) {
      dot.className = 'status-dot online';
      txt.textContent = 'Ollama ready';
      document.getElementById('setup-banner-2').style.display = 'none';
      return true;
    }
  } catch (e) {}
  dot.className = 'status-dot error';
  txt.textContent = 'Ollama offline';
  document.getElementById('setup-banner-2').style.display = 'flex';
  return false;
}

// ──────────────────────────────────────────────
//  BACKEND STATUS CHECK (new — RAG server must also be running)
// ──────────────────────────────────────────────
async function checkBackend() {
  const banner = document.getElementById('backend-banner');
  try {
    const res = await fetch(`${BACKEND_URL}/api/health`, { signal: AbortSignal.timeout(3000) });
    if (res.ok) {
      if (banner) banner.style.display = 'none';
      return true;
    }
  } catch (e) {}
  if (banner) banner.style.display = 'flex';
  return false;
}

// ──────────────────────────────────────────────
//  MODEL SELECTOR — fetch available models dynamically from Ollama
// ──────────────────────────────────────────────
async function loadModels() {
  const select = document.getElementById('model-select');
  const previousValue = select.value;
  try {
    const res = await fetch(`${OLLAMA_URL}/api/tags`, { signal: AbortSignal.timeout(3000) });
    if (!res.ok) throw new Error('bad response');
    const data = await res.json();
    const names = (data.models || []).map(m => m.name).filter(Boolean);
    if (names.length === 0) return; // keep existing hardcoded fallback options

    select.innerHTML = names.map(n => `<option value="${n}">${n}</option>`).join('');
    if (names.includes(previousValue)) {
      select.value = previousValue;
    }
  } catch (e) {
    // Ollama offline or unreachable — leave the existing fallback option list as-is
  }
}

// ──────────────────────────────────────────────
//  DOCUMENT UPLOAD  — supports multiple files
// ──────────────────────────────────────────────
// Each entry: { sessionId, filename, ext }
let activeDocs = [];
let pendingDocAttachments = []; // docs not yet sent with a message

function _pillHTML(fileId, filename, ext) {
  return `
    <div class="doc-pill" id="pill-${fileId}" data-file-id="${fileId}">
      <div class="doc-pill-progress" id="pill-progress-${fileId}">
        <svg class="donut-ring" viewBox="0 0 36 36" width="36" height="36">
          <circle class="donut-bg" cx="18" cy="18" r="14"/>
          <circle class="donut-fill" id="donut-${fileId}" cx="18" cy="18" r="14"
            stroke-dasharray="0 88" stroke-dashoffset="22"/>
        </svg>
      </div>
      <div class="doc-pill-icon" id="pill-icon-${fileId}" style="display:none">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round" stroke-linejoin="round" width="16" height="16">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
        </svg>
      </div>
      <div class="doc-pill-info">
        <span class="doc-pill-name">${filename}</span>
        <span class="doc-pill-type">${ext}</span>
      </div>
      <button class="doc-pill-remove" id="pill-remove-${fileId}" style="display:none"
        onclick="removeDocPill('${fileId}')" title="Remove">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
          stroke-linecap="round" stroke-linejoin="round" width="13" height="13">
          <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
        </svg>
      </button>
    </div>`;
}

async function handleDocUpload(event) {
  const files = Array.from(event.target.files);
  event.target.value = '';
  if (!files.length) return;

  const pillRow = document.getElementById('doc-pill-row');
  pillRow.style.display = 'flex';
  removeWelcome();

  const circumference = 2 * Math.PI * 14;

  for (const file of files) {
    const fileId = Math.random().toString(36).slice(2);
    const ext = (file.name.split('.').pop() || 'FILE').toUpperCase();

    // Insert pill immediately
    pillRow.insertAdjacentHTML('beforeend', _pillHTML(fileId, file.name, ext));

    const donutCircle = document.getElementById(`donut-${fileId}`);
    const progressEl  = document.getElementById(`pill-progress-${fileId}`);
    const iconEl      = document.getElementById(`pill-icon-${fileId}`);
    const removeEl    = document.getElementById(`pill-remove-${fileId}`);

    // Animate donut
    let fakeProgress = 0;
    const interval = setInterval(() => {
      fakeProgress = Math.min(fakeProgress + Math.random() * 8 + 3, 85);
      const filled = (fakeProgress / 100) * circumference;
      donutCircle.setAttribute('stroke-dasharray', `${filled} ${circumference - filled}`);
    }, 200);

    // Upload
    try {
      const formData = new FormData();
      formData.append('file', file);
      const res = await fetch(`${BACKEND_URL}/api/upload-doc`, { method: 'POST', body: formData });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      clearInterval(interval);
      donutCircle.setAttribute('stroke-dasharray', `${circumference} 0`);
      await new Promise(r => setTimeout(r, 350));
      progressEl.style.display = 'none';
      iconEl.style.display = 'flex';
      removeEl.style.display = 'flex';

      activeDocs.push({ sessionId: data.doc_session_id, filename: data.filename, ext, fileId });
      pendingDocAttachments.push({ sessionId: data.doc_session_id, filename: data.filename, ext, fileId });

    } catch (err) {
      clearInterval(interval);
      document.getElementById(`pill-${fileId}`)?.remove();
      if (!pillRow.querySelector('.doc-pill')) pillRow.style.display = 'none';
      alert(`Could not process ${file.name}: ${err.message}`);
    }
  }

  // Update placeholder
  const names = activeDocs.map(d => d.filename).join(', ');
  document.getElementById('user-input').placeholder =
    activeDocs.length ? `Ask about ${names}…` : 'Ask about IIBX trading, regulations, GIFT City…';
}

function removeDocPill(fileId) {
  const doc = activeDocs.find(d => d.fileId === fileId);
  if (doc) {
    // Clean up backend collection
    fetch(`${BACKEND_URL}/api/doc-session/${doc.sessionId}`, { method: 'DELETE' }).catch(() => {});
    activeDocs = activeDocs.filter(d => d.fileId !== fileId);
    pendingDocAttachments = pendingDocAttachments.filter(d => d.fileId !== fileId);
  }
  document.getElementById(`pill-${fileId}`)?.remove();
  const pillRow = document.getElementById('doc-pill-row');
  if (!pillRow.querySelector('.doc-pill')) pillRow.style.display = 'none';

  const names = activeDocs.map(d => d.filename).join(', ');
  document.getElementById('user-input').placeholder =
    activeDocs.length ? `Ask about ${names}…` : 'Ask about IIBX trading, regulations, GIFT City…';
}

// Clean up all doc sessions on page unload
window.addEventListener('beforeunload', () => {
  activeDocs.forEach(d => {
    navigator.sendBeacon(`${BACKEND_URL}/api/doc-session/${d.sessionId}`, '');
  });
});

// ──────────────────────────────────────────────
//  SEND MESSAGE  (now routed through the RAG backend)
// ──────────────────────────────────────────────
async function sendMessage() {
  if (isGenerating) return;
  const input = document.getElementById('user-input');
  const text = input.value.trim();
  if (!text) return;

  const [ollamaOk, backendOk] = await Promise.all([checkOllama(), checkBackend()]);
  if (!backendOk) {
    alert('The IIBX RAG backend is not running. Start it with:\n\ncd backend && uvicorn app:app --reload --port 8000');
    return;
  }
  if (!ollamaOk) {
    alert('Ollama is not running. Please start it with: ollama serve');
    return;
  }

  removeWelcome();
  input.value = '';
  input.style.height = 'auto';

  // Dismiss the restore banner if still showing — user chose to start fresh
  const restoreBanner = document.getElementById('restore-banner');
  if (restoreBanner) restoreBanner.style.display = 'none';

  setGeneratingState(true);
  abortController = new AbortController();

  const historyForRequest = [...conversationHistory]; // snapshot BEFORE this turn
  conversationHistory.push({ role: 'user', content: text });
  messageLog.push({ role: 'user', content: text, timestamp: new Date() });

  // If docs are pending, attach all of them to this message and hide the pill row
  let docAttachments = null;
  if (pendingDocAttachments.length) {
    docAttachments = pendingDocAttachments.map(d => ({ filename: d.filename, ext: d.ext }));
    pendingDocAttachments = []; // clear — they've been sent
    document.getElementById('doc-pill-row').style.display = 'none';
  }

  const userDiv = appendMessage('user', text, docAttachments);
  const typingDiv = appendTypingIndicator();

  let fullText = '';
  let sources = [];
  let blocked = false;
  let completed = false;

  try {
    const response = await fetch(`${BACKEND_URL}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: abortController.signal,
      body: JSON.stringify({
        model: getModel(),
        query: text,
        history: historyForRequest,
        top_k: 6,
        doc_session_ids: activeDocs.length ? activeDocs.map(d => d.sessionId) : null,
      })
    });

    if (!response.ok) throw new Error(`Backend error: ${response.status}`);

    const messages = document.getElementById('messages');
    let botDiv = null;
    let bubble = null;

    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    while (true) {
      const { done, value } = await reader.read();
      if (done) { completed = true; break; }
      if (abortController.signal.aborted) break;
      const chunk = decoder.decode(value);
      const lines = chunk.split('\n').filter(l => l.trim());
      for (const line of lines) {
        try {
          const parsed = JSON.parse(line);
          if (parsed.message?.content) {
            // First token — swap typing indicator for the real bubble
            if (!botDiv) {
              typingDiv.remove();
              botDiv = appendMessage('bot', '');
              bubble = botDiv.querySelector('.bubble');
            }
            fullText += parsed.message.content;
            bubble.innerHTML = renderMarkdown(fullText);
            const nearBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80;
            if (nearBottom) messages.scrollTop = messages.scrollHeight;
          }
          if (parsed.done) {
            sources = parsed.sources || [];
            blocked = !!parsed.blocked;
          }
        } catch (e) {}
      }
    }

    if (completed && fullText && botDiv) {
      conversationHistory.push({ role: 'assistant', content: fullText });
      messageLog.push({ role: 'assistant', content: fullText, timestamp: new Date(), sources });
      appendResponseFooter(botDiv, { sources, blocked, userQuery: text, assistantText: fullText });
      saveSession();
    }

  } catch (err) {
    typingDiv?.remove();
    if (err.name === 'AbortError') {
      conversationHistory.pop();
      messageLog.pop();
      // Show a Re-ask button on the orphaned user bubble
      const reaskBtn = document.createElement('button');
      reaskBtn.className = 'reask-btn';
      reaskBtn.textContent = '↺ Re-ask';
      reaskBtn.addEventListener('click', () => {
        userDiv.remove();
        document.getElementById('user-input').value = text;
        sendMessage();
      });
      userDiv.appendChild(reaskBtn);
    } else {
      appendMessage('bot', `⚠️ **Error:** ${err.message}\n\nMake sure both the backend (\`uvicorn app:app --port 8000\`) and Ollama (\`ollama serve\`) are running.`);
      document.getElementById('status-dot').className = 'status-dot error';
      document.getElementById('status-text').textContent = 'Error';
    }
  } finally {
    abortController = null;
    setGeneratingState(false);
    input.focus();
  }
}

// ──────────────────────────────────────────────
//  CHAT CONTROLS
// ──────────────────────────────────────────────
function ask(question) {
  document.getElementById('user-input').value = question;
  sendMessage();
}

function clearChat() {
  if (messageLog.length === 0) return;
  if (!confirm('Clear the current conversation? This cannot be undone.')) return;
  if (abortController) abortController.abort();
  abortController = null;
  setGeneratingState(false);
  conversationHistory = [];
  messageLog = [];
  latestRetryBtn = null;
  latestUserDiv = null;
  localStorage.removeItem(SESSION_KEY);

  // Clean up all doc sessions
  activeDocs.forEach(d => {
    fetch(`${BACKEND_URL}/api/doc-session/${d.sessionId}`, { method: 'DELETE' }).catch(() => {});
  });
  activeDocs = [];
  pendingDocAttachments = [];
  document.getElementById('doc-pill-row').innerHTML = '';
  document.getElementById('doc-pill-row').style.display = 'none';
  document.getElementById('user-input').placeholder = 'Ask about IIBX trading, regulations, GIFT City…';

  document.getElementById('messages').innerHTML = welcomeCardHTML();
  document.getElementById('user-input').focus();
}

// ──────────────────────────────────────────────
//  CHAT EXPORT — download conversation as a timestamped .txt file
// ──────────────────────────────────────────────
function exportChat() {
  if (messageLog.length === 0) {
    alert('Nothing to export yet — start a conversation first.');
    return;
  }

  const lines = [
    'IIBX Assistant — Conversation Export',
    `Generated: ${new Date().toLocaleString()}`,
    '─'.repeat(50),
    ''
  ];

  for (const m of messageLog) {
    const label = m.role === 'user' ? 'User' : 'Assistant';
    const ts = m.timestamp instanceof Date ? m.timestamp.toLocaleString() : String(m.timestamp);
    lines.push(`[${ts}] ${label}:`);
    lines.push(m.content);
    if (m.sources && m.sources.length) {
      lines.push(`Source: ${m.sources.map(s => `${s.document}, ${s.section}`).join(' · ')}`);
    }
    lines.push('');
  }

  const blob = new Blob([lines.join('\n')], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `iibx-chat-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.txt`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ──────────────────────────────────────────────
//  TEXTAREA AUTO-RESIZE + ENTER TO SEND
// ──────────────────────────────────────────────
const textarea = document.getElementById('user-input');
textarea.addEventListener('input', () => {
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 120) + 'px';
});
textarea.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

// ──────────────────────────────────────────────
//  INIT
// ──────────────────────────────────────────────
checkOllama();
checkBackend();
loadModels();
checkRestoreBanner();
setInterval(checkOllama, 30000);
setInterval(checkBackend, 30000);
textarea.focus();
