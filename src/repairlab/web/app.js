const $ = (id) => document.getElementById(id);
const state = { fixtures: [], config: {}, workspace: null, selected: null, stream: null, cursor: 0,
  eventIds: new Set(), editingRepository: null, directoryAuthorized: false, repositoryProtected: [], runMode: 'repair' };
const terminal = new Set(['passed', 'failed', 'error', 'cancelled', 'interrupted', 'budget_exhausted', 'blocked', 'reviewed']);
const statusNames = { queued: '等待执行', running: '修复进行中', verifying: '独立验收中', passed: '验收通过', failed: '验收未通过', error: '执行失败', cancelled: '已停止', interrupted: '运行已中断', budget_exhausted: '预算已耗尽' };
statusNames.blocked = '暂未解决';
statusNames.reviewed = '检查完成 · 未提交补丁';
statusNames.running = 'Agent 执行中';
const tools = { list_files: '查看文件', search: '搜索代码', read_file: '读取文件', symbols: '定位符号', edit_file: '修改源代码', run_tests: '运行开发测试', update_plan: '更新修复计划', record_finding: '记录修复假设', get_patch: '审查当前补丁', rollback_edit: '撤销最近修改', submit: '提交候选补丁', finish: '报告未解决原因' };
Object.assign(tools, {record_candidate:'登记候选问题', resolve_candidate:'判断候选问题', write_reproducer:'构造最小复现', run_reproducer:'验证问题假设', finish:'结束并报告调查结果', update_plan:'调整行动计划', references:'定位引用', outline:'查看仓库骨架'});
const TEST_CATEGORIES = {ok:'通过', test_failure:'断言失败', code_error:'语法错误', collection_error:'收集失败', environment_error:'环境故障', timeout:'超时', cancelled:'取消', output_limit:'输出超限', workspace_changed_during_test:'测试期间代码被改动', insufficient_test_evidence:'测试证据不完整', reproducer_error:'复现测试自身错误', reproducer_no_assertion_result:'复现缺少断言结果', probe_workspace_changed:'复现期间代码被改动'};
const escape = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  let body = null;
  try { body = await response.json(); } catch { /* non-JSON error body (e.g. plain-text 500) */ }
  if (!response.ok) throw new Error(body?.detail && typeof body.detail === 'string' ? body.detail : `请求失败（HTTP ${response.status}）`);
  return body;
}
const workspaceEntry = () => state.fixtures.find(f => f.id === state.workspace);

/* ---------- 工作区（目录）与侧栏 ---------- */
function buildWorkspaceMenu() {
  const menu = $('workspace-menu');
  const groups = [['builtin', '内置演示仓库'], ['local', '本地仓库']];
  menu.innerHTML = groups.map(([kind, label]) => `<div class="ws-group">${label}</div>` +
    state.fixtures.filter(f => f.kind === kind).map(f =>
      `<button class="ws-option ${f.id === state.workspace ? 'selected' : ''}" data-id="${escape(f.id)}">${escape(f.name)}</button>`).join('')).join('') +
    `<div class="ws-sep"></div><button id="register-repository" class="ws-action">＋ 接入本地仓库（选择目录）</button>` +
    (workspaceEntry()?.kind === 'local' ? `<button id="edit-repository" class="ws-action">修改当前仓库配置</button>` : '');
  menu.querySelectorAll('.ws-option').forEach(b => b.onclick = () => { setWorkspace(b.dataset.id); closeWorkspaceMenu(); });
  menu.querySelector('#register-repository').onclick = () => { closeWorkspaceMenu(); openRepositoryDialog(); };
  const edit = menu.querySelector('#edit-repository');
  if (edit) edit.onclick = async () => { closeWorkspaceMenu(); try { openRepositoryDialog(await api(`/api/repositories/${state.workspace}`)); } catch(e) { $('form-error').textContent = e.message; } };
}
function closeWorkspaceMenu() { $('workspace-menu').classList.add('hidden'); }
$('workspace-select').onclick = (e) => {
  e.stopPropagation();
  const menu = $('workspace-menu');
  if (menu.classList.contains('hidden')) {
    const rect = $('workspace-select').getBoundingClientRect();
    menu.style.top = rect.bottom + 8 + 'px';
    menu.style.left = rect.left + 'px';
    menu.style.width = Math.max(rect.width, 300) + 'px';
  }
  menu.classList.toggle('hidden');
};
document.addEventListener('click', (e) => { if (!e.target.closest('#workspace-menu')) closeWorkspaceMenu(); });
function setWorkspace(id, { keepComposer = false } = {}) {
  state.workspace = id;
  localStorage.setItem('repairlab.workspace', id);
  const entry = workspaceEntry();
  $('workspace-name').textContent = entry?.name || id;
  $('hero-note').textContent = entry
    ? (entry.kind === 'local' ? `${entry.name} · 选择上方运行模式后发送；检查工具在输入框左侧 ＋ 中，模型与环境在设置中。`
      : `当前工作区：${entry.name}${entry.description ? ' · ' + String(entry.description).slice(0, 60) : ''}`)
    : $('hero-note').textContent;
  const custom = entry?.kind === 'local';
  $('repository-tools').classList.toggle('hidden', !custom);
  // ＋ 只承载对话相关功能；内置演示仓库没有可放的项，直接隐藏按钮避免空菜单。
  $('composer-plus').classList.toggle('hidden', !custom);
  $('repository-source').textContent = entry?.source || '';
  renderPreflight('repository-preflight', entry?.preflight);
  $('allow-local').checked = false;
  if (!keepComposer) $('composer-text').value = '';
  buildWorkspaceMenu();
  refreshTasks().catch(() => {});
}
async function refreshRepositories(selected) {
  state.fixtures = await api('/api/repositories');
  const wanted = selected || localStorage.getItem('repairlab.workspace');
  if (state.fixtures.some(f => f.id === wanted)) setWorkspace(wanted);
  else if (state.fixtures.length) setWorkspace(state.fixtures[0].id);
}
async function refreshTasks() {
  const tasks = await api(`/api/tasks?fixture=${encodeURIComponent(state.workspace)}`);
  $('task-list').innerHTML = tasks.length ? tasks.map(t => `<button class="task-item ${state.selected === t.id ? 'selected' : ''}" data-id="${escape(t.id)}">${escape(t.title)}<small>${escape(statusNames[t.status] || t.status)} · ${t.mode === 'demo' ? '脚本演示' : '真实模型'}${t.round > 1 ? ` · ${t.round} 轮` : ''}</small></button>`).join('') : '<p class="quiet">本工作区尚无任务。<br>在下方输入框描述问题开始第一个。</p>';
  $('task-list').querySelectorAll('button').forEach(b => b.onclick = () => openTask(b.dataset.id));
}

