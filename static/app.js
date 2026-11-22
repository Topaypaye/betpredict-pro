// BetPredict Pro - Frontend Controller

let currentTab = 'football';
let currentLeague = 'EPL';
let showFinished = true;
let showFinishedNba = true;
let selectedConfidence = 'ALL';
let selectedNbaConfidence = 'ALL';
let selectedTennisConfidence = 'ALL';
let appStatus = null;
let currentModalMatch = null;

// In-memory pre-fetch cache & shared in-flight requests for instant 0ms tab switching
const appCache = {};
const inFlight = {};

// Calendar state
let dailyPicksData = null;
let calSelectedSport = 'ALL';
let calSelectedMonth = null;
let calSelectedDate = null;
let calHistoryFilter = 'ALL';

const LEAGUE_NAMES = {
  EPL: '🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League',
  LALIGA: '🇪🇸 LaLiga',
  SERIEA: '🇮🇹 Serie A',
  BUNDESLIGA: '🇩🇪 Bundesliga',
  LIGUE1: '🇫🇷 Ligue 1',
  UCL: '🇪🇺 Champions League',
  UEL: '🇪🇺 Europa League',
  NATIONS: '🌐 Nations League'
};

async function fetchCached(key, url, forceRefresh = false) {
  if (!forceRefresh && appCache[key]) {
    return appCache[key];
  }
  if (!forceRefresh && inFlight[key]) {
    return await inFlight[key];
  }
  const p = (async () => {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 65000);
    try {
      const res = await fetch(url, { signal: controller.signal });
      const data = await res.json();
      appCache[key] = data;
      return data;
    } finally {
      clearTimeout(timeoutId);
      delete inFlight[key];
    }
  })();
  inFlight[key] = p;
  return await p;
}

function clearDataCache() {
  Object.keys(appCache).forEach(k => delete appCache[k]);
}

function updateSyncBadge(text, stateClass) {
  const el = document.getElementById('bgSyncBadge');
  if (!el) return;
  el.className = `sync-badge ${stateClass}`;
  el.innerText = text;
}

async function refreshAllTabsInBackground() {
  const tasks = [
    { key: 'nba_games', url: '/api/nba/games?with_finished=true' },
    { key: 'tennis_matches', url: '/api/tennis/matches' },
    { key: 'daily_picks', url: '/api/daily-picks' },
    { key: 'fb_EPL', url: '/api/football/fixtures?league=EPL&with_finished=true' },
    { key: 'fb_LALIGA', url: '/api/football/fixtures?league=LALIGA&with_finished=true' },
    { key: 'fb_SERIEA', url: '/api/football/fixtures?league=SERIEA&with_finished=true' },
    { key: 'fb_BUNDESLIGA', url: '/api/football/fixtures?league=BUNDESLIGA&with_finished=true' },
    { key: 'fb_LIGUE1', url: '/api/football/fixtures?league=LIGUE1&with_finished=true' },
    { key: 'fb_UCL', url: '/api/football/fixtures?league=UCL&with_finished=true' },
    { key: 'fb_UEL', url: '/api/football/fixtures?league=UEL&with_finished=true' },
    { key: 'fb_NATIONS', url: '/api/football/fixtures?league=NATIONS&with_finished=true' },
    { key: 'bt_football', url: '/api/backtest/football' },
    { key: 'bt_nba', url: '/api/backtest/nba' },
    { key: 'bt_tennis', url: '/api/backtest/tennis' },
  ];

  let doneCount = 0;
  const total = tasks.length;
  updateSyncBadge(`⚡ Syncing tabs (0/${total})...`, 'syncing');

  // Trigger server-side warmup thread concurrently
  fetch('/api/warmup', { method: 'POST' }).catch(() => {});

  // Run background fetches in controlled concurrency so UI remains ultra-responsive
  const concurrency = 4;
  const queue = [...tasks];
  async function worker() {
    while (queue.length > 0) {
      const item = queue.shift();
      if (!item) break;
      try {
        await fetchCached(item.key, item.url, false);
        if (item.key === 'daily_picks' && appCache.daily_picks) {
          dailyPicksData = appCache.daily_picks;
        }
      } catch (e) {
        // non-fatal background prefetch error
      }
      doneCount++;
      if (doneCount < total) {
        updateSyncBadge(`⚡ Syncing tabs (${doneCount}/${total})...`, 'syncing');
      }
    }
  }
  await Promise.all(Array.from({ length: concurrency }, () => worker()));
  updateSyncBadge('✓ All Tabs Ready', 'ready');
}

// ------------------------------------------------------------- Init & Service Worker
document.addEventListener('DOMContentLoaded', async () => {
  setupNavigation();
  setupSettingsHandlers();
  await loadStatus();
  await loadFootballFixtures();

  // Immediately pre-load all other tabs & leagues in the background
  refreshAllTabsInBackground();

  // Register PWA service worker
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js').catch(err => {
      console.log('SW registration note:', err);
    });
  }
});

// ------------------------------------------------------------- Navigation
function setupNavigation() {
  document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const tab = e.currentTarget.dataset.tab;
      switchTab(tab);
    });
  });

  document.querySelectorAll('#footballSubNav .pill-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      document.querySelectorAll('#footballSubNav .pill-btn').forEach(b => b.classList.remove('active'));
      e.currentTarget.classList.add('active');
      currentLeague = e.currentTarget.dataset.league;
      document.getElementById('leagueTitle').innerText = LEAGUE_NAMES[currentLeague] + ' Fixtures';
      loadFootballFixtures();
    });
  });

  document.querySelectorAll('#footballConfFilter .conf-pill').forEach(btn => {
    btn.addEventListener('click', (e) => {
      document.querySelectorAll('#footballConfFilter .conf-pill').forEach(b => b.classList.remove('active'));
      e.currentTarget.classList.add('active');
      selectedConfidence = e.currentTarget.dataset.conf;
      loadFootballFixtures();
    });
  });

  document.getElementById('chkFinished').addEventListener('change', (e) => {
    showFinished = e.target.checked;
    loadFootballFixtures();
  });

  const btnToggleAll = document.getElementById('btnToggleAllSections');
  if (btnToggleAll && !btnToggleAll.dataset.wired) {
    btnToggleAll.dataset.wired = 'true';
    btnToggleAll.addEventListener('click', () => {
      const sections = document.querySelectorAll('.matchweek-section');
      const anyOpen = Array.from(sections).some(s => !s.classList.contains('collapsed'));
      sections.forEach(s => {
        if (anyOpen) {
          s.classList.add('collapsed');
        } else {
          s.classList.remove('collapsed');
        }
      });
    });
  }

  document.querySelectorAll('#tennisConfFilter .conf-pill').forEach(btn => {
    btn.addEventListener('click', (e) => {
      document.querySelectorAll('#tennisConfFilter .conf-pill').forEach(b => b.classList.remove('active'));
      e.currentTarget.classList.add('active');
      selectedTennisConfidence = e.currentTarget.dataset.conf;
      loadTennisMatches();
    });
  });

  const chkUpcoming = document.getElementById('chkUpcomingTennis');
  if (chkUpcoming) chkUpcoming.addEventListener('change', () => loadTennisMatches());
  const chkHideTbd = document.getElementById('chkHideTbdTennis');
  if (chkHideTbd) chkHideTbd.addEventListener('change', () => loadTennisMatches());

  // NBA controls
  document.querySelectorAll('#nbaConfFilter .conf-pill').forEach(btn => {
    btn.addEventListener('click', (e) => {
      document.querySelectorAll('#nbaConfFilter .conf-pill').forEach(b => b.classList.remove('active'));
      e.currentTarget.classList.add('active');
      selectedNbaConfidence = e.currentTarget.dataset.conf;
      loadNbaGames();
    });
  });

  const chkFinNba = document.getElementById('chkFinishedNba');
  if (chkFinNba) {
    chkFinNba.addEventListener('change', (e) => {
      showFinishedNba = e.target.checked;
      loadNbaGames();
    });
  }

  const btnToggleNba = document.getElementById('btnToggleAllNba');
  if (btnToggleNba && !btnToggleNba.dataset.wired) {
    btnToggleNba.dataset.wired = 'true';
    btnToggleNba.addEventListener('click', () => {
      const sections = document.querySelectorAll('#nbaList .matchweek-section');
      const anyOpen = Array.from(sections).some(s => !s.classList.contains('collapsed'));
      sections.forEach(s => {
        if (anyOpen) s.classList.add('collapsed');
        else s.classList.remove('collapsed');
      });
    });
  }

  const btnRefreshNba = document.getElementById('btnRefreshNba');
  if (btnRefreshNba) btnRefreshNba.addEventListener('click', () => loadNbaGames(true));

  const btnToggleTennis = document.getElementById('btnToggleAllTennis');
  if (btnToggleTennis && !btnToggleTennis.dataset.wired) {
    btnToggleTennis.dataset.wired = 'true';
    btnToggleTennis.addEventListener('click', () => {
      const sections = document.querySelectorAll('#tennisList .matchweek-section');
      const anyOpen = Array.from(sections).some(s => !s.classList.contains('collapsed'));
      sections.forEach(s => {
        if (anyOpen) s.classList.add('collapsed');
        else s.classList.remove('collapsed');
      });
    });
  }

  document.getElementById('btnRefreshFootball').addEventListener('click', () => loadFootballFixtures(true));
  document.getElementById('btnRefreshTennis').addEventListener('click', () => loadTennisMatches(true));

  const btnRefreshCal = document.getElementById('btnRefreshCalendar');
  if (btnRefreshCal) btnRefreshCal.addEventListener('click', () => loadDailyPicks(true));

  document.querySelectorAll('#calSportFilter .conf-pill').forEach(btn => {
    btn.addEventListener('click', (e) => {
      document.querySelectorAll('#calSportFilter .conf-pill').forEach(b => b.classList.remove('active'));
      e.currentTarget.classList.add('active');
      calSelectedSport = e.currentTarget.dataset.sport;
      renderDailyCalendarView();
    });
  });

  document.querySelectorAll('#calHistoryFilter .conf-pill').forEach(btn => {
    btn.addEventListener('click', (e) => {
      document.querySelectorAll('#calHistoryFilter .conf-pill').forEach(b => b.classList.remove('active'));
      e.currentTarget.classList.add('active');
      calHistoryFilter = e.currentTarget.dataset.hfilter;
      renderCalendarHistoryTable();
    });
  });

  // Model Guide Sub-Nav
  document.querySelectorAll('#docsSubNav .pill-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      document.querySelectorAll('#docsSubNav .pill-btn').forEach(b => b.classList.remove('active'));
      e.currentTarget.classList.add('active');
      const targetDoc = e.currentTarget.dataset.doc;
      document.querySelectorAll('.doc-content-block').forEach(blk => {
        blk.style.display = blk.id === targetDoc ? 'flex' : 'none';
      });
    });
  });
}

function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.nav-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  
  document.getElementById('viewFootball').style.display = tab === 'football' ? 'block' : 'none';
  document.getElementById('footballSubNav').style.display = tab === 'football' ? 'flex' : 'none';
  document.getElementById('viewNba').style.display = tab === 'nba' ? 'block' : 'none';
  document.getElementById('viewTennis').style.display = tab === 'tennis' ? 'block' : 'none';
  const viewCal = document.getElementById('viewCalendar');
  if (viewCal) viewCal.style.display = tab === 'calendar' ? 'block' : 'none';
  document.getElementById('viewModels').style.display = tab === 'models' ? 'block' : 'none';
  document.getElementById('viewDocs').style.display = tab === 'docs' ? 'block' : 'none';
  document.getElementById('viewBacktest').style.display = tab === 'backtest' ? 'block' : 'none';

  if (tab === 'football') loadFootballFixtures();
  if (tab === 'nba') loadNbaGames();
  if (tab === 'tennis') loadTennisMatches();
  if (tab === 'calendar') loadDailyPicks();
  if (tab === 'models') renderModelSliders();
  if (tab === 'backtest') loadBacktestData();
}

// ------------------------------------------------------------- Status & Settings
async function loadStatus() {
  try {
    const res = await fetch('/api/status');
    appStatus = await res.json();
    renderModelSliders();
  } catch (err) {
    console.error('Error loading status:', err);
  }
}

let currentSimSport = 'football';
let simDraft = {
  football: { market_share: null, stats_weights: null, dirty: false },
  nba: { market_share: null, stats_weights: null, dirty: false },
  tennis: { market_share: null, stats_weights: null, dirty: false },
};
let simDebounceTimer = null;

function _getSportSavedConfig(sport) {
  const s = appStatus?.settings || {};
  const wObj = appStatus?.weights?.[sport] || {};
  const ms = sport === 'football'
    ? (s.football_market_share ?? wObj.market_share ?? 0.65)
    : (sport === 'nba' ? (s.nba_market_share ?? wObj.market_share ?? 0.30) : (s.tennis_market_share ?? wObj.market_share ?? 0.65));
  const sw = { ...(wObj.stats_weights || {}) };
  return { market_share: ms, stats_weights: sw };
}

