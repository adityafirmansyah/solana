    function updateClock() {
      const now = new Date();
      // WIB = UTC+7
      const utc = now.getTime() + (now.getTimezoneOffset() * 60000);
      const wib = new Date(utc + (3600000 * 7));
      document.getElementById('clockWib').innerText = wib.toTimeString().split(' ')[0] + ' WIB';
    }
    setInterval(updateClock, 1000);
    updateClock();

    async function refreshData() {
      try {
        const [statsRes, posRes, histRes] = await Promise.all([
          fetch('/api/stats').then(r => r.json()),
          fetch('/api/positions').then(r => r.json()),
          fetch('/api/history').then(r => r.json())
        ]);

        renderStats(statsRes);
        renderPositions(posRes);
        renderHistory(histRes);
      } catch (err) {
        console.error('Failed to refresh dashboard data:', err);
      }
    }

    function renderStats(data) {
      if (!data) return;
      const equity = data.equity || 100.0;
      const avail = data.available_cash || 100.0;
      const margin = data.margin_locked || 0.0;
      const marginPct = ((margin / equity) * 100).toFixed(1);
      const todayPnl = data.today_pnl || 0.0;
      const todayPnlPct = ((todayPnl / 100.0) * 100).toFixed(2);
      const dailyLoss = Math.abs(data.daily_loss || 0.0);

      document.getElementById('kpiEquity').innerText = '$' + equity.toFixed(2);
      const delta = equity - 100.0;
      const deltaEl = document.getElementById('kpiEquityDelta');
      deltaEl.innerText = (delta >= 0 ? '+$' : '-$') + Math.abs(delta).toFixed(2) + ' (' + (delta >= 0 ? '+' : '') + delta.toFixed(2) + '%)';
      deltaEl.className = 'text-[11px] font-mono ' + (delta > 0 ? 'text-emerald-400' : (delta < 0 ? 'text-rose-400' : 'text-slate-400'));

      document.getElementById('kpiAvailable').innerText = '$' + avail.toFixed(2);
      document.getElementById('kpiMargin').innerText = '$' + margin.toFixed(2);
      document.getElementById('kpiMarginPct').innerText = marginPct + '% of $' + equity.toFixed(0);

      const pnlEl = document.getElementById('kpiTodayPnl');
      pnlEl.innerText = (todayPnl >= 0 ? '+$' : '-$') + Math.abs(todayPnl).toFixed(2);
      pnlEl.className = 'text-2xl font-bold font-mono ' + (todayPnl > 0 ? 'text-emerald-400' : (todayPnl < 0 ? 'text-rose-400' : 'text-slate-200'));
      document.getElementById('kpiTodayPnlPct').innerText = (todayPnl >= 0 ? '+' : '') + todayPnlPct + '% today';

      document.getElementById('kpiCircuitLoss').innerText = '$' + dailyLoss.toFixed(2);
      const circuitPct = Math.min(100, (dailyLoss / 10.0) * 100);
      const bar = document.getElementById('circuitBar');
      bar.style.width = circuitPct + '%';
      if (circuitPct >= 100) {
        bar.className = 'bg-rose-500 h-1.5 rounded-full';
        document.getElementById('kpiCircuitStatus').innerText = 'HALTED';
        document.getElementById('kpiCircuitStatus').className = 'px-1.5 py-0.5 rounded text-[10px] font-mono bg-rose-500/20 text-rose-400 border border-rose-500/30';
      } else {
        bar.className = 'bg-emerald-500 h-1.5 rounded-full';
        document.getElementById('kpiCircuitStatus').innerText = 'ARMED';
        document.getElementById('kpiCircuitStatus').className = 'px-1.5 py-0.5 rounded text-[10px] font-mono bg-emerald-500/20 text-emerald-400 border border-emerald-500/30';
      }

      const total = data.total_trades || 0;
      const wins = data.total_wins || 0;
      const wr = total > 0 ? ((wins / total) * 100).toFixed(1) : '--';
      document.getElementById('kpiWinRate').innerText = wr + (total > 0 ? '%' : '');
      document.getElementById('kpiTradeCount').innerText = wins + ' Wins / ' + total + ' Trades';

      if (data.target_margin_pct) {
        document.getElementById('ruleMarginPct').innerText = data.target_margin_pct;
        const hMarg = document.getElementById('headerMarginText');
        if (hMarg) hMarg.innerText = data.target_margin_pct + ' Margin';
      }
      if (data.target_margin_usd) document.getElementById('ruleMarginUsd').innerText = '(' + data.target_margin_usd + ')';
      if (data.target_notional_usd) document.getElementById('ruleNotional').innerText = '~$' + Number(data.target_notional_usd).toFixed(2) + ' USD';
      if (data.circuit_breaker_limit) document.getElementById('ruleCircuit').innerText = '$' + Number(data.circuit_breaker_limit).toFixed(2) + ' (10%)';

      if (data.settings) {
        const s = data.settings;
        if (s.virtual_capital) document.getElementById('ruleBankroll').innerText = '$' + Number(s.virtual_capital).toFixed(2) + ' USD';
        if (s.leverage) {
          document.getElementById('ruleLeverage').innerText = s.leverage + 'x ISOLATED';
          const hLev = document.getElementById('headerLeverageText');
          if (hLev) hLev.innerText = s.leverage + 'x Isolated';
        }
        if (s.max_concurrent) {
          document.getElementById('ruleMaxOpen').innerText = s.max_concurrent + ' Positions';
          const maxMarg = (s.margin_per_trade_usd || 4.0) * s.max_concurrent;
          document.getElementById('ruleMaxMargin').innerText = '(≤ $' + maxMarg.toFixed(0) + ' Margin)';
        }
      }
    }

    function renderPositions(positions) {
      const tbody = document.getElementById('positionsTableBody');
      const badge = document.getElementById('activeCountBadge');
      if (!positions || positions.length === 0) {
        badge.innerText = '0 / 2 Active';
        badge.className = 'px-2 py-0.5 rounded-full text-xs font-mono font-medium bg-slate-800 text-slate-400 border border-slate-700';
        tbody.innerHTML = `
          <tr>
            <td colspan="9" class="py-8 text-center text-slate-500">
              No active positions. Engine is monitoring 1s feed for Lead-Lag impulse...
            </td>
          </tr>
        `;
        return;
      }

      badge.innerText = `${positions.length} / 2 Active`;
      badge.className = 'px-2 py-0.5 rounded-full text-xs font-mono font-medium bg-amber-500/10 text-amber-300 border border-amber-500/30';

      tbody.innerHTML = positions.map(pos => {
        const isLong = pos.direction === 'bullish';
        const pnl = pos.unrealized_pnl_usd || 0.0;
        const pnlPct = pos.unrealized_pnl_pct || 0.0;
        const pnlClass = pnl >= 0 ? 'text-emerald-400' : 'text-rose-400';
        const sideBadge = isLong 
          ? '<span class="px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-bold">LONG</span>'
          : '<span class="px-2 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20 font-bold">SHORT</span>';
        const levBadge = (pos.leverage || 10) + 'x Iso';

        return `
          <tr class="hover:bg-slate-800/40 transition">
            <td class="py-3 px-4 font-bold text-white flex items-center gap-1.5">
              <span>${pos.symbol}</span>
            </td>
            <td class="py-3 px-4">${sideBadge} <span class="text-amber-400/90 font-mono text-[10px] font-semibold">${levBadge}</span></td>
            <td class="py-3 px-4 text-slate-300">$${pos.entry_price.toFixed(4)}</td>
            <td class="py-3 px-4 text-white font-bold">$${(pos.mark_price || pos.entry_price).toFixed(4)}</td>
            <td class="py-3 px-4 text-slate-300">${pos.entry_qty} ($${pos.notional_usd.toFixed(2)})</td>
            <td class="py-3 px-4 text-amber-400 font-semibold">$${pos.margin_used.toFixed(2)}</td>
            <td class="py-3 px-4 text-emerald-400 font-mono">$${pos.tp_price.toFixed(4)}</td>
            <td class="py-3 px-4 text-rose-400 font-mono">$${pos.sl_price.toFixed(4)}</td>
            <td class="py-3 px-4 text-right font-bold ${pnlClass}">
              ${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)} (${pnl >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%)
            </td>
          </tr>
        `;
      }).join('');
    }

    function renderHistory(history) {
      const tbody = document.getElementById('historyTableBody');
      const badge = document.getElementById('historyCountBadge');
      if (!history || history.length === 0) {
        badge.innerText = '0 Total';
        tbody.innerHTML = `
          <tr>
            <td colspan="9" class="py-8 text-center text-slate-500">
              No closed executions yet.
            </td>
          </tr>
        `;
        return;
      }

      badge.innerText = `${history.length} Total`;
      tbody.innerHTML = history.slice(0, 30).map(row => {
        const isLong = row.direction === 'bullish';
        const pnl = row.pnl_usd || 0.0;
        const pnlPct = row.pnl_pct || 0.0;
        const pnlClass = pnl > 0 ? 'text-emerald-400' : (pnl < 0 ? 'text-rose-400' : 'text-slate-400');

        let reasonBadge = '<span class="px-1.5 py-0.5 rounded bg-slate-800 text-slate-400">TIME_STOP</span>';
        if (row.exit_reason === 'tp_hit') {
          reasonBadge = '<span class="px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">🎯 TP WIN</span>';
        } else if (row.exit_reason === 'sl_hit') {
          reasonBadge = '<span class="px-1.5 py-0.5 rounded bg-rose-500/20 text-rose-400 border border-rose-500/30">🛑 SL LOSS</span>';
        }

        return `
          <tr class="hover:bg-slate-800/40 transition">
            <td class="py-2.5 px-4 text-slate-400">${row.closed_wib || row.closed_utc || '--'}</td>
            <td class="py-2.5 px-4 font-bold text-slate-300">#${row.prediction_id || row.id}</td>
            <td class="py-2.5 px-4 font-semibold text-white">${row.symbol}</td>
            <td class="py-2.5 px-4 ${isLong ? 'text-emerald-400' : 'text-rose-400'} font-semibold">
              ${isLong ? 'LONG' : 'SHORT'}
            </td>
            <td class="py-2.5 px-4 text-slate-300">$${row.entry_price.toFixed(4)}</td>
            <td class="py-2.5 px-4 text-white">$${row.exit_price.toFixed(4)}</td>
            <td class="py-2.5 px-4">${reasonBadge}</td>
            <td class="py-2.5 px-4 text-slate-400">${row.duration || '--'}</td>
            <td class="py-2.5 px-4 text-right font-bold ${pnlClass}">
              ${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)} (${pnl >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%)
            </td>
          </tr>
        `;
      }).join('');
    }

    // Auto refresh every 3 seconds
    refreshData();
    setInterval(refreshData, 3000);

