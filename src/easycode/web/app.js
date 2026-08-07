/* ============================================================
   EasyCode — chat-first 前端（T7 版）
   职责：状态、API、SSE、DOM 编排。纯渲染函数在 render.js
   （escape/markdown/语法高亮/diff/工具块 HTML），本文件不拼装富 HTML。
   结构：状态 → 主题 → API → 流式渲染 → 会话列表 → 事件流渲染 →
   composer（新会话/steering/continue）→ SSE → 对话框。
   ============================================================ */
'use strict';
const $ = (id) => document.getElementById(id);
const state = {
  sessions: [], selected: null, session: null, config: null, providers: [], modelInfo: null,
  live: null, liveBuf: '', eventIds: new Set(), pending: [], stick: true,
  usageList: [], stepCount: 0, sessionFilter: '',
};

const TERMINAL = new Set(['completed', 'error', 'cancelled', 'max_steps', 'max_cost', 'max_seconds', 'interrupted']);
const STATUS_LABEL = { queued: '排队中', running: '进行中', completed: '已完成', error: '出错', cancelled: '已停止',
  max_steps: '达到步数上限', max_cost: '达到花费上限', max_seconds: '超时自动停止', interrupted: '已中断' };

/* ---------- 基础 ---------- */
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = null; }
  if (!response.ok) throw new Error((body && body.detail) || `HTTP ${response.status}`);
  return body;
}
/* ---------- Toast（网络类错误统一走这里，composer-error 只留输入校验） ---------- */
function toast(message, cls = 'info', ms = 3000) {
  const node = document.createElement('div');
  node.className = `toast ${cls}`;
  node.textContent = message;
  $('toasts').appendChild(node);
  requestAnimationFrame(() => node.classList.add('in'));
  setTimeout(() => { node.classList.remove('in'); setTimeout(() => node.remove(), 220); }, ms);
}
/* 滚动跟随：距底 <40px 才跟随，用户上翻时不再被拽回。 */
function nearBottom() {
  const el = $('stream');
  return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
}
function scrollStream(force) {
  if (force || state.stick) { const el = $('stream'); el.scrollTop = el.scrollHeight; }
}
function addNode(html, cls) {
  const node = document.createElement('div');
  node.className = cls;
  node.innerHTML = html;
  $('stream').appendChild(node);
  scrollStream();
  return node;
}

/* ---------- 主题（亮 / 暗 / 跟随系统三态循环） ---------- */const THEME_ORDER = ['light', 'dark', 'system'];
const THEME_ICON = { light: '☀', dark: '☾', system: '◐' };
const THEME_LABEL = { light: '亮色', dark: '暗色', system: '跟随系统' };
function applyTheme(mode) {
  const dark = mode === 'dark' || (mode === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  $('theme-toggle').textContent = THEME_ICON[mode];
  $('theme-toggle').title = `主题：${THEME_LABEL[mode]}（点击切换）`;
}
$('theme-toggle').onclick = () => {
  const current = localStorage.getItem('easycode-theme') || 'system';
  const next = THEME_ORDER[(THEME_ORDER.indexOf(current) + 1) % THEME_ORDER.length];
  localStorage.setItem('easycode-theme', next);
  applyTheme(next);
};
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
  if ((localStorage.getItem('easycode-theme') || 'system') === 'system') applyTheme('system');
});

/* ---------- 流式 live 气泡（markdown 节流重渲染，保留光标） ---------- */
let liveTimer = 0;
function resetLive() {
  if (liveTimer) { clearTimeout(liveTimer); liveTimer = 0; }
  if (state.live) { state.live.remove(); state.live = null; }
  state.liveBuf = '';
}
function scheduleLive() {
  if (liveTimer) return;
  liveTimer = setTimeout(() => {
    liveTimer = 0;
    if (!state.live) return;
    state.live.innerHTML = renderMarkdown(state.liveBuf) + '<span class="cursor">▍</span>';
    scrollStream();
  }, 120);
}

/* ---------- 工具块（pending → result 原位替换） ---------- */
function appendToolEvent(kind, data) {
  if (kind === 'tool_call') {
    const node = addNode(toolBlockHTML({ name: data.name, arguments: safeJson(data.arguments) }, true), 'event tool');
    state.pending.push(node);
    return;
  }
  // tool_result：按 FIFO 与 pending 配对（工具顺序执行）
  const node = state.pending.shift();
  if (node) {
    node.classList.remove('pending');
    node.innerHTML = toolBlockHTML(data, false);
    scrollStream();
  } else {
    addNode(toolBlockHTML(data, false), 'event tool');
  }
}