/* ---------- 仓库接入对话框（沿用） ---------- */
const splitLines = id => $(id).value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
function registrationPayload() {
  return {profile: {name: $('repo-name').value.trim(), source: $('repo-source').value.trim(),
    description: $('repo-description').value.trim(), editable: splitLines('repo-editable'), protected: state.repositoryProtected,
    development_tests: splitLines('repo-tests'), regression_tests: splitLines('repo-regression'),
      required_acceptance_tests: splitLines('repo-required-acceptance'), required_regression_tests: splitLines('repo-required-regression'),
    acceptance: $('repo-acceptance').value.trim() || null, image: $('repo-image').value.trim(), issue: $('repo-issue').value.trim()},
    backend: $('repo-backend').value, allow_local: false,
    authorize_directory: state.directoryAuthorized, collect_tests: false};
}
function registrationBusy(busy) {
  $('repository-fields').disabled = busy;
  $('close-repository').disabled = busy;
  $('inspect-registration').disabled = busy || !state.directoryAuthorized;
  $('save-repository').disabled = busy || !state.directoryAuthorized || !splitLines('repo-editable').length || !splitLines('repo-tests').length || !splitLines('repo-regression').length;
  $('repository-dialog').dataset.busy = String(busy);
}
function renderPreflight(id, report) {
  const el = $(id);
  if (!report) { el.innerHTML = ''; return; }
  const collected = report.checks.some(c => c.id === 'collection' && c.status === 'pass');
  const label = report.ready ? (collected ? '预检及测试收集通过' : '静态预检通过 · 测试导入尚未检查') : '预检发现需要处理的问题';
  const backend = report.backend === 'local' ? '本地 Python' : 'Docker';
  el.innerHTML = `<div class="preflight-summary ${report.ready ? '' : 'blocked'}">${escape(label)}<div>${escape(backend)} · ${escape(new Date(report.checked_at * 1000).toLocaleString())}</div></div><details ${report.ready ? '' : 'open'}><summary>检查明细与处理建议</summary><ul>${report.checks.map(c => `<li class="${escape(c.status)}"><strong>${c.status === 'pass' ? '✓' : c.status === 'error' ? '×' : '△'} ${escape(c.label)}</strong>${escape(c.detail)}</li>`).join('')}</ul>${report.collection ? `<pre>${escape(report.collection.stdout || report.collection.stderr)}</pre>` : ''}</details>`;
}
function renderDiscovery(profile, summary) {
  $('repo-selected-path').textContent = profile.source;
  $('repository-discovery').classList.remove('hidden');
  $('repository-discovery').innerHTML = `<h3>${escape(profile.name)}</h3><div class="discovery-facts"><span>${summary.source_count != null ? summary.source_count + ' 个源码文件' : '源码范围已保存'}</span><span>${summary.test_count != null ? summary.test_count + ' 个测试文件' : '测试位置已保存'}</span>${summary.file_count != null ? `<span>${summary.file_count} 个文本文件</span>` : ''}</div><p>测试位置：${escape(summary.test_targets.join('、') || '尚未找到')} · ${escape(summary.test_origin)}</p>${summary.notes.map(note => `<p class="discovery-warning">${escape(note)}</p>`).join('')}`;
}
function populateRepository(profile) {
  for (const [id, key] of [['repo-name','name'],['repo-source','source'],['repo-description','description'],['repo-acceptance','acceptance'],['repo-image','image'],['repo-issue','issue']]) $(id).value = profile[key] || '';
  for (const [id, key] of [['repo-editable','editable'],['repo-tests','development_tests'],['repo-regression','regression_tests']]) $(id).value = profile[key].join('\n');
  state.repositoryProtected = profile.protected || [];
  $('repo-required-acceptance').value = (profile.required_acceptance_tests || []).join('\n');
  $('repo-required-regression').value = (profile.required_regression_tests || []).join('\n');
}
function openRepositoryDialog(entry = null) {
  $('repository-form').reset(); state.editingRepository = entry?.id || null;
  state.directoryAuthorized = !!entry; state.repositoryProtected = [];
  $('repository-dialog-title').textContent = entry ? '修改本地仓库配置' : '接入本地仓库';
  $('save-repository').textContent = entry ? '保存配置 ↗' : '接入仓库 ↗';
  $('repository-advanced').open = false;
  $('repo-selected-path').textContent = '尚未选择文件夹'; $('repository-discovery').classList.add('hidden');
  if (entry) {
    populateRepository(entry.profile);
    renderDiscovery(entry.profile, {source_count:entry.preflight?.editable_files?.length ?? null, test_count:entry.profile.protected?.length || null,
      test_targets: entry.profile.development_tests, test_origin:'已保存配置', notes:[]});
  }
  $('repo-backend').value = $('backend').value;
  $('repository-error').textContent = ''; renderPreflight('registration-preflight', null);
  if (!state.config.directory_picker) $('repository-advanced').open = true;
  registrationBusy(false);
  $('repository-dialog').showModal();
}
async function discoverDirectory(nativePicker) {
  $('repository-error').textContent = ''; registrationBusy(true);
  $('choose-repository-directory').querySelector('strong').textContent = nativePicker ? '正在打开目录选择窗口…' : '正在识别项目…';
  try {
    const result = await api(nativePicker ? '/api/repositories/pick-directory' : '/api/repositories/discover',
      {method:'POST', body:JSON.stringify(nativePicker ? {initial:$('repo-source').value} : {source:$('repo-source').value.trim(), authorize_directory:true})});
    if (result.cancelled) return;
    const issue = $('repo-issue').value;
    populateRepository(result.profile); $('repo-issue').value = issue;
    state.directoryAuthorized = true;
    renderDiscovery(result.profile, result.summary);
    renderPreflight('registration-preflight', null);
    if (result.summary.needs_configuration) {
      $('repository-advanced').open = true;
      $('repository-error').textContent = result.summary.notes.join('\n');
    } else {
      const report = await api('/api/repositories/preflight', {method:'POST', body:JSON.stringify(registrationPayload())});
      renderPreflight('registration-preflight', report);
    }
  } catch (e) { $('repository-error').textContent = e.message; }
  finally {
    $('choose-repository-directory').querySelector('strong').textContent = $('repo-source').value ? '重新选择文件夹' : '选择文件夹';
    registrationBusy(false);
  }
}
async function submitRegistration(save) {
  if (!$('repository-form').reportValidity()) return;
  $('repository-error').textContent = '';
  const body = registrationPayload();
  if (!body.authorize_directory) { $('repository-error').textContent = '请先选择文件夹，让系统识别仓库。'; return; }
  registrationBusy(true);
  try {
    const id = state.editingRepository;
    const result = await api(save ? (id ? `/api/repositories/${id}` : '/api/repositories') : '/api/repositories/preflight',
      {method: save && id ? 'PUT' : 'POST', body: JSON.stringify(body)});
    if (save) {
      $('repository-dialog').close(); await refreshRepositories(result.id);
      $('backend').value = body.backend; setBackend();
    } else renderPreflight('registration-preflight', result);
  } catch (e) { $('repository-error').textContent = e.message; }
  finally { registrationBusy(false); }
}
async function checkSelectedRepository(collect) {
  const id = state.workspace;
  $('form-error').textContent = '';
  if (collect && $('backend').value === 'local' && !$('allow-local').checked) { promptLocalConsent(); return; }
  ['check-repository', 'collect-repository', 'composer-send'].forEach(key => $(key).disabled = true);
  try {
    const report = await api(`/api/repositories/${id}/preflight`, {method:'POST', body:JSON.stringify({backend:$('backend').value, allow_local:$('allow-local').checked, collect_tests:collect})});
    const fixture = workspaceEntry(); if (fixture) { fixture.preflight = report; renderPreflight('repository-preflight', report); }
  } catch (e) { $('form-error').textContent = e.message; }
  finally { ['check-repository', 'collect-repository', 'composer-send'].forEach(key => $(key).disabled = false); }
}