function renderModelSliders() {
  if (!appStatus) return;
  const sport = currentSimSport;
  const saved = _getSportSavedConfig(sport);
  const draft = simDraft[sport];
  const ms = draft.market_share !== null ? draft.market_share : saved.market_share;
  const sw = draft.stats_weights !== null ? draft.stats_weights : saved.stats_weights;
  const ve = appStatus.settings?.value_edge_min ?? 0.04;

  // Market share & Value Edge sliders
  const rngMs = document.getElementById('rngSimMarketShare');
  if (rngMs) {
    rngMs.value = ms;
    const msPct = Math.round(ms * 100);
    document.getElementById('lblSimMarketTitle').innerText = `${formatVarName(sport)} Market Share`;
    document.getElementById('lblSimMarketShare').innerText = `${msPct}%`;
    const splitBadge = document.getElementById('simBlendSplitBadge');
    if (splitBadge) splitBadge.innerText = `${msPct}% Market / ${100 - msPct}% Stats`;
  }
  const rngVe = document.getElementById('rngValueEdge');
  if (rngVe) {
    rngVe.value = ve;
    document.getElementById('lblValueEdge').innerText = '+' + (ve * 100).toFixed(1) + '%';
  }

  // Statistical variable sliders (showing normalized % so user sees exact effective share)
  const totalRaw = Object.values(sw).reduce((a, b) => a + Number(b || 0), 0) || 1;
  const infoMap = appStatus.weights?.[sport]?.component_info || {};
  const container = document.getElementById('simSlidersContainer');
  const titleEl = document.getElementById('simVarsTitle');
  const colorVar = sport === 'football' ? 'var(--primary)' : (sport === 'nba' ? 'var(--accent-amber)' : 'var(--accent-purple)');
  if (titleEl) {
    titleEl.innerText = `${formatVarName(sport)} Statistical Variables`;
    titleEl.style.color = colorVar;
  }

  if (container) {
    container.innerHTML = Object.entries(sw).map(([k, v]) => {
      const normPct = ((Number(v || 0) / totalRaw) * 100).toFixed(1);
      return `
        <div class="slider-group">
          <div class="slider-label">
            <span>${formatVarName(k)}</span>
            <span id="lbl_sim_${k}" style="color:${colorVar}">${normPct}%</span>
          </div>
          <input type="range" class="slider-input sim-var-slider" data-var="${k}" min="0" max="1" step="0.01" value="${v}">
          <span style="font-size:0.7rem; color:var(--text-dim);">${infoMap[k] || ''}</span>
        </div>
      `;
    }).join('');
  }

  // Situational Adjustments summary for selected sport
  const sitEl = document.getElementById('simSituationalList');
  if (sitEl) {
    if (sport === 'football') {
      sitEl.innerHTML = `
        <div class="var-row"><span class="var-name">Home Advantage</span><span class="var-val">+46 Elo points</span></div>
        <div class="var-row"><span class="var-name">Key Player Missing</span><span class="var-val">-3.5% xG per player (cap -15%)</span></div>
        <div class="var-row"><span class="var-name">Short Rest (&le;3 days)</span><span class="var-val">-3.0% attacking xG</span></div>
      `;
    } else if (sport === 'nba') {
      sitEl.innerHTML = `
        <div class="var-row"><span class="var-name">Home Court Advantage</span><span class="var-val">+50 Elo (~2.2 pts)</span></div>
        <div class="var-row"><span class="var-name">Star / Starter Missing</span><span class="var-val">-2.8 pts margin per player (cap -11 pts)</span></div>
        <div class="var-row"><span class="var-name">Back-to-Back (0d rest)</span><span class="var-val">-2.2 pts margin penalty</span></div>
      `;
    } else {
      sitEl.innerHTML = `
        <div class="var-row"><span class="var-name">7-Day Set Fatigue</span><span class="var-val">-1.2% per net set (cap &plusmn;6%)</span></div>
        <div class="var-row"><span class="var-name">Best-of-5 Grand Slam Scaling</span><span class="var-val">Binomial Best-of-3 &rarr; Best-of-5</span></div>
        <div class="var-row"><span class="var-name">Surface Blend</span><span class="var-val">50% Overall Elo + 50% Surface Elo</span></div>
      `;
    }
  }

  const btnApply = document.getElementById('btnApplyWeights');
  if (btnApply) btnApply.style.display = draft.dirty ? 'inline-flex' : 'none';

  attachSliderEvents();
  runLiveSimulation();
}

function formatVarName(str) {
  return str.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
}

function _refreshNormalizedLabels() {
  const sliders = Array.from(document.querySelectorAll('.sim-var-slider'));
  const total = sliders.reduce((acc, s) => acc + parseFloat(s.value || 0), 0) || 1;
  sliders.forEach(s => {
    const lbl = document.getElementById('lbl_sim_' + s.dataset.var);
    if (lbl) lbl.innerText = ((parseFloat(s.value || 0) / total) * 100).toFixed(1) + '%';
  });
}

function _readCurrentDraftFromDom() {
  const ms = parseFloat(document.getElementById('rngSimMarketShare')?.value ?? 0.65);
  const ve = parseFloat(document.getElementById('rngValueEdge')?.value ?? 0.04);
  const sw = {};
  document.querySelectorAll('.sim-var-slider').forEach(s => {
    sw[s.dataset.var] = parseFloat(s.value || 0);
  });
  return { market_share: ms, stats_weights: sw, value_edge_min: ve };
}

function scheduleLiveSimulation() {
  clearTimeout(simDebounceTimer);
  simDebounceTimer = setTimeout(() => runLiveSimulation(), 110);
}

async function runLiveSimulation() {
  const sport = currentSimSport;
  const { market_share, stats_weights, value_edge_min } = _readCurrentDraftFromDom();
  try {
    const res = await fetch('/api/simulate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sport, market_share, stats_weights, value_edge_min })
    });
    if (!res.ok) return;
    const data = await res.json();
    renderSimulationImpact(data);
  } catch (err) {
    console.error('Simulation error:', err);
  }
}

function _fmtDeltaBadge(el, diffVal, unit = '%', higherIsBetter = true, decimals = 1) {
  if (!el) return;
  const threshold = unit === '%' ? 0.0005 : 0.0005;
  if (Math.abs(diffVal) < threshold) {
    el.className = 'sim-delta neutral';
    el.innerText = '0.0' + unit + ' (Baseline)';
    return;
  }
  const isGood = higherIsBetter ? diffVal > 0 : diffVal < 0;
  const sign = diffVal > 0 ? '+' : '';
  const arrow = diffVal > 0 ? '▲' : '▼';
  const disp = unit === '%' ? (diffVal * 100).toFixed(decimals) : diffVal.toFixed(decimals);
  el.className = `sim-delta ${isGood ? 'pos' : 'neg'}`;
  el.innerText = `${sign}${disp}${unit} ${arrow}`;
}

function renderSimulationImpact(data) {
  if (!data || !data.current) return;
  const cur = data.current;
  const base = data.default_baseline;
  const draft = simDraft[data.sport];

  const lblSample = document.getElementById('simSampleLabel');
  if (lblSample) {
    lblSample.innerHTML = `<strong>${data.sample_label}</strong> &bull; ${cur.matches.toLocaleString()} evaluated matches`;
  }
  const pill = document.getElementById('simStatePill');
  if (pill) {
    if (draft.dirty) {
      pill.className = 'badge badge-amber';
      pill.innerText = '⚡ Unsaved Preview (Click Apply & Save to use live)';
    } else {
      pill.className = 'badge badge-green';
      pill.innerText = '✓ Active Saved Weights';
    }
  }

  // 1. Overall Winrate %
  document.getElementById('simWinrate').innerText = `${(cur.accuracy * 100).toFixed(1)}%`;
  _fmtDeltaBadge(document.getElementById('simWinrateDelta'), cur.accuracy - base.accuracy, '%', true, 2);
  document.getElementById('simWinrateSub').innerText =
    `${cur.hits} / ${cur.matches} wins (Default: ${(base.accuracy * 100).toFixed(1)}%)`;

  // 2. High-Confidence Winrate %
  const hcPct = Math.round((cur.high_conf_threshold || 0.6) * 100);
  document.getElementById('simHcTitle').innerText = `HIGH-CONF (≥${hcPct}%) WINRATE`;
  document.getElementById('simHcWinrate').innerText = cur.high_conf_matches > 0
    ? `${(cur.high_conf_win_rate * 100).toFixed(1)}%` : '-';
  _fmtDeltaBadge(document.getElementById('simHcDelta'), cur.high_conf_win_rate - base.high_conf_win_rate, '%', true, 2);
  document.getElementById('simHcSub').innerText =
    `${cur.high_conf_hits} / ${cur.high_conf_matches} wins (Default: ${(base.high_conf_win_rate * 100).toFixed(1)}%)`;

  // 3. Value Bets ROI
  const vb = cur.value_bets || {};
  const bvb = base.value_bets || {};
  const roiSign = (vb.roi || 0) >= 0 ? '+' : '';
  document.getElementById('simVbRoi').innerText = vb.bets > 0 ? `${roiSign}${((vb.roi || 0) * 100).toFixed(1)}%` : '-';
  _fmtDeltaBadge(document.getElementById('simVbDelta'), (vb.roi || 0) - (bvb.roi || 0), '%', true, 1);
  document.getElementById('simVbSub').innerText = vb.bets > 0
    ? `${vb.hits}/${vb.bets} hits (${vb.profit_units >= 0 ? '+' : ''}${vb.profit_units}u)`
    : 'No value bets at this threshold';

  // 4. Log-Loss
  document.getElementById('simLogLoss').innerText = cur.log_loss.toFixed(4);
  _fmtDeltaBadge(document.getElementById('simLogLossDelta'), cur.log_loss - base.log_loss, '', false, 4);

  // Confidence Tier Comparison Table
  const tbody = document.getElementById('simTiersBody');
  if (tbody) {
    const curTiers = cur.tiers || [];
    const baseTiers = base.tiers || [];
    tbody.innerHTML = curTiers.map((t, idx) => {
      const bt = baseTiers[idx] || {};
      const cWr = t.win_rate !== null && t.win_rate !== undefined ? (t.win_rate * 100).toFixed(1) + '%' : '-';
      const bWr = bt.win_rate !== null && bt.win_rate !== undefined ? (bt.win_rate * 100).toFixed(1) + '%' : '-';
      let diffHtml = '<span style="color:var(--text-dim);">—</span>';
      if (t.win_rate !== null && bt.win_rate !== null && t.win_rate !== undefined && bt.win_rate !== undefined) {
        const diff = (t.win_rate - bt.win_rate) * 100;
        if (Math.abs(diff) >= 0.05) {
          const cls = diff > 0 ? 'var(--primary)' : 'var(--accent-rose)';
          const sgn = diff > 0 ? '+' : '';
          diffHtml = `<strong style="color:${cls};">${sgn}${diff.toFixed(2)}% ${diff > 0 ? '▲' : '▼'}</strong>`;
        } else {
          diffHtml = `<span style="color:var(--text-dim);">0.00%</span>`;
        }
      }
      return `
        <tr>
          <td style="font-weight:700;">${t.label}</td>
          <td style="color:var(--text-muted);">${t.hits} / ${t.matches}</td>
          <td style="font-weight:800; color:#fff;">${cWr}</td>
          <td style="color:var(--text-muted);">${bWr}</td>
          <td>${diffHtml}</td>
        </tr>
      `;
    }).join('');
  }

  // Component Standalone Leaderboard
  const lbEl = document.getElementById('simCompLeaderboard');
  if (lbEl && data.components) {
    const maxAcc = Math.max(...data.components.map(c => c.accuracy), 0.5);
    lbEl.innerHTML = data.components.map((c, idx) => {
      const pct = (c.accuracy * 100).toFixed(1);
      const fillW = Math.min(100, Math.max(8, (c.accuracy / maxAcc) * 100));
      const medal = idx === 0 ? '🥇 ' : (idx === 1 ? '🥈 ' : (idx === 2 ? '🥉 ' : ''));
      return `
        <div class="sim-lb-row">
          <span class="sim-lb-name" title="${c.name}">${medal}${formatVarName(c.name)}</span>
          <div class="sim-lb-bar-bg">
            <div class="sim-lb-bar-fill" style="width:${fillW}%;"></div>
          </div>
          <span class="sim-lb-stats">${pct}% <span style="color:var(--text-dim); font-weight:500; font-size:0.72rem;">(${c.log_loss.toFixed(3)})</span></span>
        </div>
      `;
    }).join('');
  }
}

function setupSettingsHandlers() {
  // Sport switcher inside Variables & Logic tab
  document.querySelectorAll('#simSportBar .conf-pill').forEach(btn => {
    btn.addEventListener('click', (e) => {
      document.querySelectorAll('#simSportBar .conf-pill').forEach(b => b.classList.remove('active'));
      e.currentTarget.classList.add('active');
      currentSimSport = e.currentTarget.dataset.simsport;
      renderModelSliders();
    });
  });

  const rngMs = document.getElementById('rngSimMarketShare');
  if (rngMs) {
    rngMs.addEventListener('input', (e) => {
      const ms = parseFloat(e.target.value);
      const msPct = Math.round(ms * 100);
      document.getElementById('lblSimMarketShare').innerText = msPct + '%';
      const splitBadge = document.getElementById('simBlendSplitBadge');
      if (splitBadge) splitBadge.innerText = `${msPct}% Market / ${100 - msPct}% Stats`;

      const { stats_weights } = _readCurrentDraftFromDom();
      simDraft[currentSimSport] = { market_share: ms, stats_weights, dirty: true };
      const btnApply = document.getElementById('btnApplyWeights');
      if (btnApply) btnApply.style.display = 'inline-flex';
      scheduleLiveSimulation();
    });
  }

  const rngVe = document.getElementById('rngValueEdge');
  if (rngVe) {
    rngVe.addEventListener('input', (e) => {
      document.getElementById('lblValueEdge').innerText = '+' + (parseFloat(e.target.value) * 100).toFixed(1) + '%';
      scheduleLiveSimulation();
    });
    rngVe.addEventListener('change', async (e) => {
      await updateSettingParam({ value_edge_min: parseFloat(e.target.value) });
    });
  }

  const btnApply = document.getElementById('btnApplyWeights');
  if (btnApply) {
    btnApply.addEventListener('click', async () => {
      const sport = currentSimSport;
      const { market_share, stats_weights, value_edge_min } = _readCurrentDraftFromDom();
      const payload = { value_edge_min };
      if (sport === 'football') {
        payload.football_market_share = market_share;
        payload.football_stats_weights = stats_weights;
      } else if (sport === 'nba') {
        payload.nba_market_share = market_share;
        payload.nba_stats_weights = stats_weights;
      } else {
        payload.tennis_market_share = market_share;
        payload.tennis_stats_weights = stats_weights;
      }
      await updateSettingParam(payload);
      simDraft[sport] = { market_share: null, stats_weights: null, dirty: false };
      await loadStatus();
    });
  }

  const btnOpt = document.getElementById('btnOptimizeWeights');
  if (btnOpt) {
    btnOpt.addEventListener('click', async () => {
      const origText = btnOpt.innerText;
      btnOpt.innerText = '⏳ Searching...';
      btnOpt.disabled = true;
      try {
        const ve = parseFloat(document.getElementById('rngValueEdge')?.value ?? 0.04);
        const res = await fetch('/api/simulate/optimize', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sport: currentSimSport, value_edge_min: ve })
        });
        const data = await res.json();
        if (data.optimized_params) {
          simDraft[currentSimSport] = {
            market_share: data.optimized_params.market_share,
            stats_weights: data.optimized_params.stats_weights,
            dirty: true,
          };
          renderModelSliders();
        }
      } catch (err) {
        console.error('Optimize error:', err);
      } finally {
        btnOpt.innerText = origText;
        btnOpt.disabled = false;
      }
    });
  }

  document.getElementById('btnResetWeights').addEventListener('click', async () => {
    await updateSettingParam({
      football_market_share: 0.65,
      nba_market_share: 0.30,
      tennis_market_share: 0.65,
      football_stats_weights: null,
      nba_stats_weights: null,
      tennis_stats_weights: null,
      value_edge_min: 0.04
    });
    simDraft = {
      football: { market_share: null, stats_weights: null, dirty: false },
      nba: { market_share: null, stats_weights: null, dirty: false },
      tennis: { market_share: null, stats_weights: null, dirty: false },
    };
    await loadStatus();
  });
}

