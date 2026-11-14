const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  tunnels: [],
  mappings: {},
  settings: {},
  currentId: null,
  zones: [],
  logOpen: false,
  logOffset: 0,
  busy: false,
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
    cache: "no-store",
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok) {
    const msg = data.detail || data.message || `请求失败（HTTP ${res.status}）`;
    throw new Error(msg);
  }
  return data;
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

let toastTimer = null;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = `toast ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3200);
}

function openModal(html) {
  $("#modalCard").innerHTML = html;
  $("#modal").classList.remove("hidden");
  $$("[data-close]", $("#modalCard")).forEach(b => b.onclick = closeModal);
  $(".modal-card input, .modal-card textarea")?.focus?.();
}
function closeModal() { $("#modal").classList.add("hidden"); $("#modalCard").innerHTML = ""; }

function confirmDialog(title, text, okLabel, onOk) {
  openModal(`
    <h2>${esc(title)}</h2>
    <p class="sub">${esc(text)}</p>
    <div class="modal-actions">
      <button data-close>取消</button>
      <button class="primary" id="cfmOk">${esc(okLabel || "确定")}</button>
    </div>`);
  $("#cfmOk").onclick = async () => {
    closeModal();
    try { await onOk(); } catch (e) { toast(e.message, "err"); }
  };
}

function tunnelById(id) { return state.tunnels.find(t => t.id === id); }
function mappingsOf(id) { return state.mappings[id] || []; }

// 新建/编辑后先把服务端返回的这条塞进本地状态，列表立刻能看见，
// 不依赖随后的 /api/state（网络慢或状态接口失败时也不会“看起来没加上”）。
function rememberMapping(tid, m) {
  if (!m) return;
  const list = state.mappings[tid] || (state.mappings[tid] = []);
  const i = list.findIndex(x => String(x.id) === String(m.id));
  if (i >= 0) list[i] = m; else list.push(m);
}

/* --------------------------------------------------------------- bootstrap */
async function loadState() {
  const data = await api("/api/state");
  if (!data.authenticated) {
    $("#mainView").classList.add("hidden");
    if (data.needs_setup) {
      $("#loginView").classList.add("hidden");
      $("#setupView").classList.remove("hidden");
      $("#setupPw").focus();
      return;
    }
    $("#setupView").classList.add("hidden");
    $("#loginView").classList.remove("hidden");
    const r = await api("/api/reveal", { method: "POST" }).catch(() => ({ needs_setup: false }));
    if (r.needs_setup) {
      $("#loginView").classList.add("hidden");
      $("#setupView").classList.remove("hidden");
    }
    $("#loginHint").textContent = "";
    return;
  }
  state.tunnels = data.tunnels || [];
  state.mappings = data.mappings || {};
  state.settings = data.settings || {};
  if (!state.currentId || !tunnelById(state.currentId)) {
    state.currentId = state.tunnels[0]?.id || null;
  }
  $("#setupView").classList.add("hidden");
  $("#loginView").classList.add("hidden");
  $("#mainView").classList.remove("hidden");
  renderTopbar();
  renderTunnels();
  renderContent();
}

function renderTopbar() {
  const s = state.settings;
  const chip = $("#accountChip");
  chip.textContent = s.has_token ? "Cloudflare 已连接" : "未配置 Token";
  chip.className = "chip " + (s.has_token ? "ok" : "off");
  $("#autoDnsChip").textContent = s.auto_dns ? "自动 DNS 开" : "自动 DNS 关";
  $("#autoDnsChip").className = "chip " + (s.auto_dns ? "ok" : "");
  const banner = $("#tokenBanner");
  if (banner) banner.classList.toggle("hidden", !!s.has_token);
}

function renderTunnels() {
  const list = $("#tunnelList");
  if (!state.tunnels.length) {
    list.innerHTML = `<div class="empty" style="padding:26px 8px">还没有连接<br><span class="muted">点右上「新建」创建一条</span></div>`;
    return;
  }
  list.innerHTML = state.tunnels.map(t => {
    const st = t.status?.state || "stopped";
    const sub = t.hosts?.length ? t.hosts.slice(0, 2).join(" · ") : (t.mode === "token" ? "控制台托管" : "未配置映射");
    return `<div class="tunnel-item ${t.id === state.currentId ? "active" : ""}" data-tid="${t.id}">
      <span class="dot ${st}"></span>
      <div class="tunnel-meta">
        <div class="tunnel-name">${esc(t.name)}</div>
        <div class="tunnel-sub">${esc(sub)}</div>
      </div>
      <span class="badge ${st}">${stateLabel(st)}</span>
    </div>`;
  }).join("");
  $$(".tunnel-item", list).forEach(el => el.onclick = () => {
    state.currentId = el.dataset.tid;
    state.logOpen = false;
    state.logOffset = 0;
    renderTunnels();
    renderContent();
  });
}

function stateLabel(st) {
  return { connected: "在线", connecting: "连接中", error: "异常", stopped: "未运行" }[st] || st;
}

/* --------------------------------------------------------------- content */
function renderContent() {
  const c = $("#content");
  if (!state.tunnels.length) {
    c.innerHTML = `
      <div class="card" style="text-align:center;padding:56px 24px">
        <div style="font-size:40px;margin-bottom:10px">🛰️</div>
        <h2 style="font-size:18px;margin:0 0 8px">开始一条新的穿透连接</h2>
        <p class="muted" style="margin:0 0 22px">新建 Cloudflare 隧道，或把已有隧道的 Token 粘贴进来纳管。</p>
        <div style="display:flex;gap:10px;justify-content:center">
          <button class="primary" id="emptyNew">新建连接</button>
          <button id="emptyImport">导入 Token</button>
        </div>
      </div>`;
    $("#emptyNew").onclick = openNewTunnel;
    $("#emptyImport").onclick = openImportToken;
    return;
  }
  const t = tunnelById(state.currentId);
  if (!t) return;
  const st = t.status || {};
  const rows = mappingsOf(t.id);

  c.innerHTML = `
    <div class="detail-head">
      <div>
        <div class="detail-title">${esc(t.name)} <span class="badge ${st.state}" id="statusBadge">${stateLabel(st.state)}</span></div>
        <div class="detail-sub">
          <span>隧道 ID <code>${esc(t.cf_id)}</code></span>
          <span>${t.mode === "local" ? "本地配置下发" : "控制台托管"}</span>
          <span>${rows.length} 条映射</span>
          ${st.metrics_port ? `<span>健康检查端口 ${st.metrics_port}</span>` : ""}
        </div>
      </div>
      <div class="detail-actions">
        ${t.mode === "token" ? `<button class="primary" id="takeoverBtn">接管到面板</button>` : ""}
        <button id="restartBtn">重启</button>
        <button id="toggleRunBtn">${st.running ? "停止" : "启动"}</button>
        <button id="configBtn">查看配置</button>
        <button class="danger" id="deleteTunnelBtn">删除连接</button>
      </div>
    </div>

    <div class="card">
      <div class="card-title">快速映射 <span class="muted" style="text-transform:none;letter-spacing:0">填端口即用，秒级生效</span></div>
      <div class="quick" id="quickAdd">
        <div class="qg" style="flex:2 1 300px">
          <label class="field-label">公网域名</label>
          <div class="host-group">
            <input id="qSub" placeholder="子域名，如 nas" autocomplete="off">
            <span class="dot-sep">.</span>
            <select id="qZone" data-zone-select></select>
          </div>
        </div>
        <div class="qg" style="flex:0 0 84px">
          <label class="field-label">协议</label>
          <select id="qScheme">
            <option value="http">http</option>
            <option value="https">https</option>
          </select>
        </div>
        <div class="qg" style="flex:1 1 160px">
          <label class="field-label">目标主机</label>
          <input id="qHost" value="127.0.0.1" autocomplete="off">
        </div>
        <div class="qg" style="flex:0 0 90px">
          <label class="field-label">端口</label>
          <input id="qPort" type="number" min="1" max="65535" placeholder="8080">
        </div>
        <div class="qg" style="flex:0 0 auto; display:flex; gap:8px; align-items:flex-end">
          <button class="mini" id="qTest" title="测试容器到目标的连通性">测试</button>
          <button class="mini" id="qScan" title="扫描目标主机开放端口">扫描端口</button>
          <button class="primary" id="qAdd">添加映射</button>
        </div>
      </div>
      <div class="form-row" style="margin:12px 0 0">
        <input id="qNote" placeholder="备注（可选，例如：群晖 DSM）" autocomplete="off">
      </div>
    </div>

    <div class="card">
      <div class="card-title">映射列表 <button class="mini" id="syncDnsBtn">同步全部 DNS</button></div>
      <div id="mapTableWrap"></div>
    </div>

    <div class="card">
      <div class="card-title log-head">
        <span>运行日志</span>
        <span class="row-gap">
          <button class="mini" id="copyLogBtn">复制</button>
          <button class="mini" id="toggleLogBtn">${state.logOpen ? "收起" : "展开"}</button>
        </span>
      </div>
      <div class="log-box ${state.logOpen ? "" : "hidden"}" id="logBox"></div>
    </div>`;

  renderMapTable();
  loadZones();

  $("#restartBtn").onclick = () => runAction("restart");
  $("#toggleRunBtn").onclick = () => runAction(st.running ? "stop" : "start");
  $("#configBtn").onclick = showConfig;
  $("#deleteTunnelBtn").onclick = deleteTunnel;
  $("#syncDnsBtn").onclick = syncDns;
  $("#takeoverBtn")?.addEventListener("click", takeoverTunnel);
  $("#qAdd").onclick = quickAdd;
  $("#qTest").onclick = testPort;
  $("#qScan").onclick = openScanModal;
  $("#toggleLogBtn").onclick = toggleLogs;
  $("#copyLogBtn").onclick = copyLogs;
  if (state.logOpen) { $("#toggleLogBtn").textContent = "收起"; loadLogs(true); }
}

function renderMapTable() {
  const wrap = $("#mapTableWrap");
  if (!wrap) return;
  const t = tunnelById(state.currentId);
  const rows = mappingsOf(t.id);
  if (!rows.length) {
    wrap.innerHTML = `<div class="empty">还没有映射。上面填一个子域名和端口，点「添加」即可。</div>`;
    return;
  }
  wrap.innerHTML = `
    <table class="map-table">
      <thead><tr><th>公网域名</th><th>内网目标</th><th>DNS</th><th>备注</th><th style="text-align:right">操作</th></tr></thead>
      <tbody>
        ${rows.map(m => `
          <tr data-mid="${m.id}">
            <td>
              <div class="map-host">
                <a href="${esc(m.url)}" target="_blank" rel="noreferrer">${esc(m.hostname)}${m.path ? `<span class="path">${esc(m.path)}</span>` : ""}</a>
              </div>
            </td>
            <td><span class="svc">${esc(m.service)}</span>${m.no_tls_verify ? ` <span class="dns-pill">跳过证书校验</span>` : ""}</td>
            <td><span class="dns-pill ${m.dns_record_id ? "on" : ""}">${m.dns_record_id ? "已解析" : "未创建"}</span></td>
            <td class="muted" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(m.note || "-")}</td>
            <td>
              <div class="actions">
                <button class="mini" data-act="copy">复制</button>
                <button class="mini" data-act="edit">编辑</button>
                <button class="mini danger" data-act="del">删除</button>
              </div>
            </td>
          </tr>`).join("")}
      </tbody>
    </table>`;
  $$("tr[data-mid]", wrap).forEach(tr => {
    const mid = tr.dataset.mid;
    $$("button[data-act]", tr).forEach(b => b.onclick = () => {
      const m = rows.find(x => x.id === mid);
      if (b.dataset.act === "copy") copyText(m.url);
      if (b.dataset.act === "edit") editMapping(m);
      if (b.dataset.act === "del") deleteMapping(m);
    });
  });
}

function spacing(step = 10) { return `<div style="height:${step}px"></div>`; }

/* --------------------------------------------------------------- zones */
async function loadZones() {
  if (!state.zones.length) {
    try {
      const data = await api("/api/zones");
      state.zones = data.zones || [];
    } catch (e) {
      state.zones = [];
    }
  }
  const last = localStorage.getItem("td_last_zone") || "";
  const opts = state.zones.map(z => `<option value="${esc(z.name)}">${esc(z.name)}</option>`).join("");
  const dl = $("#zoneList");
  if (dl) dl.innerHTML = state.zones.map(z => `<option value="${esc(z.name)}"></option>`).join("");
  $$("select[data-zone-select]").forEach(sel => {
    const keep = sel.value || last;
    sel.innerHTML = opts || `<option value="">先配置 Token 才能选域名</option>`;
    if (keep && state.zones.some(z => z.name === keep)) sel.value = keep;
    sel.onchange = () => localStorage.setItem("td_last_zone", sel.value);
  });
}

/* --------------------------------------------------------------- actions */
async function refreshCurrent() {
  const data = await api("/api/state");
  if (!data.authenticated) return;
  state.tunnels = data.tunnels || [];
  state.mappings = data.mappings || {};
  state.settings = data.settings || {};
  renderTopbar();
  renderTunnels();
}

async function runAction(action) {
  const t = tunnelById(state.currentId);
  if (!t || state.busy) return;
  state.busy = true;
  try {
    const r = await api(`/api/tunnels/${t.id}/${action}`, { method: "POST" });
    await refreshCurrent();
    renderContent();
    toast(action === "stop" ? "已停止" : "已重启，稍等几秒看状态", "ok");
  } catch (e) {
    toast(e.message, "err");
  } finally { state.busy = false; }
}

function composeHostname() {
  const sub = ($("#qSub").value || "").trim().toLowerCase();
  const domain = ($("#qZone")?.value || "").trim().toLowerCase();
  if (sub.includes(".")) return sub;
  if (!sub && !domain) throw new Error("请填写子域名");
  if (!sub) return domain;
  if (!domain) throw new Error("请先在设置里配置 Token，再选域名");
  return `${sub}.${domain}`;
}

async function quickAdd() {
  const t = tunnelById(state.currentId);
  if (!t) return;
  let hostname;
  try { hostname = composeHostname(); } catch (e) { return toast(e.message, "err"); }
  const port = parseInt($("#qPort").value, 10);
  if (!port) return toast("请填写本地端口", "err");
  const body = {
    hostname,
    scheme: $("#qScheme").value,
    host: ($("#qHost").value || "127.0.0.1").trim(),
    port,
    note: ($("#qNote").value || "").trim(),
    path: "",
  };
  try {
    const r = await api(`/api/tunnels/${t.id}/mappings`, { method: "POST", body });
    $("#qSub").value = ""; $("#qPort").value = ""; $("#qNote").value = "";
    rememberMapping(t.id, r.mapping);
    await refreshCurrent();
    renderContent();
    if (r.warnings && r.warnings.length) toast(r.warnings[0], "err");
    else toast(`已映射 ${hostname} → ${r.mapping.service}`, "ok");
  } catch (e) {
    toast(e.message, "err");
  }
}

async function testPort() {
  const host = ($("#qHost").value || "127.0.0.1").trim();
  const port = parseInt($("#qPort").value, 10);
  if (!port) return toast("先填端口再测试", "err");
  const r = await api("/api/check", { method: "POST", body: { host, port } });
  if (r.ok) toast(`端口可达，${r.latency_ms}ms`, "ok");
  else toast(`连不上 ${r.host || host}:${port} — ${r.error}`, "err");
}

/* --------------------------------------------------------------- port scan */
function openScanModal() {
  if (!tunnelById(state.currentId)) return;
  const host = ($("#qHost")?.value || "127.0.0.1").trim();
  const sub = ($("#qSub")?.value || "").trim();
  openModal(`
    <h2>扫描端口</h2>
    <p class="sub">扫描在<b>面板所在的容器里</b>执行，扫的是那台机器（NAS / PC）的端口，不是你现在打开网页这台电脑的端口。</p>
    <div class="form-grid">
      <div class="form-row"><label class="field-label">目标主机</label><input id="scHost" value="${esc(host)}"></div>
      <div class="form-row"><label class="field-label">扫描范围</label>
        <select id="scMode">
          <option value="common">常用端口（约 100 个，几秒）</option>
          <option value="range">指定范围</option>
          <option value="full">全端口 1-65535（30~60 秒）</option>
        </select>
      </div>
    </div>
    <div class="form-grid hidden" id="scRangeRow">
      <div class="form-row"><input id="scStart" type="number" value="8000" placeholder="起始端口"></div>
      <div class="form-row"><input id="scEnd" type="number" value="9000" placeholder="结束端口"></div>
    </div>
    <div class="form-grid">
      <div class="form-row"><label class="field-label">子域名（留空自动用 p 端口）</label><input id="scSub" value="${esc(sub)}" placeholder="nas"></div>
      <div class="form-row"><label class="field-label">域名</label><select id="scZone" data-zone-select></select></div>
    </div>
    <div class="scan-bar">
      <button class="primary" id="scRun">开始扫描</button>
      <span class="muted" id="scStatus">等待扫描</span>
    </div>
    <div id="scResult"></div>
    <div class="modal-actions"><button data-close>关闭</button></div>`);
  loadZones();
  $("#scMode").onchange = () => {
    $("#scRangeRow").classList.toggle("hidden", $("#scMode").value !== "range");
  };
  $("#scRun").onclick = runScan;
}

async function runScan() {
  const btn = $("#scRun");
  const mode = $("#scMode").value;
  const body = { host: $("#scHost").value.trim() || "127.0.0.1", mode };
  if (mode === "range") {
    body.start = parseInt($("#scStart").value, 10) || 1;
    body.end = parseInt($("#scEnd").value, 10) || 1;
  }
  btn.disabled = true;
  $("#scStatus").textContent = mode === "full" ? "扫描中… 全端口大概 30~60 秒" : "扫描中…";
  $("#scResult").innerHTML = "";
  try {
    const r = await api("/api/scan", { method: "POST", body });
    const who = r.from ? `${r.from.name}${r.from.ip ? " / " + r.from.ip : ""}` : "面板容器";
    $("#scStatus").textContent =
      `从 ${who} 扫 ${r.host}：${r.scanned} 个端口，${(r.duration_ms / 1000).toFixed(1)} 秒，开放 ${r.open.length} 个`;
    renderScanResult(r);
  } catch (e) {
    $("#scStatus").textContent = "扫描失败：" + e.message;
  } finally {
    btn.disabled = false;
  }
}

function renderScanResult(r) {
  if (!r.open.length) {
    $("#scResult").innerHTML = `<div class="empty">这个范围里没有开放端口，换个主机或范围再试。</div>`;
    return;
  }
  const rows = r.open.map(p => {
    const tcp = p.scheme === "tcp";
    return `<div class="scan-row" data-port="${p.port}" data-scheme="${p.scheme}">
      <span class="scan-port">${p.port}</span>
      <span class="scan-svc">${esc(p.service)}</span>
      <span class="badge ${p.scheme === "https" ? "connected" : ""}">${p.scheme}</span>
      <span class="scan-detail muted">${esc((p.detail || "").slice(0, 46))}</span>
      <button class="mini ${tcp ? "" : "primary"}" data-port="${p.port}" ${tcp ? 'disabled title="TCP 服务不能直接做 HTTP 映射"' : ""}>映射</button>
    </div>`;
  }).join("");
  $("#scResult").innerHTML = `<div class="scan-list">${rows}</div>
    <div class="scan-tip muted">只有 http / https 能走 Cloudflare 隧道对外；SSH、数据库这类 TCP 服务请用 cloudflared access 接入。</div>`;
  $$("button[data-port]", $("#scResult")).forEach(b => b.onclick = () => addFromScan(b));
}

async function addFromScan(btn) {
  const t = tunnelById(state.currentId);
  const port = parseInt(btn.dataset.port, 10);
  const scheme = btn.closest(".scan-row").dataset.scheme;
  const host = $("#scHost").value.trim() || "127.0.0.1";
  const sub = ($("#scSub").value || "").trim().toLowerCase();
  const domain = ($("#scZone").value || "").trim().toLowerCase();
  if (!sub && !domain) return toast("请填写子域名", "err");
  if (sub && !sub.includes(".") && !domain) return toast("请先在设置里配置 Token，再选域名", "err");
  const hostname = sub ? (sub.includes(".") ? sub : `${sub}.${domain}`) : `p${port}.${domain}`;
  btn.disabled = true;
  try {
    const added = await api(`/api/tunnels/${t.id}/mappings`, {
      method: "POST",
      body: { hostname, scheme, host, port, note: host },
    });
    localStorage.setItem("td_last_zone", domain);
    rememberMapping(t.id, added.mapping);
    await refreshCurrent();
    renderContent();
    btn.textContent = "已添加";
    toast(`已添加 ${hostname} → ${host}:${port}`, "ok");
  } catch (e) {
    btn.disabled = false;
    toast(e.message, "err");
  }
}

function editMapping(m) {
  const row = (label, inner) => `<div class="form-row"><label class="field-label">${label}</label>${inner}</div>`;
  openModal(`
    <h2>编辑映射</h2>
    <p class="sub">修改后会自动重启该连接，几秒内生效。</p>
    ${row("公网域名", `<input id="eHost" value="${esc(m.hostname)}">`)}
    <div class="form-grid">
      <div>${row("路径（可选）", `<input id="ePath" value="${esc(m.path || "")}" placeholder="/api">`)}</div>
      <div>${row("备注", `<input id="eNote" value="${esc(m.note || "")}">`)}</div>
    </div>
    <div class="form-grid">
      <div>${row("协议", `<select id="eScheme">
          <option value="http" ${m.scheme === "http" ? "selected" : ""}>http</option>
          <option value="https" ${m.scheme === "https" ? "selected" : ""}>https</option>
        </select>`)}</div>
      <div>${row("端口", `<input id="ePort" type="number" value="${esc(m.port || "")}">`)}</div>
    </div>
    ${row("目标主机", `<input id="eHostTarget" value="${esc(m.host || "127.0.0.1")}">`)}
    ${row("Host 头（可选）", `<input id="eHeader" value="${esc(m.http_host_header || "")}" placeholder="不改写留空">`)}
    <label class="switch"><input type="checkbox" id="eTls" ${m.no_tls_verify ? "checked" : ""}> 跳过源站证书校验（https 自签可用）</label>
    <div style="height:10px"></div>
    <label class="switch"><input type="checkbox" id="eEnabled" ${m.enabled ? "checked" : ""}> 启用这条映射</label>
    <div class="modal-actions">
      <button data-close>取消</button>
      <button class="primary" id="eSave">保存</button>
    </div>`);
  $("#eSave").onclick = async () => {
    const body = {
      hostname: $("#eHost").value.trim(),
      path: $("#ePath").value.trim(),
      scheme: $("#eScheme").value,
      host: $("#eHostTarget").value.trim(),
      port: parseInt($("#ePort").value, 10),
      no_tls_verify: $("#eTls").checked,
      http_host_header: $("#eHeader").value.trim(),
      note: $("#eNote").value.trim(),
      enabled: $("#eEnabled").checked,
    };
    closeModal();
    try {
      await api(`/api/mappings/${m.id}`, { method: "PATCH", body });
      await refreshCurrent();
      renderContent();
      toast("已保存", "ok");
    } catch (e) { toast(e.message, "err"); }
  };
}

function deleteMapping(m) {
  confirmDialog("删除映射", `将移除 ${m.hostname}，同时会删除对应的 DNS 记录。`, "删除", async () => {
    await api(`/api/mappings/${m.id}`, { method: "DELETE" });
    await refreshCurrent();
    renderContent();
    toast("已删除", "ok");
  });
}

async function syncDns() {
  const t = tunnelById(state.currentId);
  try {
    const r = await api(`/api/tunnels/${t.id}/dns_sync`, { method: "POST" });
    await refreshCurrent();
    renderContent();
    if (r.failed.length) toast(`${r.failed.length} 条失败：${r.failed[0]}`, "err");
    else toast(`已同步 ${r.synced.length} 条 DNS`, "ok");
  } catch (e) { toast(e.message, "err"); }
}

async function showConfig() {
  const t = tunnelById(state.currentId);
  const r = await api(`/api/tunnels/${t.id}/config`);
  openModal(`
    <h2>config.yml</h2>
    <p class="sub">面板按当前映射实时生成，cloudflared 用它连接 Cloudflare。</p>
    <div class="log-box" style="height:340px">${esc(r.config)}</div>
    <div class="modal-actions"><button data-close>关闭</button></div>`);
}

async function takeoverTunnel() {
  const t = tunnelById(state.currentId);
  confirmDialog("接管这条连接", "将把该隧道切换为「本地配置」，之后控制台里的映射不再生效，全部由本面板管理。", "确认接管", async () => {
    await api(`/api/tunnels/${t.id}/takeover`, { method: "POST" });
    await refreshCurrent();
    renderContent();
    toast("已接管，现在可以在这里加映射了", "ok");
  });
}

function deleteTunnel() {
  const t = tunnelById(state.currentId);
  confirmDialog("删除连接", `会停止进程并移除面板记录。勾选下方的「同时删除 Cloudflare 隧道」将不可恢复。`, "继续", async () => {
    openModal(`
      <h2>删除「${esc(t.name)}」</h2>
      <p class="sub">选择删除范围。</p>
      <label class="switch"><input type="checkbox" id="dDns" checked> 同时删除 DNS 记录</label>
      <div style="height:10px"></div>
      <label class="switch"><input type="checkbox" id="dCf"> 同时删除 Cloudflare 上的隧道（不可恢复）</label>
      <div class="modal-actions">
        <button data-close>取消</button>
        <button class="danger" id="dOk">删除</button>
      </div>`);
    $("#dOk").onclick = async () => {
      const q = `?delete_cf=${$("#dCf").checked}&delete_dns=${$("#dDns").checked}`;
      closeModal();
      try {
        const r = await api(`/api/tunnels/${t.id}${q}`, { method: "DELETE" });
        state.currentId = null;
        await refreshCurrent();
        renderContent();
        toast(r.warnings?.length ? r.warnings[0] : "已删除", r.warnings?.length ? "err" : "ok");
      } catch (e) { toast(e.message, "err"); }
    };
  });
}

/* --------------------------------------------------------------- tunnel create */
function openNewTunnel() {
  openModal(`
    <h2>新建连接</h2>
    <p class="sub">会在 Cloudflare 上创建一条本地配置隧道，并把凭据存到面板数据卷里。</h2>
    <div class="form-row">
      <label class="field-label">连接名称</label>
      <input id="nName" placeholder="例如：家里的 NAS" autocomplete="off">
    </div>
    <div class="tabs-note">创建后进入「快速映射」，填域名和端口即可对外访问。若还没配 API Token，请先到设置里填。</div>
    <div class="modal-actions">
      <button data-close>取消</button>
      <button class="primary" id="nCreate">创建</button>
    </div>`);
  $("#nName").focus();
  $("#nCreate").onclick = async () => {
    const name = $("#nName").value.trim();
    if (!name) return toast("请填写名称", "err");
    closeModal();
    try {
      const r = await api("/api/tunnels", { method: "POST", body: { name } });
      state.currentId = r.tunnel.id;
      await refreshCurrent();
      renderContent();
      toast("连接已创建", "ok");
    } catch (e) { toast(e.message, "err"); }
  };
}

function openImportToken() {
  openModal(`
    <h2>导入已有连接</h2>
    <p class="sub">把 Cloudflare 控制台里那条隧道的 Token 粘进来，面板负责守护进程和日志。</p>
    <div class="guide">
      <div class="guide-step"><span class="idx">1</span><div>
        <b>找隧道 Token</b><br>
        <a href="https://one.dash.cloudflare.com" target="_blank" rel="noreferrer">Zero Trust</a> → Networks → Tunnels
        → 点开那条隧道 → <i>Configure</i> / <i>Install connector</i>，命令里 <code>--token eyJ…</code> 后面那串就是。
      </div></div>
      <div class="guide-step"><span class="idx">2</span><div>
        <b>它是私钥</b><br>隧道 Token 等于该隧道的凭据，别贴到公开的地方；面板会以 600 权限存在数据卷里。
      </div></div>
    </div>
    <div class="form-row">
      <label class="field-label">Token</label>
      <textarea id="iToken" rows="4" placeholder="eyJhIjoi..."></textarea>
    </div>
    <div class="form-row">
      <label class="field-label">名称（可选）</label>
      <input id="iName" placeholder="留空则读取 Cloudflare 上的隧道名">
    </div>
    <div class="tabs-note">导入后默认沿用控制台配置；想让面板下发映射，点详情里的「接管到面板」。</div>
    <div class="modal-actions">
      <button data-close>取消</button>
      <button class="primary" id="iOk">导入</button>
    </div>`);
  $("#iOk").onclick = async () => {
    const token = $("#iToken").value.trim();
    if (!token) return toast("请粘贴 Token", "err");
    closeModal();
    try {
      const r = await api("/api/tunnels/import", { method: "POST", body: { token, name: $("#iName")?.value || "" } });
      state.currentId = r.tunnel.id;
      await refreshCurrent();
      renderContent();
      if (r.warnings && r.warnings.length) toast(r.warnings[0], "err");
      else toast("已导入", "ok");
    } catch (e) { toast(e.message, "err"); }
  };
}

/* --------------------------------------------------------------- settings */
function tokenGuideHtml() {
  return `<div class="guide">
      <div class="guide-step"><span class="idx">1</span><div>
        <b>Account ID</b><br>
        登录 <a href="https://dash.cloudflare.com" target="_blank" rel="noreferrer">dash.cloudflare.com</a>，
        首页右侧栏「Account ID」直接复制；或看地址栏 <code>dash.cloudflare.com/&lt;32位ID&gt;</code>。
      </div></div>
      <div class="guide-step"><span class="idx">2</span><div>
        <b>API Token</b><br>
        右上头像 → My Profile → <a href="https://dash.cloudflare.com/profile/api-tokens" target="_blank" rel="noreferrer">API Tokens</a>
        → <i>Create Token</i> → 最下面 <i>Create Custom Token</i>，名字随便填，权限加两条：<br>
        <code>Account · Cloudflare Tunnel · Edit</code><br>
        <code>Zone · DNS · Edit</code>（Zone 可选自己的域名），然后 Continue → Create Token，复制 <code>cfut_…</code>（只显示一次）。
      </div></div>
      <div class="guide-step"><span class="idx">3</span><div>
        <b>别拿错 Token</b><br>
        这一步要的是「API Token」；导入连接用的是另一种「隧道 Token」，在 Zero Trust → Networks → Tunnels 里。
      </div></div>
    </div>`;
}

function openTokenGuide() {
  openModal(`
    <h2>去哪拿 Account ID 和 API Token</h2>
    <p class="sub">两步复制、一步创建，权限只要 Tunnel 和 DNS 两条。</p>
    ${tokenGuideHtml()}
    <div class="modal-actions"><button class="primary" data-close>知道了</button></div>`);
}

async function openSettings() {
  const s = state.settings;
  let info = null;
  try { info = await api("/api/info"); } catch (e) { info = null; }
  const infoLine = info
    ? (info.mode === "native"
      ? `<div class="scan-tip">面板直接运行在这台电脑上<code>${esc(info.hostname)}</code>${info.ip ? `（${esc(info.ip)}）` : ""}；<code>127.0.0.1</code> 就是它自己，扫描和映射都指向本机端口。</div>`
      : `<div class="scan-tip">面板运行在 <code>${esc(info.hostname)}</code>${info.ip ? `（${esc(info.ip)}）` : ""}；扫描、映射都以这台机器为准${info.gateway ? `，<code>127.0.0.1</code> 会指向 ${esc(info.gateway)}` : ""}${info.cloudflared ? `。cloudflared：${esc(info.cloudflared)}` : ""}</div>`)
    : "";
  openModal(`
    <h2>设置</h2>
    <p class="sub">面板要两样东西：Account ID 和 API Token。跟着下面三步走，30 秒能拿到。</p>
    ${infoLine}
    ${tokenGuideHtml()}
    <div class="form-row">
      <label class="field-label">Account ID</label>
      <input id="sAccount" value="${esc(s.account_id || "")}" placeholder="32 位十六进制">
    </div>
    <div class="form-row">
      <label class="field-label">Cloudflare API Token ${s.cf_env_locked ? "（已由 .env 注入）" : ""}</label>
      <input id="sToken" value="" placeholder="${s.has_token ? "已保存，留空则不修改" : "粘贴 cfut_… 开头的 API Token"}" ${s.cf_env_locked ? "disabled" : ""}>
      ${s.cf_env_locked ? '<div class="scan-tip">当前 Token 来自 .env 的 <code>CF_API_TOKEN</code>，面板里改不了；要换就改 .env 再 <code>docker compose up -d</code>。</div>' : ""}
    </div>
    <div class="form-row">
      <label class="field-label">面板访问密码 ${s.password_locked ? "（已由 .env 的 PANEL_PASSWORD 固定）" : ""}</label>
      <input id="sPassword" type="password" value="" placeholder="留空不修改；改的话至少 6 位" autocomplete="new-password" ${s.password_locked ? "disabled" : ""}>
    </div>
    <label class="switch"><input type="checkbox" id="sAutoDns" ${s.auto_dns ? "checked" : ""}> 新建映射时自动创建 DNS 记录</label>
    <label class="switch"><input type="checkbox" id="sLoopback" ${s.loopback_alias ? "checked" : ""}> 把 <code>127.0.0.1</code> / <code>localhost</code> 当作宿主机 IP</label>
    <div class="scan-tip">面板跑在容器里，容器自己的 127.0.0.1 不是你的 NAS / 电脑。勾上后，填 127.0.0.1 会被换成容器能访问宿主机的那条地址（<code>host.docker.internal</code>），扫描、测试和映射都一致。用 host 网络部署时无需替换，会自动保持原样。</div>
    <div class="scan-tip" style="margin-top:14px">
      项目主页：<a href="https://github.com/jerryzwj/tunnel-deck" target="_blank" rel="noreferrer">github.com/jerryzwj/tunnel-deck</a>
      ${info?.version ? ` · 版本 ${esc(info.version)}` : ""} · 源码、Docker 镜像与 Windows 版都在里面
    </div>
    <div class="modal-actions">
      <button data-close>取消</button>
      <button class="primary" id="sSave">保存并校验</button>
    </div>`);
  $("#sSave").onclick = async () => {
    const body = {
      auto_dns: $("#sAutoDns").checked,
      loopback_alias: $("#sLoopback").checked,
      cf_account_id: $("#sAccount").value.trim(),
    };
    if (!s.cf_env_locked) body.cf_api_token = $("#sToken").value.trim() || undefined;
    if (!s.password_locked) body.panel_password = $("#sPassword").value.trim() || undefined;
    try {
      const r = await api("/api/settings", { method: "POST", body });
      closeModal();
      await loadState();
      const pwMsg = body.panel_password ? "密码已更新，" : "";
      toast(r.warning || (r.verified ? pwMsg + "Token 校验通过" : "已保存"), r.warning ? "err" : "ok");
    } catch (e) { toast(e.message, "err"); }
  };
}

/* --------------------------------------------------------------- logs */
function toggleLogs() {
  state.logOpen = !state.logOpen;
  const box = $("#logBox");
  box.classList.toggle("hidden", !state.logOpen);
  $("#toggleLogBtn").textContent = state.logOpen ? "收起" : "展开";
  if (state.logOpen) { state.logOffset = 0; loadLogs(true); }
}

async function loadLogs(reset = false) {
  if (!state.logOpen) return;
  const t = tunnelById(state.currentId);
  if (!t) return;
  try {
    const r = await api(`/api/tunnels/${t.id}/logs?offset=${reset ? 0 : state.logOffset}`);
    const box = $("#logBox");
    if (reset) box.innerHTML = "";
    if (r.lines.length) {
      const near = box.scrollTop + box.clientHeight > box.scrollHeight - 60;
      box.innerHTML += r.lines.map(l =>
        `<div class="${/ERR|error|failed|panic/i.test(l) ? "err" : ""}">${esc(l)}</div>`).join("");
      if (near) box.scrollTop = box.scrollHeight;
    }
    state.logOffset = r.total;
    $("#toggleLogBtn").textContent = "收起";
  } catch (e) { /* ignore */ }
}

function copyLogs() {
  const box = $("#logBox");
  copyText(box.innerText || "");
}

function copyText(text) {
  navigator.clipboard?.writeText(text).then(() => toast("已复制", "ok"))
    .catch(() => toast("复制失败，请手动选择", "err"));
}

/* --------------------------------------------------------------- polling */
setInterval(async () => {
  if ($("#mainView").classList.contains("hidden")) return;
  if ($("#modal").classList.contains("hidden") === false) return;
  try {
    const data = await api("/api/state");
    state.tunnels = data.tunnels || [];
    state.mappings = data.mappings || {};
    $$(".tunnel-item").forEach(el => {
      const t = state.tunnels.find(x => x.id === el.dataset.tid);
      if (!t) return;
      const dot = el.querySelector(".dot");
      dot.className = "dot " + t.status.state;
      const badge = el.querySelector(".badge");
      badge.className = "badge " + t.status.state;
      badge.textContent = stateLabel(t.status.state);
    });
    const cur = tunnelById(state.currentId);
    if (cur) {
      const badge = $("#statusBadge");
      if (badge) {
        badge.className = "badge " + cur.status.state;
        badge.textContent = stateLabel(cur.status.state);
      }
      const runBtn = $("#toggleRunBtn");
      if (runBtn) runBtn.textContent = cur.status.running ? "停止" : "启动";
    }
  } catch (e) { /* ignore */ }
}, 5000);

setInterval(() => loadLogs(false), 2500);

/* --------------------------------------------------------------- wiring */
$("#setupForm").onsubmit = async (e) => {
  e.preventDefault();
  const pw = $("#setupPw").value.trim();
  const pw2 = $("#setupPw2").value.trim();
  if (pw !== pw2) { $("#setupHint").textContent = "两次输入的密码不一致"; return; }
  if (pw.length < 6) { $("#setupHint").textContent = "密码至少 6 位"; return; }
  try {
    const r = await api("/api/setup", {
      method: "POST",
      body: {
        password: pw,
        account_id: $("#setupAccount").value.trim() || null,
        api_token: $("#setupToken").value.trim() || null,
      },
    });
    await loadState();
    if (r.warning) toast(r.warning, "err");
    else if (r.verified) toast("初始化完成，Cloudflare 校验通过", "ok");
    else toast("初始化完成，可以开始添加映射了", "ok");
  } catch (err) {
    $("#setupHint").textContent = err.message;
  }
};
$("#setupHelp").onclick = (e) => { e.preventDefault(); openTokenGuide(); };

$("#loginForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/login", { method: "POST", body: { password: $("#loginPassword").value } });
    await loadState();
  } catch (err) {
    $("#loginHint").textContent = err.message;
  }
};
$("#btnSettings").onclick = openSettings;
$("#bannerLink").onclick = (e) => { e.preventDefault(); openSettings(); };
$("#btnLogout").onclick = async () => {
  await api("/api/logout", { method: "POST" });
  location.reload();
};
$("#btnNewTunnel").onclick = openNewTunnel;
$("#btnImportToken").onclick = openImportToken;
$("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

loadState();