/* ---------- 任务渲染 ---------- */
function renderTask(task) {
  $('run-title-sm').textContent = task.title;
  $('status').textContent = statusNames[task.status] || task.status;
  $('status').className = 'status ' + task.status;
  $('run-meta').textContent = `${task.task_kind === 'inspect' ? '自主检查并修复' : '修复指定问题'} / ${task.mode === 'demo' ? '脚本演示，非真实模型' : task.model} / ${task.backend === 'local' ? '可信本地执行，无容器隔离' : 'Docker'} / ${task.strategy === 'feedback' ? '反馈迭代' : '单次补丁尝试'}`;
  $('metric-calls').textContent = task.model_calls ?? '—';
  $('metric-tokens').textContent = task.mode === 'demo' ? '不适用' : (Number(task.tokens?.prompt || 0) + Number(task.tokens?.completion || 0)).toLocaleString();
  $('metric-time').textContent = task.seconds != null ? task.seconds + 's' : '进行中';
  $('cancel').classList.toggle('hidden', terminal.has(task.status));
  $('resume').classList.toggle('hidden', !(task.status === 'interrupted' && task.resumeable));
  if (task.patch) {
    $('patch').innerHTML = task.patch.split('\n').map(line => `<span class="${line.startsWith('+') ? 'code-add' : line.startsWith('-') ? 'code-remove' : ''}">${escape(line)}</span>`).join('\n');
    $('download-patch').href = `/api/tasks/${task.id}/artifacts/patch`; $('download-patch').classList.remove('hidden');
  }
  const v = task.verification;
  if (v) {
    let html = `<div class="verification-banner ${v.passed ? '' : 'failed'}">${v.passed ? (v.independent_acceptance ? '✓ 补丁已通过外置验收与回归' : '✓ 补丁已通过干净副本回归（无外置验收）') : '补丁未通过最终验证'}<div style="font-size:11px;margin-top:4px">${escape(v.reason)} · ${escape((v.changed_files || []).join(', '))}</div></div>`;
    for (const [key, label] of [['acceptance', '验收用例'], ['regression', '回归测试']]) {
      const r = v[key];
      if (r) {
        const evidence = r.test_evidence;
        const policy = r.policy;
        const nodes = (evidence?.nodes || []).map(n => `${n.outcome}  ${n.nodeid}`).join('\n');
        const issues = policy ? [...(policy.missing || []).map(n => `缺失：${n}`), ...(policy.not_passed || []).map(n => `未通过：${n}`)].join('\n') : '';
        html += `<div class="verification-case"><strong>${label}</strong><small>${policy ? (policy.passed ? 'PASS' : 'FAIL') : '历史记录 · 无节点核验'} / ${r.seconds}s</small><p>${evidence?.valid ? `已记录 ${(evidence.nodes || []).length} 个测试节点；关键用例 ${policy?.required_count || 0} 项` : '缺少有效结构化测试报告'}</p>${issues ? `<pre>${escape(issues)}</pre>` : ''}<details><summary>测试节点与结果</summary><pre>${escape(nodes || '无节点记录')}</pre></details><details><summary>执行日志</summary><pre>${escape(r.stdout || r.stderr)}</pre></details></div>`;
      }
    }
    for (const probe of v.generated_reproducers || []) html += `<div class="verification-case"><strong>复现 ${escape(probe.id)} · 生成测试，非独立验收依据</strong><small>${probe.evidence.category === 'ok' ? 'PASS' : 'FAIL'}</small><pre>${escape(probe.result.stdout || probe.result.stderr)}</pre></div>`;
    if (task.status === 'passed' && task.patch && !BUILTIN_FIXTURES.has(task.fixture)) {
      html += `<div class="apply-controls"><button id="apply-patch" class="secondary">应用到原始仓库 ↗</button><button id="undo-apply" class="secondary hidden">撤销应用 ↩</button><span id="apply-note" class="apply-note"></span></div>`;
    }
    $('verification').innerHTML = html;
    bindApplyControls(task);
  } else if (task.error || (terminal.has(task.status) && task.status !== 'passed')) {
    $('verification').textContent = task.error || task.summary || task.result || statusNames[task.status];
  }
  if (terminal.has(task.status)) {
    $('download-report').href = `/api/tasks/${task.id}/artifacts/report`; $('download-report').classList.remove('hidden');
  }
  updateComposerMode(task);
}
function renderWorkflow(w) {
  $('workflow').classList.toggle('hidden', !w);
  if (!w) return;
  const phases = {inspecting:'理解仓库', investigating:'调查问题假设', reviewed:'调查结束', repairing:'分析反馈', testing:'验证当前修改', ready_to_submit:'开发测试通过 · 提交前仍需检查', submitted:'已提交候选', blocked:'检查阻塞原因'};
  const baseline = w.baseline_test ? (w.reproduction_confirmed ? '已观察到失败' : (w.baseline_test.category === 'ok' ? '通过，尚未复现缺陷' : TEST_CATEGORIES[w.baseline_test.category])) : '尚未执行';
  const stale = (w.submission_blockers || []).some(b => b.startsWith('tests_not_current'));
  const current = stale ? '当前版本待测试' : w.latest_test ? TEST_CATEGORIES[w.latest_test.category] : '尚未执行';
  $('workflow').innerHTML = `<div class="workflow-head"><strong>${escape(phases[w.phase] || w.phase)}</strong><span>代码版本 ${w.revision}</span></div><div class="workflow-facts"><span>原始测试：${escape(baseline)}</span><span>最近测试：${escape(current)}</span></div>${w.hypothesis ? `<p><b>修复假设 · 基于版本 ${w.hypothesis.revision}</b> ${escape(w.hypothesis.summary)}</p>` : ''}`;
}
const BUILTIN_FIXTURES = new Set(['boundary-discount', 'csv-tags', 'pagination', 'investigation']);
function bindApplyControls(task) {
  const applyBtn = document.getElementById('apply-patch');
  if (!applyBtn) return;
  const note = document.getElementById('apply-note');
  const undoBtn = document.getElementById('undo-apply');
  if (task.applied) {
    applyBtn.disabled = true; applyBtn.textContent = '补丁已应用';
    undoBtn.classList.remove('hidden');
    note.textContent = '补丁已应用到原始仓库，可撤销恢复原文件。';
  }
  applyBtn.onclick = async () => {
    if (applyBtn.dataset.armed !== '1') {
      applyBtn.dataset.armed = '1';
      applyBtn.textContent = '确认覆盖原始仓库文件？再次点击执行';
      applyBtn.classList.add('danger');
      return;
    }
    applyBtn.disabled = true;
    try {
      const result = await api(`/api/tasks/${task.id}/apply`, { method: 'POST', body: JSON.stringify({ confirm: true }) });
      note.textContent = `已应用 ${result.applied.length} 个文件到 ${result.source}；可撤销恢复原文件。`;
      applyBtn.textContent = '补丁已应用';
      undoBtn.classList.remove('hidden');
      await refreshTasks().catch(() => {});
    } catch (e) {
      note.textContent = e.message;
      applyBtn.disabled = false;
    }
  };
  undoBtn.onclick = async () => {
    undoBtn.disabled = true;
    try {
      const result = await api(`/api/tasks/${task.id}/apply/undo`, { method: 'POST', body: '{}' });
      note.textContent = `已撤销：恢复 ${result.restored.length} 个原始文件。`;
      applyBtn.disabled = false; applyBtn.textContent = '应用到原始仓库 ↗'; applyBtn.dataset.armed = '';
      applyBtn.classList.remove('danger');
      undoBtn.classList.add('hidden');
    } catch (e) { note.textContent = e.message; }
  };
}
function renderCandidates(w, taskId = state.selected) {  const candidates = w?.candidates || [];
  $('candidates').classList.toggle('hidden', !candidates.length);
  const labels = {suspected:'待验证', confirmed:'已复现断言不匹配', rejected:'假设已否定', inconclusive:'证据不足'};
  $('candidates').innerHTML = candidates.map(c => `<details class="candidate" ${c.status === 'confirmed' ? 'open' : ''}><summary><b>${escape(c.id)} · ${escape(labels[c.status] || c.status)}</b><span>${escape(c.summary)}</span></summary><p><b>预期：</b>${escape(c.expectation)}</p><p><b>依据：</b>${escape(c.basis)} (${escape((c.basis_paths || []).join(', '))})</p><p>涉及源码：${escape(c.paths.join(', '))}</p>${c.resolution ? `<p>处置：${escape(c.resolution)}</p>` : ''}${c.baseline_probe ? `<p>原始复现：${escape(c.baseline_probe.category)} · 最近复现：${escape(c.latest_probe.category)} / 代码版本 ${c.latest_probe.revision}</p>` : ''}${c.artifact ? `<a href="/api/tasks/${escape(taskId)}/artifacts/reproducer-${escape(c.id)}">下载复现测试 ↗</a>` : ''}<small>行为依据由模型解释，复现成功不代表独立确认了业务要求。</small></details>`).join('');
}

