// Painel de Relatórios: busca /app/relatorios/dados com os filtros e desenha tudo.
// Clicar em marketplace ou motivo vira filtro e todos os gráficos se atualizam juntos; clicar num produto abre
// os pedidos de origem. Os números se atualizam sozinhos a cada 30 s (sem recarregar a página).
(() => {
  const root = document.querySelector("[data-dash]");
  if (!root) return;
  const $ = (sel) => root.querySelector(sel);
  const PLATFORM = { tiktok: "TikTok", shopee: "Shopee", mercado_livre: "Mercado Livre", shein: "Shein" };
  const state = {
    start: root.dataset.start, end: root.dataset.end, platform: root.dataset.platform,
    store: root.dataset.store, reason: root.dataset.reason, sku: root.dataset.sku, group: root.dataset.group || "day",
  };
  const REFRESH_MS = 30000;
  const SLOT_CLASSES = ["rc1", "rc2", "rc3", "rc4"];
  const SHOW = { reasons: 5, products: 10 }; // quantos aparecem antes de "ver todos"
  const open = { reasons: false, products: false };
  let sort = { key: "total" };
  let products = [];
  let reasonsRows = [];
  let prod = null; // período próprio do bloco de produtos ({start, end}); null = o mesmo do painel
  let prodSeq = 0;
  let reasonColor = {};
  let seq = 0;
  let loadedAt = 0;
  let lastSeries = null;
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => lastSeries && renderSeries(lastSeries), 150); });

  const brl = (v) => (v ?? 0).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
  const int = (v) => (v ?? 0).toLocaleString("pt-BR");
  const pct = (v) => (v == null ? "—" : `${Math.round(v * 100)}%`);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const day = (iso) => { const [y, m, d] = iso.split("-"); return `${d}/${m}`; };
  const MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];

  function params() {
    const q = new URLSearchParams();
    q.set("inicio", state.start); q.set("fim", state.end); q.set("agrupar", state.group);
    if (state.platform) q.set("plataforma", state.platform);
    if (state.store) q.set("loja", state.store);
    if (state.reason) q.set("motivo", state.reason);
    if (state.sku) q.set("sku", state.sku);
    return q;
  }

  // silent = atualização automática: não apaga a tela nem mostra "Carregando"
  async function load(silent = false) {
    const my = ++seq;
    const q = params();
    history.replaceState(null, "", `/app/relatorios?${q}`);
    $("[data-excel]").href = `/app/relatorios/excel?${q}`;
    syncControls();
    if (!silent) { $("[data-status]").textContent = "Carregando…"; root.classList.add("loading"); }
    try {
      const resp = await fetch(`/app/relatorios/dados?${q}`, { headers: { Accept: "application/json" } });
      if (resp.redirected || !resp.ok) throw new Error(resp.status === 401 ? "sessão expirada" : `erro ${resp.status}`);
      const data = await resp.json();
      if (my !== seq) return; // chegou uma resposta mais nova
      render(data);
      loadedAt = Date.now();
      tickLive();
      $("[data-status]").textContent =
        `Devoluções solicitadas de ${day(data.filters.start)} a ${day(data.filters.end)} · comparado com ${day(data.previous.start)} a ${day(data.previous.end)}`;
    } catch (e) {
      if (silent) { $("[data-live]").classList.add("stale"); return; } // tenta de novo na próxima rodada
      $("[data-status]").textContent = `Não foi possível carregar o painel (${e.message}). Recarregue a página.`;
    } finally {
      root.classList.remove("loading");
    }
  }

  // ---------- ao vivo ----------
  function tickLive() {
    if (!loadedAt) return;
    const s = Math.round((Date.now() - loadedAt) / 1000);
    $("[data-live]").classList.toggle("stale", s > 90);
    $("[data-live-text]").textContent = s < 5 ? "ao vivo · atualizado agora" : `ao vivo · atualizado há ${s < 60 ? `${s} s` : `${Math.floor(s / 60)} min`}`;
  }
  setInterval(tickLive, 5000);
  setInterval(() => { if (!document.hidden) load(true); }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && loadedAt && Date.now() - loadedAt > REFRESH_MS) load(true); });

  // ---------- controles ----------
  function syncControls() {
    root.querySelector('[data-f="start"]').value = state.start;
    root.querySelector('[data-f="end"]').value = state.end;
    root.querySelector('[data-f="platform"]').value = state.platform || "";
    root.querySelector('[data-f="store"]').value = state.store || "";
    root.querySelectorAll("[data-preset]").forEach((b) =>
      b.classList.toggle("active", b.dataset.from === state.start && b.dataset.to === state.end));
    root.querySelectorAll("[data-group-btn]").forEach((b) => b.classList.toggle("active", b.dataset.groupBtn === state.group));
    const chips = [];
    if (state.reason) chips.push(["reason", `Motivo: ${state.reason}`]);
    if (state.sku) chips.push(["sku", `SKU: ${state.sku}`]);
    $("[data-chips]").innerHTML = chips.map(([k, label]) =>
      `<button type="button" class="chip-filter" data-clear="${k}" aria-label="Remover filtro ${esc(label)}">${esc(label)} ✕</button>`).join("");
  }

  root.addEventListener("click", (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    const d = t.dataset;
    if (d.sort) { sort = { key: d.sort }; renderProducts(); }
    else if (d.more) { open[d.more] = !open[d.more]; d.more === "reasons" ? renderReasons() : renderProducts(); }
    else if ("prodPreset" in d) { prod = d.prodPreset ? { start: d.from, end: d.to } : null; loadProducts(); }
    else if ("drawerClose" in d) closeDrawer();
    else if ("drawerFilter" in d) { state.sku = drawer.sku; closeDrawer(); load(); }
    else if ("drawerReason" in d) { drawer.reason = drawer.reason === d.drawerReason ? "" : d.drawerReason; renderDrawer(); }
    else if (d.preset) { state.start = d.from; state.end = d.to; load(); }
    else if (d.groupBtn) { state.group = d.groupBtn; load(); }
    else if (d.clear) { state[d.clear] = ""; load(); }
    else if (d.platformPick) { state.platform = state.platform === d.platformPick ? "" : d.platformPick; state.store = ""; load(); }
    else if (d.reasonPick) { state.reason = state.reason === d.reasonPick ? "" : d.reasonPick; load(); }
    else if (d.sku) openDrawer(d.sku);
  });
  root.querySelectorAll("[data-pf]").forEach((el) => el.addEventListener("change", () => {
    if (!el.value) return;
    prod = { start: prod?.start || state.start, end: prod?.end || state.end, [el.dataset.pf]: el.value };
    loadProducts();
  }));

  // Bloco de produtos com período próprio: busca só o ranking
  async function loadProducts() {
    syncProdControls();
    if (!prod) { products = lastProducts; renderProducts(); return; }
    const my = ++prodSeq;
    const q = params(); q.set("inicio", prod.start); q.set("fim", prod.end); q.delete("sku");
    $("[data-products]").classList.add("loading");
    try {
      const resp = await fetch(`/app/relatorios/produtos?${q}`, { headers: { Accept: "application/json" } });
      if (!resp.ok) throw new Error(`erro ${resp.status}`);
      const data = await resp.json();
      if (my !== prodSeq) return;
      products = data.products;
      renderProducts();
    } catch (e) {
      $("[data-products]").innerHTML = `<li class="muted small">Não foi possível carregar os produtos (${esc(e.message)}).</li>`;
    } finally {
      $("[data-products]").classList.remove("loading");
    }
  }

  function syncProdControls() {
    const start = prod?.start || state.start, end = prod?.end || state.end;
    root.querySelector('[data-pf="start"]').value = start;
    root.querySelector('[data-pf="end"]').value = end;
    root.querySelectorAll("[data-prod-preset]").forEach((b) => b.classList.toggle("active",
      b.dataset.prodPreset ? (prod && b.dataset.from === prod.start && b.dataset.to === prod.end) : !prod));
    $("[data-prod-period]").textContent = `${day(start)} a ${day(end)}`;
  }

  root.querySelectorAll("[data-f]").forEach((el) => el.addEventListener("change", () => {
    const k = el.dataset.f;
    if ((k === "start" || k === "end") && !el.value) return;
    state[k] = el.value;
    if (k === "platform") state.store = "";
    load();
  }));

  // ---------- desenho ----------
  function render(d) {
    // lojas do marketplace escolhido
    const sel = root.querySelector('[data-f="store"]');
    const stores = d.stores.filter((s) => !state.platform || s.platform === state.platform);
    sel.innerHTML = `<option value="">Todas</option>` + stores.map((s) =>
      `<option value="${s.id}">${esc(PLATFORM[s.platform] || s.platform)} · ${esc(s.name)}</option>`).join("");
    sel.value = state.store || "";

    renderKpis(d.kpis.current, d.kpis.previous, d.queue, d.repeat);
    renderSeries(d.series);
    const platTotal = d.platforms.reduce((s, p) => s + p.total, 0) || 1;
    renderBars($("[data-platforms]"), d.platforms.map((p) => ({ key: p.platform, label: PLATFORM[p.platform] || p.platform, value: p.total,
      share: p.total / platTotal, cls: `pbar-${p.platform}` })), "platformPick", state.platform);
    const reasonTotal = d.reasons.reduce((s, r) => s + r.total, 0) || 1;
    reasonsRows = d.reasons.map((r) => ({ key: r.reason, label: r.reason, value: r.total, share: r.total / reasonTotal, sub: brl(r.cost) }));
    renderReasons();
    renderConditions(d.conditions);
    lastSeries = d.series;
    // Cor fixa por motivo: os 4 mais comuns do período têm cor própria; os demais ficam em "Outros"
    reasonColor = {};
    d.reasons.slice(0, SLOT_CLASSES.length).forEach((r, i) => { reasonColor[r.reason] = SLOT_CLASSES[i]; });
    lastProducts = d.products;
    if (prod) { syncProdControls(); renderProducts(); } else { products = d.products; syncProdControls(); renderProducts(); }
  }
  let lastProducts = [];

  function renderReasons() {
    const box = $("[data-reasons]"), btn = root.querySelector('[data-more="reasons"]');
    const extra = reasonsRows.length - SHOW.reasons;
    // O motivo filtrado continua visível mesmo com a lista recolhida
    const rows = open.reasons || extra <= 0 ? reasonsRows
      : reasonsRows.filter((r, i) => i < SHOW.reasons || r.key === state.reason);
    renderBars(box, rows, "reasonPick", state.reason);
    btn.hidden = extra <= 0;
    btn.setAttribute("aria-expanded", String(open.reasons));
    btn.textContent = open.reasons ? "Mostrar menos ▴" : `Ver todos os ${reasonsRows.length} motivos ▾`;
  }

  function delta(cur, prev, { goodWhenUp = false, points = false } = {}) {
    if (cur == null || prev == null) return `<span class="delta">sem comparação</span>`;
    let diff, txt;
    if (points) { diff = (cur - prev) * 100; txt = `${Math.abs(diff).toFixed(1).replace(".", ",")} pts`; }
    else if (!prev) return `<span class="delta">sem base anterior</span>`;
    else { diff = ((cur - prev) / prev) * 100; txt = `${Math.abs(Math.round(diff))}%`; }
    if (Math.abs(diff) < 0.5) return `<span class="delta">= estável</span>`;
    const up = diff > 0;
    const good = up === goodWhenUp;
    return `<span class="delta ${good ? "good" : "bad"}">${up ? "▲" : "▼"} ${txt} vs. anterior</span>`;
  }

  function renderKpis(c, p, queue, repeat) {
    const tiles = [
      ["Devoluções", int(c.total), delta(c.total, p.total)],
      ["Custo para a empresa", brl(c.cost), delta(c.cost, p.cost)],
      ["Reembolsado", brl(c.refunded), delta(c.refunded, p.refunded)],
      ["Taxa de defeito", pct(c.defect_rate), delta(c.defect_rate, p.defect_rate, { points: true }) + `<span class="delta">${int(c.inspected)} conferidas · ${int(c.mismatch)} item dif.</span>`],
      ["Tempo médio p/ resolver", c.avg_days == null ? "—" : `${String(c.avg_days).replace(".", ",")} dias`, delta(c.avg_days, p.avg_days)],
    ];
    $("[data-kpis]").innerHTML = tiles.map(([label, value, sub]) =>
      `<div class="tile-stat"><span class="stat-label">${label}</span><strong class="stat-value">${value}</strong>${sub}</div>`).join("") +
      `<a class="tile-stat tile-link ${queue.vencidas ? "tile-alert" : ""}" href="/app?aba=decidir"><span class="stat-label">Aguardando decisão agora</span>` +
      `<strong class="stat-value">${int(queue.decidir)}</strong><span class="delta ${queue.vencidas ? "bad" : ""}">${queue.vencidas ? `⚠ ${int(queue.vencidas)} vencidas` : "nenhuma vencida"} · ${int(queue.conferir)} para conferir</span></a>` +
      (repeat ? `<a class="tile-stat tile-link ${repeat.with_mismatch ? "tile-alert" : ""}" href="/app/reincidentes?${params()}"><span class="stat-label">Compradores reincidentes</span>` +
        `<strong class="stat-value">${int(repeat.buyers)}</strong><span class="delta ${repeat.with_mismatch ? "bad" : ""}">3+ devoluções${repeat.with_mismatch ? ` · ${int(repeat.with_mismatch)} com item diferente` : ""} · Shopee e ML</span></a>` : "");
  }

  function periodLabel(iso) {
    if (state.group === "month") { const [y, m] = iso.split("-"); return `${MONTHS[+m - 1]}/${y.slice(2)}`; }
    if (state.group === "week") return `sem. ${day(iso)}`;
    return day(iso);
  }

  // Curva suave passando pelos pontos (Catmull-Rom convertida em Bézier)
  function smooth(pts) {
    if (pts.length < 3) return pts.map(([px, py], i) => `${i ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`).join(" ");
    let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
      const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
      const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
      d += ` C${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
    }
    return d;
  }

  function renderSeries(series) {
    const box = $("[data-series]");
    if (!series.length) { box.innerHTML = `<p class="muted">Sem devoluções no período.</p>`; return; }
    const W = Math.max(300, Math.round(box.clientWidth || 760)), H = W < 560 ? 190 : 230, L = 34, R = 10, T = 12, B = 24;
    const max = Math.max(1, ...series.map((s) => s.total));
    const nice = Math.ceil(max / 5) * 5 || 5;
    const x = (i) => L + (series.length === 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (series.length - 1));
    const y = (v) => T + (H - T - B) * (1 - v / nice);
    // Custo em linha tracejada na mesma altura relativa (sem segundo eixo: o valor aparece no detalhe)
    const maxCost = Math.max(1, ...series.map((s) => s.cost || 0));
    const yc = (v) => T + (H - T - B) * (1 - (v || 0) / (maxCost * 1.1));
    const line = smooth(series.map((s, i) => [x(i), y(s.total)]));
    const costLine = smooth(series.map((s, i) => [x(i), yc(s.cost)]));
    const area = `${line} L${x(series.length - 1).toFixed(1)},${y(0)} L${x(0).toFixed(1)},${y(0)} Z`;
    const costArea = `${costLine} L${x(series.length - 1).toFixed(1)},${y(0)} L${x(0).toFixed(1)},${y(0)} Z`;
    const grid = [0, 0.5, 1].map((f) => {
      const v = Math.round(nice * f);
      return `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" class="grid"/><text x="${L - 6}" y="${y(v) + 4}" class="axis" text-anchor="end">${int(v)}</text>`;
    }).join("");
    const step = Math.max(1, Math.ceil(series.length / Math.max(3, Math.floor(W / 80))));
    const ticks = series.map((s, i) => ((i % step === 0 && series.length - 1 - i >= step) || i === series.length - 1)
      ? `<text x="${x(i)}" y="${H - 6}" class="axis" text-anchor="${i === 0 ? "start" : i === series.length - 1 ? "end" : "middle"}">${periodLabel(s.period)}</text>` : "").join("");
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="line-chart" role="img" aria-label="Devoluções por ${state.group === "day" ? "dia" : state.group === "week" ? "semana" : "mês"}">
      <defs>
        <linearGradient id="area-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" class="area-top"/><stop offset="1" class="area-bottom"/></linearGradient>
        <linearGradient id="cost-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" class="cost-top"/><stop offset="1" class="cost-bottom"/></linearGradient>
        <filter id="line-glow" x="-5%" y="-30%" width="110%" height="160%"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
      </defs>
      ${grid}${ticks}
      <path d="${costArea}" class="cost-area" fill="url(#cost-fill)"/>
      <path d="${costLine}" class="cost-line"/>
      <path d="${area}" class="area" fill="url(#area-fill)"/>
      <path d="${line}" class="line" filter="url(#line-glow)"/>
      <line class="cross" y1="${T}" y2="${H - B}" x1="0" x2="0" visibility="hidden"/>
      <circle class="hover-dot" r="5" cx="0" cy="0" visibility="hidden"/>
      <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" class="hit"/>
    </svg><div class="tooltip" hidden></div>`;
    const svg = box.querySelector("svg"), tip = box.querySelector(".tooltip");
    const cross = svg.querySelector(".cross"), hd = svg.querySelector(".hover-dot");
    const move = (clientX) => {
      const r = svg.getBoundingClientRect();
      const px = ((clientX - r.left) / r.width) * W;
      const i = Math.max(0, Math.min(series.length - 1, Math.round(((px - L) / (W - L - R)) * (series.length - 1))));
      const s = series[i];
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i));
      hd.setAttribute("cx", x(i)); hd.setAttribute("cy", y(s.total)); cross.setAttribute("visibility", "visible"); hd.setAttribute("visibility", "visible");
      tip.hidden = false;
      tip.innerHTML = `<strong>${int(s.total)} devoluções · ${brl(s.cost)} de custo</strong><br>${periodLabel(s.period)} · ${brl(s.amount)} reembolsados`;
      const left = (x(i) / W) * r.width;
      tip.style.left = `${Math.min(Math.max(left, 70), r.width - 70)}px`;
    };
    const hit = svg.querySelector(".hit");
    hit.addEventListener("mousemove", (e) => move(e.clientX));
    hit.addEventListener("touchstart", (e) => move(e.touches[0].clientX), { passive: true });
    hit.addEventListener("touchmove", (e) => move(e.touches[0].clientX), { passive: true });
    hit.addEventListener("mouseleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); hd.setAttribute("visibility", "hidden"); });
  }

  function renderBars(box, rows, attr, selected) {
    if (!rows.length) { box.innerHTML = `<p class="muted small">Sem dados.</p>`; return; }
    const max = Math.max(1, ...rows.map((r) => r.value));
    const dataAttr = attr.replace(/[A-Z]/g, (m) => `-${m.toLowerCase()}`);
    // Linha compacta: nome · barra · quantidade e % (o custo aparece ao passar o mouse)
    box.innerHTML = rows.map((r) => `
      <button type="button" class="hrow ${selected && selected !== r.key ? "dim" : ""} ${selected === r.key ? "on" : ""}" data-${dataAttr}="${esc(r.key)}"
              title="${esc(r.label)}: ${int(r.value)}${r.sub ? " · custo " + r.sub : ""}">
        <span class="hrow-name">${esc(r.label)}</span>
        <span class="hbar-track"><span class="hbar-fill ${r.cls || ""}" style="width:${((r.value / max) * 100).toFixed(1)}%"></span></span>
        <span class="hrow-val"><strong>${int(r.value)}</strong>${r.share != null ? `<span class="muted">${Math.round(r.share * 100)}%</span>` : ""}</span>
      </button>`).join("");
  }

  function renderConditions(c) {
    const parts = [["resale", "Revenda", "cond-ok"], ["defect", "Defeito", "cond-bad"], ["mismatch", "Item diferente", "cond-mis"]];
    const inspected = parts.reduce((s, [k]) => s + (c[k] || 0), 0);
    const pending = c.not_inspected || 0;
    const box = $("[data-conditions]");
    const note = `<p class="small muted">${int(pending)} ${pending === 1 ? "devolução física ainda não conferida" : "devoluções físicas ainda não conferidas"} no período.</p>`;
    if (!inspected) { box.innerHTML = `<p class="muted small">Nenhuma devolução conferida no período.</p>${pending ? note : ""}`; return; }
    const share = (c.resale || 0) / inspected, R = 46, C = 2 * Math.PI * R;
    box.innerHTML = `<div class="donut-wrap">
        <svg viewBox="0 0 120 120" class="donut" role="img" aria-label="${Math.round(share * 100)}% das conferidas voltam para revenda">
          <circle cx="60" cy="60" r="${R}" class="donut-track"/>
          <circle cx="60" cy="60" r="${R}" class="donut-fill" stroke-dasharray="${(share * C).toFixed(1)} ${C.toFixed(1)}" transform="rotate(-90 60 60)"/>
          <text x="60" y="67" text-anchor="middle" class="donut-text">${Math.round(share * 100)}%</text>
        </svg><span class="small muted">das conferidas voltam para revenda</span></div>
      <div class="stack-bar" role="img" aria-label="Condição das ${int(inspected)} devoluções conferidas">
        ${parts.map(([k, label, cls]) => c[k] ? `<span class="${cls}" style="flex:${c[k]}" title="${label}: ${int(c[k])}"></span>` : "").join("")}
      </div>
      <ul class="legend">${parts.map(([k, label, cls]) => `<li><span class="sw ${cls}"></span>${label}<strong>${int(c[k] || 0)}</strong>
        <span class="muted">${Math.round(((c[k] || 0) / inspected) * 100)}%</span></li>`).join("")}</ul>
      <p class="small muted">De ${int(inspected)} conferidas.</p>${pending ? note : ""}`;
  }

  // ---------- produtos que mais voltam (ranking com foto e motivos) ----------
  function reasonStack(p) {
    const total = p.reasons.reduce((s, r) => s + r.total, 0) || 1;
    const parts = {}, other = { n: 0, names: [] };
    p.reasons.forEach((r) => {
      const cls = reasonColor[r.reason];
      if (cls) parts[cls] = (parts[cls] || 0) + r.total; else { other.n += r.total; other.names.push(`${r.reason} ${r.total}`); }
    });
    const seg = (cls, n, label) => n ? `<i class="${cls}" style="flex:${n}" title="${esc(label)}: ${int(n)} (${Math.round((n / total) * 100)}%)"></i>` : "";
    const named = Object.entries(reasonColor).map(([reason, cls]) => seg(cls, parts[cls], reason)).join("");
    return `<span class="rstack">${named}${seg("rc-other", other.n, `Outros: ${other.names.join(", ")}`)}</span>`;
  }

  function thumb(src, cls = "thumb") {
    return src ? `<img class="${cls}" src="${esc(src)}" alt="" loading="lazy" referrerpolicy="no-referrer" data-thumb>`
      : `<span class="${cls} thumb-empty" aria-hidden="true"></span>`;
  }
  // Foto que não carrega (link do marketplace expirado) vira um quadro neutro
  root.addEventListener("error", (e) => {
    if (e.target.matches?.("img[data-thumb]")) { e.target.removeAttribute("src"); e.target.classList.add("thumb-empty"); }
  }, true);

  function renderProducts() {
    const list = $("[data-products]");
    root.querySelectorAll("[data-sort]").forEach((b) => b.classList.toggle("active", b.dataset.sort === sort.key));
    const legend = Object.entries(reasonColor).map(([r, cls]) => `<span><b class="${cls}"></b>${esc(r)}</span>`).join("");
    $("[data-rank-legend]").innerHTML = legend ? `${legend}<span><b class="rc-other"></b>Outros</span>` : "";
    const btn = root.querySelector('[data-more="products"]');
    btn.hidden = products.length <= SHOW.products;
    btn.setAttribute("aria-expanded", String(open.products));
    btn.textContent = open.products ? "Mostrar menos ▴" : `Ver os ${products.length} produtos ▾`;
    if (!products.length) { list.innerHTML = `<li class="muted">Sem devoluções no período.</li>`; return; }
    const sorted = [...products].sort((a, b) => (b[sort.key] ?? -1) - (a[sort.key] ?? -1));
    const rows = open.products ? sorted : sorted.slice(0, SHOW.products);
    list.innerHTML = rows.map((p, i) => `
      <li><button type="button" class="rank-row ${state.sku === p.sku ? "on" : ""}" data-sku="${esc(p.sku)}" title="Ver os pedidos de ${esc(p.sku)}">
        <span class="rank-pos">${i + 1}</span>
        ${thumb(p.image)}
        <span class="rank-name"><strong>${esc(p.name || p.sku)}</strong><span class="mono small muted">${esc(p.sku)}</span></span>
        ${reasonStack(p)}
        <span class="rank-meta small">
          <span class="${p.defect_rate >= 0.15 ? "bad-text" : "muted"}">${pct(p.defect_rate)} defeito</span>
          ${p.mismatches ? `<span class="warn-text">${int(p.mismatches)} item dif.</span>` : ""}
          ${p.cost == null ? "" : `<span class="muted">${brl(p.cost)}</span>`}
        </span>
        <span class="rank-total">${int(p.total)}</span>
      </button></li>`).join("");
  }

  // ---------- painel lateral: pedidos de origem do produto ----------
  const CONDITION = { resale: ["revenda", "ok"], defect: ["defeito", "bad"], mismatch: ["item diferente", "warn"] };
  const drawer = { sku: null, reason: "", orders: [], limited: false };
  const panel = root.querySelector("[data-drawer]"), back = root.querySelector("[data-drawer-back]");
  back.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawer.sku) closeDrawer(); });

  async function openDrawer(sku) {
    Object.assign(drawer, { sku, reason: "", orders: [], limited: false });
    const p = products.find((x) => x.sku === sku) || { sku, total: 0 };
    $("[data-drawer-prod]").innerHTML = `${thumb(p.image, "thumb thumb-lg")}<div class="drawer-title"><strong>${esc(p.name || sku)}</strong>
      <span class="mono small muted">${esc(sku)}</span><span class="small muted">${int(p.total)} devoluções no período</span></div>`;
    $("[data-drawer-reasons]").innerHTML = "";
    $("[data-drawer-orders]").innerHTML = `<li class="muted small">Carregando pedidos…</li>`;
    panel.hidden = false; back.hidden = false;
    document.body.classList.add("drawer-open");
    panel.focus();
    const q = params(); q.set("sku", sku);
    try {
      const resp = await fetch(`/app/relatorios/pedidos?${q}`, { headers: { Accept: "application/json" } });
      if (!resp.ok) throw new Error(`erro ${resp.status}`);
      const data = await resp.json();
      if (drawer.sku !== sku) return;
      drawer.orders = data.orders; drawer.limited = data.limited;
      renderDrawer();
    } catch (e) {
      $("[data-drawer-orders]").innerHTML = `<li class="muted small">Não foi possível carregar os pedidos (${esc(e.message)}).</li>`;
    }
  }

  function renderDrawer() {
    const counts = {};
    drawer.orders.forEach((o) => { counts[o.reason] = (counts[o.reason] || 0) + 1; });
    $("[data-drawer-reasons]").innerHTML = Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([r, n]) =>
      `<button type="button" class="chip-filter ${drawer.reason === r ? "on" : ""}" data-drawer-reason="${esc(r)}"><b class="${reasonColor[r] || "rc-other"}"></b>${esc(r)} · ${n}</button>`).join("");
    const rows = drawer.orders.filter((o) => !drawer.reason || o.reason === drawer.reason);
    $("[data-drawer-orders]").innerHTML = rows.map((o) => {
      const cond = o.received && CONDITION[o.condition];
      const when = o.requested_at ? new Date(o.requested_at).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }) : "";
      const badge = cond ? `<span class="badge tone-${cond[1]}">${cond[0]}</span>` : `<span class="badge tone-${esc(o.status_tone)}">${esc(o.status_label)}</span>`;
      return `<li><a href="/app/devolucoes/${o.id}" class="order-row">
        <span class="order-top"><strong>${esc(PLATFORM[o.platform] || o.platform)} · <span class="mono">${esc(o.order)}</span></strong>${badge}</span>
        <span class="small muted">${esc(o.reason)}${o.variant ? ` · ${esc(o.variant)}` : ""}${when ? ` · ${when}` : ""}</span>
      </a></li>`;
    }).join("") + (drawer.limited ? `<li class="small muted">Mostrando os 300 mais recentes.</li>` : "");
  }

  function closeDrawer() {
    drawer.sku = null;
    panel.hidden = true; back.hidden = true;
    document.body.classList.remove("drawer-open");
  }

  load();
})();
