/* ============================================================
   EasyCode — 纯函数渲染模块（T7 拆分）
   职责：escape / markdown（含表格）/ 手写语法高亮 / 行级+词级 diff /
   工具块 HTML 构建。不访问 DOM、不持有状态——app.js 负责编排。
   安全约定：所有动态文本先 escape 再进入 HTML；高亮 tokenizer
   逐段 escape 产出，无注入面。零依赖、无构建。
   ============================================================ */
'use strict';

function escape(text) {
  return String(text ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function relTime(ts) {
  if (!ts) return '';
  const seconds = Math.max(1, Math.round(Date.now() / 1000 - ts));
  if (seconds < 90) return `${seconds}s 前`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m 前`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h 前`;
  return `${Math.round(seconds / 86400)}d 前`;
}
function safeJson(text) {
  try { return JSON.parse(text); } catch { return null; }
}

/* ---------- 手写语法高亮 ----------
   规则按优先级排列（注释→字符串→关键字→数字→函数→…），合并为一个带命名
   分组的大正则；命中哪个分组就是哪类 token，逐段 escape 产出 HTML。
   未覆盖语言返回 null，调用方回退纯色。 */
const HL_RULES = {
  python: [
    ['com', '#.*'],
    ['str', '(?:[rbuf]{0,2})(?:"""[\\s\\S]*?"""|\'\'\'[\\s\\S]*?\'\'\'|"(?:\\\\.|[^"\\\\\\n])*"|\'(?:\\\\.|[^\'\\\\\\n])*\')'],
    ['kw', '\\b(?:False|None|True|and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield|match|case|self|cls)\\b'],
    ['num', '\\b(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|\\d[\\d_]*(?:\\.[\\d_]+)?(?:[eE][+-]?\\d+)?)\\b'],
    ['func', '\\b[A-Za-z_]\\w*(?=\\s*\\()'],
    ['attr', '@[\\w.]+'],
    ['opr', '[+\\-*/%=<>!&|^~]+'],
  ],
  js: [
    ['com', '//.*|/\\*[\\s\\S]*?\\*/'],
    ['str', '`(?:\\\\.|[^`\\\\])*`|"(?:\\\\.|[^"\\\\\\n])*"|\'(?:\\\\.|[^\'\\\\\\n])*\''],
    ['kw', '\\b(?:const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|default|class|extends|super|new|this|typeof|instanceof|in|of|try|catch|finally|throw|async|await|yield|import|export|from|as|delete|void|static|get|set|null|undefined|true|false|interface|type|enum|readonly|public|private|protected)\\b'],
    ['num', '\\b(?:0[xX][0-9a-fA-F]+|\\d+(?:\\.\\d+)?(?:[eE][+-]?\\d+)?)\\b'],
    ['func', '\\b[A-Za-z_$][\\w$]*(?=\\s*\\()'],
    ['opr', '[+\\-*/%=<>!&|^~?:]+'],
  ],
  json: [
    ['attr', '"(?:\\\\.|[^"\\\\])*"(?=\\s*:)'],
    ['str', '"(?:\\\\.|[^"\\\\])*"'],
    ['kw', '\\b(?:true|false|null)\\b'],
    ['num', '-?\\b\\d+(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b'],
  ],
  bash: [
    ['com', '^#!.*|#.*'],
    ['str', '"(?:\\\\.|[^"\\\\])*"|\'[^\']*\''],
    ['var', '\\$\\{[^}]*\\}|\\$[\\w@*?#$!-]+'],
    ['kw', '\\b(?:if|then|else|elif|fi|for|while|until|do|done|case|esac|in|function|select|time|return|exit|local|set|unset|shift|read)\\b'],
    ['func', '\\b(?:echo|printf|cd|ls|cat|grep|sed|awk|rm|cp|mv|mkdir|touch|chmod|export|source|alias|which|git|docker|python|python3|pip|uv|pytest|npm|node|curl)\\b'],
    ['num', '\\b\\d+\\b'],
  ],
  html: [
    ['com', '<!--[\\s\\S]*?-->'],
    ['kw', '</?[A-Za-z][\\w-]*|/?>'],
    ['attr', '[A-Za-z-][\\w-]*(?==)'],
    ['str', '"(?:[^"\\\\]|\\\\.)*"|\'(?:[^\'\\\\]|\\\\.)*\''],
  ],
  css: [
    ['com', '/\\*[\\s\\S]*?\\*/'],
    ['kw', '@[\\w-]+'],
    ['str', '"(?:[^"\\\\]|\\\\.)*"|\'(?:[^\'\\\\]|\\\\.)*\''],
    ['attr', '[a-zA-Z-]+(?=\\s*:)'],
    ['num', '#[0-9a-fA-F]{3,8}\\b|-?\\b\\d+(?:\\.\\d+)?(?:px|em|rem|vh|vw|s|ms|%)?\\b'],
  ],
  yaml: [
    ['com', '#.*'],
    ['attr', '^[ \\t]*[\\w.-]+(?=\\s*:)'],
    ['str', '"(?:\\\\.|[^"\\\\])*"|\'[^\']*\''],
    ['kw', '\\b(?:true|false|null|yes|no|on|off)\\b'],
    ['num', '\\b\\d+(?:\\.\\d+)?\\b'],
  ],
  diff: [
    ['meta', '^(?:diff |index |--- |\\+\\+\\+ ).*$'],
    ['add', '^\\+.*$'],
    ['del', '^-.*$'],
    ['meta', '^@@[^\\n]*$'],
  ],
};
const HL_ALIAS = {
  py: 'python', python: 'python',
  js: 'js', javascript: 'js', jsx: 'js', ts: 'js', tsx: 'js', typescript: 'js', mjs: 'js',
  json: 'json',
  bash: 'bash', sh: 'bash', shell: 'bash', zsh: 'bash', console: 'bash',
  html: 'html', xml: 'html', vue: 'html',
  css: 'css', scss: 'css', less: 'css',
  yaml: 'yaml', yml: 'yaml', toml: 'yaml',
  diff: 'diff', patch: 'diff',
  text: null, txt: null, plain: null,
};
const HL_CACHE = {};
function hlPattern(lang) {
  let re = HL_CACHE[lang];
  if (!re) {
    re = new RegExp(HL_RULES[lang].map(([type, source]) => `(?<${type}>${source})`).join('|'), 'gm');
    HL_CACHE[lang] = re;
  }
  return re;
}
function highlight(code, lang) {
  if (!HL_RULES[lang]) return null;
  const re = hlPattern(lang);
  re.lastIndex = 0;
  let html = '', last = 0, m;
  while ((m = re.exec(code))) {
    if (m[0] === '') { re.lastIndex++; continue; }
    html += escape(code.slice(last, m.index));
    let type = 'opr';
    for (const key in m.groups) { if (m.groups[key] !== undefined) { type = key; break; } }
    html += `<span class="hl-${type}">${escape(m[0])}</span>`;
    last = m.index + m[0].length;
  }
  html += escape(code.slice(last));
  return html;
}

/* ---------- 轻量 markdown（先转义后生成，无注入面；链接/引用/表格） ---------- */
function mdInline(s) {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}
function renderMarkdown(text) {
  const parts = String(text ?? '').split(/```/);
  return parts.map((part, index) => index % 2 ? codeBlock(part) : mdProse(part)).join('');
}
function codeBlock(raw) {
  const nl = raw.indexOf('\n');
  const lang = (nl === -1 ? raw : raw.slice(0, nl)).trim().split(/\s+/)[0].toLowerCase();
  const code = (nl === -1 ? '' : raw.slice(nl + 1)).replace(/\n$/, '');
  const langId = Object.prototype.hasOwnProperty.call(HL_ALIAS, lang) ? HL_ALIAS[lang] : lang;
  const body = langId ? highlight(code, langId) : null;
  const label = lang ? `<span class="md-lang">${escape(lang)}</span>` : '';
  const pre = `<pre class="md-code">${body ?? escape(code)}</pre>`;
  return `<div class="md-wrap"><button class="copy-btn" data-copy>复制</button>${label}${pre}</div>`;
}
function isTableSeparator(row) {
  const cells = row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|');
  return cells.length >= 1 && cells.every((c) => /^\s*:?-+:?\s*$/.test(c));
}
function mdTableBlock(rows) {
  const parse = (row) => row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
  const header = parse(rows[0]);
  const aligns = parse(rows[1]).map((c) =>
    c.startsWith(':') && c.endsWith(':') ? ' style="text-align:center"'
      : c.endsWith(':') ? ' style="text-align:right"' : '');
  const cell = (content, i, tag) => `<${tag}${aligns[i] ?? ''}>${mdInline(escape(content))}</${tag}>`;
  let html = `<table class="md-table"><thead><tr>${header.map((c, i) => cell(c, i, 'th')).join('')}</tr></thead><tbody>`;
  for (const row of rows.slice(2)) {
    const cells = parse(row);
    html += `<tr>${header.map((_, i) => cell(cells[i] ?? '', i, 'td')).join('')}</tr>`;
  }
  return html + '</tbody></table>';
}
function mdProse(text) {
  let html = '', list = null, quote = null;
  const closeList = () => { if (list) { html += list === 'ul' ? '</ul>' : '</ol>'; list = null; } };
  const closeQuote = () => { if (quote) { html += '</blockquote>'; quote = null; } };
  const lines = String(text ?? '').split(/\r?\n/);
  for (let li = 0; li < lines.length; li++) {
    const raw = lines[li];
    const line = escape(raw.trimEnd());
    /* 表格：连续 | 行且第二行是分隔行才成表，否则按普通段落处理 */
    if (/^\s*\|.*\|\s*$/.test(line)) {
      let j = li;
      while (j < lines.length && /^\s*\|.*\|\s*$/.test(lines[j])) j++;
      const block = lines.slice(li, j);
      if (block.length >= 3 && isTableSeparator(block[1])) {
        closeQuote(); closeList();
        html += mdTableBlock(block);
        li = j - 1;
        continue;
      }
    }
    if (/^#{1,4}\s/.test(line)) { closeQuote(); closeList(); html += `<div class="md-h">${mdInline(line.replace(/^#+\s*/, ''))}</div>`; }
    else if (/^&gt;\s?/.test(line)) { if (!quote) { closeList(); html += '<blockquote class="md-quote">'; quote = true; } html += `<p class="md-p">${mdInline(line.replace(/^&gt;\s?/, ''))}</p>`; }
    else if (/^\s*[-*]\s+/.test(line)) { closeQuote(); if (list !== 'ul') { closeList(); list = 'ul'; html += '<ul class="md-list">'; } html += `<li>${mdInline(line.replace(/^\s*[-*]\s+/, ''))}</li>`; }
    else if (/^\s*\d+[.)]\s+/.test(line)) { closeQuote(); if (list !== 'ol') { closeList(); list = 'ol'; html += '<ol class="md-list">'; } html += `<li>${mdInline(line.replace(/^\s*\d+[.)]\s+/, ''))}</li>`; }
    else if (!line.trim()) { closeList(); closeQuote(); }
    else { closeQuote(); closeList(); html += `<p class="md-p">${mdInline(line)}</p>`; }
  }
  closeList(); closeQuote();
  return html;
}

/* ---------- 行级 diff（LCS + 上下文折叠）+ 词级微高亮 ---------- */
function diffOps(a, b) {
  const CAP = 250;
  if (a.length > CAP || b.length > CAP) return [...a.map(t => ['del', t]), ...b.map(t => ['add', t])];
  const dp = Array.from({ length: a.length + 1 }, () => new Uint16Array(b.length + 1));
  for (let i = a.length - 1; i >= 0; i--) for (let j = b.length - 1; j >= 0; j--)
    dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const ops = [];
  let i = 0, j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { ops.push(['same', a[i]]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { ops.push(['del', a[i++]]); }
    else { ops.push(['add', b[j++]]); }
  }
  while (i < a.length) ops.push(['del', a[i++]]);
  while (j < b.length) ops.push(['add', b[j++]]);
  return ops;
}
function diffLineHTML(type, content, isHtml) {
  if (type === 'hunk') return `<div class="diff-line hunk"><span class="diff-gutter">…</span><span class="diff-text">${escape(content)}</span></div>`;
  const sign = type === 'add' ? '+' : type === 'del' ? '−' : ' ';
  const text = isHtml ? content : (escape(content) || '&nbsp;');
  return `<div class="diff-line ${type}"><span class="diff-gutter">${sign}</span><span class="diff-text">${text}</span></div>`;
}
/* 相邻 del/add 行对做词级二次对齐，产出 <del>/<ins> 微高亮 */
function wordTokens(text) {
  return String(text).match(/\w+|\s+|[^\w\s]/g) || [];
}
function wordDiffPair(oldLine, newLine) {
  const a = wordTokens(oldLine), b = wordTokens(newLine);
  if (a.length > 400 || b.length > 400) return null;
  let delHtml = '', addHtml = '';
  for (const [type, tok] of diffOps(a, b)) {
    const esc = escape(tok);
    if (type === 'same') { delHtml += esc; addHtml += esc; }
    else if (type === 'del') delHtml += `<del>${esc}</del>`;
    else addHtml += `<ins>${esc}</ins>`;
  }
  return { del: delHtml, add: addHtml };
}
function renderInlineDiff(oldText, newText, context = 2) {
  const a = String(oldText ?? '').split('\n'), b = String(newText ?? '').split('\n');
  if (a.length && b.length && a[a.length - 1] === '' && b[b.length - 1] === '') { a.pop(); b.pop(); }
  const ops = diffOps(a, b);
  const changed = ops.map(op => op[0] !== 'same');
  const kept = ops.filter((op, index) => {
    if (changed[index]) return true;
    let distance = Infinity;
    for (let k = index - 1; k >= 0 && distance > context; k--) if (changed[k]) distance = index - k;
    for (let k = index + 1; k < ops.length && distance > context; k++) if (changed[k]) distance = Math.min(distance, k - index);
    return distance <= context;
  });
  if (!changed.some(Boolean)) return `<pre class="t-out">${escape(newText)}</pre>`;
  const out = [];
  for (let k = 0; k < kept.length; k++) {
    const [type, text] = kept[k];
    if (type !== 'del') { out.push(diffLineHTML(type, text, false)); continue; }
    /* 收集连续 del 块及其后紧跟的 add 块，逐行配对做词级高亮 */
    let d = k;
    while (d < kept.length && kept[d][0] === 'del') d++;
    let a2 = d;
    while (a2 < kept.length && kept[a2][0] === 'add') a2++;
    const delLines = kept.slice(k, d), addLines = kept.slice(d, a2);
    if (!addLines.length) { out.push(diffLineHTML('del', text, false)); continue; }
    const pairs = Math.max(delLines.length, addLines.length);
    for (let p = 0; p < pairs; p++) {
      const dl = delLines[p], al = addLines[p];
      if (dl && al) {
        const pair = wordDiffPair(dl[1], al[1]);
        out.push(diffLineHTML('del', pair ? pair.del : dl[1], !!pair));
        out.push(diffLineHTML('add', pair ? pair.add : al[1], !!pair));
      } else if (dl) {
        out.push(diffLineHTML('del', dl[1], false));
      } else {
        out.push(diffLineHTML('add', al[1], false));
      }
    }
    k = a2 - 1;
  }
  return `<div class="diff-view">${out.join('')}</div>`;
}

/* ---------- 工具块（pending → result 原位替换，由 app.js 编排） ---------- */
const TOOL_ICONS = { read: '📄', bash: '$', edit: '✎', write: '✚', ls: '▤', grep: '🔍' };
function toolArgsSummary(name, args) {
  if (!args) return '';
  if (name === 'bash') return String(args.command || '').split('\n')[0];
  if (name === 'grep') return String(args.pattern || '');
  if (name === 'read' || name === 'write' || name === 'edit' || name === 'ls') return String(args.path || '');
  return JSON.stringify(args).slice(0, 80);
}
function badge(label, cls) { return `<span class="t-badge ${cls}">${escape(label)}</span>`; }
function toolHeadHTML(data, pending) {
  const icon = TOOL_ICONS[data.name] || '⚙';
  const args = toolArgsSummary(data.name, data.arguments);
  const stateHtml = pending
    ? '<span class="t-state pending">执行中</span>'
    : (data.ok === false ? '<span class="t-state bad">!</span>' : '<span class="t-state ok">✓</span>');
  const secs = !pending && data.seconds != null ? `<span class="t-seconds">${data.seconds}s</span>` : '';
  return `<summary><span class="t-icon">${icon}</span><span class="t-name">${escape(data.name)}</span>` +
    `<span class="t-args mono">${escape(args)}</span>${stateHtml}${secs}</summary>`;
}
function copyWrap(pre) { return `<div class="md-wrap"><button class="copy-btn" data-copy>复制</button>${pre}</div>`; }
function toolBodyHTML(data) {
  const d = data.details || {};
  let html = '';
  if (data.name === 'edit' && d.old != null) {
    html = renderInlineDiff(d.old, d.new ?? (data.arguments && data.arguments.new) ?? '');
  } else if (data.name === 'write') {
    const content = String((data.arguments && data.arguments.content) ?? '');
    if (!d.created && d.before_head != null) html = renderInlineDiff(d.before_head, content);
    else html = `<div class="t-meta">新建文件 · ${escape(String(d.path || ''))}</div>` +
      copyWrap(`<pre class="t-out add-lines">${escape(content)}</pre>`);
  } else if (data.name === 'bash') {
    const flags = [badge(`exit ${d.exit_code ?? '?'}`, d.exit_code === 0 ? 'ok' : 'warn')];
    if (d.timed_out) flags.push(badge('超时', 'bad'));
    if (d.cancelled) flags.push(badge('已取消', 'bad'));
    if (d.output_limited) flags.push(badge('输出太长，已截断', 'bad'));
    html = `<div class="t-badges">${flags.join('')}</div>`;
    if (d.stdout) html += `<div class="t-io"><b>stdout</b>${copyWrap(`<pre>${escape(d.stdout)}</pre>`)}</div>`;
    if (d.stderr) html += `<div class="t-io"><b>stderr</b>${copyWrap(`<pre class="stderr">${escape(d.stderr)}</pre>`)}</div>`;
  } else if (data.name === 'read') {
    html = copyWrap(`<pre class="t-out mono-pad">${escape(data.output || '')}</pre>`);
    if (d.total_lines != null) {
      html += `<div class="t-meta">显示 ${d.start}-${d.end} / 共 ${d.total_lines} 行` +
        (d.truncated ? '（内容太长，已截断）' : '') + `</div>`;
    }
  } else {
    let meta = '';
    if (d.hits != null) meta = `<div class="t-meta">命中 ${d.hits} 处（扫描 ${d.searched_files} 个文件）</div>`;
    html = meta + copyWrap(`<pre class="t-out">${escape(data.output || '')}</pre>`);
  }
  return `<div class="t-body">${html}</div>`;
}
function toolBlockHTML(data, pending) {
  return `<details class="tool-block${pending ? ' pending' : ''}" open>${toolHeadHTML(data, pending)}` +
    (pending ? '<div class="t-body"><span class="pending-note">工具执行中…</span></div>' : toolBodyHTML(data)) + `</details>`;
}
