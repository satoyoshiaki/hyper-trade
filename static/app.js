/* HL Maker Bot Dashboard - Frontend polling logic */

const POLL_INTERVAL_MS = 2000;
let currentSection = 'overview';

// --- Section navigation ---
function showSection(name) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
  const sec = document.getElementById('section-' + name);
  if (sec) sec.classList.add('active');
  const link = document.querySelector(`.nav-link[onclick*="'${name}'"]`);
  if (link) link.classList.add('active');
  currentSection = name;
}

// --- Fetch helpers ---
async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function fmt(val, decimals = 4) {
  if (val === null || val === undefined) return '-';
  const n = parseFloat(val);
  if (isNaN(n)) return val;
  return n.toFixed(decimals);
}

function fmtTime(isoStr) {
  if (!isoStr) return '-';
  return new Date(isoStr).toLocaleTimeString();
}

function flag(val) {
  const cls = val ? 'flag-true' : 'flag-false';
  return `<span class="${cls}">${val ? 'YES' : 'no'}</span>`;
}

function pnlClass(val) {
  const n = parseFloat(val);
  if (isNaN(n)) return '';
  return n > 0 ? 'positive' : n < 0 ? 'negative' : '';
}

// --- Header update ---
function updateHeader(overview) {
  const dot = document.getElementById('bot-status-indicator');
  const text = document.getElementById('bot-status-text');
  const ksBadge = document.getElementById('ks-badge');
  const netBadge = document.getElementById('network-badge');

  const status = overview.bot_status || 'unknown';
  dot.className = 'status-dot ' + status.toLowerCase();
  text.textContent = status.toUpperCase();

  if (overview.kill_switch_active) {
    ksBadge.className = 'badge badge-kill';
    ksBadge.textContent = 'KillSwitch: ON';
  } else {
    ksBadge.className = 'badge badge-ok';
    ksBadge.textContent = 'KillSwitch: OFF';
  }

  if (overview.testnet) {
    netBadge.className = 'badge badge-testnet';
    netBadge.textContent = 'TESTNET';
  } else {
    netBadge.className = 'badge badge-mainnet';
    netBadge.textContent = 'MAINNET';
  }

  document.getElementById('last-update').textContent = '更新: ' + new Date().toLocaleTimeString();
}

// --- Section renderers ---
function renderOverview(data) {
  const set = (id, val, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = val;
    if (cls) el.className = 'card-value ' + cls;
  };

  set('ov-bot-status', data.bot_status);
  set('ov-ws', data.ws_connected ? '✓ Connected' : '✗ Disconnected', data.ws_connected ? 'positive' : 'negative');
  set('ov-network', data.testnet ? 'TESTNET' : 'MAINNET');
  set('ov-symbols', (data.symbols || []).join(', '));
  set('ov-realized', fmt(data.today_realized_pnl, 4) + ' USDC', pnlClass(data.today_realized_pnl));
  set('ov-fees', fmt(data.today_fees, 4) + ' USDC');
  set('ov-net', fmt(data.net_pnl, 4) + ' USDC', pnlClass(data.net_pnl));
  set('ov-ks', data.kill_switch_active ? '🔴 ACTIVE' : '✓ OFF', data.kill_switch_active ? 'negative' : 'positive');

  const ksReason = document.getElementById('ov-ks-reason');
  if (data.kill_switch_active && data.kill_switch_reason) {
    ksReason.textContent = 'Kill reason: ' + data.kill_switch_reason;
    ksReason.classList.remove('hidden');
  } else {
    ksReason.classList.add('hidden');
  }
}

