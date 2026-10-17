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

function renderMultiCoinStats(bySymbol) {
  if (!bySymbol) return;
  for (const [sym, st] of Object.entries(bySymbol)) {
    const el = document.getElementById('stat_' + sym);
    if (el) {
      if (st.graded > 0) {
        el.textContent = `${st.win_rate}% (${st.wins}W/${st.losses}L)`;
      } else {
        el.textContent = `${st.total} call${st.total === 1 ? '' : 's'}`;
      }
    }
  }
}

function renderActiveTrades(activeTrades) {
  const container = document.getElementById('activeTradesList');
  const badge = document.getElementById('activeTradesCountBadge');
  if (!container) return;
  
  const count = (activeTrades && activeTrades.length) || 0;
  if (badge) {
    badge.textContent = `${count} ACTIVE`;
    badge.className = `text-[10px] px-2 py-0.5 rounded font-mono ${count > 0 ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 animate-pulse' : 'bg-amber-500/10 text-amber-400 border border-amber-500/20'}`;
  }

  if (!count) {
    container.innerHTML = `
      <div class="col-span-full py-4 text-center text-xs text-slate-500 font-mono bg-slate-950/40 rounded-lg border border-slate-800/60">
        No active lead-lag trades holding. Monitoring 8 coin streams for impulse-disparity triggers.
      </div>`;
    return;
  }

  container.innerHTML = activeTrades.map(t => {
    const isBull = t.direction === 'bullish';
    const dirClass = isBull ? 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30' : 'text-rose-400 bg-rose-500/10 border-rose-500/30';
    const sym = t.symbol || 'SOLUSDT';
    const leader = t.leader_symbol || 'BTCUSDT';
    const fmt = (v) => v < 0.1 ? v.toFixed(5) : (v < 2 ? v.toFixed(4) : (v < 100 ? v.toFixed(3) : v.toFixed(2)));
    const refStr = t.ref_price ? fmt(t.ref_price) : '--';
    const tpStr = t.tp_price ? fmt(t.tp_price) : '--';
    const slStr = t.sl_price ? fmt(t.sl_price) : '--';

    return `
      <div class="bg-slate-950/80 border border-slate-800 rounded-xl p-3 flex flex-col justify-between hover:border-slate-700 transition-colors">
        <div class="flex items-center justify-between mb-2">
          <div class="flex items-center gap-1.5">
            <button onclick="setChartSymbol('${sym}')" class="font-bold text-xs font-mono text-amber-300 hover:text-amber-200 hover:underline flex items-center gap-1" title="Click to view ${sym} chart">
              ${sym} <span class="text-[10px] text-slate-500 font-normal">📈</span>
            </button>
            <span class="text-[10px] px-1.5 py-0.2 rounded border font-mono font-bold ${dirClass}">${t.direction.toUpperCase()}</span>
          </div>
          <span class="text-[10px] text-slate-500 font-mono">#${t.id} via ${leader}</span>
        </div>
        <div class="grid grid-cols-3 gap-2 font-mono text-xs my-1 bg-slate-900/60 p-2 rounded-lg border border-slate-800/60 text-center">
          <div>
            <div class="text-[9px] text-slate-500 uppercase">Entry</div>
            <div class="text-white font-semibold text-[11px] mt-0.5">${refStr}</div>
          </div>
          <div>
            <div class="text-[9px] text-emerald-400 uppercase">Target (TP)</div>
            <div class="text-emerald-300 font-semibold text-[11px] mt-0.5">${tpStr}</div>
          </div>
          <div>
            <div class="text-[9px] text-rose-400 uppercase">Stop Loss</div>
            <div class="text-rose-300 font-semibold text-[11px] mt-0.5">${slStr}</div>
          </div>
        </div>
        <div class="flex items-center justify-between text-[10px] text-slate-400 font-mono mt-1 pt-1.5 border-t border-slate-800/60">
          <span>Disparity: <strong class="text-amber-400">${t.disparity_pct ? t.disparity_pct.toFixed(2) + '%' : '--'}</strong></span>
          <button onclick="setChartSymbol('${sym}')" class="text-amber-400 hover:text-amber-300 text-[10px] font-semibold">View on Chart &rarr;</button>
        </div>
      </div>
    `;
  }).join('');
}

function renderEquityChart(series) {
  const ctx = document.getElementById('equityChart').getContext('2d');
  
  const labels = series.length ? series.map(s => s.time) : ['Genesis'];
  const data = series.length ? series.map(s => s.points) : [0];

  if (equityChart) equityChart.destroy();

  equityChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        label: 'Net Points Accumulated',
        data: data,
        borderColor: '#f59e0b',
        backgroundColor: 'rgba(245, 158, 11, 0.08)',
        borderWidth: 2,
        tension: 0.1,
        fill: true,
        pointBackgroundColor: '#f59e0b',
        pointRadius: series.length > 20 ? 1 : 4,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: function(ctx) { return `Points: ${ctx.parsed.y > 0 ? '+' : ''}${ctx.parsed.y.toFixed(2)}`; }
          }
        }
      },
      scales: {
        x: {
          grid: { color: 'rgba(255, 255, 255, 0.03)' },
          ticks: { color: '#64748b', maxTicksLimit: 8, font: { family: 'JetBrains Mono', size: 10 } }
        },
        y: {
          grid: { color: 'rgba(255, 255, 255, 0.05)' },
          ticks: { color: '#64748b', font: { family: 'JetBrains Mono', size: 10 } }
        }
      }
    }
  });
}

function renderDistributionChart(wins, losses, noise) {
  const ctx = document.getElementById('distributionChart').getContext('2d');
  if (distributionChart) distributionChart.destroy();

  const total = wins + losses + noise;
  const data = total > 0 ? [wins, losses, noise] : [0, 0, 1];

  distributionChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Wins', 'Losses', 'Noise Filtered'],
      datasets: [{
        data: data,
        backgroundColor: ['#10b981', '#f43f5e', '#334155'],
        borderWidth: 0,
        hoverOffset: 4
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false }
      },
      cutout: '72%'
    }
  });
}

