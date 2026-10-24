const $ = (id) => document.getElementById(id);
const state = { fixtures: [], config: {}, selected: null, stream: null, cursor: 0, eventIds: new Set(), editingRepository: null, directoryAuthorized: false, repositoryProtected: [] };
const terminal = new Set(['passed', 'failed', 'error', 'cancelled', 'interrupted', 'budget_exhausted', 'blocked', 'reviewed']);
const statusNames = { queued: '等待执行', running: '修复进行中', verifying: '独立验收中', passed: '验收通过', failed: '验收未通过', error: '执行失败', cancelled: '已停止', interrupted: '运行已中断', budget_exhausted: '预算已耗尽' };
statusNames.blocked = '暂未解决';
statusNames.reviewed = '检查完成 · 未提交补丁';
statusNames.running = 'Agent 执行中';
const tools = { list_files: '查看文件', search: '搜索代码', read_file: '读取文件', symbols: '定位符号', edit_file: '修改源代码', run_tests: '运行开发测试', update_plan: '更新修复计划', record_finding: '记录修复假设', get_patch: '审查当前补丁', rollback_edit: '撤销最近修改', submit: '提交候选补丁', finish: '报告未解决原因' };
Object.assign(tools, {record_candidate:'登记候选问题', resolve_candidate:'判断候选问题', write_reproducer:'构造最小复现', run_reproducer:'验证问题假设', finish:'结束并报告调查结果', update_plan:'调整行动计划'});
const escape = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '请求参数无效，请检查输入。');
  return body;
}
function setFixture() {
  const fixture = state.fixtures.find(f => f.id === $('fixture').value);
  if (fixture) {
    $('issue').value = fixture.issue; $('fixture-description').textContent = fixture.description;
    const custom = fixture.kind === 'local';
    $('mode').querySelector('[value="demo"]').disabled = custom;
    if (custom) $('mode').value = 'model';
    $('task-kind').value = custom ? 'inspect' : 'repair';
    $('repository-tools').classList.toggle('hidden', !custom);
    $('edit-repository').classList.toggle('hidden', !custom);
    $('repository-source').textContent = fixture.source || '';
    renderPreflight('repository-preflight', fixture.preflight);
    $('allow-local').checked = false;
    setTaskKind();
  }
}
function setBackend() { $('local-consent').classList.toggle('hidden', $('backend').value !== 'local'); }
function setMode() {
  if ($('mode').value === 'demo' && $('task-kind').value === 'inspect') { $('task-kind').value = 'repair'; setTaskKind(); return; }
  $('mode-note').innerHTML = $('mode').value === 'demo'
    ? '<span>ⓘ</span><p>脚本演示使用预设动作检查流程，不代表大模型修复能力。</p>'
    : '<span>ⓘ</span><p>模型自主选择工具，根据观察调整计划。生成的复现测试用于验证假设；最终补丁另行验收。</p>';
}
function setTaskKind() {
  const inspect = $('task-kind').value === 'inspect';
  if (inspect) { $('mode').value = 'model'; $('strategy').value = 'feedback'; }
  $('strategy').disabled = inspect;
  $('issue-label').textContent = inspect ? '检查关注点（可选）' : '问题描述';
  $('issue').placeholder = inspect ? '可留空，或填写关注的模块、边界输入和行为约定…' : '描述预期行为、实际结果，以及复现条件…';
  $('issue').value = inspect ? '' : (state.fixtures.find(f => f.id === $('fixture').value)?.issue || '');
  $('task-kind-note').textContent = inspect ? 'Agent 自主调查候选问题、构造复现，只修复证据成立的问题；也可能报告未发现可确认问题。' : '根据问题描述定位、复现和修复。';
  if (['20','32'].includes($('steps').value)) $('steps').value = inspect ? '32' : '20';
  $('start').innerHTML = inspect ? '启动自主检查 <span>↗</span>' : '启动修复 <span>↗</span>';
  setMode();
}
function renderPreflight(id, report) {
  const el = $(id);
  if (!report) { el.innerHTML = ''; return; }
  const collected = report.checks.some(c => c.id === 'collection' && c.status === 'pass');
  const label = report.ready ? (collected ? '预检及测试收集通过' : '静态预检通过 · 测试导入尚未检查') : '预检发现需要处理的问题';
  const backend = report.backend === 'local' ? '本地 Python' : 'Docker';
  el.innerHTML = `<div class="preflight-summary ${report.ready ? '' : 'blocked'}">${escape(label)}<div>${escape(backend)} · ${escape(new Date(report.checked_at * 1000).toLocaleString())}</div></div><details ${report.ready ? '' : 'open'}><summary>检查明细与处理建议</summary><ul>${report.checks.map(c => `<li class="${escape(c.status)}"><strong>${c.status === 'pass' ? '✓' : c.status === 'error' ? '×' : '△'} ${escape(c.label)}</strong>${escape(c.detail)}</li>`).join('')}</ul>${report.collection ? `<pre>${escape(report.collection.stdout || report.collection.stderr)}</pre>` : ''}</details>`;
}
async function refreshRepositories(selected) {
  state.fixtures = await api('/api/repositories');
  const groups = [['builtin', '内置演示仓库'], ['local', '我的本地仓库']];
  $('fixture').innerHTML = groups.map(([kind, label]) => `<optgroup label="${label}">${state.fixtures.filter(f => f.kind === kind).map(f => `<option value="${escape(f.id)}">${escape(f.name)}</option>`).join('')}</optgroup>`).join('');
  if (selected && state.fixtures.some(f => f.id === selected)) $('fixture').value = selected;
  setFixture();
}
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
      test_targets:entry.profile.development_tests, test_origin:'已保存配置', notes:[]});
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
  const id = $('fixture').value;
  $('form-error').textContent = '';
  if (collect && $('backend').value === 'local' && !$('allow-local').checked) { $('form-error').textContent = '请先确认只在本机运行可信代码。'; return; }
  ['check-repository', 'collect-repository', 'start'].forEach(key => $(key).disabled = true);
  try {
    const report = await api(`/api/repositories/${id}/preflight`, {method:'POST', body:JSON.stringify({backend:$('backend').value, allow_local:$('allow-local').checked, collect_tests:collect})});
    const fixture = state.fixtures.find(f => f.id === id); if (fixture) fixture.preflight = report;
    if ($('fixture').value === id) renderPreflight('repository-preflight', report);
  } catch (e) { $('form-error').textContent = e.message; }
  finally { ['check-repository', 'collect-repository', 'start'].forEach(key => $(key).disabled = false); }
}
async function refreshTasks() {
  const tasks = await api('/api/tasks');
  $('task-list').innerHTML = tasks.length ? tasks.map(t => `<button class="task-item ${state.selected === t.id ? 'selected' : ''}" data-id="${escape(t.id)}">${escape(t.title)}<small>${escape(statusNames[t.status] || t.status)} · ${t.mode === 'demo' ? '脚本演示' : '真实模型'}</small></button>`).join('') : '<p class="quiet">尚无任务</p>';
  $('task-list').querySelectorAll('button').forEach(b => b.onclick = () => openTask(b.dataset.id));
}
function renderTask(task) {
  $('empty').classList.add('hidden'); $('run-content').classList.remove('hidden');
  $('run-title').textContent = task.title;
  $('status').textContent = statusNames[task.status] || task.status;
  $('status').className = 'status ' + task.status;
  $('run-meta').textContent = `${task.task_kind === 'inspect' ? '自主检查并修复' : '修复指定问题'} / ${task.mode === 'demo' ? '脚本演示，非真实模型' : task.model} / ${task.backend === 'local' ? '可信本地执行，无容器隔离' : 'Docker'} / ${task.strategy === 'feedback' ? '反馈迭代' : '单次补丁尝试'}`;
  $('metric-calls').textContent = task.model_calls ?? '—';
  $('metric-tokens').textContent = task.mode === 'demo' ? '不适用' : (Number(task.tokens?.prompt || 0) + Number(task.tokens?.completion || 0)).toLocaleString();
  $('metric-time').textContent = task.seconds != null ? task.seconds + 's' : '进行中';
  $('cancel').classList.toggle('hidden', terminal.has(task.status));
  $('resume').classList.toggle('hidden', !(task.status === 'interrupted' && task.resumeable));
  renderWorkflow(task.workflow);
  renderCandidates(task.workflow, task.id);
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
    $('verification').innerHTML = html;
  } else if (task.error || (terminal.has(task.status) && task.status !== 'passed')) {
    $('verification').textContent = task.error || task.summary || task.result || statusNames[task.status];
  }
  if (terminal.has(task.status)) {
    $('download-report').href = `/api/tasks/${task.id}/artifacts/report`; $('download-report').classList.remove('hidden');
  }
}
function renderWorkflow(w) {
  $('workflow').classList.toggle('hidden', !w);
  if (!w) return;
  const phases = {inspecting:'理解仓库', investigating:'调查问题假设', reviewed:'调查结束', repairing:'分析反馈', testing:'验证当前修改', ready_to_submit:'开发测试通过 · 提交前仍需检查', submitted:'已提交候选', blocked:'检查阻塞原因'};
  const categories = {ok:'通过', test_failure:'断言失败', code_error:'语法错误', collection_error:'收集失败', environment_error:'环境故障', timeout:'超时', cancelled:'取消', output_limit:'输出超限', workspace_changed_during_test:'测试期间代码被改动'};
  const baseline = w.baseline_test ? (w.reproduction_confirmed ? '已观察到失败' : (w.baseline_test.category === 'ok' ? '通过，尚未复现缺陷' : categories[w.baseline_test.category])) : '尚未执行';
  const stale = (w.submission_blockers || []).some(b => b.startsWith('tests_not_current'));
  const current = stale ? '当前版本待测试' : w.latest_test ? categories[w.latest_test.category] : '尚未执行';
  $('workflow').innerHTML = `<div class="workflow-head"><strong>${escape(phases[w.phase] || w.phase)}</strong><span>代码版本 ${w.revision}</span></div><div class="workflow-facts"><span>原始测试：${escape(baseline)}</span><span>最近测试：${escape(current)}</span></div>${w.hypothesis ? `<p><b>修复假设 · 基于版本 ${w.hypothesis.revision}</b> ${escape(w.hypothesis.summary)}</p>` : ''}`;
}
function renderCandidates(w, taskId = state.selected) {
  const candidates = w?.candidates || [];
  $('candidates').classList.toggle('hidden', !candidates.length);
  const labels = {suspected:'待验证', confirmed:'已复现断言不匹配', rejected:'假设已否定', inconclusive:'证据不足'};
  $('candidates').innerHTML = candidates.map(c => `<details class="candidate" ${c.status === 'confirmed' ? 'open' : ''}><summary><b>${escape(c.id)} · ${escape(labels[c.status] || c.status)}</b><span>${escape(c.summary)}</span></summary><p><b>预期：</b>${escape(c.expectation)}</p><p><b>依据：</b>${escape(c.basis)} (${escape((c.basis_paths || []).join(', '))})</p><p>涉及源码：${escape(c.paths.join(', '))}</p>${c.resolution ? `<p>处置：${escape(c.resolution)}</p>` : ''}${c.baseline_probe ? `<p>原始复现：${escape(c.baseline_probe.category)} · 最近复现：${escape(c.latest_probe.category)} / 代码版本 ${c.latest_probe.revision}</p>` : ''}${c.artifact ? `<a href="/api/tasks/${escape(taskId)}/artifacts/reproducer-${escape(c.id)}">下载复现测试 ↗</a>` : ''}<small>行为依据由模型解释，复现成功不代表独立确认了业务要求。</small></details>`).join('');
}
function streamBubble(step) {
  let el = document.getElementById('stream-' + step);
  if (!el) {
    el = document.createElement('div'); el.className = 'event streaming'; el.id = 'stream-' + step;
    el.innerHTML = `<div class="event-top"><span class="event-title">模型输出 · 流式</span><span>STEP ${step}</span></div><pre></pre>`;
    $('timeline').appendChild(el);
  }
  return el;
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
  if (event.kind === 'model_output') {
    const bubble = streamBubble(d.step), body = bubble.querySelector('pre');
    if (d.delta == null) body.textContent = '输出中断，正在重试…';
    else body.textContent += d.delta;
    return;
  }
  if (event.kind === 'action' || event.kind === 'format_error') document.getElementById('stream-' + d.step)?.remove();
  const title = event.kind === 'action' ? tools[d.action.tool] : event.kind === 'observation' ? (d.ok ? '获得执行反馈' : '发现问题，等待调整') : event.kind === 'started' ? (d.resumed ? '任务续跑' : '任务开始') : ({format_error:'动作格式需修正', verification_started:'开始独立验收', verification:'验收完成', finished:'任务结束', error:'执行异常', cancel_requested:'请求停止', interrupted:'服务重启'}[event.kind] || event.kind);
  const detail = event.kind === 'action' ? d.note : event.kind === 'observation' ? (d.error || d.category) : d.detail || d.note || statusNames[d.status] || '';
  const el = document.createElement('div'); el.className = 'event' + (d.ok === false || ['error','format_error'].includes(event.kind) ? ' error' : '');
  el.innerHTML = `<div class="event-top"><span class="event-title">${escape(title)}</span>${d.step ? `<span>STEP ${d.step}</span>` : ''}</div><p>${escape(detail)}</p><details><summary>查看记录</summary><pre>${escape(JSON.stringify(d, null, 2))}</pre></details>`;
  $('timeline').appendChild(el);
}
async function openTask(id) {
  if (state.stream) state.stream.close();
  state.selected = id; state.cursor = 0; state.eventIds.clear();
  $('timeline').innerHTML = ''; $('plan').classList.add('hidden'); $('patch').textContent = '提交后显示补丁。';
  $('verification').textContent = '独立验收结果尚未生成。';
  $('download-patch').classList.add('hidden'); $('download-report').classList.add('hidden');
  $('workflow').classList.add('hidden');
  $('candidates').classList.add('hidden');
  const task = await api(`/api/tasks/${id}`);
  if (state.selected !== id) return;
  renderTask(task);
  const events = await api(`/api/tasks/${id}/events`);
  if (state.selected !== id) return;
  events.forEach(appendEvent);
  if (!terminal.has(task.status)) {
    state.stream = new EventSource(`/api/tasks/${id}/stream?after=${state.cursor}`);
    state.stream.onmessage = async (message) => {
      if (state.selected !== id) return;
      const event = JSON.parse(message.data); appendEvent(event);
      if (['verification', 'finished', 'error'].includes(event.kind)) {
        const latest = await api(`/api/tasks/${id}`);
        if (state.selected !== id) return;
        renderTask(latest); await refreshTasks();
        if (event.kind === 'finished') state.stream.close();
      }
    };
  }
  await refreshTasks();
}
$('fixture').onchange = setFixture; $('backend').onchange = setBackend; $('mode').onchange = setMode;
$('task-kind').onchange = setTaskKind;
$('register-repository').onclick = () => openRepositoryDialog();
$('edit-repository').onclick = async () => {
  try { openRepositoryDialog(await api(`/api/repositories/${$('fixture').value}`)); }
  catch(e) { $('form-error').textContent = e.message; }
};
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
$('cancel').onclick = async () => { if (state.selected) await api(`/api/tasks/${state.selected}/cancel`, { method:'POST' }); };
$('resume').onclick = async () => {
  if (!state.selected) return;
  $('resume').disabled = true;
  try {
    const task = await api(`/api/tasks/${state.selected}/resume`, { method: 'POST' });
    await openTask(task.id); await refreshTasks();
  } catch (e) { $('form-error').textContent = e.message; }
  finally { $('resume').disabled = false; }
};
$('start').onclick = async () => {
  $('form-error').textContent = '';
  if ($('backend').value === 'local' && !$('allow-local').checked) { $('form-error').textContent = '请先确认只在本机运行可信代码。'; return; }
  if ($('mode').value === 'model' && !state.config.model_configured) { $('form-error').textContent = '尚未配置真实模型，请按 README 配置项目2独立的 .env 后重启。'; return; }
  $('start').disabled = true;
  try {
    const task = await api('/api/tasks', { method:'POST', body:JSON.stringify({ fixture:$('fixture').value,
      issue:$('issue').value, task_kind:$('task-kind').value, mode:$('mode').value, backend:$('backend').value, strategy:$('strategy').value,
      allow_local:$('allow-local').checked, limits:{ max_steps:Number($('steps').value), command_seconds:Number($('timeout').value),
        max_tokens:$('task-kind').value === 'inspect' ? 60000 : 30000, max_seconds:$('task-kind').value === 'inspect' ? 600 : 300 } }) });
    await openTask(task.id);
  } catch(e) { $('form-error').textContent = e.message; }
  finally { $('start').disabled = false; }
};
document.querySelectorAll('.tab').forEach(button => button.onclick = () => {
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('selected', b === button));
  document.querySelectorAll('.tab-content').forEach(el => el.classList.toggle('hidden', el.id !== 'tab-' + button.dataset.tab));
});
async function init() {
  state.config = await api('/api/config');
  $('model-status').textContent = state.config.model_configured ? '● ' + state.config.model : '○ 真实模型待配置';
  $('model-status').classList.toggle('ready', state.config.model_configured);
  if (!state.config.docker_installed) $('backend').value = 'local';
  await refreshRepositories(); setBackend(); await refreshTasks();
}
init().catch(e => $('form-error').textContent = e.message);