function renderSymbols(data) {
  const tbody = document.getElementById('symbols-tbody');
  tbody.innerHTML = data.map(s => `
    <tr>
      <td><strong>${s.symbol}</strong></td>
      <td>${fmt(s.mid, 2)}</td>
      <td>${fmt(s.spread_bps, 2)}</td>
      <td>${fmt(s.imbalance, 3)}</td>
      <td>${fmt(s.short_term_vol, 6)}</td>
      <td>${flag(s.stale)}</td>
      <td>${flag(s.abrupt_move)}</td>
      <td>${flag(!s.book_corrupted)}</td>
      <td>${s.quoting ? '<span class="positive">YES</span>' : '<span class="flag-false">no</span>'}</td>
      <td class="side-buy">${s.bid_quote ? fmt(s.bid_quote, 2) + ' x ' + fmt(s.bid_size, 4) : '-'}</td>
      <td class="side-sell">${s.ask_quote ? fmt(s.ask_quote, 2) + ' x ' + fmt(s.ask_size, 4) : '-'}</td>
      <td>${fmtTime(s.updated_at)}</td>
    </tr>
  `).join('');
}

function renderOrders(data) {
  const tbody = document.getElementById('orders-tbody');
  tbody.innerHTML = data.slice(0, 50).map(o => `
    <tr>
      <td>${o.symbol}</td>
      <td class="side-${o.side}">${o.side.toUpperCase()}</td>
      <td>${fmt(o.price, 2)}</td>
      <td>${fmt(o.size, 4)}</td>
      <td>${fmt(o.filled_size, 4)}</td>
      <td>${o.tif}</td>
      <td>${o.kind}</td>
      <td class="status-${o.status}">${o.status}</td>
      <td>${o.exchange_oid || '-'}</td>
      <td>${fmtTime(o.created_at)}</td>
      <td>${o.reject_reason || ''}</td>
    </tr>
  `).join('');
}

function renderFills(data) {
  const tbody = document.getElementById('fills-tbody');
  tbody.innerHTML = data.slice(0, 50).map(f => `
    <tr>
      <td>${fmtTime(f.filled_at)}</td>
      <td>${f.symbol}</td>
      <td class="side-${f.side}">${f.side.toUpperCase()}</td>
      <td>${fmt(f.price, 2)}</td>
      <td>${fmt(f.size, 4)}</td>
      <td>${fmt(f.fee, 4)}</td>
      <td>${f.is_maker ? '<span class="positive">M</span>' : '<span class="flag-true">T</span>'}</td>
    </tr>
  `).join('');
}

function renderPositions(data) {
  const tbody = document.getElementById('positions-tbody');
  tbody.innerHTML = data.map(p => `
    <tr>
      <td><strong>${p.symbol}</strong></td>
      <td class="${parseFloat(p.size) > 0 ? 'side-buy' : parseFloat(p.size) < 0 ? 'side-sell' : ''}">${fmt(p.size, 4)}</td>
      <td>${fmt(p.avg_cost, 2)}</td>
      <td class="${pnlClass(p.unrealized_pnl)}">${fmt(p.unrealized_pnl, 4)}</td>
      <td>${fmt(p.exposure_usd, 2)}</td>
      <td>${fmt(p.skew_bps, 2)}</td>
      <td>${flag(p.inventory_limit_long)}</td>
      <td>${flag(p.inventory_limit_short)}</td>
    </tr>
  `).join('');
}

function renderPnl(data) {
  const set = (id, val, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = val;
    if (cls) el.className = 'card-value ' + cls;
  };
  set('pnl-realized', fmt(data.realized_pnl, 4) + ' USDC', pnlClass(data.realized_pnl));
  set('pnl-unrealized', fmt(data.unrealized_pnl, 4) + ' USDC', pnlClass(data.unrealized_pnl));
  set('pnl-fees', fmt(data.fees_paid, 4) + ' USDC');
  set('pnl-net', fmt(data.net_pnl, 4) + ' USDC', pnlClass(data.net_pnl));
  set('pnl-daily-loss', fmt(data.daily_loss_pct, 3) + '%');
  set('pnl-drawdown', fmt(data.intraday_drawdown_pct, 3) + '%');
  set('pnl-equity', data.day_start_equity ? fmt(data.day_start_equity, 2) + ' USDC' : '-');
}

