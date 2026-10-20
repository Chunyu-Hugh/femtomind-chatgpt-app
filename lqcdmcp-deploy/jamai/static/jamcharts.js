  /* ---------- interactive charts (jamai/static/jamcharts.js) ----------
     One SVG chart for every analysis view: the JAM panel and the SBI
     dashboard inline this file where they say @jamcharts (the gateway and the
     SBI plugin do it when they serve the page), so both draw the same way.
     The page defines esc() first.

     Points with one or two error bars, lines and credible bands (drawn in
     runs that stop at missing values and breakBefore), reference lines,
     categorical ticks, linear or log axes. Hover shows the values, a drag
     zooms, a double click (or reset) zooms out, a legend entry switches its
     series off and on (double click: only that one), a click on a point can
     select it (onPick). "customize" edits the chart for export: title, axis
     labels and ranges, log axes, fonts, line and marker sizes, each series'
     color and name, legend and grid, export size and background; PNG
     (1-3x), SVG and CSV exports use them. Customizations are kept per chart
     name in this browser. No library, no network.

     makeChart(box, opts) -> {set(series, opts), reset(), resetZoom(), opts}
       opts: name, height, title, xlabel, ylabel, xlog, ylog, logButtons,
             hline, hlines:[{y,label,fit}], vlines:[{x,color,dash,width,opacity}],
             xticks:[{v,label}], xpad, ymin, ymax, onPick(point, series)
       series: {key, name, legendName, color, points:[{x,y,lo,hi,lo2,hi2,info,tag,breakBefore}],
                line, width, opacity, dash, markers, hollow, shape ("diamond"), r, errWidth,
                band:[{x,lo,hi,breakBefore}], bandStyle ("area"|"bars"), hover, legend} */
  // The customize panel's styles travel with the chart (the pages style the chart itself).
  (function(){
    if (document.getElementById("jamcharts-css")) return;
    const s = document.createElement("style"); s.id = "jamcharts-css";
    s.textContent = ".chart-cust{border:1px dashed var(--line);border-radius:8px;padding:10px;margin:4px 0 6px;font-size:12px;background:var(--surface-2);}"
      + ".chart-cust .cc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;}"
      + ".chart-cust label{display:flex;flex-direction:column;gap:3px;color:var(--muted);font-size:11.5px;}"
      + ".chart-cust label.ck,.chart-cust label.cc-s{flex-direction:row;align-items:center;gap:5px;color:var(--fg);}"
      + ".chart-cust input[type=text],.chart-cust input[type=number],.chart-cust select{font-size:12px;padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--fg);min-width:0;}"
      + ".chart-cust .cc-row{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:flex-end;margin-top:8px;}"
      + ".chart-cust .cc-series{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:8px;}"
      + ".chart-cust .cc-s input[type=color]{width:26px;height:22px;padding:0;border:1px solid var(--line);border-radius:4px;background:none;}"
      + ".chart-cust .cc-s input[type=text]{width:150px;}.chart-cust .cc-hint{color:var(--muted);font-size:11px;margin-top:6px;}"
      + ".ct-title{font-weight:600;}.chart-legend[hidden]{display:none;}";
    document.head.appendChild(s);
  })();
  const SVGNS = "http://www.w3.org/2000/svg";
  const PALETTE = ["#2560c9","#d9480f","#2b8a3e","#ae3ec9","#c92a2a","#0b7285","#e67700","#5f3dc4","#868e96","#a61e4d"];
  const isNum = v=>typeof v === "number" && isFinite(v);
  // A CSS variable is not resolved in an SVG presentation attribute: those go in style.
  function svgEl(tag, attrs, parent){
    const n = document.createElementNS(SVGNS, tag);
    for (const k in attrs || {}){ const v = attrs[k]; if (typeof v === "string" && v.indexOf("var(") >= 0) n.style.setProperty(k, v); else n.setAttribute(k, v); }
    if (parent) parent.appendChild(n);
    return n;
  }
  function fmtVal(v, digits){
    if (!isNum(v)) return "—";
    if (digits != null) return v.toFixed(digits);
    const a = Math.abs(v);
    return a !== 0 && (a >= 1e5 || a < 1e-3) ? v.toExponential(3) : String(+v.toPrecision(5));
  }
  function fmtTick(v){
    const a = Math.abs(v);
    return a !== 0 && (a >= 1e4 || a < 1e-3) ? v.toExponential(0).replace("e+", "e") : String(+v.toPrecision(4));
  }
  // A line or band is drawn in runs: a missing value (a ratio left out for the
  // whole ensemble) or a point marked breakBefore ends one run.
  function runs(points, ok){
    const out = []; let cur = [];
    for (const p of points){
      if (!ok(p) || p.breakBefore){ if (cur.length) out.push(cur); cur = []; }
      if (ok(p)) cur.push(p);
    }
    if (cur.length) out.push(cur);
    return out;
  }
  function minMax(a){ let lo = Infinity, hi = -Infinity; for (const v of a){ if (v < lo) lo = v; if (v > hi) hi = v; } return [lo, hi]; }
  function linTicks(lo, hi, n){
    const span = hi - lo;
    if (!(span > 0)) return [lo];
    const raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw))), r = raw / mag;
    const step = (r > 5 ? 10 : r > 2 ? 5 : r > 1 ? 2 : 1) * mag, out = [];
    for (let t = Math.ceil(lo / step) * step; t <= hi + step * 1e-9; t += step) out.push(+t.toPrecision(12));
    return out;
  }
  function logTicks(lo, hi){                       // lo, hi: log10 of the axis ends
    const out = [], a = Math.floor(lo), b = Math.ceil(hi);
    if (b - a <= 2){
      for (let e = a; e <= b; e++) for (const m of [1, 2, 5]){ const t = e + Math.log10(m); if (t >= lo - 1e-9 && t <= hi + 1e-9) out.push(t); }
    } else {
      const stride = Math.max(1, Math.ceil((b - a) / 7));
      for (let e = a; e <= b; e += stride) if (e >= lo - 1e-9 && e <= hi + 1e-9) out.push(e);
    }
    return out;
  }
  function download(data, type, name){
    try{
      const a = document.createElement("a");
      a.href = URL.createObjectURL(data instanceof Blob ? data : new Blob([data], {type}));
      a.download = name; document.body.appendChild(a); a.click();
      setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 1500);
    }catch(e){}
  }
  // Any CSS color (a name, rgb(), var(--x)) as #rrggbb, for <input type=color>.
  function toHex(color, scope){
    const s = document.createElement("span"); s.style.color = color; s.style.display = "none"; (scope || document.body).appendChild(s);
    const m = getComputedStyle(s).color.match(/\d+(\.\d+)?/g); s.remove();
    return m ? "#" + m.slice(0, 3).map(v=>(+v).toString(16).padStart(2, "0")).join("") : "#2560c9";
  }
  const CUSTOM_KEY = "jamchart:";
  function loadCustom(name){ try{ return JSON.parse(localStorage.getItem(CUSTOM_KEY + name) || "{}") || {}; }catch(e){ return {}; } }
  function saveCustom(name, c){ try{ localStorage.setItem(CUSTOM_KEY + name, JSON.stringify(c)); }catch(e){} }

  function makeChart(box, opts){
    const st = {series: [], hidden: new Set(), view: null,
                opts: Object.assign({height: 340, xlog: false, ylog: false, logButtons: true, xlabel: "", ylabel: "", hline: null,
                                     hlines: [], vlines: [], name: "chart"}, opts)};
    st.custom = loadCustom(st.opts.name);
    box.className = "chart";
    box.innerHTML = '<div class="chart-bar"></div><div class="chart-cust" hidden></div><div class="chart-plot"></div><div class="chart-legend"></div><div class="chart-tip" hidden></div>';
    const bar = box.querySelector(".chart-bar"), cust = box.querySelector(".chart-cust"), plot = box.querySelector(".chart-plot"),
          legend = box.querySelector(".chart-legend"), tip = box.querySelector(".chart-tip");
    const button = (label, title, fn)=>{ const b = document.createElement("button"); b.type = "button"; b.className = "cb";
      b.textContent = label; b.title = title; b.onclick = fn; bar.appendChild(b); return b; };
    const bx = st.opts.logButtons ? button("log x", "Logarithmic x axis", ()=>{ setLog("xlog"); }) : null;
    const by = st.opts.logButtons ? button("log y", "Logarithmic y axis", ()=>{ setLog("ylog"); }) : null;
    button("reset zoom", "Show everything again (or double-click the plot)", ()=>{ st.view = null; draw(); });
    const bc = button("customize", "Title, labels, ranges, colors and export settings", ()=>{ cust.hidden = !cust.hidden; bc.classList.toggle("on", !cust.hidden); if (!cust.hidden) paintCustom(); });
    button("PNG", "Download the chart as a PNG image (customize sets size and resolution)", ()=>exportPng());
    button("SVG", "Download the chart as an SVG image", ()=>download(svgString(), "image/svg+xml", fileName() + ".svg"));
    button("CSV", "Download the plotted values", ()=>download(csv(), "text/csv", fileName() + ".csv"));
    let svg = null, marks = [], geom = null;
    const C = ()=>st.custom;
    const xlog = ()=>C().xlog != null ? C().xlog : st.opts.xlog, ylog = ()=>C().ylog != null ? C().ylog : st.opts.ylog;
    function setLog(k){ st.custom[k] = !(k === "xlog" ? xlog() : ylog()); st.view = null; saveCustom(st.opts.name, st.custom); draw(); if (!cust.hidden) paintCustom(); }
    const fileName = ()=>String(C().title || st.opts.name).replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 80) || "chart";
    const shown = ()=>st.series.filter(s=>!st.hidden.has(s.key) && !(C().off || {})[s.key]);
    const colorOf = s=>(C().colors || {})[s.key] || s.color;
    const nameOf = s=>(C().names || {})[s.key] || s.legendName || s.key;
    const tf = (v, log)=>log ? (v > 0 ? Math.log10(v) : NaN) : v;
    function hlinesAll(){ return (st.opts.hline != null ? [{y: st.opts.hline}] : []).concat(st.opts.hlines || []); }
    function extent(){
      const xs = [], ys = [], X = xlog(), Y = ylog();
      for (const s of shown()){
        for (const p of s.points || []){ xs.push(tf(p.x, X)); for (const v of [p.y, p.lo, p.hi, p.lo2, p.hi2]) if (isNum(v)) ys.push(tf(v, Y)); }
        for (const p of s.band || []){ xs.push(tf(p.x, X)); ys.push(tf(p.lo, Y), tf(p.hi, Y)); }
      }
      for (const l of st.opts.vlines || []) if (isNum(l.x)) xs.push(tf(l.x, X));
      for (const l of hlinesAll()) if (isNum(l.y) && l.fit !== false && !Y) ys.push(l.y);
      const fx = xs.filter(isNum), fy = ys.filter(isNum);
      if (!fx.length || !fy.length) return null;
      let [x0, x1] = minMax(fx), [y0, y1] = minMax(fy);
      if (!Y && st.opts.ymin != null) y0 = Math.min(y0, st.opts.ymin);
      if (!Y && st.opts.ymax != null) y1 = st.opts.ymax;
      const px = (x1 - x0) || Math.abs(x0) || 1, py = (y1 - y0) || Math.abs(y0) || 1, xp = st.opts.xpad == null ? 0.03 : st.opts.xpad;
      return [x0 - px * xp, x1 + px * xp, y0 - py * 0.06, y1 + py * 0.06];
    }
    // Custom axis ranges apply when the chart is not zoomed; a zoom (what is on screen) wins.
    function domain(){
      const d = st.view ? st.view.slice() : extent();
      if (!d || st.view) return d;
      const c = C(), X = xlog(), Y = ylog();
      if (isNum(c.xmin) && isNum(tf(c.xmin, X))) d[0] = tf(c.xmin, X);
      if (isNum(c.xmax) && isNum(tf(c.xmax, X))) d[1] = tf(c.xmax, X);
      if (isNum(c.ymin) && isNum(tf(c.ymin, Y))) d[2] = tf(c.ymin, Y);
      if (isNum(c.ymax) && isNum(tf(c.ymax, Y))) d[3] = tf(c.ymax, Y);
      return d[1] > d[0] && d[3] > d[2] ? d : extent();
    }
    // The chart as an <svg> of W x H in parent. exporting: no interaction layer,
    // and the legend is drawn inside the figure.
    function render(parent, W, H, exporting){
      const c = C(), fs = +c.font || 11, title = c.title != null && c.title !== "" ? c.title : (st.opts.title || "");
      const m = {l: 52 + fs * 1.1, r: 14, t: title ? fs * 2 + 14 : 12, b: 30 + fs * 1.3}, iw = W - m.l - m.r, ih = H - m.t - m.b;
      const out = {marks: []};
      const s0 = svgEl("svg", {width: W, height: H, viewBox: "0 0 " + W + " " + H, class: "chart-svg", xmlns: SVGNS}, parent);
      out.svg = s0;
      const text = (attrs, t, size, cls)=>{ const n = svgEl("text", Object.assign({class: cls || ""}, attrs), s0); n.style.fontSize = size + "px"; n.textContent = t; return n; };
      if (title) text({x: m.l + iw / 2, y: fs + 8, "text-anchor": "middle"}, title, fs + 3, "ct-label ct-title");
      const dom = domain();
      if (!dom){ text({x: W / 2, y: H / 2, "text-anchor": "middle"}, "Nothing to draw: every series is off, or no positive values on a log axis.", fs + 1, "ct-label"); return out; }
      const [x0, x1, y0, y1] = dom, XL = xlog(), YL = ylog();
      const X = v=>m.l + (tf(v, XL) - x0) / (x1 - x0) * iw, Y = v=>m.t + ih - (tf(v, YL) - y0) / (y1 - y0) * ih;
      out.geom = {W, H, m, iw, ih, dom};
      const clip = "clip" + Math.random().toString(36).slice(2);
      svgEl("rect", {x: m.l, y: m.t, width: iw, height: ih}, svgEl("clipPath", {id: clip}, svgEl("defs", {}, s0)));
      const axis = svgEl("g", {class: "ct-axis"}, s0), grid = c.grid !== false;
      const xt = st.opts.xticks ? st.opts.xticks.filter(t=>tf(t.v, XL) >= x0 && tf(t.v, XL) <= x1).map(t=>({x: X(t.v), label: t.label}))
        : (XL ? logTicks(x0, x1).map(t=>Math.pow(10, t)) : linTicks(x0, x1, Math.max(3, Math.floor(iw / (fs * 8))))).map(v=>({x: X(v), label: fmtTick(v)}));
      const yt = (YL ? logTicks(y0, y1).map(t=>Math.pow(10, t)) : linTicks(y0, y1, Math.max(3, Math.floor(ih / (fs * 5))))).map(v=>({y: Y(v), label: fmtTick(v)}));
      for (const t of xt){ if (grid) svgEl("line", {x1: t.x, x2: t.x, y1: m.t, y2: m.t + ih, class: "ct-grid"}, axis);
        const n = svgEl("text", {x: t.x, y: m.t + ih + fs + 4, "text-anchor": "middle"}, axis); n.style.fontSize = fs + "px"; n.textContent = t.label; }
      for (const t of yt){ if (grid) svgEl("line", {x1: m.l, x2: m.l + iw, y1: t.y, y2: t.y, class: "ct-grid"}, axis);
        const n = svgEl("text", {x: m.l - 6, y: t.y + fs * 0.35, "text-anchor": "end"}, axis); n.style.fontSize = fs + "px"; n.textContent = t.label; }
      svgEl("rect", {x: m.l, y: m.t, width: iw, height: ih, class: "ct-frame"}, axis);
      const xlabel = c.xlabel != null && c.xlabel !== "" ? c.xlabel : st.opts.xlabel, ylabel = c.ylabel != null && c.ylabel !== "" ? c.ylabel : st.opts.ylabel;
      text({x: m.l + iw / 2, y: H - 8, "text-anchor": "middle"}, xlabel || "", fs + 1, "ct-label");
      text({x: fs + 3, y: m.t + ih / 2, "text-anchor": "middle", transform: "rotate(-90 " + (fs + 3) + " " + (m.t + ih / 2) + ")"}, ylabel || "", fs + 1, "ct-label");
      const body = svgEl("g", {"clip-path": "url(#" + clip + ")"}, s0);
      const okY = v=>isNum(v) && isNum(tf(v, YL)), okX = v=>isNum(v) && isNum(tf(v, XL));
      for (const l of hlinesAll()){ if (!okY(l.y)) continue; const y = Y(l.y); if (y < m.t || y > m.t + ih) continue;
        svgEl("line", {x1: m.l, x2: m.l + iw, y1: y, y2: y, class: "ct-ref"}, body);
        if (l.label){ const n = svgEl("text", {x: m.l + iw - 4, y: y - 4, "text-anchor": "end", class: "ct-reftext"}, body); n.style.fontSize = (fs - 0.5) + "px"; n.textContent = l.label; } }
      for (const l of st.opts.vlines || []){ if (!okX(l.x)) continue; const x = X(l.x);
        svgEl("line", {x1: x, x2: x, y1: m.t, y2: m.t + ih, stroke: l.color || "var(--muted)", "stroke-width": (l.width || 1.2) * (+c.lw || 1),
                       "stroke-dasharray": l.dash || "none", opacity: l.opacity == null ? 0.9 : l.opacity}, body); }
      const lw = +c.lw || 1, ms = +c.ms || 1;
      for (const s of shown()){
        const col = colorOf(s);
        if (s.band && s.band.length){
          const ok = p=>okX(p.x) && okY(p.lo) && okY(p.hi);
          if (s.bandStyle === "bars"){
            for (const p of s.band.filter(ok)) svgEl("rect", {x: X(p.x) - 3, width: 6, y: Y(p.hi), height: Math.max(1.5, Y(p.lo) - Y(p.hi)), fill: col, "fill-opacity": 0.25}, body);
          } else {
            for (const run of runs(s.band, ok)) if (run.length > 1){
              const d = "M" + run.map(p=>X(p.x).toFixed(1) + "," + Y(p.hi).toFixed(1)).join("L") + "L"
                      + run.slice().reverse().map(p=>X(p.x).toFixed(1) + "," + Y(p.lo).toFixed(1)).join("L") + "Z";
              svgEl("path", {d, fill: col, "fill-opacity": 0.18, stroke: "none"}, body);
            }
          }
        }
        const ok = p=>okX(p.x) && okY(p.y), pts = (s.points || []).filter(ok);
        if (s.line) for (const run of runs(s.points || [], ok)) if (run.length > 1){
          svgEl("path", {d: "M" + run.map(p=>X(p.x).toFixed(1) + "," + Y(p.y).toFixed(1)).join("L"), fill: "none", stroke: col,
                         "stroke-width": (s.width || 1.8) * lw, "stroke-opacity": s.opacity == null ? 1 : s.opacity, "stroke-dasharray": s.dash || "none"}, body);
        }
        if (s.markers){
          for (const p of pts){
            const x = X(p.x), y = Y(p.y), r = (s.r || (s.hollow ? 3.2 : 2.8)) * ms;
            if (okY(p.lo2) && okY(p.hi2)) svgEl("line", {x1: x, x2: x, y1: Y(p.lo2), y2: Y(p.hi2), stroke: col, "stroke-width": 1.2 * lw, opacity: 0.8}, body);
            if (okY(p.lo) && okY(p.hi)) svgEl("line", {x1: x, x2: x, y1: Y(p.lo), y2: Y(p.hi), stroke: col, "stroke-width": (s.errWidth || 1.2) * lw}, body);
            if (s.shape === "diamond") svgEl("path", {d: "M" + x + "," + (y - r * 1.5) + "L" + (x + r * 1.5) + "," + y + "L" + x + "," + (y + r * 1.5) + "L" + (x - r * 1.5) + "," + y + "Z",
                                                      fill: s.hollow ? "var(--surface)" : col, stroke: col, "stroke-width": 1.6}, body);
            else svgEl("circle", {cx: x, cy: y, r, fill: s.hollow ? "var(--surface)" : col, stroke: col, "stroke-width": s.hollow ? 1.4 : 0}, body);
          }
        }
        if (s.hover !== false) for (const p of pts) out.marks.push({x: X(p.x), y: Y(p.y), s, p});
      }
      if (exporting && c.legend !== false){
        const seen = new Map(); for (const s of shown()) if (s.legend !== false && !seen.has(s.key)) seen.set(s.key, s);
        const items = [...seen.values()];
        if (items.length){
          const lh = fs + 6, wMax = Math.max(...items.map(s=>nameOf(s).length)) * fs * 0.62 + 26, lx = m.l + iw - wMax - 8, ly = m.t + 8;
          svgEl("rect", {x: lx - 6, y: ly - 4, width: wMax + 6, height: items.length * lh + 6, fill: "var(--surface)", "fill-opacity": 0.85, stroke: "var(--line)"}, s0);
          items.forEach((s, i)=>{ svgEl("rect", {x: lx, y: ly + i * lh + 2, width: fs, height: fs * 0.8, rx: 2, fill: colorOf(s)}, s0);
            text({x: lx + fs + 6, y: ly + i * lh + fs * 0.8}, nameOf(s), fs, "ct-label"); });
        }
      }
      return out;
    }
    function draw(){
      if (bx) bx.classList.toggle("on", !!xlog());
      if (by) by.classList.toggle("on", !!ylog());
      const W = Math.max(260, Math.round(plot.clientWidth || box.clientWidth || 600)), H = st.opts.height;
      plot.innerHTML = "";
      const r = render(plot, W, H, false);
      svg = r.svg; marks = r.marks; geom = r.geom;
      legend.hidden = C().legend === false;
      paintLegend();
      if (!geom) return;
      const {m, iw, ih, dom} = geom, [x0, x1, y0, y1] = dom;
      const ov = svgEl("rect", {x: m.l, y: m.t, width: iw, height: ih, class: "ct-ov" + (st.opts.onPick ? " pick" : "")}, svg);
      const ring = svgEl("circle", {r: 5.5, class: "ct-ring", visibility: "hidden"}, svg);
      const sel = svgEl("rect", {class: "ct-sel", visibility: "hidden"}, svg);
      const at = ev=>{ const b = svg.getBoundingClientRect(); return [(ev.clientX - b.left) * W / b.width, (ev.clientY - b.top) * H / b.height]; };
      const nearest = (px, py)=>{ let best = null, bd = 900; for (const k of marks){ const d = (k.x - px) ** 2 + (k.y - py) ** 2; if (d < bd){ bd = d; best = k; } } return best; };
      let start = null;
      ov.addEventListener("pointerdown", ev=>{ start = at(ev); try{ ov.setPointerCapture(ev.pointerId); }catch(e){} });
      ov.addEventListener("pointermove", ev=>{
        const [px, py] = at(ev);
        if (start){
          const [sx, sy] = start;
          sel.setAttribute("x", Math.min(sx, px)); sel.setAttribute("y", Math.min(sy, py));
          sel.setAttribute("width", Math.abs(px - sx)); sel.setAttribute("height", Math.abs(py - sy));
          sel.setAttribute("visibility", "visible"); tip.hidden = true; ring.setAttribute("visibility", "hidden"); return;
        }
        const best = nearest(px, py);
        if (!best){ tip.hidden = true; ring.setAttribute("visibility", "hidden"); return; }
        ring.setAttribute("cx", best.x); ring.setAttribute("cy", best.y); ring.style.stroke = colorOf(best.s);
        ring.setAttribute("visibility", "visible");
        const p = best.p, xl = C().xlabel || st.opts.xlabel || "x", yl = C().ylabel || st.opts.ylabel || "y";
        const lines = [best.s.name].concat(p.info && p.info.length && st.opts.infoOnly ? p.info
          : [xl + " = " + fmtVal(p.x), yl + " = " + fmtVal(p.y) + (isNum(p.lo) && isNum(p.hi) ? "  [" + fmtVal(p.lo) + ", " + fmtVal(p.hi) + "]" : "")].concat(p.info || []));
        tip.innerHTML = lines.map((t, i)=>i ? esc(t) : '<b>' + esc(t) + '</b>').join("<br>");
        tip.hidden = false;
        const b = svg.getBoundingClientRect(), hb = box.getBoundingClientRect();
        const left = b.left - hb.left + best.x * b.width / W, top = b.top - hb.top + best.y * b.height / H;
        tip.style.left = Math.max(4, Math.min(left + 12, hb.width - tip.offsetWidth - 4)) + "px";
        tip.style.top = Math.max(4, top - tip.offsetHeight - 10) + "px";
      });
      ov.addEventListener("pointerup", ev=>{
        if (!start) return;
        const [sx, sy] = start, [px, py] = at(ev);
        start = null; sel.setAttribute("visibility", "hidden");
        if (Math.abs(px - sx) < 8 && Math.abs(py - sy) < 8){ const best = nearest(px, py); if (best && st.opts.onPick) st.opts.onPick(best.p, best.s); return; }
        if (Math.abs(px - sx) < 8 || Math.abs(py - sy) < 8) return;
        const ix = v=>x0 + (v - m.l) / iw * (x1 - x0), iy = v=>y0 + (m.t + ih - v) / ih * (y1 - y0);
        st.view = [ix(Math.min(sx, px)), ix(Math.max(sx, px)), iy(Math.max(sy, py)), iy(Math.min(sy, py))];
        draw();
      });
      ov.addEventListener("pointercancel", ()=>{ start = null; sel.setAttribute("visibility", "hidden"); });
      ov.addEventListener("pointerleave", ()=>{ if (!start){ tip.hidden = true; ring.setAttribute("visibility", "hidden"); } });
      ov.addEventListener("dblclick", ()=>{ st.view = null; draw(); });
    }
    function paintLegend(){
      const seen = new Map();
      for (const s of st.series) if (s.legend !== false && !seen.has(s.key) && !(C().off || {})[s.key]) seen.set(s.key, s);
      legend.innerHTML = "";
      for (const [key, s] of seen){
        const item = document.createElement("span");
        item.className = "lg" + (st.hidden.has(key) ? " off" : "");
        item.title = "Click: show or hide. Double-click: only this one.";
        item.innerHTML = '<i style="background:' + esc(colorOf(s)) + '"></i>' + esc(nameOf(s));
        item.onclick = ()=>{ if (st.hidden.has(key)) st.hidden.delete(key); else st.hidden.add(key); draw(); };
        item.ondblclick = ()=>{ st.hidden = new Set([...seen.keys()].filter(k=>k !== key)); draw(); };
        legend.appendChild(item);
      }
    }
    /* the customize panel */
    function paintCustom(){
      const c = C(), keys = [], seen = new Set();
      for (const s of st.series) if (s.legend !== false && !seen.has(s.key)){ seen.add(s.key); keys.push(s); }
      const val = v=>v == null ? "" : esc(v);
      const field = (k, label, type, extra)=>'<label>' + esc(label) + '<input data-k="' + k + '" type="' + type + '" value="' + val(c[k]) + '"' + (extra || "") + '></label>';
      const check = (k, label, on)=>'<label class="ck"><input data-k="' + k + '" type="checkbox"' + (on ? " checked" : "") + '> ' + esc(label) + '</label>';
      const W = Math.max(260, Math.round(plot.clientWidth || 600));
      cust.innerHTML = '<div class="cc-grid">'
        + field("title", "Title", "text", ' placeholder="' + esc(st.opts.title || "") + '"')
        + field("xlabel", "x label", "text", ' placeholder="' + esc(st.opts.xlabel || "") + '"')
        + field("ylabel", "y label", "text", ' placeholder="' + esc(st.opts.ylabel || "") + '"')
        + field("xmin", "x min", "number", ' step="any" placeholder="auto"') + field("xmax", "x max", "number", ' step="any" placeholder="auto"')
        + field("ymin", "y min", "number", ' step="any" placeholder="auto"') + field("ymax", "y max", "number", ' step="any" placeholder="auto"')
        + field("font", "font size", "number", ' min="8" max="28" step="1" placeholder="11"')
        + field("lw", "line width ×", "number", ' min="0.25" max="5" step="0.25" placeholder="1"')
        + field("ms", "marker size ×", "number", ' min="0.25" max="5" step="0.25" placeholder="1"')
        + '</div><div class="cc-row">' + check("xlog", "log x", xlog()) + check("ylog", "log y", ylog())
        + check("legend", "legend", c.legend !== false) + check("grid", "grid", c.grid !== false) + '</div>'
        + (keys.length ? '<div class="cc-series">' + keys.map(s=>'<label class="cc-s"><input type="checkbox" data-on="' + esc(s.key) + '"' + ((c.off || {})[s.key] ? "" : " checked") + '>'
          + '<input type="color" data-color="' + esc(s.key) + '" value="' + toHex(colorOf(s), box) + '">'
          + '<input type="text" data-name="' + esc(s.key) + '" value="' + esc(nameOf(s)) + '"></label>').join("") + '</div>' : "")
        + '<div class="cc-row">' + field("exportW", "export width px", "number", ' min="200" max="6000" step="10" placeholder="' + W + '"')
        + field("exportH", "export height px", "number", ' min="150" max="6000" step="10" placeholder="' + st.opts.height + '"')
        + '<label>PNG scale<select data-k="scale">' + [1, 2, 3, 4].map(v=>'<option' + ((+c.scale || 2) === v ? " selected" : "") + '>' + v + '</option>').join("") + '</select></label>'
        + '<label>background<select data-k="bg">' + [["theme", "as on screen"], ["white", "white (print)"], ["transparent", "transparent"]]
          .map(o=>'<option value="' + o[0] + '"' + ((c.bg || "white") === o[0] ? " selected" : "") + '>' + o[1] + '</option>').join("") + '</select></label>'
        + '<button type="button" class="cb" data-act="reset">reset customizations</button></div>'
        + '<div class="cc-hint">Changes apply at once and are kept in this browser for this chart. PNG and SVG use the export size, scale and background.</div>';
      const num = v=>v === "" ? null : +v;
      cust.querySelectorAll("[data-k]").forEach(el=>{
        const k = el.dataset.k;
        el.oninput = el.onchange = ()=>{
          if (el.type === "checkbox") c[k] = el.checked;
          else if (el.type === "number") c[k] = num(el.value);
          else c[k] = el.value;
          if (k === "xlog" || k === "ylog") st.view = null;
          saveCustom(st.opts.name, c); draw();
        };
      });
      cust.querySelectorAll("[data-color]").forEach(el=>el.oninput = ()=>{ (c.colors = c.colors || {})[el.dataset.color] = el.value; saveCustom(st.opts.name, c); draw(); });
      cust.querySelectorAll("[data-name]").forEach(el=>el.oninput = ()=>{ (c.names = c.names || {})[el.dataset.name] = el.value; saveCustom(st.opts.name, c); draw(); });
      cust.querySelectorAll("[data-on]").forEach(el=>el.onchange = ()=>{ (c.off = c.off || {})[el.dataset.on] = !el.checked; saveCustom(st.opts.name, c); draw(); });
      cust.querySelector('[data-act="reset"]').onclick = ()=>{ st.custom = {}; saveCustom(st.opts.name, {}); st.view = null; draw(); paintCustom(); };
    }
    /* exports: the figure as customized, drawn afresh at the export size */
    function svgString(){
      const c = C(), cs = getComputedStyle(box), v = n=>cs.getPropertyValue(n).trim();
      const W = +c.exportW || Math.max(260, Math.round(plot.clientWidth || 600)), H = +c.exportH || st.opts.height;
      const holder = document.createElement("div"); holder.style.cssText = "position:absolute;left:-100000px;top:0;";
      box.appendChild(holder);
      const r = render(holder, W, H, true), s = r.svg;
      const light = (c.bg || "white") !== "theme";
      const fg = light ? "#1f2937" : v("--fg"), muted = light ? "#4b5563" : v("--muted"), line = light ? "#d1d5db" : v("--line"), surface = light ? "#ffffff" : v("--surface");
      const style = svgEl("style", {});
      style.textContent = ".ct-axis text,.ct-reftext{font-family:Helvetica,Arial,sans-serif;fill:" + muted + "}.ct-label{font-family:Helvetica,Arial,sans-serif;fill:" + fg + "}"
        + ".ct-title{font-weight:600}.ct-grid{stroke:" + line + ";stroke-width:1;opacity:.7}.ct-frame{fill:none;stroke:" + line + "}.ct-ref{stroke:" + muted + ";stroke-dasharray:4 3}";
      s.insertBefore(style, s.firstChild);
      if ((c.bg || "white") !== "transparent") s.insertBefore(svgEl("rect", {width: "100%", height: "100%", fill: surface}), style.nextSibling);
      let text = new XMLSerializer().serializeToString(s);
      holder.remove();
      const vars = {"--surface": surface, "--fg": fg, "--muted": muted, "--line": line};
      return text.replace(/var\((--[A-Za-z0-9-]+)\)/g, (_, n)=>vars[n] || v(n) || "#888");
    }
    function exportPng(){
      const c = C(), scale = +c.scale || 2, text = svgString();
      const W = +c.exportW || Math.max(260, Math.round(plot.clientWidth || 600)), H = +c.exportH || st.opts.height;
      const img = new Image();
      img.onload = ()=>{
        const cv = document.createElement("canvas"); cv.width = Math.round(W * scale); cv.height = Math.round(H * scale);
        const ctx = cv.getContext("2d"); ctx.scale(scale, scale); ctx.drawImage(img, 0, 0, W, H);
        cv.toBlob(b=>{ if (b) download(b, "image/png", fileName() + ".png"); }, "image/png");
      };
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(text);
    }
    function csv(){
      const q = v=>'"' + String(v == null ? "" : v).replace(/"/g, '""') + '"';
      const xl = C().xlabel || st.opts.xlabel || "x", yl = C().ylabel || st.opts.ylabel || "y";
      const rows = [["series", xl, yl, "lower", "upper"].map(q).join(",")];
      for (const s of shown()) for (const p of s.points || []) rows.push([nameOf(s) === s.key ? s.name : nameOf(s), p.x, p.y, p.lo, p.hi].map(q).join(","));
      return rows.join("\n");
    }
    let frame = 0;
    if (window.ResizeObserver) new ResizeObserver(()=>{ cancelAnimationFrame(frame); frame = requestAnimationFrame(()=>{ if (box.isConnected) draw(); }); }).observe(plot);
    return {
      set(series, o){
        if (o && o.name && o.name !== st.opts.name){ st.custom = loadCustom(o.name); }
        st.series = series; Object.assign(st.opts, o || {}); draw();
        if (!cust.hidden) paintCustom();
      },
      reset(){ st.view = null; st.hidden.clear(); },
      resetZoom(){ st.view = null; },
      get opts(){ return st.opts; },
    };
  }