/* ---------- 对话事件流 ---------- */
function streamBubble(step) {
  let el = document.getElementById('stream-' + step);
  if (!el) {
    el = document.createElement('div'); el.className = 'event streaming'; el.id = 'stream-' + step;
    el.innerHTML = `<div class="event-top"><span class="event-title">模型输出 · 流式</span><span>STEP ${step}</span></div><pre></pre>`;
    $('timeline').appendChild(el);
  }
  return el;
}
const MILESTONE_META = { hypothesis: ['◇', '修复假设'], change: ['✎', '代码修改'], rollback: ['↩', '撤销修改'],
  test_result: ['⚙', '测试结果'], repro: ['⊙', '问题复现'], candidate: ['?', '候选问题'],
  finding: ['§', '候选处置'], submitted: ['⇪', '已提交候选'], report: ['❏', '检查报告'], verdict: ['✓', '最终结果'] };
const fmtCounts = counts => Object.entries(counts || {}).map(([k, v]) => `${v} ${k}`).join(' · ');
function appendMilestone(d) {
  const [icon, label] = MILESTONE_META[d.kind] || ['●', d.kind];
  let body = '';
  if (d.kind === 'hypothesis') {
    body = `<p><b>${d.revision ? `第 ${d.revision} 版修复假设` : '针对原始代码的修复假设'}：</b>${escape(d.summary)}</p><p class="milestone-paths">涉及 ${escape((d.paths || []).join('、'))}</p><small>这是模型对缺陷的解释，最终以独立验收为准。</small>`;
  } else if (d.kind === 'change') {
    body = `<p>${escape(d.note || '应用了一处源码修改。')}</p><p class="milestone-paths">修改 ${escape(d.path)}</p>`;
  } else if (d.kind === 'rollback') {
    body = `<p>已撤销 ${escape(d.path)} 的最近修改；此前的测试证据过期，需要重新测试。</p>`;
  } else if (d.kind === 'test_result') {
    const scope = d.baseline ? '原始代码（修改前）' : d.revision ? `第 ${d.revision} 版` : '当前版本';
    const subset = d.scope === 'partial' ? `（部分测试：${(d.targets || []).join('、')}）` : '';
    const counts = fmtCounts(d.counts);
    body = `<p><b>${scope}测试${subset}：${TEST_CATEGORIES[d.category] || d.category}</b>${counts ? ` · ${counts}` : ''}</p>`;
    if ((d.failed_nodes || []).length) body += `<pre>${escape(d.failed_nodes.slice(0, 3).join('\n'))}</pre>`;
  } else if (d.kind === 'repro') {
    body = d.first_probe
      ? (d.confirmed
        ? `<p><b>复现成功：</b>在原始代码上断言失败，候选 ${escape(d.candidate)} 确认为真实缺陷。</p><p>预期：${escape(d.expectation)}</p>`
        : `<p><b>复现未成立：</b>${TEST_CATEGORIES[d.category] || d.category}（候选 ${escape(d.candidate)}），需重新检查假设。</p><p>预期：${escape(d.expectation)}</p>`)
      : (d.category === 'ok'
        ? `<p><b>复现通过：</b>修改后问题不再出现（候选 ${escape(d.candidate)}）。</p>`
        : `<p><b>复现仍失败：</b>${TEST_CATEGORIES[d.category] || d.category}，本次修改未解决问题（候选 ${escape(d.candidate)}）。</p>`);
  } else if (d.kind === 'candidate') {
    body = `<p><b>可疑行为（候选 ${escape(d.candidate)}）：</b>${escape(d.summary)}</p><p><b>预期：</b>${escape(d.expectation)}</p><p><b>依据：</b>${escape(d.basis)}</p><p class="milestone-paths">涉及 ${escape((d.paths || []).join('、'))}</p>`;
  } else if (d.kind === 'finding') {
    body = d.status === 'rejected' ? `<p><b>假设已否定：</b>${escape(d.resolution)}</p>` : `<p><b>证据不足，暂不处理：</b>${escape(d.resolution)}</p>`;
  } else if (d.kind === 'submitted') {
    body = `<p>${escape(d.summary)}</p><p>候选补丁已提交；独立验收将在干净副本上重跑验收与回归测试。</p>`;
  } else if (d.kind === 'report') {
    body = `<p>${escape(d.summary)}</p>`;
  } else if (d.kind === 'verdict') {
    if (d.passed === true) body = `<p><b>✓ 最终结果：补丁通过独立验收，任务完成。</b></p><p>${escape(d.reason || '')}${(d.changed_files || []).length ? ` · 修改 ${escape(d.changed_files.join('、'))}` : ''}</p>`;
    else if (d.passed === false) body = `<p><b>✗ 最终结果：补丁未通过独立验收。</b></p><p>${escape(d.reason || '')}</p>`;
    else body = `<p><b>任务结束：${statusNames[d.status] || d.status}</b></p><p>${escape(d.reason || '')}</p>`;
    if (d.summary) body += `<p><b>Agent 总结：</b>${escape(d.summary)}</p>`;
  }
  const state = d.kind === 'verdict' ? (d.passed === true ? ' passed' : d.passed === false ? ' failed' : '') : '';
  const el = document.createElement('div'); el.className = 'milestone ' + d.kind + state;
  el.innerHTML = `<div class="event-top"><span class="milestone-tag">${icon} ${label}</span>${d.step ? `<span>STEP ${d.step}</span>` : ''}</div>${body}<details><summary>查看数据</summary><pre>${escape(JSON.stringify(d, null, 2))}</pre></details>`;
  $('timeline').appendChild(el);
}
function appendEvent(event) {
  if (state.eventIds.has(event.id)) return;
  state.eventIds.add(event.id); state.cursor = Math.max(state.cursor, event.id);
  const d = event.data;
  if (event.kind === 'workflow_state') { renderWorkflow(d); renderCandidates(d); return; }
  if (event.kind === 'plan') {
    $('plan').classList.remove('hidden'); $('plan').innerHTML = `<strong>当前行动计划</strong>${d.reason ? `<p>${escape(d.reason)}</p>` : ''}<ol>${d.items.map(item => `<li>${escape(item)}</li>`).join('')}</ol>`;
    return;
  }
  if (event.kind === 'thinking') return;
  if (event.kind === 'instruction') {
    const divider = document.createElement('div'); divider.className = 'round-divider';
    divider.innerHTML = `<span>第 ${d.round} 轮 · 用户追加指令</span>`;
    const bubble = document.createElement('div'); bubble.className = 'chat-user'; bubble.innerHTML = `<p>${escape(d.text)}</p>`;
    $('timeline').append(divider, bubble);
    return;
  }
  if (event.kind === 'model_output') {
    const bubble = streamBubble(d.step), body = bubble.querySelector('pre');
    if (d.delta == null) body.textContent = '输出中断，正在重试…';
    else body.textContent += d.delta;
    return;
  }
  if (event.kind === 'milestone') { appendMilestone(d); return; }
  if (event.kind === 'action' || event.kind === 'format_error') document.getElementById('stream-' + d.step)?.remove();
  const title = event.kind === 'action' ? tools[d.action.tool] : event.kind === 'observation' ? (d.ok ? '获得执行反馈' : '发现问题，等待调整') : event.kind === 'started' ? (d.follow_up ? `第 ${d.round || 2} 轮开始` : d.resumed ? '任务续跑' : '任务开始') : ({format_error:'动作格式需修正', verification_started:'开始独立验收', verification:'验收完成', finished:'任务结束', error:'执行异常', cancel_requested:'请求停止', interrupted:'服务重启'}[event.kind] || event.kind);
  const detail = event.kind === 'action' ? d.note : event.kind === 'observation' ? (d.error || d.category) : d.detail || d.note || statusNames[d.status] || '';
  const el = document.createElement('div'); el.className = 'event' + (d.ok === false || ['error','format_error'].includes(event.kind) ? ' error' : '');
  el.innerHTML = `<div class="event-top"><span class="event-title">${escape(title)}</span>${d.step ? `<span>STEP ${d.step}</span>` : ''}</div><p>${escape(detail)}</p><details><summary>查看记录</summary><pre>${escape(JSON.stringify(d, null, 2))}</pre></details>`;
  $('timeline').appendChild(el);
}
function scrollConversationBottom() {
  const pane = $('main-scroll');
  pane.scrollTop = pane.scrollHeight;
}
async function openTask(id) {
  if (state.stream) state.stream.close();
  state.selected = id; state.cursor = 0; state.eventIds.clear();
  $('hero').classList.add('hidden'); $('run-panel').classList.remove('hidden');
  $('new-task').classList.remove('active'); window.scrollTo(0, 0);
  $('timeline').innerHTML = ''; $('plan').classList.add('hidden'); $('patch').textContent = '提交后显示补丁。';
  $('verification').textContent = '独立验收结果尚未生成。';
  $('download-patch').classList.add('hidden'); $('download-report').classList.add('hidden');
  $('workflow').classList.add('hidden');
  $('candidates').classList.add('hidden');
  const task = await api(`/api/tasks/${id}`);
  if (state.selected !== id) return;
  renderTask(task);
  workspaceLoadedFor = null; $('workspace-files').innerHTML = ''; $('workspace-content').textContent = '选择左侧文件查看只读内容。';
  $('workspace-file-head').classList.add('hidden');
  if (task.issue) {
    const divider = document.createElement('div'); divider.className = 'round-divider'; divider.innerHTML = '<span>第 1 轮 · 任务描述</span>';
    const bubble = document.createElement('div'); bubble.className = 'chat-user'; bubble.innerHTML = `<p>${escape(task.issue)}</p>`;
    $('timeline').append(divider, bubble);
  }
  const events = await api(`/api/tasks/${id}/events`);
  if (state.selected !== id) return;
  events.forEach(appendEvent);
  if (!terminal.has(task.status)) {
    state.stream = new EventSource(`/api/tasks/${id}/stream?after=${state.cursor}`);
    state.stream.onmessage = async (message) => {
      if (state.selected !== id) return;
      const event = JSON.parse(message.data); appendEvent(event);
      scrollConversationBottom();
      if (['verification', 'finished', 'error'].includes(event.kind)) {
        const latest = await api(`/api/tasks/${id}`);
        if (state.selected !== id) return;
        renderTask(latest); await refreshTasks();
        if (event.kind === 'finished') state.stream.close();
      }
    };
  }
  scrollConversationBottom();
  await refreshTasks();
}
function showHero() {
  if (state.stream) state.stream.close();
  state.selected = null;
  $('hero').classList.remove('hidden'); $('run-panel').classList.add('hidden');
  $('new-task').classList.add('active');
  updateComposerMode(null);
  $('composer-text').focus();
  refreshTasks().catch(() => {});
}
$('new-task').onclick = showHero;

