/* HL Maker Bot Dashboard - Frontend polling logic */

const POLL_INTERVAL_MS = 2000;
const DEFAULT_LOCALE = 'ja';
const SUPPORTED_LOCALES = ['ja', 'en'];

let currentSection = 'overview';
let currentLocale = DEFAULT_LOCALE;
let messages = {};

function showSection(name) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
  const sec = document.getElementById('section-' + name);
  if (sec) sec.classList.add('active');
  const link = document.querySelector(`.nav-link[onclick*="'${name}'"]`);
  if (link) link.classList.add('active');
  currentSection = name;
}

function getMessage(key) {
  return key.split('.').reduce((acc, part) => (acc && acc[part] !== undefined ? acc[part] : null), messages);
}

function t(key, vars = {}) {
  const template = getMessage(key) ?? key;
  return Object.entries(vars).reduce(
    (text, [name, value]) => text.replaceAll(`{${name}}`, value),
    String(template),
  );
}

function setTextContent(node, value) {
  if (!node) return;
  if (node.tagName === 'TITLE') {
    document.title = value;
    return;
  }
  node.textContent = value;
}

function applyTranslations() {
  document.documentElement.lang = currentLocale;
  document.querySelectorAll('[data-i18n]').forEach(node => {
    setTextContent(node, t(node.dataset.i18n));
  });
  document.querySelectorAll('[data-i18n-aria-label]').forEach(node => {
    node.setAttribute('aria-label', t(node.dataset.i18nAriaLabel));
  });
}

async function loadLocale(locale) {
  const target = SUPPORTED_LOCALES.includes(locale) ? locale : DEFAULT_LOCALE;
  const res = await fetch(`/static/locales/${target}.json`);
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  messages = await res.json();
  currentLocale = target;
  const select = document.getElementById('language-select');
  if (select) select.value = currentLocale;
  localStorage.setItem('dashboard.locale', currentLocale);
  applyTranslations();
}

async function setLocale(locale) {
  try {
    await loadLocale(locale);
  } catch (err) {
    if (locale !== DEFAULT_LOCALE) {
      await loadLocale(DEFAULT_LOCALE);
      return;
    }
    throw err;
  }
}