function renderTable(predictions) {
  const tbody = document.getElementById('ledgerTableBody');
  if (!predictions || !predictions.length) {
    tbody.innerHTML = '<tr><td colspan="11" class="text-center py-6 text-slate-500">No predictions found</td></tr>';
    return;
  }

  const fmtPrice = (v) => {
    if (v === null || v === undefined) return '—';
    if (v < 0.1) return v.toFixed(5);
    if (v < 2.0) return v.toFixed(4);
    if (v < 100.0) return v.toFixed(3);
    return v.toFixed(2);
  };

  tbody.innerHTML = predictions.map(p => {
    const isBull = p.direction === 'bullish';
    const isBear = p.direction === 'bearish';
    const dirBadge = isBull 
      ? '<span class="px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-semibold">BULL</span>'
      : (isBear 
        ? '<span class="px-2 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20 font-semibold">BEAR</span>'
        : '<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-400">NO CALL</span>');

    let statusBadge = '<span class="text-slate-500 font-mono">Pending...</span>';
    if (p.graded_wib) {
      if (p.is_noise) {
        statusBadge = '<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">NOISE</span>';
      } else if (p.correct === 1) {
        statusBadge = '<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-400 font-bold border border-emerald-500/30">WIN</span>';
      } else if (p.correct === 0) {
        statusBadge = '<span class="px-2 py-0.5 rounded bg-rose-500/20 text-rose-400 font-bold border border-rose-500/30">LOSS</span>';
      } else if (p.correct === null && !p.is_noise) {
        statusBadge = '<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">NO-CALL</span>';
      }
    } else if (p.direction === 'no_call') {
      statusBadge = '<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">NO-CALL</span>';
    }

    let targetBadge = "";
    if (p.target_reached === 1) {
      targetBadge = ' <span class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-bold border border-emerald-500/40" title="Target (TP) reached">🎯 TP</span>';
    } else if (p.sl_hit === 1) {
      targetBadge = ' <span class="text-[10px] px-1.5 py-0.5 rounded bg-rose-500/20 text-rose-300 font-bold border border-rose-500/40" title="Stop Loss (SL) hit">🛑 SL</span>';
    }

    const moveText = p.move !== null ? `${p.move > 0 ? '+' : ''}${fmtPrice(p.move)}` : '—';
    const moveColor = p.move > 0 ? 'text-emerald-400' : (p.move < 0 ? 'text-rose-400' : 'text-slate-400');
    
    const scoreVal = p.score !== undefined ? p.score : (p.direction === 'bearish' ? -p.confidence : p.confidence || 0);
    const pBull = p.bullish_confidence !== undefined 
      ? (p.bullish_confidence * 100).toFixed(1) 
      : ((0.5 + 0.5 * scoreVal) * 100).toFixed(1);
    const pBear = p.bearish_confidence !== undefined 
      ? (p.bearish_confidence * 100).toFixed(1) 
      : ((0.5 - 0.5 * scoreVal) * 100).toFixed(1);

    let reasonSummary = "Technical Confluence";
    if (p.notes) {
      if (p.notes.includes("Lead-Lag")) {
        reasonSummary = p.notes;
      } else if (p.notes.includes("Bullish bias:")) {
        reasonSummary = "Bullish momentum & structure";
      } else if (p.notes.includes("Bearish bias:")) {
        reasonSummary = "Bearish rejection & drag";
      } else if (p.notes.includes("Market in low-conviction chop")) {
        reasonSummary = "Market chop (Abstained)";
      } else if (p.notes.includes("Abstained:")) {
        reasonSummary = p.notes.split("Abstained:")[1].split(". ")[0].trim();
      } else if (p.notes.startsWith("contrib=")) {
        reasonSummary = "Calculated multi-factor score";
      } else {
        reasonSummary = p.notes.split('. ')[0];
      }
    }

    const sym = p.symbol || 'SOLUSDT';
    const leaderStr = p.leader_symbol ? `<span class="text-[9px] text-slate-500 block">via ${p.leader_symbol}</span>` : '';

    let tpPrice = p.target_price;
    let slPrice = p.sl_price;
    if (!tpPrice || !slPrice) {
      try {
        const inp = JSON.parse(p.inputs_json || '{}');
        tpPrice = inp.target_tp_price || inp.tp_price;
        slPrice = inp.stop_loss_price || inp.sl_price;
      } catch(e) {}
    }

    return `
      <tr class="hover:bg-slate-800/40 transition-colors cursor-pointer group" onclick="openDetailsModal(${p.id})">
        <td class="py-3 px-4 font-mono font-medium text-slate-400 group-hover:text-amber-400">#${p.id}</td>
        <td class="py-3 px-4 font-mono">
          <button onclick="setChartSymbol('${sym}')" class="px-2 py-0.5 rounded font-bold text-[10px] bg-amber-500/10 text-amber-300 border border-amber-500/20 hover:bg-amber-500/20 hover:border-amber-400 cursor-pointer" title="Click to view ${sym} on chart">
            ${sym} 📈
          </button>
          ${leaderStr}
        </td>
        <td class="py-3 px-4 text-slate-300 whitespace-nowrap">${p.created_wib.slice(5, 16)}</td>
        <td class="py-3 px-4">${dirBadge}</td>
        <td class="py-3 px-4">
          <div class="flex flex-col gap-1">
            <div class="flex items-center gap-2">
              <span class="font-mono text-xs font-bold tracking-tight whitespace-nowrap">
                <span class="text-emerald-400">🐂 ${pBull}% Bull</span>
                <span class="text-slate-500">·</span>
                <span class="text-rose-400">🐻 ${pBear}% Bear</span>
              </span>
              <span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700/60 font-mono group-hover:text-amber-300">Details &rarr;</span>
            </div>
            <div class="text-[11px] text-slate-400 truncate max-w-xs group-hover:text-slate-300" title="${reasonSummary}">${reasonSummary}</div>
          </div>
        </td>
        <td class="py-3 px-4 text-right font-mono tabular font-medium text-white">${fmtPrice(p.ref_price)}</td>
        <td class="py-3 px-4 text-right font-mono tabular text-emerald-400">${fmtPrice(tpPrice)}</td>
        <td class="py-3 px-4 text-right font-mono tabular text-rose-400">${fmtPrice(slPrice)}</td>
        <td class="py-3 px-4 text-right font-mono tabular text-slate-200">${fmtPrice(p.realized_price)}</td>
        <td class="py-3 px-4 text-right font-mono tabular font-medium ${moveColor}">${moveText}</td>
        <td class="py-3 px-4 text-center whitespace-nowrap">${statusBadge}${targetBadge}</td>
      </tr>
    `;
  }).join('');
}

function renderSentimentTable(predictions) {
  const tbody = document.getElementById('sentimentTableBody');
  if (!tbody) return;
  if (!predictions || !predictions.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="text-center py-6 text-slate-500">No sentiment records found in this window</td></tr>';
    return;
  }

  tbody.innerHTML = predictions.map(p => {
    const isBull = p.direction === 'bullish';
    const isBear = p.direction === 'bearish';
    const dirBadge = isBull 
      ? '<span class="px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-semibold">BULLISH</span>'
      : (isBear 
        ? '<span class="px-2 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20 font-semibold">BEARISH</span>'
        : '<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-semibold">ABSTAINED</span>');

    let statusBadge = '<span class="text-slate-500 font-mono">Pending...</span>';
    if (p.graded_wib) {
      if (p.is_noise) {
        statusBadge = '<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">NOISE</span>';
      } else if (p.correct === 1) {
        statusBadge = '<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-400 font-bold border border-emerald-500/30">WIN</span>';
      } else if (p.correct === 0) {
        statusBadge = '<span class="px-2 py-0.5 rounded bg-rose-500/20 text-rose-400 font-bold border border-rose-500/30">LOSS</span>';
      } else {
        statusBadge = '<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">NO-CALL</span>';
      }
    } else if (p.direction === 'no_call') {
      statusBadge = '<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">NO-CALL</span>';
    }

    const moveText = p.move !== null ? `${p.move > 0 ? '+' : ''}${p.move.toFixed(2)}` : '—';
    const moveColor = p.move > 0 ? 'text-emerald-400' : (p.move < 0 ? 'text-rose-400' : 'text-slate-400');
    
    let inputs = {};
    try { inputs = JSON.parse(p.inputs_json || '{}'); } catch(e){}
    const claimCount = (inputs.claims || []).length;
    const claimBadge = claimCount > 0
      ? `<span class="text-[10px] px-1.5 py-0.2 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-mono">${claimCount} Cited Sources</span>`
      : `<span class="text-[10px] px-1.5 py-0.2 rounded bg-slate-800 text-slate-400 font-mono">Macro Narrative</span>`;

    let summary = p.notes || "Grounded news sentiment extraction";

    return `
      <tr class="hover:bg-slate-800/40 transition-colors cursor-pointer group" onclick="openDetailsModal(${p.id})">
        <td class="py-3 px-4 font-mono font-medium text-slate-400 group-hover:text-amber-400">#${p.id}</td>
        <td class="py-3 px-4 text-slate-300 whitespace-nowrap">${p.created_wib.slice(5, 16)}</td>
        <td class="py-3 px-4 font-mono text-slate-400">${p.horizon}</td>
        <td class="py-3 px-4">${dirBadge}</td>
        <td class="py-3 px-4">
          <div class="flex items-center gap-2 text-slate-300 group-hover:text-amber-300">
            <span class="truncate max-w-md">${summary}</span>
            ${claimBadge}
            <span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700/60 font-mono whitespace-nowrap">View Citations &rarr;</span>
          </div>
        </td>
        <td class="py-3 px-4 text-right font-mono tabular">${p.ref_price.toFixed(2)}</td>
        <td class="py-3 px-4 text-right font-mono tabular text-white">${p.realized_price ? p.realized_price.toFixed(2) : '—'}</td>
        <td class="py-3 px-4 text-right font-mono tabular font-medium ${moveColor}">${moveText}</td>
        <td class="py-3 px-4 text-center">${statusBadge}</td>
      </tr>
    `;
  }).join('');
}

