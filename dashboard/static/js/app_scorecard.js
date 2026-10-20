// Technical Indicator Scorecard Helper
function renderModalScorecard(p) {
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

}
