/* ============================================================================
   云学通 · 看板共享图表库
   ----------------------------------------------------------------------------
   各项目页面共用同一套 SVG 图表函数（无第三方依赖）。
   主题色通过 CSS 变量 --accent 传入，函数内部用 ACCENT 兜底。
   ========================================================================== */

const BI = (() => {
  const PALETTE = ["#5578f6", "#21a99a", "#e4a044", "#9078d8", "#e56b70",
                   "#4ea980", "#7890b2", "#c47a55"];
  const ACCENT = "#5578f6";

  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const fmt = (value, digits = 0) => value == null ? "—"
    : Number(value).toLocaleString("zh-CN",
        {minimumFractionDigits: digits, maximumFractionDigits: digits});
  const empty = (message = "暂无可用数据") => `<div class="chart-empty">${esc(message)}</div>`;

  function svgText(x, y, text, attrs = "") {
    return `<text x="${x}" y="${y}" ${attrs}>${esc(text)}</text>`;
  }

  /* ------------------------------------------------------------ 条形图 */
  function bars(items, {height = 32, limit = 10, colors = PALETTE, showPct = false} = {}) {
    const data = (items || []).filter(x => Number(x.count) > 0).slice(0, limit);
    if (!data.length) return empty();
    const width = 560, labelW = 150, valueW = 56, plotW = width - labelW - valueW - 14;
    const gap = 9, rowH = height + gap, svgH = data.length * rowH + 8;
    const max = Math.max(1, ...data.map(d => Number(d.count)));
    const total = data.reduce((s, d) => s + Number(d.count), 0);
    const body = data.map((d, i) => {
      const y = 5 + i * rowH;
      const w = Math.max(2, plotW * Number(d.count) / max);
      const color = colors[i % colors.length];
      const label = String(d.label ?? "");
      const display = showPct
        ? `${fmt(d.count)} · ${((Number(d.count) / total) * 100).toFixed(0)}%`
        : fmt(d.count);
      const title = `${label}: ${fmt(d.count)}`
        + (showPct ? ` (${((Number(d.count) / total) * 100).toFixed(1)}%)` : "");
      return `<g>
        <title>${esc(title)}</title>
        ${svgText(labelW - 9, y + height * .68,
          label.length > 12 ? label.slice(0, 11) + "…" : label,
          'text-anchor="end" fill="#68758b" font-size="11"')}
        <rect x="${labelW}" y="${y}" width="${plotW}" height="${height}" rx="5" fill="#f0f2f7"/>
        <rect x="${labelW}" y="${y}" width="${w}" height="${height}" rx="5" fill="${color}"/>
        ${svgText(labelW + plotW + 8, y + height * .68, display,
          'fill="#39465e" font-size="10" font-weight="650"')}
      </g>`;
    }).join("");
    return `<svg viewBox="0 0 ${width} ${svgH}" role="img" aria-label="分类条形图">${body}</svg>`;
  }

  /* --------------------------------------------------------- 迷你折线 */
  function sparkline(points, color = ACCENT, width = 240, height = 36) {
    const valid = (points || []).filter(p => p.value != null);
    if (valid.length < 2) return "";
    const values = valid.map(p => Number(p.value));
    const min = Math.min(...values), max = Math.max(...values);
    const range = max - min || 1, pad = 3;
    const coords = values.map((v, i) => {
      const x = pad + i * (width - pad * 2) / (values.length - 1);
      const y = height - pad - ((v - min) / range) * (height - pad * 2);
      return [x, y];
    });
    const path = coords.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
    const last = coords[coords.length - 1];
    return `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true">
      <path d="${path}" fill="none" stroke="${color}" stroke-width="2.4"
        stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="${last[0]}" cy="${last[1]}" r="3" fill="${color}"/></svg>`;
  }

  /* --------------------------------------------------------- 批次趋势 */
  function lineChart(series, {height = 230} = {}) {
    const source = series || [];
    const points = source.flatMap(s => (s.points || []).map(p => ({...p, label: s.label})));
    const batches = [...new Set(points.map(p => p.batch))].sort();
    if (!batches.length) return empty();
    const w = 620, h = height, left = 42, right = 15, top = 17, bottom = 33;
    const plotH = h - top - bottom, plotW = w - left - right;
    const vals = points.map(p => Number(p.value)).filter(Number.isFinite);
    const min = Math.min(0, ...vals), max = Math.max(1, ...vals), range = max - min || 1;
    const x = (b) => left + (batches.indexOf(b) / Math.max(1, batches.length - 1)) * plotW;
    const y = (v) => top + (max - Number(v)) / range * plotH;

    let grid = "";
    for (let i = 0; i < 4; i++) {
      const gy = top + i * plotH / 3, gval = max - i * range / 3;
      grid += `<line x1="${left}" y1="${gy}" x2="${w - right}" y2="${gy}" stroke="#edf0f5"/>`;
      grid += svgText(left - 7, gy + 3, fmt(gval),
        'text-anchor="end" fill="#a0a8b6" font-size="9"');
    }
    const lines = source.map((s, i) => {
      const ps = (s.points || []).filter(p => p.value != null);
      if (!ps.length) return "";
      const segments = ps.slice(1).map((p, j) => {
        const prev = ps[j], isDemo = p.is_demo || prev.is_demo;
        const stroke = isDemo ? "#e4a044" : (s.color || PALETTE[i % PALETTE.length]);
        return `<line x1="${x(prev.batch)}" y1="${y(prev.value)}" x2="${x(p.batch)}"
          y2="${y(p.value)}" stroke="${stroke}" stroke-width="2.2" stroke-linecap="round"
          ${isDemo ? 'stroke-dasharray="5 4"' : ""}/>`;
      }).join("");
      const dots = ps.map(p => {
        const stroke = p.is_demo ? "#e4a044" : (s.color || PALETTE[i % PALETTE.length]);
        return `<circle cx="${x(p.batch)}" cy="${y(p.value)}" r="3"
          fill="${p.is_demo ? "#e4a044" : "#fff"}" stroke="${stroke}" stroke-width="2">
          <title>${esc(s.label)} · ${esc(p.batch)}${p.is_demo ? " · 演示批次" : ""}: ${fmt(p.value, s.decimals)}</title>
        </circle>`;
      }).join("");
      return segments + dots;
    }).join("");
    const labels = batches.map(batch =>
      svgText(x(batch), h - 8, batch, 'text-anchor="middle" fill="#8690a1" font-size="9"')).join("");
    return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="批次趋势">${grid}${lines}${labels}</svg>`;
  }

  /* -------------------------------------------------------- 环形比例图 */
  function pieLegend(items) {
    const valid = (items || []).filter(i => Number(i.count) > 0);
    if (!valid.length) return empty();
    const total = valid.reduce((s, i) => s + Number(i.count), 0);
    const cx = 95, cy = 95, r = 67, circ = 2 * Math.PI * r;
    let used = 0;
    const arcs = valid.map((item, i) => {
      const ratio = Number(item.count) / total, offset = -used * circ;
      used += ratio;
      return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none"
        stroke="${PALETTE[i % PALETTE.length]}" stroke-width="25"
        stroke-dasharray="${ratio * circ} ${circ}" stroke-dashoffset="${offset}">
        <title>${esc(item.label)}: ${fmt(item.count)}</title></circle>`;
    }).join("");
    const legend = valid.slice(0, 8).map((item, i) =>
      `<span><i style="--c:${PALETTE[i % PALETTE.length]}"></i>${esc(item.label)} ${fmt(item.count)}</span>`
    ).join("");
    return `<svg viewBox="0 0 190 190" style="max-width:190px;margin:auto" role="img"
      aria-label="比例分布图"><g transform="rotate(-90 95 95)">
      <circle cx="95" cy="95" r="${r}" fill="none" stroke="#eff1f5" stroke-width="25"/>${arcs}
      </g><text x="95" y="92" text-anchor="middle" fill="#202b40" font-size="23"
      font-weight="700">${fmt(total)}</text>
      <text x="95" y="110" text-anchor="middle" fill="#8791a2" font-size="9">条记录</text>
      </svg><div class="legend">${legend}</div>`;
  }

  /* ------------------------------------------------------ 月度趋势面积图 */
  function areaChart(items, {labelKey = "month", valueKey = "count", accent = ACCENT} = {}) {
    const data = (items || []).filter(d => Number(d[valueKey]) >= 0);
    if (!data.length) return empty("当前批次没有可解析的时间维度");
    const w = 560, h = 235, l = 40, r = 15, t = 15, b = 38;
    const pw = w - l - r, ph = h - t - b;
    const max = Math.max(1, ...data.map(d => Number(d[valueKey])));
    const grid = [0, 1, 2, 3].map(i => {
      const gy = t + i * ph / 3;
      return `<line x1="${l}" y1="${gy}" x2="${w - r}" y2="${gy}" stroke="#edf0f5"/>` +
        svgText(l - 7, gy + 3, fmt(max * (1 - i / 3)),
          'text-anchor="end" fill="#a0a8b6" font-size="9"');
    }).join("");
    const coords = data.map((d, i) => [
      l + (data.length === 1 ? pw / 2 : i * pw / (data.length - 1)),
      t + ph - (Number(d[valueKey]) / max) * ph,
    ]);
    const path = coords.map((p, i) => `${i ? "L" : "M"}${p[0]},${p[1]}`).join(" ");
    const area = `${path} L${coords[coords.length - 1][0]},${t + ph} L${coords[0][0]},${t + ph} Z`;
    const dots = data.map((d, i) =>
      `<circle cx="${coords[i][0]}" cy="${coords[i][1]}" r="4" fill="#fff"
        stroke="${accent}" stroke-width="2">
        <title>${esc(d[labelKey])}: ${fmt(d[valueKey])}</title></circle>` +
      svgText(coords[i][0], h - 12, String(d[labelKey]).slice(2),
        'text-anchor="middle" fill="#8690a1" font-size="9"')).join("");
    return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="月度趋势">
      ${grid}<path d="${area}" fill="${accent}" opacity=".08"/>
      <path d="${path}" fill="none" stroke="${accent}" stroke-width="2.5"/>${dots}</svg>`;
  }

  /* --------------------------------------------------------------- 热力图 */
  function heatmap(data, {accent = ACCENT} = {}) {
    if (!data?.months?.length || !data?.courses?.length) return empty();
    const max = Math.max(1, ...data.heatmap.map(x => x.count));
    const valueFor = (course, month) =>
      data.heatmap.find(x => x.course === course && x.month === month)?.count || 0;
    const head = data.months.map(m => `<th>${esc(m.label)}</th>`).join("");
    const rows = data.courses.map(course => {
      const cells = data.months.map(month => {
        const v = valueFor(course.label, month.label);
        const alpha = v ? (.10 + .72 * v / max) : .035;
        const color = v ? `rgba(${hexToRgb(accent)},${alpha})` : "#f7f8fb";
        return `<td style="background:${color}"
          title="${esc(course.label)} · ${esc(month.label)}: ${v} 条">${v ? fmt(v) : "·"}</td>`;
      }).join("");
      return `<tr><td title="${esc(course.label)}">${esc(course.label)}</td>${cells}</tr>`;
    }).join("");
    return `<table aria-label="课程月份人次热力图"><thead><tr><th>课程 / 月份</th>${head}</tr>
      </thead><tbody>${rows}</tbody></table>`;
  }

  function hexToRgb(hex) {
    const h = String(hex).replace("#", "");
    const full = h.length === 3 ? h.split("").map(c => c + c).join("") : h;
    const n = parseInt(full, 16);
    return `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
  }

  /* ------------------------------------------------------------- KPI 卡片 */
  function kpiCard(kpi, trend, i) {
    const color = PALETTE[i % PALETTE.length];
    const value = fmt(kpi.value, kpi.decimals) + (kpi.unit || "");
    const delta = kpi.delta;
    const deltaText = delta == null ? "暂无上期对比"
      : `${delta > 0 ? "+" : ""}${fmt(delta, kpi.decimals)}${kpi.unit || ""} 对比上期`;
    const deltaClass = delta == null ? "flat" : delta > 0 ? "up" : delta < 0 ? "down" : "flat";
    return `<article class="kpi" style="--tone:${color}">
      <div class="kpi-head"><span class="kpi-label">${esc(kpi.label)}</span>
        <i class="kpi-dot"></i></div>
      <div class="kpi-value">${esc(value)}</div>
      ${kpi.desc ? `<div class="kpi-desc">${esc(kpi.desc)}</div>` : ""}
      <div class="kpi-foot"><span class="delta ${deltaClass}">${esc(deltaText)}</span></div>
      <div class="spark">${sparkline(trend?.points, color)}</div>
    </article>`;
  }

  /* --------------------------------------------------------------- 主题 */
  function applyTheme(accent, accentSoft) {
    if (accent) document.documentElement.style.setProperty("--accent", accent);
    if (accentSoft) document.documentElement.style.setProperty("--accent-soft", accentSoft);
  }

  /* --------------------------------------------------------- 异常分布图 */
  /**
   * 异常分布条形图。
   * 刻意区分三种「空」——它们对业务的含义完全不同，混为一谈会误导决策：
   *   1) 未配置异常规则     → 该数据集本来就不做异常检测，不是「没问题」
   *   2) 配了规则但本批次 0  → 真的没问题（可信的「绿灯」）
   *   3) 有历史但本批次为空   → 本批次未发现异常
   */
  function anomalyChart(anomalies, {height = 25, limit = 8, colors = ["#e56b70", "#e4a044", "#9078d8"]} = {}) {
    const a = anomalies || {};
    const rules = a.by_rule || [];
    if (!rules.length) {
      if (a.rules_configured === false) {
        return `<div class="chart-empty">该数据集未配置异常检测规则，此处不展示异常统计</div>`;
      }
      return `<div class="chart-empty">本批次未发现异常</div>`;
    }
    return bars(rules.map(x => ({label: x.label, count: x.count})), {height, limit, colors});
  }

  /* ------------------------------------------------------ 分组堆叠柱状图 */
  /**
   * 按维度分组的堆叠柱状图（每个维度一根柱，柱内按 group_key 堆叠）。
   *
   * 用途：像「每月报名了 1/2/3/4 门课的人数」这种
   * 「维度 × 分档」交叉分布 —— 既有总量对比，又能看构成。
   *
   * @param items  [{label, groups:{分档: 值}, total}]
   * @param groupOrder 分档的显示顺序（不传则按数值自动排序）
   */
  function stackedBars(items, {groupOrder, colors = PALETTE, height = 240, labelKey = "label"} = {}) {
    const data = (items || []).filter(x => x && x.groups);
    if (!data.length) return empty();
    // 收集全部分档
    const all = new Set();
    data.forEach(d => Object.keys(d.groups || {}).forEach(k => all.add(k)));
    let order = groupOrder && groupOrder.length
      ? groupOrder.filter(k => all.has(k))
      : [...all].sort((a, b) => {
          const na = parseFloat(a), nb = parseFloat(b);
          if (Number.isFinite(na) && Number.isFinite(nb)) return na - nb;
          return String(a).localeCompare(String(b), "zh-CN");
        });
    if (!order.length) order = [...all];
    const w = 640, left = 44, right = 14, top = 16, bottom = 34;
    const pw = w - left - right, ph = height - top - bottom;
    const totals = data.map(d => order.reduce((s, k) => s + Number(d.groups[k] || 0), 0));
    const max = Math.max(1, ...totals);
    const slot = pw / data.length;
    const barW = Math.min(56, slot * 0.62);

    const grid = [0, 1, 2, 3].map(i => {
      const gy = top + i * ph / 3;
      return `<line x1="${left}" y1="${gy}" x2="${w - right}" y2="${gy}" stroke="#edf0f5"/>` +
        svgText(left - 7, gy + 3, fmt(max * (1 - i / 3)),
          'text-anchor="end" fill="#a0a8b6" font-size="9"');
    }).join("");

    const barsSvg = data.map((d, i) => {
      const cx = left + slot * i + slot / 2;
      let acc = 0;
      const segs = order.map((k, gi) => {
        const v = Number(d.groups[k] || 0);
        if (v <= 0) return "";
        const h = (v / max) * ph;
        const y = top + ph - acc - h;
        acc += h;
        const color = colors[gi % colors.length];
        return `<rect x="${(cx - barW / 2).toFixed(1)}" y="${y.toFixed(1)}"
          width="${barW.toFixed(1)}" height="${h.toFixed(1)}" fill="${color}"
          rx="2"><title>${esc(d[labelKey] || "")} · ${esc(k)}: ${fmt(v)}</title></rect>`;
      }).join("");
      const total = totals[i];
      return segs +
        svgText(cx, top + ph - acc - 5, fmt(total),
          'text-anchor="middle" fill="#39465e" font-size="10" font-weight="650"') +
        svgText(cx, height - 10, String(d[labelKey] ?? ""),
          'text-anchor="middle" fill="#8690a1" font-size="10"');
    }).join("");

    const legend = order.map((k, i) =>
      `<span><i style="--c:${colors[i % colors.length]}"></i>${esc(k)}</span>`).join("");
    return `<svg viewBox="0 0 ${w} ${height}" role="img" aria-label="分组堆叠柱状图">
      ${grid}${barsSvg}</svg><div class="legend">${legend}</div>`;
  }

  return {
    $, esc, fmt, empty, bars, sparkline, lineChart, pieLegend, areaChart,
    heatmap, kpiCard, applyTheme, anomalyChart, stackedBars, PALETTE, hexToRgb,
  };
})();
