// ── State ────────────────────────────────────────────────────────────────────
let signalsPage = 1;
let signalsLoading = false;
let signalsExhausted = false;
let activeSignalId = null;

// Leaderboard sort + filter state
let lbEntries = [];           // raw entries from API
let lbSortKey = 'pct_from_first';
let lbSortDir = 'desc';
let lbTimeframe = 'all';

// ── Boot ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    setupSentinel();
    if (window.location.hash === '#leaderboard') {
        showView('leaderboard');
    } else {
        showView('feed');
    }
});

// ── View switching ────────────────────────────────────────────────────────────
function showView(name) {
    closeTickerOverlay();
    document.querySelectorAll('.view').forEach(v => v.classList.add('hidden'));
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    document.getElementById(`view-${name}`).classList.remove('hidden');
    document.getElementById(`btn-${name}`).classList.add('active');
    window.location.hash = name === 'feed' ? '' : name;

    if (name === 'feed' && signalsPage === 1 && !signalsLoading) {
        loadSignals();
    }
    if (name === 'leaderboard') {
        loadLeaderboard();
    }
}

// ── Feed: signal list ─────────────────────────────────────────────────────────
async function loadSignals() {
    if (signalsLoading || signalsExhausted) return;
    signalsLoading = true;

    const container = document.getElementById('signals-container');
    if (signalsPage === 1) {
        container.innerHTML = '<div class="loading">Loading signals…</div>';
    }

    try {
        const res = await fetch(`/api/signals?page=${signalsPage}&per_page=30`);
        const data = await res.json();

        if (signalsPage === 1) container.innerHTML = '';

        if (data.signals.length === 0 && signalsPage === 1) {
            container.innerHTML = '<div class="empty-state">No finance signals yet.<br>Run <code>python monitor.py</code> to fetch tweets.</div>';
            return;
        }

        data.signals.forEach(s => container.appendChild(renderSignalCard(s)));
        updateNavMeta(data.total);

        if (data.signals.length < data.per_page) {
            signalsExhausted = true;
        } else {
            signalsPage++;
        }
    } catch (e) {
        if (signalsPage === 1) {
            container.innerHTML = '<div class="empty-state">Failed to load signals.</div>';
        }
    } finally {
        signalsLoading = false;
    }
}

function updateNavMeta(total) {
    const el = document.getElementById('nav-meta');
    if (!el) return;
    const t = new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
    el.innerHTML = `<span>${total} signals · </span><span>last sync <b>${t}</b></span>`;
}

function renderSignalCard(signal) {
    const el = document.createElement('div');
    el.className = 'signal-card';
    el.dataset.tweetId = signal.tweet_id;

    const sentiment = (signal.sentiment || 'neutral').toLowerCase();

    // Render chips — show new badge on first mention, but not clickable in feed
    const chips = (signal.tickers || [])
        .slice(0, 6)
        .map(t => {
            const isUnresolved = t.name === '(unresolved)';
            const isNew = t.is_new && !isUnresolved;
            return `<span class="ticker-chip${isUnresolved ? ' unresolved' : ''}${isNew ? ' new-mention' : ''}">${esc(t.symbol)}${isNew ? '<span class="chip-new">new</span>' : ''}</span>`;
        })
        .join('');

    // Thesis-only card; fall back to tweet text only if no thesis available
    const thesisText = signal.thesis_summary || signal.tweet_text || '';

    el.innerHTML = `
        <div class="card-meta">
            <span class="sentiment-badge badge-${sentiment}">${sentiment}</span>
            ${signal.is_reply ? '<span class="reply-tag">↩ reply</span>' : ''}
            <span class="card-time">${fmtDate(signal.published_at)}</span>
        </div>
        <div class="card-thesis">${esc(thesisText)}</div>
        ${signal.market_context ? `<div class="card-market">${esc(signal.market_context)}</div>` : ''}
        ${chips ? `<div class="ticker-chips">${chips}</div>` : ''}
    `;

    el.addEventListener('click', () => selectSignal(signal.tweet_id, el));
    return el;
}