function attachSliderEvents() {
  document.querySelectorAll('.sim-var-slider').forEach(sl => {
    sl.addEventListener('input', () => {
      _refreshNormalizedLabels();
      const { market_share, stats_weights } = _readCurrentDraftFromDom();
      simDraft[currentSimSport] = { market_share, stats_weights, dirty: true };
      const btnApply = document.getElementById('btnApplyWeights');
      if (btnApply) btnApply.style.display = 'inline-flex';
      scheduleLiveSimulation();
    });
  });
}

async function updateSettingParam(payload) {
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const out = await res.json();
    clearDataCache();
    refreshAllTabsInBackground();
    return out;
  } catch (err) {
    console.error('Error updating settings:', err);
  }
}

// ------------------------------------------------------------- Football Fixtures
async function loadFootballFixtures(forceRefresh = false) {
  const container = document.getElementById('fixturesList');
  const cacheKey = `fb_${currentLeague}`;
  if (forceRefresh || !appCache[cacheKey]) {
    container.innerHTML = `
      <div class="state-box">
        <div class="spinner"></div>
        <p>Loading European fixtures & statistical calculations...</p>
      </div>
    `;
  }

  try {
    const url = `/api/football/fixtures?league=${currentLeague}&with_finished=true${forceRefresh ? '&refresh=true' : ''}`;
    const data = await fetchCached(cacheKey, url, forceRefresh);
    let fixtures = data.fixtures || [];
    if (!showFinished) {
      fixtures = fixtures.filter(f => f.state !== 'post');
    }

    if (fixtures.length === 0) {
      container.innerHTML = `<div class="state-box"><p>No fixtures found for ${LEAGUE_NAMES[currentLeague]}.</p></div>`;
      return;
    }

    if (selectedConfidence !== 'ALL') {
      fixtures = fixtures.filter(f => f.recommendation?.confidence === selectedConfidence);
    }

    if (fixtures.length === 0) {
      container.innerHTML = `
        <div class="state-box">
          <p>No fixtures matching <strong>${selectedConfidence} Confidence</strong> in ${LEAGUE_NAMES[currentLeague]}.</p>
          <p style="font-size:0.85rem; color:var(--text-muted); margin-top:0.4rem;">Select "All Conf" to view all scheduled games.</p>
        </div>
      `;
      return;
    }

    // Regroup fixtures by Matchweek / Tournament Round
    const groups = {};
    fixtures.forEach(f => {
      const mw = f.matchweek || 'Fixtures';
      if (!groups[mw]) {
        groups[mw] = { title: mw, dateSpan: f.date_span || '', fixtures: [] };
      }
      groups[mw].fixtures.push(f);
    });

    container.innerHTML = Object.values(groups).map((grp, idx) => {
      const isPast = grp.fixtures.every(f => f.state === 'post');
      let hits = 0;
      let finCount = 0;
      grp.fixtures.forEach(f => {
        if (f.state === 'post' && f.home_score != null && f.away_score != null && f.recommendation?.pick_index != null) {
          finCount++;
          const hs = Number(f.home_score);
          const as = Number(f.away_score);
          const actualIdx = hs > as ? 0 : (hs === as ? 1 : 2);
          if (f.recommendation.pick_index === actualIdx) hits++;
        }
      });
      const accBadge = finCount > 0
        ? `<span class="badge ${Math.round((hits / finCount) * 100) >= 50 ? 'badge-green' : 'badge-rose'}" title="${hits} of ${finCount} 1X2 match predictions correct">🎯 ${Math.round((hits / finCount) * 100)}% (${hits}/${finCount})</span>`
        : '';

      return `
        <div class="matchweek-section ${isPast ? 'collapsed' : ''}" id="sec_${idx}">
          <div class="matchweek-header" data-target="sec_${idx}">
            <div class="mw-left">
              <span class="mw-title">📅 ${grp.title}</span>
              ${grp.dateSpan ? `<span class="mw-dates">${grp.dateSpan}</span>` : ''}
              ${isPast ? '<span style="font-size:0.75rem; color:var(--text-muted);">(Past Results)</span>' : ''}
              ${accBadge}
            </div>
            <div class="mw-right">
              <span class="mw-badge">${grp.fixtures.length} match${grp.fixtures.length > 1 ? 'es' : ''}</span>
              <span class="mw-chevron">▼</span>
            </div>
          </div>
          <div class="fixtures-grid">
            ${grp.fixtures.map(renderFootballCard).join('')}
          </div>
        </div>
      `;
    }).join('');

    // Toggle collapse on click
    document.querySelectorAll('.matchweek-header').forEach(hdr => {
      hdr.addEventListener('click', (e) => {
        const secId = e.currentTarget.dataset.target;
        const sec = document.getElementById(secId);
        if (sec) {
          sec.classList.toggle('collapsed');
        }
      });
    });

    attachCardEvents();
  } catch (err) {
    container.innerHTML = `<div class="state-box"><p style="color:var(--accent-rose)">Failed to load fixtures: ${err.message}</p></div>`;
  }
}