function openDetailsModal(id) {
  const p = currentPredictions.find(item => item.id === id);
  if (!p) return;

  const isBull = p.direction === 'bullish';
  const isBear = p.direction === 'bearish';
  
  const icon = document.getElementById('modalDirIcon');
  if (isBull) {
    icon.className = 'w-8 h-8 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 flex items-center justify-center font-bold text-sm';
    icon.textContent = '▲';
  } else if (isBear) {
    icon.className = 'w-8 h-8 rounded-lg bg-rose-500/10 text-rose-400 border border-rose-500/20 flex items-center justify-center font-bold text-sm';
    icon.textContent = '▼';
  } else {
    icon.className = 'w-8 h-8 rounded-lg bg-slate-800 text-slate-400 border border-slate-700 flex items-center justify-center font-bold text-sm';
    icon.textContent = '—';
  }

  document.getElementById('modalTitle').textContent = `Prediction #${p.id} — ${p.direction.toUpperCase()}`;
  document.getElementById('modalHorizonBadge').textContent = p.horizon.toUpperCase();
  document.getElementById('modalTimestamp').textContent = `${p.created_wib} (${p.signal_source.toUpperCase()})`;

  const isSent = p.signal_source === 'sentiment';
  const techSec = document.getElementById('modalTechnicalSection');
  const sentSec = document.getElementById('modalSentimentSection');

  if (isSent) {
    if (techSec) techSec.classList.add('hidden');
    if (sentSec) sentSec.classList.remove('hidden');

    let inputs = {};
    try { inputs = JSON.parse(p.inputs_json || '{}'); } catch(e){}
    const claims = inputs.claims || [];
    const claimsContainer = document.getElementById('modalClaimsContainer');

    document.getElementById('modalRationaleText').textContent = p.notes || "Qualitative sentiment synthesis from market news feeds.";

    if (claims.length > 0) {
      claimsContainer.innerHTML = claims.map((c, i) => {
        let domain = "";
        try { domain = new URL(c.source_url).hostname.replace('www.', ''); } catch(e){}
        return `
          <div class="bg-slate-950/40 p-3 rounded-xl border border-slate-800 space-y-2">
            <div class="text-slate-200 text-xs font-sans leading-relaxed">
              &ldquo;${c.text}&rdquo;
            </div>
            <div class="flex items-center justify-between text-[11px] font-mono pt-1.5 border-t border-slate-900">
              <span class="text-slate-400 font-semibold text-[10px] uppercase tracking-wider">${domain || 'Verified Feed'}</span>
              <a href="${c.source_url}" target="_blank" rel="noopener noreferrer" class="text-amber-400 hover:text-amber-300 hover:underline flex items-center gap-1 font-sans">
                <span>View Source Article</span>
                <span class="text-[10px]">&rarr;</span>
              </a>
            </div>
          </div>
        `;
      }).join('');
    } else {
      claimsContainer.innerHTML = `<div class="text-slate-500 py-3 text-center">No individual claim citations stored for this prediction.</div>`;
    }
  } else {
    if (techSec) techSec.classList.remove('hidden');
    if (sentSec) sentSec.classList.add('hidden');

    // Contributions: computed server-side from features_json+params_json for
    // every technical prediction (predictor.score() is pure)
    const contribs = p.contributions || {};

    // Populate Easy-to-understand Scorecard
    const scoreContainer = document.getElementById('modalScorecardContainer');
    const scoreBadge = document.getElementById('modalTotalScoreBadge');
    
    const scoreVal = p.score !== undefined ? p.score : (p.direction === 'bearish' ? -p.confidence : p.confidence || 0);
    const signedScore = `${scoreVal > 0 ? '+' : ''}${Number(scoreVal).toFixed(3)}`;
    const pBull = p.bullish_confidence !== undefined ? (p.bullish_confidence * 100).toFixed(1) : ((0.5 + 0.5 * scoreVal) * 100).toFixed(1);
    const pBear = p.bearish_confidence !== undefined ? (p.bearish_confidence * 100).toFixed(1) : ((0.5 - 0.5 * scoreVal) * 100).toFixed(1);
    scoreBadge.textContent = `Score: ${signedScore} | 🐂 ${pBull}% Bull · 🐻 ${pBear}% Bear (Bar: ±0.28)`;
    scoreBadge.className = `text-xs font-mono px-2 py-0.5 rounded font-bold ${scoreVal >= 0.28 ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' : (scoreVal <= -0.28 ? 'bg-rose-500/20 text-rose-400 border border-rose-500/30' : 'bg-slate-800 text-slate-300')}`;

    const indicatorMeta = {
      cross_venue_drift: {
        title: "Cross-Exchange Basis (Binance vs Global)",
        desc: "Is Binance spot price leading or lagging the global volume-weighted index (CoinGecko)?",
        explain: (v) => v > 0.02 ? "Binance trading at a discount to global spot; mean-reversion upwards expected." : (v < -0.02 ? "Binance trading at a premium; mean-reversion downwards expected." : "Binance in tight equilibrium with global exchanges.")
      },
      position: {
        title: "Price Position (vs 20-hour Average)",
        desc: "Are buyers paying more or less than the average price over the last 20 hours?",
        explain: (v) => v > 0.05 ? "Price trading well above recent moving average (strong buyer dominance)." : (v < -0.05 ? "Price pinned below recent moving average (seller dominance)." : "Price hovering right near the 20-hour average.")
      },
      rsi: {
        title: "RSI Momentum (Buying Speedometer)",
        desc: "How fast is price climbing or falling on a scale of 0 to 100?",
        explain: (v) => v > 0.05 ? "Speedometer shows strong upward buying momentum (> 50)." : (v < -0.05 ? "Speedometer shows downward selling momentum (< 50)." : "Momentum is balanced near the 50 neutral mark.")
      },
      trend: {
        title: "Moving Average Trend (Fast vs Slow)",
        desc: "Is the short-term 12h average moving faster than the medium-term 26h average?",
        explain: (v) => v > 0.02 ? "Fast 12-EMA sits above 26-EMA; the overall elevator is moving up." : (v < -0.02 ? "Fast 12-EMA sits under 26-EMA; the overall elevator is moving down." : "Fast and slow moving averages are flat/intertwined.")
      },
      momentum: {
        title: "24-Hour Velocity",
        desc: "Where is the price right now compared to exactly 24 hours ago?",
        explain: (v) => v > 0.02 ? "Positive gain over the past full day (healthy continuation)." : (v < -0.02 ? "Price is lower than 24 hours ago (acting as overhead drag/resistance)." : "Flat compared to yesterday.")
      },
      volume_confirm: {
        title: "Volume Confirmation",
        desc: "Are large volume traders actively backing up this move?",
        explain: (v) => v > 0.02 ? "High volume confirms and supports the directional move." : (v < -0.02 ? "Lower than average volume (move lacks strong institutional fuel)." : "Average trading volume.")
      },
      breadth: {
        title: "Range Breakout (6-Hour High/Low)",
        desc: "Did the current bar break out of the 6-hour range?",
        explain: (v) => v > 0 ? "Broke out to make a fresh 6-hour higher-high." : (v < 0 ? "Broke down to make a fresh 6-hour lower-low." : "Trading inside recent 6-hour boundaries (no breakout).")
      }
    };

    if (Object.keys(contribs).length > 0) {
      scoreContainer.innerHTML = Object.entries(contribs).map(([key, val]) => {
        const meta = indicatorMeta[key] || { title: key, desc: "", explain: () => "" };
        const isPos = val > 0.01;
        const isNeg = val < -0.01;
        
        const badgeClass = isPos ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : (isNeg ? 'bg-rose-500/10 text-rose-400 border border-rose-500/20' : 'bg-slate-800 text-slate-400 border border-slate-700/50');
        const voteText = isPos ? `+${val.toFixed(3)} Bullish` : (isNeg ? `${val.toFixed(3)} Bearish` : `0.000 Neutral`);

        return `
          <div class="bg-slate-950/40 p-3 rounded-xl border border-slate-800/80 flex flex-col sm:flex-row sm:items-center justify-between gap-2">
            <div class="space-y-1">
              <div class="flex items-center gap-2">
                <span class="font-semibold text-slate-200 text-xs">${meta.title}</span>
                <div class="group/tip relative inline-flex items-center">
                  <span class="w-4 h-4 rounded-full bg-slate-800 text-slate-400 group-hover/tip:bg-slate-700 group-hover/tip:text-amber-300 flex items-center justify-center text-[10px] font-mono cursor-help transition-colors border border-slate-700/60 font-bold">?</span>
                  <div class="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 hidden group-hover/tip:block w-56 p-2 bg-slate-900 border border-slate-700 text-slate-200 text-[11px] rounded-lg shadow-xl z-30 font-sans normal-case leading-normal pointer-events-none">
                    ${meta.desc}
                    <div class="w-2 h-2 bg-slate-900 border-r border-b border-slate-700 transform rotate-45 absolute -bottom-1 left-1/2 -translate-x-1/2"></div>
                  </div>
                </div>
                <span class="text-[10px] font-mono px-1.5 py-0.5 rounded font-semibold ${badgeClass}">${voteText}</span>
              </div>
              <div class="text-[11px] text-slate-300 font-sans italic">${meta.explain(val)}</div>
            </div>
          </div>
        `;
      }).join('');

      // Generate comprehensive summary purely from row data
      const posDrivers = Object.entries(contribs).filter(([k, v]) => v > 0.02).map(([k, v]) => indicatorMeta[k]?.title.split(' ')[0] || k);
      const negDrivers = Object.entries(contribs).filter(([k, v]) => v < -0.02).map(([k, v]) => indicatorMeta[k]?.title.split(' ')[0] || k);
      
      let driverSentence = "";
      if (p.direction === 'bullish') {
        if (posDrivers.length) driverSentence += `Upward momentum was driven primarily by <strong>${posDrivers.join(', ')}</strong>. `;
        if (negDrivers.length) driverSentence += `Counter-acting drag was noted from <strong>${negDrivers.join(', ')}</strong>. `;
      } else if (p.direction === 'bearish') {
        if (negDrivers.length) driverSentence += `Downward pressure was driven primarily by <strong>${negDrivers.join(', ')}</strong>. `;
        if (posDrivers.length) driverSentence += `Counter-acting support was noted from <strong>${posDrivers.join(', ')}</strong>. `;
      } else {
        driverSentence = "Neither buyers nor sellers showed sufficient conviction to clear the directional hurdle. ";
      }

      let actionText = "";
      if (p.notes && p.notes.includes("Abstained:")) {
        actionText = `Because regime risk was detected (<strong>${p.notes.split("Abstained:")[1].split(". ")[0].trim()}</strong>), Satoshi abstained from directional exposure.`;
      } else if (p.direction === 'no_call') {
        actionText = `Because the score fell inside the <strong>±0.28</strong> neutral zone, she wisely abstained to avoid flat market chop.`;
      } else {
        actionText = `Because the final score cleared the <strong>±0.28</strong> threshold, she committed to the call with an ATR volatility cushion of <strong>$${p.noise_threshold ? p.noise_threshold.toFixed(2) : 0}</strong>.`;
      }

      let summaryHtml = `Satoshi reached a <strong>${p.direction.toUpperCase()}</strong> call with a conviction score of <strong>${signedScore}</strong>. ${driverSentence}${actionText}`;

      document.getElementById('modalRationaleText').innerHTML = summaryHtml;
    } else {
      scoreContainer.innerHTML = `<div class="text-slate-500 py-3 text-center">No indicator contributions available for this prediction.</div>`;
      document.getElementById('modalRationaleText').textContent = p.notes || "Technical indicators confluence score calculated prior to bar formation.";
    }

    // Populate technical features
    const featuresGrid = document.getElementById('modalFeaturesGrid');
    let feats = {};
    try { feats = JSON.parse(p.features_json || '{}'); } catch(e){}

    const featureItems = [
      { label: 'RSI (14)', val: feats.rsi14 ? feats.rsi14.toFixed(1) : '—', color: feats.rsi14 > 55 ? 'text-emerald-400' : (feats.rsi14 < 45 ? 'text-rose-400' : 'text-slate-300') },
      { label: '20-SMA', val: feats.sma20 ? `$${feats.sma20.toFixed(1)}` : '—', color: 'text-slate-300' },
      { label: 'Fast EMA (12)', val: feats.ema12 ? `$${feats.ema12.toFixed(1)}` : '—', color: 'text-slate-300' },
      { label: 'Slow EMA (26)', val: feats.ema26 ? `$${feats.ema26.toFixed(1)}` : '—', color: 'text-slate-300' },
      { label: 'Volatility (ATR14)', val: feats.atr14 ? `$${feats.atr14.toFixed(2)}` : '—', color: 'text-amber-400' },
      { label: 'Volume vs Avg', val: feats.vol_vs_avg ? `${feats.vol_vs_avg.toFixed(2)}x` : '—', color: 'text-slate-300' }
    ];

    featuresGrid.innerHTML = featureItems.map(f => `
      <div class="bg-slate-950/40 p-2.5 rounded-lg border border-slate-800">
        <div class="text-[10px] text-slate-500 uppercase">${f.label}</div>
        <div class="font-bold text-xs mt-0.5 ${f.color}">${f.val}</div>
      </div>
    `).join('');
  }
  document.getElementById('modalRefPrice').textContent = `$${p.ref_price.toFixed(2)}`;
  document.getElementById('modalNoiseThr').textContent = p.noise_threshold ? `$${p.noise_threshold.toFixed(2)}` : '—';
  document.getElementById('modalRealizedPrice').textContent = p.realized_price ? `$${p.realized_price.toFixed(2)}` : 'In Progress...';

  // Populate Trade Targets (TP & SL) based on ATR
  let featsObj = {};
  try { featsObj = JSON.parse(p.features_json || '{}'); } catch(e){}
  let inputsObj = {};
  try { inputsObj = JSON.parse(p.inputs_json || '{}'); } catch(e){}

  const atrVal = featsObj.atr14 || (p.noise_threshold ? p.noise_threshold * 2.0 : 0);
  // Stored multipliers are authoritative; otherwise derive them from the actual
  // stored point deltas so the label can never claim a ratio the prices don't show.
  let tpMult = inputsObj.tp_mult;
  let slMult = inputsObj.sl_mult;
  if ((tpMult === undefined || tpMult === null) && inputsObj.target_tp_points && atrVal) {
    tpMult = Number((inputsObj.target_tp_points / atrVal).toFixed(2));
  }
  if ((slMult === undefined || slMult === null) && inputsObj.stop_loss_points && atrVal) {
    slMult = Number((inputsObj.stop_loss_points / atrVal).toFixed(2));
  }
  if (!tpMult) tpMult = ((p.horizon === '8h' || p.horizon === '12h') ? 2.0 : 1.5);
  if (!slMult) slMult = 1.0;
  const rrRatio = inputsObj.rr_ratio || (slMult > 0 ? Number((tpMult / slMult).toFixed(2)) : 1.5);

  const tpPts = inputsObj.target_tp_points !== undefined ? inputsObj.target_tp_points : (atrVal ? Number((atrVal * tpMult).toFixed(2)) : 0);
  const slPts = inputsObj.stop_loss_points !== undefined ? inputsObj.stop_loss_points : (atrVal ? Number((atrVal * slMult).toFixed(2)) : 0);
  const isBullSide = p.direction === 'bullish' || (p.direction === 'no_call' && (p.bullish_confidence !== undefined ? p.bullish_confidence : 0.5) >= (p.bearish_confidence !== undefined ? p.bearish_confidence : 0.5));
  const tpPrice = p.target_price || inputsObj.target_tp_price || (isBullSide ? p.ref_price + tpPts : p.ref_price - tpPts);
  const slPrice = p.sl_price || inputsObj.stop_loss_price || (isBullSide ? p.ref_price - slPts : p.ref_price + slPts);
  const tpSign = isBullSide ? '+' : '-';
  const slSign = isBullSide ? '-' : '+';
  const tpEl = document.getElementById('modalTpPrice');
  const tpPtsEl = document.getElementById('modalTpPoints');
  const slEl = document.getElementById('modalSlPrice');
  const slPtsEl = document.getElementById('modalSlPoints');
  const tpLabelEl = document.getElementById('modalTpLabel');
  const slLabelEl = document.getElementById('modalSlLabel');
  if (tpLabelEl) tpLabelEl.textContent = `Target (Take Profit · ${tpMult.toFixed(1)}× ATR · R:R 1:${rrRatio.toFixed(1)})`;
  if (slLabelEl) slLabelEl.textContent = `Stop Loss (${slMult.toFixed(1)}× ATR)`;
  if (tpEl) tpEl.textContent = `$${tpPrice.toFixed(2)}`;
  if (tpPtsEl) tpPtsEl.textContent = `${tpSign}$${tpPts.toFixed(2)} pts`;
  if (slEl) slEl.textContent = `$${slPrice.toFixed(2)}`;
  if (slPtsEl) slPtsEl.textContent = `${slSign}$${slPts.toFixed(2)} pts`;

  // Populate Target Reached Banner
  const bannerEl = document.getElementById('modalTargetReachedBanner');
  const iconEl = document.getElementById('modalTargetReachedIcon');
  const textEl = document.getElementById('modalTargetReachedText');
  const subEl = document.getElementById('modalTargetReachedSub');
  const horizonLabel = p.horizon || '1h';

  if (bannerEl) {
    if (p.graded_wib) {
      if (p.target_reached === 1) {
        bannerEl.className = 'p-3 rounded-xl border text-xs font-mono flex items-center justify-between bg-emerald-500/10 border-emerald-500/30 text-emerald-300';
        if (iconEl) iconEl.textContent = '🎯';
        if (textEl) textEl.textContent = `Target Reached during ${horizonLabel} bar!`;
        const ext = isBullSide ? p.bar_high : p.bar_low;
        if (subEl) subEl.textContent = `Target: $${tpPrice.toFixed(2)} · ${isBullSide ? 'High' : 'Low'}: $${ext ? ext.toFixed(2) : '—'}`;
      } else if (p.sl_hit === 1) {
        bannerEl.className = 'p-3 rounded-xl border text-xs font-mono flex items-center justify-between bg-rose-500/10 border-rose-500/30 text-rose-300';
        if (iconEl) iconEl.textContent = '🛑';
        if (textEl) textEl.textContent = `Stop Loss Hit during ${horizonLabel} bar!`;
        const ext = isBullSide ? p.bar_low : p.bar_high;
        if (subEl) subEl.textContent = `SL: $${slPrice.toFixed(2)} · ${isBullSide ? 'Low' : 'High'}: $${ext ? ext.toFixed(2) : '—'}`;
      } else {
        bannerEl.className = 'p-3 rounded-xl border text-xs font-mono flex items-center justify-between bg-slate-800/40 border-slate-700/60 text-slate-300';
        if (iconEl) iconEl.textContent = '⏳';
        if (textEl) textEl.textContent = `Target Not Reached during ${horizonLabel} bar`;
        if (subEl) {
          if (p.bar_low && p.bar_high) {
            subEl.textContent = `${horizonLabel} Bar Range: $${p.bar_low.toFixed(2)} – $${p.bar_high.toFixed(2)}`;
          } else {
            subEl.textContent = `Target: $${tpPrice.toFixed(2)}`;
          }
        }
      }
    } else {
      bannerEl.className = 'p-3 rounded-xl border text-xs font-mono flex items-center justify-between bg-amber-500/10 border-amber-500/20 text-amber-300';
      if (iconEl) iconEl.textContent = '⏱️';
      if (textEl) textEl.textContent = `Active ${horizonLabel} Bar — Monitoring Target Resolution`;
      if (subEl) subEl.textContent = `Target: $${tpPrice.toFixed(2)} | SL: $${slPrice.toFixed(2)}`;
    }
  }

  const outcomeEl = document.getElementById('modalOutcomeStatus');
  if (p.graded_wib) {
    if (p.is_noise) {
      outcomeEl.textContent = 'NOISE (Ignored)';
      outcomeEl.className = 'font-bold text-base mt-1 text-slate-400';
    } else if (p.correct === 1) {
      outcomeEl.textContent = `WIN (+${p.move.toFixed(2)} pts)`;
      outcomeEl.className = 'font-bold text-base mt-1 text-emerald-400';
    } else if (p.correct === 0) {
      outcomeEl.textContent = `LOSS (${p.move.toFixed(2)} pts)`;
      outcomeEl.className = 'font-bold text-base mt-1 text-rose-400';
    }
  } else {
    outcomeEl.textContent = 'Pending Grading';
    outcomeEl.className = 'font-bold text-base mt-1 text-amber-400';
  }

  // Sources & Inputs
  let inputs = {};
  try { inputs = JSON.parse(p.inputs_json || '{}'); } catch(e){}
  const sourcesDiv = document.getElementById('modalSourcesSection');
  if (inputs.paxg || inputs.spot) {
    sourcesDiv.classList.remove('hidden');
    document.getElementById('modalSourcesText').innerHTML = `
      Spot: <strong>$${inputs.spot || '—'}</strong> | PAXG: <strong>$${inputs.paxg || '—'}</strong> | Spread: <strong>$${inputs.spread || 0.01}</strong> | Macro Calendar Events: <strong>${inputs.calendar_events || 0}</strong>
    `;
  } else {
    sourcesDiv.classList.add('hidden');
  }

  document.getElementById('detailsModal').classList.remove('hidden');
}