/* ---------- 工作区文件浏览 ---------- */
let workspaceLoadedFor = null;
async function loadWorkspaceFiles(taskId) {
  if (workspaceLoadedFor === taskId) return;
  const box = $('workspace-files');
  box.innerHTML = '<p class="quiet ws-note">加载中…</p>';
  try {
    const data = await api(`/api/tasks/${taskId}/workspace/files`);
    workspaceLoadedFor = taskId;
    const head = `<div class="ws-files-head">工作区文件 · ${data.files.length}</div>`;
    if (!data.files.length) {
      box.innerHTML = head + '<p class="quiet ws-note">工作区没有可展示的文本文件。</p>';
      return;
    }
    box.innerHTML = head + data.files.map(f =>
      `<button class="ws-file" data-path="${escape(f.path)}"><span class="path">${escape(f.path)}</span><small>${f.size} B</small></button>`).join('');
    box.querySelectorAll('button').forEach(b => b.onclick = () => openWorkspaceFile(taskId, b));
    openWorkspaceFile(taskId, box.querySelector('button'));
  } catch (e) { box.innerHTML = `<p class="quiet ws-note">${escape(e.message)}</p>`; }
}
async function openWorkspaceFile(taskId, button) {
  const path = button.dataset.path;
  const box = button.parentElement;
  box.querySelectorAll('.ws-file.selected').forEach(x => x.classList.remove('selected'));
  button.classList.add('selected');
  $('workspace-file-head').classList.remove('hidden');
  $('workspace-file-head').textContent = path;
  const view = $('workspace-content');
  view.textContent = '加载中…';
  try {
    const data = await api(`/api/tasks/${taskId}/workspace/file?path=${encodeURIComponent(path)}`);
    view.textContent = data.text + (data.total_lines > 400 ? `\n…（共 ${data.total_lines} 行，仅显示前 400 行）` : '');
  } catch (e) { view.textContent = e.message; }
}
document.querySelectorAll('.tab').forEach(button => button.onclick = () => {
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('selected', b === button));
  document.querySelectorAll('.tab-content').forEach(el => el.classList.toggle('hidden', el.id !== 'tab-' + button.dataset.tab));
  if (button.dataset.tab === 'workspace' && state.selected) loadWorkspaceFiles(state.selected);
});