/* ---------- 会话列表（搜索过滤 + 重命名 + 两步确认删除） ---------- */
function renderSessions() {
  const filter = state.sessionFilter.trim().toLowerCase();
  const visible = state.sessions.filter((session) => !filter
    || String(session.title || '').toLowerCase().includes(filter)
    || String(session.workspace || '').toLowerCase().includes(filter));
  $('session-list').innerHTML = visible.map((session) => {
    const active = session.id === state.selected;
    const dot = TERMINAL.has(session.status) ? (session.status === 'completed' ? 'done' : 'bad') : 'run';
    const round = session.round > 1 ? `<span class="s-round">R${session.round}</span>` : '';
    return `<div class="session-item${active ? ' active' : ''}" data-id="${escape(session.id)}">` +
      `<button class="s-open" data-id="${escape(session.id)}">` +
      `<span class="s-dot ${dot}"></span><span class="s-body"><span class="s-title">${escape(session.title || '(无标题)')}</span>` +
      `<span class="s-sub"><span class="s-path mono">${escape((session.workspace || '').split(/[\\/]/).pop())}</span>` +
      `${round}<span class="s-time">${relTime(session.created)}</span></span></span></button>` +
      `<button class="s-act s-rename" data-rename="${escape(session.id)}" title="重命名">✎</button>` +
      `<button class="s-delete" data-del="${escape(session.id)}" title="删除会话">×</button></div>`;
  }).join('') || (filter ? '<div class="session-empty">无匹配会话</div>' : '<div class="session-empty">还没有会话</div>');
  document.querySelectorAll('.s-open').forEach((node) => { node.onclick = () => openSession(node.dataset.id); });
  document.querySelectorAll('.s-delete').forEach((node) => { node.onclick = (e) => { e.stopPropagation(); deleteSession(node.dataset.del, node); }; });
  document.querySelectorAll('.s-rename').forEach((node) => {
    node.onclick = (e) => { e.stopPropagation(); startRename(node.dataset.rename, node.closest('.session-item')); };
  });
}
/* 行内重命名：Enter 提交 PATCH，Esc / 失焦取消 */
function startRename(sessionId, itemEl) {
  const session = state.sessions.find((s) => s.id === sessionId);
  if (!session || itemEl.querySelector('.s-title-edit')) return;
  const titleEl = itemEl.querySelector('.s-title');
  const input = document.createElement('input');
  input.className = 's-title-edit';
  input.value = session.title || '';
  titleEl.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const title = input.value.trim();
    if (save && title && title !== session.title) {
      api(`/api/sessions/${sessionId}`, { method: 'PATCH', body: JSON.stringify({ title }) })
        .then(() => { session.title = title; return refreshSessions(); })
        .catch((error) => { toast(`重命名失败：${error.message}`, 'error'); renderSessions(); });
      return;
    }
    renderSessions();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(false));
  input.addEventListener('click', (e) => e.stopPropagation());
}
async function deleteSession(sessionId, node) {
  if (node.dataset.armed !== 'yes') {
    node.dataset.armed = 'yes';
    node.classList.add('armed');
    node.textContent = '确认';
    setTimeout(() => { node.dataset.armed = ''; node.classList.remove('armed'); node.textContent = '×'; }, 2500);
    return;
  }
  try {
    await api(`/api/sessions/${sessionId}`, { method: 'DELETE' });
    if (state.selected === sessionId) showWelcome();
    await refreshSessions();
  } catch (error) { toast(`删除失败：${error.message}`, 'error'); }
}
async function refreshSessions() {
  state.sessions = await api('/api/sessions');
  renderSessions();
}