function closeDetailsModal() {
  document.getElementById('detailsModal').classList.add('hidden');
}

function handleBackdropClick(event) {
  const card = document.getElementById('detailsModalCard');
  if (card && !card.contains(event.target)) {
    closeDetailsModal();
  }
}

// Close modal on Escape
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeDetailsModal();
});

// Auto-refresh every 30 seconds
loadData();
setInterval(loadData, 30000);

// Initialize Chart & Live Ticker
initCandleChart();
loadLivePrice();
setInterval(loadLivePrice, 15000); // 15s spot ticker refresh
// Candle refresh now lives inside initCandleChart's subscribeBar handler -
// NOT a resetData() poll, which used to wipe every drawn overlay within 30s.

function setChartTab(tab) {
  activeChartTab = tab;
  const tabCandles = document.getElementById('tabCandles');
  const tabTv = document.getElementById('tabTradingView');
  const btnCandles = document.getElementById('btn-tab-candles');
  const btnTv = document.getElementById('btn-tab-tv');
  const tfPills = document.getElementById('intervalPills');
  const tvIframe = document.getElementById('tvIframe');

  if (tab === 'candles') {
    tabCandles.classList.remove('hidden');
    tabTv.classList.add('hidden');
    tfPills.classList.remove('hidden');

    btnCandles.className = "px-3 py-1 rounded-md text-amber-400 font-semibold bg-slate-900 shadow";
    btnTv.className = "px-3 py-1 rounded-md text-slate-400 hover:text-white";

    if (candleChart) {
      setTimeout(() => {
        candleChart.resize();
      }, 50);
    }
  } else {
    tabCandles.classList.add('hidden');
    tabTv.classList.remove('hidden');
    tfPills.classList.add('hidden');

    btnTv.className = "px-3 py-1 rounded-md text-amber-400 font-semibold bg-slate-900 shadow";
    btnCandles.className = "px-3 py-1 rounded-md text-slate-400 hover:text-white";

    // Lazy load the TradingView embed iframe to save bandwidth
    if (tvIframe.src === "about:blank") {
      tvIframe.src = "https://s.tradingview.com/widgetembed/?symbol=BINANCE%3ASOLUSDT&interval=60&theme=dark&style=1&locale=en&hide_side_toolbar=1&allow_symbol_change=0&save_image=0";
    }
  }
}

