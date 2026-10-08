// Pulse page: plain JavaScript and SVG, no libraries and nothing loaded from the internet.
// Every URL is relative: the Orbit OS Launcher serves the page under its route (/pulse).
"use strict";

(() => {
  const RANGES = [
    [3600, "1 h"], [21600, "6 h"], [86400, "24 h"],
    [604800, "7 d"], [2592000, "30 d"], [31536000, "1 y"],
  ];
  const COLORS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"];
  const SVG = "http://www.w3.org/2000/svg";
  const STORE_KEY = "pulse.range";

  // Each chart: the series it asks for (by key or key prefix) and how to name them.
  const CHARTS = [
    {
      id: "cpu", title: "CPU", unit: "pct", max: 100, keys: ["cpu.total_pct"],
      names: { "cpu.total_pct": "All cores" },
      detail: { label: "Per core", prefix: "cpu.core_pct@", name: (l) => `Core ${l}` },
    },
    {
      id: "mem", title: "Memory", unit: "pct", max: 100, keys: ["mem.used_pct", "mem.swap_used_pct"],
      names: { "mem.used_pct": "RAM used", "mem.swap_used_pct": "Swap used" },
    },
    {
      id: "temp", title: "Temperature", unit: "c", floating: true,
      prefix: "temp.zone_c@", fallback: ["temp.soc_c"], names: { "temp.soc_c": "SoC" },
    },
    {
      id: "net", title: "Network", unit: "bps", keys: ["net.rx_bps@all", "net.tx_bps@all"],
      names: { "net.rx_bps@all": "Received", "net.tx_bps@all": "Sent" },
      detail: { label: "Per interface", prefix: ["net.rx_bps@", "net.tx_bps@"], skip: "@all",
        name: (l, k) => `${l} ${k.startsWith("net.rx") ? "in" : "out"}` },
    },
    {
      id: "diskio", title: "Disk activity", unit: "bps", keys: ["disk.read_bps", "disk.write_bps"],
      names: { "disk.read_bps": "Read", "disk.write_bps": "Written" },
    },
    { id: "disk", title: "Disk space used", unit: "pct", max: 100, prefix: "disk.used_pct@" },
    {
      id: "load", title: "Load average", unit: "num", keys: ["load.1m", "load.5m", "load.15m"],
      names: { "load.1m": "1 min", "load.5m": "5 min", "load.15m": "15 min" },
      sub: "processes waiting for a CPU",
    },
  ];

  const state = { range: 3600, info: null, config: null, charts: [], appCharts: [], selectedApp: "", timer: 0 };

  // ── small helpers ─────────────────────────────────────────────────────────

  function h(tag, attrs, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === false || v == null) continue;
      if (k === "text") e.textContent = v;
      else e.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children) if (c != null) e.append(c);
    return e;
  }

  function s(tag, attrs) {
    const e = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    return e;
  }

  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`${r.status} ${url}`);
    return r.json();
  }

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function bytes(v) {
    const units = ["B", "kB", "MB", "GB", "TB"];
    let i = 0;
    while (Math.abs(v) >= 1000 && i < units.length - 1) { v /= 1000; i++; }
    return `${i === 0 ? Math.round(v) : v.toFixed(Math.abs(v) < 10 ? 1 : 0)} ${units[i]}`;
  }

  const FORMAT = {
    pct: (v) => `${Math.abs(v) < 10 ? v.toFixed(1) : Math.round(v)} %`,
    c: (v) => `${v.toFixed(1)} °C`,
    bps: (v) => `${bytes(v)}/s`,
    bytes: (v) => bytes(v),
    num: (v) => v.toFixed(2),
  };

  // axis ticks are round numbers: no trailing zeros ("0 %", "2.5 kB/s", "46 °C")
  const TICK = {
    pct: (v) => `${+v.toFixed(2)} %`,
    c: (v) => `${+v.toFixed(1)} °C`,
    bps: (v) => `${bytes(v).replace(/\.0 /, " ")}/s`,
    bytes: (v) => bytes(v).replace(/\.0 /, " "),
    num: (v) => `${+v.toFixed(3)}`,
  };

  function duration(sec) {
    sec = Math.max(0, Math.floor(sec));
    const d = Math.floor(sec / 86400), hr = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
    if (d) return `${d} d ${hr} h`;
    if (hr) return `${hr} h ${m} min`;
    return `${m} min`;
  }

  const dateTime = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
  const timeOnly = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
  const dayMonth = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });
  const monthOnly = new Intl.DateTimeFormat(undefined, { month: "short" });
  const monthYear = new Intl.DateTimeFormat(undefined, { month: "short", year: "numeric" });

  function label(key) {
    const at = key.indexOf("@");
    return at < 0 ? key : key.slice(at + 1);
  }

  // ── axes ──────────────────────────────────────────────────────────────────

  function niceScale(lo, hi, count) {
    if (!(hi > lo)) hi = lo + 1;
    const raw = (hi - lo) / count;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const n = raw / mag;
    const step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
    const start = Math.floor(lo / step) * step;
    const end = Math.ceil(hi / step) * step;
    const ticks = [];
    for (let v = start; v <= end + step / 2; v += step) ticks.push(+v.toFixed(10));
    return { lo: start, hi: end, ticks };
  }

  function timeTicks(t0, t1, maxTicks) {
    const span = t1 - t0;
    const ticks = [];
    if (span > 60 * 86400) {
      // month starts, every k months
      const months = span / (30.4 * 86400);
      const every = [1, 2, 3, 6, 12].find((k) => months / k <= maxTicks) || 12;
      const d = new Date(t0 * 1000);
      d.setDate(1); d.setHours(0, 0, 0, 0);
      while (d.getTime() / 1000 <= t1) {
        const t = d.getTime() / 1000;
        if (t >= t0 && d.getMonth() % every === 0) {
          ticks.push([t, d.getMonth() === 0 ? monthYear.format(d) : monthOnly.format(d)]);
        }
        d.setMonth(d.getMonth() + 1);
      }
      return ticks;
    }
    const steps = [300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 345600, 604800];
    const step = steps.find((st) => span / st <= maxTicks) || 604800;
    const tz = -new Date(t0 * 1000).getTimezoneOffset() * 60; // seconds east of UTC
    for (let t = Math.ceil((t0 + tz) / step) * step - tz; t <= t1; t += step) {
      const d = new Date(t * 1000);
      ticks.push([t, step < 86400 ? timeOnly.format(d) : dayMonth.format(d)]);
    }
    return ticks;
  }

  // ── line chart ────────────────────────────────────────────────────────────

  class LineChart {
    constructor(def) {
      this.def = def;
      this.data = null;
      this.detail = false;
      this.showTable = false;
      this.hover = -1;

      this.tableBtn = h("button", { class: "chip", type: "button", "aria-pressed": "false", text: "Table" });
      this.tableBtn.addEventListener("click", () => {
        this.showTable = !this.showTable;
        this.tableBtn.setAttribute("aria-pressed", String(this.showTable));
        this.render();
      });
      const tools = h("div", { class: "tools" });
      if (def.detail) {
        this.detailBtn = h("button", { class: "chip", type: "button", "aria-pressed": "false", text: def.detail.label });
        this.detailBtn.addEventListener("click", () => {
          this.detail = !this.detail;
          this.detailBtn.setAttribute("aria-pressed", String(this.detail));
          this.load();
        });
        tools.append(this.detailBtn);
      }
      tools.append(this.tableBtn);

      this.sub = h("span", { class: "muted" });
      this.legend = h("div", { class: "legend" });
      this.plot = h("div", { class: "plot" });
      this.tip = h("div", { class: "tip", hidden: true });
      this.tableWrap = h("div", { class: "table-wrap", hidden: true });
      this.card = h("figure", { class: "card chart", style: "margin:0" },
        h("div", { class: "card-head" }, h("h2", { text: def.title }), this.sub, tools),
        this.legend, this.plot, this.tableWrap);

      let lastWidth = 0;
      new ResizeObserver(() => {
        const w = this.plot.clientWidth;
        if (w && Math.abs(w - lastWidth) > 4) { lastWidth = w; this.render(); }
      }).observe(this.plot);
    }

    url() {
      const p = new URLSearchParams();
      const d = this.def;
      if (this.detail && d.detail) {
        for (const prefix of [].concat(d.detail.prefix)) p.append("prefix", prefix);
      } else {
        for (const k of d.keys || []) p.append("key", k);
        for (const k of d.fallback || []) p.append("key", k);
        if (d.prefix) p.append("prefix", d.prefix);
      }
      p.set("range", String(state.range));
      p.set("points", String(Math.max(60, Math.min(800, Math.round((this.plot.clientWidth || 600) / 2)))));
      return `api/series?${p}`;
    }

    async load() {
      try {
        const data = await getJSON(this.url());
        this.data = this.prepare(data);
      } catch (e) {
        this.data = { error: String(e.message || e) };
      }
      this.render();
    }

    prepare(data) {
      const d = this.def;
      let keys = Object.keys(data.series).filter((k) => data.series[k].length);
      if (this.detail && d.detail && d.detail.skip) keys = keys.filter((k) => !k.endsWith(d.detail.skip));
      if (!this.detail && d.prefix && d.fallback) {
        // prefer the detailed series (e.g. every thermal zone) when the device gives them
        const detailed = keys.filter((k) => k.startsWith(d.prefix));
        if (detailed.length) keys = detailed;
      }
      keys.sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
      const order = (d.keys || []).concat(d.fallback || []);
      keys.sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));
      const extra = Math.max(0, keys.length - COLORS.length);
      keys = keys.slice(0, COLORS.length); // never a ninth colour
      const series = keys.map((k, i) => ({
        key: k,
        name: this.name(k),
        color: css(COLORS[i]),
        points: data.series[k],
        byTime: new Map(data.series[k].map((p) => [p[0], p])),
      }));
      const times = [...new Set(series.flatMap((x) => x.points.map((p) => p[0])))].sort((a, b) => a - b);
      const interval = (state.config && state.config.interval_s) || 30;
      return { ...data, series, times, extra, band: series.length === 1 && data.step > interval };
    }

    name(key) {
      const d = this.def;
      if (this.detail && d.detail) return d.detail.name(label(key), key);
      if (d.names && d.names[key]) return d.names[key];
      return label(key);
    }

    render() {
      const data = this.data;
      const fmt = FORMAT[this.def.unit];
      this.sub.textContent = this.def.sub || "";
      this.legend.replaceChildren();
      this.plot.replaceChildren();
      this.tableWrap.replaceChildren();
      if (!data) return;
      if (data.error) {
        this.plot.append(h("p", { class: "empty", text: `Could not load: ${data.error}` }));
        return;
      }
      const series = data.series;
      if (!series.length) {
        this.plot.append(h("p", { class: "empty", text: "No data for this range yet." }));
        return;
      }
      const tierNote = { raw: "every sample", "5m": "5-minute averages", "1h": "hourly averages" }[data.tier] || "";
      this.sub.textContent = [this.def.sub, data.band ? `${tierNote}, band = min–max` : tierNote].filter(Boolean).join(" · ");

      if (series.length > 1) {
        for (const x of series) {
          this.legend.append(h("span", { class: "item" }, h("span", { class: "key", style: `background:${x.color}` }), x.name));
        }
        if (data.extra) this.legend.append(h("span", { class: "item muted", text: `+${data.extra} more not shown` }));
      }

      this.tableWrap.hidden = !this.showTable;
      this.plot.hidden = this.showTable;
      if (this.showTable) {
        this.renderTable(series, data.times, fmt);
        return;
      }

      const W = Math.max(280, this.plot.clientWidth || 600);
      const H = 210;
      const m = { l: 52, r: 74, t: 10, b: 24 };
      const pw = W - m.l - m.r;
      const ph = H - m.t - m.b;
      const t0 = data.from, t1 = data.to;

      let lo = Infinity, hi = -Infinity;
      for (const x of series) for (const p of x.points) {
        lo = Math.min(lo, data.band ? p[2] : p[1]);
        hi = Math.max(hi, data.band ? p[3] : p[1]);
      }
      let scale;
      if (this.def.max) scale = niceScale(0, Math.max(this.def.max, hi), 4);
      else if (this.def.floating) scale = niceScale(Math.floor(lo - 2), Math.ceil(hi + 2), 4);
      else scale = niceScale(0, hi > 0 ? hi * 1.1 : 1, 4);

      const x = (t) => m.l + ((t - t0) / (t1 - t0)) * pw;
      const y = (v) => m.t + ph - ((v - scale.lo) / (scale.hi - scale.lo)) * ph;
      const gap = data.step * 2.5;

      const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, tabindex: "0", role: "img",
        "aria-label": `${this.def.title} chart. Use the arrow keys to read values, or open the table.` });

      for (const v of scale.ticks) {
        svg.append(s("line", { x1: m.l, x2: m.l + pw, y1: y(v), y2: y(v), stroke: css(v === 0 ? "--axis" : "--grid"), "stroke-width": 1, "shape-rendering": "crispEdges" }));
        const t = s("text", { x: m.l - 8, y: y(v) + 4, "text-anchor": "end" });
        t.textContent = TICK[this.def.unit](v);
        svg.append(t);
      }
      for (const [t, text] of timeTicks(t0, t1, Math.max(2, Math.floor(pw / 90)))) {
        const tx = s("text", { x: x(t), y: H - 6, "text-anchor": "middle" });
        tx.textContent = text;
        svg.append(tx);
      }

      // segments: the line breaks where samples are missing (device off, app stopped)
      const segments = (pts) => {
        const out = [];
        let cur = [];
        for (const p of pts) {
          if (cur.length && p[0] - cur[cur.length - 1][0] > gap) { out.push(cur); cur = []; }
          cur.push(p);
        }
        if (cur.length) out.push(cur);
        return out;
      };

      const surface = css("--surface");
      for (const sr of series) {
        const segs = segments(sr.points);
        if (data.band) {
          for (const seg of segs) {
            const d = seg.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[3]).toFixed(1)}`).join("") +
              seg.slice().reverse().map((p) => `L${x(p[0]).toFixed(1)},${y(p[2]).toFixed(1)}`).join("") + "Z";
            svg.append(s("path", { d, fill: sr.color, "fill-opacity": 0.14, stroke: "none" }));
          }
        }
        for (const seg of segs) {
          if (seg.length === 1) {
            svg.append(s("circle", { cx: x(seg[0][0]), cy: y(seg[0][1]), r: 2, fill: sr.color }));
            continue;
          }
          const d = seg.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
          svg.append(s("path", { d, fill: "none", stroke: sr.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
        }
      }

      // end markers and end values; a label that would collide is left to the legend and tooltip
      const ends = series.map((sr) => ({ sr, p: sr.points[sr.points.length - 1] }))
        .sort((a, b) => y(a.p[1]) - y(b.p[1]));
      let lastY = -Infinity;
      for (const { sr, p } of ends) {
        svg.append(s("circle", { cx: x(p[0]), cy: y(p[1]), r: 4, fill: sr.color, stroke: surface, "stroke-width": 2 }));
        const ly = y(p[1]) + 4;
        if (ly - lastY >= 13) {
          const t = s("text", { x: x(p[0]) + 9, y: ly, class: "end" });
          t.textContent = fmt(p[1]);
          svg.append(t);
          lastY = ly;
        }
      }

      // hover: a crosshair that snaps to the nearest sample, one tooltip for every series
      const cross = s("line", { y1: m.t, y2: m.t + ph, stroke: css("--axis"), "stroke-width": 1, visibility: "hidden" });
      const dots = s("g", { visibility: "hidden" });
      svg.append(cross, dots);
      const hit = s("rect", { x: m.l, y: m.t, width: pw, height: ph, fill: "transparent" });
      svg.append(hit);

      const show = (i) => {
        if (i < 0 || i >= data.times.length) return;
        this.hover = i;
        const t = data.times[i];
        const cx = x(t);
        cross.setAttribute("x1", cx); cross.setAttribute("x2", cx);
        cross.setAttribute("visibility", "visible");
        dots.replaceChildren();
        const rows = [];
        for (const sr of series) {
          const p = sr.byTime.get(t);
          if (!p) continue;
          dots.append(s("circle", { cx, cy: y(p[1]), r: 4, fill: sr.color, stroke: surface, "stroke-width": 2 }));
          rows.push(h("div", { class: "row" },
            h("span", { class: "key", style: `background:${sr.color}` }),
            h("strong", { text: fmt(p[1]) }),
            h("span", { class: "name", text: series.length > 1 ? sr.name : "" }),
            data.band && p[2] !== p[3] ? h("span", { class: "range", text: `${fmt(p[2])} – ${fmt(p[3])}` }) : null));
        }
        dots.setAttribute("visibility", "visible");
        this.tip.replaceChildren(h("div", { class: "when", text: dateTime.format(new Date(t * 1000)) }), ...rows);
        this.tip.hidden = false;
        const scaleX = this.plot.clientWidth / W;
        const left = cx * scaleX;
        const tw = this.tip.offsetWidth;
        this.tip.style.left = `${left + 14 + tw > this.plot.clientWidth ? Math.max(0, left - 14 - tw) : left + 14}px`;
        this.tip.style.top = `${m.t}px`;
      };
      const hide = () => {
        cross.setAttribute("visibility", "hidden");
        dots.setAttribute("visibility", "hidden");
        this.tip.hidden = true;
      };
      const nearest = (clientX) => {
        const r = svg.getBoundingClientRect();
        const t = t0 + (((clientX - r.left) * (W / r.width)) - m.l) / pw * (t1 - t0);
        let a = 0, b = data.times.length - 1;
        while (a < b) {
          const mid = (a + b) >> 1;
          if (data.times[mid] < t) a = mid + 1; else b = mid;
        }
        if (a > 0 && Math.abs(data.times[a - 1] - t) < Math.abs(data.times[a] - t)) a--;
        return a;
      };
      svg.addEventListener("pointermove", (e) => show(nearest(e.clientX)));
      svg.addEventListener("pointerleave", () => { if (document.activeElement !== svg) hide(); });
      svg.addEventListener("focus", () => show(this.hover >= 0 ? Math.min(this.hover, data.times.length - 1) : data.times.length - 1));
      svg.addEventListener("blur", hide);
      svg.addEventListener("keydown", (e) => {
        const step = e.shiftKey ? 10 : 1;
        if (e.key === "ArrowLeft") { show(Math.max(0, this.hover - step)); e.preventDefault(); }
        else if (e.key === "ArrowRight") { show(Math.min(data.times.length - 1, this.hover + step)); e.preventDefault(); }
        else if (e.key === "Escape") { svg.blur(); }
      });

      this.plot.append(svg, this.tip);
      this.tip.hidden = true;
    }

    renderTable(series, times, fmt) {
      const head = h("tr", {}, h("th", { scope: "col", text: "Time" }),
        ...series.map((sr) => h("th", { scope: "col", class: "num", text: sr.name })));
      const body = h("tbody");
      for (const t of times.slice(-500).reverse()) {
        body.append(h("tr", {}, h("td", { text: dateTime.format(new Date(t * 1000)) }),
          ...series.map((sr) => {
            const p = sr.byTime.get(t);
            return h("td", { class: "num", text: p ? fmt(p[1]) : "—" });
          })));
      }
      this.tableWrap.append(h("table", {}, h("thead", {}, head), body));
    }
  }

  // ── tiles ─────────────────────────────────────────────────────────────────

  function tile(labelText, value, detail) {
    return h("div", { class: "tile" },
      h("div", { class: "label", text: labelText }),
      h("div", { class: "value", text: value }),
      h("div", { class: "detail", text: detail || " " }));
  }

  async function loadTiles() {
    const now = await getJSON("api/now");
    const v = now.values || {};
    const info = (state.info && state.info.device) || {};
    const tiles = [];
    const has = (k) => typeof v[k] === "number";
    tiles.push(tile("CPU", has("cpu.total_pct") ? FORMAT.pct(v["cpu.total_pct"]) : "—",
      has("cpu.freq_mhz") ? `${Math.round(v["cpu.freq_mhz"])} MHz` : ""));
    tiles.push(tile("Memory", has("mem.used_pct") ? FORMAT.pct(v["mem.used_pct"]) : "—",
      has("mem.used_bytes") ? `${bytes(v["mem.used_bytes"])} of ${info.total_ram ? bytes(info.total_ram) : "?"}` : ""));
    const zones = Object.keys(v).filter((k) => k.startsWith("temp.zone_c@"));
    const temp = zones.length ? Math.max(...zones.map((k) => v[k])) : v["temp.soc_c"];
    tiles.push(tile("Temperature", typeof temp === "number" ? FORMAT.c(temp) : "—",
      zones.length > 1 ? `hottest of ${zones.length} sensors` : ""));
    tiles.push(tile("Disk /", has("disk.used_pct@/") ? FORMAT.pct(v["disk.used_pct@/"]) : "—",
      has("disk.used_bytes@/") ? `${bytes(v["disk.used_bytes@/"])} used` : ""));
    tiles.push(tile("Network", has("net.rx_bps@all") ? `↓ ${FORMAT.bps(v["net.rx_bps@all"])}` : "—",
      has("net.tx_bps@all") ? `↑ ${FORMAT.bps(v["net.tx_bps@all"])}` : ""));
    tiles.push(tile("Up for", has("sys.uptime_s") ? duration(v["sys.uptime_s"]) : "—",
      has("load.1m") ? `load ${v["load.1m"].toFixed(2)}` : ""));
    document.getElementById("tiles").replaceChildren(...tiles);
  }

  // ── apps ──────────────────────────────────────────────────────────────────

  async function loadApps() {
    const { apps } = await getJSON("api/apps");
    const remote = state.info && state.info.mode === "remote";
    document.getElementById("apps-note").textContent = remote
      ? "CPU and memory per app are measured when Pulse runs on the device."
      : "Share of the whole CPU. Select an app to see its history.";
    apps.sort((a, b) => (b.running - a.running) || ((b.cpu_pct || 0) - (a.cpu_pct || 0)) || a.name.localeCompare(b.name));
    const body = document.querySelector("#apps tbody");
    body.replaceChildren(...apps.map((a) => {
      const btn = h("button", { class: "app-name", type: "button", text: a.name });
      const tr = h("tr", { "aria-selected": String(a.package_id === state.selectedApp) },
        h("td", {}, btn, h("span", { class: "pkg", text: `${a.package_id} · ${a.version}` })),
        h("td", {}, h("span", { class: `state${a.running ? " on" : ""}`, text: a.running ? "Running" : "Stopped" })),
        h("td", { class: "num", text: typeof a.cpu_pct === "number" ? FORMAT.pct(a.cpu_pct) : "—" }),
        h("td", { class: "num", text: typeof a.mem_bytes === "number" ? bytes(a.mem_bytes) : "—" }),
        h("td", { class: "num", text: a.running ? duration(a.uptime_s) : "—" }));
      tr.addEventListener("click", () => selectApp(a.package_id === state.selectedApp ? "" : a.package_id, a.name));
      return tr;
    }));
  }

  function selectApp(pkg, name) {
    state.selectedApp = pkg;
    for (const tr of document.querySelectorAll("#apps tbody tr")) {
      tr.setAttribute("aria-selected", String(tr.querySelector(".pkg").textContent.startsWith(`${pkg} `)));
    }
    const box = document.getElementById("app-charts");
    state.appCharts = pkg ? [
      new LineChart({ id: "app-cpu", title: `${name} · CPU`, unit: "pct", keys: [`app.cpu_pct@${pkg}`], names: { [`app.cpu_pct@${pkg}`]: "CPU" } }),
      new LineChart({ id: "app-mem", title: `${name} · Memory`, unit: "bytes", keys: [`app.mem_bytes@${pkg}`], names: { [`app.mem_bytes@${pkg}`]: "Memory" } }),
    ] : [];
    box.replaceChildren(...state.appCharts.map((c) => c.card));
    for (const c of state.appCharts) c.load();
  }

  // ── settings ──────────────────────────────────────────────────────────────

  async function loadStats() {
    const st = await getJSON("api/stats");
    const when = (t) => (t ? dateTime.format(new Date(t * 1000)) : "—");
    const rows = [
      ["Size", bytes(st.file_bytes)],
      ["Series", String(st.series)],
      ["Every sample", `${st.tiers.raw.rows.toLocaleString()} rows, since ${when(st.tiers.raw.oldest)}`],
      ["5-minute averages", `${st.tiers["5m"].rows.toLocaleString()} rows, since ${when(st.tiers["5m"].oldest)}`],
      ["Hourly averages", `${st.tiers["1h"].rows.toLocaleString()} rows, since ${when(st.tiers["1h"].oldest)}`],
    ];
    document.getElementById("db-stats").replaceChildren(...rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })]));
  }

  function fillForm() {
    const form = document.getElementById("config-form");
    for (const [k, v] of Object.entries(state.config || {})) {
      if (form.elements[k]) form.elements[k].value = v;
    }
  }

  async function saveConfig(e) {
    e.preventDefault();
    const form = e.target;
    const msg = document.getElementById("config-msg");
    if (!form.reportValidity()) return;
    const body = {};
    for (const input of form.querySelectorAll("input")) body[input.name] = Number(input.value);
    msg.className = "muted";
    msg.textContent = "Saving…";
    try {
      const r = await fetch("api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || r.status);
      state.config = data;
      fillForm();
      msg.className = "saved";
      msg.textContent = "Saved. Retention applies at the next hourly clean-up.";
      schedule();
    } catch (err) {
      msg.className = "error";
      msg.textContent = `Not saved: ${err.message || err}`;
    }
  }

  function updateExport() {
    const p = new URLSearchParams();
    for (const prefix of ["cpu.", "mem.", "temp.", "net.", "disk.", "load.", "sys.", "apps.", "app."]) p.append("prefix", prefix);
    p.set("range", String(state.range));
    document.getElementById("export").href = `api/export.csv?${p}`;
  }

  // ── page ──────────────────────────────────────────────────────────────────

  async function refresh() {
    const box = document.getElementById("charts");
    box.classList.add("loading");
    const jobs = [loadTiles(), loadApps(), ...state.charts.map((c) => c.load()), ...state.appCharts.map((c) => c.load())];
    if (document.getElementById("settings").open) jobs.push(loadStats());
    const results = await Promise.allSettled(jobs);
    box.classList.remove("loading");
    const failed = results.find((r) => r.status === "rejected");
    document.getElementById("status").textContent = failed
      ? `Could not reach Pulse (${failed.reason.message || failed.reason})`
      : `Updated ${timeOnly.format(new Date())}`;
  }

  function schedule() {
    clearInterval(state.timer);
    const interval = (state.config && state.config.interval_s) || 30;
    const every = state.range <= 86400 ? Math.max(10, interval) : 300;
    state.timer = setInterval(() => { if (!document.hidden) refresh(); }, every * 1000);
  }

  function setRange(range) {
    state.range = range;
    try { localStorage.setItem(STORE_KEY, String(range)); } catch (_) { /* private mode */ }
    for (const b of document.querySelectorAll("#ranges button")) b.setAttribute("aria-pressed", String(Number(b.dataset.range) === range));
    updateExport();
    schedule();
    refresh();
  }

  async function init() {
    try {
      const saved = Number(localStorage.getItem(STORE_KEY));
      if (RANGES.some(([r]) => r === saved)) state.range = saved;
    } catch (_) { /* storage blocked */ }

    const ranges = document.getElementById("ranges");
    for (const [r, text] of RANGES) {
      const b = h("button", { type: "button", "data-range": r, "aria-pressed": String(r === state.range), text });
      b.addEventListener("click", () => setRange(r));
      ranges.append(b);
    }
    state.charts = CHARTS.map((d) => new LineChart(d));
    document.getElementById("charts").replaceChildren(...state.charts.map((c) => c.card));

    document.getElementById("config-form").addEventListener("submit", saveConfig);
    document.getElementById("settings").addEventListener("toggle", (e) => { if (e.target.open) loadStats().catch(() => {}); });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

    try {
      state.info = await getJSON("api/info");
      state.config = state.info.config;
      const d = state.info.device || {};
      document.getElementById("device").textContent =
        [d.hardware, d.os, d.api ? `API ${d.api}` : ""].filter(Boolean).join(" · ") || "Orbit OS device";
      document.getElementById("mode").hidden = state.info.mode !== "remote";
      fillForm();
    } catch (e) {
      document.getElementById("device").textContent = "Pulse is not answering.";
    }
    updateExport();
    schedule();
    refresh();
  }

  init();
})();
