const state = { skills: [], runs: [], proposals: [], suites: [], jobs: [], selectedRun: null, selectedSkill: null, selectedProposal: null, selectedJob: null };

// ------------------------------------------------------------------ 基础工具
function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)skillforge_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}

async function api(method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  if (method !== 'GET') {
    headers['X-CSRF-Token'] = csrfToken();
    headers['X-Requested-With'] = 'skillforge';
  }
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { raw: text }; }
  if (!res.ok) {
    const err = data.error || { code: 'unknown', message: res.statusText };
    throw new Error(`[${err.code}] ${err.message}`);
  }
  return data;
}

function el(id) { return document.getElementById(id); }
function esc(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function alertBox(message, kind) {
  const box = el('alert');
  box.textContent = message;
  box.className = 'alert' + (kind ? ' ' + kind : '');
  if (!message) box.classList.add('hidden');
  else { box.classList.remove('hidden'); if (kind !== 'error') setTimeout(() => box.classList.add('hidden'), 6000); }
}
function statusBadge(status) {
  const ok = ['completed', 'active', 'eligible', 'activated', 'passed', 'frozen', 'done', 'ok'];
  const bad = ['failed', 'rejected', 'stale', 'blocked', 'cancelled'];
  const warn = ['running', 'queued', 'draft', 'evaluating', 'awaiting_input', 'interrupted', 'budget_stopped', 'inconclusive', 'needs_evidence'];
  let cls = '';
  if (ok.includes(status)) cls = 'ok'; else if (bad.includes(status)) cls = 'bad'; else if (warn.includes(status)) cls = 'warn';
  return `<span class="badge ${cls}">${esc(status)}</span>`;
}
function fmtTime(value) { return value ? String(value).replace('T', ' ').replace('Z', '') : '-'; }
function parseJSON(text, fallback) {
  if (!text || !text.trim()) return fallback;
  try { return JSON.parse(text); } catch (e) { throw new Error('JSON 解析失败：' + e.message); }
}

// ------------------------------------------------------------------ Tab 切换
document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    tab.classList.add('active');
    document.querySelectorAll('.panel').forEach((p) => p.classList.add('hidden'));
    el('tab-' + tab.dataset.tab).classList.remove('hidden');
    if (tab.dataset.tab === 'eval') { loadSuites(); loadEvalJobs(); }
    if (tab.dataset.tab === 'jobs') loadJobs();
    if (tab.dataset.tab === 'settings') { loadUsage(); loadMetrics(); loadAudit(); loadWorkspaces(); }
    if (tab.dataset.tab === 'learning') { loadLessons(); loadProposals(); loadLearningSettings(); }
  });
});

// ------------------------------------------------------------------ 顶部状态
async function loadHealth() {
  try {
    const data = await api('GET', '/api/health');
    const model = data.model || {};
    el('health').textContent = '服务正常 · v' + (data.version || '?');
    el('health').className = 'pill pill-ok';
    el('model-badge').textContent = model.model ? `${model.model}` : '模型未配置';
    el('model-badge').className = 'pill ' + (model.model ? 'pill-ok' : 'pill-warn');
  } catch (e) {
    el('health').textContent = '服务不可用';
    el('health').className = 'pill pill-bad';
  }
}

// ------------------------------------------------------------------ 工作区 + 技能下拉
async function loadWorkspaces() {
  const data = await api('GET', '/api/workspaces');
  const list = data.workspaces || [];
  ['run-workspace', 'import-workspace'].forEach((id) => {
    const select = el(id);
    const current = select.value;
    select.innerHTML = list.map((w) => `<option value="${esc(w.id)}">${esc(w.name)}</option>`).join('');
    if (current) select.value = current;
  });
  el('workspace-list').innerHTML = list.map((w) => `
    <li data-ws="${esc(w.id)}">
      <div class="title"><strong>${esc(w.name)}</strong>${statusBadge(w.status)}</div>
      <div class="meta">输入：${esc(w.root_path)}<br/>产物：${esc(w.artifact_path)}<br/>工具：${esc((w.allowed_tools || []).join(', '))}</div>
      <div class="row" style="margin-top:6px">
        <button class="btn btn-ghost" data-action="check-ws" data-id="${esc(w.id)}" type="button">检查访问</button>
        <button class="btn btn-ghost btn-danger" data-action="remove-ws" data-id="${esc(w.id)}" type="button">移除</button>
      </div>
    </li>`).join('') || '<li class="muted">还没有工作区，请先添加。</li>';
}

