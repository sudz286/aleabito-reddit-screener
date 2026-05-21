// Standalone ticker history page
const SYMBOL = window.__SYMBOL__;

async function loadTicker() {
    const page = document.getElementById('ticker-page');
    try {
        const res = await fetch(`/api/tickers/${encodeURIComponent(SYMBOL)}/signals`);
        if (!res.ok) throw new Error(res.status);
        const data = await res.json();
        document.title = `${data.symbol} — Signal Monitor`;
        document.getElementById('nav-meta').textContent = data.name || data.symbol;
        page.innerHTML = buildPage(data);
    } catch {
        page.innerHTML = `<div class="empty-state">Failed to load data for ${SYMBOL}.</div>`;
    }
}

function buildPage(data) {
    const cp = data.current_price != null
        ? `${data.currency || ''} ${fmt(data.current_price)}` : '—';

    // Signals sorted newest-first; oldest price is at the end of the array
    const withPrice = data.signals.filter(s => s.price_at_mention != null);
    const firstPrice = withPrice.length ? withPrice[withPrice.length - 1].price_at_mention : null;
    let overallPctHtml = '';
    if (firstPrice && data.current_price && firstPrice > 0) {
        const pct = (data.current_price - firstPrice) / firstPrice * 100;
        const cls = pct >= 0 ? 'price-pos' : 'price-neg';
        overallPctHtml = `<span class="${cls}" style="font-size:17px;font-weight:600;font-family:'IBM Plex Mono',monospace">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}% since first mention</span>`;
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
            ? `<a class="tweet-link" href="${esc(s.tweet_url)}" target="_blank" rel="noopener noreferrer" style="margin-left:auto;font-family:'IBM Plex Mono',monospace;font-size:11px">View tweet ↗</a>`
            : '';
        return `
        <div class="tsr-row">
            <div class="tsr-meta">
                <span class="sentiment-badge badge-${sentiment}">${sentiment}</span>
                ${s.confidence ? `<span class="confidence-badge">${esc(s.confidence)} confidence</span>` : ''}
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
        <div class="ticker-view-meta" style="flex-wrap:wrap;gap:16px">
            ${data.exchange ? `<span class="t-exchange">${esc(data.exchange)}</span>` : ''}
            <span class="ticker-view-price">${cp}</span>
            ${overallPctHtml}
        </div>
    </div>
    <div class="ticker-view-count">${data.signals.length} mention${data.signals.length === 1 ? '' : 's'}</div>
    <div class="ticker-signals-list">${rows}</div>
    `;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
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

loadTicker();