// ── Feed: signal detail ───────────────────────────────────────────────────────
async function selectSignal(tweetId, cardEl) {
    document.querySelectorAll('.signal-card').forEach(c => c.classList.remove('active'));
    if (cardEl) cardEl.classList.add('active');
    activeSignalId = tweetId;

    const pane = document.getElementById('signal-detail');
    pane.innerHTML = '<div class="loading">Loading…</div>';
    pane.classList.add('mobile-open');

    try {
        const res = await fetch(`/api/signals/${tweetId}`);
        if (!res.ok) throw new Error();
        const data = await res.json();
        pane.innerHTML = buildDetail(data);
    } catch {
        pane.innerHTML = '<div class="empty-state">Failed to load signal detail.</div>';
    }
}

function closeMobileDetail() {
    document.getElementById('signal-detail').classList.remove('mobile-open');
}

function buildDetail(d) {
    const sentiment = (d.sentiment || 'neutral').toLowerCase();
    const replyCtx = d.is_reply && d.parent_text
        ? `<div class="detail-reply-ctx">Replying to: ${esc(d.parent_text.slice(0, 240))}${d.parent_text.length > 240 ? '…' : ''}</div>`
        : '';

    const tickerRows = (d.tickers || []).map(buildTickerRow).join('');
    const tickersSection = d.tickers?.length ? `
        <div class="detail-section">
            <div class="detail-label">Tickers · ${d.tickers.length}</div>
            <table class="tickers-table">
                <thead><tr>
                    <th>Symbol</th><th>Name</th><th>Exchange</th>
                    <th class="r">@ Mention</th><th class="r">Current</th><th class="r">Change</th>
                </tr></thead>
                <tbody>${tickerRows}</tbody>
            </table>
        </div>` : '';

    return `
        <button class="mobile-detail-back" onclick="closeMobileDetail()">← All signals</button>
        <div class="detail-header">
            <div class="detail-meta">
                <span class="sentiment-badge badge-${sentiment}">${sentiment}</span>
                ${d.confidence ? `<span class="confidence-badge">${esc(d.confidence)} confidence</span>` : ''}
                <span class="detail-time">${fmtDate(d.published_at)}</span>
            </div>
            ${replyCtx}
        </div>

        ${d.thesis_summary ? `
        <div class="detail-section">
            <div class="detail-label">Thesis</div>
            <div class="detail-text">${esc(d.thesis_summary)}</div>
        </div>` : ''}

        ${d.market_context ? `
        <div class="detail-section">
            <div class="detail-label">Market context</div>
            <div class="market-text">${esc(d.market_context)}</div>
        </div>` : ''}

        ${tickersSection}

        <div class="detail-section">
            <div class="detail-label">Original tweet</div>
            <div class="detail-tweet">${esc(d.tweet_text || '')}</div>
            ${d.tweet_url ? `<a class="tweet-link" href="${d.tweet_url}" target="_blank" rel="noopener noreferrer">View on X ↗</a>` : ''}
        </div>
    `;
}

function buildTickerRow(t) {
    if (t.is_unresolved) {
        return `
        <tr id="trow-${t.id}">
            <td><span class="t-symbol" style="color: var(--mix)">${esc(t.symbol)}</span></td>
            <td colspan="2">
                <span class="unresolved-label">unresolved</span>
                <span class="resolve-wrap">
                    <button class="resolve-btn" onclick="toggleResolveForm(${t.id})">Edit</button>
                    <div id="rform-${t.id}" style="display:none" class="resolve-form">
                        <input class="resolve-input" id="rinput-${t.id}"
                               placeholder="Search company or symbol…"
                               oninput="debouncedSearch(${t.id}, this.value)" />
                        <div class="search-results" id="rresults-${t.id}" style="display:none"></div>
                    </div>
                </span>
            </td>
            <td class="r">—</td><td class="r">—</td><td class="r">—</td>
        </tr>`;
    }

    const mp  = t.price_at_mention != null ? `${t.currency || ''} ${fmt(t.price_at_mention)}` : '—';
    const cp  = t.current_price   != null ? `${t.currency || ''} ${fmt(t.current_price)}`   : '—';
    let pctHtml = '—';
    if (t.pct_change != null) {
        const cls   = t.pct_change >= 0 ? 'price-pos' : 'price-neg';
        const arrow = t.pct_change >= 0 ? '▲' : '▼';
        pctHtml = `<span class="${cls}">${arrow} ${Math.abs(t.pct_change).toFixed(2)}%</span>`;
    }

    return `
    <tr>
        <td><span class="t-symbol">${esc(t.resolved_symbol || t.symbol)}</span></td>
        <td><span class="t-name">${esc(t.name || '')}</span></td>
        <td><span class="t-exchange">${esc(t.exchange || '')}</span></td>
        <td class="r" style="font-family: 'IBM Plex Mono', monospace; color: var(--ink-2)">${mp}</td>
        <td class="r" style="font-family: 'IBM Plex Mono', monospace; color: var(--ink); font-weight: 600">${cp}</td>
        <td class="r">${pctHtml}</td>
    </tr>`;
}

