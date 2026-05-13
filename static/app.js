// ── State ────────────────────────────────────────────────────────────────────
let signalsPage = 1;
let signalsLoading = false;
let signalsExhausted = false;
let activeSignalId = null;

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

function renderSignalCard(signal) {
    const el = document.createElement('div');
    el.className = 'signal-card';
    el.dataset.tweetId = signal.tweet_id;

    const sentiment = (signal.sentiment || 'neutral').toLowerCase();
    const chips = (signal.tickers || [])
        .filter(t => t.name !== '(unresolved)')
        .slice(0, 5)
        .map(t => `<span class="ticker-chip">${esc(t.symbol)}</span>`)
        .join('');

    el.innerHTML = `
        <div class="card-meta">
            <span class="sentiment-badge badge-${sentiment}">${sentiment}</span>
            ${signal.is_reply ? '<span class="reply-tag">↩ reply</span>' : ''}
            <span class="card-time">${fmtDate(signal.published_at)}</span>
        </div>
        <div class="card-thesis">${esc(signal.thesis_summary || signal.tweet_text || '')}</div>
        ${signal.market_context ? `<div class="card-market">${esc(signal.market_context)}</div>` : ''}
        ${chips ? `<div class="ticker-chips">${chips}</div>` : ''}
    `;

    el.addEventListener('click', () => selectSignal(signal.tweet_id, el));
    return el;
}

// ── Feed: signal detail ───────────────────────────────────────────────────────
async function selectSignal(tweetId, cardEl) {
    document.querySelectorAll('.signal-card').forEach(c => c.classList.remove('active'));
    cardEl.classList.add('active');
    activeSignalId = tweetId;

    const pane = document.getElementById('signal-detail');
    pane.innerHTML = '<div class="loading">Loading…</div>';

    try {
        const res = await fetch(`/api/signals/${tweetId}`);
        if (!res.ok) throw new Error();
        const data = await res.json();
        pane.innerHTML = buildDetail(data);
    } catch {
        pane.innerHTML = '<div class="empty-state">Failed to load signal detail.</div>';
    }
}

function buildDetail(d) {
    const sentiment = (d.sentiment || 'neutral').toLowerCase();
    const replyCtx = d.is_reply && d.parent_text
        ? `<div class="detail-reply-ctx">Replying to: ${esc(d.parent_text.slice(0, 200))}${d.parent_text.length > 200 ? '…' : ''}</div>`
        : '';

    const tickerRows = (d.tickers || []).map(buildTickerRow).join('');
    const tickersSection = d.tickers?.length ? `
        <div class="detail-section">
            <div class="detail-label">Tickers</div>
            <table class="tickers-table">
                <thead><tr>
                    <th>Symbol</th><th>Name</th><th>Exchange</th>
                    <th>@ Mention</th><th>Current</th><th>Change</th>
                </tr></thead>
                <tbody>${tickerRows}</tbody>
            </table>
        </div>` : '';

    return `
        <div class="detail-header">
            <div class="detail-meta">
                <span class="sentiment-badge badge-${sentiment}">${sentiment}</span>
                ${d.confidence ? `<span class="confidence-badge">${d.confidence} confidence</span>` : ''}
                <span class="detail-time">${fmtDate(d.published_at)}</span>
            </div>
            ${replyCtx}
            <div class="detail-tweet">${esc(d.tweet_text || '')}</div>
            <a class="tweet-link" href="${esc(d.tweet_url)}" target="_blank" rel="noopener">↗ View on X</a>
        </div>
        ${d.thesis_summary ? `
        <div class="detail-section">
            <div class="detail-label">Thesis</div>
            <div class="detail-text">${esc(d.thesis_summary)}</div>
        </div>` : ''}
        ${d.market_context ? `
        <div class="detail-section">
            <div class="detail-label">Market</div>
            <div class="market-text">${esc(d.market_context)}</div>
        </div>` : ''}
        ${tickersSection}
    `;
}

