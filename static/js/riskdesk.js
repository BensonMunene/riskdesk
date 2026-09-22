/* RiskDesk front-end helpers.
   - RD.*        Plotly chart builders; every chart reads its data from a <script type="application/json"> block.
   - Cards       every card holding a chart or table gets maximise + CSV buttons; tables get click-to-sort and a filter box.
   - Forms       long-running forms show a spinner on submit (data-busy attribute).
*/
window.RD = (function () {
  const palette = ["#2f6df6", "#f28e2b", "#59a14f", "#e15759", "#76b7b2", "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
                   "#4e79a7", "#a0cbe8", "#f1ce63", "#8cd17d", "#d37295"];
  const font = { family: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif", size: 11, color: "#2b3444" };

  function data(id) {
    const el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { console.error("bad chart json", id, e); return null; }
  }

  function layout(extra) {
    return Object.assign({
      font: font, margin: { l: 48, r: 16, t: 24, b: 40 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
      hovermode: "closest", legend: { orientation: "h", y: -0.18, font: { size: 10 } },
      xaxis: { gridcolor: "#eef1f5", zerolinecolor: "#d5dae2" }, yaxis: { gridcolor: "#eef1f5", zerolinecolor: "#d5dae2" },
    }, extra || {});
  }
  const config = { displayModeBar: false, responsive: true };

  function pct(v, d) { return v == null ? "" : (v * 100).toFixed(d == null ? 1 : d) + "%"; }
  function money(v) {
    if (v == null) return "";
    const s = v < 0 ? "-" : ""; v = Math.abs(v);
    if (v >= 1e9) return s + "$" + (v / 1e9).toFixed(2) + "bn";
    if (v >= 1e6) return s + "$" + (v / 1e6).toFixed(2) + "m";
    if (v >= 1e3) return s + "$" + (v / 1e3).toFixed(0) + "k";
    return s + "$" + v.toFixed(0);
  }
  function el(id) { return typeof id === "string" ? document.getElementById(id) : id; }
  function fitHeight(node, labels, horizontal) {
    // horizontal bar charts with many categories need more room; remember the natural height for maximise
    if (!horizontal || !labels) return undefined;
    const h = Math.max(node.clientHeight || 260, labels.length * 22 + 90);
    node.dataset.naturalHeight = h;
    node.style.height = h + "px";
    return h;
  }

  function bar(id, labels, values, opts) {
    opts = opts || {};
    const node = el(id);
    const colors = opts.colors || values.map(v => (v < 0 ? "#e15759" : (opts.color || palette[0])));
    const trace = { type: "bar", x: opts.horizontal ? values : labels, y: opts.horizontal ? labels : values,
      orientation: opts.horizontal ? "h" : "v", marker: { color: colors },
      text: values.map(v => opts.fmt ? opts.fmt(v) : pct(v)), textposition: "auto", textfont: { size: 10 },
      hovertemplate: "%{" + (opts.horizontal ? "y" : "x") + "}: %{text}<extra></extra>" };
    const lay = layout({ yaxis: { tickformat: opts.tickformat || ".0%", gridcolor: "#eef1f5", automargin: true },
      xaxis: { tickformat: opts.horizontal ? (opts.tickformat || ".0%") : undefined, automargin: true, gridcolor: "#eef1f5" } });
    if (opts.horizontal) { lay.yaxis = { autorange: "reversed", automargin: true }; lay.margin.l = 90; fitHeight(node, labels, true); }
    if (opts.title) lay.title = { text: opts.title, font: { size: 12 } };
    Plotly.newPlot(node, [trace], lay, config);
  }

  function groupedBar(id, labels, series, opts) {
    opts = opts || {};
    const node = el(id);
    const traces = series.map((s, i) => ({ type: "bar", name: s.name, x: opts.horizontal ? s.values : labels,
      y: opts.horizontal ? labels : s.values, orientation: opts.horizontal ? "h" : "v",
      marker: { color: s.color || palette[i] }, hovertemplate: s.name + " %{" + (opts.horizontal ? "y" : "x") + "}: %{" + (opts.horizontal ? "x" : "y") + ":" + (opts.hoverfmt || ".1%") + "}<extra></extra>" }));
    const lay = layout({ barmode: opts.stacked ? "relative" : "group" });
    if (opts.horizontal) { lay.xaxis.tickformat = opts.tickformat || ".0%"; lay.yaxis.autorange = "reversed"; lay.yaxis.automargin = true; lay.margin.l = 90; fitHeight(node, labels, true); }
    else { lay.yaxis.tickformat = opts.tickformat || ".0%"; lay.xaxis.automargin = true; }
    if (opts.title) lay.title = { text: opts.title, font: { size: 12 } };
    Plotly.newPlot(node, traces, lay, config);
  }

  function line(id, series, opts) {
    opts = opts || {};
    const traces = series.filter(s => s && s.x).map((s, i) => ({ type: "scatter", mode: "lines", name: s.name, x: s.x, y: s.y,
      line: { color: s.color || palette[i], width: s.width || 1.6, dash: s.dash }, fill: s.fill, fillcolor: s.fillcolor,
      hovertemplate: s.name + " %{x}: %{y:" + (opts.hoverfmt || ".2%") + "}<extra></extra>" }));
    const lay = layout({ yaxis: { tickformat: opts.tickformat || ".0%", gridcolor: "#eef1f5" }, xaxis: { gridcolor: "#eef1f5" }, hovermode: "x unified" });
    if (opts.title) lay.title = { text: opts.title, font: { size: 12 } };
    if (opts.shapes) lay.shapes = opts.shapes;
    if (opts.annotations) lay.annotations = opts.annotations;
    Plotly.newPlot(el(id), traces, lay, config);
  }

  function area(id, x, series, opts) {
    // stacked area, e.g. weights over time. series: {name: [values]}
    opts = opts || {};
    const traces = Object.keys(series).map((k, i) => ({ type: "scatter", mode: "lines", name: k, x: x, y: series[k], stackgroup: "one",
      line: { width: 0.5, color: palette[i % palette.length] }, fillcolor: palette[i % palette.length],
      hovertemplate: k + " %{x}: %{y:.1%}<extra></extra>" }));
    const lay = layout({ yaxis: { tickformat: ".0%", gridcolor: "#eef1f5" }, xaxis: { gridcolor: "#eef1f5" }, hovermode: "x unified",
      legend: { orientation: "h", y: -0.2, font: { size: 9 } } });
    Plotly.newPlot(el(id), traces, lay, config);
  }

  function donut(id, labels, values) {
    const trace = { type: "pie", labels: labels, values: values.map(v => Math.abs(v)), hole: 0.55, textinfo: "label+percent",
      textfont: { size: 10 }, marker: { colors: palette }, hovertemplate: "%{label}: %{percent}<extra></extra>", sort: false };
    Plotly.newPlot(el(id), [trace], layout({ showlegend: false, margin: { l: 10, r: 10, t: 10, b: 10 } }), config);
  }

  function heatmap(id, x, y, z, opts) {
    opts = opts || {};
    const node = el(id);
    const trace = { type: "heatmap", x: x, y: y, z: z, colorscale: opts.colorscale || [[0, "#c0392b"], [0.5, "#ffffff"], [1, "#1f77b4"]],
      zmin: opts.zmin == null ? -1 : opts.zmin, zmax: opts.zmax == null ? 1 : opts.zmax, showscale: true,
      hovertemplate: "%{y} × %{x}: %{z:.2f}<extra></extra>", colorbar: { thickness: 10, len: 0.8 } };
    if (opts.showText) { trace.text = z.map(r => r.map(v => v.toFixed(opts.textDigits == null ? 2 : opts.textDigits))); trace.texttemplate = "%{text}"; trace.textfont = { size: 9 }; }
    const lay = layout({ margin: { l: 70, r: 10, t: 10, b: 70 }, xaxis: { tickangle: -45, automargin: true, gridcolor: "rgba(0,0,0,0)" },
      yaxis: { autorange: "reversed", automargin: true, gridcolor: "rgba(0,0,0,0)" } });
    const h = Math.max(node.clientHeight || 260, y.length * 20 + 120);
    node.dataset.naturalHeight = h; node.style.height = h + "px";
    Plotly.newPlot(node, [trace], lay, config);
  }

  function bubble(id, d, opts) {
    opts = opts || {};
    const groups = {};
    d.labels.forEach((t, i) => { const g = d.sector ? d.sector[i] : "All"; (groups[g] = groups[g] || []).push(i); });
    const sizes = d.size || d.weight;
    const maxSize = Math.max.apply(null, sizes);
    const traces = Object.keys(groups).map((g, gi) => ({ type: "scatter", mode: "markers+text", name: g,
      x: groups[g].map(i => d.x[i]), y: groups[g].map(i => d.y[i]), text: groups[g].map(i => d.labels[i]), textposition: "top center", textfont: { size: 9 },
      marker: { size: groups[g].map(i => 8 + 30 * (sizes[i] / maxSize)), color: palette[gi % palette.length], opacity: 0.8, line: { width: 1, color: "#fff" } },
      hovertemplate: "%{text}<br>" + (opts.xlabel || "x") + ": %{x:.1%}<br>" + (opts.ylabel || "y") + ": %{y:.1%}<extra>" + g + "</extra>" }));
    const lay = layout({ xaxis: { title: { text: opts.xlabel, font: { size: 11 }, standoff: 6 }, tickformat: ".0%", gridcolor: "#eef1f5" },
      yaxis: { title: { text: opts.ylabel, font: { size: 11 } }, tickformat: ".0%", gridcolor: "#eef1f5" },
      legend: { orientation: "h", y: -0.3, font: { size: 9 } }, margin: { l: 56, r: 16, t: 24, b: 70 } });
    if (opts.diagonal) lay.shapes = [{ type: "line", x0: 0, y0: 0, x1: opts.diagonal, y1: opts.diagonal, line: { color: "#adb5bd", dash: "dot", width: 1 } }];
    Plotly.newPlot(el(id), traces, lay, config);
  }

  function scatter(id, pts, opts) {
    opts = opts || {};
    const traces = pts.map((p, i) => Object.assign({ type: "scatter", mode: p.mode || "markers", name: p.name, x: p.x, y: p.y, text: p.text,
      textposition: "top center", textfont: { size: 9 }, marker: Object.assign({ color: palette[i], size: 8 }, p.marker || {}), line: p.line,
      hovertemplate: "%{text}<br>vol %{x:.1%}<br>return %{y:.1%}<extra>" + p.name + "</extra>" }, p.extra || {}));
    const lay = layout({ xaxis: { title: { text: opts.xlabel || "Volatility", font: { size: 11 }, standoff: 6 }, tickformat: ".0%", gridcolor: "#eef1f5" },
      yaxis: { title: { text: opts.ylabel || "Expected return", font: { size: 11 } }, tickformat: ".0%", gridcolor: "#eef1f5" },
      legend: { orientation: "h", y: -0.32, font: { size: 10 } }, margin: { l: 56, r: 16, t: 24, b: 64 } });
    Plotly.newPlot(el(id), traces, lay, config);
  }

  function histogram(id, counts, edges, lines) {
    const centers = edges.slice(0, -1).map((e, i) => (e + edges[i + 1]) / 2);
    const trace = { type: "bar", x: centers, y: counts, marker: { color: centers.map(c => c < 0 ? "#e15759" : "#59a14f"), opacity: 0.8 },
      hovertemplate: "%{x:.2%}: %{y} days<extra></extra>", width: (edges[1] - edges[0]) * 0.95 };
    const shapes = [], annotations = [];
    (lines || []).forEach(l => {
      shapes.push({ type: "line", x0: l.x, x1: l.x, y0: 0, y1: 1, yref: "paper", line: { color: l.color, width: 1.5, dash: "dash" } });
      annotations.push({ x: l.x, y: 1, yref: "paper", text: l.label, showarrow: false, font: { size: 9, color: l.color }, xanchor: "right", yshift: -8 - (l.offset || 0) });
    });
    const lay = layout({ xaxis: { tickformat: ".1%", gridcolor: "#eef1f5" }, yaxis: { title: { text: "Days", font: { size: 10 } }, gridcolor: "#eef1f5" }, shapes: shapes, annotations: annotations, bargap: 0.05 });
    Plotly.newPlot(el(id), [trace], lay, config);
  }

  /* ------------------------------------------------------------------ cards: maximise + csv */
  function resizeCharts(card) {
    card.querySelectorAll(".chart").forEach(c => { if (c.data) Plotly.Plots.resize(c); });
  }

  function toggleMax(card) {
    const on = !card.classList.contains("rd-fullscreen");
    document.querySelectorAll(".card.rd-fullscreen").forEach(c => { if (c !== card) exitMax(c); });
    if (on) {
      card.classList.add("rd-fullscreen");
      document.body.classList.add("rd-has-fullscreen");
      const charts = card.querySelectorAll(".chart");
      const avail = window.innerHeight - 110;
      charts.forEach(c => { c.style.height = Math.max(240, Math.floor(avail / Math.max(1, charts.length)) - 10) + "px"; });
      card.querySelector(".rd-max i").className = "bi bi-fullscreen-exit";
    } else {
      exitMax(card);
    }
    resizeCharts(card);
  }

  function exitMax(card) {
    card.classList.remove("rd-fullscreen");
    document.body.classList.remove("rd-has-fullscreen");
    card.querySelectorAll(".chart").forEach(c => { c.style.height = c.dataset.naturalHeight ? c.dataset.naturalHeight + "px" : ""; });
    const icon = card.querySelector(".rd-max i"); if (icon) icon.className = "bi bi-arrows-fullscreen";
    resizeCharts(card);
  }

  function tableToCsv(table) {
    const rows = [];
    table.querySelectorAll("tr").forEach(tr => {
      if (tr.hidden) return;
      const cells = [];
      tr.querySelectorAll("th, td").forEach(td => {
        const input = td.querySelector("input, select");
        let text = input ? (input.value || "") : td.innerText;
        text = text.replace(/\s+/g, " ").trim().replace(/"/g, '""');
        cells.push('"' + text + '"');
      });
      if (cells.length) rows.push(cells.join(","));
    });
    return rows.join("\n");
  }

  function download(name, text) {
    const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }

  function enhanceCards() {
    document.querySelectorAll(".card").forEach(card => {
      const hasChart = card.querySelector(".chart");
      const table = card.querySelector("table.rd:not(.nosort)");
      if (!hasChart && !table) return;
      if (card.querySelector(".login-card")) return;
      let header = card.querySelector(":scope > .card-header");
      if (!header) { header = document.createElement("div"); header.className = "card-header"; header.innerHTML = "<span class='hint'></span>"; card.prepend(header); }
      const tools = document.createElement("span");
      tools.className = "rd-tools";
      if (table && table.tBodies[0] && table.tBodies[0].rows.length > 10) {
        const f = document.createElement("input");
        f.type = "search"; f.placeholder = "filter"; f.className = "rd-filter form-control form-control-sm";
        f.addEventListener("input", () => filterTable(table, f.value));
        tools.appendChild(f);
      }
      if (table) {
        const b = document.createElement("button");
        b.type = "button"; b.className = "rd-tool"; b.title = "Download as CSV"; b.innerHTML = "<i class='bi bi-download'></i>";
        b.addEventListener("click", () => {
          const title = (header.childNodes[0] && header.childNodes[0].textContent || "table").trim().replace(/[^a-z0-9]+/gi, "_").slice(0, 40);
          download((title || "table") + ".csv", tableToCsv(table));
        });
        tools.appendChild(b);
      }
      const m = document.createElement("button");
      m.type = "button"; m.className = "rd-tool rd-max"; m.title = "Maximise (Esc to close)"; m.innerHTML = "<i class='bi bi-arrows-fullscreen'></i>";
      m.addEventListener("click", () => toggleMax(card));
      tools.appendChild(m);
      header.appendChild(tools);
    });
    document.addEventListener("keydown", e => { if (e.key === "Escape") document.querySelectorAll(".card.rd-fullscreen").forEach(exitMax); });
    const backdrop = document.createElement("div"); backdrop.className = "rd-backdrop";
    backdrop.addEventListener("click", () => document.querySelectorAll(".card.rd-fullscreen").forEach(exitMax));
    document.body.appendChild(backdrop);
  }

  /* ------------------------------------------------------------------ tables: sort + filter */
  function cellValue(td) {
    const input = td.querySelector("input");
    let t = (td.dataset.sort != null) ? td.dataset.sort : (input ? input.value : td.innerText);
    t = (t || "").trim();
    if (t === "" || t === "–" || t === "-") return { n: null, s: "" };
    const m = t.replace(/[,$\s]/g, "").match(/^([+-]?\d*\.?\d+)(%|bn|m|k|d|h|x|×)?/i);
    if (m) {
      let n = parseFloat(m[1]);
      const suf = (m[2] || "").toLowerCase();
      if (suf === "bn") n *= 1e9; else if (suf === "m") n *= 1e6; else if (suf === "k") n *= 1e3; else if (suf === "h") n /= 6.5;
      return { n: n, s: t.toLowerCase() };
    }
    return { n: null, s: t.toLowerCase() };
  }

  function sortTable(table, idx, th) {
    const tbody = table.tBodies[0];
    if (!tbody) return;
    const dir = th.dataset.dir === "asc" ? "desc" : "asc";
    table.querySelectorAll("thead th").forEach(h => { h.dataset.dir = ""; h.classList.remove("sorted-asc", "sorted-desc"); });
    th.dataset.dir = dir; th.classList.add(dir === "asc" ? "sorted-asc" : "sorted-desc");
    const rows = Array.from(tbody.rows);
    const totals = rows.filter(r => r.classList.contains("total"));
    const body = rows.filter(r => !r.classList.contains("total"));
    body.sort((a, b) => {
      const va = cellValue(a.cells[idx] || a.cells[0]), vb = cellValue(b.cells[idx] || b.cells[0]);
      let c;
      if (va.n != null && vb.n != null) c = va.n - vb.n;
      else if (va.n != null) c = -1; else if (vb.n != null) c = 1;
      else c = va.s.localeCompare(vb.s);
      return dir === "asc" ? c : -c;
    });
    body.concat(totals).forEach(r => tbody.appendChild(r));
  }

  function sortableTables() {
    document.querySelectorAll("table.rd:not(.nosort)").forEach(table => {
      const head = table.tHead; if (!head || !table.tBodies[0] || table.tBodies[0].rows.length < 2) return;
      Array.from(head.rows[0].cells).forEach((th, idx) => {
        if (!th.textContent.trim()) return;
        th.classList.add("sortable"); th.title = "Click to sort";
        th.addEventListener("click", () => sortTable(table, idx, th));
      });
    });
  }

  function filterTable(table, q) {
    q = (q || "").toLowerCase();
    Array.from(table.tBodies[0].rows).forEach(r => { r.hidden = q !== "" && !r.classList.contains("total") && r.innerText.toLowerCase().indexOf(q) === -1; });
  }

  /* ------------------------------------------------------------------ forms: busy state */
  function busyForms() {
    document.querySelectorAll("form[data-busy]").forEach(form => {
      form.addEventListener("submit", e => {
        const btn = e.submitter || form.querySelector("button[type=submit], button:not([type])");
        if (!btn) return;
        // keep the button enabled (a disabled button's name/value would be dropped from the POST); just look busy
        btn.classList.add("disabled");
        btn.innerHTML = "<span class='spinner-border spinner-border-sm me-1'></span>" + (form.dataset.busy || "Working…");
        form.querySelectorAll("button").forEach(b => { if (b !== btn) b.classList.add("disabled"); });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => { enhanceCards(); sortableTables(); busyForms(); });

  return { data, layout, config, palette, pct, money, bar, groupedBar, line, area, donut, heatmap, bubble, scatter, histogram, toggleMax, filterTable };
})();

/* dynamic trade rows on the what-if page */
window.RDTrades = {
  addRow: function (ticker, mode, value) {
    const tbody = document.getElementById("trade-rows");
    const tpl = document.getElementById("trade-row-template").content.cloneNode(true);
    if (ticker) tpl.querySelector("input[name='ticker[]']").value = ticker;
    if (mode) tpl.querySelector("select[name='mode[]']").value = mode;
    if (value !== undefined && value !== null) tpl.querySelector("input[name='value[]']").value = value;
    tbody.appendChild(tpl);
  },
  removeRow: function (btn) { const tr = btn.closest("tr"); if (document.querySelectorAll("#trade-rows tr").length > 1) tr.remove(); else { tr.querySelectorAll("input").forEach(i => i.value = ""); } },
};
