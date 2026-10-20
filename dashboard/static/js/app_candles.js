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