/* ---------- 事件流渲染 ---------- */
function statusChip(session) {
  const label = STATUS_LABEL[session.status] || session.status;
  $('status').textContent = label;
  $('status').className = 'status ' + (TERMINAL.has(session.status) ? session.status : 'running');
  $('cancel-btn').classList.toggle('hidden', TERMINAL.has(session.status));
  const running = !TERMINAL.has(session.status);
  $('composer-send').textContent = running ? '补充' : '继续';
  $('composer-hint').textContent = running
    ? '它正在工作——现在发送会先记下来，做完手头这步就按你的补充继续'
    : 'Enter 发送 = 在这个会话继续（之前的经过它都记得）';
  const queued = (session.queued || []).length;
  $('queue-chip').classList.toggle('hidden', !queued);
  $('queue-chip').textContent = `排队 ${queued} 条`;
}
function renderUsage(usage) {
  if (!usage) return;
  const hitRate = usage.prompt ? Math.round((usage.cached || 0) / usage.prompt * 100) : 0;
  const cost = usage.cost != null ? ` · ¥${Number(usage.cost).toFixed(4)}` : '';
  $('usage').textContent = `${usage.calls || 0} 次调用 · ${usage.prompt || 0}+${usage.completion || 0} tok` +
    (usage.cached ? `（缓存 ${hitRate}%）` : '') + cost;
}
function renderTopbar(session) {
  $('tools-chip').textContent = `工具 ${(session.tools || []).join('/')}`;
  $('tools-chip').title = `步数上限 ${session.limits ? session.limits.max_steps : '—'} · 成本上限 ${session.limits && session.limits.max_cost != null ? session.limits.max_cost : '不限'}`;
}
/* 用量明细：usage 事件是累计快照，逐轮行 = 相邻快照差分；工具轮数来自带 tool_calls 的 assistant_message */
function openUsageDialog() {
  const list = state.usageList;
  const last = list[list.length - 1] || { calls: 0, prompt: 0, completion: 0, cached: 0, cost: null };
  const hitRate = last.prompt ? Math.round((last.cached || 0) / last.prompt * 100) : 0;
  const cell = (label, value) => `<div class="usage-cell"><b>${escape(value)}</b><span>${escape(label)}</span></div>`;
  $('usage-summary').innerHTML =
    cell('调用次数', String(last.calls || 0)) +
    cell('Token 用量（输入+输出）', `${last.prompt || 0} + ${last.completion || 0}`) +
    cell('缓存命中率', `${hitRate}%`) +
    cell('花费', last.cost != null ? `¥${Number(last.cost).toFixed(4)}` : '未计价');
  const maxSteps = state.session && state.session.limits ? state.session.limits.max_steps : null;
  const steps = state.stepCount;
  $('steps-fill').style.width = maxSteps ? `${Math.min(100, Math.round(steps / maxSteps * 100))}%` : '0%';
  $('steps-note').textContent = maxSteps ? `已执行 ${steps} / ${maxSteps} 步` : `已执行 ${steps} 步`;
  if (!list.length) {
    $('usage-rows').innerHTML = '<div class="usage-empty">还没有用量事件。</div>';
  } else {
    let rows = '<table class="usage-table"><thead><tr><th>轮次</th><th>prompt tok</th><th>completion tok</th><th>缓存</th><th>成本</th></tr></thead><tbody>';
    let prev = { prompt: 0, completion: 0, cached: 0, cost: 0 };
    list.forEach((u, i) => {
      const dp = (u.prompt || 0) - prev.prompt;
      const dc = (u.completion || 0) - prev.completion;
      const dk = (u.cached || 0) - (prev.cached || 0);
      const cost = u.cost != null ? `¥${(Number(u.cost) - Number(prev.cost || 0)).toFixed(4)}` : '—';
      rows += `<tr><td>#${i + 1}</td><td>${dp}</td><td>${dc}</td><td>${dk || 0}</td><td>${cost}</td></tr>`;
      prev = u;
    });
    $('usage-rows').innerHTML = rows + '</tbody></table>';
  }
  $('usage-dialog').showModal();
}
function appendEvent(kind, data) {
  if (kind === 'message_delta') {
    if (data.text === null) { resetLive(); return; }
    if (!state.live) state.live = addNode('', 'bubble assistant live');
    state.liveBuf += data.text;
    scheduleLive();
    return;
  }
  if (kind === 'assistant_message') {
    resetLive();
    if (data.tool_calls && data.tool_calls.length) state.stepCount++;
    const content = (data.content || '').trim();
    if (content) addNode(renderMarkdown(content), 'bubble assistant');
    return;
  }
  if (kind === 'tool_call' || kind === 'tool_result') { appendToolEvent(kind, data); return; }
  if (kind === 'steer_queued') {
    addNode(`<span class="q-tag">已排队</span>${escape(data.text)}`, 'bubble user queued');
    return;
  }
  if (kind === 'steer_applied') {
    addNode(`<span class="q-tag">补充</span>${escape(data.text)}`, 'bubble user');
    return;
  }
  if (kind === 'user_message') { addNode(escape(data.text), 'bubble user'); return; }
  if (kind === 'interrupted') {
    addNode('<b>服务重启，这一轮被打断了</b><div>在下方输入框继续发送即可接着做，之前的经过它都记得。</div>', 'event error-card');
    return;
  }
  if (kind === 'error') {
    addNode(`<b>执行出错</b><div>${escape(data.message || '')}</div>`, 'event error-card');
    return;
  }
  if (kind === 'done') {
    addNode(`<span>本轮结束 · ${escape(STATUS_LABEL[data.stop_reason] || data.stop_reason)}</span>`, 'event done-card');
    return;
  }
}
function clearStream() {
  resetLive();
  $('stream').innerHTML = '';
  state.pending = [];
}

