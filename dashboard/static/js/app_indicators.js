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