function setChartSymbol(sym) {
  window.__hasUserSwitchedSymbol = true;
  currentChartSymbol = sym.toUpperCase();
  
  // Update header text
  const titleEl = document.getElementById('mainChartTitle');
  if (titleEl) titleEl.textContent = `${currentChartSymbol} Live Candlesticks & Overlays`;

  // Update select dropdown value
  const sel = document.getElementById('coinSelect');
  if (sel && sel.value !== currentChartSymbol) {
    sel.value = currentChartSymbol;
  }

  // Update TradingView iframe if active
  const tvIframe = document.getElementById('tvIframe');
  if (tvIframe && activeChartTab === 'tv') {
    tvIframe.src = `https://s.tradingview.com/widgetembed/?symbol=BINANCE%3A${currentChartSymbol}&interval=60&theme=dark&style=1&locale=en&hide_side_toolbar=1&allow_symbol_change=0&save_image=0`;
  }

  // Reload candles for this symbol
  if (candleChart) {
    candleChart.setSymbol({ ticker: currentChartSymbol });
    fetchAndApplyBars((bars) => {
      candleChart.applyNewData(bars);
    });
  }
}

function setCandleInterval(tf) {
  currentInterval = tf;
  ['15m', '1h', '4h', '1d'].forEach(t => {
    const btn = document.getElementById(`btn-tf-${t}`);
    if (btn) {
      if (t === tf) {
        btn.className = "px-2 py-1 rounded-md text-amber-400 font-semibold bg-slate-900 text-[11px] shadow";
      } else {
        btn.className = "px-2 py-1 rounded-md text-slate-400 hover:text-white text-[11px]";
      }
    }
  });
  if (candleChart) candleChart.setPeriod(periodForInterval(tf));
}

