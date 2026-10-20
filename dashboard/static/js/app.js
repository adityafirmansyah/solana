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