async function loadSkills() {
  const query = el('skill-search').value || '';
  const data = await api('GET', '/api/skills' + (query ? '?query=' + encodeURIComponent(query) : ''));
  state.skills = data.skills || [];
  el('skill-list').innerHTML = state.skills.map((s) => {
    const current = s.current || {};
    return `<li data-skill="${esc(s.id)}">
      <div class="title"><strong>${esc(s.name)}</strong>${statusBadge(s.status)}</div>
      <div class="meta">${esc(s.slug)} · ${esc(s.scope)}${s.workspace_id ? ' · ' + esc(s.workspace_id) : ''} · 当前版本 ${esc((current.version_id || '未启用').slice(0, 18))} (rev ${esc(current.revision ?? '-')})</div>
      <div class="meta">${esc(s.description || '')}</div>
    </li>`;
  }).join('') || '<li class="muted">还没有技能，先导入一个 SKILL.md 包或创建草稿。</li>';

  el('run-skills').innerHTML = state.skills.filter((s) => s.status === 'active')
    .map((s) => `<option value="${esc(s.id)}">${esc(s.name)} (${esc(s.scope)})</option>`).join('');
  el('suite-skill').innerHTML = state.skills
    .map((s) => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');

  document.querySelectorAll('#skill-list li').forEach((li) => li.addEventListener('click', () => showSkill(li.dataset.skill)));
}

async function showSkill(skillId) {
  state.selectedSkill = skillId;
  document.querySelectorAll('#skill-list li').forEach((li) => li.classList.toggle('selected', li.dataset.skill === skillId));
  const data = await api('GET', `/api/skills/${skillId}`);
  const skill = data.skill || {};
  const current = data.current || {};
  const versions = data.versions || [];
  const files = data.files || [];
  const activation = data.activation || {};
  el('skill-detail').innerHTML = `
    <div class="title"><h3 style="margin:0">${esc(skill.name)} <span class="muted">${esc(skill.slug)}</span></h3>
      <span>${statusBadge(skill.status)} ${activation.unverified ? '<span class="badge warn">效果未验证</span>' : ''}</span></div>
    <div class="meta muted">${esc(skill.description || '')}</div>
    <p class="hint">作用域：${esc(skill.scope)}${skill.workspace_id ? ' · 工作区 ' + esc(skill.workspace_id) : ''}；生效版本：${esc(current.version_id || '无')}（revision ${esc(current.revision ?? '-')}，来源 ${esc(current.origin || '-')}）</p>
    <div class="row">
      <button class="btn" data-action="export" data-id="${esc(skillId)}" type="button">导出完整包</button>
      <button class="btn" data-action="archive" data-id="${esc(skillId)}" type="button">归档 / 恢复</button>
      <button class="btn btn-ghost" data-action="manual-trial" data-id="${esc(skillId)}" type="button">手工试用某版本</button>
    </div>
    <h3>文件清单（当前版本）</h3>
    <table><thead><tr><th>路径</th><th>大小</th><th>hash</th></tr></thead><tbody>
      ${files.map((f) => `<tr><td class="mono">${esc(f.path)}</td><td>${esc(f.size)}</td><td class="mono">${esc(String(f.content_hash || '').slice(0, 12))}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">无文件</td></tr>'}
    </tbody></table>
    <h3>版本历史（启用记录）</h3>
    <table><thead><tr><th>revision</th><th>版本</th><th>来源</th><th>未验证</th><th>时间</th><th></th></tr></thead><tbody>
      ${(data.activations || []).map((a) => `<tr>
        <td>${esc(a.revision)}</td><td class="mono">${esc(String(a.version_id).slice(0, 18))}</td><td>${esc(a.origin)}</td>
        <td>${a.unverified ? '是' : '否'}</td><td>${esc(fmtTime(a.created_at))}</td>
        <td><button class="btn btn-ghost" data-action="rollback" data-skill="${esc(skillId)}" data-version="${esc(a.version_id)}" type="button">回退到此完整包</button></td>
      </tr>`).join('') || '<tr><td colspan="6" class="muted">尚无启用记录</td></tr>'}
    </tbody></table>
    <h3>版本列表</h3>
    <table><thead><tr><th>版本</th><th>来源</th><th>校验</th><th>文件数</th><th>包 hash</th><th>时间</th></tr></thead><tbody>
      ${versions.map((v) => `<tr><td class="mono">${esc(String(v.id).slice(0, 18))}</td><td>${esc(v.origin)}</td><td>${statusBadge(v.validation_status)}</td>
        <td>${esc(v.file_count)}</td><td class="mono">${esc(String(v.package_hash || '').slice(0, 12))}</td><td>${esc(fmtTime(v.created_at))}</td></tr>`).join('')}
    </tbody></table>`;
}

// ------------------------------------------------------------------ 任务
async function loadRuns() {
  const status = el('run-filter-status').value;
  const data = await api('GET', '/api/runs' + (status ? '?status=' + encodeURIComponent(status) : ''));
  state.runs = data.runs || [];
  el('run-list').innerHTML = state.runs.map((r) => `
    <li data-run="${esc(r.id)}">
      <div class="title"><strong>${esc((r.request || '').slice(0, 46))}</strong>${statusBadge(r.execution_status)}</div>
      <div class="meta">验收：${esc(r.outcome)} · ${esc(fmtTime(r.created_at))} · 调用 ${esc((r.usage || {}).model_calls ?? 0)} 次</div>
    </li>`).join('') || '<li class="muted">还没有任务。</li>';
  document.querySelectorAll('#run-list li').forEach((li) => li.addEventListener('click', () => showRun(li.dataset.run)));
}

async function showRun(runId) {
  state.selectedRun = runId;
  document.querySelectorAll('#run-list li').forEach((li) => li.classList.toggle('selected', li.dataset.run === runId));
  const data = await api('GET', `/api/runs/${runId}`);
  const run = data.run || {};
  const artifacts = data.artifacts || [];
  const actions = data.actions || [];
  const loads = data.loads || [];
  el('run-detail').innerHTML = `
    <div class="title"><h3 style="margin:0">${esc((run.request || '').slice(0, 80))}</h3>
      <span>${statusBadge(run.execution_status)} <span class="badge info">验收 ${esc(run.outcome)}</span></span></div>
    <p class="hint">执行状态与验收状态分离：completed ≠ accepted。</p>
    <h3>产物</h3>
    <ul class="list small">${artifacts.map((a) => `<li><div class="title"><span class="mono">${esc(a.path)}</span><span>${esc(a.size)} B</span></div></li>`).join('') || '<li class="muted">暂无产物</li>'}</ul>
    <h3>模型输出</h3>
    <pre>${esc(run.output_text || '（无）')}</pre>
    <h3>验收与纠正</h3>
    <div class="row">
      <button class="btn" data-action="accept" data-run="${esc(runId)}" data-verdict="accepted" type="button">符合</button>
      <button class="btn" data-action="accept" data-run="${esc(runId)}" data-verdict="partially_accepted" type="button">部分符合</button>
      <button class="btn" data-action="accept" data-run="${esc(runId)}" data-verdict="rejected" type="button">不符合</button>
      <button class="btn btn-ghost" data-action="cancel-run" data-run="${esc(runId)}" type="button">取消任务</button>
      <button class="btn btn-ghost" data-action="resume-run" data-run="${esc(runId)}" type="button">中断恢复</button>
    </div>
    <label>文字纠正（原话必填）<textarea id="feedback-content" rows="3" placeholder="例如：结论缺少来源引用，以后同类任务都要给出处"></textarea></label>
    <label>适用范围
      <select id="feedback-scope">
        <option value="task">仅本次任务</option>
        <option value="workspace">当前工作区同类任务</option>
        <option value="personal">个人范围（跨项目）同类任务</option>
      </select>
    </label>
    <button class="btn btn-primary" data-action="submit-correction" data-run="${esc(runId)}" type="button">提交纠正</button>
    <div id="feedback-history"></div>
    <h3>加载记录（学习归因只认这里）</h3>
    <table><thead><tr><th>版本</th><th>文件</th><th>类型</th><th>字符</th><th>状态</th></tr></thead><tbody>
      ${loads.map((l) => `<tr><td class="mono">${esc(String(l.version_id).slice(0, 14))}</td><td class="mono">${esc(l.file_path)}</td><td>${esc(l.load_kind)}</td><td>${esc(l.chars)}</td><td>${esc(l.load_status)}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">无加载记录</td></tr>'}
    </tbody></table>
    <details><summary>工具动作（${actions.length}）</summary>
      <table><thead><tr><th>#</th><th>工具</th><th>状态</th><th>副作用</th><th>耗时(ms)</th></tr></thead><tbody>
      ${actions.map((a) => `<tr><td>${esc(a.seq)}</td><td>${esc(a.tool_name)}</td><td>${statusBadge(a.status)}</td><td>${esc(a.side_effect_status)}</td><td>${esc(a.duration_ms ?? '-')}</td></tr>`).join('')}
      </tbody></table></details>
    <details><summary>事件流（${(data.events || []).length}）</summary><pre>${esc(JSON.stringify(data.events || [], null, 2))}</pre></details>
    <details><summary>运行快照（权限 / 预算 / 模型）</summary><pre>${esc(JSON.stringify({ model: run.model_snapshot, budget: run.budget, permissions: run.permissions, input: run.input_snapshot, context_hash: run.context_hash }, null, 2))}</pre></details>
    <div id="run-feedback">${renderFeedback(data.feedback || [])}</div>`;
}

function renderFeedback(list) {
  if (!list.length) return '';
  return `<h3>反馈历史</h3><table><thead><tr><th>类型</th><th>来源</th><th>范围</th><th>判定</th><th>状态</th><th>内容</th></tr></thead><tbody>
    ${list.map((f) => `<tr><td>${esc(f.type)}</td><td>${esc(f.source_kind)}</td><td>${esc(f.scope)}</td><td>${esc(f.verdict || '-')}</td><td>${statusBadge(f.status)}</td><td>${esc((f.content || '').slice(0, 60))}</td></tr>`).join('')}
  </tbody></table>`;
}

// ------------------------------------------------------------------ 学习建议
async function loadLearningSettings() {
  const data = await api('GET', '/api/learning/settings');
  el('learning-enabled').checked = !!data.enabled;
}

async function loadLessons() {
  const data = await api('GET', '/api/lessons');
  const lessons = data.lessons || [];
  el('lesson-list').innerHTML = lessons.map((l) => `
    <li data-lesson="${esc(l.id)}">
      <div class="title"><strong>${esc((l.rule_text || '').slice(0, 52))}</strong><span class="badge">${esc(l.attribution)}</span></div>
      <div class="meta">范围 ${esc(l.scope)} · 证据 ${esc(l.evidence_strength)} · 来源 ${esc(l.source_count)} 条 · ${statusBadge(l.status)}</div>
      <div class="meta">目标技能：${esc(l.target_skill_id || '待归因')}</div>
      <div class="row" style="margin-top:6px">
        <button class="btn btn-ghost" data-action="ignore-lesson" data-id="${esc(l.id)}" type="button">忽略（仅本次 / 归因错误 / 重复 / 成本不值）</button>
      </div>
    </li>`).join('') || '<li class="muted">暂无经验。提交纠正后，系统会提炼可复用的方法建议。</li>';
}

async function loadProposals() {
  const data = await api('GET', '/api/proposals');
  state.proposals = data.proposals || [];
  el('proposal-list').innerHTML = state.proposals.map((p) => `
    <li data-proposal="${esc(p.id)}">
      <div class="title"><strong>${esc((p.summary || p.reason || '').slice(0, 50))}</strong>${statusBadge(p.status)}</div>
      <div class="meta">基线 ${esc(String(p.baseline_version_id).slice(0, 16))} → 候选 ${esc(String(p.candidate_version_id || '未生成').slice(0, 16))}</div>
      <div class="meta">归因 ${esc(p.attribution)} · ${esc(fmtTime(p.created_at))}</div>
    </li>`).join('') || '<li class="muted">暂无候选修改。</li>';
  document.querySelectorAll('#proposal-list li').forEach((li) => li.addEventListener('click', () => showProposal(li.dataset.proposal)));
}

async function showProposal(proposalId) {
  state.selectedProposal = proposalId;
  document.querySelectorAll('#proposal-list li').forEach((li) => li.classList.toggle('selected', li.dataset.proposal === proposalId));
  const data = await api('GET', `/api/proposals/${proposalId}`);
  const p = data.proposal || {};
  const diff = data.diff || [];
  const checks = p.checks || {};
  const evaluation = p.evaluation || {};
  el('proposal-detail').innerHTML = `
    <div class="title"><h3 style="margin:0">候选 ${esc(proposalId)}</h3><span>${statusBadge(p.status)}</span></div>
    <p class="hint">基线：${esc(p.baseline_version_id)} → 候选：${esc(p.candidate_version_id || '未生成')}；作用域：${esc(p.scope)}</p>
    <p><strong>理由：</strong>${esc(p.reason || '-')}</p>
    <p><strong>预期收益：</strong>${esc(p.expected_benefit || '-')}</p>
    <p><strong>潜在退化：</strong>${esc(p.potential_regression || '-')}</p>
    <p><strong>能力变化（权限）：</strong><span class="mono">${esc(JSON.stringify(p.capability_change || {}))}</span></p>
    <h3>硬检查与质量提示</h3>
    <pre>${esc(JSON.stringify(checks, null, 2))}</pre>
    <h3>逐文件差异</h3>
    ${diff.map((d) => `<details><summary>${esc(d.path)} · ${esc(d.change)}</summary><pre>${renderUnified(d.diff || '')}</pre></details>`).join('') || '<p class="muted">无差异</p>'}
    <h3>评测</h3>
    <p class="hint">${esc(evaluation.verdict ? '结论：' + evaluation.verdict + '（报告 ' + (evaluation.report_id || '-') + '）' : '尚未评测')}</p>
    <div class="row">
      <button class="btn btn-primary" data-action="evaluate" data-id="${esc(proposalId)}" type="button">启动对照评测</button>
      <button class="btn" data-action="reject-proposal" data-id="${esc(proposalId)}" type="button">拒绝</button>
      <button class="btn" data-action="activate-proposal" data-id="${esc(proposalId)}" type="button">启用（需 eligible + 报告匹配）</button>
    </div>
    <h3>来源（经验 / 反馈）</h3>
    <table><thead><tr><th>经验</th><th>规则</th><th>归因</th></tr></thead><tbody>
      ${(data.sources || []).map((s) => `<tr><td class="mono">${esc(String(s.lesson_id).slice(0, 14))}</td><td>${esc(s.rule_text || '')}</td><td>${esc(s.attribution || '')}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">无来源</td></tr>'}
    </tbody></table>`;
}

function renderUnified(text) {
  return esc(text).split('\n').map((line) => {
    if (line.startsWith('+')) return `<span class="diff-add">${line}</span>`;
    if (line.startsWith('-')) return `<span class="diff-remove">${line}</span>`;
    return line;
  }).join('\n');
}

// ------------------------------------------------------------------ 评测
async function loadSuites() {
  const data = await api('GET', '/api/eval/suites');
  state.suites = data.suites || [];
}

async function loadEvalJobs() {
  const data = await api('GET', '/api/eval/jobs');
  state.jobs = data.jobs || [];
  el('eval-job-list').innerHTML = state.jobs.map((j) => `
    <li data-job="${esc(j.id)}">
      <div class="title"><strong>${esc(j.id)}</strong>${statusBadge(j.status)}</div>
      <div class="meta">结论：${esc(j.verdict || '待定')} · 基线 ${esc(String(j.baseline_version_id).slice(0, 14))} → 候选 ${esc(String(j.candidate_version_id).slice(0, 14))}</div>
      <div class="meta">${esc(fmtTime(j.created_at))}</div>
    </li>`).join('') || '<li class="muted">暂无评测作业。</li>';
  document.querySelectorAll('#eval-job-list li').forEach((li) => li.addEventListener('click', () => showEvalJob(li.dataset.job)));
}

async function showEvalJob(jobId) {
  const data = await api('GET', `/api/eval/jobs/${jobId}`);
  const job = data.job || {};
  const report = data.report || {};
  const results = data.results || [];
  el('eval-report').innerHTML = `
    <div class="title"><h3 style="margin:0">作业 ${esc(jobId)}</h3><span>${statusBadge(job.status)} ${job.verdict ? statusBadge(job.verdict) : ''}</span></div>
    <p class="hint">独立样例 ${esc(report.independent_cases ?? '-')} 个（holdout ${esc(report.holdout_cases ?? '-')}），配对 ${esc(report.complete_pairs ?? '-')}/${esc(report.planned_pairs ?? '-')}；5 样例结论只表示该冻结评测集内符合门禁，不代表统计显著。</p>
    <div class="row">
      <button class="btn btn-ghost" data-action="resume-eval" data-id="${esc(jobId)}" type="button">续跑未完成配对</button>
      <button class="btn btn-ghost" data-action="cancel-eval" data-id="${esc(jobId)}" type="button">取消作业</button>
    </div>
    <h3>结论计算</h3><pre>${esc(JSON.stringify(job.verdict_detail || {}, null, 2))}</pre>
    <h3>逐案结果</h3>
    <table><thead><tr><th>样例</th><th>次</th><th>状态</th><th>基线</th><th>候选</th><th>基线分</th><th>候选分</th></tr></thead><tbody>
      ${results.map((r) => `<tr><td class="mono">${esc(String(r.case_id).slice(0, 14))}</td><td>${esc(r.repetition)}</td><td>${statusBadge(r.result_status)}</td>
        <td class="mono">${esc(String(r.baseline_run_id || '-').slice(0, 14))}</td><td class="mono">${esc(String(r.candidate_run_id || '-').slice(0, 14))}</td>
        <td>${esc(r.baseline_score ?? '-')}</td><td>${esc(r.candidate_score ?? '-')}</td></tr>`).join('') || '<tr><td colspan="7" class="muted">暂无结果</td></tr>'}
    </tbody></table>
    <h3>成本与上限</h3><pre>${esc(JSON.stringify({ budget: job.budget, usage: job.usage }, null, 2))}</pre>`;
}

// ------------------------------------------------------------------ 作业 / 数据
async function loadJobs() {
  const data = await api('GET', '/api/jobs');
  el('job-list').innerHTML = (data.jobs || []).map((j) => `
    <li>
      <div class="title"><strong>${esc(j.type)}</strong>${statusBadge(j.status)}</div>
      <div class="meta">${esc(j.id)} · 尝试 ${esc(j.attempts)} · 优先级 ${esc(j.priority)} · ${esc(fmtTime(j.created_at))}</div>
      <div class="row" style="margin-top:6px">
        <button class="btn btn-ghost" data-action="cancel-job" data-id="${esc(j.id)}" type="button">取消</button>
        <span class="muted">${esc(j.error_message || '')}</span>
      </div>
    </li>`).join('') || '<li class="muted">暂无作业。</li>';
}

async function loadUsage() {
  const data = await api('GET', '/api/data/usage');
  const u = data.usage || {};
  el('usage').innerHTML = `<p>模型调用：<strong>${esc(u.model_calls ?? 0)}</strong> 次 · token：<strong>${esc(u.tokens ?? 0)}</strong> · 用量未知调用：${esc(u.unknown_usage ?? 0)} 次</p>
    <p class="hint">制品 ${esc(u.blob_count ?? 0)} 个 / ${esc(u.blob_bytes ?? 0)} 字节；数据库 ${esc(u.db_bytes ?? 0)} 字节；剩余磁盘 ${esc(u.free_bytes ?? 0)} 字节。缺价格时只显示 token 与调用数，不虚构金额。</p>`;
}

async function loadMetrics() {
  const data = await api('GET', '/api/metrics');
  const m = data.metrics || {};
  el('metrics').innerHTML = `<pre>${esc(JSON.stringify(m, null, 2))}</pre>`;
}

async function loadAudit() {
  const data = await api('GET', '/api/audit?limit=30');
  el('audit-list').innerHTML = (data.audit || []).map((a) => `
    <li><div class="title"><strong>${esc(a.action)}</strong><span class="muted">${esc(fmtTime(a.created_at))}</span></div>
    <div class="meta">${esc(a.entity_type)} ${esc(a.entity_id)} · ${esc(a.actor)} ${a.reason ? '· ' + esc(a.reason) : ''}</div></li>`).join('') || '<li class="muted">暂无审计记录。</li>';
}

// ------------------------------------------------------------------ 事件绑定
el('refresh').addEventListener('click', () => refreshAll());

async function refreshAll() {
  try {
    await loadHealth();
    await loadWorkspaces();
    await loadSkills();
    await loadRuns();
    await loadProposals();
    await loadLessons();
    await loadSuites();
    await loadEvalJobs();
  } catch (e) { alertBox(e.message, 'error'); }
}

el('create-run').addEventListener('click', async () => {
  try {
    const body = {
      request: el('run-request').value.trim(),
      workspace_id: el('run-workspace').value,
      completion_criteria: el('run-criteria').value.split('\n').map((s) => s.trim()).filter(Boolean),
      skill_ids: Array.from(el('run-skills').selectedOptions).map((o) => o.value),
      learning_enabled: el('run-learning').checked,
    };
    if (!body.request) throw new Error('请填写请求');
    const data = await api('POST', '/api/runs', body);
    alertBox('任务已提交：' + data.run_id, 'ok');
    await loadRuns();
    showRun(data.run_id);
  } catch (e) { alertBox(e.message, 'error'); }
});

el('reload-runs').addEventListener('click', () => loadRuns());
el('run-filter-status').addEventListener('change', () => loadRuns());

document.body.addEventListener('click', async (event) => {
  const btn = event.target.closest('button[data-action]');
  if (!btn) return;
  const action = btn.dataset.action;
  try {
    if (action === 'check-ws') alertBox(JSON.stringify(await api('POST', `/api/workspaces/${btn.dataset.id}/check`)), 'ok');
    else if (action === 'remove-ws') { await api('DELETE', `/api/workspaces/${btn.dataset.id}`); await loadWorkspaces(); }
    else if (action === 'accept' || action === 'submit-correction') {
      const content = el('feedback-content') ? el('feedback-content').value.trim() : '';
      const body = {
        run_id: btn.dataset.run,
        type: action === 'accept' ? 'acceptance' : 'correction',
        verdict: btn.dataset.verdict || null,
        content,
        scope: el('feedback-scope') ? el('feedback-scope').value : 'task',
      };
      if (action === 'submit-correction' && !content) throw new Error('文字纠正需要填写原话');
      const data = await api('POST', `/api/runs/${btn.dataset.run}/feedback`, body);
      alertBox('反馈已保存：' + data.feedback_id, 'ok');
      showRun(btn.dataset.run);
    } else if (action === 'cancel-run') { await api('POST', `/api/runs/${btn.dataset.run}/cancel`); showRun(btn.dataset.run); }
    else if (action === 'resume-run') { await api('POST', `/api/runs/${btn.dataset.run}/resume`); showRun(btn.dataset.run); }
    else if (action === 'ignore-lesson') {
      const reason = prompt('拒绝原因：only_this_time / wrong_attribution / wrong_rule / duplicate / not_worth_cost', 'only_this_time');
      if (!reason) return;
      await api('POST', `/api/lessons/${btn.dataset.id}/ignore`, { reason_code: reason });
      await loadLessons();
    } else if (action === 'evaluate') {
      const data = await api('POST', `/api/proposals/${btn.dataset.id}/evaluate`, {});
      alertBox('评测作业已启动：' + data.job_id, 'ok');
      await loadEvalJobs();
    } else if (action === 'reject-proposal') {
      const reason = prompt('拒绝原因：only_this_time / wrong_attribution / wrong_rule / duplicate / not_worth_cost', 'only_this_time');
      if (!reason) return;
      await api('POST', `/api/proposals/${btn.dataset.id}/reject`, { reason_code: reason });
      showProposal(btn.dataset.id);
    } else if (action === 'activate-proposal') {
      const revision = prompt('请输入当前生效 revision（expected_revision）', '0');
      if (revision === null) return;
      const data = await api('POST', `/api/proposals/${btn.dataset.id}/activate`, { expected_revision: Number(revision) });
      alertBox('已启用完整包：' + data.version_id, 'ok');
      await loadSkills();
    } else if (action === 'archive') { await api('POST', `/api/skills/${btn.dataset.id}/archive`, {}); await loadSkills(); }
    else if (action === 'export') { alertBox(JSON.stringify(await api('POST', `/api/skills/${btn.dataset.id}/export`, {})), 'ok'); }
    else if (action === 'manual-trial') {
      const version = prompt('输入要试用的源版本 id');
      if (!version) return;
      const revision = prompt('当前生效 revision（expected_revision）', '0');
      if (revision === null) return;
      const data = await api('POST', `/api/skills/${btn.dataset.id}/manual-trial`, { source_version_id: version, unverified_ack: true, expected_revision: Number(revision) });
      alertBox('已创建手工试用版本（标记效果未验证）：' + data.version_id, 'ok');
      await loadSkills();
    } else if (action === 'rollback') {
      const revision = prompt('当前生效 revision（expected_revision）', '0');
      if (revision === null) return;
      await api('POST', `/api/skills/${btn.dataset.skill}/rollback`, { target_version_id: btn.dataset.version, expected_revision: Number(revision) });
      alertBox('已回退到完整包历史版本', 'ok');
      showSkill(btn.dataset.skill);
    } else if (action === 'cancel-job') { await api('POST', `/api/jobs/${btn.dataset.id}/cancel`, {}); await loadJobs(); }
    else if (action === 'cancel-eval') { await api('POST', `/api/eval/jobs/${btn.dataset.id}/cancel`, {}); showEvalJob(btn.dataset.id); }
    else if (action === 'resume-eval') { await api('POST', `/api/eval/jobs/${btn.dataset.id}/resume`, {}); showEvalJob(btn.dataset.id); }
  } catch (e) { alertBox(e.message, 'error'); }
});

el('skill-search').addEventListener('input', () => { clearTimeout(window.__searchTimer); window.__searchTimer = setTimeout(loadSkills, 250); });

el('do-import').addEventListener('click', async () => {
  try {
    const scope = el('import-scope').value;
    const body = {
      source_path: el('import-path').value.trim(),
      scope,
      workspace_id: scope === 'workspace' ? el('import-workspace').value : null,
      on_conflict: el('import-conflict').value,
    };
    const data = await api('POST', '/api/skills/import', body);
    alertBox(`导入完成：${data.skill_id} / ${data.version_id}（脚本未执行，原目录未被修改）`, 'ok');
    await loadSkills();
    showSkill(data.skill_id);
  } catch (e) { alertBox(e.message, 'error'); }
});

el('do-draft').addEventListener('click', async () => {
  try {
    const body = { name: el('draft-name').value.trim(), description: el('draft-desc').value.trim(), body: el('draft-body').value, scope: 'personal' };
    const data = await api('POST', '/api/skills/draft', body);
    alertBox('草稿已创建（初始版本，效果未验证）', 'ok');
    await loadSkills();
    showSkill(data.skill_id);
  } catch (e) { alertBox(e.message, 'error'); }
});

el('save-learning').addEventListener('click', async () => {
  try {
    await api('PUT', '/api/learning/settings', { enabled: el('learning-enabled').checked });
    alertBox('已保存学习开关', 'ok');
  } catch (e) { alertBox(e.message, 'error'); }
});

el('create-suite').addEventListener('click', async () => {
  try {
    const body = {
      skill_id: el('suite-skill').value,
      name: el('suite-name').value.trim(),
      category: el('suite-category').value.trim(),
      cases: parseJSON(el('suite-cases').value, []),
      rubric: parseJSON(el('suite-rubric').value, {}),
      critical_checks: parseJSON(el('suite-critical').value, []),
      freeze: el('suite-freeze').checked,
    };
    if (!body.cases.length) throw new Error('至少需要一个样例');
    const data = await api('POST', '/api/eval/suites', body);
    alertBox('评测集已创建：' + data.suite_id + (body.freeze ? '（已冻结）' : ''), 'ok');
    await loadSuites();
  } catch (e) { alertBox(e.message, 'error'); }
});

el('save-model').addEventListener('click', async () => {
  try {
    const body = {
      provider: el('model-provider').value,
      base_url: el('model-base-url').value.trim(),
      model: el('model-name').value.trim(),
      temperature: Number(el('model-temp').value || 0.2),
    };
    const key = el('model-api-key').value;
    if (key) body.api_key = key;
    await api('PUT', '/api/model', body);
    el('model-api-key').value = '';
    alertBox('已保存模型配置（密钥只存本机凭据库）', 'ok');
    await loadHealth();
  } catch (e) { alertBox(e.message, 'error'); }
});

el('check-model').addEventListener('click', async () => {
  try { alertBox(JSON.stringify(await api('POST', '/api/model/check', {})), 'ok'); }
  catch (e) { alertBox(e.message, 'error'); }
});

el('add-workspace').addEventListener('click', async () => {
  try {
    await api('POST', '/api/workspaces', {
      name: el('ws-name').value.trim(),
      root_path: el('ws-root').value.trim(),
      artifact_path: el('ws-artifacts').value.trim() || null,
      allowed_tools: el('ws-tools').value.split(',').map((s) => s.trim()).filter(Boolean),
      allow_scripts: el('ws-scripts').checked,
    });
    alertBox('工作区已添加', 'ok');
    await loadWorkspaces();
  } catch (e) { alertBox(e.message, 'error'); }
});

el('do-backup').addEventListener('click', async () => {
  try { alertBox(JSON.stringify(await api('POST', '/api/data/backup', {})), 'ok'); } catch (e) { alertBox(e.message, 'error'); }
});
el('do-export').addEventListener('click', async () => {
  try { alertBox(JSON.stringify(await api('POST', '/api/data/export', { scope: {} })), 'ok'); } catch (e) { alertBox(e.message, 'error'); }
});
el('do-restore-preview').addEventListener('click', async () => {
  const path = prompt('备份目录路径');
  if (!path) return;
  try { alertBox(JSON.stringify(await api('POST', '/api/data/restore', { backup_path: path, dry_run: true })), 'ok'); }
  catch (e) { alertBox(e.message, 'error'); }
});

// ------------------------------------------------------------------ 启动
(async function boot() {
  await refreshAll();
  setInterval(async () => {
    try {
      await loadHealth();
      const active = state.runs.some((r) => ['queued', 'running', 'cancelling'].includes(r.execution_status));
      if (active) { await loadRuns(); if (state.selectedRun) showRun(state.selectedRun); }
    } catch (e) { /* 轮询失败不打扰用户 */ }
  }, 5000);
})();

页面打开事件（page_view）
api('POST', '/api/events/page-view', { page_id: 'workbench' }).catch(() => {});