async function loadLivePrice() {
  try {
    const res = await fetch('/api/live');
    const d = await res.json();
    if (d.ok && d.spot) {
      document.getElementById('tickerSpot').textContent = `$${d.spot.toFixed(2)}`;
      const sign = d.basis >= 0 ? '+' : '';
      document.getElementById('tickerBasis').textContent = `${sign}$${d.basis.toFixed(2)} (${d.basis_pct}%)`;

      if (d.spots) {
        for (const [sym, px] of Object.entries(d.spots)) {
          const el = document.getElementById('price_' + sym);
          if (el) {
            const formatted = px < 0.1 ? `$${px.toFixed(5)}` : (px < 2.0 ? `$${px.toFixed(4)}` : (px < 100.0 ? `$${px.toFixed(3)}` : `$${px.toFixed(2)}`));
            el.textContent = formatted;
          }
        }
      }
    }
  } catch(e){}
}

function periodForInterval(iv) {
  const map = {
    '15m': { span: 15, type: 'minute' },
    '1h': { span: 1, type: 'hour' },
    '4h': { span: 4, type: 'hour' },
    '1d': { span: 1, type: 'day' },
  };
  return map[iv] || map['1h'];
}

function initCandleChart() {
  const container = document.getElementById('candleChartContainer');
  if (!container || !window.klinecharts) return;

  candleChart = klinecharts.init(container, {
    styles: 'dark',
    layout: [
      { type: 'candle', content: [], options: { order: 10 } },
    ],
  });
  candleChart.setStyles({
    grid: {
      horizontal: { color: 'rgba(255,255,255,0.04)' },
      vertical: { color: 'rgba(255,255,255,0.03)' },
    },
    candle: {
      bar: {
        upColor: '#10b981', downColor: '#f43f5e', noChangeColor: '#94a3b8',
        upBorderColor: '#10b981', downBorderColor: '#f43f5e',
        upWickColor: '#10b981', downWickColor: '#f43f5e',
      },
      tooltip: { showRule: 'always', showType: 'standard' },
    },
    crosshair: {
      horizontal: { line: { color: '#f59e0b' } },
      vertical: { line: { color: '#f59e0b' } },
    },
    xAxis: { axisLine: { color: 'rgba(255,255,255,0.08)' } },
    yAxis: { axisLine: { color: 'rgba(255,255,255,0.08)' } },
  });

  registerSatoshiIndicators();

  window.addEventListener('resize', () => {
    if (candleChart) candleChart.resize();
  });

  candleChart.setDataLoader({
    getBars: ({ callback }) => { fetchAndApplyBars(callback); },
    subscribeBar: ({ callback }) => {
      candleRefreshTimer = setInterval(() => {
        if (activeChartTab === 'candles') fetchAndApplyBars(null, callback);
      }, 30000);
    },
    unsubscribeBar: () => {
      if (candleRefreshTimer) { clearInterval(candleRefreshTimer); candleRefreshTimer = null; }
    },
  });
  candleChart.setSymbol({ ticker: 'SOLUSDT' });
  candleChart.setPeriod(periodForInterval(currentInterval));
}

