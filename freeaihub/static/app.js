(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const S = {
    overview: null,
    providers: [],
    models: [],
    clients: [],
    logs: [],
    settings: null,
    providerFilter: "all",
    modelFilter: "all",
    providerSearch: "",
    modelSearch: "",
    providerSelect: "all",
    page: 1,
    pageSize: 50,
    protocol: "chat",
    activeTab: "overview",
    refreshing: false,
    activeProvider: null,
    testModel: null,
  };

  const MODALITY_LABELS = {
    text: "文本",
    code: "代码",
    reasoning: "推理",
    vision: "视觉",
    image: "图像",
    video: "视频",
    audio: "音频",
  };

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[ch]));
  }

  function fmtInt(value) {
    const n = Number(value || 0);
    if (!Number.isFinite(n)) return "0";
    return n.toLocaleString("zh-CN");
  }

  function fmtPct(value) {
    if (value === null || value === undefined) return "—";
    return `${Math.round(Number(value) * 100)}%`;
  }

  function fmtLatency(value) {
    if (value === null || value === undefined) return "—";
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${Math.round(n)}ms`;
  }

  function fmtSpeed(value) {
    if (value === null || value === undefined) return "—";
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) return "—";
    return `${n.toFixed(n >= 100 ? 0 : 1)} tok/s`;
  }

  function fmtContext(value) {
    const n = Number(value || 0);
    if (!n) return "—";
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
    if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
    return String(n);
  }

  function timeAgo(iso) {
    if (!iso) return "尚未";
    const time = new Date(iso).getTime();
    if (!Number.isFinite(time)) return "—";
    const seconds = Math.max(0, Math.round((Date.now() - time) / 1000));
    if (seconds < 10) return "刚刚";
    if (seconds < 60) return `${seconds} 秒前`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} 分钟前`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} 小时前`;
    const days = Math.round(hours / 24);
    if (days < 7) return `${days} 天前`;
    return new Date(iso).toLocaleString("zh-CN", { hour12: false });
  }

  function statusInfo(status) {
    const map = {
      online: ["在线", "ok"],
      available: ["可用", "ok"],
      checking: ["检测中", "warn"],
      degraded: ["降级", "warn"],
      needs_key: ["需要 Key", "warn"],
      invalid_key: ["Key 无效", "bad"],
      offline: ["离线", "bad"],
      restricted: ["受限", "think"],
      disabled: ["已停用", "bad"],
      catalog: ["仅目录", ""],
      keyless: ["免 Key", "accent"],
      unknown: ["未检测", ""],
    };
    const [label, cls] = map[status] || [status || "未知", ""];
    return { label, cls };
  }

  function badge(status) {
    const info = statusInfo(status);
    return `<span class="badge ${info.cls}">${escapeHtml(info.label)}</span>`;
  }

  function avatar(name) {
    const text = String(name || "?").trim();
    let hash = 0;
    for (let i = 0; i < text.length; i += 1) hash = (hash * 31 + text.charCodeAt(i)) % 360;
    const initial = text.replace(/[^A-Za-z0-9\u4e00-\u9fff]/g, "").slice(0, 1).toUpperCase() || "?";
    return `<span class="avatar" style="background:hsl(${hash} 38% 42%)">${escapeHtml(initial)}</span>`;
  }

  function providerById(id) {
    return S.providers.find((p) => p.id === id) || null;
  }

  function modelsForProvider(id) {
    return S.models.filter((m) => m.provider_id === id);
  }

  function apiBase() {
    const port = S.settings?.port || 8899;
    return `http://127.0.0.1:${port}/v1`;
  }

  function apiKeyLabel() {
    const settings = S.settings || {};
    return settings.api_key_masked || "已生成（在设置中查看）";
  }

  async function api(path, options = {}) {
    const opts = { ...options, headers: { ...(options.headers || {}) } };
    if (opts.body && !opts.headers["Content-Type"]) opts.headers["Content-Type"] = "application/json";
    const response = await fetch(path, opts);
    const text = await response.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { raw: text }; }
    if (!response.ok) {
      if (response.status === 401 && path.startsWith("/api")) {
        location.href = "/login";
      }
      const message = data?.error?.message || data?.message || `${response.status} ${response.statusText}`;
      throw new Error(message);
    }
    return data;
  }

  let toastTimer = null;
  function toast(message, type = "") {
    const el = $("#toast");
    el.textContent = message;
    el.className = `toast show ${type}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = "toast"; }, 3200);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("已复制", "ok");
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      try { document.execCommand("copy"); toast("已复制", "ok"); }
      catch (err) { toast("复制失败", "bad"); }
      area.remove();
    }
  }

  function setTab(name) {
    S.activeTab = name;
    $$(".tab-btn").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === name));
    $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${name}`));
    if (name === "data") renderData();
    if (name === "models") renderModels();
    if (name === "clients") renderClients();
    if (name === "logs") renderLogs();
    if (name === "settings") renderSettings();
    if (name === "access") renderAccess();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("freeaihub.theme", theme); } catch (_) {}
  }

  function initTheme() {
    let theme = "light";
    try { theme = localStorage.getItem("freeaihub.theme") || "light"; } catch (_) {}
    if (theme === "system") theme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    applyTheme(theme === "dark" ? "dark" : "light");
  }

  // ------------------------------------------------------------------ load
  async function loadAll() {
    const [overview, providers, models, clients, logs, settings] = await Promise.all([
      api("/api/overview"),
      api("/api/providers"),
      api("/api/models"),
      api("/api/clients"),
      api("/api/logs?limit=300"),
      api("/api/settings"),
    ]);
    S.overview = overview;
    S.providers = providers.providers || [];
    S.models = models.models || [];
    S.clients = clients.clients || [];
    S.logs = logs.logs || [];
    S.settings = settings.settings || null;
    renderAll();
  }

  async function loadLive() {
    if (document.hidden || S.refreshing) return;
    if (!["overview", "logs"].includes(S.activeTab)) return;
    if (!$("#drawer").classList.contains("hidden")) return;
    try {
      const [overview, logs] = await Promise.all([api("/api/overview"), api("/api/logs?limit=300")]);
      S.overview = overview;
      S.logs = logs.logs || [];
      renderHeader();
      if (S.activeTab === "overview") renderOverview();
      if (S.activeTab === "logs") renderLogs();
    } catch (_) { /* transient */ }
  }

  // --------------------------------------------------------------- header
  function renderHeader() {
    const o = S.overview;
    if (!o) return;
    const c = o.counts || {};
    $("#topMeta").textContent = `${fmtInt(c.providers_online)}/${fmtInt(c.providers_total)} 在线 · ${fmtInt(c.models_available)} 个可用模型`;
    const dot = $("#liveDot");
    dot.className = "live-dot";
    if (!c.providers_online) dot.classList.add("warn");
    if (o.refresh?.running) dot.classList.add("warn");
    $("#lastRefresh").textContent = c.refreshed_at ? `上次巡检 ${timeAgo(c.refreshed_at)}` : "尚未巡检";
    setRefreshButtonState(!!o.refresh?.running || S.refreshing);
  }

  function setRefreshButtonState(loading) {
    const btn = $("#refreshBtn");
    btn.classList.toggle("loading", loading);
    btn.disabled = loading;
    $("#heroRefresh").disabled = loading;
    $("#heroRefresh").textContent = loading ? "巡检中…" : "立即巡检";
  }

  // -------------------------------------------------------------- overview
  function renderAll() {
    renderHeader();
    renderOverview();
    renderData();
    renderProviderSelect();
    renderProviders();
    renderModels();
    renderClients();
    renderLogs();
    renderAccess();
    renderSettings();
  }

  function kpiCard(label, value, sub, cls = "") {
    return `<div class="kpi ${cls}"><div class="k">${escapeHtml(label)}</div><div class="v">${value}</div><div class="s">${sub}</div></div>`;
  }

  function renderOverview() {
    const o = S.overview;
    if (!o) return;
    const c = o.counts || {};
    const kpis = [
      kpiCard("提供商", `${fmtInt(c.providers_online)}<span style="font-size:15px;color:var(--dim)"> / ${fmtInt(c.providers_total)}</span>`, `${fmtInt(c.providers_keyless)} 个免 Key · ${fmtInt(c.providers_needs_key)} 个待配置 Key`, "green"),
      kpiCard("可用模型", fmtInt(c.models_available), `目录 ${fmtInt(c.models_catalog)} · 已知 ${fmtInt(c.models_known)}`, "blue"),
      kpiCard("免 Key 路由", fmtInt(c.models_keyless_available), "无需注册 / 无需 API Key", "purple"),
      kpiCard("请求数", fmtInt(c.requests), `成功率 <b>${fmtPct(c.success_rate)}</b> · 失败 ${fmtInt(c.failures)}`, "warn"),
      kpiCard("平均延迟", fmtLatency(c.avg_latency_ms), `Token 入 ${fmtInt(c.tokens_in)} · 出 ${fmtInt(c.tokens_out)}`, ""),
      kpiCard("巡检", o.refresh?.running ? "运行中" : (c.refreshed_at ? "已完成" : "待巡检"), o.refresh?.running ? `${o.refresh.done}/${o.refresh.total}` : (c.refreshed_at ? timeAgo(c.refreshed_at) : "点击右上角巡检"), "green"),
    ];
    $("#kpiGrid").innerHTML = kpis.join("");

    const keyless = o.keyless || [];
    $("#keylessGrid").innerHTML = keyless.length
      ? keyless.map((p) => {
          const info = statusInfo(p.status);
          return `<div class="keyless-card" data-provider-id="${escapeHtml(p.id)}">
            <div class="kc-top"><span class="kc-name">${escapeHtml(p.name)}</span>${badge(p.status)}</div>
            <div class="kc-meta"><span>${fmtInt(p.enabled_models)} 个模型</span><span>${fmtLatency(p.latency_ms)}</span><span class="status-text" style="color:var(--${info.cls === "ok" ? "accent2" : info.cls === "bad" ? "bad" : "warn"})">${escapeHtml(info.label)}</span></div>
            <div class="kc-url">${escapeHtml(p.base_url || "—")}</div>
          </div>`;
        }).join("")
      : `<div class="empty"><b>还没有免 Key 路由</b>点击右上角巡检，或到「提供商」配置 Key。</div>`;

    const topRoutes = S.models.filter((m) => m.status === "available" && m.keyless).slice(0, 8);
    $("#keylessRoutes").innerHTML = topRoutes.length
      ? `<div class="field-label">自动路由优先</div><div class="model-chips">${topRoutes.map((m) => `<button class="model-chip" data-copy="${escapeHtml(m.id)}" title="${escapeHtml(m.id)}">${escapeHtml(m.id)}</button>`).join("")}</div><div class="muted" style="margin-top:9px;font-size:10.5px;line-height:1.6">巡检只读取各 provider 的 <code>/models</code> 列表，不发送对话内容、不消耗对话额度；自动路由只会选择当前在线且免 Key 的候选。</div>`
      : "";

    const series = o.counts?.series || [];
    const maxReq = Math.max(1, ...series.map((x) => Number(x.requests) || 0));
    const hasTraffic = series.some((x) => Number(x.requests) > 0);
    const spark = hasTraffic
      ? series.map((x) => {
          const h = Math.max(4, Math.round((Number(x.requests) || 0) / maxReq * 100));
          return `<i class="${x.requests ? "" : "zero"}" style="height:${h}%" title="${escapeHtml(x.t)} · ${x.requests} req · ${x.avg_latency_ms ? Math.round(x.avg_latency_ms) + "ms" : "—"}"></i>`;
        }).join("")
      : "";
    const strategy = { balanced: "均衡", fastest: "最快", keyless: "免 Key 优先" }[o.settings?.routing_strategy] || "均衡";
    $("#servicePanel").innerHTML = `
      <div class="service-list">
        <div class="service-row"><span class="sl">API Base</span><span class="sv">${escapeHtml(apiBase())}</span></div>
        <div class="service-row"><span class="sl">路由策略</span><span class="sv">${escapeHtml(strategy)}</span></div>
        <div class="service-row"><span class="sl">认证</span><span class="sv">Bearer（始终启用）</span></div>
        <div class="service-row"><span class="sl">巡检</span><span class="sv">${o.refresh?.running ? `运行中 ${o.refresh.done}/${o.refresh.total}` : (c.refreshed_at ? timeAgo(c.refreshed_at) : "尚未")}</span></div>
        <div class="service-row"><span class="sl">目录模型</span><span class="sv">${fmtInt(c.models_catalog)}</span></div>
      </div>
      <div class="spark" aria-label="近 2 小时请求">${spark}</div>
      <div class="muted" style="margin-top:7px;font-size:10.5px">${hasTraffic ? `近 2 小时请求 · 最高 ${maxReq} 次/5 分钟` : "近 2 小时暂无请求"}</div>`;
    $("#recentTable").innerHTML = logsTable(o.recent || [], true);
    $("#quickBase").textContent = apiBase();
    const available = S.models.filter((m) => m.status === "available");
    const examples = (available.length ? available : S.models).slice(0, 8);
    $("#quickModels").innerHTML = examples.length
      ? examples.map((m) => `<button class="model-chip" data-copy="${escapeHtml(m.id)}" title="${escapeHtml(m.id)}">${escapeHtml(m.id)}</button>`).join("")
      : `<span class="muted">尚未发现可用模型</span>`;
    const sampleModel = examples[0]?.id || "auto";
    $("#quickCurl").textContent = curlExample(sampleModel);
  }

  function curlExample(model) {
    return `curl ${apiBase()}/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer fah-..." \\
  -d '${JSON.stringify({ model, messages: [{ role: "user", content: "用一句中文介绍你自己" }] })}'`;
  }

  // ------------------------------------------------------------- providers
  function renderData() {
    const o = S.overview;
    if (!o) return;
    const c = o.counts || {};
    const models = c.models_detail || [];
    const kpis = [
      kpiCard("总 Token", fmtInt(c.tokens_total), `入 ${fmtInt(c.tokens_in)} · 出 ${fmtInt(c.tokens_out)}`, "blue"),
      kpiCard("生成速度", fmtSpeed(c.avg_speed), "流式按首字后计算", "green"),
      kpiCard("缓存命中率", fmtPct(c.cache_hit_rate), c.cache_reported ? `缓存 ${fmtInt(c.cached_tokens)} / 输入 ${fmtInt(c.tokens_in)}` : "上游未返回缓存明细", "purple"),
      kpiCard("请求成功率", fmtPct(c.success_rate), `${fmtInt(c.successes)} 成功 · ${fmtInt(c.failures)} 失败`, "green"),
      kpiCard("请求耗时", fmtLatency(c.avg_latency_ms), `P95 ${fmtLatency(c.p95_latency_ms)}`, ""),
      kpiCard("请求数", fmtInt(c.requests), `${fmtInt(models.length)} 个模型有流量`, "warn"),
    ];
    $("#dataKpis").innerHTML = kpis.join("");

    const series = c.series || [];
    const maxReq = Math.max(1, ...series.map((row) => Number(row.requests) || 0));
    const hasTraffic = series.some((row) => Number(row.requests) > 0);
    $("#dataChart").innerHTML = hasTraffic
      ? series.map((row) => {
          const total = Number(row.requests) || 0;
          const failed = Math.min(total, Number(row.failures) || 0);
          const ok = Math.max(0, total - failed);
          const okH = total ? Math.max(2, Math.round(ok / maxReq * 100)) : 0;
          const badH = total ? Math.max(0, Math.round(failed / maxReq * 100)) : 0;
          const title = `${row.t} · ${total} req · 失败 ${failed} · Token ${fmtInt(row.tokens_total)} · ${row.avg_latency_ms ? Math.round(row.avg_latency_ms) + "ms" : "—"}`;
          return `<div class="data-col ${total ? "" : "zero"}" title="${escapeHtml(title)}">
            <div class="seg" style="height:${okH}%"></div>
            <div class="seg bad" style="height:${badH}%"></div>
          </div>`;
        }).join("")
      : `<div class="data-empty">最近 24 小时还没有请求</div>`;
    $("#dataChart").setAttribute("aria-label", hasTraffic ? "近 24 小时请求柱状图" : "暂无请求");

    $("#dataHealth").innerHTML = [
      ["缓存命中", fmtPct(c.cache_hit_rate), c.cache_reported ? `${fmtInt(c.cached_tokens)} 缓存 Token` : "上游未返回缓存明细"],
      ["平均生成速度", fmtSpeed(c.avg_speed), "输出 Token / 生成时间"],
      ["平均请求耗时", fmtLatency(c.avg_latency_ms), `P95 ${fmtLatency(c.p95_latency_ms)}`],
      ["请求成功率", fmtPct(c.success_rate), `${fmtInt(c.successes)} / ${fmtInt(c.requests)}`],
    ].map(([label, value, sub]) => `<div class="data-health-row"><div><b>${value}</b><div class="muted">${escapeHtml(label)}</div></div><span class="muted">${escapeHtml(sub)}</span></div>`).join("");

    $("#dataModelTable tbody").innerHTML = models.length
      ? models.map((row) => `<tr>
          <td><span class="model-id"><span class="mid-text" title="${escapeHtml(row.model)}">${escapeHtml(row.model)}</span></span></td>
          <td>${fmtInt(row.requests)}</td>
          <td>${fmtPct(row.success_rate)}</td>
          <td>${fmtInt(row.tokens_in)}</td>
          <td>${fmtInt(row.tokens_out)}</td>
          <td>${fmtPct(row.cache_hit_rate)}</td>
          <td>${fmtSpeed(row.avg_speed)}</td>
          <td>${fmtLatency(row.avg_latency_ms)}</td>
          <td>${fmtLatency(row.p95_latency_ms)}</td>
        </tr>`).join("")
      : `<tr><td colspan="9"><div class="empty"><b>还没有模型数据</b>调用一次 API 后会自动出现。</div></td></tr>`;
    $("#dataModelMeta").textContent = models.length ? `最近 ${fmtInt(c.requests)} 条请求` : "";

    const logs = (S.logs || []).slice(0, 30);
    $("#dataRecentTable tbody").innerHTML = logs.length
      ? logs.map((row) => {
          const latency = Number(row.latency_ms) || 0;
          const first = Number(row.first_token_ms) || 0;
          const speed = row.tokens_out > 0 && latency > 0 ? row.tokens_out / Math.max(0.001, (latency - first) / 1000) : null;
          const cache = row.tokens_in > 0 ? (Number(row.cached_tokens || 0) / row.tokens_in) : null;
          return `<tr>
            <td class="muted" title="${escapeHtml(row.ts)}">${escapeHtml(timeAgo(row.ts))}</td>
            <td><span class="model-id"><span class="mid-text" title="${escapeHtml(row.model)}">${escapeHtml(row.model)}</span></span></td>
            <td>${badge(row.ok ? "online" : "offline")} <span class="muted">${escapeHtml(String(row.status || ""))}</span></td>
            <td>${fmtInt(row.tokens_in)}</td>
            <td>${fmtInt(row.tokens_out)}${row.estimated ? '<span class="muted">*</span>' : ""}</td>
            <td>${fmtSpeed(speed)}</td>
            <td>${fmtPct(cache)}</td>
            <td>${fmtLatency(row.latency_ms)}</td>
          </tr>`;
        }).join("")
      : `<tr><td colspan="8"><div class="empty"><b>还没有请求</b>去「接入」页调用一次 API。</div></td></tr>`;
    $("#dataRecentMeta").textContent = logs.length ? `最近 ${logs.length} 条` : "";
  }

  function renderProviderSelect() {
    const select = $("#providerSelect");
    if (!select) return;
    const current = S.providerSelect;
    const options = [`<option value="all">全部提供商</option>`].concat(
      S.providers.map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}</option>`)
    );
    select.innerHTML = options.join("");
    if (!S.providers.some((p) => p.id === current)) S.providerSelect = "all";
    select.value = S.providerSelect;
  }

  function providerMatches(p) {
    const query = S.providerSearch.trim().toLowerCase();
    if (query && !`${p.name} ${p.id} ${p.base_url || ""}`.toLowerCase().includes(query)) return false;
    switch (S.providerFilter) {
      case "keyless": return !!p.keyless;
      case "keyed": return !!p.key_required;
      case "online": return ["online", "degraded", "checking"].includes(p.status);
      case "catalog": return !p.routable || p.status === "catalog";
      default: return true;
    }
  }

  function renderProviders() {
    if (!S.providers.length) return;
    const filtered = S.providers.filter(providerMatches);
    $("#providerSummary").textContent = `显示 ${filtered.length} / ${S.providers.length} 个提供商`;
    const grid = $("#providerGrid");
    grid.innerHTML = filtered.length
      ? filtered.map((p) => {
          const info = statusInfo(p.status);
          const badges = [];
          if (p.keyless) badges.push(`<span class="badge accent no-dot">免 Key</span>`);
          if (p.key_required && p.key_configured) badges.push(`<span class="badge ok no-dot">已配 Key</span>`);
          return `<div class="provider-card" data-provider-id="${escapeHtml(p.id)}">
            <div class="pc-top">
              <div class="pc-name">${escapeHtml(p.name)}<span>${escapeHtml(p.id)}</span></div>
              <div class="pc-badges">${badge(p.status)}${badges.join("")}</div>
            </div>
            <div class="pc-url" title="${escapeHtml(p.base_url || "")}">${escapeHtml(p.base_url || "无公开 Base URL")}</div>
            <div class="pc-stats">
              <div class="pc-stat"><div class="k">模型</div><div class="v">${fmtInt(p.enabled_models)}</div></div>
              <div class="pc-stat"><div class="k">延迟</div><div class="v">${fmtLatency(p.latency_ms)}</div></div>
              <div class="pc-stat"><div class="k">请求</div><div class="v">${fmtInt(p.requests)}</div></div>
            </div>
            <div class="pc-footer"><span class="status-text" style="color:var(--${info.cls === "ok" ? "accent2" : info.cls === "bad" ? "bad" : "dim"})">${escapeHtml(info.label)}</span><span>${p.keyless ? "免 Key" : p.key_required ? "需 Key" : "目录"} · ${fmtInt(p.free_model_count)} 个免费模型</span></div>
          </div>`;
        }).join("")
      : `<div class="empty"><b>没有匹配的提供商</b>换一个搜索词或筛选条件。</div>`;
  }

  // ---------------------------------------------------------------- clients
  function renderClients() {
    const grid = $("#clientGrid");
    if (!grid) return;
    const clients = S.clients || [];
    grid.innerHTML = clients.map((client) => {
      const stateClass = !client.installed ? "off" : client.configured ? "on" : "warn";
      const stateText = !client.installed ? "未检测到" : client.configured ? "已配置" : "已安装，未配置";
      const actionLabel = client.configured ? "重新一键配置" : "一键配置";
      return `<div class="client-card">
        <div class="client-top">
          <div>
            <div class="client-name">${escapeHtml(client.name)}</div>
            <div class="client-protocol">${escapeHtml(client.protocol)}</div>
          </div>
          <span class="client-state ${stateClass}">${escapeHtml(stateText)}</span>
        </div>
        <div class="client-path" title="${escapeHtml(client.display_paths?.[0] || client.command || "")}">${escapeHtml(client.display_paths?.[0] || client.command || "未找到安装路径")}</div>
        <div class="client-actions">
          <button class="btn small ${client.installed ? "primary" : ""}" data-client-configure="${escapeHtml(client.id)}" ${client.installed ? "" : "disabled"}>${escapeHtml(actionLabel)}</button>
          ${client.has_backup ? `<button class="btn small ghost" data-client-restore="${escapeHtml(client.id)}">恢复备份</button>` : ""}
        </div>
      </div>`;
    }).join("") || `<div class="empty"><b>无法读取客户端列表</b>刷新页面或检查后端。</div>`;
  }

  async function configureClient(clientId) {
    try {
      await api(`/api/clients/${encodeURIComponent(clientId)}/configure`, { method: "POST", body: "{}" });
      toast("已写入配置，重启客户端后生效", "ok");
      const clients = await api("/api/clients");
      S.clients = clients.clients || [];
      renderClients();
    } catch (err) {
      toast(err.message, "bad");
    }
  }

  async function restoreClient(clientId) {
    try {
      const result = await api(`/api/clients/${encodeURIComponent(clientId)}/restore`, { method: "POST", body: "{}" });
      toast(result.restored?.length ? "已恢复最近备份" : "没有找到备份", result.restored?.length ? "ok" : "bad");
      const clients = await api("/api/clients");
      S.clients = clients.clients || [];
      renderClients();
    } catch (err) {
      toast(err.message, "bad");
    }
  }

  // ----------------------------------------------------------------- models
  function modelMatches(m) {
    const query = S.modelSearch.trim().toLowerCase();
    if (query && !`${m.id} ${m.name} ${m.provider_name} ${m.provider_id}`.toLowerCase().includes(query)) return false;
    if (S.providerSelect !== "all" && m.provider_id !== S.providerSelect) return false;
    switch (S.modelFilter) {
      case "available": return m.status === "available";
      case "keyless": return !!m.keyless;
      case "needs_key": return m.status === "needs_key";
      case "reasoning": return (m.modalities || []).includes("reasoning");
      case "code": return (m.modalities || []).includes("code");
      case "vision": return (m.modalities || []).includes("vision");
      case "disabled": return !m.enabled || !m.policy_enabled;
      default: return true;
    }
  }

  function renderModels() {
    if (!S.models.length) return;
    const filtered = S.models.filter(modelMatches).sort((a, b) => {
      const rank = (m) => (m.status === "available" ? 0 : m.keyless ? 1 : 2);
      return rank(a) - rank(b) || a.provider_name.localeCompare(b.provider_name, "zh-CN") || a.id.localeCompare(b.id);
    });
    const totalPages = Math.max(1, Math.ceil(filtered.length / S.pageSize));
    S.page = Math.min(Math.max(1, S.page), totalPages);
    const start = (S.page - 1) * S.pageSize;
    const pageItems = filtered.slice(start, start + S.pageSize);
    $("#modelCount").textContent = `${fmtInt(filtered.length)} 个模型 · 可用 ${fmtInt(filtered.filter((m) => m.status === "available").length)} 个`;
    $("#modelSummary").textContent = `目录 ${fmtInt(S.overview?.counts?.models_catalog)} · 已知 ${fmtInt(S.models.length)} · 可用 ${fmtInt(S.models.filter((m) => m.status === "available").length)}`;
    $("#pageInfo").textContent = `第 ${S.page} / ${totalPages} 页`;
    const tbody = $("#modelsTable tbody");
    tbody.innerHTML = pageItems.length
      ? pageItems.map((m) => {
          const modalities = (m.modalities || []).filter((x) => x !== "text");
          const shown = modalities.slice(0, 3).map((x) => `<span class="badge no-dot">${escapeHtml(MODALITY_LABELS[x] || x)}</span>`).join("");
          const more = modalities.length > 3 ? `<span class="badge no-dot">+${modalities.length - 3}</span>` : "";
          return `<tr>
            <td><div class="model-id"><span class="mid-text" title="${escapeHtml(m.id)}">${escapeHtml(m.id)}</span><button class="copy-btn copy-model" data-copy="${escapeHtml(m.id)}">复制</button></div></td>
            <td><div class="provider-cell">${avatar(m.provider_name)}<span class="pname" title="${escapeHtml(m.provider_name)}">${escapeHtml(m.provider_name)}</span></div></td>
            <td>${badge(m.status)}</td>
            <td><div class="chips">${shown || '<span class="badge no-dot">文本</span>'}${more}</div></td>
            <td>${escapeHtml(fmtContext(m.context))}</td>
            <td><span class="badge no-dot">${escapeHtml(m.source || "—")}</span></td>
            <td><div class="route-cell">
              <label class="switch-mini" title="是否启用该模型"><input type="checkbox" data-policy-enabled="${escapeHtml(m.id)}" ${m.policy_enabled ? "checked" : ""} ${m.enabled ? "" : "disabled"}></label>
              <input class="weight-input" type="number" min="0" max="100" step="0.1" value="${Number(m.weight ?? 1).toFixed(1)}" data-policy-weight="${escapeHtml(m.id)}" title="自动路由权重；0 = 不参与自动分发" ${m.enabled ? "" : "disabled"}>
            </div></td>
            <td><button class="btn small" data-test-model="${escapeHtml(m.id)}">测试</button></td>
          </tr>`;
        }).join("")
      : `<tr><td colspan="8"><div class="empty"><b>没有匹配的模型</b>试试取消筛选，或点右上角巡检。</div></td></tr>`;
    renderPager(totalPages);
  }

  async function updateModelPolicy(modelId, patch) {
    try {
      const result = await api(`/api/models/${encodeModelPath(modelId)}/settings`, {
        method: "POST",
        body: JSON.stringify(patch),
      });
      const index = S.models.findIndex((model) => model.id === modelId);
      if (index >= 0 && result.model) S.models[index] = result.model;
      renderModels();
      toast("路由设置已保存", "ok");
    } catch (err) {
      toast(err.message, "bad");
      renderModels();
    }
  }

  function renderPager(totalPages) {
    const pager = $("#modelPager");
    const pages = [];
    const add = (n) => pages.push(`<button class="pager-btn ${n === S.page ? "active" : ""}" data-page="${n}">${n}</button>`);
    const windowStart = Math.max(1, S.page - 2);
    const windowEnd = Math.min(totalPages, S.page + 2);
    pages.push(`<button class="pager-btn" data-page="${S.page - 1}" ${S.page <= 1 ? "disabled" : ""}>上一页</button>`);
    if (windowStart > 1) { add(1); if (windowStart > 2) pages.push(`<span class="muted">…</span>`); }
    for (let i = windowStart; i <= windowEnd; i += 1) add(i);
    if (windowEnd < totalPages) { if (windowEnd < totalPages - 1) pages.push(`<span class="muted">…</span>`); add(totalPages); }
    pages.push(`<button class="pager-btn" data-page="${S.page + 1}" ${S.page >= totalPages ? "disabled" : ""}>下一页</button>`);
    pager.innerHTML = `<div class="muted">每页 ${S.pageSize} 条</div><div class="pager-btns">${pages.join("")}</div>`;
  }

  // ------------------------------------------------------------------- logs
  function logsTable(logs, compact = false) {
    if (!logs || !logs.length) {
      return `<div class="empty"><b>还没有请求</b>${compact ? "去「接入」页拿 curl 示例试试。" : "刷新后再看。"}</div>`;
    }
    if (compact) {
      return `<div class="table-wrap"><table class="data-table" style="min-width:520px">
        <thead><tr><th>时间</th><th>模型</th><th>提供商</th><th>状态</th><th>延迟</th></tr></thead>
        <tbody>${logs.map((l) => `<tr>
          <td class="muted">${escapeHtml(timeAgo(l.ts))}</td>
          <td><span class="model-id"><span class="mid-text" title="${escapeHtml(l.model)}">${escapeHtml(l.model)}</span></span></td>
          <td>${escapeHtml(l.provider)}</td>
          <td>${badge(l.ok ? "online" : "offline")}</td>
          <td>${escapeHtml(fmtLatency(l.latency_ms))}</td>
        </tr>`).join("")}</tbody>
      </table></div>`;
    }
    return `<div class="table-wrap"><table class="data-table" style="min-width:900px">
      <thead><tr><th>时间</th><th>模型</th><th>提供商</th><th>状态</th><th>延迟</th><th>Token</th><th>流式</th><th>错误</th></tr></thead>
      <tbody>${logs.map((l) => `<tr>
        <td class="muted" title="${escapeHtml(l.ts)}">${escapeHtml(timeAgo(l.ts))}</td>
        <td><span class="model-id"><span class="mid-text" title="${escapeHtml(l.model)}">${escapeHtml(l.model)}</span></span></td>
        <td>${escapeHtml(l.provider)}</td>
        <td>${badge(l.ok ? "online" : "offline")} <span class="muted">${escapeHtml(String(l.status || ""))}</span></td>
        <td>${escapeHtml(fmtLatency(l.latency_ms))}</td>
        <td>${fmtInt((l.tokens_in || 0) + (l.tokens_out || 0))}${l.estimated ? '<span class="muted">*</span>' : ""}</td>
        <td>${l.stream ? "是" : "否"}</td>
        <td class="muted" title="${escapeHtml(l.error || "")}">${escapeHtml((l.error || "").slice(0, 80))}</td>
      </tr>`).join("")}</tbody>
    </table></div>`;
  }

  function renderLogs() {
    const logs = S.logs || [];
    $("#logsTable tbody").innerHTML = logs.length
      ? logs.map((l) => `<tr>
          <td class="muted" title="${escapeHtml(l.ts)}">${escapeHtml(timeAgo(l.ts))}</td>
          <td><span class="model-id"><span class="mid-text" title="${escapeHtml(l.model)}">${escapeHtml(l.model)}</span></span></td>
          <td>${escapeHtml(l.provider)}</td>
          <td>${badge(l.ok ? "online" : "offline")} <span class="muted">${escapeHtml(String(l.status || ""))}</span></td>
          <td>${escapeHtml(fmtLatency(l.latency_ms))}</td>
          <td>${fmtInt((l.tokens_in || 0) + (l.tokens_out || 0))}${l.estimated ? '<span class="muted">*</span>' : ""}</td>
          <td>${l.stream ? "是" : "否"}</td>
          <td class="muted" title="${escapeHtml(l.error || "")}">${escapeHtml((l.error || "").slice(0, 80))}</td>
        </tr>`).join("")
      : `<tr><td colspan="8"><div class="empty"><b>还没有请求</b>去「接入」页调用一次 API。</div></td></tr>`;
    $("#logCount").textContent = `${fmtInt(logs.length)} 条请求`;
  }

  // ----------------------------------------------------------------- access
  function renderAccess() {
    const protocol = S.protocol || "chat";
    const select = $("#protocolSelect");
    if (select) select.value = protocol;
    const port = S.settings?.port || 8899;
    const host = `http://127.0.0.1:${port}`;
    const base = protocol === "messages" ? host : `${host}/v1`;
    const key = "fah-...";
    $("#accessBase").textContent = base;
    $("#accessKey").textContent = apiKeyLabel();
    const hint = {
      chat: "DSH 里选「OpenAI Chat Completions」，Base URL 填到 /v1。",
      responses: "DSH 里选「OpenAI Responses」，Base URL 填到 /v1。",
      messages: "DSH 里选「Anthropic Messages」，Base URL 只填到端口，不要加 /v1。",
    }[protocol] || "";
    $("#protocolHint").textContent = hint;

    if (protocol === "messages") {
      $("#accessCurl").textContent = `curl ${base}/v1/messages \\
  -H "Content-Type: application/json" \\
  -H "x-api-key: ${key}" \\
  -H "anthropic-version: 2023-06-01" \\
  -d '${JSON.stringify({ model: "auto", max_tokens: 1024, messages: [{ role: "user", content: "你好" }] })}'`;
      $("#accessPython").textContent = `from anthropic import Anthropic

client = Anthropic(
    base_url="${base}",
    api_key="${key}",
)
resp = client.messages.create(
    model="auto",
    max_tokens=1024,
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.content[0].text)`;
      $("#accessDsh").textContent = `# DSH 添加模型
API 协议: Anthropic Messages
Base URL: ${base}
API Key: ${key}
模型名: auto   # 或任意名称`;
      $("#accessNotice").innerHTML = "Anthropic Messages 的 Base URL 只填到端口；SDK 会自己拼 <code>/v1/messages</code>。模型名可以随便填，未知名称会走权重池。";
    } else if (protocol === "responses") {
      $("#accessCurl").textContent = `curl ${base}/responses \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${key}" \\
  -d '${JSON.stringify({ model: "auto", input: "你好" })}'`;
      $("#accessPython").textContent = `from openai import OpenAI

client = OpenAI(
    base_url="${base}",
    api_key="${key}",
)
resp = client.responses.create(
    model="auto",
    input="你好",
)
print(resp.output_text)`;
      $("#accessDsh").textContent = `# DSH 添加模型
API 协议: OpenAI Responses
Base URL: ${base}
API Key: ${key}
模型名: auto   # 或任意名称`;
      $("#accessNotice").innerHTML = "Responses 协议会转换成上游 Chat Completions；模型名可以随便填，未知名称会按权重池分发，失败自动切换。";
    } else {
      $("#accessCurl").textContent = `curl ${base}/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${key}" \\
  -d '${JSON.stringify({ model: "auto", messages: [{ role: "user", content: "你好" }] })}'`;
      $("#accessPython").textContent = `from openai import OpenAI

client = OpenAI(
    base_url="${base}",
    api_key="${key}",
)
resp = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)`;
      $("#accessDsh").textContent = `# DSH 添加模型
API 协议: OpenAI Chat Completions
Base URL: ${base}
API Key: ${key}
模型名: auto   # 或任意名称`;
      $("#accessNotice").innerHTML = "模型名可以填 <code>auto</code>，也可以随便写一个名字；网关会按权重池自动分发，失败时自动切换。";
    }
  }

  // --------------------------------------------------------------- settings
  function renderSettings() {
    if (!S.settings) return;
    const form = $("#settingsForm");
    const s = S.settings;
    form.elements.refresh_interval_sec.value = s.refresh_interval_sec ?? 600;
    form.elements.routing_strategy.value = s.routing_strategy || "balanced";
    form.elements.upstream_proxy.value = s.upstream_proxy || "";
    form.elements.request_timeout_sec.value = s.request_timeout_sec ?? 90;
    form.elements.max_retries.value = s.max_retries ?? 2;
    form.elements.enable_keyless.checked = !!s.enable_keyless;
    form.elements.enable_keyed.checked = !!s.enable_keyed;
    form.elements.api_key.value = s.api_key || "";
    form.elements.dashboard_password.value = "";
    $("#settingsHint").textContent = "API Key 始终启用；面板密码默认 admin，可在上方修改。";
  }

  async function saveSettings(event) {
    event.preventDefault();
    const form = $("#settingsForm");
    const payload = {
      refresh_interval_sec: Number(form.elements.refresh_interval_sec.value),
      routing_strategy: form.elements.routing_strategy.value,
      upstream_proxy: form.elements.upstream_proxy.value.trim(),
      request_timeout_sec: Number(form.elements.request_timeout_sec.value),
      max_retries: Number(form.elements.max_retries.value),
      enable_keyless: form.elements.enable_keyless.checked,
      enable_keyed: form.elements.enable_keyed.checked,
      api_key: form.elements.api_key.value.trim(),
    };
    if (!payload.api_key) {
      toast("API Key 不能为空", "bad");
      form.elements.api_key.focus();
      return;
    }
    const newPassword = form.elements.dashboard_password.value;
    if (newPassword) payload.dashboard_password = newPassword;
    try {
      const result = await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
      S.settings = result.settings;
      toast(newPassword ? "设置与面板密码已保存" : "设置已保存", "ok");
      renderSettings();
      renderAccess();
      renderHeader();
      setRefreshButtonState(false);
    } catch (err) {
      toast(err.message, "bad");
    }
  }

  // ----------------------------------------------------------------- drawer
  function openProvider(id) {
    const p = providerById(id);
    if (!p) return;
    S.activeProvider = id;
    const models = modelsForProvider(id).sort((a, b) => (a.status === "available" ? -1 : 1) - (b.status === "available" ? -1 : 1) || a.id.localeCompare(b.id));
    const keySection = p.key_required || p.key_optional ? `
      <div class="drawer-section">
        <h4>API Key</h4>
        <div class="key-box">
          <input id="providerKeyInput" type="password" autocomplete="new-password" placeholder="${p.key_configured ? `已配置 (${escapeHtml(p.key_masked || "")})，输入新 Key 可替换` : "输入 API Key"}">
          <button class="btn small primary" id="saveProviderKey">保存</button>
          ${p.key_configured ? `<button class="btn small ghost" id="deleteProviderKey">删除</button>` : ""}
        </div>
        <div class="muted" style="margin-top:6px;font-size:10.5px">也可用环境变量：${(p.key_hints || []).map((h) => `<code>${escapeHtml(h)}</code>`).join(" 或 ") || "—"}</div>
      </div>` : `<div class="drawer-section"><h4>API Key</h4><div class="notice">免 Key 提供商，不需要配置密钥。</div></div>`;
    const modelRows = models.length ? models.slice(0, 60).map((m) => `<div class="drawer-model"><code title="${escapeHtml(m.id)}">${escapeHtml(m.id)}</code><span style="display:flex;gap:4px;align-items:center">${badge(m.status)}<button class="btn small" data-test-model="${escapeHtml(m.id)}">测试</button></span></div>`).join("") : `<div class="empty">目录里暂无已知模型 ID</div>`;
    $("#drawerTitle").textContent = p.name;
    $("#drawerEyebrow").textContent = p.id.toUpperCase();
    $("#drawerBody").innerHTML = `
      <div class="drawer-section">
        <div class="pc-badges" style="justify-content:flex-start">${badge(p.status)}${p.keyless ? '<span class="badge accent no-dot">免 Key</span>' : ""}${p.key_required ? `<span class="badge no-dot">${p.key_configured ? "已配 Key" : "需要 Key"}</span>` : ""}${p.restricted ? '<span class="badge think no-dot">受限</span>' : ""}</div>
        <dl class="drawer-kv" style="margin-top:12px">
          <dt>Base URL</dt><dd>${escapeHtml(p.base_url || "—")}</dd>
          <dt>状态</dt><dd>${escapeHtml(statusInfo(p.status).label)} · ${fmtLatency(p.latency_ms)}</dd>
          <dt>免费模型</dt><dd>${fmtInt(p.free_model_count)} 个（目录）· ${fmtInt(p.enabled_models)} 个已启用</dd>
          <dt>上下文</dt><dd>${escapeHtml(p.max_context || "—")}</dd>
          <dt>Tier</dt><dd>${escapeHtml(p.tier || "—")}</dd>
          <dt>来源</dt><dd>${(p.source || []).map(escapeHtml).join(" + ") || "—"}</dd>
          <dt>检测时间</dt><dd>${escapeHtml(timeAgo(p.last_checked))}</dd>
        </dl>
        ${p.notes ? `<div class="notice" style="margin-top:10px">${escapeHtml(p.notes)}</div>` : ""}
        ${p.error ? `<div class="notice" style="margin-top:10px;color:var(--bad);background:var(--bad-soft);border-color:rgba(181,71,59,.25)">${escapeHtml(p.error)}</div>` : ""}
      </div>
      ${keySection}
      <div class="drawer-section">
        <h4>操作</h4>
        <div class="chips">
          ${p.routable && !p.restricted ? `<button class="btn small primary" id="testProviderBtn">测试提供商</button>` : ""}
          <button class="btn small" id="refreshProviderBtn">重新巡检全部</button>
          <a class="btn small ghost" href="${escapeHtml(p.key_url || "#")}" target="_blank" rel="noopener noreferrer" ${p.key_url ? "" : 'style="display:none"'}>申请 Key ↗</a>
        </div>
        <div class="test-result" id="providerTestResult" style="margin-top:10px">尚未测试</div>
      </div>
      <div class="drawer-section">
        <h4>模型（${models.length}）</h4>
        <div class="drawer-models">${modelRows}</div>
      </div>`;
    $("#drawer").classList.remove("hidden");
    $("#drawerOverlay").classList.remove("hidden");
    $("#saveProviderKey")?.addEventListener("click", () => saveProviderKey(id));
    $("#deleteProviderKey")?.addEventListener("click", () => deleteProviderKey(id));
    $("#testProviderBtn")?.addEventListener("click", () => testProvider(id));
    $("#refreshProviderBtn")?.addEventListener("click", triggerRefresh);
    $$("#drawerBody [data-test-model]").forEach((btn) => btn.addEventListener("click", () => openTest(btn.dataset.testModel)));
  }

  function closeDrawer() {
    $("#drawer").classList.add("hidden");
    $("#drawerOverlay").classList.add("hidden");
    S.activeProvider = null;
  }

  async function saveProviderKey(id) {
    const input = $("#providerKeyInput");
    const value = input?.value.trim() || "";
    if (!value) { toast("请输入 API Key", "bad"); input?.focus(); return; }
    try {
      await api(`/api/providers/${encodeURIComponent(id)}/key`, { method: "POST", body: JSON.stringify({ api_key: value }) });
      toast("Key 已保存，正在检测…", "ok");
      setTimeout(async () => { await loadAll(); if (S.activeProvider) openProvider(S.activeProvider); }, 2200);
    } catch (err) { toast(err.message, "bad"); }
  }

  async function deleteProviderKey(id) {
    try {
      await api(`/api/providers/${encodeURIComponent(id)}/key`, { method: "DELETE" });
      toast("Key 已删除", "ok");
      await loadAll();
      openProvider(id);
    } catch (err) { toast(err.message, "bad"); }
  }

  async function testProvider(id) {
    const el = $("#providerTestResult");
    if (el) el.textContent = "正在请求…";
    try {
      const result = await api(`/api/providers/${encodeURIComponent(id)}/test`, { method: "POST", body: JSON.stringify({}) });
      if (el) el.textContent = result.ok
        ? `HTTP 200 · ${Math.round(result.latency_ms)}ms\n模型: ${result.model}\n\n${result.reply || "(空回复)"}`
        : `失败: ${result.error || result.code || "unknown"}\n模型: ${result.model || "—"}`;
    } catch (err) {
      if (el) el.textContent = `失败: ${err.message}`;
    }
  }

  // ------------------------------------------------------------------ modal
  function encodeModelPath(modelId) {
    return String(modelId || "").split("/").map(encodeURIComponent).join("/");
  }

  function openTest(modelId) {
    S.testModel = modelId;
    $("#testTitle").textContent = "测试模型";
    $("#testModel").textContent = modelId;
    $("#testResult").innerHTML = '<span class="muted">尚未发送</span>';
    $("#testHint").textContent = "";
    $("#testModal").classList.remove("hidden");
  }

  function closeTest() {
    $("#testModal").classList.add("hidden");
    S.testModel = null;
  }

  async function runTest() {
    if (!S.testModel) return;
    const prompt = $("#testPrompt").value.trim() || "Reply with exactly: OK";
    const btn = $("#testRun");
    btn.disabled = true;
    $("#testHint").textContent = "请求中…";
    $("#testResult").textContent = "正在等待上游…";
    try {
      const result = await api(`/api/models/${encodeModelPath(S.testModel)}/test`, { method: "POST", body: JSON.stringify({ prompt }) });
      if (result.ok) {
        $("#testResult").textContent = `HTTP 200 · ${Math.round(result.latency_ms)}ms\n提供商: ${result.provider_name} (${result.provider})\n模型: ${result.model}\n\n${result.reply || "(空回复)"}`;
        $("#testHint").textContent = "";
      } else {
        $("#testResult").textContent = `失败: ${result.error || result.code || "unknown"}\n模型: ${result.model || S.testModel}`;
        $("#testHint").textContent = "检查该提供商的 Key 或稍后重试";
      }
    } catch (err) {
      $("#testResult").textContent = `失败: ${err.message}`;
      $("#testHint").textContent = "";
    } finally {
      btn.disabled = false;
    }
  }

  // ----------------------------------------------------------------- refresh
  async function triggerRefresh() {
    if (S.refreshing) return;
    S.refreshing = true;
    setRefreshButtonState(true);
    try {
      await api("/api/refresh", { method: "POST", body: "{}" });
      toast("已开始巡检，请稍候…", "");
      pollRefresh();
    } catch (err) {
      S.refreshing = false;
      setRefreshButtonState(false);
      toast(err.message, "bad");
    }
  }

  async function pollRefresh() {
    try {
      const result = await api("/api/refresh/status");
      const refresh = result.refresh || {};
      if (refresh.running) {
        $("#lastRefresh").textContent = `巡检中 ${refresh.done}/${refresh.total}`;
        setTimeout(pollRefresh, 1000);
        return;
      }
      S.refreshing = false;
      setRefreshButtonState(false);
      await loadAll();
      toast("巡检完成", "ok");
    } catch (err) {
      S.refreshing = false;
      setRefreshButtonState(false);
      toast(err.message, "bad");
    }
  }

  // ------------------------------------------------------------------- bind
  function bind() {
    $$(".tab-btn").forEach((btn) => btn.addEventListener("click", () => setTab(btn.dataset.tab)));
    $$("[data-tab-link]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); setTab(link.dataset.tabLink); }));
    $("#refreshBtn").addEventListener("click", triggerRefresh);
    $("#heroRefresh").addEventListener("click", triggerRefresh);
    $("#themeBtn").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
    });
    $("#drawerClose").addEventListener("click", closeDrawer);
    $("#drawerOverlay").addEventListener("click", closeDrawer);
    $("#testClose").addEventListener("click", closeTest);
    $("#testRun").addEventListener("click", runTest);
    $("#clientsRefresh").addEventListener("click", async () => {
      try {
        const result = await api("/api/clients");
        S.clients = result.clients || [];
        renderClients();
        toast("已重新检测", "ok");
      } catch (err) { toast(err.message, "bad"); }
    });
    $("#clientGrid").addEventListener("click", (event) => {
      const configure = event.target.closest("[data-client-configure]");
      if (configure) { configureClient(configure.dataset.clientConfigure); return; }
      const restore = event.target.closest("[data-client-restore]");
      if (restore) restoreClient(restore.dataset.clientRestore);
    });
    $("#dataRefresh").addEventListener("click", async () => {
      try {
        const [overview, logs] = await Promise.all([api("/api/overview"), api("/api/logs?limit=300")]);
        S.overview = overview;
        S.logs = logs.logs || [];
        renderOverview();
        renderData();
        toast("数据已刷新", "ok");
      } catch (err) { toast(err.message, "bad"); }
    });
    $("#logsRefresh").addEventListener("click", async () => {
      try {
        const result = await api("/api/logs?limit=300");
        S.logs = result.logs || [];
        renderLogs();
        toast("日志已刷新", "ok");
      } catch (err) { toast(err.message, "bad"); }
    });
    $("#settingsForm").addEventListener("submit", saveSettings);
    $("#copyApiKey").addEventListener("click", () => copyText($("#settingsForm").elements.api_key.value));
    $("#copyFullApiKey").addEventListener("click", () => copyText(S.settings?.api_key || ""));

    $("#protocolSelect").addEventListener("change", (event) => {
      S.protocol = event.target.value;
      renderAccess();
    });
    $("#providerSearch").addEventListener("input", (event) => { S.providerSearch = event.target.value; renderProviders(); });
    $("#modelSearch").addEventListener("input", (event) => { S.modelSearch = event.target.value; S.page = 1; renderModels(); });
    $("#providerSelect").addEventListener("change", (event) => { S.providerSelect = event.target.value; S.page = 1; renderModels(); });
    $$("#providerFilters .chip").forEach((chip) => chip.addEventListener("click", () => {
      S.providerFilter = chip.dataset.filter;
      $$("#providerFilters .chip").forEach((c) => c.classList.toggle("active", c === chip));
      renderProviders();
    }));
    $$("#modelFilters .chip").forEach((chip) => chip.addEventListener("click", () => {
      S.modelFilter = chip.dataset.filter;
      S.page = 1;
      $$("#modelFilters .chip").forEach((c) => c.classList.toggle("active", c === chip));
      renderModels();
    }));

    $("#providerGrid").addEventListener("click", (event) => {
      const card = event.target.closest("[data-provider-id]");
      if (card) openProvider(card.dataset.providerId);
    });
    $("#keylessGrid").addEventListener("click", (event) => {
      const card = event.target.closest("[data-provider-id]");
      if (card) openProvider(card.dataset.providerId);
    });
    $("#keylessRoutes").addEventListener("click", (event) => {
      const chip = event.target.closest("[data-copy]");
      if (chip) copyText(chip.dataset.copy);
    });
    $("#modelsTable").addEventListener("click", (event) => {
      const copy = event.target.closest("[data-copy]");
      if (copy) { copyText(copy.dataset.copy); return; }
      const test = event.target.closest("[data-test-model]");
      if (test) { openTest(test.dataset.testModel); return; }
      const page = event.target.closest("[data-page]");
      if (page && !page.disabled) { S.page = Number(page.dataset.page); renderModels(); }
    });
    $("#modelsTable").addEventListener("change", (event) => {
      const toggle = event.target.closest("[data-policy-enabled]");
      if (toggle) {
        updateModelPolicy(toggle.dataset.policyEnabled, { enabled: toggle.checked });
        return;
      }
      const weight = event.target.closest("[data-policy-weight]");
      if (weight) {
        const value = Number(weight.value);
        if (!Number.isFinite(value) || value < 0 || value > 100) {
          toast("权重范围是 0–100", "bad");
          renderModels();
          return;
        }
        updateModelPolicy(weight.dataset.policyWeight, { weight: value });
      }
    });
    $("#modelPager").addEventListener("click", (event) => {
      const page = event.target.closest("[data-page]");
      if (page && !page.disabled) { S.page = Number(page.dataset.page); renderModels(); }
    });
    document.addEventListener("click", (event) => {
      const copy = event.target.closest("[data-copy-target]");
      if (copy) {
        const target = document.getElementById(copy.dataset.copyTarget);
        if (target) copyText(target.textContent || "");
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        if (!$("#testModal").classList.contains("hidden")) closeTest();
        else if (!$("#drawer").classList.contains("hidden")) closeDrawer();
      }
    });
    setInterval(loadLive, 20000);
  }

  async function init() {
    initTheme();
    bind();
    try {
      await loadAll();
      const params = new URLSearchParams(location.search);
      const requestedTheme = params.get("theme");
      if (requestedTheme === "dark" || requestedTheme === "light") applyTheme(requestedTheme);
      const requestedTab = params.get("tab");
      if (requestedTab && document.getElementById(`tab-${requestedTab}`)) setTab(requestedTab);
      const requestedProvider = params.get("provider");
      if (requestedProvider) openProvider(requestedProvider);
      const requestedTest = params.get("test");
      if (requestedTest) openTest(requestedTest);
      if (S.overview?.refresh?.running) { S.refreshing = true; setRefreshButtonState(true); pollRefresh(); }
    } catch (err) {
      $("#topMeta").textContent = "无法连接后端";
      $("#liveDot").className = "live-dot bad";
      toast(`加载失败：${err.message}`, "bad");
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