// ── Resolve unresolved tickers ────────────────────────────────────────────────
const searchTimers = {};

function toggleResolveForm(id) {
    const form = document.getElementById(`rform-${id}`);
    const open = form.style.display !== 'none';
    form.style.display = open ? 'none' : 'block';
    if (!open) document.getElementById(`rinput-${id}`).focus();
}

function debouncedSearch(id, q) {
    clearTimeout(searchTimers[id]);
    const results = document.getElementById(`rresults-${id}`);
    if (!q || q.length < 2) { results.style.display = 'none'; return; }
    searchTimers[id] = setTimeout(() => doSearch(id, q), 350);
}

async function doSearch(id, q) {
    const results = document.getElementById(`rresults-${id}`);
    results.innerHTML = '<div style="padding:10px 14px;color:var(--muted);font-size:12.5px">Searching…</div>';
    results.style.display = 'block';
    try {
        const res  = await fetch(`/api/tickers/search?q=${encodeURIComponent(q)}`);
        const data = await res.json();
        if (!data.results.length) {
            results.innerHTML = '<div style="padding:10px 14px;color:var(--muted);font-size:12.5px">No results</div>';
            return;
        }
        results.innerHTML = data.results.map(r => `
            <div class="sr-item" onclick="pickResolution(${id}, '${escAttr(r.symbol)}')">
                <span class="sr-symbol">${esc(r.symbol)}</span>
                <span class="sr-name">${esc(r.name)}</span>
                <span class="sr-exchange">${esc(r.exchange)}</span>
            </div>`).join('');
    } catch {
        results.innerHTML = '<div style="padding:10px 14px;color:var(--bear);font-size:12.5px">Search failed</div>';
    }
}