function renderRisk(data) {
  const set = (id, val, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = val;
    if (cls) el.className = 'card-value ' + cls;
  };
  set('risk-ks', data.kill_switch_active ? '🔴 ACTIVE' : '✓ OFF', data.kill_switch_active ? 'negative' : 'positive');
  set('risk-daily-loss', `${fmt(data.daily_loss_pct, 2)}% / ${fmt(data.daily_loss_limit_pct, 1)}%`);
  set('risk-drawdown', `${fmt(data.intraday_drawdown_pct, 2)}% / ${fmt(data.drawdown_limit_pct, 1)}%`);
  set('risk-exposure', `$${fmt(data.total_exposure_usd, 2)} / $${fmt(data.total_exposure_limit_usd, 0)}`);
  set('risk-rejects', `${data.consecutive_rejects} / ${data.max_reject_streak}`);
  set('risk-reconnect', `${data.reconnect_streak} / ${data.max_reconnect_streak} (total: ${data.reconnect_count})`);
  set('risk-stale', `${data.stale_data_age_ms}ms (limit: ${data.stale_data_threshold_ms}ms)`);
  const spreadEl = document.getElementById('risk-spread');
  if (spreadEl) { spreadEl.innerHTML = flag(data.abnormal_spread); }
  const abruptEl = document.getElementById('risk-abrupt');
  if (abruptEl) { abruptEl.innerHTML = flag(data.abrupt_move); }
  const bookEl = document.getElementById('risk-book');
  if (bookEl) { bookEl.innerHTML = flag(data.book_corrupted); }
}

function renderEvents(data) {
  const tbody = document.getElementById('events-tbody');
  const levelColor = { critical: '#f44', error: '#f84', warn: '#fa4', info: '#aaa', debug: '#666' };
  tbody.innerHTML = data.slice(0, 50).map(e => `
    <tr>
      <td>${fmtTime(e.occurred_at)}</td>
      <td style="color:${levelColor[e.level] || '#aaa'}">${e.level.toUpperCase()}</td>
      <td>${e.event_type}</td>
      <td>${e.symbol || ''}</td>
      <td>${e.message}</td>
    </tr>
  `).join('');
}

// --- Control actions ---
async function requestStop() {
  if (!confirm('Graceful stop を実行しますか？\n現在のサイクルを完了後、注文をキャンセルして停止します。')) return;
  try {
    const res = await fetch('/api/stop', { method: 'POST' });
    const data = await res.json();
    alert(data.message);
  } catch (e) {
    alert('Stop request failed: ' + e.message);
  }
}

async function requestKill() {
  if (!confirm('【危険】Emergency Kill Switch を発動しますか？\nポジションのフラット化が試行されます。')) return;
  if (!confirm('本当に実行しますか？この操作は取り消せません。')) return;
  try {
    const res = await fetch('/api/kill', { method: 'POST' });
    const data = await res.json();
    alert(data.message);
  } catch (e) {
    alert('Kill request failed: ' + e.message);
  }
}

// --- Main poll loop ---
async function poll() {
  try {
    // Always fetch overview for header
    const overview = await fetchJSON('/api/overview');
    updateHeader(overview);

    // Fetch and render current section
    switch (currentSection) {
      case 'overview':
        renderOverview(overview);
        break;
      case 'symbols':
        renderSymbols(await fetchJSON('/api/symbols'));
        break;
      case 'orders':
        renderOrders(await fetchJSON('/api/orders'));
        break;
      case 'fills':
        renderFills(await fetchJSON('/api/fills'));
        break;
      case 'positions':
        renderPositions(await fetchJSON('/api/positions'));
        break;
      case 'pnl':
        renderPnl(await fetchJSON('/api/pnl'));
        break;
      case 'risk':
        renderRisk(await fetchJSON('/api/risk'));
        break;
      case 'events':
        renderEvents(await fetchJSON('/api/events'));
        break;
    }
  } catch (e) {
    document.getElementById('last-update').textContent = 'エラー: ' + e.message;
  }
}

// Start polling
poll();
setInterval(poll, POLL_INTERVAL_MS);
