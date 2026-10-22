/* Foreclosure Map — front end (plain JS + Leaflet). */
(() => {
  "use strict";

  // ---------- helpers ----------
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt$ = (v) => (v == null ? "—" : (v < 0 ? "-$" : "$") + Math.abs(Math.round(v)).toLocaleString("en-US"));
  const short$ = (v) => {
    if (v == null) return "—";
    const a = Math.abs(v), s = v < 0 ? "-$" : "$";
    if (a >= 999500) return s + (a / 1e6).toFixed(a >= 1e7 ? 0 : 2).replace(/\.0+$|(\.\d*?)0+$/, "$1") + "M";
    if (a >= 1e3) return s + Math.round(a / 1e3) + "K";
    return s + a;
  };
  const num = (v) => (v == null ? "—" : Number(v).toLocaleString("en-US"));
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
  };
  const api = async (url, opts = {}) => {
    const r = await fetch(url, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  };
  const toast = (msg, ms = 4000) => {
    const t = $("#toast"); t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
  };

  // Equity buckets drive every color in the UI.
  const eqClass = (r) => {
    if (r.margin == null) return "none";
    if (r.margin >= 50) return "strong";
    if (r.margin >= 15) return "pos";
    if (r.margin >= 0) return "thin";
    return "neg";
  };
  const EQ_LABEL = { strong: "Equity ≥ 50%", pos: "Equity 15–49%", thin: "Equity 0–14%", neg: "Negative equity", none: "No loan data (HOA etc.)" };
  const EQ_COLOR = { strong: "#0e8a4f", pos: "#3aa66b", thin: "#c98a12", neg: "#d6453d", none: "#7c8594" };

  // "10-1" -> "10 AM – 1 PM" (3-hour window when the trustee may sell)
  const hourLabel = (h) => (h >= 8 && h <= 11 ? `${h} AM` : `${h} PM`);
  const saleWindow = (t) => {
    const m = /^(\d{1,2})-(\d{1,2})$/.exec(t || "");
    return m ? `${hourLabel(+m[1])} – ${hourLabel(+m[2])}` : t || "—";
  };
  // Texas foreclosure sales happen on the first Tuesday of the month.
  const firstTuesday = (month) => {
    const m = /^(\d{1,2})\/(\d{4})$/.exec(month || "");
    if (!m) return null;
    const d = new Date(+m[2], +m[1] - 1, 1);
    while (d.getDay() !== 2) d.setDate(d.getDate() + 1);
    return d;
  };
  const monthName = (month) => {
    const m = /^(\d{1,2})\/(\d{4})$/.exec(month || "");
    return m ? new Date(+m[2], +m[1] - 1, 1).toLocaleDateString("en-US", { month: "long", year: "numeric" }) : "";
  };
  const titleCase = (s) => (s || "").toLowerCase().replace(/\b([a-z])/g, (c) => c.toUpperCase());
  const beds = (r) => (r.beds ? r.beds : null);
  const baths = (r) => (r.baths ? r.baths : null);

  // ---------- state ----------
  const DEFAULT_FILTERS = { city: [], type: [], loan: [], eqMin: null, eqMax: null, valMin: null, valMax: null, status: [], q: "", needsCheck: false };
  const S = {
    books: [], book: null, rows: [], filtered: [],
    filters: { ...DEFAULT_FILTERS, ...store.get("fm.filters", {}) },
    sort: store.get("fm.sort", "id"), inView: store.get("fm.inView", false),
    hot: null, selected: null, markers: new Map(),
  };
  S.filters.q = ""; S.filters.needsCheck = false;

  // ---------- map ----------
  let map, layer, legend;
  function initMap() {
    if (map) return;
    map = L.map("map", { zoomControl: true, preferCanvas: false }).setView([30.27, -97.74], 10);
    const streets = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    });
    const satellite = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 19, attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics",
    });
    const baseName = store.get("fm.base", "Streets");
    const bases = { Streets: streets, Satellite: satellite };
    (bases[baseName] || streets).addTo(map);
    L.control.layers(bases, null, { position: "topright" }).addTo(map);
    map.on("baselayerchange", (e) => store.set("fm.base", e.name));
    layer = L.layerGroup().addTo(map);
    legend = L.control({ position: "bottomleft" });
    legend.onAdd = () => {
      const d = L.DomUtil.create("div", "legend");
      d.innerHTML = Object.keys(EQ_LABEL).map((k) => `<div><i style="background:${EQ_COLOR[k]}"></i>${EQ_LABEL[k]}</div>`).join("");
      return d;
    };
    legend.addTo(map);
    map.on("zoomend", refreshIcons);
    map.on("moveend", () => { if (S.inView) renderList(); });
  }

  const pinMode = () => (map.getZoom() >= 12 ? "pin" : "dot");
  function iconFor(r) {
    const cls = ["p-" + eqClass(r)];
    if (r.status === "Sold") cls.push("sold");
    if (r.status === "Unsold") cls.push("unsold");
    if (r.geoMatch === "approximate") cls.push("approx");
    if (S.hot === r.seq || S.selected === r.seq) cls.push("hot");
    if (pinMode() === "dot") {
      return L.divIcon({ className: "dot-icon", html: `<div class="dot ${cls.join(" ")}"></div>`, iconSize: [0, 0] });
    }
    const label = r.status === "Sold" && r.soldPrice ? "Sold " + short$(r.soldPrice) : short$(r.assessed);
    return L.divIcon({ className: "pin-icon", html: `<div class="pin ${cls.join(" ")}">${label}</div>`, iconSize: [0, 0] });
  }

  function hoverCard(r) {
    const k = eqClass(r);
    return `<div class="hc">
      <div class="hc-top" style="background:${EQ_COLOR[k]}"><span>#${r.id} · ${r.margin != null ? r.margin + "% equity" : "HOA / no loan"}</span><span>${r.equity != null ? short$(r.equity) : ""}</span></div>
      <div class="hc-body">
        <div class="v">${fmt$(r.assessed)} <span class="muted" style="font-size:11px;font-weight:500">assessed</span></div>
        <div><b>${esc(titleCase(r.street))}</b>, ${esc(titleCase(r.city))} ${esc(r.zip)}</div>
        <div class="muted">${[beds(r) && beds(r) + " bd", baths(r) && baths(r) + " ba", r.sqft && num(r.sqft) + " sqft", r.yearBuilt && "Built " + r.yearBuilt].filter(Boolean).join(" · ")}</div>
        <div>Owed ≈ ${fmt$(r.estBalance)} · ${esc(r.loanType || "—")}</div>
        <div class="muted">Sale ${saleWindow(r.saleTime)}${r.status ? " · <b>" + esc(r.status) + (r.soldPrice ? " " + fmt$(r.soldPrice) : "") + "</b>" : ""}</div>
      </div></div>`;
  }

  function buildMarkers() {
    layer.clearLayers(); S.markers.clear();
    for (const r of S.rows) {
      if (r.lat == null) continue;
      const m = L.marker([r.lat, r.lon], { icon: iconFor(r), riseOnHover: true, keyboard: false });
      m.bindTooltip(() => hoverCard(r), { direction: "top", offset: [0, -28], className: "hover-card", opacity: 1 });
      m.on("mouseover", () => setHot(r.seq, "map"));
      m.on("mouseout", () => setHot(null, "map"));
      m.on("click", () => openDrawer(r.seq, true));
      S.markers.set(r.seq, m);
    }
  }
  function refreshIcons() {
    for (const [seq, m] of S.markers) {
      const r = S.rows.find((x) => x.seq === seq);
      m.setIcon(iconFor(r));
    }
  }
  function syncMarkerVisibility() {
    const show = new Set(S.filtered.map((r) => r.seq));
    for (const [seq, m] of S.markers) {
      const on = layer.hasLayer(m);
      if (show.has(seq) && !on) layer.addLayer(m);
      if (!show.has(seq) && on) layer.removeLayer(m);
    }
  }
  function fitToRows(rows) {
    const pts = rows.filter((r) => r.lat != null).map((r) => [r.lat, r.lon]);
    if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.08), { maxZoom: 14 });
  }

  function setHot(seq, from) {
    if (S.hot === seq) return;
    const prev = S.hot; S.hot = seq;
    for (const s of [prev, seq]) {
      if (s == null) continue;
      const m = S.markers.get(s), r = S.rows.find((x) => x.seq === s);
      if (m) { m.setIcon(iconFor(r)); m.setZIndexOffset(s === seq ? 1000 : 0); }
      const card = $(`.card[data-seq="${s}"]`);
      if (card) card.classList.toggle("hot", s === seq);
    }
    if (from === "list") {
      const m = seq != null && S.markers.get(seq);
      if (prev != null && S.markers.get(prev)) S.markers.get(prev).closeTooltip();
      if (m && layer.hasLayer(m)) m.openTooltip();
    }
  }

  // ---------- filtering ----------
  function applyFilters() {
    const f = S.filters, q = f.q.trim().toUpperCase();
    let rows = S.rows.filter((r) => {
      if (f.city.length && !f.city.includes(r.city)) return false;
      if (f.type.length && !f.type.includes(r.propertyClass || "Unknown")) return false;
      if (f.loan.length && !f.loan.includes(r.loanType || "Unknown")) return false;
      if (f.eqMin != null && (r.margin == null || r.margin < f.eqMin)) return false;
      if (f.eqMax != null && (r.margin == null || r.margin > f.eqMax)) return false;
      if (f.valMin != null && (r.assessed ?? 0) < f.valMin) return false;
      if (f.valMax != null && (r.assessed ?? 0) > f.valMax) return false;
      if (f.status.length && !f.status.includes(r.status || "No result")) return false;
      if (f.needsCheck && !(r.warnings?.length || r.geoMatch !== "exact")) return false;
      if (q) {
        const hay = [r.street, r.city, r.zip, r.mortgagor, r.mortgagee, r.trustee, r.legal, r.id && "#" + r.id].join(" ").toUpperCase();
        if (!q.split(/\s+/).every((w) => hay.includes(w))) return false;
      }
      return true;
    });
    const key = {
      id: (r) => r.id, "equity-desc": (r) => -(r.equity ?? -Infinity), "margin-desc": (r) => -(r.margin ?? -Infinity),
      "assessed-desc": (r) => -(r.assessed ?? 0), "assessed-asc": (r) => r.assessed ?? Infinity,
      "balance-desc": (r) => -(r.estBalance ?? -Infinity), "sold-desc": (r) => -(r.soldPrice ?? -Infinity),
    }[S.sort] || ((r) => r.id);
    rows.sort((a, b) => key(a) - key(b) || a.id - b.id);
    S.filtered = rows;
    store.set("fm.filters", { ...S.filters, q: "", needsCheck: false });
    store.set("fm.sort", S.sort);
    syncMarkerVisibility();
    renderChips();
    renderList();
  }

  function listRows() {
    if (!S.inView || !map) return S.filtered;
    const b = map.getBounds();
    return S.filtered.filter((r) => r.lat != null && b.contains([r.lat, r.lon]));
  }

  // ---------- list ----------
  function cardHTML(r) {
    const k = eqClass(r);
    const tags = [`<span class="tag">#${r.id}</span>`];
    if (r.loanType) tags.push(`<span class="tag dark">${esc(r.loanType)}</span>`);
    if (r.geoMatch !== "exact") tags.push(`<span class="tag warn">${r.lat == null ? "Not on map" : "Approx. location"}</span>`);
    if (r.warnings?.length) tags.push(`<span class="tag warn" title="${esc(r.warnings.join("; "))}">Check</span>`);
    const facts = [
      beds(r) != null && `<span><b>${beds(r)}</b> bd</span>`,
      baths(r) != null && `<span><b>${baths(r)}</b> ba</span>`,
      r.sqft && `<span><b>${num(r.sqft)}</b> sqft</span>`,
      r.yearBuilt && `<span>Built ${r.yearBuilt}</span>`,
    ].filter(Boolean).join("");
    let sold = "";
    if (r.status === "Sold") sold = `<div class="sold-line sold">Sold${r.soldPrice ? " " + fmt$(r.soldPrice) : ""}</div>`;
    else if (r.status) sold = `<div class="sold-line unsold">${esc(r.status)}</div>`;
    return `<div class="card${S.selected === r.seq ? " selected" : ""}" data-seq="${r.seq}" tabindex="0">
      <div class="card-top eq-${k}">
        <div class="tags">${tags.join("")}</div>
        <div class="equity-big">${r.equity != null ? (r.equity >= 0 ? "+" : "") + fmt$(r.equity) + `<small>${r.margin}% equity</small>` : "No loan data"}</div>
      </div>
      <div class="card-body">
        <div class="card-price">${fmt$(r.assessed)}<span class="lbl">assessed</span></div>
        <div class="card-facts">${facts || '<span class="muted">' + esc(r.propertyClass || "") + "</span>"}</div>
        <div class="card-addr">${esc(titleCase(r.street))}, ${esc(titleCase(r.city))}, TX ${esc(r.zip)}</div>
        <div class="card-meta"><span>Owed ≈ ${short$(r.estBalance)}</span><span>${esc(titleCase(r.propertyClass || ""))}</span><span>Sale ${saleWindow(r.saleTime)}</span></div>
        ${sold}
      </div></div>`;
  }

  function renderList() {
    const rows = listRows();
    const el = $("#cards");
    el.innerHTML = rows.length ? rows.map(cardHTML).join("") : `<div class="empty muted">No postings match these filters.</div>`;
    const m = S.book.meta;
    const total = S.rows.length;
    $("#book-sub").textContent = `${rows.length === total ? total : rows.length + " of " + total} postings` +
      (S.inView ? " in map area" : "") + (m.month ? ` · sale ${firstTuesday(m.month)?.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric" })}` : "");
  }

  function renderNotices() {
    const check = S.rows.filter((r) => r.warnings?.length).length;
    const approx = S.rows.filter((r) => r.lat != null && r.geoMatch !== "exact").length;
    const none = S.rows.filter((r) => r.lat == null).length;
    const parts = [];
    if (check) parts.push(`${check} with numbers to double-check`);
    if (approx) parts.push(`${approx} with approximate map location`);
    if (none) parts.push(`${none} not on the map`);
    const m = S.book.meta;
    let html = "";
    if (m.postedCount && m.postedCount !== S.rows.length) {
      html += `<div class="notice">The book says ${m.postedCount} postings, but ${S.rows.length} rows were read.</div>`;
    }
    if (parts.length) {
      html += `<div class="notice">${parts.join(" · ")}. <button id="show-check">${S.filters.needsCheck ? "Show all" : "Show them"}</button></div>`;
    }
    $("#notices").innerHTML = html;
    const b = $("#show-check");
    if (b) b.onclick = () => { S.filters.needsCheck = !S.filters.needsCheck; renderNotices(); applyFilters(); };
  }

  // ---------- filter chips ----------
  const counts = (fn) => {
    const c = new Map();
    for (const r of S.rows) { const k = fn(r); c.set(k, (c.get(k) || 0) + 1); }
    return [...c.entries()].sort((a, b) => b[1] - a[1]);
  };
  function checklist(key, fn) {
    const sel = S.filters[key];
    return counts(fn).map(([v, n]) => `<label class="opt"><input type="checkbox" value="${esc(v)}" ${sel.includes(v) ? "checked" : ""}> ${esc(titleCase(v) || v)}<span class="n">${n}</span></label>`).join("") +
      `<div class="pop-actions"><button data-act="clear">Clear</button><button data-act="done">Done</button></div>`;
  }
  const POPS = {
    city: () => checklist("city", (r) => r.city || "Unknown"),
    type: () => checklist("type", (r) => r.propertyClass || "Unknown"),
    loan: () => checklist("loan", (r) => r.loanType || "Unknown"),
    status: () => checklist("status", (r) => r.status || "No result"),
    equity: () => `<h4>Equity margin (% of assessed value)</h4>
      <div class="range"><input type="number" data-k="eqMin" placeholder="Min %" value="${S.filters.eqMin ?? ""}"> – <input type="number" data-k="eqMax" placeholder="Max %" value="${S.filters.eqMax ?? ""}"></div>
      <div class="presets"><button data-p="0,">Positive</button><button data-p="20,">20%+</button><button data-p="50,">50%+</button><button data-p=",-1">Underwater</button></div>
      <div class="pop-actions"><button data-act="clear">Clear</button><button data-act="done">Done</button></div>`,
    value: () => `<h4>Assessed value</h4>
      <div class="range"><input type="number" data-k="valMin" placeholder="Min $" step="10000" value="${S.filters.valMin ?? ""}"> – <input type="number" data-k="valMax" placeholder="Max $" step="10000" value="${S.filters.valMax ?? ""}"></div>
      <div class="presets"><button data-p=",300000">Under $300K</button><button data-p="300000,600000">$300K–600K</button><button data-p="600000,">$600K+</button></div>
      <div class="pop-actions"><button data-act="clear">Clear</button><button data-act="done">Done</button></div>`,
  };
  const RANGE_KEYS = { equity: ["eqMin", "eqMax"], value: ["valMin", "valMax"] };

  function renderChips() {
    const f = S.filters;
    const n = { city: f.city.length, type: f.type.length, loan: f.loan.length, status: f.status.length,
      equity: (f.eqMin != null) + (f.eqMax != null), value: (f.valMin != null) + (f.valMax != null) };
    let any = false;
    for (const w of $$(".chip-wrap")) {
      const k = w.dataset.filter, c = n[k];
      $(".chip-count", w).textContent = c ? (RANGE_KEYS[k] ? "✓" : c) : "";
      $(".chip", w).classList.toggle("active", !!c);
      if (c) any = true;
    }
    $("#btn-reset").hidden = !any;
  }

  function wireFilters() {
    for (const w of $$(".chip-wrap")) {
      const k = w.dataset.filter, pop = $(".pop", w);
      $(".chip", w).addEventListener("click", (e) => {
        e.stopPropagation();
        const open = w.classList.contains("open");
        $$(".chip-wrap.open").forEach((x) => x.classList.remove("open"));
        if (!open) { pop.innerHTML = POPS[k](); w.classList.add("open"); }
      });
      pop.addEventListener("click", (e) => e.stopPropagation());
      pop.addEventListener("change", (e) => {
        if (e.target.type === "checkbox") {
          S.filters[k] = $$("input[type=checkbox]:checked", pop).map((i) => i.value);
          applyFilters();
        }
      });
      pop.addEventListener("input", (e) => {
        if (e.target.dataset.k) {
          const v = e.target.value.trim();
          S.filters[e.target.dataset.k] = v === "" ? null : Number(v);
          clearTimeout(pop._t); pop._t = setTimeout(applyFilters, 250);
        }
      });
      pop.addEventListener("click", (e) => {
        const act = e.target.dataset.act, p = e.target.dataset.p;
        if (act === "done") w.classList.remove("open");
        if (act === "clear") {
          if (RANGE_KEYS[k]) RANGE_KEYS[k].forEach((x) => (S.filters[x] = null)); else S.filters[k] = [];
          pop.innerHTML = POPS[k](); applyFilters();
        }
        if (p != null) {
          const [a, b] = p.split(",");
          const [ka, kb] = RANGE_KEYS[k];
          S.filters[ka] = a === "" ? null : Number(a); S.filters[kb] = b === "" ? null : Number(b);
          pop.innerHTML = POPS[k](); applyFilters();
        }
      });
    }
    document.addEventListener("click", () => $$(".chip-wrap.open").forEach((x) => x.classList.remove("open")));
    $("#btn-reset").onclick = () => { S.filters = { ...DEFAULT_FILTERS, q: S.filters.q }; applyFilters(); renderNotices(); };
    $("#sort").onchange = (e) => { S.sort = e.target.value; applyFilters(); };
    $("#search").addEventListener("input", (e) => { S.filters.q = e.target.value; clearTimeout(wireFilters._t); wireFilters._t = setTimeout(applyFilters, 150); });
    $("#in-view").onchange = (e) => { S.inView = e.target.checked; store.set("fm.inView", S.inView); renderList(); };

    const cards = $("#cards");
    cards.addEventListener("mouseover", (e) => { const c = e.target.closest(".card"); if (c) setHot(+c.dataset.seq, "list"); });
    cards.addEventListener("mouseleave", () => setHot(null, "list"));
    cards.addEventListener("click", (e) => { const c = e.target.closest(".card"); if (c) openDrawer(+c.dataset.seq, false); });
    cards.addEventListener("keydown", (e) => { if (e.key === "Enter") { const c = e.target.closest(".card"); if (c) openDrawer(+c.dataset.seq, false); } });
  }

  // ---------- detail drawer ----------
  function kv(pairs) {
    return `<div class="kv">${pairs.filter(([, v]) => v != null && v !== "").map(([k, v]) => `<div>${k}</div><div>${v}</div>`).join("")}</div>`;
  }
  function openDrawer(seq, fromMap) {
    const r = S.rows.find((x) => x.seq === seq);
    if (!r) return;
    const prevSel = S.selected;
    S.selected = seq;
    for (const s of [prevSel, seq]) {
      const m = s != null && S.markers.get(s);
      if (m) m.setIcon(iconFor(S.rows.find((x) => x.seq === s)));
      const c = $(`.card[data-seq="${s}"]`); if (c) c.classList.toggle("selected", s === seq);
    }
    if (fromMap) { const c = $(`.card[data-seq="${seq}"]`); if (c) c.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
    else if (r.lat != null && !map.getBounds().contains([r.lat, r.lon])) map.panTo([r.lat, r.lon]);

    const k = eqClass(r), m = S.book.meta;
    const addr = `${r.street}, ${r.city}, TX ${r.zip}`;
    const q = encodeURIComponent(addr);
    const zillowSlug = `${r.street} ${r.city} TX ${r.zip}`.replace(/[^A-Za-z0-9 ]/g, "").trim().replace(/\s+/g, "-");
    const sale = firstTuesday(m.month);
    const tags = [`<span class="tag">#${r.id}</span>`, r.status && `<span class="tag dark">${esc(r.status)}</span>`,
      r.geoMatch !== "exact" && `<span class="tag warn">${r.lat == null ? "Not located" : "Approximate location"}</span>`].filter(Boolean);
    const list = listRows(), idx = list.findIndex((x) => x.seq === seq);
    const statuses = ["", "Sold", "Unsold", "Cancelled", "Postponed"];

    $("#drawer").innerHTML = `
      <div class="d-head">
        <button class="icon-btn" data-nav="-1" title="Previous (←)" ${idx <= 0 ? "disabled" : ""}>‹</button>
        <button class="icon-btn" data-nav="1" title="Next (→)" ${idx < 0 || idx >= list.length - 1 ? "disabled" : ""}>›</button>
        <span class="nav-hint">${idx >= 0 ? `${idx + 1} of ${list.length}` : ""}</span>
        <div class="grow"></div>
        <button class="icon-btn" data-close title="Close (Esc)">✕</button>
      </div>
      <div class="d-hero eq-${k}">
        <div class="tags">${tags.join("")}</div>
        <h2>${esc(titleCase(r.street))}</h2>
        <div class="sub">${esc(titleCase(r.city))}, TX ${esc(r.zip)} · ${esc(titleCase(r.propertyClass || ""))}${r.yearBuilt ? " · Built " + r.yearBuilt : ""}</div>
      </div>
      <div class="d-body">
        <div class="stats">
          <div class="stat"><div class="k">Assessed value</div><div class="v">${fmt$(r.assessed)}</div></div>
          <div class="stat"><div class="k">Est. equity</div><div class="v ${r.equity == null ? "" : r.equity >= 0 ? "pos" : "neg"}">${r.equity == null ? "—" : fmt$(r.equity) + ` <small>(${r.margin}%)</small>`}</div></div>
          <div class="stat"><div class="k">Est. unpaid balance</div><div class="v">${fmt$(r.estBalance)}</div></div>
          <div class="stat"><div class="k">Original loan</div><div class="v">${fmt$(r.origAmount)}</div></div>
          <div class="stat"><div class="k">Monthly P&amp;I</div><div class="v">${r.monthlyPayment ? fmt$(r.monthlyPayment) : "—"}</div></div>
          <div class="stat"><div class="k">Sale window</div><div class="v" style="font-size:15px">${sale ? sale.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" }) + "<br>" : ""}${saleWindow(r.saleTime)}</div></div>
        </div>
        <div class="links">
          <a href="https://www.zillow.com/homes/${encodeURIComponent(zillowSlug)}_rb/" target="_blank" rel="noopener">Zillow</a>
          <a href="https://www.google.com/maps/search/?api=1&query=${q}" target="_blank" rel="noopener">Google Maps</a>
          ${r.lat != null ? `<a href="https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=${r.lat},${r.lon}" target="_blank" rel="noopener">Street View</a>` : ""}
          <a href="https://www.google.com/search?q=${encodeURIComponent(addr + " " + (m.county || "") + " CAD")}" target="_blank" rel="noopener">Appraisal district</a>
        </div>
        ${r.warnings?.length ? `<div class="section"><div class="warnbox"><b>Double-check against the PDF (page ${r.page}):</b> ${esc(r.warnings.join("; "))}</div></div>` : ""}
        ${r.comments?.length ? `<div class="section"><h3>Comments</h3>${r.comments.map((c) => `<div class="comment">${esc(c)}</div>`).join("")}</div>` : ""}
        <div class="section"><h3>Owner &amp; lender</h3>${kv([
          ["Mortgagor (owner)", esc(r.mortgagor)], ["Mortgagee (lender)", esc(r.mortgagee)], ["Trustee", esc(r.trustee)]])}</div>
        <div class="section"><h3>Property</h3>${kv([
          ["Type", esc(r.propertyClass)], ["Year built", r.yearBuilt], ["Units", r.units],
          ["Living area", r.sqft ? num(r.sqft) + " sqft" : null],
          ["Beds / baths / garage", [r.beds, r.baths, r.garage].some((x) => x != null) ? `${r.beds ?? "—"} / ${r.baths ?? "—"} / ${r.garage ?? "—"}` : null],
          ["Pool", { Y: "Yes", P: "Yes (P)", S: "Spa (S)", N: "No" }[r.pool] || r.pool],
          ["Legal description", esc(r.legal)]])}</div>
        <div class="section"><h3>Loan</h3>${kv([
          ["Loan date", r.loanDate], ["Interest rate", r.rate != null ? r.rate + "%" : null], ["Loan type", esc(r.loanType)],
          ["Original amount", fmt$(r.origAmount)], ["Est. unpaid balance", fmt$(r.estBalance)],
          ["Monthly payment", r.monthlyPayment ? fmt$(r.monthlyPayment) : null], ["Matures", r.maturity],
          ["Doc # (Vol/Pg)", esc(r.docNumber)]])}</div>
        <div class="section"><h3>Auction result</h3>
          <div class="results-form">
            <input id="sold-input" type="number" placeholder="Sold price $" value="${r.soldPrice ?? ""}">
            <select id="status-input">${statuses.map((s) => `<option value="${s}" ${(r.status || "") === s ? "selected" : ""}>${s || "No result yet"}</option>`).join("")}</select>
            <button class="btn" id="save-result">Save</button>
          </div>
          <p class="muted" style="font-size:13px">Sets this posting's result by hand. Uploading a results list with <b>Add results</b> fills these in automatically and marks every other posting Unsold.</p>
        </div>
        <details class="raw"><summary>Source: page ${r.page} of ${esc(m.filename || "the PDF")} · show raw OCR text</summary>
          <pre>${esc(Object.entries(r.raw || {}).map(([k2, v]) => k2.toUpperCase() + ": " + v.join(" / ")).join("\n"))}</pre>
          <div class="muted" style="font-size:12px">Location: ${esc(r.geoAddress || "—")} (${esc(r.geoSource || "none")}, ${esc(r.geoMatch || "not found")})</div>
        </details>
      </div>`;
    const d = $("#drawer");
    d.classList.add("open"); d.setAttribute("aria-hidden", "false"); d.scrollTop = 0;
    $("#drawer-backdrop").hidden = false;
    $("[data-close]", d).onclick = closeDrawer;
    $$("[data-nav]", d).forEach((b) => (b.onclick = () => navDrawer(+b.dataset.nav)));
    $("#save-result", d).onclick = () => saveResult(r);
  }
  function navDrawer(dir) {
    const list = listRows(), i = list.findIndex((x) => x.seq === S.selected);
    const next = list[i + dir];
    if (next) openDrawer(next.seq, true);
  }
  function closeDrawer() {
    const d = $("#drawer");
    d.classList.remove("open"); d.setAttribute("aria-hidden", "true");
    $("#drawer-backdrop").hidden = true;
    const prev = S.selected; S.selected = null;
    const m = prev != null && S.markers.get(prev);
    if (m) m.setIcon(iconFor(S.rows.find((x) => x.seq === prev)));
    const c = $(`.card[data-seq="${prev}"]`); if (c) c.classList.remove("selected");
  }
  async function saveResult(r) {
    const v = $("#sold-input").value.trim(), st = $("#status-input").value;
    try {
      await api(`/api/books/${S.book.meta.id}/rows/${r.seq}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ soldPrice: v === "" ? null : Number(v), status: st || null }),
      });
      await loadBook(S.book.meta.id, { keepView: true });
      openDrawer(r.seq, true);
      toast("Result saved");
    } catch (e) { toast("Could not save: " + e.message); }
  }

  // ---------- books / upload ----------
  async function refreshBooks() {
    S.books = await api("/api/books");
    const sel = $("#book-select");
    sel.innerHTML = S.books.map((b) => `<option value="${esc(b.id)}">${esc((b.county || "Book") + " · " + (monthName(b.month) || b.filename))}</option>`).join("") +
      `<option value="__manage">Manage books…</option>`;
    if (S.book) sel.value = S.book.meta.id;
    const ul = $("#library-list");
    ul.innerHTML = S.books.map((b) => `<li data-id="${esc(b.id)}"><div class="grow"><div class="title">${esc(b.county || "Book")} County · ${esc(monthName(b.month))}</div>
      <div class="muted">${b.rowCount} postings · ${esc(b.filename || "")}${b.resultsAttached ? " · results attached" : ""}</div></div>
      <button class="del" data-del="${esc(b.id)}" title="Remove from library">Remove</button></li>`).join("");
    $("#library").hidden = !S.books.length;
  }

  async function loadBook(id, { keepView = false } = {}) {
    const book = await api(`/api/books/${encodeURIComponent(id)}`);
    S.book = book; S.rows = book.rows;
    showMain();
    initMap();
    const m = book.meta;
    $("#book-title").textContent = `${m.county || "Foreclosure"} County · ${monthName(m.month)}`;
    $("#status-filter").hidden = !m.resultsAttached;
    if (!m.resultsAttached) S.filters.status = [];
    $("#book-select").value = m.id;
    try { history.replaceState(null, "", "#book=" + encodeURIComponent(m.id)); } catch { /* ignore */ }
    buildMarkers();
    applyFilters();
    renderNotices();
    if (!keepView) requestAnimationFrame(() => { map.invalidateSize(); fitToRows(S.rows); });
  }

  function showMain() { $("#upload-view").hidden = true; $("#main-view").hidden = false; if (map) setTimeout(() => map.invalidateSize(), 0); }
  function showUpload() {
    $("#main-view").hidden = true; $("#upload-view").hidden = false;
    $("#upload-cancel").hidden = !S.book;
    $("#progress").hidden = true; $("#upload-error").hidden = true;
    refreshBooks();
  }

  async function pollJob(jobId, onProgress) {
    for (;;) {
      const j = await api(`/api/jobs/${jobId}`);
      onProgress(j);
      if (j.finished) { if (j.error) throw new Error(j.error); return j; }
      await new Promise((r) => setTimeout(r, 600));
    }
  }

  async function uploadBook(file) {
    if (!file) return;
    $("#upload-error").hidden = true;
    $("#progress").hidden = false;
    const stage = $("#progress-stage"), fill = $("#progress-fill"), detail = $("#progress-detail"), bar = $(".progress-bar");
    stage.textContent = "Uploading " + file.name + "…";
    try {
      const { job } = await api("/api/upload", { method: "POST", headers: { "X-Filename": file.name }, body: file });
      const j = await pollJob(job, (p) => {
        stage.textContent = p.stage || "Working…";
        const pct = p.total ? Math.round((100 * p.done) / p.total) : 0;
        bar.classList.toggle("indeterminate", !p.total);
        fill.style.width = pct + "%";
        detail.textContent = p.total ? (p.stage?.startsWith("Reading") ? `Page ${p.done} of ${p.total}` : `${p.done} of ${p.total}`) : "";
      });
      await refreshBooks();
      await loadBook(j.bookId);
      toast(`Loaded ${S.rows.length} postings`);
    } catch (e) {
      $("#progress").hidden = true;
      const err = $("#upload-error"); err.textContent = e.message; err.hidden = false;
    }
  }

  async function uploadResults(file) {
    if (!file || !S.book) return;
    toast("Reading results… this can take a minute", 60000);
    try {
      const { job } = await api(`/api/books/${S.book.meta.id}/results`, { method: "POST", headers: { "X-Filename": file.name }, body: file });
      const j = await pollJob(job, (p) => toast(`${p.stage}${p.total ? ` (${p.done}/${p.total})` : ""}…`, 60000));
      await loadBook(S.book.meta.id, { keepView: true });
      const s = j.stats || {};
      toast(`Results: matched ${s.matched ?? 0} of ${s.entries ?? 0} lines · ${s.sold ?? 0} sold · ${s.unsold ?? 0} unsold`, 8000);
    } catch (e) { toast("Results failed: " + e.message, 8000); }
  }

  function wireUpload() {
    const dz = $("#dropzone"), input = $("#file-input");
    input.onchange = () => uploadBook(input.files[0]);
    ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
    dz.addEventListener("drop", (e) => uploadBook(e.dataTransfer.files[0]));
    $("#btn-upload").onclick = showUpload;
    $("#upload-cancel").onclick = showMain;
    $("#library-list").addEventListener("click", async (e) => {
      const del = e.target.closest("[data-del]");
      if (del) {
        e.stopPropagation();
        if (!confirm("Remove this book from your library?")) return;
        await api(`/api/books/${del.dataset.del}`, { method: "DELETE" });
        if (S.book?.meta.id === del.dataset.del) S.book = null;
        $("#upload-cancel").hidden = !S.book;
        return refreshBooks();
      }
      const li = e.target.closest("li[data-id]");
      if (li) loadBook(li.dataset.id);
    });
    $("#book-select").onchange = (e) => {
      if (e.target.value === "__manage") { e.target.value = S.book?.meta.id || ""; showUpload(); }
      else loadBook(e.target.value);
    };
    $("#btn-results").onclick = () => $("#results-input").click();
    $("#results-input").onchange = (e) => { uploadResults(e.target.files[0]); e.target.value = ""; };
  }

  // ---------- boot ----------
  document.addEventListener("keydown", (e) => {
    if (!$("#drawer").classList.contains("open")) return;
    if (e.target.matches("input, select, textarea")) return;
    if (e.key === "Escape") closeDrawer();
    if (e.key === "ArrowLeft") navDrawer(-1);
    if (e.key === "ArrowRight") navDrawer(1);
  });
  $("#drawer-backdrop").onclick = closeDrawer;

  async function boot() {
    wireUpload(); wireFilters();
    $("#sort").value = S.sort; $("#in-view").checked = S.inView;
    await refreshBooks();
    const want = decodeURIComponent((location.hash.match(/book=([^&]+)/) || [])[1] || "");
    const pick = S.books.find((b) => b.id === want) || S.books[0];
    if (pick) await loadBook(pick.id); else showUpload();
  }
  boot().catch((e) => { showUpload(); const err = $("#upload-error"); err.textContent = e.message; err.hidden = false; });
})();
