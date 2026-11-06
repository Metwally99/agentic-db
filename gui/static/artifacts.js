/* Render helpers for the per-turn artifact bag from /api/chat.
 *
 * The chat handler in app.js / manager.js calls
 *   ArtifactRenderers.renderAll(data.artifacts, agentWrapper)
 * after appending the agent's textual reply. Anything in `artifacts` that
 * is null is skipped. Rendering order: explain -> dry_run -> analytics -> chart.
 */
(function () {
    const findContent = (parent) =>
        (parent && parent.querySelector('.max-w-full.space-y-4')) || document.getElementById('chatDisplay');

    const card = (innerHTML) => {
        const el = document.createElement('div');
        el.innerHTML = `<div class="glass-card no-borders rounded-2xl px-5 py-4 inner-glow mt-3 w-full">${innerHTML}</div>`;
        return el.firstElementChild;
    };

    const escapeHtml = (s) => String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

    function renderChart(spec, parent) {
        if (!spec || (!spec.vega_lite && !spec.mark)) return;
        const vegaSpec = spec.vega_lite || spec;
        // Force a white mark color across every chart unless the spec
        // explicitly encoded a color channel. Keeps the whole UI on the
        // monochrome palette.
        if (!vegaSpec.encoding || !vegaSpec.encoding.color) {
            const mark = vegaSpec.mark;
            if (typeof mark === 'string') {
                vegaSpec.mark = { type: mark, color: '#ffffff' };
            } else if (mark && typeof mark === 'object' && mark.color == null) {
                mark.color = '#ffffff';
            }
        }
        const wrapper = card('<div class="text-label-sm font-label-sm text-secondary mb-2">CHART</div><div class="chart-mount" style="width:100%;min-height:280px"></div>');
        findContent(parent).appendChild(wrapper);
        const mount = wrapper.querySelector('.chart-mount');
        if (typeof vegaEmbed === 'undefined') {
            mount.innerHTML = '<p class="text-error">vega-embed not loaded</p>';
            return;
        }
        vegaEmbed(mount, vegaSpec, { actions: false, theme: 'dark' })
            .catch((err) => { mount.innerHTML = `<p class="text-error">Chart error: ${escapeHtml(err.message || err)}</p>`; });
    }

    function renderExplain(payload, parent) {
        if (!payload || !payload.plan) return;
        const rows = payload.plan.map((row, idx) => {
            const cells = Object.entries(row).map(([k, v]) =>
                `<span class="text-outline">${escapeHtml(k)}=</span>${escapeHtml(v)}`).join(' &nbsp; ');
            return `<li class="font-label-sm"><span class="text-primary">${idx + 1}.</span> ${cells}</li>`;
        }).join('');
        const raw = payload.raw ? `<details class="mt-2"><summary class="text-label-sm text-outline cursor-pointer">raw plan</summary><pre class="text-label-sm text-outline mt-2 whitespace-pre-wrap">${escapeHtml(payload.raw)}</pre></details>` : '';
        const html = `
            <div class="text-label-sm font-label-sm text-primary mb-2">QUERY PLAN</div>
            <ol class="space-y-1">${rows}</ol>
            ${raw}
        `;
        findContent(parent).appendChild(card(html));
    }

    function renderDryRun(payload, parent) {
        if (!payload || payload.affected_count == null) return;
        const cols = payload.sample_columns || [];
        const rows = payload.sample_rows || [];
        const tableHtml = cols.length ? `
            <div class="overflow-x-auto mt-3 rounded-lg border border-white/10">
                <table class="w-full text-left border-collapse">
                    <thead><tr class="bg-surface-container-highest/40">
                        ${cols.map(c => `<th class="px-3 py-2 text-label-sm font-label-sm text-primary">${escapeHtml(c)}</th>`).join('')}
                    </tr></thead>
                    <tbody>${rows.map((r, i) => `
                        <tr class="${i % 2 === 0 ? 'bg-white/5' : ''}">
                            ${r.map(v => `<td class="px-3 py-2 text-label-sm text-on-surface-variant">${escapeHtml(v == null ? 'NULL' : v)}</td>`).join('')}
                        </tr>`).join('')}
                    </tbody>
                </table>
            </div>` : '';
        const callout = `
            <div class="px-4 py-3 rounded-lg bg-error-container/30 border border-error/30 flex items-center gap-2">
                <span class="material-symbols-outlined text-error" data-icon="warning">warning</span>
                <span class="text-label-md font-label-md text-error">This will affect <strong>${payload.affected_count}</strong> row${payload.affected_count === 1 ? '' : 's'} in <code>${escapeHtml(payload.table || '?')}</code>.</span>
            </div>`;
        findContent(parent).appendChild(card(`<div class="text-label-sm font-label-sm text-error mb-2">DRY RUN</div>${callout}${tableHtml}`));
    }

    function renderAnalytics(payload, parent) {
        if (!payload || !payload.kind || !payload.data) return;
        if (payload.kind === 'trend') return renderTrend(payload.data, parent);
        if (payload.kind === 'compare') return renderCompare(payload.data, parent);
        if (payload.kind === 'top_n') return renderTopN(payload.data, parent);
        if (payload.kind === 'distribution') return renderDistribution(payload.data, parent);
    }

    function renderDistribution(d, parent) {
        const s = d.summary || {};
        if (!s.count) {
            findContent(parent).appendChild(card(`
                <div class="text-label-sm font-label-sm text-secondary mb-2">DISTRIBUTION</div>
                <p class="text-outline">No non-null values to analyse.</p>
            `));
            return;
        }
        const fmt = (v) => v == null ? '-' : Number(v).toFixed(2);
        const pct = s.percentiles || {};
        const histogram = d.histogram || [];
        const maxCount = Math.max(...histogram.map(h => h.count || 0)) || 1;
        const bars = histogram.map(h => {
            const w = ((h.count || 0) / maxCount) * 100;
            return `
                <div class="space-y-1">
                    <div class="flex justify-between text-label-sm">
                        <span class="text-on-surface font-label-sm">${fmt(h.bin_start)} - ${fmt(h.bin_end)}</span>
                        <span class="text-outline">${h.count}</span>
                    </div>
                    <div class="h-2 rounded-full bg-surface-container-highest/40"><div class="h-full rounded-full bg-white" style="width:${w}%"></div></div>
                </div>`;
        }).join('');
        const statCell = (label, value) =>
            `<div><div class="text-label-sm text-outline">${label}</div><div class="text-body-md font-bold">${value}</div></div>`;
        const html = `
            <div class="text-label-sm font-label-sm text-secondary mb-2">DISTRIBUTION - ${escapeHtml(d.table)}.${escapeHtml(d.column)}</div>
            <div class="grid grid-cols-3 sm:grid-cols-6 gap-3 mb-4">
                ${statCell('count', s.count.toLocaleString())}
                ${statCell('min', fmt(s.min))}
                ${statCell('max', fmt(s.max))}
                ${statCell('mean', fmt(s.mean))}
                ${statCell('median', fmt(s.median))}
                ${statCell('stddev', fmt(s.stddev))}
            </div>
            <div class="grid grid-cols-3 sm:grid-cols-6 gap-3 mb-4 text-label-sm">
                ${statCell('p25', fmt(pct.p25))}
                ${statCell('p50', fmt(pct.p50))}
                ${statCell('p75', fmt(pct.p75))}
                ${statCell('p90', fmt(pct.p90))}
                ${statCell('p95', fmt(pct.p95))}
                ${statCell('p99', fmt(pct.p99))}
            </div>
            <div class="text-label-sm font-label-sm text-outline mb-2">HISTOGRAM</div>
            <div class="space-y-1.5">${bars}</div>
        `;
        findContent(parent).appendChild(card(html));
    }

    function renderTrend(d, parent) {
        const series = d.series || [];
        if (!series.length) {
            // Don't render a misleading "0% across 0 periods" card.
            findContent(parent).appendChild(card(`
                <div class="text-label-sm font-label-sm text-secondary mb-2">TREND</div>
                <p class="text-outline">No data points to plot for that period.</p>
            `));
            return;
        }
        const pct = d.change_pct || 0;
        const arrow = pct >= 0 ? '↑' : '↓';
        const color = pct >= 0 ? 'text-tertiary' : 'text-error';
        const grain = d.granularity || 'period';
        const valueLabel = d.value_column
            ? `${(d.agg || 'value').toUpperCase()}(${d.value_column})`
            : 'value';
        const html = `
            <div class="text-label-sm font-label-sm text-secondary mb-2">TREND</div>
            <div class="flex items-baseline gap-3 mb-3">
                <span class="text-headline-md font-headline-md font-bold ${color}">${arrow} ${Math.abs(pct).toFixed(1)}%</span>
                <span class="text-label-sm text-outline">across ${series.length} ${grain}${series.length === 1 ? '' : 's'}</span>
                ${d.inflection_period ? `<span class="text-label-sm text-outline">· inflection at <code>${escapeHtml(d.inflection_period)}</code></span>` : ''}
            </div>
            <div class="chart-mount" style="width:100%;min-height:260px"></div>
        `;
        const wrapper = card(html);
        findContent(parent).appendChild(wrapper);
        const mount = wrapper.querySelector('.chart-mount');
        const spec = {
            '$schema': 'https://vega.github.io/schema/vega-lite/v5.json',
            'data': { 'values': series },
            'mark': {
                'type': 'line', 'point': { 'color': '#ffffff' },
                'tooltip': true, 'strokeWidth': 2, 'color': '#ffffff',
            },
            'encoding': {
                'x': {
                    'field': 'period', 'type': 'ordinal',
                    'title': grain.charAt(0).toUpperCase() + grain.slice(1),
                    'axis': { 'labelAngle': -45, 'labelLimit': 80 },
                },
                'y': {
                    'field': 'value', 'type': 'quantitative',
                    'title': valueLabel,
                },
                'tooltip': [
                    { 'field': 'period', 'type': 'ordinal', 'title': grain },
                    { 'field': 'value', 'type': 'quantitative', 'title': valueLabel, 'format': ',.2f' },
                ],
            },
            'width': 'container', 'height': 240,
        };
        if (typeof vegaEmbed === 'undefined') {
            mount.innerHTML = '<p class="text-error">vega-embed not loaded</p>';
            return;
        }
        vegaEmbed(mount, spec, { actions: false, theme: 'dark' })
            .catch(err => { mount.innerHTML = `<p class="text-error">Chart error: ${escapeHtml(err.message || err)}</p>`; });
    }

    function renderCompare(d, parent) {
        const a = Number(d.a) || 0, b = Number(d.b) || 0, pct = Number(d.change_pct) || 0;
        if (a === 0 && b === 0) {
            findContent(parent).appendChild(card(`
                <div class="text-label-sm font-label-sm text-secondary mb-2">COMPARE PERIODS</div>
                <p class="text-outline">Both periods returned zero - check the date range or filter values.</p>
            `));
            return;
        }
        const arrow = pct >= 0 ? '↑' : '↓';
        const color = pct >= 0 ? 'text-tertiary' : 'text-error';
        let breakdown = '';
        if (Array.isArray(d.by_breakdown) && d.by_breakdown.length) {
            const rows = d.by_breakdown.map(r => `
                <tr><td class="px-3 py-1 text-label-sm">${escapeHtml(r.key)}</td>
                    <td class="px-3 py-1 text-label-sm text-right">${Number(r.a).toFixed(1)}</td>
                    <td class="px-3 py-1 text-label-sm text-right">${Number(r.b).toFixed(1)}</td>
                    <td class="px-3 py-1 text-label-sm text-right ${Number(r.change_pct) >= 0 ? 'text-tertiary' : 'text-error'}">${Number(r.change_pct).toFixed(1)}%</td></tr>
            `).join('');
            breakdown = `<table class="w-full mt-3 border-t border-white/10"><thead><tr class="text-outline text-label-sm"><th class="px-3 py-1 text-left">Group</th><th class="px-3 py-1 text-right">A</th><th class="px-3 py-1 text-right">B</th><th class="px-3 py-1 text-right">Δ</th></tr></thead><tbody>${rows}</tbody></table>`;
        }
        const html = `
            <div class="text-label-sm font-label-sm text-secondary mb-2">COMPARE PERIODS</div>
            <div class="grid grid-cols-3 gap-4">
                <div><div class="text-label-sm text-outline">Period A</div><div class="text-headline-md font-bold">${a.toFixed(1)}</div></div>
                <div><div class="text-label-sm text-outline">Period B</div><div class="text-headline-md font-bold">${b.toFixed(1)}</div></div>
                <div><div class="text-label-sm text-outline">Change</div><div class="text-headline-md font-bold ${color}">${arrow} ${Math.abs(pct).toFixed(1)}%</div></div>
            </div>
            ${breakdown}
        `;
        findContent(parent).appendChild(card(html));
    }

    function renderTopN(d, parent) {
        const rows = d.rows || [];
        if (!rows.length) {
            findContent(parent).appendChild(card(`
                <div class="text-label-sm font-label-sm text-secondary mb-2">TOP N</div>
                <p class="text-outline">No groups to rank for that filter.</p>
            `));
            return;
        }
        const hasShare = rows.some(r => r.share != null);
        // Bar lengths scale by share when meaningful, otherwise by raw value
        // (some aggregates like avg / min / max don't have a "share" metaphor).
        const maxValue = Math.max(...rows.map(r => Number(r.value) || 0)) || 1;
        const maxShare = hasShare
            ? (Math.max(...rows.map(r => Number(r.share) || 0)) || 1)
            : 1;
        const aggLabel = d.agg ? `${String(d.agg).toUpperCase()}(${d.value_column || ''})` : '';
        const bars = rows.map(r => {
            const value = Number(r.value) || 0;
            const share = r.share == null ? null : Number(r.share);
            const w = hasShare
                ? (share / maxShare) * 100
                : (value / maxValue) * 100;
            const rightLabel = share == null
                ? value.toFixed(1)
                : `${value.toFixed(1)} (${share.toFixed(1)}%)`;
            return `
                <div class="space-y-1">
                    <div class="flex justify-between text-label-sm"><span class="text-on-surface">${escapeHtml(r.group)}</span><span class="text-outline">${rightLabel}</span></div>
                    <div class="h-2 rounded-full bg-surface-container-highest/40"><div class="h-full rounded-full bg-white" style="width:${w}%"></div></div>
                </div>`;
        }).join('');
        const header = aggLabel
            ? `TOP ${rows.length} - ${escapeHtml(aggLabel)}`
            : `TOP ${rows.length}`;
        const html = `
            <div class="text-label-sm font-label-sm text-secondary mb-2">${header}</div>
            <div class="space-y-2">${bars}</div>
        `;
        findContent(parent).appendChild(card(html));
    }

    function renderTableArtifact(payload, parent) {
        if (!payload) return;
        const cols = payload.columns || [];
        const rows = payload.rows || [];
        if (!cols.length && !rows.length) return;

        const toolLabel = (payload.tool || 'query').replace(/_/g, ' ').toUpperCase();
        const meta = [];
        if (payload.table_name) meta.push(`<code class="text-outline">${escapeHtml(payload.table_name)}</code>`);
        if (typeof payload.total_rows === 'number') {
            meta.push(`${payload.total_rows.toLocaleString()} total rows`);
        } else if (typeof payload.row_count === 'number') {
            meta.push(`${payload.row_count} row${payload.row_count === 1 ? '' : 's'}`);
        }
        if (payload.truncated_at_cap) meta.push('<span class="text-error">truncated at cap</span>');
        if (payload.has_more) meta.push('more pages available');
        const metaLine = meta.length ? `<span class="text-outline text-label-sm">${meta.join(' · ')}</span>` : '';

        const head = `<thead><tr class="bg-surface-container-highest/40 border-b border-white/10">${
            cols.map(c => `<th class="px-3 py-2 text-label-sm font-bold text-primary sticky top-0 backdrop-blur-md z-10">${escapeHtml(c)}</th>`).join('')
        }</tr></thead>`;
        const body = `<tbody class="divide-y divide-white/5">${
            rows.map((r, i) => `<tr class="${i % 2 === 0 ? 'bg-white/5' : ''}">${
                (Array.isArray(r) ? r : cols.map(c => r[c])).map(v =>
                    `<td class="px-3 py-2 text-label-sm text-on-surface-variant">${v == null ? '<span class="text-outline">NULL</span>' : escapeHtml(v)}</td>`
                ).join('')
            }</tr>`).join('')
        }</tbody>`;
        const html = `
            <div class="flex items-center justify-between mb-2">
                <span class="text-label-sm font-label-sm text-secondary">${toolLabel}</span>
                ${metaLine}
            </div>
            <div class="overflow-x-auto max-h-[400px] rounded-lg border border-white/10">
                <table class="w-full text-left border-collapse min-w-max">${head}${body}</table>
            </div>
        `;
        findContent(parent).appendChild(card(html));
    }

    function renderAll(artifacts, parent) {
        if (!artifacts) return;
        if (artifacts.explain)   renderExplain(artifacts.explain, parent);
        if (artifacts.dry_run)   renderDryRun(artifacts.dry_run, parent);
        if (artifacts.table)     renderTableArtifact(artifacts.table, parent);
        if (artifacts.analytics) renderAnalytics(artifacts.analytics, parent);
        if (artifacts.chart)     renderChart(artifacts.chart, parent);
    }

    window.ArtifactRenderers = {
        renderAll, renderChart, renderExplain, renderDryRun, renderAnalytics, renderTableArtifact,
    };
})();