async function fetchAndApplyBars(initCallback, pushCallback) {
  try {
    const sym = currentChartSymbol || 'SOLUSDT';
    const [cRes, mRes, iRes] = await Promise.all([
      fetch(`/api/candles?interval=${currentInterval}&limit=300&symbol=${sym}`),
      fetch(`/api/markers?signal=technical&symbol=${sym}`),
      fetch(`/api/indicators?interval=${currentInterval}&limit=300&symbol=${sym}`),
    ]);
    const cData = await cRes.json();
    const mData = await mRes.json();
    const iData = await iRes.json();

    if (iData.ok) window.__solanaIndData = iData;
    window.__solanaMarkerData = mData;

    if (cData.ok && cData.bars && cData.bars.length) {
      const klineBars = cData.bars.map(b => ({
        timestamp: b.time * 1000,
        open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume,
      }));

      if (initCallback) {
        initCallback(klineBars);
      } else if (pushCallback) {
        pushCallback(klineBars[klineBars.length - 1]);
      }

      applyActiveIndicators();
      applyMarkers(mData);
    } else if (initCallback) {
      initCallback([]);
    }
  } catch (e) {
    console.error('Failed to load chart candles/markers/indicators:', e);
    if (initCallback) initCallback([]);
  }
}

function applyMarkers(mData) {
  if (!candleChart || !mData || !mData.ok || !mData.markers) return;

  (window.__solanaMarkerIds || []).forEach(id => candleChart.removeOverlay({ id }));
  const newIds = [];

  const kLineDataList = candleChart.getDataList() || [];
  const barMap = {};
  kLineDataList.forEach(k => { barMap[k.timestamp] = k; });

  const byBar = {};
  mData.markers.forEach(m => {
    if (m.symbol && m.symbol !== currentChartSymbol) return;
    if (!m.target_bar_utc) return;
    if (!activeMarkerKinds[m.kind]) return;

    const entryTs = m.created_utc ? new Date(m.created_utc).getTime() : new Date(m.target_bar_utc).getTime();
    const hourMs = 3600 * 1000;
    const snappedTs = Math.floor(entryTs / hourMs) * hourMs;

    if (!byBar[snappedTs]) byBar[snappedTs] = [];
    byBar[snappedTs].push(m);
  });

  Object.keys(byBar).forEach(tsStr => {
    const ts = parseInt(tsStr, 10);
    const bar = barMap[ts];
    const candleHigh = bar ? bar.high : null;

    const list = byBar[ts];
    list.sort((a, b) => a.id - b.id);

    list.forEach((m, idx) => {
      const stackLevel = list.length - 1 - idx;

      let color = '#5a6b7c';
      let statusLabel = m.kind.toUpperCase();

      if (m.kind === 'win') {
        color = '#10b981';
        statusLabel = 'WIN';
      } else if (m.kind === 'loss') {
        color = '#f43f5e';
        statusLabel = 'LOSS';
      } else if (m.kind === 'noise') {
        color = '#5a6b7c';
        statusLabel = 'NOISE';
      } else if (m.kind === 'no_call_resolved' || m.kind === 'no_call_pending') {
        color = '#5a6b7c';
        statusLabel = 'NO-CALL';
      } else if (m.kind === 'pending') {
        color = '#f59e0b';
        statusLabel = 'PENDING';
      }

      let displayText = `#${m.id} ${statusLabel}`;
      if (m.direction && m.direction !== 'no_call' && m.kind === 'pending') {
        displayText += ` (${m.direction.toLowerCase()})`;
      }

      const refVal = candleHigh !== null ? candleHigh : m.ref_price;

      const id = candleChart.createOverlay({
        name: 'solanaSignalMarker',
        points: [{ timestamp: ts, value: refVal }],
        lock: true,
        extendData: {
          text: displayText,
          color: color,
          kind: m.kind,
          direction: m.direction,
          stackLevel: stackLevel,
        }
      });
      if (id) newIds.push(id);
    });
  });

  window.__solanaMarkerIds = newIds;
}

function toggleMarkerKind(kind) {
  activeMarkerKinds[kind] = !activeMarkerKinds[kind];
  if (kind === 'no_call_resolved') {
    activeMarkerKinds.no_call_pending = activeMarkerKinds.no_call_resolved;
  }
  const idMap = { win: 'btn-mk-win', loss: 'btn-mk-loss', pending: 'btn-mk-pending',
                  noise: 'btn-mk-noise', no_call_resolved: 'btn-mk-nocall' };
  const textColorMap = {
    win: 'text-emerald-300 bg-emerald-500/10 hover:bg-emerald-500/20',
    loss: 'text-rose-300 bg-rose-500/10 hover:bg-rose-500/20',
    pending: 'text-amber-300 bg-amber-500/10 hover:bg-amber-500/20',
    noise: 'text-slate-200 bg-slate-500/10 hover:bg-slate-500/20',
    no_call_resolved: 'text-zinc-300 bg-zinc-500/10 hover:bg-zinc-500/20',
  };
  const btn = document.getElementById(idMap[kind]);
  if (btn) {
    const on = activeMarkerKinds[kind];
    btn.className = `w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-xs text-left font-semibold ${on ? textColorMap[kind] : 'text-slate-400 hover:bg-slate-800 hover:text-white'}`;
    const dot = btn.querySelector('.ml-auto');
    if (dot) dot.className = `ml-auto text-[10px] ${on ? 'text-slate-500' : 'text-transparent'}`;
  }
  if (typeof updateIndBadge === 'function') updateIndBadge();
  if (window.__solanaMarkerData) {
    applyMarkers(window.__solanaMarkerData);
  }
}