function renderFootballCard(f) {
  const reco = f.recommendation || {};
  const probs = f.probs || [0.33, 0.33, 0.33];
  const pH = Math.round(probs[0] * 100);
  const pD = Math.round(probs[1] * 100);
  const pA = Math.round(probs[2] * 100);

  const statusClass = f.state === 'in' ? 'status-live' : (f.state === 'post' ? 'status-post' : 'status-pre');
  const kickoffFormatted = new Date(f.kickoff).toLocaleString('en-US', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
  });

  const valueBetsHtml = (reco.value_bets && reco.value_bets.length > 0) ? `
    <div class="value-banner">
      <div>
        <span class="value-highlight">⭐ VALUE:</span> ${reco.value_bets[0].outcome} @ <strong>${reco.value_bets[0].odds}</strong>
      </div>
      <span class="edge-pill">+${(reco.value_bets[0].edge * 100).toFixed(1)}%</span>
    </div>
  ` : '';

  const absences = f.absences?.applied || {};
  const hasAbsences = (absences.home_out > 0 || absences.away_out > 0);
  const isScored = f.state === 'post' || f.state === 'in';
  const hScore = isScored ? (f.home_score ?? 0) : '';
  const aScore = isScored ? (f.away_score ?? 0) : '';
  const homeWon = isScored && Number(f.home_score) > Number(f.away_score);
  const awayWon = isScored && Number(f.away_score) > Number(f.home_score);

  const hPlayers = f.absences?.home_players || [];
  const aPlayers = f.absences?.away_players || [];
  const hRot = f.absences?.home_rotated || [];
  const aRot = f.absences?.away_rotated || [];
  const hStars = hPlayers.filter(p => p.is_star).length;
  const aStars = aPlayers.filter(p => p.is_star).length;
  const hInjBadge = hPlayers.length > 0
    ? ` <span style="color:var(--accent-rose); font-size:0.74rem; cursor:help;" title="Injuries/Suspensions: ${hPlayers.map(p => `${p.is_star ? '⭐ ' : ''}${p.player} (${p.status}${p.reason ? ': ' + p.reason : ''})`).join(', ')}">🩹${hPlayers.length}${hStars > 0 ? ` <span style='color:var(--accent-amber); font-weight:700;'>⭐${hStars}</span>` : ''}</span>`
    : '';
  const aInjBadge = aPlayers.length > 0
    ? ` <span style="color:var(--accent-rose); font-size:0.74rem; cursor:help;" title="Injuries/Suspensions: ${aPlayers.map(p => `${p.is_star ? '⭐ ' : ''}${p.player} (${p.status}${p.reason ? ': ' + p.reason : ''})`).join(', ')}">🩹${aPlayers.length}${aStars > 0 ? ` <span style='color:var(--accent-amber); font-weight:700;'>⭐${aStars}</span>` : ''}</span>`
    : '';
  const hRotBadge = hRot.length >= 2
    ? ` <span style="color:var(--accent-purple); font-size:0.74rem; font-weight:700; cursor:help;" title="Confirmed Starting XI Rotation (usual starters benched/rested): ${hRot.join(', ')}">🔄${hRot.length}</span>`
    : '';
  const aRotBadge = aRot.length >= 2
    ? ` <span style="color:var(--accent-purple); font-size:0.74rem; font-weight:700; cursor:help;" title="Confirmed Starting XI Rotation (usual starters benched/rested): ${aRot.join(', ')}">🔄${aRot.length}</span>`
    : '';

  let pickStatusClass = '';
  let ouStatusClass = '';
  let bttsStatusClass = '';

  const overProb = f.over25 ?? 0.5;
  const pickOver25 = overProb >= 0.5;
  const ouPickText = pickOver25 ? `O2.5 (${Math.round(overProb * 100)}%)` : `U2.5 (${Math.round((1 - overProb) * 100)}%)`;

  const bttsProb = f.btts ?? 0.5;
  const pickBtts = bttsProb >= 0.5;
  const bttsPickText = pickBtts ? `Yes (${Math.round(bttsProb * 100)}%)` : `No (${Math.round((1 - bttsProb) * 100)}%)`;

  if (f.state === 'post' && f.home_score != null && f.away_score != null) {
    const hs = Number(f.home_score);
    const as = Number(f.away_score);
    if (reco.pick_index != null) {
      const actualIdx = hs > as ? 0 : (hs === as ? 1 : 2);
      pickStatusClass = reco.pick_index === actualIdx ? 'pick-hit' : 'pick-miss';
    }
    const actualOver25 = (hs + as) > 2.5;
    ouStatusClass = (pickOver25 === actualOver25) ? 'pick-hit' : 'pick-miss';

    const actualBtts = hs > 0 && as > 0;
    bttsStatusClass = (pickBtts === actualBtts) ? 'pick-hit' : 'pick-miss';
  }

  return `
    <div class="match-card" data-id="${f.espn_id}">
      <div class="match-strip">
        <!-- Col 1: Status & Kickoff -->
        <div class="strip-meta">
          <div class="match-status">
            <span class="status-dot ${statusClass}"></span>
            <span>${f.status_detail || f.status}</span>
          </div>
          <div class="meta-sub">${kickoffFormatted}</div>
        </div>

        <!-- Col 2: Stacked Double-Row Teams & Scores -->
        <div class="strip-teams">
          <div class="team-line">
            <div class="team-line-left">
              ${f.home_rank ? `<span class="team-rank-pill" title="${f.home_standing_info || `${f.league} #${f.home_rank}`}">#${f.home_rank}</span>` : ''}
              ${f.home_logo ? `<img class="team-logo-sm" src="${f.home_logo}" alt="${f.home}">` : ''}
              <span class="team-name ${homeWon ? 'winner' : ''}">${f.home}</span>
              <span class="team-sub-inline">${f.home_record || ''} ${f.home_form ? `(${f.home_form})` : ''}${hInjBadge}${hRotBadge}</span>
            </div>
            <span class="team-score-cell">${hScore}</span>
          </div>

          <div class="team-line">
            <div class="team-line-left">
              ${f.away_rank ? `<span class="team-rank-pill" title="${f.away_standing_info || `${f.league} #${f.away_rank}`}">#${f.away_rank}</span>` : ''}
              ${f.away_logo ? `<img class="team-logo-sm" src="${f.away_logo}" alt="${f.away}">` : ''}
              <span class="team-name ${awayWon ? 'winner' : ''}">${f.away}</span>
              <span class="team-sub-inline">${f.away_record || ''} ${f.away_form ? `(${f.away_form})` : ''}${aInjBadge}${aRotBadge}</span>
            </div>
            <span class="team-score-cell">${aScore}</span>
          </div>
        </div>

        <!-- Col 3: Pick, 1X2 Bar & Goal Metrics -->
        <div class="strip-prediction">
          <div class="pred-head">
            <span class="pick-title ${pickStatusClass}">🎯 ${reco.pick || 'N/A'}</span>
            <span class="badge ${reco.confidence === 'High' ? 'badge-green' : (reco.confidence === 'Medium' ? 'badge-blue' : 'badge-amber')}">
              ${reco.confidence || 'Low'} (${Math.round((reco.probability || 0) * 100)}%)
            </span>
          </div>

          <div class="prob-bar" title="1: ${pH}% | X: ${pD}% | 2: ${pA}%">
            <div class="prob-seg seg-home" style="width: ${pH}%">${pH}%</div>
            <div class="prob-seg seg-draw" style="width: ${pD}%">${pD}%</div>
            <div class="prob-seg seg-away" style="width: ${pA}%">${pA}%</div>
          </div>

          <div class="strip-submetrics">
            <span title="Over/Under 2.5 Total Goals prediction">O/U: <strong class="sub-pick ${ouStatusClass}">${ouPickText}</strong></span>
            <span title="Both Teams To Score prediction">BTTS: <strong class="sub-pick ${bttsStatusClass}">${bttsPickText}</strong></span>
          </div>
        </div>

        <!-- Col 4: Bookmaker & Fair Odds + Value Bet -->
        <div class="strip-odds">
          <div class="odds-line">
            <span class="odds-label" title="${f.odds_books && (f.odds_books['Stake.com'] || f.odds_books['Winamax.fr']) ? 'Stake.com (International Low-Vig Odds)' : 'Bookmaker Odds'}">${f.odds_books && (f.odds_books['Stake.com'] || f.odds_books['Winamax.fr']) ? '🎰 Stake' : 'Book'}</span>
            <span class="odds-vals" style="color:var(--accent-blue)">${f.best_odds ? `${f.best_odds[0]} / ${f.best_odds[1]} / ${f.best_odds[2]}` : 'Awaiting'}</span>
          </div>
          <div class="odds-line">
            <span class="odds-label">Fair</span>
            <span class="odds-vals" style="color:var(--primary)">${reco.fair_odds ? `${reco.fair_odds[0]} / ${reco.fair_odds[1]} / ${reco.fair_odds[2]}` : '-'}</span>
          </div>
          ${valueBetsHtml}
        </div>

        <!-- Col 5: Details Toggle -->
        <div class="strip-actions">
          <button class="btn-toggle-vars" data-target="vars_${f.espn_id}">
            📊 Details ▼
          </button>
        </div>
      </div>

      <div class="variables-panel" id="vars_${f.espn_id}">
        ${renderFootballVariables(f)}
        <div style="margin-top:0.6rem; text-align:right;">
          <button class="pill-btn btn-absence" data-id="${f.espn_id}" data-home="${f.home}" data-away="${f.away}">
            🩹 ${hasAbsences ? `Absences (${absences.home_out}H / ${absences.away_out}A)` : 'Set Player Absences'}
          </button>
        </div>
      </div>
    </div>
  `;
}

function renderFootballVariables(f) {
  const feat = f.features || {};
  const comps = f.components || {};
  const abs = f.absences?.applied || {};
  const hPlayers = f.absences?.home_players || [];
  const aPlayers = f.absences?.away_players || [];
  const hXgPen = Math.min(15, (abs.home_out || 0) * 3.5).toFixed(1);
  const aXgPen = Math.min(15, (abs.away_out || 0) * 3.5).toFixed(1);
  const hAbsText = hPlayers.length > 0
    ? `-${hXgPen}% xG (${hPlayers.map(p => `${p.is_star ? '⭐ ' : ''}${p.player} [${p.status}]`).join(', ')})`
    : ((abs.home_out || 0) > 0 ? `-${hXgPen}% xG (manual)` : 'Full Strength (0%)');
  const aAbsText = aPlayers.length > 0
    ? `-${aXgPen}% xG (${aPlayers.map(p => `${p.is_star ? '⭐ ' : ''}${p.player} [${p.status}]`).join(', ')})`
    : ((abs.away_out || 0) > 0 ? `-${aXgPen}% xG (manual)` : 'Full Strength (0%)');

  return `
    <div style="font-weight:700; color:var(--accent-blue); margin-bottom:0.4rem;">Model Sub-Components:</div>
    ${Object.entries(comps).map(([k, c]) => `
      <div class="var-row">
        <span class="var-name">${formatVarName(k)} (weight: ${Math.round(c.weight * 100)}%)</span>
        <span class="var-val">${Math.round(c.p[0] * 100)}% / ${Math.round(c.p[1] * 100)}% / ${Math.round(c.p[2] * 100)}%</span>
      </div>
    `).join('')}

    <div style="font-weight:700; color:var(--accent-blue); margin-top:0.6rem; margin-bottom:0.4rem;">Advanced Team Stats:</div>
    <div class="var-row">
      <span class="var-name">Elo Ratings (H / A)</span>
      <span class="var-val">${feat.elo ? feat.elo.join(' vs ') : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">Expected Goals (xG) / game</span>
      <span class="var-val">${feat.xg_for_pg ? feat.xg_for_pg.join(' vs ') : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">xG Conceded / game</span>
      <span class="var-val">${feat.xg_against_pg ? feat.xg_against_pg.join(' vs ') : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">Recent Form (last 5)</span>
      <span class="var-val">${feat.form ? feat.form.join(' vs ') : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">Rest Days</span>
      <span class="var-val">${feat.rest_days ? `${feat.rest_days[0] ?? '?'}d vs ${feat.rest_days[1] ?? '?'}d` : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">Promoted Status</span>
      <span class="var-val">${feat.promoted ? (feat.promoted[0] ? 'Home promoted' : '') + (feat.promoted[1] ? 'Away promoted' : '') || 'None' : 'None'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">Home Absences / Injuries</span>
      <span class="var-val" style="color:${hPlayers.length || abs.home_out ? 'var(--accent-rose)' : 'var(--text-main)'}; text-align:right; max-width:65%;">${hAbsText}</span>
    </div>
    <div class="var-row">
      <span class="var-name">Away Absences / Injuries</span>
      <span class="var-val" style="color:${aPlayers.length || abs.away_out ? 'var(--accent-rose)' : 'var(--text-main)'}; text-align:right; max-width:65%;">${aAbsText}</span>
    </div>

    ${f.best_odds ? `
      <div style="font-weight:700; color:var(--accent-blue); margin-top:0.6rem; margin-bottom:0.4rem;">Market & Bookmaker Odds:</div>
      <div class="var-row">
        <span class="var-name">Best Market Odds (1 / X / 2)</span>
        <span class="var-val">${f.best_odds.join(' / ')}</span>
      </div>
      <div class="var-row">
        <span class="var-name">Model Fair Odds (1 / X / 2)</span>
        <span class="var-val">${(f.recommendation?.fair_odds || []).join(' / ')}</span>
      </div>
    ` : ''}
  `;
}

function attachCardEvents() {
  document.querySelectorAll('.btn-toggle-vars').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const panel = document.getElementById(e.currentTarget.dataset.target);
      panel.classList.toggle('open');
      e.currentTarget.innerText = panel.classList.contains('open') ? '▲ Hide' : '📊 Details ▼';
    });
  });

  document.querySelectorAll('.btn-absence').forEach(btn => {
    btn.addEventListener('click', (e) => {
      openAbsenceModal(e.currentTarget.dataset.id, e.currentTarget.dataset.home, e.currentTarget.dataset.away);
    });
  });
}

// ------------------------------------------------------------- Absence Modal
function openAbsenceModal(espnId, home, away) {
  currentModalMatch = espnId;
  document.getElementById('modalMatchTitle').innerText = `${home} vs ${away}`;
  document.getElementById('lblModalHome').innerText = `${home} Missing Key Players`;
  document.getElementById('lblModalAway').innerText = `${away} Missing Key Players`;

  const curr = appStatus?.settings?.manual_absences?.[espnId] || { home: 0, away: 0 };
  document.getElementById('numModalHome').value = curr.home || 0;
  document.getElementById('numModalAway').value = curr.away || 0;

  const modal = document.getElementById('absenceModal');
  modal.style.display = 'flex';
}

document.getElementById('btnCancelModal').addEventListener('click', () => {
  document.getElementById('absenceModal').style.display = 'none';
});

document.getElementById('btnSaveModal').addEventListener('click', async () => {
  const homeAbs = parseFloat(document.getElementById('numModalHome').value) || 0;
  const awayAbs = parseFloat(document.getElementById('numModalAway').value) || 0;
  
  await fetch('/api/settings/absences', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      espn_id: currentModalMatch,
      home_absences: homeAbs,
      away_absences: awayAbs
    })
  });

  document.getElementById('absenceModal').style.display = 'none';
  clearDataCache();
  await loadStatus();
  if (currentTab === 'nba') {
    await loadNbaGames(true);
  } else {
    await loadFootballFixtures(true);
  }
  refreshAllTabsInBackground();
});

// ------------------------------------------------------------- NBA Games
async function loadNbaGames(forceRefresh = false) {
  const container = document.getElementById('nbaList');
  if (!container) return;
  if (forceRefresh || !appCache['nba_games']) {
    container.innerHTML = `
      <div class="state-box">
        <div class="spinner"></div>
        <p>Loading NBA scoreboard, Net Ratings, B2B rest & live odds...</p>
      </div>
    `;
  }

  try {
    const url = `/api/nba/games?with_finished=true${forceRefresh ? '&refresh=true' : ''}`;
    const data = await fetchCached('nba_games', url, forceRefresh);
    let games = data.games || [];
    if (!showFinishedNba) {
      games = games.filter(g => g.state !== 'post');
    }

    if (games.length === 0) {
      container.innerHTML = `<div class="state-box"><p>No NBA games found in the current window.</p></div>`;
      return;
    }

    if (selectedNbaConfidence !== 'ALL') {
      games = games.filter(g => g.recommendation?.confidence === selectedNbaConfidence);
    }

    if (games.length === 0) {
      container.innerHTML = `
        <div class="state-box">
          <p>No NBA games matching <strong>${selectedNbaConfidence} Confidence</strong>.</p>
          <p style="font-size:0.85rem; color:var(--text-muted); margin-top:0.4rem;">Select "All Conf" to view all scheduled games.</p>
        </div>
      `;
      return;
    }

    // Group games by gameday
    const groups = {};
    games.forEach(g => {
      const gd = g.gameday || 'NBA Games';
      if (!groups[gd]) {
        groups[gd] = { title: gd, dateSpan: g.date_span || '', games: [] };
      }
      groups[gd].games.push(g);
    });

    container.innerHTML = Object.values(groups).map((grp, idx) => {
      const isPast = grp.games.every(g => g.state === 'post');
      let hits = 0;
      let finCount = 0;
      grp.games.forEach(g => {
        if (g.state === 'post' && g.home_score != null && g.away_score != null && g.recommendation?.pick_index != null) {
          finCount++;
          const hs = Number(g.home_score);
          const as = Number(g.away_score);
          const actualIdx = hs > as ? 0 : 1;
          if (g.recommendation.pick_index === actualIdx) hits++;
        }
      });
      const accBadge = finCount > 0
        ? `<span class="badge ${Math.round((hits / finCount) * 100) >= 50 ? 'badge-green' : 'badge-rose'}" title="${hits} of ${finCount} winner predictions correct">🎯 ${Math.round((hits / finCount) * 100)}% (${hits}/${finCount})</span>`
        : '';

      return `
        <div class="matchweek-section ${isPast ? 'collapsed' : ''}" id="sec_nba_${idx}">
          <div class="matchweek-header" data-target="sec_nba_${idx}">
            <div class="mw-left">
              <span class="mw-title">🏀 ${grp.title}</span>
              ${grp.dateSpan ? `<span class="mw-dates">${grp.dateSpan}</span>` : ''}
              ${isPast ? '<span style="font-size:0.75rem; color:var(--text-muted);">(Final Results)</span>' : ''}
              ${accBadge}
            </div>
            <div class="mw-right">
              <span class="mw-badge">${grp.games.length} game${grp.games.length > 1 ? 's' : ''}</span>
              <span class="mw-chevron">▼</span>
            </div>
          </div>
          <div class="fixtures-grid">
            ${grp.games.map(renderNbaCard).join('')}
          </div>
        </div>
      `;
    }).join('');

    container.querySelectorAll('.matchweek-header').forEach(hdr => {
      hdr.addEventListener('click', (e) => {
        const secId = e.currentTarget.dataset.target;
        const sec = document.getElementById(secId);
        if (sec) sec.classList.toggle('collapsed');
      });
    });

    attachCardEvents();
  } catch (err) {
    container.innerHTML = `<div class="state-box"><p style="color:var(--accent-rose)">Failed to load NBA games: ${err.message}</p></div>`;
  }
}

function renderNbaCard(g) {
  const reco = g.recommendation || {};
  const probs = g.probs || [0.5, 0.5];
  const pH = Math.round(probs[0] * 100);
  const pA = Math.round(probs[1] * 100);

  const statusClass = g.state === 'in' ? 'status-live' : (g.state === 'post' ? 'status-post' : 'status-pre');
  const kickoffFormatted = new Date(g.kickoff).toLocaleString('en-GB', {
    timeZone: 'Europe/Paris',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false
  }) + ' (Paris)';
  const statusLabel = g.state === 'pre' ? 'Scheduled' : (g.status_detail || g.status);

  const valueBetsHtml = (reco.value_bets && reco.value_bets.length > 0) ? `
    <div class="value-banner">
      <div>
        <span class="value-highlight">⭐ VALUE:</span> ${reco.value_bets[0].outcome} @ <strong>${reco.value_bets[0].odds}</strong>
      </div>
      <span class="edge-pill">+${(reco.value_bets[0].edge * 100).toFixed(1)}%</span>
    </div>
  ` : '';

  const absences = g.absences?.applied || {};
  const hasAbsences = (absences.home_out > 0 || absences.away_out > 0);
  const isScored = g.state === 'post' || g.state === 'in';
  const hScore = isScored ? (g.home_score ?? 0) : '';
  const aScore = isScored ? (g.away_score ?? 0) : '';
  const homeWon = isScored && Number(g.home_score) > Number(g.away_score);
  const awayWon = isScored && Number(g.away_score) > Number(g.home_score);

  let pickStatusClass = '';
  let ouStatusClass = '';
  let spreadStatusClass = '';

  const ouLine = g.ou_line || 226.5;
  const overProb = g.over_prob ?? 0.5;
  const pickOver = overProb >= 0.5;
  const ouPickText = pickOver
    ? `O ${ouLine} (${Math.round(overProb * 100)}%)`
    : `U ${ouLine} (${Math.round((1 - overProb) * 100)}%)`;

  const projMargin = g.proj_margin ?? 0;
  const hAbbr = g.home_abbr || g.home.split(' ').pop();
  const aAbbr = g.away_abbr || g.away.split(' ').pop();
  let spreadPickText = '';
  const hasBookSpread = g.spread_line != null;
  const hSpread = hasBookSpread ? Number(g.spread_line) : -(Math.round(projMargin * 2) / 2);
  const pickHomeCover = hasBookSpread ? (projMargin + hSpread) >= 0 : projMargin >= 0;

  if (pickHomeCover) {
    const fmtH = hSpread > 0 ? `+${hSpread}` : `${hSpread}`;
    spreadPickText = `${hAbbr} ${fmtH}`;
  } else {
    const aSpread = -hSpread;
    const fmtA = aSpread > 0 ? `+${aSpread}` : `${aSpread}`;
    spreadPickText = `${aAbbr} ${fmtA}`;
  }

  if (g.state === 'post' && g.home_score != null && g.away_score != null) {
    const hs = Number(g.home_score);
    const as = Number(g.away_score);
    if (reco.pick_index != null) {
      const actualIdx = hs > as ? 0 : 1;
      pickStatusClass = reco.pick_index === actualIdx ? 'pick-hit' : 'pick-miss';
    }
    const actualTotal = hs + as;
    if (actualTotal !== ouLine) {
      const actualOver = actualTotal > ouLine;
      ouStatusClass = (pickOver === actualOver) ? 'pick-hit' : 'pick-miss';
    }
    const actualMargin = hs - as;
    if ((actualMargin + hSpread) !== 0) {
      const actualHomeCover = (actualMargin + hSpread) > 0;
      spreadStatusClass = (pickHomeCover === actualHomeCover) ? 'pick-hit' : 'pick-miss';
    }
  }

  const b2b = g.features?.b2b || [false, false];
  const hB2B = b2b[0] ? ' <span style="color:var(--accent-amber); font-weight:700;" title="Back-to-Back (0 rest days)">[B2B]</span>' : '';
  const aB2B = b2b[1] ? ' <span style="color:var(--accent-amber); font-weight:700;" title="Back-to-Back (0 rest days)">[B2B]</span>' : '';

  const hPlayers = g.absences?.home_players || [];
  const aPlayers = g.absences?.away_players || [];
  const hInjBadge = hPlayers.length > 0
    ? ` <span style="color:var(--accent-rose); font-size:0.74rem;" title="Auto-detected absences: ${hPlayers.map(p => `${p.player} (${p.status})`).join(', ')}">🩹${hPlayers.length}</span>`
    : '';
  const aInjBadge = aPlayers.length > 0
    ? ` <span style="color:var(--accent-rose); font-size:0.74rem;" title="Auto-detected absences: ${aPlayers.map(p => `${p.player} (${p.status})`).join(', ')}">🩹${aPlayers.length}</span>`
    : '';

  return `
    <div class="match-card" data-id="${g.espn_id}">
      <div class="match-strip">
        <!-- Col 1: Status & Tipoff -->
        <div class="strip-meta">
          <div class="match-status">
            <span class="status-dot ${statusClass}"></span>
            <span>${statusLabel}</span>
          </div>
          <div class="meta-sub">${kickoffFormatted}</div>
        </div>

        <!-- Col 2: Stacked Double-Row Teams & Scores -->
        <div class="strip-teams">
          <div class="team-line">
            <div class="team-line-left">
              ${g.home_rank ? `<span class="team-rank-pill" title="${g.home_standing_info || `NBA #${g.home_rank}`}">#${g.home_rank}</span>` : ''}
              ${g.home_logo ? `<img class="team-logo-sm" src="${g.home_logo}" alt="${g.home}">` : ''}
              <span class="team-name ${homeWon ? 'winner' : ''}">${g.home}</span>
              <span class="team-sub-inline">${g.home_record || ''}${hB2B}${hInjBadge}</span>
            </div>
            <span class="team-score-cell">${hScore}</span>
          </div>

          <div class="team-line">
            <div class="team-line-left">
              ${g.away_rank ? `<span class="team-rank-pill" title="${g.away_standing_info || `NBA #${g.away_rank}`}">#${g.away_rank}</span>` : ''}
              ${g.away_logo ? `<img class="team-logo-sm" src="${g.away_logo}" alt="${g.away}">` : ''}
              <span class="team-name ${awayWon ? 'winner' : ''}">${g.away}</span>
              <span class="team-sub-inline">${g.away_record || ''}${aB2B}${aInjBadge}</span>
            </div>
            <span class="team-score-cell">${aScore}</span>
          </div>
        </div>

        <!-- Col 3: Pick, 2-Way Bar & Projected Points / O-U -->
        <div class="strip-prediction">
          <div class="pred-head">
            <span class="pick-title ${pickStatusClass}">🎯 ${reco.pick || 'N/A'}</span>
            <span class="badge ${reco.confidence === 'High' ? 'badge-green' : (reco.confidence === 'Medium' ? 'badge-blue' : 'badge-amber')}">
              ${reco.confidence || 'Low'} (${Math.round((reco.probability || 0) * 100)}%)
            </span>
          </div>

          <div class="prob-bar" title="Home: ${pH}% | Away: ${pA}%">
            <div class="prob-seg seg-home" style="width: ${pH}%">${pH}%</div>
            <div class="prob-seg seg-away" style="width: ${pA}%">${pA}%</div>
          </div>

          <div class="strip-submetrics">
            <span title="Model Projected Score">Proj: <strong style="color:var(--text-main)">${Math.round(g.proj_home || 112)}-${Math.round(g.proj_away || 110)}</strong></span>
            <span title="Over/Under Total Pick">O/U: <strong class="sub-pick ${ouStatusClass}">${ouPickText}</strong></span>
            <span title="Against The Spread (ATS) Pick (Proj Margin: ${(g.proj_margin || 0) > 0 ? '+' : ''}${(g.proj_margin || 0).toFixed(1)})">Spread: <strong class="sub-pick ${spreadStatusClass}">${spreadPickText}</strong></span>
          </div>
        </div>

        <!-- Col 4: Bookmaker & Fair Odds + Value Bet -->
        <div class="strip-odds">
          <div class="odds-line">
            <span class="odds-label" title="${g.odds_books && (g.odds_books['Stake.com'] || g.odds_books['Winamax.fr']) ? 'Stake.com (International Low-Vig Odds)' : 'Bookmaker Odds'}">${g.odds_books && (g.odds_books['Stake.com'] || g.odds_books['Winamax.fr']) ? '🎰 Stake' : 'Book'}</span>
            <span class="odds-vals" style="color:var(--accent-blue)">${g.best_odds ? `${g.best_odds[0]} / ${g.best_odds[1]}` : 'Awaiting'}</span>
          </div>
          <div class="odds-line">
            <span class="odds-label">Fair</span>
            <span class="odds-vals" style="color:var(--primary)">${reco.fair_odds ? `${reco.fair_odds[0]} / ${reco.fair_odds[1]}` : '-'}</span>
          </div>
          ${valueBetsHtml}
        </div>

        <!-- Col 5: Details Toggle -->
        <div class="strip-actions">
          <button class="btn-toggle-vars" data-target="vars_nba_${g.espn_id}">
            📊 Details ▼
          </button>
        </div>
      </div>

      <div class="variables-panel" id="vars_nba_${g.espn_id}">
        ${renderNbaVariables(g)}
      </div>
    </div>
  `;
}

function renderNbaVariables(g) {
  const feat = g.features || {};
  const comps = g.components || {};
  const netRtg = feat.net_rtg || feat.net_rating;
  const offPpg = feat.off_ppg || feat.ppg;
  const defPpg = feat.def_ppg || feat.opp_ppg;
  const last10 = feat.last10 || feat.l10;
  const splits = feat.home_road_splits;
  const abs = g.absences?.applied || {};
  const hPlayers = g.absences?.home_players || [];
  const aPlayers = g.absences?.away_players || [];
  const hPtsPen = ((abs.home_out || 0) * 2.8).toFixed(1);
  const aPtsPen = ((abs.away_out || 0) * 2.8).toFixed(1);
  const hAbsText = hPlayers.length > 0
    ? `-${hPtsPen} pts (${hPlayers.map(p => `${p.is_star ? '⭐ ' : ''}${p.player} [${p.status}]`).join(', ')})`
    : 'Full Strength (0 pts)';
  const aAbsText = aPlayers.length > 0
    ? `-${aPtsPen} pts (${aPlayers.map(p => `${p.is_star ? '⭐ ' : ''}${p.player} [${p.status}]`).join(', ')})`
    : 'Full Strength (0 pts)';

  return `
    <div style="font-weight:700; color:var(--accent-blue); margin-bottom:0.4rem;">NBA Model Sub-Components (Home / Away):</div>
    ${Object.entries(comps).map(([k, c]) => {
      const pHome = Array.isArray(c.p) ? Number(c.p[0]) : Number(c.p ?? 0.5);
      const pAway = Array.isArray(c.p) ? Number(c.p[1]) : (1 - pHome);
      return `
        <div class="var-row">
          <span class="var-name">${formatVarName(k)} (weight: ${Math.round((c.weight || 0) * 100)}%)</span>
          <span class="var-val">${Math.round(pHome * 100)}% / ${Math.round(pAway * 100)}%</span>
        </div>
      `;
    }).join('')}

    <div style="font-weight:700; color:var(--accent-blue); margin-top:0.6rem; margin-bottom:0.4rem;">Team Efficiency & Situational Metrics:</div>
    <div class="var-row">
      <span class="var-name">Power Elo Ratings (H / A)</span>
      <span class="var-val">${feat.elo ? feat.elo.join(' vs ') : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">True Net Rating (per 100 poss)</span>
      <span class="var-val">${netRtg ? netRtg.map(v => (v > 0 ? '+' : '') + v).join(' vs ') : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">Pace — Possessions / 48m (H / A)</span>
      <span class="var-val">${feat.pace ? feat.pace.join(' vs ') : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">Offensive Rating ORtg (H / A)</span>
      <span class="var-val">${feat.ortg ? feat.ortg.join(' vs ') : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">Defensive Rating DRtg (H / A)</span>
      <span class="var-val">${feat.drtg ? feat.drtg.join(' vs ') : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">Raw Offense PPG (H / A)</span>
      <span class="var-val">${offPpg ? offPpg.join(' vs ') : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">Raw Defense Opp PPG (H / A)</span>
      <span class="var-val">${defPpg ? defPpg.join(' vs ') : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">Home vs Road Splits (H / A)</span>
      <span class="var-val">${splits ? `${splits[0]} (Home) vs ${splits[1]} (Road)` : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">Recent L10 Form</span>
      <span class="var-val">${last10 ? `${last10[0]} vs ${last10[1]}` : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">Rest Days (H / A)</span>
      <span class="var-val">${feat.rest_days ? `${feat.rest_days[0] ?? '2+'}d vs ${feat.rest_days[1] ?? '2+'}d` : '-'}</span>
    </div>
    <div class="var-row">
      <span class="var-name">🩹 Auto Injury / Bench Impact (${g.home})</span>
      <span class="var-val" style="color:${hPlayers.length > 0 ? 'var(--accent-rose)' : 'var(--primary)'};">${hAbsText}</span>
    </div>
    <div class="var-row">
      <span class="var-name">🩹 Auto Injury / Bench Impact (${g.away})</span>
      <span class="var-val" style="color:${aPlayers.length > 0 ? 'var(--accent-rose)' : 'var(--primary)'};">${aAbsText}</span>
    </div>
    ${(g.spread_line != null || g.ou_line != null) ? `
      <div class="var-row">
        <span class="var-name">Bookmaker Spread / Total Line</span>
        <span class="var-val">${g.spread_line != null ? (g.spread_line > 0 ? `+${g.spread_line}` : g.spread_line) : '-'} | O/U ${g.ou_line || '-'}</span>
      </div>
    ` : ''}
  `;
}

// ------------------------------------------------------------- Tennis Matches
async function loadTennisMatches(forceRefresh = false) {
  const container = document.getElementById('tennisList');
  const notice = document.getElementById('tennisFilesNotice');
  if (forceRefresh || !appCache['tennis_matches']) {
    container.innerHTML = `
      <div class="state-box">
        <div class="spinner"></div>
        <p>Loading ATP & WTA tournament draws & calculating Elo predictions...</p>
        <p style="font-size:0.8rem; color:var(--text-muted); margin-top:0.35rem;">Initial load indexes 12 seasons of tennis data (takes ~5-10s)...</p>
      </div>
    `;
  }

  try {
    const url = `/api/tennis/matches${forceRefresh ? '?refresh=true' : ''}`;
    const data = await fetchCached('tennis_matches', url, forceRefresh);

    if (!data.files_status || data.files_status.length === 0) {
      notice.style.display = 'block';
      notice.innerHTML = `
        <div style="background:rgba(56, 189, 248, 0.1); border:1px solid var(--accent-blue); padding:0.75rem 1rem; border-radius:8px; font-size:0.82rem;">
          ℹ️ <strong>Tennis History Notice:</strong> Live ATP/WTA tournament draws and fixtures are fetched live from ESPN. To enable historical surface-Elo and odds calibration, place yearly Excel files from tennis-data.co.uk into <code>data/tennis/</code> (e.g. <code>2025.xlsx</code>, <code>2026.xlsx</code>).
        </div>
      `;
    } else {
      notice.style.display = 'none';
    }

    if (!data.matches || data.matches.length === 0) {
      container.innerHTML = `<div class="state-box"><p>No live tennis matches found at this time.</p></div>`;
      return;
    }

    const scheduledOnly = document.getElementById('chkUpcomingTennis')?.checked ?? false;
    const hideTbd = document.getElementById('chkHideTbdTennis')?.checked ?? true;

    let filteredMatches = data.matches;
    if (scheduledOnly) {
      filteredMatches = filteredMatches.filter(m => m.state === 'pre' || m.state === 'in');
    }
    if (hideTbd) {
      filteredMatches = filteredMatches.filter(m => m.a !== 'TBD' && m.b !== 'TBD');
    }
    if (selectedTennisConfidence !== 'ALL') {
      filteredMatches = filteredMatches.filter(m => m.recommendation?.confidence === selectedTennisConfidence);
    }

    if (filteredMatches.length === 0) {
      container.innerHTML = `<div class="state-box"><p>No tennis matches matching the selected filters.</p></div>`;
      return;
    }

    // Group matches by Tournament and sort chronologically within each Tour
    const groupsMap = {};
    filteredMatches.forEach(m => {
      const tour = (m.tour || 'ATP').toUpperCase();
      const tier = m.tier || `${tour} 250`;
      const key = `${tour} • ${m.tournament || 'Tournament'}`;
      if (!groupsMap[key]) {
        groupsMap[key] = {
          tour,
          tier,
          title: `${key} — ${tier}`,
          surface: m.surface || 'Hard',
          minStart: m.start || '9999',
          maxStart: m.start || '',
          matches: []
        };
      }
      if (m.start && m.start < groupsMap[key].minStart) groupsMap[key].minStart = m.start;
      if (m.start && m.start > groupsMap[key].maxStart) groupsMap[key].maxStart = m.start;
      groupsMap[key].matches.push(m);
    });

    const sortedGroups = Object.values(groupsMap).sort((a, b) => {
      if (a.tour !== b.tour) return a.tour.localeCompare(b.tour);
      return a.minStart.localeCompare(b.minStart);
    });

    container.innerHTML = sortedGroups.map((grp, idx) => {
      grp.matches.sort((a, b) => (a.start || '').localeCompare(b.start || ''));
      const isPast = grp.matches.every(m => m.state === 'post');
      let hits = 0;
      let finCount = 0;
      grp.matches.forEach(m => {
        if (m.state === 'post' && m.recommendation?.pick_index != null && (m.a_winner || m.b_winner)) {
          finCount++;
          const actualIdx = m.a_winner ? 0 : 1;
          if (m.recommendation.pick_index === actualIdx) hits++;
        }
      });
      const accBadge = finCount > 0
        ? `<span class="badge ${Math.round((hits / finCount) * 100) >= 50 ? 'badge-green' : 'badge-rose'}" title="${hits} of ${finCount} match winner predictions correct">🎯 ${Math.round((hits / finCount) * 100)}% (${hits}/${finCount})</span>`
        : '';

      let dateSpan = `${grp.surface} Court`;
      if (grp.minStart && grp.minStart !== '9999') {
        const d0 = new Date(grp.minStart).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        const d1 = grp.maxStart ? new Date(grp.maxStart).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : d0;
        dateSpan = `${d0 === d1 ? d0 : `${d0} – ${d1}`} • ${grp.surface} Court`;
      }

      return `
        <div class="matchweek-section ${isPast ? 'collapsed' : ''}" id="sec_tn_${idx}">
          <div class="matchweek-header" data-target="sec_tn_${idx}">
            <div class="mw-left">
              <span class="mw-title">🎾 ${grp.title}</span>
              <span class="mw-dates">${dateSpan}</span>
              ${isPast ? '<span style="font-size:0.75rem; color:var(--text-muted);">(Past Results)</span>' : ''}
              ${accBadge}
            </div>
            <div class="mw-right">
              <span class="mw-badge">${grp.matches.length} match${grp.matches.length > 1 ? 'es' : ''}</span>
              <span class="mw-chevron">▼</span>
            </div>
          </div>
          <div class="fixtures-grid">
            ${grp.matches.map(renderTennisCard).join('')}
          </div>
        </div>
      `;
    }).join('');

    container.querySelectorAll('.matchweek-header').forEach(hdr => {
      hdr.addEventListener('click', (e) => {
        const secId = e.currentTarget.dataset.target;
        const sec = document.getElementById(secId);
        if (sec) sec.classList.toggle('collapsed');
      });
    });

    attachCardEvents();
  } catch (err) {
    container.innerHTML = `<div class="state-box"><p style="color:var(--accent-rose)">Failed to load tennis: ${err.message}</p></div>`;
  }
}

function renderTennisCard(m) {
  const reco = m.recommendation || {};
  const probs = m.probs || [0.5, 0.5];
  const pA = Math.round(probs[0] * 100);
  const pB = Math.round(probs[1] * 100);
  const statusClass = m.state === 'in' ? 'status-live' : (m.state === 'post' ? 'status-post' : 'status-pre');

  const ranks = m.features?.rank || [];
  const tourLabel = m.tour || 'ATP';
  const rankA = m.a_rank || (ranks[0] && ranks[0] < 300 ? ranks[0] : null);
  const rankB = m.b_rank || (ranks[1] && ranks[1] < 300 ? ranks[1] : null);
  const seedAText = m.a_seed ? `Seed #${m.a_seed}` : '';
  const seedBText = m.b_seed ? `Seed #${m.b_seed}` : '';

  const valueBetsHtml = (reco.value_bets && reco.value_bets.length > 0) ? `
    <div class="value-banner">
      <div>
        <span class="value-highlight">⭐ VALUE:</span> ${reco.value_bets[0].outcome} @ <strong>${reco.value_bets[0].odds}</strong>
      </div>
      <span class="edge-pill">+${(reco.value_bets[0].edge * 100).toFixed(1)}%</span>
    </div>
  ` : '';

  // Parse set scores per player (e.g. "6-4 6-3" -> A: "6  6", B: "4  3")
  let scoreA = '';
  let scoreB = '';
  if (m.score) {
    const sets = m.score.trim().split(/\s+/);
    const sA = [];
    const sB = [];
    sets.forEach(st => {
      const parts = st.split('-');
      if (parts.length === 2) {
        sA.push(parts[0]);
        sB.push(parts[1]);
      }
    });
    scoreA = sA.join(' ');
    scoreB = sB.join(' ');
  }

  let pickStatusClass = '';
  if (m.state === 'post' && reco.pick_index != null) {
    const actualIdx = m.a_winner ? 0 : (m.b_winner ? 1 : null);
    if (actualIdx !== null) {
      pickStatusClass = reco.pick_index === actualIdx ? 'pick-hit' : 'pick-miss';
    }
  }

  return `
    <div class="match-card">
      <div class="match-strip">
        <!-- Col 1: Status, Tournament & Surface -->
        <div class="strip-meta">
          <div class="match-status">
            <span class="status-dot ${statusClass}"></span>
            <span>${m.status_detail || m.status}</span>
          </div>
          <div class="meta-sub">${m.tournament}${m.tier ? ` (${m.tier})` : ''} • ${m.round || ''} (${m.surface})</div>
        </div>

        <!-- Col 2: Stacked Double-Row Players & Set Scores -->
        <div class="strip-teams">
          <div class="team-line">
            <div class="team-line-left">
              ${rankA ? `<span class="team-rank-pill" title="${tourLabel} Ranking #${rankA}">#${rankA}</span>` : ''}
              ${m.a_flag ? `<img class="team-logo-sm" src="${m.a_flag}" alt="" style="border-radius:2px;">` : ''}
              <span class="team-name ${m.a_winner ? 'winner' : ''}">${m.a}</span>
              ${seedAText ? `<span class="team-sub-inline" style="color:var(--accent-blue);">${seedAText}</span>` : ''}
            </div>
            <span class="team-score-cell">${scoreA}</span>
          </div>

          <div class="team-line">
            <div class="team-line-left">
              ${rankB ? `<span class="team-rank-pill" title="${tourLabel} Ranking #${rankB}">#${rankB}</span>` : ''}
              ${m.b_flag ? `<img class="team-logo-sm" src="${m.b_flag}" alt="" style="border-radius:2px;">` : ''}
              <span class="team-name ${m.b_winner ? 'winner' : ''}">${m.b}</span>
              ${seedBText ? `<span class="team-sub-inline" style="color:var(--accent-blue);">${seedBText}</span>` : ''}
            </div>
            <span class="team-score-cell">${scoreB}</span>
          </div>
        </div>

        <!-- Col 3: Pick, Win Probability Bar & Surface Elo -->
        <div class="strip-prediction">
          <div class="pred-head">
            <span class="pick-title ${pickStatusClass}">🎯 ${reco.pick || m.a}</span>
            <span class="badge ${reco.confidence === 'High' ? 'badge-green' : (reco.confidence === 'Medium' ? 'badge-blue' : 'badge-amber')}">
              ${reco.confidence || 'Low'} (${Math.round((reco.probability || 0.5) * 100)}%)
            </span>
          </div>

          <div class="prob-bar" title="${m.a}: ${pA}% | ${m.b}: ${pB}%">
            <div class="prob-seg seg-player-a" style="width: ${pA}%">${pA}%</div>
            <div class="prob-seg seg-player-b" style="width: ${pB}%">${pB}%</div>
          </div>

          <div class="strip-submetrics">
            <span>Elo (${m.surface}): <strong style="color:var(--text-main)">${m.features?.surface_elo ? m.features.surface_elo.join(' vs ') : '-'}</strong></span>
            <span>H2H: <strong style="color:var(--text-main)">${m.features?.h2h || '-'}</strong></span>
          </div>
        </div>

        <!-- Col 4: Bookmaker & Fair Odds + Value Bet -->
        <div class="strip-odds">
          <div class="odds-line">
            <span class="odds-label" title="${m.odds_books && (m.odds_books['Stake.com'] || m.odds_books['Winamax.fr']) ? 'Stake.com (International Low-Vig Odds)' : 'Bookmaker Odds'}">${m.odds_books && (m.odds_books['Stake.com'] || m.odds_books['Winamax.fr']) ? '🎰 Stake' : 'Book'}</span>
            <span class="odds-vals" style="color:var(--accent-blue)">${m.best_odds ? `${m.best_odds[0]} / ${m.best_odds[1]}` : (m.market_odds ? `${m.market_odds[0]} / ${m.market_odds[1]}` : 'Awaiting')}</span>
          </div>
          <div class="odds-line">
            <span class="odds-label">Fair</span>
            <span class="odds-vals" style="color:var(--primary)">${reco.fair_odds ? `${reco.fair_odds[0]} / ${reco.fair_odds[1]}` : '-'}</span>
          </div>
          ${valueBetsHtml}
        </div>

        <!-- Col 5: Details Toggle -->
        <div class="strip-actions">
          <button class="btn-toggle-vars" data-target="vars_tn_${m.espn_id}">
            📊 Details ▼
          </button>
        </div>
      </div>

      <div class="variables-panel" id="vars_tn_${m.espn_id}">
        <div class="var-row">
          <span class="var-name">Surface</span>
          <span class="var-val">${m.surface} (Best of ${m.best_of})</span>
        </div>
        ${m.features ? `
          <div class="var-row">
            <span class="var-name">Surface Elo</span>
            <span class="var-val">${m.features.surface_elo ? m.features.surface_elo.join(' vs ') : '-'}</span>
          </div>
          <div class="var-row">
            <span class="var-name">Head to Head</span>
            <span class="var-val">${m.features.h2h || '-'}</span>
          </div>
          <div class="var-row">
            <span class="var-name">Fatigue (Sets last 7d)</span>
            <span class="var-val">${m.features.sets_last_7d ? m.features.sets_last_7d.join(' vs ') : '-'}</span>
          </div>
        ` : ''}
        ${m.market_odds ? `
          <div class="var-row">
            <span class="var-name">Market Odds</span>
            <span class="var-val">${m.market_odds.join(' vs ')}</span>
          </div>
        ` : ''}
      </div>
    </div>
  `;
}

let currentBacktestSport = 'football';
let currentBacktestData = null;

function computeCalibrationWinRate(calibration, thresholdPct) {
  if (!Array.isArray(calibration) || calibration.length === 0) return null;
  let totalN = 0;
  let totalHits = 0;
  calibration.forEach(b => {
    const m = String(b.bin || '').match(/(\d+)%-(\d+)%/);
    if (!m) return;
    const low = parseFloat(m[1]);
    const high = parseFloat(m[2]);
    const n = Number(b.n || 0);
    const actual = Number(b.actual || 0);
    if (low >= thresholdPct) {
      totalN += n;
      totalHits += n * actual;
    } else if (high > thresholdPct && high > low) {
      const frac = (high - thresholdPct) / (high - low);
      totalN += n * frac;
      totalHits += n * frac * actual;
    }
  });
  if (totalN <= 0) return null;
  return {
    winRate: totalHits / totalN,
    matches: Math.round(totalN),
    hits: Math.round(totalHits),
  };
}

function setupBacktestToggle() {
  const btnFb = document.getElementById('btnBtFootball');
  const btnNba = document.getElementById('btnBtNba');
  const btnTn = document.getElementById('btnBtTennis');
  const chkExclude = document.getElementById('chkExcludeDraws');
  const rngConf = document.getElementById('rngBtConf');

  if (btnFb && !btnFb.dataset.wired) {
    btnFb.dataset.wired = 'true';
    btnFb.addEventListener('click', () => {
      btnFb.classList.add('active');
      if (btnNba) btnNba.classList.remove('active');
      if (btnTn) btnTn.classList.remove('active');
      currentBacktestSport = 'football';
      loadBacktestData();
    });
  }

  if (btnNba && !btnNba.dataset.wired) {
    btnNba.dataset.wired = 'true';
    btnNba.addEventListener('click', () => {
      btnNba.classList.add('active');
      if (btnFb) btnFb.classList.remove('active');
      if (btnTn) btnTn.classList.remove('active');
      currentBacktestSport = 'nba';
      loadBacktestData();
    });
  }

  if (btnTn && !btnTn.dataset.wired) {
    btnTn.dataset.wired = 'true';
    btnTn.addEventListener('click', () => {
      btnTn.classList.add('active');
      if (btnFb) btnFb.classList.remove('active');
      if (btnNba) btnNba.classList.remove('active');
      currentBacktestSport = 'tennis';
      loadBacktestData();
    });
  }

  if (chkExclude && !chkExclude.dataset.wired) {
    chkExclude.dataset.wired = 'true';
    chkExclude.addEventListener('change', () => {
      renderBacktestView();
    });
  }

  if (rngConf && !rngConf.dataset.wired) {
    rngConf.dataset.wired = 'true';
    rngConf.addEventListener('input', () => {
      renderBacktestView();
    });
  }
}

async function loadBacktestData(forceRefresh = false) {
  setupBacktestToggle();
  try {
    const cacheKey = `bt_${currentBacktestSport}`;
    currentBacktestData = await fetchCached(cacheKey, `/api/backtest/${currentBacktestSport}`, forceRefresh);
    renderBacktestView();
  } catch (err) {
    console.error('Error loading backtest:', err);
  }
}

function renderBacktestView() {
  const data = currentBacktestData;
  if (!data || !data.test) return;
  const test = data.test;

  const drawFilterContainer = document.getElementById('drawFilterContainer');
  const chkExclude = document.getElementById('chkExcludeDraws');
  const rngConf = document.getElementById('rngBtConf');
  const lblConfVal = document.getElementById('lblBtConfVal');
  const statConfSample = document.getElementById('statConfSample');

  const confThreshold = rngConf ? parseInt(rngConf.value, 10) : 60;
  if (lblConfVal) lblConfVal.innerText = `${confThreshold}%`;

  // In tennis/NBA, draws do not exist, so hide the draw filter toggle unless football is selected
  if (drawFilterContainer) {
    drawFilterContainer.style.display = currentBacktestSport === 'football' ? 'flex' : 'none';
  }

  const excludeDraws = currentBacktestSport === 'football' && (chkExclude?.checked ?? false);

  // Dynamic metrics when draws are excluded
  const statAccuracySub = document.getElementById('statAccuracySub');
  if (excludeDraws && test.no_draw) {
    document.getElementById('statTestMatches').innerText = `${test.no_draw.matches}`;
    document.getElementById('statAccuracy').innerText = `${(test.no_draw.accuracy * 100).toFixed(1)}%`;
    document.getElementById('statAccuracy').style.color = 'var(--primary)';
    if (statAccuracySub) statAccuracySub.innerText = `Decisive games only (draws excluded)`;
  } else {
    document.getElementById('statTestMatches').innerText = test.matches;
    document.getElementById('statAccuracy').innerText = (test.blends.final.accuracy * 100).toFixed(1) + '%';
    if (statAccuracySub) {
      statAccuracySub.innerText = currentBacktestSport === 'nba'
        ? 'vs 55.4% NBA home baseline'
        : 'vs 44.2% league baseline';
    }
  }

  // NBA Home vs Away Backing Accuracy cards
  const cardBtHomeAcc = document.getElementById('cardBtHomeAcc');
  const cardBtAwayAcc = document.getElementById('cardBtAwayAcc');
  if (cardBtHomeAcc && cardBtAwayAcc) {
    if (currentBacktestSport === 'nba') {
      cardBtHomeAcc.style.display = 'block';
      cardBtAwayAcc.style.display = 'block';
      const allRec = data.recent_predictions || [];
      let hPicks = 0, hHits = 0, aPicks = 0, aHits = 0;
      allRec.forEach(r => {
        const isHomePick = r.pick_side ? (r.pick_side === 'home') : String(r.match || '').startsWith((r.pick || '') + ' vs ');
        if (isHomePick) {
          hPicks++;
          if (r.hit) hHits++;
        } else {
          aPicks++;
          if (r.hit) aHits++;
        }
      });
      const splits = test.home_away_splits || {};
      const hAcc = hPicks > 0 ? (hHits / hPicks) : (splits.home_accuracy ?? 0.692);
      const aAcc = aPicks > 0 ? (aHits / aPicks) : (splits.away_accuracy ?? 0.635);
      document.getElementById('statHomeAcc').innerText = `${(hAcc * 100).toFixed(1)}%`;
      document.getElementById('statHomeAccSub').innerText = hPicks > 0
        ? `${hHits} / ${hPicks} wins when backing Home`
        : `Win rate when picking Home`;
      document.getElementById('statAwayAcc').innerText = `${(aAcc * 100).toFixed(1)}%`;
      document.getElementById('statAwayAccSub').innerText = aPicks > 0
        ? `${aHits} / ${aPicks} wins when backing Away`
        : `Win rate when picking Away`;
    } else {
      cardBtHomeAcc.style.display = 'none';
      cardBtAwayAcc.style.display = 'none';
    }
  }

  // Dynamic Confidence Slicer Win Rate calculation
  const calib = test.blends?.final?.calibration || [];
  const calibRes = computeCalibrationWinRate(calib, confThreshold);
  if (calibRes) {
    document.getElementById('statConfWinRate').innerText = `${(calibRes.winRate * 100).toFixed(1)}%`;
    if (statConfSample) {
      statConfSample.innerText = `${calibRes.hits} / ${calibRes.matches} wins at ≥${confThreshold}% conf`;
    }
  } else {
    const confItem = test.blends?.final?.betting?.['top_pick_conf>=60%@avg'];
    document.getElementById('statConfWinRate').innerText = confItem ? (confItem.hit_rate * 100).toFixed(1) + '%' : '-';
    if (statConfSample) {
      statConfSample.innerText = `High confidence prediction accuracy`;
    }
  }

  const roiItem = test.blends.final.betting['value_edge>=4%@avg'] || test.blends.final.betting['value_edge>=4%@max'];
  const roiVal = roiItem ? roiItem.roi : 0;
  const roiEl = document.getElementById('statValueRoi');
  roiEl.innerText = (roiVal >= 0 ? '+' : '') + (roiVal * 100).toFixed(1) + '%';
  roiEl.style.color = roiVal >= 0 ? 'var(--primary)' : 'var(--accent-rose)';

  // Table rows filtered by Exclude Draws & Confidence Slicer if applicable
  const tbody = document.getElementById('backtestRows');
  let recent = data.recent_predictions || [];

  if (excludeDraws) {
    recent = recent.filter(r => r.result !== 'D');
  }
  if (rngConf && recent.length > 0) {
    recent = recent.filter(r => Math.max(...(r.probs || [0])) * 100 >= confThreshold);
  }

  if (recent.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;">Showing aggregate backtest metrics above (no recent sample matches at ≥${confThreshold}% confidence).</td></tr>`;
    return;
  }
  tbody.innerHTML = recent.slice(0, 50).map(r => {
    const isHit = r.hit;
    return `
      <tr>
        <td>${r.date}</td>
        <td><span class="badge badge-blue">${r.league || r.tour || 'Match'}</span></td>
        <td style="font-weight:600;">${r.match}</td>
        <td style="font-weight:700;">${r.score || '-'}</td>
        <td>${(r.probs[0]*100).toFixed(0)}% / ${(r.probs[1]*100).toFixed(0)}% ${r.probs[2] !== undefined ? '/ ' + (r.probs[2]*100).toFixed(0) + '%' : ''}</td>
        <td style="font-weight:700; color:var(--accent-blue);">${r.pick}</td>
        <td>${r.result}</td>
        <td>
          <span class="badge ${isHit ? 'badge-green' : 'badge-rose'}">
            ${isHit ? '✓ HIT' : '✗ MISS'}
          </span>
        </td>
      </tr>
    `;
  }).join('');
}

// ============================================================= Daily Top 3 Safe Bets Calendar & 3-Month History
const SPORT_ICONS = {
  football: '⚽',
  nba: '🏀',
  tennis: '🎾'
};

function getDayPicksForSport(dayObj, sportFilter) {
  if (!dayObj) return [];
  if (!sportFilter || sportFilter === 'ALL') {
    return dayObj.top3 || [];
  }
  return (dayObj.by_sport && dayObj.by_sport[sportFilter]) || [];
}

function formatMonthLabel(ym) {
  const [y, m] = String(ym).split('-').map(Number);
  if (!y || !m) return ym;
  const d = new Date(Date.UTC(y, m - 1, 1));
  return d.toLocaleDateString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' });
}

function formatFullDateLabel(dateStr) {
  const [y, m, d] = String(dateStr).split('-').map(Number);
  if (!y || !m || !d) return dateStr;
  const dt = new Date(Date.UTC(y, m - 1, d));
  return dt.toLocaleDateString('en-US', { weekday: 'long', month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
}

async function loadDailyPicks(forceRefresh = false) {
  const spotEl = document.getElementById('calendarSpotlight');
  if (forceRefresh || !appCache['daily_picks']) {
    if (spotEl) {
      spotEl.innerHTML = `
        <div class="state-box">
          <div class="spinner"></div>
          <p>Evaluating Top 3 Safest Bets per day across Football, NBA & Tennis (past 3 months + upcoming)...</p>
        </div>
      `;
    }
  }

  try {
    const url = `/api/daily-picks${forceRefresh ? '?refresh=true' : ''}`;
    dailyPicksData = await fetchCached('daily_picks', url, forceRefresh);
    if (!calSelectedMonth && dailyPicksData.months && dailyPicksData.months.length > 0) {
      const todayYm = (dailyPicksData.today || '').slice(0, 7);
      calSelectedMonth = dailyPicksData.months.includes(todayYm) ? todayYm : dailyPicksData.months[0];
    }
    if (!calSelectedDate && dailyPicksData.days) {
      const todayStr = dailyPicksData.today;
      const allDates = Object.keys(dailyPicksData.days).sort();
      if (todayStr && dailyPicksData.days[todayStr]) {
        calSelectedDate = todayStr;
      } else {
        const upOrRecent = allDates.filter(d => d <= todayStr);
        calSelectedDate = upOrRecent.length > 0 ? upOrRecent[upOrRecent.length - 1] : allDates[allDates.length - 1];
      }
      if (calSelectedDate) {
        calSelectedMonth = calSelectedDate.slice(0, 7);
      }
    }
    renderDailyCalendarView();
  } catch (err) {
    if (spotEl) {
      spotEl.innerHTML = `<div class="state-box"><p style="color:var(--accent-rose)">Failed to load Daily Top 3 Safe Bets: ${err.message}</p></div>`;
    }
  }
}

function renderDailyCalendarView() {
  if (!dailyPicksData) return;
  const sum = (calSelectedSport === 'ALL' || !dailyPicksData.summary_by_sport)
    ? dailyPicksData.summary
    : (dailyPicksData.summary_by_sport[calSelectedSport] || dailyPicksData.summary);

  if (sum) {
    document.getElementById('calStatHitRate').innerText = `${(sum.hit_rate * 100).toFixed(1)}%`;
    document.getElementById('calStatHitSub').innerText = `${sum.hits} / ${sum.settled_picks} winning Top 3 picks (${sum.total_days} active days)`;

    document.getElementById('calStatLock1').innerText = `${(sum.lock1_hit_rate * 100).toFixed(1)}%`;
    document.getElementById('calStatLock1Sub').innerText = `${sum.lock1_hits} / ${sum.lock1_settled} wins on #1 Safest Lock`;

    const twoPlusRate = sum.full_settled_days > 0 ? ((sum.two_plus_days / sum.full_settled_days) * 100).toFixed(1) + '%' : '-';
    document.getElementById('calStatTwoPlus').innerText = twoPlusRate;
    document.getElementById('calStatTwoPlusSub').innerText = `${sum.two_plus_days} / ${sum.full_settled_days} full days won ≥2 of 3`;

    const sweepRate = sum.full_settled_days > 0 ? ((sum.sweep_days / sum.full_settled_days) * 100).toFixed(1) + '%' : '0%';
    document.getElementById('calStatSweeps').innerText = `${sum.sweep_days} Days (${sweepRate})`;
    document.getElementById('calStatSweepsSub').innerText = `Perfect 3/3 daily clean sweeps`;

    const profEl = document.getElementById('calStatProfit');
    const pSign = sum.profit_units >= 0 ? '+' : '';
    const rSign = sum.roi >= 0 ? '+' : '';
    profEl.innerText = `${pSign}${sum.profit_units.toFixed(1)}u (${rSign}${(sum.roi * 100).toFixed(1)}%)`;
    profEl.style.color = sum.profit_units >= 0 ? 'var(--primary)' : 'var(--accent-rose)';
  }

  renderCalendarMonthsBar();
  renderCalendarSpotlight();
  renderMonthlyGrid();
  renderCalendarHistoryTable();
}

function renderCalendarMonthsBar() {
  const bar = document.getElementById('calendarMonthsBar');
  if (!bar || !dailyPicksData) return;
  const months = dailyPicksData.months || [];
  const days = dailyPicksData.days || {};

  bar.innerHTML = months.map(ym => {
    let mSettled = 0;
    let mHits = 0;
    Object.values(days).forEach(dObj => {
      if (dObj.month !== ym) return;
      const picks = getDayPicksForSport(dObj, calSelectedSport);
      picks.forEach(p => {
        if (p.state === 'post' && p.hit !== null && p.hit !== undefined) {
          mSettled++;
          if (p.hit) mHits++;
        }
      });
    });
    const mRate = mSettled > 0 ? ` (${Math.round((mHits / mSettled) * 100)}%)` : '';
    const isAct = ym === calSelectedMonth ? 'active' : '';
    return `<button class="pill-btn ${isAct}" data-ym="${ym}">🗓️ ${formatMonthLabel(ym)}${mRate}</button>`;
  }).join('');

  bar.querySelectorAll('.pill-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      calSelectedMonth = e.currentTarget.dataset.ym;
      // If selected date is not in this month, pick the latest active date in this month
      if (!calSelectedDate || !calSelectedDate.startsWith(calSelectedMonth)) {
        const monthDates = Object.keys(days).filter(d => d.startsWith(calSelectedMonth)).sort();
        if (monthDates.length > 0) {
          const todayIso = dailyPicksData.today || '';
          calSelectedDate = monthDates.includes(todayIso) ? todayIso : monthDates[monthDates.length - 1];
        }
      }
      renderDailyCalendarView();
    });
  });
}

function renderCalendarSpotlight() {
  const spotEl = document.getElementById('calendarSpotlight');
  if (!spotEl || !dailyPicksData) return;

  const days = dailyPicksData.days || {};
  const activeDates = Object.keys(days).filter(d => getDayPicksForSport(days[d], calSelectedSport).length > 0).sort();
  if (activeDates.length === 0) {
    spotEl.innerHTML = `<div class="state-box"><p>No picks found for the selected sport filter.</p></div>`;
    return;
  }

  if (!calSelectedDate || !days[calSelectedDate]) {
    calSelectedDate = activeDates[activeDates.length - 1];
  }

  const dayObj = days[calSelectedDate];
  const picks = getDayPicksForSport(dayObj, calSelectedSport);
  const currIdx = activeDates.indexOf(calSelectedDate);
  const prevDate = currIdx > 0 ? activeDates[currIdx - 1] : null;
  const nextDate = currIdx >= 0 && currIdx < activeDates.length - 1 ? activeDates[currIdx + 1] : null;
  const isToday = calSelectedDate === dailyPicksData.today;

  let settled = 0;
  let hits = 0;
  let combOdds = 1.0;
  let dayProfit = 0.0;
  picks.forEach(p => {
    combOdds *= Number(p.odds || 1);
    if (p.state === 'post' && p.hit !== null && p.hit !== undefined) {
      settled++;
      if (p.hit) {
        hits++;
        dayProfit += (Number(p.odds || 1) - 1.0);
      } else {
        dayProfit -= 1.0;
      }
    }
  });

  let dayBadgeHtml = '';
  if (settled === picks.length && picks.length > 0) {
    const isSweep = hits === picks.length && picks.length === 3;
    const bClass = hits >= 2 ? 'badge-green' : (hits === 1 ? 'badge-amber' : 'badge-rose');
    const pStr = dayProfit >= 0 ? `+${dayProfit.toFixed(2)}u` : `${dayProfit.toFixed(2)}u`;
    dayBadgeHtml = `<span class="badge ${bClass}" style="font-size:0.8rem; padding:0.3rem 0.7rem;">${isSweep ? '💎 3/3 CLEAN SWEEP' : `🎯 ${hits}/${settled} WON`} (${pStr})</span>`;
  } else {
    dayBadgeHtml = `<span class="badge badge-blue" style="font-size:0.8rem; padding:0.3rem 0.7rem;">⏳ ${picks.length - settled} Pending / Scheduled</span>`;
  }

  const rankLabels = ['🥇 #1 SAFEST LOCK', '🥈 #2 SAFE PICK', '🥉 #3 SAFE PICK'];

  spotEl.innerHTML = `
    <div class="cal-spotlight-header">
      <div style="display:flex; align-items:center; gap:0.65rem; flex-wrap:wrap;">
        <button class="pill-btn" id="btnSpotPrev" ${!prevDate ? 'disabled style="opacity:0.4; cursor:default;"' : ''} title="Previous Day">◀</button>
        <div>
          <div style="display:flex; align-items:center; gap:0.5rem;">
            <h3 style="font-size:1.12rem; font-weight:800; color:var(--text-main);">
              ${formatFullDateLabel(calSelectedDate)}
            </h3>
            ${isToday ? '<span class="badge badge-green">TODAY</span>' : ''}
          </div>
          <span style="font-size:0.76rem; color:var(--text-muted);">
            Top ${picks.length} Highest-Safety Selections • Combined 3-Pick Parlay Multiplier: <strong style="color:var(--accent-blue);">${combOdds.toFixed(2)}x</strong>
          </span>
        </div>
        <button class="pill-btn" id="btnSpotNext" ${!nextDate ? 'disabled style="opacity:0.4; cursor:default;"' : ''} title="Next Day">▶</button>
      </div>
      <div style="display:flex; align-items:center; gap:0.5rem; flex-wrap:wrap;">
        ${dayBadgeHtml}
        ${dailyPicksData.today && days[dailyPicksData.today] && !isToday
          ? `<button class="pill-btn active" id="btnJumpToday" style="font-size:0.76rem;">Jump to Today (${dailyPicksData.today})</button>`
          : ''}
      </div>
    </div>

    ${picks.length === 0 ? `<div class="state-box" style="padding:1.5rem;"><p>No matches available on ${calSelectedDate} for this sport filter.</p></div>` : `
      <div class="safe-picks-grid">
        ${picks.map((p, idx) => {
          const isPost = p.state === 'post' && p.hit !== null && p.hit !== undefined;
          const cardStateClass = isPost ? (p.hit ? 'card-hit' : 'card-miss') : '';
          const probPct = (Number(p.probability || 0) * 100).toFixed(1);
          const spIcon = SPORT_ICONS[p.sport] || '🏆';
          const profStr = isPost
            ? (p.hit ? `+${(Number(p.odds) - 1).toFixed(2)}u` : `-1.00u`)
            : `Fair @ ${p.fair_odds}`;
          const outcomeHtml = isPost
            ? (p.hit
                ? `<span style="color:var(--primary);">✓ WON ${p.score ? `(${p.score})` : ''}</span><span style="color:var(--primary);">${profStr}</span>`
                : `<span style="color:var(--accent-rose);">✗ LOST ${p.score ? `(${p.score})` : ''}</span><span style="color:var(--accent-rose);">${profStr}</span>`)
            : `<span style="color:var(--accent-blue);">⏳ ${p.state === 'in' ? 'LIVE IN PLAY' : 'SCHEDULED'}</span><span style="color:var(--text-muted);">${profStr}</span>`;

          return `
            <div class="safe-pick-card ${cardStateClass}">
              <div class="safe-pick-top">
                <span class="rank-pill rank-${idx + 1}">${rankLabels[idx] || `#${idx + 1} SAFE PICK`}</span>
                <span class="badge badge-blue">${spIcon} ${p.league_name || p.league}</span>
              </div>

              <div class="safe-pick-match">
                <div style="display:flex; align-items:center; gap:0.45rem; min-width:0;">
                  ${p.home_logo ? `<img class="team-logo-sm" src="${p.home_logo}" alt="">` : ''}
                  <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${p.match}</span>
                  ${p.away_logo ? `<img class="team-logo-sm" src="${p.away_logo}" alt="">` : ''}
                </div>
              </div>

              <div class="safe-pick-target">
                <div>
                  <div style="font-size:0.68rem; color:var(--text-dim); text-transform:uppercase; font-weight:700;">Recommended Bet (${p.market_type})</div>
                  <div style="font-size:0.96rem; font-weight:800; color:#fff; margin-top:0.15rem;">🎯 ${p.pick}</div>
                </div>
                <div style="text-align:right;">
                  <div style="font-size:0.95rem; font-weight:800; color:var(--primary);">${probPct}%</div>
                  <div style="font-size:0.75rem; font-weight:700; color:var(--accent-blue);">Odds @ ${Number(p.odds).toFixed(2)}</div>
                </div>
              </div>

              <div class="prob-bar" style="height:8px;">
                <div class="prob-seg seg-player-a" style="width:${Math.min(100, Math.round(p.probability * 100))}%;"></div>
              </div>

              <div class="safe-pick-outcome">
                ${outcomeHtml}
              </div>
            </div>
          `;
        }).join('')}
      </div>
    `}
  `;

  const btnPrev = document.getElementById('btnSpotPrev');
  if (btnPrev && prevDate) {
    btnPrev.addEventListener('click', () => {
      calSelectedDate = prevDate;
      calSelectedMonth = prevDate.slice(0, 7);
      renderDailyCalendarView();
    });
  }
  const btnNext = document.getElementById('btnSpotNext');
  if (btnNext && nextDate) {
    btnNext.addEventListener('click', () => {
      calSelectedDate = nextDate;
      calSelectedMonth = nextDate.slice(0, 7);
      renderDailyCalendarView();
    });
  }
  const btnToday = document.getElementById('btnJumpToday');
  if (btnToday && dailyPicksData.today) {
    btnToday.addEventListener('click', () => {
      calSelectedDate = dailyPicksData.today;
      calSelectedMonth = dailyPicksData.today.slice(0, 7);
      renderDailyCalendarView();
    });
  }
}

function renderMonthlyGrid() {
  const grid = document.getElementById('calendarGrid');
  if (!grid || !dailyPicksData || !calSelectedMonth) return;

  const [year, month] = calSelectedMonth.split('-').map(Number);
  const firstDayDate = new Date(Date.UTC(year, month - 1, 1));
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  // Monday-based weekday index (0 = Mon, ..., 6 = Sun)
  const startOffset = (firstDayDate.getUTCDay() + 6) % 7;

  const daysMap = dailyPicksData.days || {};
  const todayIso = dailyPicksData.today || '';
  const cells = [];

  for (let i = 0; i < startOffset; i++) {
    cells.push(`<div class="cal-day cal-empty"></div>`);
  }

  for (let d = 1; d <= daysInMonth; d++) {
    const dStr = `${calSelectedMonth}-${String(d).padStart(2, '0')}`;
    const dObj = daysMap[dStr];
    const picks = getDayPicksForSport(dObj, calSelectedSport);
    const isToday = dStr === todayIso;
    const isSelected = dStr === calSelectedDate;

    let settled = 0;
    let hits = 0;
    picks.forEach(p => {
      if (p.state === 'post' && p.hit !== null && p.hit !== undefined) {
        settled++;
        if (p.hit) hits++;
      }
    });

    const isSweep = picks.length === 3 && settled === 3 && hits === 3;
    let dayBadge = '';
    if (picks.length > 0) {
      if (settled === picks.length) {
        const cls = hits === picks.length ? 'badge-green' : (hits >= 2 ? 'badge-blue' : (hits === 1 ? 'badge-amber' : 'badge-rose'));
        dayBadge = `<span class="badge ${cls}" style="font-size:0.65rem; padding:0.12rem 0.4rem;">${hits}/${settled}</span>`;
      } else {
        dayBadge = `<span class="badge badge-blue" style="font-size:0.65rem; padding:0.12rem 0.4rem;">⏳ ${picks.length}</span>`;
      }
    }

    const miniListHtml = picks.map((p, idx) => {
      const isPost = p.state === 'post' && p.hit !== null && p.hit !== undefined;
      const stCls = isPost ? (p.hit ? 'mini-hit' : 'mini-miss') : 'mini-pre';
      const icon = SPORT_ICONS[p.sport] || '•';
      const statusSym = isPost ? (p.hit ? '<span style="color:var(--primary)">✓</span>' : '<span style="color:var(--accent-rose)">✗</span>') : '<span style="color:var(--accent-blue)">•</span>';
      const shortPick = p.pick_team || p.pick;
      return `
        <div class="cal-mini-pick ${stCls}" title="#${idx + 1}: ${p.match} → ${p.pick} (${Math.round(p.probability * 100)}% @ ${p.odds})">
          <span class="cal-mini-name">${icon} ${shortPick}</span>
          <span class="cal-mini-meta">${Math.round(p.probability * 100)}% ${statusSym}</span>
        </div>
      `;
    }).join('');

    cells.push(`
      <div class="cal-day ${isToday ? 'cal-today' : ''} ${isSelected ? 'cal-selected' : ''} ${isSweep ? 'cal-sweep' : ''}" data-date="${dStr}">
        <div class="cal-day-head">
          <span class="cal-day-num">
            ${d}
            ${isToday ? '<span style="font-size:0.6rem; color:var(--primary); font-weight:800;">TODAY</span>' : ''}
          </span>
          ${dayBadge}
        </div>
        <div class="cal-mini-list">
          ${miniListHtml || '<span style="font-size:0.68rem; color:var(--text-dim); margin:auto 0;">No games</span>'}
        </div>
      </div>
    `);
  }

  grid.innerHTML = cells.join('');

  grid.querySelectorAll('.cal-day[data-date]').forEach(cell => {
    cell.addEventListener('click', (e) => {
      const dt = e.currentTarget.dataset.date;
      if (!dt) return;
      calSelectedDate = dt;
      renderCalendarSpotlight();
      renderMonthlyGrid();
      const spot = document.getElementById('calendarSpotlight');
      if (spot) spot.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
  });
}

function renderCalendarHistoryTable() {
  const tbody = document.getElementById('calendarHistoryRows');
  if (!tbody || !dailyPicksData) return;

  const daysMap = dailyPicksData.days || {};
  const todayIso = dailyPicksData.today || '';
  const sortedDates = Object.keys(daysMap).sort().reverse();

  const rowsHtml = [];

  sortedDates.forEach(dStr => {
    const dObj = daysMap[dStr];
    const picks = getDayPicksForSport(dObj, calSelectedSport);
    if (!picks || picks.length === 0) return;

    let settled = 0;
    let hits = 0;
    picks.forEach(p => {
      if (p.state === 'post' && p.hit !== null && p.hit !== undefined) {
        settled++;
        if (p.hit) hits++;
      }
    });

    const isSweep = picks.length === 3 && settled === 3 && hits === 3;
    const isTwoPlus = settled >= 2 && hits >= 2;
    const isUpcoming = dStr >= todayIso || settled < picks.length;

    if (calHistoryFilter === 'SWEEP' && !isSweep) return;
    if (calHistoryFilter === 'TWO_PLUS' && !isTwoPlus) return;
    if (calHistoryFilter === 'UPCOMING' && !isUpcoming) return;

    const daySumBadge = settled === picks.length
      ? `<span class="badge ${hits === picks.length ? 'badge-green' : (hits >= 2 ? 'badge-blue' : (hits === 1 ? 'badge-amber' : 'badge-rose'))}">${isSweep ? '💎 3/3 SWEEP' : `${hits}/${settled} WON`}</span>`
      : `<span class="badge badge-blue">⏳ ${settled}/${picks.length} Settled</span>`;

    picks.forEach((p, idx) => {
      const isPost = p.state === 'post' && p.hit !== null && p.hit !== undefined;
      const spIcon = SPORT_ICONS[p.sport] || '🏆';
      const rankBadge = idx === 0
        ? '<span class="rank-pill rank-1">🥇 #1</span>'
        : (idx === 1 ? '<span class="rank-pill rank-2">🥈 #2</span>' : '<span class="rank-pill rank-3">🥉 #3</span>');
      const statusBadge = isPost
        ? `<span class="badge ${p.hit ? 'badge-green' : 'badge-rose'}">${p.hit ? '✓ WON' : '✗ LOST'}</span>`
        : `<span class="badge badge-blue">⏳ ${p.state === 'in' ? 'LIVE' : 'PENDING'}</span>`;

      rowsHtml.push(`
        <tr class="cal-hist-row" data-date="${dStr}" style="cursor:pointer; ${idx === 0 ? 'border-top:2px solid var(--border);' : ''}">
          <td style="font-weight:700; white-space:nowrap;">${idx === 0 ? dStr : '<span style="color:var(--text-dim); font-size:0.75rem;">↳ same day</span>'}</td>
          <td>${idx === 0 ? daySumBadge : ''}</td>
          <td>${rankBadge}</td>
          <td><span class="badge badge-blue">${spIcon} ${p.league}</span></td>
          <td style="font-weight:600;">${p.match}</td>
          <td style="font-weight:800; color:#fff;">🎯 ${p.pick}</td>
          <td style="font-weight:700; color:var(--primary);">${(p.probability * 100).toFixed(1)}%</td>
          <td style="font-weight:700; color:var(--accent-blue);">${Number(p.odds).toFixed(2)}</td>
          <td style="font-weight:700;">${p.score || '-'}</td>
          <td>${statusBadge}</td>
        </tr>
      `);
    });
  });

  if (rowsHtml.length === 0) {
    tbody.innerHTML = `<tr><td colspan="10" style="text-align:center; padding:2rem; color:var(--text-muted);">No daily picks match the selected filter.</td></tr>`;
    return;
  }

  tbody.innerHTML = rowsHtml.join('');

  tbody.querySelectorAll('.cal-hist-row').forEach(tr => {
    tr.addEventListener('click', (e) => {
      const dt = e.currentTarget.dataset.date;
      if (!dt) return;
      calSelectedDate = dt;
      calSelectedMonth = dt.slice(0, 7);
      renderDailyCalendarView();
      const spot = document.getElementById('calendarSpotlight');
      if (spot) spot.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
}
