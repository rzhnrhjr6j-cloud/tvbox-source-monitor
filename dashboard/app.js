/* Read-only dashboard for tvbox-source-monitor (spec §22 / §23).
 *
 * Every value rendered here comes from dist/dashboard.json, which the builder
 * writes.  Nothing on this page writes anything back: V1 has no mutation
 * surface, so the only way to change a source is a git commit (spec §23).
 */
'use strict';

/** Build text nodes instead of innerHTML: source names and URLs come from
 *  third-party repositories and must never be treated as markup. */
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null && text !== '') node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function cell(row, text, className) {
  row.appendChild(el('td', text, className));
}

function statusPill(status) {
  const value = String(status || 'unknown');
  return el('span', value, 'pill ' + value);
}

function percent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return (Number(value) * 100).toFixed(1) + '%';
}

function ms(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return Math.round(Number(value)) + ' ms';
}

function num(value) {
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}

function shortTime(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString('zh-CN', { hour12: false });
}

/** dashboard.json lives at the site root; the dashboard is served from /dashboard/. */
function candidateUrls() {
  const seen = new Set();
  const list = ['../dashboard.json', 'dashboard.json', '/dashboard.json'];
  return list.filter((url) => (seen.has(url) ? false : (seen.add(url), true)));
}

async function loadDashboard() {
  let lastError = null;
  for (const url of candidateUrls()) {
    try {
      const response = await fetch(url, { cache: 'no-store' });
      if (!response.ok) {
        lastError = new Error(url + ' -> HTTP ' + response.status);
        continue;
      }
      return { data: await response.json(), url };
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error('dashboard.json 不可读取');
}

function renderSummary(data) {
  const summary = data.summary || {};
  const counts = summary.sources || {};
  document.getElementById('stat-active').textContent = num(counts.active ?? 0);
  document.getElementById('stat-degraded').textContent = num(counts.degraded ?? 0);
  document.getElementById('stat-failed').textContent = num(counts.failed ?? 0);
  document.getElementById('stat-candidates').textContent = num(summary.candidates ?? 0);
  document.getElementById('stat-output').textContent = num(summary.output_items ?? 0);
}

function renderRegions(data) {
  const target = document.getElementById('regions');
  target.replaceChildren();
  const regions = data.regions || {};
  const names = Object.keys(regions);
  if (!names.length) {
    target.appendChild(el('p', '暂无地区数据', 'empty'));
    return;
  }
  for (const name of names) {
    const entry = regions[name] || {};
    const pct = Number(entry.availability_percent || 0);
    const box = el('div', null, 'region');

    const head = el('div', null, 'region-head');
    head.appendChild(el('span', name, 'region-name'));
    head.appendChild(el('span', pct.toFixed(1) + '%', 'region-pct'));
    box.appendChild(head);

    const bar = el('div', null, 'bar' + (pct >= 90 ? '' : pct >= 60 ? ' mid' : ' low'));
    const fill = el('span');
    fill.style.width = Math.max(0, Math.min(100, pct)) + '%';
    bar.appendChild(fill);
    box.appendChild(bar);

    box.appendChild(el('div', num(entry.successes) + ' / ' + num(entry.checks) + ' 次配置取回成功（HTTP + 配置解析）', 'region-meta'));
    target.appendChild(box);
  }
}

function renderLast(data) {
  const build = (data.build_history || [])[0];
  const alert = (data.alerts || [])[0];

  document.getElementById('last-build').textContent = build ? shortTime(build.generated_at) : '暂无构建记录';
  const buildDetail = document.getElementById('last-build-detail');
  buildDetail.replaceChildren();
  if (build) {
    const pairs = [
      ['build_id', build.build_id],
      ['条数', build.items],
      ['active', build.active],
      ['degraded', build.degraded],
      ['failed', build.failed],
      ['新增 / 恢复 / 移除', num(build.new) + ' / ' + num(build.recovered) + ' / ' + num(build.removed)],
      ['结果', build.published ? '已发布' : '被安全阀拦下'],
    ];
    for (const [key, value] of pairs) {
      buildDetail.appendChild(el('dt', key));
      buildDetail.appendChild(el('dd', value));
    }
  }

  const lastAlert = document.getElementById('last-alert');
  const alertDetail = document.getElementById('last-alert-detail');
  alertDetail.replaceChildren();
  if (!alert) {
    lastAlert.textContent = '暂无告警';
    return;
  }
  lastAlert.textContent = [alert.source_id ? String(alert.source_id).slice(0, 12) : alert.kind, alert.region, alert.title]
    .filter(Boolean)
    .join(' / ');
  for (const [key, value] of [['kind', alert.kind], ['严重度', alert.severity], ['时间', shortTime(alert.created_at)], ['状态', alert.resolved_at ? '已恢复' : '未解决']]) {
    alertDetail.appendChild(el('dt', key));
    alertDetail.appendChild(el('dd', value));
  }
}

function regionText(source) {
  const ok = source.regions_ok || [];
  const failed = source.regions_failed || [];
  const parts = [];
  if (ok.length) parts.push('✓ ' + ok.join(','));
  if (failed.length) parts.push('✗ ' + failed.join(','));
  return parts.join('  ') || '—';
}

function renderSources(data) {
  const body = document.getElementById('sources-body');
  const sources = (data.sources || []).slice().sort((a, b) => Number(b.score || 0) - Number(a.score || 0));
  const filter = (document.getElementById('filter').value || '').trim().toLowerCase();

  const matches = sources.filter((source) => {
    if (!filter) return true;
    return [source.name, source.url, source.status, source.tier, source.type]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(filter));
  });

  body.replaceChildren();
  for (const source of matches) {
    const row = document.createElement('tr');

    const nameCell = document.createElement('td');
    nameCell.appendChild(el('div', source.name || String(source.id || '').slice(0, 10)));
    nameCell.appendChild(el('div', source.url, 'url'));
    row.appendChild(nameCell);

    const statusCell = document.createElement('td');
    statusCell.appendChild(statusPill(source.status));
    if (Number(source.consecutive_failure || 0) > 0) {
      statusCell.appendChild(el('div', '连续失败 ' + source.consecutive_failure, 'url'));
    }
    row.appendChild(statusCell);

    const tierCell = document.createElement('td');
    tierCell.appendChild(el('span', source.tier || '—'));
    if (source.whitelisted) tierCell.appendChild(el('div', '白名单', 'url'));
    row.appendChild(tierCell);

    cell(row, source.type || '—');
    cell(row, Number(source.score || 0).toFixed(1), 'num');
    cell(row, percent(source.availability_7d), 'num');
    cell(row, percent(source.availability_30d), 'num');
    cell(row, ms(source.avg_response_ms), 'num');
    cell(row, regionText(source));
    cell(row, shortTime(source.last_success_at));
    cell(row, shortTime(source.last_failure_at));

    body.appendChild(row);
  }

  document.getElementById('sources-empty').hidden = matches.length > 0;
}

function renderBuilds(data) {
  const body = document.getElementById('builds-body');
  body.replaceChildren();
  for (const build of data.build_history || []) {
    const row = document.createElement('tr');
    cell(row, build.build_id);
    cell(row, shortTime(build.generated_at));
    cell(row, build.published ? '是' : '否');
    cell(row, num(build.items), 'num');
    cell(row, num(build.active), 'num');
    cell(row, num(build.degraded), 'num');
    cell(row, num(build.failed), 'num');
    cell(row, num(build.new), 'num');
    cell(row, num(build.recovered), 'num');
    cell(row, num(build.removed), 'num');
    cell(row, build.blocked_reason || (build.fallback ? 'rollback' : ''), 'url');
    body.appendChild(row);
  }
}

function render(data) {
  renderSummary(data);
  renderRegions(data);
  renderLast(data);
  renderSources(data);
  renderBuilds(data);
  document.getElementById('generated-at').textContent =
    '数据生成时间 ' + shortTime(data.generated_at) + ' · build ' + (data.build_id || '—') +
    ' · 只读面板，源由 GitHub Actions 自动维护';
}

async function main() {
  const errorBox = document.getElementById('error');
  try {
    const { data } = await loadDashboard();
    render(data);
    errorBox.hidden = true;
    let timer = null;
    document.getElementById('filter').addEventListener('input', () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => renderSources(data), 120);
    });
  } catch (error) {
    errorBox.hidden = false;
    errorBox.textContent = '无法读取 dashboard.json：' + (error && error.message ? error.message : error) +
      '（首次运行尚未产出 dashboard.json 时属正常）';
    document.getElementById('generated-at').textContent = '未加载数据';
  }
}

main();
