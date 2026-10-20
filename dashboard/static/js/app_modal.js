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
  // Render Indicator Scorecard Breakdown
  if (typeof renderModalScorecard === "function") renderModalScorecard(p);
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