/* ---------- 底部输入框：新建任务 / 追加指令 ---------- */
function setBackend() {
  const local = $('backend').value === 'local';
  $('local-consent').classList.toggle('hidden', !local);
  document.querySelectorAll('#backend-seg button').forEach(b => b.classList.toggle('active', b.dataset.backend === $('backend').value));
}
function setRunMode(mode) {
  state.runMode = mode;
  document.querySelectorAll('#composer-launch .preset-chip').forEach(c => c.classList.toggle('active', c.dataset.mode === mode));
  $('composer-text').placeholder = mode === 'repair'
    ? '描述要修复的问题：预期行为、实际结果、复现条件…'
    : '直接发送即开始全面自主检查；也可填写关注的模块、边界输入和行为约定…';
}
function updateComposerMode(task) {
  // task === null → composing a new task; otherwise follow-up mode bound to the task's state.
  document.body.classList.toggle('launch', task === null);
  $('composer-launch').classList.toggle('hidden', task !== null);
  closePlusMenu();
  if (!task) {
    $('composer-send').disabled = false;
    $('composer-send').title = '发送（Enter 发送，Shift+Enter 换行）';
    $('composer-hint').textContent = 'Enter 发送 · Shift+Enter 换行';
    setRunMode(state.runMode || 'repair');
    return;
  }
  const canFollowUp = terminal.has(task.status) && task.mode === 'model';
  $('composer-send').disabled = !canFollowUp;
  $('composer-text').placeholder = task.mode !== 'model' ? '脚本演示任务不支持追加指令。'
    : canFollowUp ? '追加指令，Agent 在同一工作区继续…' : 'Agent 执行中，本轮结束后可继续下达指令…';
  $('composer-hint').textContent = task.mode !== 'model' ? '脚本演示任务不支持追加指令；真实模型任务可在结束后继续对话。'
    : terminal.has(task.status) ? '任务已结束，可继续下达指令，Agent 将在同一工作区开启新一轮。'
    : 'Agent 执行中，本轮结束后可继续下达指令…';
}
function autoGrow() {
  const box = $('composer-text');
  box.style.height = 'auto';
  box.style.height = Math.min(box.scrollHeight, 180) + 'px';
}
$('composer-text').addEventListener('input', autoGrow);
$('composer-text').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('composer-send').click(); }
});
async function sendComposer() {
  const text = $('composer-text').value.trim();
  $('form-error').textContent = '';
  if (state.selected) {
    if (!text) return;
    $('composer-send').disabled = true;
    try {
      const task = await api(`/api/tasks/${state.selected}/follow-up`, { method: 'POST', body: JSON.stringify({ text }) });
      $('composer-text').value = ''; autoGrow();
      await openTask(task.id); await refreshTasks();
    } catch (e) { $('form-error').textContent = e.message; }
    finally { updateComposerMode(await api(`/api/tasks/${state.selected}`).catch(() => null)); }
    return;
  }
  // 运行模式由上方预设按钮选择：修复模式需要问题描述，自主检查可直接发送。
  const inspect = state.runMode === 'inspect';
  if (!inspect && !text) { $('form-error').textContent = '请描述要修复的问题；也可切换为自主检查并修复。'; $('composer-text').focus(); return; }
  if ($('backend').value === 'local' && !$('allow-local').checked) { promptLocalConsent(); return; }
  if (!state.config.model_configured) { $('form-error').textContent = '尚未配置真实模型，请点击右上角模型名打开设置。'; return; }
  $('composer-send').disabled = true;
  try {
    const task = await api('/api/tasks', { method: 'POST', body: JSON.stringify({
      fixture: state.workspace, issue: text, task_kind: inspect ? 'inspect' : 'repair', mode: 'model',
      backend: $('backend').value, strategy: 'feedback', allow_local: $('allow-local').checked,
      limits: { max_steps: inspect ? Math.max(Number($('steps').value) || 20, 32) : Number($('steps').value),
        command_seconds: Number($('timeout').value),
        max_tokens: inspect ? 60000 : 30000,
        max_seconds: inspect ? 600 : 300 } }) });
    $('composer-text').value = ''; autoGrow();
    await openTask(task.id); await refreshTasks();
  } catch (e) { $('form-error').textContent = e.message; }
  finally { $('composer-send').disabled = false; }
}
$('composer-send').onclick = () => sendComposer().catch(e => $('form-error').textContent = e.message);