function buildTickerRow(t) {
    if (t.is_unresolved) {
        return `
        <tr id="trow-${t.id}">
            <td><span class="t-symbol">${esc(t.symbol)}</span></td>
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
            <td>—</td><td>—</td><td>—</td>
        </tr>`;
    }

    const mp  = t.price_at_mention != null ? `${t.currency || ''} ${fmt(t.price_at_mention)}` : '—';
    const cp  = t.current_price   != null ? `${t.currency || ''} ${fmt(t.current_price)}`   : '—';
    let pctHtml = '—';
    if (t.pct_change != null) {
        const cls   = t.pct_change >= 0 ? 'price-pos' : 'price-neg';
        const arrow = t.pct_change >= 0 ? '↑' : '↓';
        pctHtml = `<span class="${cls}">${arrow} ${Math.abs(t.pct_change).toFixed(2)}%</span>`;
    }

    return `
    <tr>
        <td><span class="t-symbol">${esc(t.resolved_symbol || t.symbol)}</span></td>
        <td><span class="t-name">${esc(t.name || '')}</span></td>
        <td><span class="t-exchange">${esc(t.exchange || '')}</span></td>
        <td>${mp}</td>
        <td>${cp}</td>
        <td>${pctHtml}</td>
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
    results.innerHTML = '<div style="padding:8px 12px;color:var(--muted)">Searching…</div>';
    results.style.display = 'block';
    try {
        const res  = await fetch(`/api/tickers/search?q=${encodeURIComponent(q)}`);
        const data = await res.json();
        if (!data.results.length) {
            results.innerHTML = '<div style="padding:8px 12px;color:var(--muted)">No results</div>';
            return;
        }
        results.innerHTML = data.results.map(r => `
            <div class="sr-item" onclick="pickResolution(${id}, '${escAttr(r.symbol)}')">
                <span class="sr-symbol">${esc(r.symbol)}</span>
                <span class="sr-name">${esc(r.name)}</span>
                <span class="sr-exchange">${esc(r.exchange)}</span>
            </div>`).join('');
    } catch {
        results.innerHTML = '<div style="padding:8px 12px;color:var(--muted)">Search failed</div>';
    }
}

async function pickResolution(id, symbol) {
    const results = document.getElementById(`rresults-${id}`);
    results.innerHTML = '<div style="padding:8px 12px;color:var(--muted)">Resolving…</div>';
    try {
        const res = await fetch(`/api/tickers/${id}/resolve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ resolved_symbol: symbol }),
        });
        if (!res.ok) {
            const err = await res.json();
            results.innerHTML = `<div style="padding:8px 12px;color:var(--red)">Error: ${esc(err.detail)}</div>`;
            return;
        }
        const data = await res.json();
        document.getElementById(`trow-${id}`).outerHTML = buildTickerRow({
            id,
            symbol: data.resolved_symbol,
            resolved_symbol: data.resolved_symbol,
            name: data.name,
            exchange: '',
            currency: data.currency || '',
            price_at_mention: data.price,
            current_price: data.price,
            pct_change: 0,
            is_unresolved: false,
        });
    } catch {
        results.innerHTML = '<div style="padding:8px 12px;color:var(--red)">Failed</div>';
    }
}

// ── Leaderboard ───────────────────────────────────────────────────────────────
async function loadLeaderboard() {
    const container = document.getElementById('leaderboard-container');
    container.innerHTML = '<div class="loading">Loading leaderboard…</div>';
    try {
        const res  = await fetch('/api/leaderboard');
        const data = await res.json();
        if (!data.entries.length) {
            container.innerHTML = '<div class="empty-state">No resolved tickers yet.</div>';
            return;
        }
        container.innerHTML = buildLeaderboard(data.entries);
    } catch {
        container.innerHTML = '<div class="empty-state">Failed to load leaderboard.</div>';
    }
}

function buildLeaderboard(entries) {
    const rows = entries.map((e, i) => {
        const fp = e.price_at_first != null ? `${e.currency} ${fmt(e.price_at_first)}` : '—';
        const lp = e.price_at_last  != null ? `${e.currency} ${fmt(e.price_at_last)}`  : '—';
        const cp = e.current_price  != null ? `${e.currency} ${fmt(e.current_price)}`  : '—';
        return `
        <tr>
            <td class="lb-rank">${i + 1}</td>
            <td class="lb-symbol">${esc(e.resolved_symbol)}</td>
            <td class="lb-name" title="${escAttr(e.name)}">${esc(e.name)}</td>
            <td class="lb-date">${fmtDateShort(e.first_posted)}</td>
            <td class="lb-date">${fmtDateShort(e.last_posted)}</td>
            <td class="lb-price">${fp}</td>
            <td class="lb-price">${lp}</td>
            <td class="lb-price">${cp}</td>
            <td>${pctCell(e.pct_from_first)}</td>
            <td>${pctCell(e.pct_from_last)}</td>
            <td class="lb-count">${e.mention_count}×</td>
        </tr>`;
    }).join('');

    return `
    <table class="lb-table">
        <thead><tr>
            <th>#</th><th>Ticker</th><th>Name</th>
            <th>First Mentioned</th><th>Last Mentioned</th>
            <th>@ First</th><th>@ Last</th><th>Current</th>
            <th>From First ↕</th><th>From Last ↕</th><th>Mentions</th>
        </tr></thead>
        <tbody>${rows}</tbody>
    </table>`;
}

function pctCell(pct) {
    if (pct == null) return '<span class="pct-null">—</span>';
    const cls   = pct >= 0 ? 'pct-pos' : 'pct-neg';
    const arrow = pct >= 0 ? '↑' : '↓';
    return `<span class="${cls}">${arrow} ${Math.abs(pct).toFixed(2)}%</span>`;
}

// ── Manual price refresh ──────────────────────────────────────────────────────
async function manualRefresh() {
    const btn = document.getElementById('refresh-btn');
    btn.textContent = '↻ Refreshing…';
    btn.disabled = true;
    try {
        await fetch('/api/prices/refresh', { method: 'POST' });
        // Reload current active data
        const activeView = document.querySelector('.view:not(.hidden)');
        if (activeView.id === 'view-leaderboard') {
            loadLeaderboard();
        } else if (activeSignalId) {
            const card = document.querySelector(`.signal-card[data-tweet-id="${activeSignalId}"]`);
            if (card) selectSignal(activeSignalId, card);
        }
    } finally {
        btn.textContent = '↻ Refresh Prices';
        btn.disabled = false;
    }
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
            month: 'short', day: 'numeric', year: 'numeric',
        });
    } catch { return iso; }
}
