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