/* ---------- SSE ---------- */
let source = null;
function closeSource() {
  if (source) { source.close(); source = null; }
}
function openStream(sessionId, after) {
  closeSource();
  state.eventIds = new Set();
  $('conn-bar').classList.add('hidden');
  source = new EventSource(`/api/sessions/${sessionId}/stream?after=${after}`);
  source.onopen = () => { $('conn-bar').classList.add('hidden'); };
  source.onerror = () => { if (source) $('conn-bar').classList.remove('hidden'); };
  source.onmessage = (event) => {
    const envelope = JSON.parse(event.data);
    if (envelope.kind === 'open' || envelope.kind === 'heartbeat') return;
    if (envelope.kind === 'close') { closeSource(); refreshSessions().catch(() => {}); return; }
    if (state.eventIds.has(envelope.id)) return;
    state.eventIds.add(envelope.id);
    if (state.eventIds.size > 4000) state.eventIds = new Set([...state.eventIds].slice(-2000));
    if (envelope.kind === 'usage') {
      state.usageList.push(envelope.data);
      renderUsage(envelope.data);
      if (state.selected) api(`/api/sessions/${state.selected}`).then((session) => statusChip(session)).catch(() => {});
      return;
    }
    appendEvent(envelope.kind, envelope.data);
  };
}

/* ---------- 会话打开 ---------- */
async function openSession(sessionId) {
  const session = await api(`/api/sessions/${sessionId}`).catch(() => null);
  if (!session) { await refreshSessions(); return; }
  state.selected = sessionId;
  state.session = session;
  state.usageList = [];
  state.stepCount = 0;
  renderSessions();
  closeMobileSidebar();
  clearStream();
  state.stick = true;
  $('topbar').hidden = false;
  $('export-btn').classList.remove('hidden');
  $('export-btn').href = `/api/sessions/${sessionId}/export`;
  $('ws-chip').textContent = session.workspace;
  $('ws-chip').title = session.workspace;
  $('backend-chip').textContent = session.backend === 'local' ? '本机直跑' : 'Docker 容器';
  renderTopbar(session);
  statusChip(session);
  renderUsage({ calls: session.calls, prompt: (session.tokens || {}).prompt,
    completion: (session.tokens || {}).completion, cached: session.cached_tokens, cost: session.cost });
  addNode(escape(session.task || session.title || ''), 'bubble user');
  const events = await api(`/api/sessions/${sessionId}/events`);
  for (const event of events) {
    if (event.kind === 'usage') { state.usageList.push(event.data); renderUsage(event.data); continue; }
    if (event.kind === 'message_delta') continue;  // 回放跳过增量，直接看定稿
    appendEvent(event.kind, event.data);
  }
  const lastId = events.length ? events[events.length - 1].id : 0;
  openStream(sessionId, lastId);
  scrollStream(true);
}
function showWelcome() {
  state.selected = null;
  closeSource();
  clearStream();
  $('topbar').hidden = true;
  $('export-btn').classList.add('hidden');
  $('stream').innerHTML = `<div class="welcome"><h1>EasyCode</h1>
    <p>给它一个文件夹和一句任务，它会自己读代码、改文件、跑命令，做完用中文向你汇报。</p>
    <p class="welcome-hint">点左侧「＋ 新会话」开始；它工作时你随时都能打字补充要求，它做完手头这步就会照办。</p></div>`;
  refreshSessions().catch(() => {});
}