/* ---------- 其余控件 ---------- */
$('close-repository').onclick = () => $('repository-dialog').close();
$('repository-dialog').oncancel = e => { if ($('repository-dialog').dataset.busy === 'true') e.preventDefault(); };
$('choose-repository-directory').onclick = () => discoverDirectory(true);
$('discover-repository-path').onclick = () => discoverDirectory(false);
$('repository-form').oninput = e => {
  renderPreflight('registration-preflight', null);
  if (e.target.id === 'repo-source') {
    state.directoryAuthorized = false; $('repository-discovery').classList.add('hidden');
    $('repo-selected-path').textContent = '目录已变更，请点击“读取并识别”。';
  }
  registrationBusy(false);
};
$('inspect-registration').onclick = () => submitRegistration(false);
$('repository-form').onsubmit = e => { e.preventDefault(); submitRegistration(true); };
$('check-repository').onclick = () => checkSelectedRepository(false);
$('collect-repository').onclick = () => checkSelectedRepository(true);
$('refresh').onclick = () => refreshTasks().catch(e => $('form-error').textContent = e.message);
$('cancel').onclick = async () => { if (state.selected) await api(`/api/tasks/${state.selected}/cancel`, { method: 'POST' }); };
$('resume').onclick = async () => {
  if (!state.selected) return;
  $('resume').disabled = true;
  try {
    const task = await api(`/api/tasks/${state.selected}/resume`, { method: 'POST' });
    await openTask(task.id); await refreshTasks();
  } catch (e) { $('form-error').textContent = e.message; }
  finally { $('resume').disabled = false; }
};
$('backend-seg').querySelectorAll('button').forEach(b => b.onclick = () => { $('backend').value = b.dataset.backend; setBackend(); });
function closePlusMenu() { $('composer-plus-menu').classList.add('hidden'); }
function openPlusMenu() {
  const menu = $('composer-plus-menu');
  if (menu.classList.contains('hidden')) {
    const rect = $('composer-plus').getBoundingClientRect();
    // 高度按加号上方的可用空间钳制，菜单再长也只在自身内部滚动，不会顶出屏幕
    const available = rect.top - 70;
    menu.style.left = Math.max(12, rect.left - 6) + 'px';
    menu.style.bottom = window.innerHeight - rect.top + 10 + 'px';
    menu.style.maxHeight = Math.max(200, available - 10) + 'px';
    menu.classList.remove('hidden');
  }
}
function pulseConsent() {
  const c = $('local-consent');
  c.classList.remove('attention'); void c.offsetWidth; c.classList.add('attention');
  c.scrollIntoView({ block: 'center' });
}
function promptLocalConsent() {
  $('form-error').textContent = '请在运行设置中确认「此仓库可信」后重试。';
  openProjectSettings(true);
}
$('composer-plus').onclick = (e) => {
  e.stopPropagation();
  $('composer-plus-menu').classList.contains('hidden') ? openPlusMenu() : closePlusMenu();
};
document.addEventListener('click', (e) => { if (!e.target.closest('#composer-plus-menu')) closePlusMenu(); });
$('preset-repair').onclick = () => setRunMode('repair');
$('preset-inspect').onclick = () => setRunMode('inspect');
$('allow-local').onchange = setBackend;
$('backend').onchange = setBackend;

