/**
 * Satoshi Quantitative Trading Dashboard Frontend
 * Handles dynamic fetching, KPI updates, Chart.js rendering, and details modal.
 */

let currentTab = 'technical';
let currentFilter = { days: null, signal: 'technical' };
let currentPredictions = [];
let equityChart = null;
let distributionChart = null;

// KLineChart instance & multi-symbol state
let candleChart = null;
let currentInterval = '1h';
let currentChartSymbol = 'SOLUSDT';
let activeChartTab = 'candles';
let candleRefreshTimer = null; // owned by subscribeBar/unsubscribeBar, never resetData()

// Technical indicator overlay state (custom indicators registered once)
let activeIndicators = { ema12: true, ema26: true, sma20: false, atr_band: false, rsi14: false };
let indicatorIds = {}; // key -> created indicator id, so re-toggling removes the right one
let drawnOverlayIds = []; // user-drawn trendline/fib/channel overlay ids, for Clear

// Prediction marker visibility: ongoing (pending) predictions are the
// useful live-read - resolved outcomes (win/loss/noise/no_call_resolved)
// are historical audit trail. Only "pending" on by default.
let activeMarkerKinds = { win: false, loss: false, pending: true, noise: false, no_call_resolved: false, no_call_pending: false };

function switchMainTab(tab) {
  currentTab = tab;
  currentFilter.signal = tab;

  // Toggle Tab Button Styles
  const btnTech = document.getElementById('btnTabTech');
  const btnSent = document.getElementById('btnTabSent');
  const viewTech = document.getElementById('viewTechnical');
  const viewSent = document.getElementById('viewSentiment');

  if (tab === 'technical') {
    btnTech.className = "flex items-center gap-2.5 px-4 py-2 rounded-xl text-xs font-semibold transition-all border border-amber-500/40 bg-amber-500/10 text-amber-400 shadow-[0_0_15px_rgba(245,158,11,0.15)]";
    btnSent.className = "flex items-center gap-2.5 px-4 py-2 rounded-xl text-xs font-semibold transition-all border border-slate-800 bg-slate-900/80 text-slate-400 hover:text-white hover:border-slate-700";
    viewTech.classList.remove('hidden');
    viewSent.classList.add('hidden');
    // Resize candle chart if needed
    if (candleChart) {
      setTimeout(() => {
        const container = document.getElementById('candleChartContainer');
        if (container) candleChart.applyOptions({ width: container.clientWidth });
      }, 50);
    }
  } else {
    btnSent.className = "flex items-center gap-2.5 px-4 py-2 rounded-xl text-xs font-semibold transition-all border border-amber-500/40 bg-amber-500/10 text-amber-400 shadow-[0_0_15px_rgba(245,158,11,0.15)]";
    btnTech.className = "flex items-center gap-2.5 px-4 py-2 rounded-xl text-xs font-semibold transition-all border border-slate-800 bg-slate-900/80 text-slate-400 hover:text-white hover:border-slate-700";
    viewTech.classList.add('hidden');
    viewSent.classList.remove('hidden');
  }

  loadData();
}

function setFilter(type, value) {
  if (type === 'days') currentFilter.days = value;
  if (type === 'signal') currentFilter.signal = value;
  
  // Update UI button active states
  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.classList.remove('border-amber-500/40', 'bg-amber-500/10', 'text-amber-400', 'font-semibold');
    btn.classList.add('border-slate-700', 'bg-slate-800', 'text-slate-300');
  });

  const dayBtn = document.getElementById(currentFilter.days ? `btn-days-${currentFilter.days}` : 'btn-days-all');
  if (dayBtn) {
    dayBtn.classList.add('border-amber-500/40', 'bg-amber-500/10', 'text-amber-400', 'font-semibold');
    dayBtn.classList.remove('border-slate-700', 'bg-slate-800', 'text-slate-300');
  }

  loadData();
}

async function loadData() {
  try {
    let url = `/api/stats?signal=${currentFilter.signal}`;
    if (currentFilter.days) url += `&days=${currentFilter.days}`;

    try {
      const res = await fetch(url);
      if (res.ok) {
        const data = await res.json();
        renderDashboard(data);
      }
    } catch (err) {
      console.error('Failed to load stats:', err);
    }

    try {
      const iRes = await fetch('/api/improvement');
      if (iRes.ok) {
        const iData = await iRes.json();
        renderImprovement(iData);
      }
    } catch (err) {
      console.error('Failed to load improvement:', err);
    }
  } catch (err) {
    console.error('Failed in loadData:', err);
  }
}