/* ---------- 新会话对话框 ---------- */
function openDialog(prefill) {
  $('new-error').textContent = '';
  $('new-task').value = prefill || '';
  $('docker-warn').classList.toggle('hidden', !!(state.config && state.config.docker_installed));
  $('new-browse').classList.toggle('hidden', !(state.config && state.config.directory_picker));
  $('new-dialog').showModal();
}
async function browseDirectory() {
  $('new-cwd-error').textContent = '';
  try {
    const result = await api('/api/pick-directory', { method: 'POST', body: JSON.stringify({ initial: $('new-cwd').value.trim() }) });
    if (result && result.source) $('new-cwd').value = result.source;
  } catch (error) { $('new-cwd-error').textContent = error.message; }
}
async function createSession() {
  const advanced = $('new-advanced').open;
  const payload = {
    cwd: $('new-cwd').value.trim(), task: $('new-task').value.trim(),
    backend: $('new-backend').value,
    tools: $('new-tools').value.split(',').map((s) => s.trim()).filter(Boolean),
    ...(advanced ? {
      max_steps: Math.max(1, Number($('new-max-steps').value) || 200),
      max_cost: Number($('new-max-cost').value) > 0 ? Number($('new-max-cost').value) : null,
      max_seconds: Math.max(10, Number($('new-max-seconds').value) || 1800),
      command_timeout: Math.max(1, Number($('new-command-timeout').value) || 60),
    } : {}),
  };
  if (!payload.cwd || !payload.task) { $('new-error').textContent = '工作目录与任务都要填。'; return; }
  try {
    const session = await api('/api/sessions', { method: 'POST', body: JSON.stringify(payload) });
    $('new-dialog').close();
    await refreshSessions();
    await openSession(session.id);
  } catch (error) { $('new-error').textContent = error.message; }
}

/* ---------- 模型设置对话框 ---------- */
async function openModelDialog() {
  $('model-error').textContent = '';
  $('model-test-result').textContent = '';
  if (!state.providers.length) {
    try { state.providers = (await api('/api/providers')).items; } catch { state.providers = []; }
  }
  try { state.modelInfo = await api('/api/model'); } catch { state.modelInfo = null; }
  const info = state.modelInfo || { provider: 'custom', base_url: '', model: '', api_key_masked: '' };
  $('model-provider').innerHTML = state.providers.map((p) =>
    `<option value="${escape(p.id)}">${escape(p.name)}</option>`).join('');
  $('model-provider').value = info.provider || 'custom';
  syncProviderFields(true);
  $('model-api-key').value = '';
  $('key-masked').textContent = info.api_key_masked ? `已保存 ${info.api_key_masked}` : '未保存';
  $('model-dialog').showModal();
}
function syncProviderFields(fill) {
  const provider = state.providers.find((p) => p.id === $('model-provider').value);
  $('provider-note').textContent = provider ? provider.note : '';
  if (fill && provider) {
    if (provider.base_url) $('model-base-url').value = provider.base_url;
    if (provider.models && provider.models.length) {
      const keep = state.modelInfo && state.modelInfo.provider === provider.id && state.modelInfo.model;
      $('model-name').value = keep || provider.models[0];
      $('model-suggestions').innerHTML = provider.models.map((m) => `<option value="${escape(m)}">`).join('');
    }
  }
}
async function testModel() {
  $('model-test-result').textContent = '测试中…';
  $('model-test-result').className = 'model-test-result';
  try {
    const result = await api('/api/model/test', { method: 'POST', body: JSON.stringify({
      model: $('model-name').value.trim(), base_url: $('model-base-url').value.trim(),
      api_key: $('model-api-key').value.trim() }) });
    $('model-test-result').textContent = (result.ok ? '✓ ' : '× ') + result.message +
      (result.latency_ms != null ? `（${result.latency_ms}ms）` : '');
    $('model-test-result').className = 'model-test-result ' + (result.ok ? 'ok' : 'bad');
  } catch (error) { $('model-test-result').textContent = '× ' + error.message; }
}
async function saveModel() {
  try {
    const info = await api('/api/model', { method: 'PUT', body: JSON.stringify({
      model: $('model-name').value.trim(), base_url: $('model-base-url').value.trim(),
      api_key: $('model-api-key').value.trim() }) });
    state.config = state.config || {};
    state.config.model = info.model;
    state.config.model_configured = info.configured;
    renderModelChip();
    $('model-dialog').close();
  } catch (error) { $('model-error').textContent = error.message; }
}
function renderModelChip() {
  const configured = state.config && state.config.model_configured;
  $('model-chip').textContent = configured ? `⚙ ${state.config.model}` : '○ 未配置模型';
  $('model-chip').classList.toggle('ready', !!configured);
}