/* ---------- 模型设置 ---------- */
async function renderModelStatus() {
  state.config = await api('/api/config');
  const chip = $('model-chip');
  chip.textContent = state.config.model_configured ? state.config.model : '○ 配置模型';
  chip.classList.toggle('ready', state.config.model_configured);
  chip.title = state.config.model_configured ? '选择或配置模型' : '点击配置模型';
}
const providerById = id => (state.providers || []).find(p => p.id === id);
function syncProviderFields(fill) {
  const p = providerById($('model-provider').value) || providerById('custom');
  if (fill && p) {
    if (p.base_url) $('model-base-url').value = p.base_url;
    if (p.models && p.models.length) $('model-name').value = p.models[0];
  }
  $('model-suggestions').innerHTML = (p?.models || []).map(m => `<option value="${escape(m)}">`).join('');
  $('provider-note').innerHTML = p ? `${p.key_url ? `<a href="${escape(p.key_url)}" target="_blank" rel="noreferrer">获取 API Key ↗</a>` : ''}${p.models?.length ? `<span>常见模型：${escape(p.models.join(' / '))}</span>` : ''}${p.note ? `<span>${escape(p.note)}</span>` : ''}` : '';
}
function renderTestResult(result) {
  const el = $('model-test-result');
  if (!result) { el.innerHTML = ''; return; }
  if (result.pending) { el.innerHTML = '<div class="preflight-summary">正在发送测试请求…</div>'; return; }
  el.innerHTML = `<div class="preflight-summary ${result.ok ? '' : 'blocked'}">${result.ok ? '✓' : '×'} ${escape(result.message)}${result.latency_ms != null ? `<div>${result.latency_ms} ms</div>` : ''}</div>`;
}
async function openModelSettings() {
  $('model-settings-error').textContent = ''; renderTestResult(null);
  const [config, providers] = await Promise.all([api('/api/settings/model'), api('/api/settings/providers')]);
  state.providers = providers;
  $('model-provider').innerHTML = providers.map(p => `<option value="${escape(p.id)}">${escape(p.name)}</option>`).join('');
  $('model-base-url').value = config.base_url || ''; $('model-name').value = config.model || '';
  $('model-api-key').value = ''; $('model-api-key').type = 'password'; $('toggle-key').textContent = '显示';
  $('model-api-key').placeholder = config.api_key_masked ? `已配置（${config.api_key_masked}），留空沿用` : 'sk-…';
  $('model-provider').value = providers.some(p => p.id === config.provider) ? config.provider : 'custom';
  syncProviderFields(false);
  $('model-settings-dialog').showModal();
}
function closeModelMenu() { $('model-menu').classList.add('hidden'); }
async function openModelMenu() {
  const menu = $('model-menu');
  const data = await api('/api/settings/models');
  const items = (data.items || []).map(p =>
    `<button type="button" class="model-item ${p.active ? 'active' : ''}" data-id="${escape(p.id)}"><span class="model-item-label">${escape(p.label)}</span>${p.active ? '<span class="model-check">✓</span>' : ''}</button>`).join('');
  menu.innerHTML = (items || '<p class="model-empty">尚未保存模型配置。</p>') +
    '<div class="ws-sep"></div><button type="button" id="configure-model" class="model-item configure">⚙ 配置自定义模型…</button>';
  const rect = $('model-chip').getBoundingClientRect();
  // 高度按输入框上方可用空间钳制；右对齐到模型按钮
  menu.style.right = Math.max(12, window.innerWidth - rect.right) + 'px';
  menu.style.bottom = window.innerHeight - rect.top + 10 + 'px';
  menu.style.maxHeight = Math.max(200, rect.top - 80) + 'px';
  menu.classList.remove('hidden');
}
$('model-chip').onclick = async (e) => {
  e.stopPropagation();
  const menu = $('model-menu');
  if (!menu.classList.contains('hidden')) { closeModelMenu(); return; }
  if (!state.config.model_configured) { openModelSettings().catch(err => $('form-error').textContent = err.message); return; }
  try { await openModelMenu(); } catch (err) { $('form-error').textContent = err.message; }
};
$('model-menu').addEventListener('click', async (e) => {
  const item = e.target.closest('.model-item');
  if (!item) return;
  if (item.id === 'configure-model') { closeModelMenu(); openModelSettings().catch(err => $('form-error').textContent = err.message); return; }
  try {
    await api('/api/settings/models/activate', { method: 'POST', body: JSON.stringify({ id: item.dataset.id }) });
    closeModelMenu(); await renderModelStatus();
  } catch (err) { $('form-error').textContent = err.message; }
});
document.addEventListener('click', (e) => { if (!e.target.closest('#model-menu, #model-chip')) closeModelMenu(); });
function openProjectSettings(attentionConsent = false) {
  setBackend();
  $('project-settings-dialog').showModal();
  if (attentionConsent) pulseConsent();
}
$('project-settings').onclick = () => openProjectSettings(false);
$('close-project-settings').onclick = () => $('project-settings-dialog').close();
$('close-project-settings-ok').onclick = () => $('project-settings-dialog').close();
$('close-model-settings').onclick = () => $('model-settings-dialog').close();
$('model-provider').onchange = () => syncProviderFields(true);
$('toggle-key').onclick = () => {
  const input = $('model-api-key');
  input.type = input.type === 'password' ? 'text' : 'password';
  $('toggle-key').textContent = input.type === 'password' ? '显示' : '隐藏';
};
$('test-model').onclick = async () => {
  $('model-settings-error').textContent = ''; renderTestResult({pending: true}); $('test-model').disabled = true;
  try {
    renderTestResult(await api('/api/settings/model/test', {method:'POST', body: JSON.stringify({
      base_url: $('model-base-url').value.trim(), model: $('model-name').value.trim(), api_key: $('model-api-key').value})}));
  } catch (e) { renderTestResult({ok: false, message: e.message}); }
  finally { $('test-model').disabled = false; }
};
$('model-settings-form').onsubmit = async e => {
  e.preventDefault(); $('model-settings-error').textContent = '';
  try {
    await api('/api/settings/model', {method: 'PUT', body: JSON.stringify({
      base_url: $('model-base-url').value.trim(), model: $('model-name').value.trim(), api_key: $('model-api-key').value})});
    $('model-settings-dialog').close(); await renderModelStatus();
  } catch (e2) { $('model-settings-error').textContent = e2.message; }
};

/* ---------- 启动 ---------- */
async function init() {
  await renderModelStatus();
  if (!state.config.docker_installed) $('backend').value = 'local';
  await refreshRepositories(); setBackend(); await refreshTasks();
  updateComposerMode(null);
  $('new-task').classList.add('active');
}
init().catch(e => $('form-error').textContent = e.message);