function registerSatoshiIndicators() {
  const lineInd = (name, color, dataKey) => ({
    name, shortName: name, series: 'price',
    figures: [{ key: 'v', title: name + ': ', type: 'line' }],
    calc: (kLineDataList) => {
      const series = (window.__solanaIndData && window.__solanaIndData[dataKey]) || [];
      const byTime = {};
      series.forEach(p => { byTime[p.time] = p.value; });
      return kLineDataList.map(k => ({ v: byTime[Math.floor(k.timestamp / 1000)] }));
    },
    styles: { lines: [{ color, size: dataKey === 'sma20' ? 1 : 1.5, style: dataKey === 'sma20' ? 'dashed' : 'solid' }] },
  });

  klinecharts.registerIndicator(lineInd('EMA12', '#fbbf24', 'ema12'));
  klinecharts.registerIndicator(lineInd('EMA26', '#38bdf8', 'ema26'));
  klinecharts.registerIndicator(lineInd('SMA20', '#a78bfa', 'sma20'));

  klinecharts.registerIndicator({
    name: 'ATRBAND', shortName: 'ATR Band', series: 'price',
    figures: [
      { key: 'upper', title: 'ATR+: ', type: 'line' },
      { key: 'lower', title: 'ATR-: ', type: 'line' },
    ],
    calc: (kLineDataList) => {
      const series = (window.__solanaIndData && window.__solanaIndData.atr_band) || [];
      const byTime = {};
      series.forEach(p => { byTime[p.time] = p; });
      return kLineDataList.map(k => {
        const p = byTime[Math.floor(k.timestamp / 1000)];
        return { upper: p ? p.upper : undefined, lower: p ? p.lower : undefined };
      });
    },
    styles: { lines: [
      { color: 'rgba(148,163,184,0.5)', size: 1 },
      { color: 'rgba(148,163,184,0.5)', size: 1 },
    ] },
  });

  klinecharts.registerIndicator({
    name: 'RSI14', shortName: 'RSI', series: 'normal',
    figures: [{ key: 'v', title: 'RSI: ', type: 'line' }],
    calc: (kLineDataList) => {
      const series = (window.__solanaIndData && window.__solanaIndData.rsi14) || [];
      const byTime = {};
      series.forEach(p => { byTime[p.time] = p.value; });
      return kLineDataList.map(k => ({ v: byTime[Math.floor(k.timestamp / 1000)] }));
    },
    styles: { lines: [{ color: '#f472b6', size: 1.5 }] },
  });

  klinecharts.registerOverlay({
    name: 'solanaSignalMarker',
    totalStep: 2,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ overlay, coordinates }) => {
      if (!coordinates.length) return [];
      const { x, y } = coordinates[0];
      const data = overlay.extendData || {};
      const color = data.color || '#f59e0b';
      const text = data.text || '';
      const kind = data.kind || 'pending';
      const direction = data.direction || 'bearish';

      const figures = [];
      const stackLevel = data.stackLevel || 0;
      const stackOffset = stackLevel * 28;
      const tipY = y - 4 - stackOffset;

      if (kind === 'noise' || kind === 'no_call_resolved' || kind === 'no_call_pending') {
        const circleY = tipY - 4;
        figures.push({
          type: 'circle',
          attrs: { x, y: circleY, r: 3.5 },
          styles: { style: 'fill', color: color }
        });
        figures.push({
          type: 'text',
          attrs: { x, y: circleY - 6, text, align: 'center', baseline: 'bottom' },
          styles: {
            color: color,
            size: 10,
            family: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
            weight: '600',
            backgroundColor: 'transparent',
            borderColor: 'transparent',
            borderSize: 0,
            paddingLeft: 0,
            paddingRight: 0,
            paddingTop: 0,
            paddingBottom: 0,
          }
        });
      } else {
        const arrowH = 13;
        const stemW = 3.5;
        const headW = 9;
        const headH = 6;

        if (direction === 'bearish') {
          figures.push({
            type: 'polygon',
            attrs: {
              coordinates: [
                { x: x - stemW / 2, y: tipY - arrowH },
                { x: x + stemW / 2, y: tipY - arrowH },
                { x: x + stemW / 2, y: tipY - headH },
                { x: x + headW / 2, y: tipY - headH },
                { x: x,             y: tipY },
                { x: x - headW / 2, y: tipY - headH },
                { x: x - stemW / 2, y: tipY - headH },
              ]
            },
            styles: { style: 'fill', color: color }
          });
          figures.push({
            type: 'text',
            attrs: { x, y: tipY - arrowH - 3, text, align: 'center', baseline: 'bottom' },
            styles: {
              color: color,
              size: 10,
              family: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
              weight: '600',
              backgroundColor: 'transparent',
              borderColor: 'transparent',
              borderSize: 0,
              paddingLeft: 0,
              paddingRight: 0,
              paddingTop: 0,
              paddingBottom: 0,
            }
          });
        } else {
          figures.push({
            type: 'polygon',
            attrs: {
              coordinates: [
                { x: x,             y: tipY - arrowH },
                { x: x + headW / 2, y: tipY - arrowH + headH },
                { x: x + stemW / 2, y: tipY - arrowH + headH },
                { x: x + stemW / 2, y: tipY },
                { x: x - stemW / 2, y: tipY },
                { x: x - stemW / 2, y: tipY - arrowH + headH },
                { x: x - headW / 2, y: tipY - arrowH + headH },
              ]
            },
            styles: { style: 'fill', color: color }
          });
          figures.push({
            type: 'text',
            attrs: { x, y: tipY - arrowH - 3, text, align: 'center', baseline: 'bottom' },
            styles: {
              color: color,
              size: 10,
              family: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
              weight: '600',
              backgroundColor: 'transparent',
              borderColor: 'transparent',
              borderSize: 0,
              paddingLeft: 0,
              paddingRight: 0,
              paddingTop: 0,
              paddingBottom: 0,
            }
          });
        }
      }

      return figures;
    }
  });
}

function applyActiveIndicators() {
  if (!candleChart) return;

  const want = {
    ema12: 'EMA12', ema26: 'EMA26', sma20: 'SMA20',
    atr_band: 'ATRBAND', rsi14: 'RSI14',
  };

  Object.entries(want).forEach(([key, name]) => {
    const onCandlePane = key !== 'rsi14';
    if (activeIndicators[key]) {
      if (!indicatorIds[key]) {
        indicatorIds[key] = candleChart.createIndicator(
          { name, paneId: onCandlePane ? 'candle_pane' : undefined },
          onCandlePane,
        );
      }
    } else if (indicatorIds[key]) {
      candleChart.removeIndicator({ name, id: indicatorIds[key] });
      indicatorIds[key] = null;
    }
  });
}

function updateIndBadge() {
  const indCount = Object.values(activeIndicators).filter(Boolean).length;
  const primaryKinds = ['pending', 'win', 'loss', 'noise', 'no_call_resolved'];
  const mkCount = primaryKinds.filter(k => activeMarkerKinds[k]).length;
  const badge = document.getElementById('indCountBadge');
  if (badge) badge.textContent = `${indCount + mkCount}`;
}

function toggleIndicator(key) {
  activeIndicators[key] = !activeIndicators[key];
  const btn = document.getElementById(`btn-ind-${key}`);
  const textColorMap = {
    ema12: 'text-amber-300 bg-amber-500/10 hover:bg-amber-500/20',
    ema26: 'text-sky-300 bg-sky-500/10 hover:bg-sky-500/20',
    sma20: 'text-violet-300 bg-violet-500/10 hover:bg-violet-500/20',
    atr_band: 'text-slate-200 bg-slate-500/10 hover:bg-slate-500/20',
    rsi14: 'text-pink-300 bg-pink-500/10 hover:bg-pink-500/20',
  };
  if (btn) {
    const on = activeIndicators[key];
    btn.className = `w-full flex items-center gap-2 px-2.5 py-1.5 rounded-lg text-xs text-left font-semibold ${on ? textColorMap[key] : 'text-slate-400 hover:bg-slate-800 hover:text-white'}`;
    const dot = btn.querySelector('.ml-auto');
    if (dot) dot.className = `ml-auto text-[10px] ${on ? 'text-slate-500' : 'text-transparent'}`;
  }
  updateIndBadge();
  applyActiveIndicators();
}

function toggleIndicatorMenu() {
  const panel = document.getElementById('indMenuPanel');
  if (panel) panel.classList.toggle('hidden');
}

// Close the indicator dropdown when clicking anywhere outside it.
document.addEventListener('click', (e) => {
  const wrapper = document.getElementById('indicatorToggles');
  const panel = document.getElementById('indMenuPanel');
  if (wrapper && panel && !wrapper.contains(e.target)) {
    panel.classList.add('hidden');
  }
});

function startDrawing(overlayName) {
  if (!candleChart) return;
  const id = candleChart.createOverlay({ name: overlayName });
  if (id) drawnOverlayIds.push(id);
}

function clearDrawings() {
  if (!candleChart) return;
  drawnOverlayIds.forEach(id => candleChart.removeOverlay({ id }));
  drawnOverlayIds = [];
}