async function fetchJSON(url, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set('X-Dashboard-Language', currentLocale);
  const res = await fetch(url, { ...options, headers });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function fmt(val, decimals = 4) {
  if (val === null || val === undefined) return '-';
  const n = parseFloat(val);
  if (Number.isNaN(n)) return val;
  return n.toFixed(decimals);
}

function fmtTime(isoStr) {
  if (!isoStr) return '-';
  return new Date(isoStr).toLocaleTimeString(currentLocale);
}

function flag(val) {
  const cls = val ? 'flag-true' : 'flag-false';
  return `<span class="${cls}">${val ? t('common.yes') : t('common.no')}</span>`;
}

function pnlClass(val) {
  const n = parseFloat(val);
  if (Number.isNaN(n)) return '';
  return n > 0 ? 'positive' : n < 0 ? 'negative' : '';
}

function botStatusLabel(status) {
  return t(`status.bot.${String(status || 'unknown').toLowerCase()}`);
}

function networkLabel(isTestnet) {
  return isTestnet ? t('network.testnet') : t('network.mainnet');
}

function killSwitchLabel(active) {
  return active ? t('common.active') : t('common.off');
}

function sideLabel(side) {
  return t(`orders.sideValues.${String(side).toLowerCase()}`);
}

function tifLabel(value) {
  return t(`orders.tifValues.${String(value)}`);
}

function kindLabel(value) {
  return t(`orders.kindValues.${String(value)}`);
}

function orderStatusLabel(value) {
  return t(`orders.statusValues.${String(value)}`);
}

function eventLevelLabel(value) {
  return t(`events.levelValues.${String(value)}`);
}

function makerTakerLabel(isMaker) {
  return isMaker ? t('fills.maker') : t('fills.taker');
}

function killReasonLabel(reason) {
  return t(`killReason.${String(reason)}`);
}

function setCardValue(id, val, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = val;
  el.className = cls ? `card-value ${cls}` : 'card-value';
}

function updateHeader(overview) {
  const dot = document.getElementById('bot-status-indicator');
  const text = document.getElementById('bot-status-text');
  const ksBadge = document.getElementById('ks-badge');
  const netBadge = document.getElementById('network-badge');

  const status = overview.bot_status || 'unknown';
  dot.className = 'status-dot ' + status.toLowerCase();
  text.textContent = botStatusLabel(status);

  if (overview.kill_switch_active) {
    ksBadge.className = 'badge badge-kill';
    ksBadge.textContent = t('header.killSwitchOn');
  } else {
    ksBadge.className = 'badge badge-ok';
    ksBadge.textContent = t('header.killSwitchOff');
  }

  if (overview.testnet) {
    netBadge.className = 'badge badge-testnet';
  } else {
    netBadge.className = 'badge badge-mainnet';
  }
  netBadge.textContent = networkLabel(overview.testnet);

  document.getElementById('last-update').textContent = t('header.lastUpdated', {
    time: new Date().toLocaleTimeString(currentLocale),
  });
}

function renderOverview(data) {
  setCardValue('ov-bot-status', botStatusLabel(data.bot_status));
  setCardValue(
    'ov-ws',
    data.ws_connected ? t('overview.wsConnectedValue') : t('overview.wsDisconnectedValue'),
    data.ws_connected ? 'positive' : 'negative',
  );
  setCardValue('ov-network', networkLabel(data.testnet));
  setCardValue('ov-symbols', (data.symbols || []).join(', '));
  setCardValue('ov-realized', fmt(data.today_realized_pnl, 4) + ' USDC', pnlClass(data.today_realized_pnl));
  setCardValue('ov-fees', fmt(data.today_fees, 4) + ' USDC');
  setCardValue('ov-net', fmt(data.net_pnl, 4) + ' USDC', pnlClass(data.net_pnl));
  setCardValue('ov-ks', killSwitchLabel(data.kill_switch_active), data.kill_switch_active ? 'negative' : 'positive');

  const ksReason = document.getElementById('ov-ks-reason');
  if (data.kill_switch_active && data.kill_switch_reason) {
    ksReason.textContent = t('overview.killReason', {
      reason: killReasonLabel(data.kill_switch_reason),
    });
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
      <td>${s.quoting ? `<span class="positive">${t('common.yes')}</span>` : `<span class="flag-false">${t('common.no')}</span>`}</td>
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
      <td class="side-${o.side}">${sideLabel(o.side)}</td>
      <td>${fmt(o.price, 2)}</td>
      <td>${fmt(o.size, 4)}</td>
      <td>${fmt(o.filled_size, 4)}</td>
      <td>${tifLabel(o.tif)}</td>
      <td>${kindLabel(o.kind)}</td>
      <td class="status-${o.status}">${orderStatusLabel(o.status)}</td>
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
      <td class="side-${f.side}">${sideLabel(f.side)}</td>
      <td>${fmt(f.price, 2)}</td>
      <td>${fmt(f.size, 4)}</td>
      <td>${fmt(f.fee, 4)}</td>
      <td>${f.is_maker ? `<span class="positive">${makerTakerLabel(true)}</span>` : `<span class="flag-true">${makerTakerLabel(false)}</span>`}</td>
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
  setCardValue('pnl-realized', fmt(data.realized_pnl, 4) + ' USDC', pnlClass(data.realized_pnl));
  setCardValue('pnl-unrealized', fmt(data.unrealized_pnl, 4) + ' USDC', pnlClass(data.unrealized_pnl));
  setCardValue('pnl-fees', fmt(data.fees_paid, 4) + ' USDC');
  setCardValue('pnl-net', fmt(data.net_pnl, 4) + ' USDC', pnlClass(data.net_pnl));
  setCardValue('pnl-daily-loss', fmt(data.daily_loss_pct, 3) + '%');
  setCardValue('pnl-drawdown', fmt(data.intraday_drawdown_pct, 3) + '%');
  setCardValue('pnl-equity', data.day_start_equity ? fmt(data.day_start_equity, 2) + ' USDC' : '-');
}

function renderRisk(data) {
  setCardValue('risk-ks', killSwitchLabel(data.kill_switch_active), data.kill_switch_active ? 'negative' : 'positive');
  setCardValue('risk-daily-loss', `${fmt(data.daily_loss_pct, 2)}% / ${fmt(data.daily_loss_limit_pct, 1)}%`);
  setCardValue('risk-drawdown', `${fmt(data.intraday_drawdown_pct, 2)}% / ${fmt(data.drawdown_limit_pct, 1)}%`);
  setCardValue('risk-exposure', `$${fmt(data.total_exposure_usd, 2)} / $${fmt(data.total_exposure_limit_usd, 0)}`);
  setCardValue('risk-rejects', `${data.consecutive_rejects} / ${data.max_reject_streak}`);
  setCardValue('risk-reconnect', t('risk.reconnectValue', {
    current: String(data.reconnect_streak),
    max: String(data.max_reconnect_streak),
    total: String(data.reconnect_count),
  }));
  setCardValue('risk-stale', t('risk.staleValue', {
    age: String(data.stale_data_age_ms),
    threshold: String(data.stale_data_threshold_ms),
  }));
  const spreadEl = document.getElementById('risk-spread');
  if (spreadEl) spreadEl.innerHTML = flag(data.abnormal_spread);
  const abruptEl = document.getElementById('risk-abrupt');
  if (abruptEl) abruptEl.innerHTML = flag(data.abrupt_move);
  const bookEl = document.getElementById('risk-book');
  if (bookEl) bookEl.innerHTML = flag(data.book_corrupted);
}

function renderEvents(data) {
  const tbody = document.getElementById('events-tbody');
  const levelColor = { critical: '#f44', error: '#f84', warn: '#fa4', info: '#aaa', debug: '#666' };
  tbody.innerHTML = data.slice(0, 50).map(e => `
    <tr>
      <td>${fmtTime(e.occurred_at)}</td>
      <td style="color:${levelColor[e.level] || '#aaa'}">${eventLevelLabel(e.level)}</td>
      <td>${e.event_type}</td>
      <td>${e.symbol || ''}</td>
      <td>${e.message}</td>
    </tr>
  `).join('');
}

async function requestStop() {
  if (!confirm(t('dialog.stopConfirm'))) return;
  try {
    const data = await fetchJSON('/api/stop', { method: 'POST' });
    alert(data.message);
  } catch (e) {
    alert(t('errors.stopRequestFailed', { message: e.message }));
  }
}

async function requestKill() {
  if (!confirm(t('dialog.killConfirmPrimary'))) return;
  if (!confirm(t('dialog.killConfirmSecondary'))) return;
  try {
    const data = await fetchJSON('/api/kill', { method: 'POST' });
    alert(data.message);
  } catch (e) {
    alert(t('errors.killRequestFailed', { message: e.message }));
  }
}

async function poll() {
  try {
    const overview = await fetchJSON('/api/overview');
    updateHeader(overview);

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
    document.getElementById('last-update').textContent = t('errors.pollFailed', { message: e.message });
  }
}

async function initDashboard() {
  const preferredLocale = localStorage.getItem('dashboard.locale') || DEFAULT_LOCALE;
  await setLocale(preferredLocale);

  const languageSelect = document.getElementById('language-select');
  if (languageSelect) {
    languageSelect.addEventListener('change', async event => {
      await setLocale(event.target.value);
      await poll();
    });
  }

  await poll();
  setInterval(poll, POLL_INTERVAL_MS);
}

initDashboard();