/* ---------- composer ---------- */
async function sendComposer() {
  const text = $('composer-text').value.trim();
  $('composer-error').textContent = '';
  if (!text) return;
  $('composer-text').value = ''; autoGrow();
  if (state.selected) {
    const session = state.sessions.find((s) => s.id === state.selected) || await api(`/api/sessions/${state.selected}`);
    if (!TERMINAL.has(session.status)) {
      try {
        await api(`/api/sessions/${state.selected}/steer`, { method: 'POST', body: JSON.stringify({ text }) });
        appendEvent('steer_queued', { text });
        const fresh = await api(`/api/sessions/${state.selected}`);
        statusChip(fresh);
      } catch (error) { toast(`发送失败：${error.message}`, 'error'); }
      return;
    }
    // 终态会话：continue——同一会话带着完整历史继续（pi 语义）
    try {
      await api(`/api/sessions/${state.selected}/continue`, { method: 'POST', body: JSON.stringify({ text }) });
      await refreshSessions();
      await openSession(state.selected);
    } catch (error) { toast(`发送失败：${error.message}`, 'error'); }
    return;
  }
  openDialog(text);
}
function autoGrow() {
  const box = $('composer-text');
  box.style.height = 'auto';
  box.style.height = Math.min(box.scrollHeight, 160) + 'px';
}

/* ---------- 移动端抽屉 ---------- */
function closeMobileSidebar() { document.body.classList.remove('drawer-open'); }
$('nav-toggle').onclick = () => document.body.classList.toggle('drawer-open');
$('scrim').onclick = closeMobileSidebar;

/* ---------- 复制按钮（事件委托） ---------- */
document.addEventListener('click', async (e) => {
  const button = e.target.closest('[data-copy]');
  if (!button) return;
  const pre = button.parentElement.querySelector('pre');
  if (!pre) return;
  try {
    await navigator.clipboard.writeText(pre.textContent);
    button.textContent = '已复制';
  } catch { button.textContent = '复制失败'; }
  setTimeout(() => { button.textContent = '复制'; }, 1500);
});

/* ---------- 快捷键（输入框内不抢按键） ---------- */
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    const box = $('session-search');
    if (box) { box.focus(); box.select(); }
    return;
  }
  const target = e.target;
  const typing = target && target.matches && (target.matches('input, textarea, select') || target.isContentEditable);
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === '/') { e.preventDefault(); $('composer-text').focus(); }
});

/* ---------- 接线 ---------- */
$('session-search').addEventListener('input', () => {
  state.sessionFilter = $('session-search').value;
  renderSessions();
});
$('composer-text').addEventListener('input', autoGrow);
$('composer-text').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendComposer(); }
});
$('composer-send').onclick = sendComposer;
$('stream').addEventListener('scroll', () => { state.stick = nearBottom(); });
$('new-session').onclick = () => openDialog('');
$('new-cancel').onclick = () => $('new-dialog').close();
$('new-create').onclick = createSession;
$('new-browse').onclick = browseDirectory;
$('cancel-btn').onclick = async () => {
  if (!state.selected) return;
  try { await api(`/api/sessions/${state.selected}/cancel`, { method: 'POST', body: '{}' }); }
  catch (error) { toast(`停止失败：${error.message}`, 'error'); }
};
$('usage').onclick = openUsageDialog;
$('usage-close').onclick = () => $('usage-dialog').close();
$('model-chip').onclick = openModelDialog;
$('model-provider').onchange = () => syncProviderFields(true);
$('model-test').onclick = testModel;
$('model-save').onclick = saveModel;
$('model-close').onclick = () => $('model-dialog').close();

(async function init() {
  applyTheme(localStorage.getItem('easycode-theme') || 'system');
  state.config = await api('/api/config').catch(() => null);
  renderModelChip();
  await refreshSessions().catch(() => toast('会话列表加载失败，请确认服务已启动', 'error'));
})();