async function pickResolution(id, symbol) {
    const results = document.getElementById(`rresults-${id}`);
    results.innerHTML = '<div style="padding:10px 14px;color:var(--muted);font-size:12.5px">Resolving…</div>';
    try {
        const res = await fetch(`/api/tickers/${id}/resolve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ resolved_symbol: symbol }),
        });
        if (!res.ok) {
            const err = await res.json();
            results.innerHTML = `<div style="padding:10px 14px;color:var(--bear);font-size:12.5px">Error: ${esc(err.detail)}</div>`;
            return;
        }
        showToast(`Resolved → ${symbol}`);
        // Reload the full detail so deduped rows are removed cleanly
        const activeCard = document.querySelector(`[data-tweet-id="${activeSignalId}"]`);
        await selectSignal(activeSignalId, activeCard);
    } catch {
        results.innerHTML = '<div style="padding:10px 14px;color:var(--bear);font-size:12.5px">Failed</div>';
    }
}

// ── Ticker overlay (leaderboard drill-down) ───────────────────────────────────
function overlayEscHandler(e) { if (e.key === 'Escape') closeTickerOverlay(); }

async function showTickerDetail(symbol) {
    const overlay  = document.getElementById('ticker-overlay');
    const body     = document.getElementById('ticker-overlay-body');
    const symEl    = document.getElementById('overlay-sym');
    symEl.textContent = symbol;
    body.innerHTML = '<div class="loading">Loading…</div>';
    overlay.style.display = 'flex';
    document.addEventListener('keydown', overlayEscHandler);
    try {
        const res = await fetch(`/api/tickers/${encodeURIComponent(symbol)}/signals`);
        if (!res.ok) throw new Error();
        const data = await res.json();
        symEl.textContent = data.name ? `${data.symbol} · ${data.name}` : data.symbol;
        body.innerHTML = buildOverlayContent(data);
    } catch {
        body.innerHTML = '<div class="empty-state">Failed to load ticker history.</div>';
    }
}

function closeTickerOverlay() {
    document.getElementById('ticker-overlay').style.display = 'none';
    document.removeEventListener('keydown', overlayEscHandler);
}

function buildOverlayContent(data) {
    const cp = data.current_price != null
        ? `${data.currency || ''} ${fmt(data.current_price)}` : '—';

    const withPrice = data.signals.filter(s => s.price_at_mention != null);
    const firstPrice = withPrice.length ? withPrice[withPrice.length - 1].price_at_mention : null;
    let overallPctHtml = '';
    if (firstPrice && data.current_price && firstPrice > 0) {
        const pct = (data.current_price - firstPrice) / firstPrice * 100;
        const cls = pct >= 0 ? 'price-pos' : 'price-neg';
        overallPctHtml = `<span class="${cls}" style="font-size:14px;font-weight:600;font-family:'IBM Plex Mono',monospace">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}% since first mention</span>`;
    }

    const rows = data.signals.map(s => {
        const sentiment = (s.sentiment || 'neutral').toLowerCase();
        const mp = s.price_at_mention != null
            ? `${s.currency || ''} ${fmt(s.price_at_mention)}` : '—';
        let pctHtml = '';
        if (s.price_at_mention && data.current_price && s.price_at_mention > 0) {
            const pct = (data.current_price - s.price_at_mention) / s.price_at_mention * 100;
            const cls = pct >= 0 ? 'price-pos' : 'price-neg';
            pctHtml = `<span class="${cls}">${pct >= 0 ? '▲' : '▼'} ${Math.abs(pct).toFixed(2)}%</span>`;
        }
        const viewLink = s.tweet_url
            ? `<a class="tweet-link" href="${esc(s.tweet_url)}" target="_blank" rel="noopener noreferrer" style="margin-left:auto;font-size:11.5px">View tweet ↗</a>`
            : '';
        return `
        <div class="tsr-row">
            <div class="tsr-meta">
                <span class="sentiment-badge badge-${sentiment}">${sentiment}</span>
                ${s.confidence ? `<span class="confidence-badge">${esc(s.confidence)}</span>` : ''}
                <span class="tsr-time">${fmtDate(s.published_at)}</span>
            </div>
            ${s.thesis_summary ? `<div class="tsr-thesis">${esc(s.thesis_summary)}</div>` : ''}
            <div class="tsr-price">
                <span>@ mention: <b>${mp}</b></span>
                ${pctHtml}
                ${viewLink}
            </div>
        </div>`;
    }).join('');

    return `
    <div class="ticker-view-header">
        <div class="ticker-view-sym">${esc(data.symbol)}</div>
        <div class="ticker-view-name">${esc(data.name || '')}</div>
        <div class="ticker-view-meta" style="flex-wrap:wrap;gap:14px;margin-top:10px">
            ${data.exchange ? `<span class="t-exchange">${esc(data.exchange)}</span>` : ''}
            <span class="ticker-view-price">${cp}</span>
            ${overallPctHtml}
        </div>
    </div>
    <div class="ticker-view-count" style="margin-top:4px">${data.signals.length} mention${data.signals.length === 1 ? '' : 's'}</div>
    <div class="ticker-signals-list" style="margin-top:8px">${rows}</div>
    `;
}

// ── Leaderboard ───────────────────────────────────────────────────────────────
async function loadLeaderboard() {
    const container = document.getElementById('leaderboard-container');
    container.innerHTML = '<div class="loading">Loading leaderboard…</div>';
    try {
        const res  = await fetch('/api/leaderboard');
        const data = await res.json();
        lbEntries = data.entries || [];
        if (!lbEntries.length) {
            container.innerHTML = '<div class="empty-state">No resolved tickers yet.</div>';
            return;
        }
        renderLeaderboard();
    } catch {
        container.innerHTML = '<div class="empty-state">Failed to load leaderboard.</div>';
    }
}

function filteredEntries() {
    // Timeframe filter — based on last_posted
    if (lbTimeframe === 'all') return lbEntries.slice();
    const now = Date.now();
    const cutoff = {
        '24h': 24 * 3600 * 1000,
        '7d':  7 * 24 * 3600 * 1000,
        '30d': 30 * 24 * 3600 * 1000,
    }[lbTimeframe];
    return lbEntries.filter(e => {
        const t = e.last_posted ? new Date(e.last_posted).getTime() : 0;
        return now - t <= cutoff;
    });
}

function sortedEntries() {
    const arr = filteredEntries();
    const k = lbSortKey, dir = lbSortDir === 'desc' ? -1 : 1;
    arr.sort((a, b) => {
        let va = a[k], vb = b[k];
        if (va == null && vb == null) return 0;
        if (va == null) return 1;
        if (vb == null) return -1;
        if (typeof va === 'number') return (va - vb) * dir;
        return String(va).localeCompare(String(vb)) * dir;
    });
    return arr;
}

function renderLeaderboard() {
    const container = document.getElementById('leaderboard-container');
    const entries = sortedEntries();

    const kpisHtml = renderKpis(entries);
    const podiumHtml = renderPodium(entries);
    const tableHtml = renderLbTable(entries);

    container.innerHTML = `
        ${kpisHtml}
        ${podiumHtml}
        ${tableHtml}
    `;

    // Wire up sort headers
    container.querySelectorAll('.lb-table th.sortable').forEach(th => {
        th.addEventListener('click', () => {
            const k = th.dataset.sortkey;
            if (lbSortKey === k) lbSortDir = lbSortDir === 'desc' ? 'asc' : 'desc';
            else { lbSortKey = k; lbSortDir = 'desc'; }
            renderLeaderboard();
        });
    });

    // Wire up timeframe filter
    container.querySelectorAll('.lb-filter').forEach(b => {
        b.addEventListener('click', () => {
            lbTimeframe = b.dataset.tf;
            renderLeaderboard();
        });
    });
}

function renderKpis(entries) {
    const tracked = entries.length;
    const totalMentions = entries.reduce((s, e) => s + (e.mention_count || 0), 0);
    const withGain = entries.filter(e => e.pct_from_first != null);
    const avg = withGain.length
        ? withGain.reduce((s, e) => s + e.pct_from_first, 0) / withGain.length
        : 0;
    const winners = withGain.filter(e => e.pct_from_first > 0).length;
    const winRate = withGain.length ? (winners / withGain.length) * 100 : 0;
    const sorted = [...withGain].sort((a, b) => b.pct_from_first - a.pct_from_first);
    const best  = sorted[0];
    const worst = sorted[sorted.length - 1];

    return `
    <div class="lb-header">
        <div>
            <h2>Performance scorecard</h2>
            <div class="lb-sub">How every ticker the account has flagged has performed since first mention.</div>
        </div>
        <div class="lb-filters">
            ${['24h','7d','30d','all'].map(tf => `
                <button class="lb-filter ${tf === lbTimeframe ? 'active' : ''}" data-tf="${tf}">
                    ${tf === 'all' ? 'all-time' : tf}
                </button>
            `).join('')}
        </div>
    </div>

    <div class="kpis">
        <div class="kpi">
            <div class="kpi-label">Tickers tracked</div>
            <div class="kpi-value">${tracked}</div>
            <div class="kpi-meta"><b>${totalMentions}</b> total mentions across signals</div>
        </div>
        <div class="kpi">
            <div class="kpi-label">Win rate</div>
            <div class="kpi-value ${winRate >= 50 ? 'bull' : 'bear'}">${winRate.toFixed(0)}%</div>
            <div class="kpi-meta">picks up since first mention</div>
        </div>
        <div class="kpi">
            <div class="kpi-label">Avg return</div>
            <div class="kpi-value ${avg >= 0 ? 'bull' : 'bear'}">${avg >= 0 ? '+' : ''}${avg.toFixed(1)}%</div>
            <div class="kpi-meta">since first mention, equal-weighted</div>
        </div>
        <div class="kpi">
            <div class="kpi-label">Best · worst</div>
            <div class="kpi-value">
                ${best ? `<span style="color: var(--bull)">+${best.pct_from_first.toFixed(0)}%</span>` : '—'}
                <span class="kpi-bw-sep">/</span>
                ${worst ? `<span style="color: var(--bear)">${worst.pct_from_first.toFixed(0)}%</span>` : '—'}
            </div>
            <div class="kpi-meta">
                ${best ? `<b>${esc(best.resolved_symbol)}</b>` : '—'} ·
                ${worst ? `<b>${esc(worst.resolved_symbol)}</b>` : '—'}
            </div>
        </div>
    </div>
    `;
}

function renderPodium(entries) {
    const withGain = entries
        .filter(e => e.pct_from_first != null)
        .sort((a, b) => b.pct_from_first - a.pct_from_first)
        .slice(0, 3);

    if (withGain.length === 0) return '';

    return `
    <div class="podium">
        ${withGain.map((e, i) => {
            const dir = e.pct_from_first > 0.05 ? 'up' : e.pct_from_first < -0.05 ? 'down' : 'flat';
            return `
            <div class="podium-card">
                <span class="podium-rank">0${i + 1}</span>
                <div class="podium-sym podium-sym-link" onclick="showTickerDetail('${escAttr(e.resolved_symbol)}')">${esc(e.resolved_symbol)}</div>
                <div class="podium-name">${esc(e.name || '')}</div>
                <div class="podium-bottom">
                    <span class="podium-gain ${dir}">${e.pct_from_first >= 0 ? '+' : ''}${e.pct_from_first.toFixed(1)}%</span>
                    ${sparkSvg(e, dir)}
                </div>
                <div class="podium-meta">
                    ${e.currency || ''} ${fmt(e.price_at_first)} → ${fmt(e.current_price)} · ${e.mention_count} mention${e.mention_count === 1 ? '' : 's'}
                </div>
            </div>
            `;
        }).join('')}
    </div>
    `;
}

function renderLbTable(entries) {
    // Max absolute gain — for bar normalization
    const maxAbsFirst = Math.max(1, ...entries.map(e => Math.abs(e.pct_from_first || 0)));
    const maxAbsLast  = Math.max(1, ...entries.map(e => Math.abs(e.pct_from_last  || 0)));

    // Map sentiment hint per row (best-effort: derive from gain direction since
    // the leaderboard API doesn't return sentiment per ticker yet).
    const sentClass = (e) => {
        if (e.pct_from_first == null) return 'sent-neutral';
        if (e.pct_from_first >  0.5) return 'sent-bullish';
        if (e.pct_from_first < -0.5) return 'sent-bearish';
        return 'sent-neutral';
    };

    const rows = entries.map((e, i) => {
        const fp  = e.price_at_first != null ? `${e.currency || ''} ${fmt(e.price_at_first)}` : '—';
        const lp  = e.price_at_last  != null ? `${e.currency || ''} ${fmt(e.price_at_last)}`  : '—';
        const cp  = e.current_price  != null ? `${e.currency || ''} ${fmt(e.current_price)}`  : '—';
        const dirFirst = e.pct_from_first > 0.05 ? 'up' : e.pct_from_first < -0.05 ? 'down' : 'flat';
        return `
        <tr>
            <td class="lb-rank">${String(i + 1).padStart(2, '0')}</td>
            <td>
                <div class="lb-symbol lb-symbol-link" onclick="showTickerDetail('${escAttr(e.resolved_symbol)}')">
                    <span class="sent-dot ${sentClass(e)}"></span>
                    <span class="lb-sym-text">${esc(e.resolved_symbol)}</span>
                </div>
                <div class="lb-name" title="${escAttr(e.name)}">${esc(e.name || '')}</div>
            </td>
            <td>${sparkSvg(e, dirFirst)}</td>
            <td class="lb-price">${fp}</td>
            <td class="lb-price">${lp}</td>
            <td class="lb-price current">${cp}</td>
            <td>${gainCell(e.pct_from_first, maxAbsFirst)}</td>
            <td>${gainCell(e.pct_from_last,  maxAbsLast)}</td>
            <td class="lb-count">${e.mention_count}×</td>
            <td class="lb-date">${fmtDateShort(e.first_posted)}</td>
        </tr>`;
    }).join('');

    const sortInd = (k) =>
        lbSortKey === k
            ? `<span class="sort-ind">${lbSortDir === 'desc' ? '↓' : '↑'}</span>`
            : '<span class="sort-ind">↕</span>';
    const sortClass = (k) => 'sortable' + (lbSortKey === k ? ' active' : '');

    return `
    <div class="lb-table-wrap">
        <table class="lb-table">
            <thead><tr>
                <th>#</th>
                <th class="${sortClass('resolved_symbol')}" data-sortkey="resolved_symbol">Ticker ${sortInd('resolved_symbol')}</th>
                <th>Trend</th>
                <th class="r ${sortClass('price_at_first')}" data-sortkey="price_at_first">@ First ${sortInd('price_at_first')}</th>
                <th class="r ${sortClass('price_at_last')}"  data-sortkey="price_at_last">@ Last ${sortInd('price_at_last')}</th>
                <th class="r ${sortClass('current_price')}"  data-sortkey="current_price">Current ${sortInd('current_price')}</th>
                <th class="r ${sortClass('pct_from_first')}" data-sortkey="pct_from_first">From first ${sortInd('pct_from_first')}</th>
                <th class="r ${sortClass('pct_from_last')}"  data-sortkey="pct_from_last">From last ${sortInd('pct_from_last')}</th>
                <th class="r ${sortClass('mention_count')}"  data-sortkey="mention_count">Mentions ${sortInd('mention_count')}</th>
                <th class="r ${sortClass('first_posted')}"   data-sortkey="first_posted">First mentioned ${sortInd('first_posted')}</th>
            </tr></thead>
            <tbody>${rows}</tbody>
        </table>
    </div>
    `;
}

function gainCell(pct, maxAbs) {
    if (pct == null) return '<span class="gain-num flat">—</span>';
    const dir = pct > 0.05 ? 'up' : pct < -0.05 ? 'down' : 'flat';
    const width = (Math.min(Math.abs(pct) / maxAbs, 1) * 50).toFixed(1); // half-width, center anchored
    return `
    <div class="gain-cell">
        <span class="gain-num ${dir}">${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%</span>
        <div class="gain-bar">
            <div class="gain-bar-mid"></div>
            ${dir === 'up'   ? `<div class="gain-bar-fill up"   style="width: ${width}%"></div>` : ''}
            ${dir === 'down' ? `<div class="gain-bar-fill down" style="width: ${width}%"></div>` : ''}
        </div>
    </div>`;
}

// 3-point sparkline from first/last/current prices
function sparkSvg(e, dir) {
    const pts = [e.price_at_first, e.price_at_last, e.current_price].filter(v => v != null);
    if (pts.length < 2) return '<svg class="spark flat" viewBox="0 0 90 28"></svg>';
    const w = 90, h = 28, pad = 3;
    const min = Math.min(...pts), max = Math.max(...pts);
    const range = max - min || 1;
    const stepX = (w - pad * 2) / (pts.length - 1);
    const coords = pts.map((v, i) => [
        pad + i * stepX,
        pad + (h - pad * 2) * (1 - (v - min) / range)
    ]);
    const path = coords.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
    const lastX = (pad + (pts.length - 1) * stepX).toFixed(1);
    const area = path + ` L${lastX},${h - pad} L${pad},${h - pad} Z`;
    const last = coords[coords.length - 1];
    return `
    <svg class="spark ${dir}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
        <path class="area" d="${area}"></path>
        <path class="line" d="${path}"></path>
        <circle class="dot" cx="${last[0]}" cy="${last[1]}" r="2.5"></circle>
    </svg>`;
}

// ── Manual price refresh ──────────────────────────────────────────────────────
async function manualRefresh() {
    const btn = document.getElementById('refresh-btn');
    btn.textContent = 'Refreshing…';
    btn.disabled = true;
    showToast('Refreshing prices…');
    try {
        await fetch('/api/prices/refresh', { method: 'POST' });
        const activeView = document.querySelector('.view:not(.hidden)');
        if (activeView.id === 'view-leaderboard') {
            await loadLeaderboard();
        } else if (activeSignalId) {
            const card = document.querySelector(`.signal-card[data-tweet-id="${activeSignalId}"]`);
            if (card) await selectSignal(activeSignalId, card);
        }
        showToast('Prices updated');
    } catch {
        showToast('Refresh failed');
    } finally {
        btn.textContent = 'Refresh prices';
        btn.disabled = false;
    }
}

// ── Toast ─────────────────────────────────────────────────────────────────────
let toastTimer = null;
function showToast(msg) {
    let el = document.getElementById('toast');
    if (!el) {
        el = document.createElement('div');
        el.id = 'toast';
        el.className = 'toast';
        document.body.appendChild(el);
    }
    el.textContent = msg;
    requestAnimationFrame(() => el.classList.add('show'));
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
}

// ── Infinite scroll ───────────────────────────────────────────────────────────
function setupSentinel() {
    const sentinel = document.getElementById('signals-sentinel');
    new IntersectionObserver(entries => {
        if (entries[0].isIntersecting) loadSignals();
    }, { threshold: 0.1 }).observe(sentinel);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function escAttr(s) {
    return String(s ?? '').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function fmt(n) {
    if (n == null) return '—';
    return Number(n).toLocaleString(undefined, { maximumFractionDigits: 4 });
}
function fmtDate(iso) {
    if (!iso) return '';
    try {
        return new Date(iso).toLocaleString('en-US', {
            month: 'short', day: 'numeric', year: 'numeric',
            hour: '2-digit', minute: '2-digit',
        });
    } catch { return iso; }
}
function fmtDateShort(iso) {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleDateString('en-US', {
            month: 'short', day: 'numeric',
        });
    } catch { return iso; }
}