function renderImprovement(d) {
  if (!d) return;

  // Badge + subtitle
  const badge = document.getElementById('impReadyBadge');
  badge.textContent = d.ready ? 'READY — GATE CAN FIRE' : 'COLLECTING DATA';
  badge.className = `text-xs font-bold font-mono px-2.5 py-1 rounded ${d.ready
    ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
    : 'bg-amber-500/10 text-amber-400 border border-amber-500/20'}`;
  document.getElementById('impSubtitle').textContent =
    `Next run: ${d.next_run_wib} · Incumbent: ${d.incumbent}`;

  // Progress bars
  const pct = (h, n) => Math.min(100, (h / Math.max(1, n)) * 100);
  const barColor = (h, n) => h >= n ? 'bg-emerald-500/80' : 'bg-amber-500/80';
  const setBar = (txtId, barId, have, need) => {
    document.getElementById(txtId).textContent = `${have} / ${need}`;
    const bar = document.getElementById(barId);
    bar.style.width = `${pct(have, need)}%`;
    bar.className = `h-full rounded-full ${barColor(have, need)}`;
  };
  setBar('impNText', 'impNBar', d.n, d.need_n);
  setBar('impHeldText', 'impHeldBar', d.held.have, d.held.need);
  setBar('impTrainText', 'impTrainBar', d.train.have, d.train.need);

  const bindingTxt = {
    held_out: 'Binding constraint: the held-out reserve. Rows land 70/30, so the 30% reserve fills last — the gate cannot unlock before it reaches 20, no matter how fast the train slice grows.',
    train: 'Binding constraint: the train slice (70%). The held-out reserve unlocks first, but the search needs 40 trainable rows before it may even try.',
    both: 'Both slices unlock at the same row count.',
  };
  document.getElementById('impBindingNote').innerHTML =
    (bindingTxt[d.binding] || '') +
    (d.n < d.need_n ? ` <span class="text-slate-500">Gate unlocks at <strong class="text-slate-300">N = ${d.need_n}</strong> (train unlocks at 58; held-out at 64).</span>` : '');

  // Right column
  document.getElementById('impNeeded').textContent = d.rows_needed;
  document.getElementById('impRate').textContent =
    `${d.accrual.qualifying_per_open_hour.toFixed(3)} / open hour`;
  document.getElementById('impSince').textContent = d.accrual.measured_since_wib || '—';
  document.getElementById('impFirst').textContent = d.first_eligible_wib || '—';
  const chainEl = document.getElementById('impChain');
  chainEl.textContent = d.chain.ok ? `OK · head ${d.chain.head}` : `TAMPERED`;
  chainEl.className = d.chain.ok ? 'text-emerald-400' : 'text-rose-400';

  // Why the count is what it is
  const why = `Every graded, non-noise prediction adds one row: <strong class="text-white">${d.n} so far</strong> (${d.sources.technical} technical + ${d.sources.sentiment} sentiment). Excluded by design: <strong class="text-white">${d.excluded.noise}</strong> noise bars (move &lt; 0.5×ATR) and <strong class="text-white">${d.excluded.no_call}</strong> resolved no-calls — they cannot support a promotion claim. Split: ${d.train.have} train / ${d.held.have} held-out of ${d.need_n} required.`;
  const backlog = (d.pending_backlog || []);
  const backlogHtml = backlog.length
    ? `<div class="mt-2.5 pt-2.5 border-t border-slate-800/60"><div class="text-[10px] uppercase tracking-wider text-slate-500 font-mono font-semibold mb-1">Not yet graded (arrive on the next open-market cycle)</div>` +
      backlog.map(b => `<div class="text-[11px] font-mono text-amber-400/90">#${b.id} · ${b.direction.toUpperCase()} · ${b.note}</div>`).join('') +
      `</div>`
    : '';
  document.getElementById('impWhyText').innerHTML = why + backlogHtml;

  // Recent attempts
  const tb = document.getElementById('impRecentBody');
  if (!d.recent || !d.recent.length) {
    tb.innerHTML = '<tr><td colspan="4" class="py-2 text-slate-500">No improvement session recorded yet.</td></tr>';
  } else {
    tb.innerHTML = d.recent.map(r => `
      <tr>
        <td class="py-1.5 pr-3">${r.slot_wib}</td>
        <td class="py-1.5 pr-3"><span class="px-1.5 py-0.5 rounded bg-slate-800 text-slate-400">${r.status}</span></td>
        <td class="py-1.5 pr-3 text-right">${r.train ?? '—'}</td>
        <td class="py-1.5 text-right">${r.held ?? '—'}</td>
      </tr>`).join('');
  }
}

