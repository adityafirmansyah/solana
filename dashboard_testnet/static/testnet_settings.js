    // Settings Modal Logic
    let currentSettings = null;

    async function openSettingsModal() {
      const modal = document.getElementById('settingsModal');
      modal.classList.remove('hidden');
      clearNotice();

      try {
        const res = await fetch('/api/settings');
        if (res.ok) {
          currentSettings = await res.json();
          populateSettingsForm(currentSettings);
        }
      } catch (err) {
        console.error('Failed to load settings:', err);
      }
    }

    function closeSettingsModal() {
      document.getElementById('settingsModal').classList.add('hidden');
    }

    // Close on backdrop click or ESC key
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeSettingsModal();
    });

    function populateSettingsForm(s) {
      if (!s) return;
      const lev = s.leverage || 10;
      document.getElementById('inputLeverageSlider').value = lev;
      document.getElementById('inputLeverageNumber').value = lev;
      document.getElementById('leverageDisplay').innerText = lev + 'x';

      document.getElementById('inputMarginPerTrade').value = (s.margin_per_trade_usd || 4.0).toFixed(2);
      document.getElementById('inputTargetNotional').value = (s.target_notional_usd || 40.0).toFixed(2);
      document.getElementById('inputMaxConcurrent').value = s.max_concurrent || 2;
      document.getElementById('inputMaxDailyLoss').value = (s.max_daily_loss || 10.0).toFixed(2);

      recalcMatrix();
    }

    function onLeverageChange(val) {
      val = Math.max(1, Math.min(50, parseInt(val) || 1));
      document.getElementById('inputLeverageSlider').value = val;
      document.getElementById('inputLeverageNumber').value = val;
      document.getElementById('leverageDisplay').innerText = val + 'x';

      const margin = parseFloat(document.getElementById('inputMarginPerTrade').value) || 4.0;
      const notional = (margin * val).toFixed(2);
      document.getElementById('inputTargetNotional').value = notional;
      recalcMatrix();
    }

    function onMarginChange(val) {
      const margin = Math.max(0.5, parseFloat(val) || 0.5);
      const lev = parseInt(document.getElementById('inputLeverageNumber').value) || 10;
      const notional = (margin * lev).toFixed(2);
      document.getElementById('inputTargetNotional').value = notional;
      recalcMatrix();
    }

    function onNotionalChange(val) {
      const notional = Math.max(5.0, parseFloat(val) || 5.0);
      const lev = parseInt(document.getElementById('inputLeverageNumber').value) || 10;
      const margin = (notional / lev).toFixed(2);
      document.getElementById('inputMarginPerTrade').value = margin;
      recalcMatrix();
    }

    function recalcMatrix() {
      const lev = parseInt(document.getElementById('inputLeverageNumber').value) || 10;
      const margin = parseFloat(document.getElementById('inputMarginPerTrade').value) || 4.0;
      const notional = parseFloat(document.getElementById('inputTargetNotional').value) || (margin * lev);
      const capital = (currentSettings && currentSettings.virtual_capital) || 100.0;
      const maxConc = parseInt(document.getElementById('inputMaxConcurrent').value) || 2;

      const marginPct = ((margin / capital) * 100).toFixed(1);
      document.getElementById('marginPctHint').innerText = marginPct + '% of $' + capital.toFixed(0) + ' Bankroll';

      document.getElementById('matrixTradeNotional').innerText = '$' + notional.toFixed(2);
      document.getElementById('matrixTradeMargin').innerText = '$' + margin.toFixed(2);

      const maxExposure = notional * maxConc;
      const maxLockedMargin = margin * maxConc;
      const maxLockedPct = ((maxLockedMargin / capital) * 100).toFixed(1);

      document.getElementById('matrixMaxExposure').innerText = '$' + maxExposure.toFixed(2);
      document.getElementById('matrixMaxMargin').innerText = '$' + maxLockedMargin.toFixed(2) + ' (' + maxLockedPct + '%)';

      const badge = document.getElementById('matrixStatusBadge');
      if (notional < 5.0) {
        badge.innerText = '⚠️ NOTIONAL < $5 (BINANCE MIN)';
        badge.className = 'text-amber-400 font-semibold';
      } else if (maxLockedMargin > capital * 0.5) {
        badge.innerText = '⚠️ HIGH MARGIN EXPOSURE (>50%)';
        badge.className = 'text-rose-400 font-semibold';
      } else {
        badge.innerText = '✓ SAFE BOUNDS';
        badge.className = 'text-emerald-400 font-semibold';
      }
    }

    function showNotice(msg, isError = false) {
      const el = document.getElementById('settingsNotice');
      el.classList.remove('hidden');
      if (isError) {
        el.className = 'p-3 rounded-lg text-xs font-mono border bg-rose-500/10 text-rose-300 border-rose-500/30';
      } else {
        el.className = 'p-3 rounded-lg text-xs font-mono border bg-emerald-500/10 text-emerald-300 border-emerald-500/30';
      }
      el.innerText = msg;
    }

    function clearNotice() {
      const el = document.getElementById('settingsNotice');
      el.classList.add('hidden');
      el.innerText = '';
    }

    async function saveSettingsForm(e) {
      e.preventDefault();
      clearNotice();

      const btn = document.getElementById('btnSaveSettings');
      const spinner = document.getElementById('saveSpinner');
      const btnText = document.getElementById('saveBtnText');

      btn.disabled = true;
      spinner.classList.remove('hidden');
      btnText.innerText = 'Syncing to Binance...';

      const payload = {
        leverage: parseInt(document.getElementById('inputLeverageNumber').value),
        margin_per_trade_usd: parseFloat(document.getElementById('inputMarginPerTrade').value),
        target_notional_usd: parseFloat(document.getElementById('inputTargetNotional').value),
        max_concurrent: parseInt(document.getElementById('inputMaxConcurrent').value),
        max_daily_loss: parseFloat(document.getElementById('inputMaxDailyLoss').value),
      };

      try {
        const res = await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (res.ok && data.status === 'ok') {
          showNotice('✓ Settings saved successfully! Leverage synced to Binance Futures testnet.', false);
          setTimeout(() => {
            closeSettingsModal();
            refreshData();
          }, 1200);
        } else {
          showNotice('❌ Error: ' + (data.message || 'Failed to save settings'), true);
        }
      } catch (err) {
        showNotice('❌ Network error: ' + err.message, true);
      } finally {
        btn.disabled = false;
        spinner.classList.add('hidden');
        btnText.innerText = 'Save & Sync to Binance';
      }
    }
