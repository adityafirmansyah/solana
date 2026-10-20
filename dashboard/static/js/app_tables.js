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
      } else if (p.move !== null) {
        // Fallback if correct is NULL but has realized move: grade by directional PnL
        const pnl = p.direction === 'bullish' ? p.move : -p.move;
        statusBadge = pnl > 0 
          ? '<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-400 font-bold border border-emerald-500/30">WIN</span>'
          : '<span class="px-2 py-0.5 rounded bg-rose-500/20 text-rose-400 font-bold border border-rose-500/30">LOSS</span>';
      } else {
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