function renderDashboard(data) {
  currentPredictions = data.recent_predictions || [];
  const k = data.kpis;
  
  document.getElementById('serverTime').textContent = data.server_time_wib;

  // Update badge counts on top tab buttons
  if (data.signal_counts) {
    const techBadge = document.getElementById('badgeTechCount');
    const sentBadge = document.getElementById('badgeSentCount');
    const totalTech = (data.signal_counts.technical || 0) + (data.signal_counts.lead_lag || 0);
    if (techBadge) techBadge.textContent = totalTech;
    if (sentBadge) sentBadge.textContent = data.signal_counts.sentiment || 0;
  }

  if (currentTab === 'sentiment') {
    renderSentimentView(data);
  } else {
    renderTechnicalView(data);
  }
}

function renderSentimentView(data) {
  const k = data.kpis;
  
  // Sentiment KPIs
  const hitEl = document.getElementById('sentKpiHitRate');
  if (hitEl) hitEl.textContent = k.hit_rate !== null ? `${k.hit_rate}%` : '--%';
  
  const gradedEl = document.getElementById('sentKpiGradedCount');
  if (gradedEl) gradedEl.textContent = `${k.graded_non_noise} graded (decided calls)`;

  const totalEl = document.getElementById('sentKpiTotal');
  if (totalEl) totalEl.textContent = k.total_predictions;

  const pendEl = document.getElementById('sentKpiPending');
  if (pendEl) pendEl.textContent = `${k.pending_predictions} pending`;

  const nocallEl = document.getElementById('sentKpiNoCalls');
  if (nocallEl) nocallEl.textContent = k.no_calls;

  const nocallPctEl = document.getElementById('sentKpiNoCallPct');
  if (nocallPctEl) {
    const pct = Math.round((k.no_calls / (k.total_predictions || 1)) * 100);
    nocallPctEl.textContent = `${pct}% low-conviction chop`;
  }

  renderSentimentTable(data.recent_predictions);
}

function renderTechnicalView(data) {
  const k = data.kpis;

  // KPIs
  document.getElementById('kpiHitRate').textContent = k.hit_rate !== null ? `${k.hit_rate}%` : '--%';
  document.getElementById('kpiEdgeBadge').textContent = k.edge_pp !== null ? `${k.edge_pp > 0 ? '+' : ''}${k.edge_pp}pp edge` : `vs ${k.base_rate}%`;
  document.getElementById('kpiGradedCount').textContent = `${k.graded_non_noise} graded (non-noise)`;
  
  const ptsEl = document.getElementById('kpiNetPoints');
  ptsEl.textContent = `${k.net_points > 0 ? '+' : ''}${k.net_points.toFixed(2)}`;
  ptsEl.className = `text-2xl font-bold font-mono tabular ${k.net_points > 0 ? 'text-emerald-400' : (k.net_points < 0 ? 'text-rose-400' : 'text-white')}`;

  document.getElementById('kpiProfitFactor').textContent = k.profit_factor >= 90 ? '∞' : k.profit_factor.toFixed(2);
  document.getElementById('kpiNoiseCount').textContent = k.noise_filtered;
  
  const totalOutcomes = k.total_outcomes || 1;
  const noisePct = Math.round((k.noise_filtered / totalOutcomes) * 100);
  document.getElementById('kpiNoisePercent').textContent = `${noisePct}% filtered out`;

  document.getElementById('kpiTotalPreds').textContent = k.total_predictions;
  document.getElementById('kpiPendingBadge').textContent = `${k.pending_predictions} pending`;

  const verdEl = document.getElementById('kpiVerdict');
  verdEl.textContent = k.verdict;
  verdEl.className = `text-xs font-bold font-mono px-2 py-1 rounded ${
    k.verdict === 'SIGNIFICANT' ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' :
    k.verdict === 'COLLECTING_DATA' ? 'bg-amber-500/10 text-amber-400 border border-amber-500/20' :
    'bg-slate-800 text-slate-400'
  }`;

  // Distribution Counts
  const winsCount = Math.round((k.graded_non_noise * (k.hit_rate || 0)) / 100);
  const lossCount = k.graded_non_noise - winsCount;
  document.getElementById('distWinText').textContent = winsCount;
  document.getElementById('distLossText').textContent = lossCount;
  document.getElementById('distNoiseText').textContent = k.noise_filtered;
  document.getElementById('kpiWinLossRatio').textContent = `W: ${winsCount} | L: ${lossCount}`;

  // Render Charts
  renderEquityChart(data.equity_curve);
  renderDistributionChart(winsCount, lossCount, k.noise_filtered);

  // Auto-switch symbol to the latest active trade if available and not switched yet
  if (!window.__hasUserSwitchedSymbol && data.active_trades && data.active_trades.length) {
    const activeSym = data.active_trades[0].symbol;
    if (activeSym && activeSym !== currentChartSymbol) {
      setChartSymbol(activeSym);
    }
  }

  // Multi-coin stats & Active trades
  renderActiveTrades(data.active_trades);
  renderMultiCoinStats(data.by_symbol);

  // Render Table
  renderTable(data.recent_predictions);
}

