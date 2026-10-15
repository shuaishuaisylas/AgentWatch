"use strict";

const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt = value => value == null ? "—" : new Intl.NumberFormat("zh-CN").format(value);
const compact = value => value == null ? "—" : value >= 1e6 ? `${(value / 1e6).toFixed(2)}M` : value >= 1e3 ? `${(value / 1e3).toFixed(1)}K` : fmt(value);
const time = value => value ? new Date(value * 1000).toLocaleTimeString("zh-CN", {timeZone:"Asia/Shanghai",hour12:false}) : "—";
const dateTime = value => value ? new Date(value * 1000).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai",hour12:false}) : "未收到";
const today = () => new Date().toLocaleDateString("sv-SE", {timeZone:"Asia/Shanghai"});
const tools = {
  codex:{name:"Codex",symbol:"C",entry:"桌面 · VS Code"},
  claude:{name:"Claude Code",symbol:"✳",entry:"终端 · 编辑器 · 桌面 Code"},
  workbuddy:{name:"WorkBuddy",symbol:"W",entry:"桌面 · 本地只读"}
};
const toolName = tool => tools[tool]?.name || tool || "未知工具";
const toolIcon = (tool,large=false) => `<span class="tool-icon ${esc(tool)}${large ? " large" : ""}">${esc(tools[tool]?.symbol || "A")}</span>`;
const surfaces = {desktop:"桌面应用",vscode:"VS Code",terminal:"本机终端",unknown:"入口待识别"};
const surfaceName = card => card.tool === "claude" && card.surface === "desktop" ? "桌面 Code" : surfaces[card.surface] || "入口待识别";
const labels = {active:"执行中",waiting_user:"等待回复",waiting_approval:"等待批准",idle:"本轮结束",ended:"运行结束",unknown:"待确认",error:"出现错误"};
const roles = {main:"主会话",child:"子 Agent",unknown:"关系未知"};
const state = {view:"live",period:"day",status:"all",snapshot:null,busy:false,request:0};
let toastTimer;

async function api(path, body) {
  const options = {credentials:"same-origin",cache:"no-store"};
  if (body !== undefined) Object.assign(options, {method:"POST",headers:{"Content-Type":"application/json","X-AgentWatch-CSRF":$('meta[name="csrf"]').content},body:JSON.stringify(body)});
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}
function toast(message) {
  clearTimeout(toastTimer);
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => $("#toast").hidden = true, 5000);
}
function query(extra={}) {
  return new URLSearchParams({tool:$("#tool-filter").value,project:$("#project-filter").value,role:$("#role-filter").value,...extra});
}
function metric(title,value,caption,symbol="↗",tone="") {
  return `<div class="metric ${tone}"><div class="metric-header">${esc(title)}<span class="metric-symbol">${symbol}</span></div><div class="metric-value">${value}</div><div class="metric-caption">${esc(caption)}</div></div>`;
}
function showView(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach(el => el.hidden = el.id !== `view-${view}`);
  document.querySelectorAll("[data-view]").forEach(el => el.classList.toggle("selected",el.dataset.view === view));
  $("#filterbar").hidden = view === "settings";
  const headings = {
    live:["会话总览","YOUR AGENTS, AT A GLANCE","每个会话，都在视线里。","先看谁需要你，再看状态与用量。"],
    history:["历史统计","OBSERVE YOUR WORK","把用量，放进时间里。","查看日、周、月、年用量与会话变化。"],
    settings:["接入设置","CONNECTED, ON YOUR TERMS","接入一次，照常使用。","查看采集范围，按需增强 Claude Code 状态。"]
  }[view];
  ["breadcrumb","eyebrow","page-title","page-description"].forEach((id,i) => $(`#${id}`).textContent = headings[i]);
  if (view === "history") loadHistory().catch(error => toast(error.message));
  if (view === "settings") return loadSettings().catch(error => toast(error.message));
}

function renderSnapshot(data) {
  state.snapshot = data;
  $("#demo-banner").hidden = !data.demo;
  const c = data.counts;
  const attention = c.waiting + (c.error || 0);
  $("#connection-text").textContent = data.demo ? "演示数据" : "本机已连接";
  $("#connection-dot").classList.remove("offline");
  $("#nav-count").textContent = c.observed;
  $("#island-online").textContent = `${c.online} 个在线`;
  $("#island-waiting").textContent = attention ? `${attention} 个需要你` : "暂无确认的等待";
  $(".island").classList.toggle("attention", attention > 0);
  $("#snapshot-time").textContent = data.demo ? "人工样例 · 刷新保留演示状态" : `${time(data.scan.last_scan)} 更新 · 每 3 秒采集`;
  $("#live-metrics").innerHTML = metric("今日可见会话",fmt(c.observed),"今日活动或当前在线的会话","⌁") + metric("当前在线",fmt(c.online),"已匹配到本机运行进程","●","green") + metric("确认执行中",fmt(c.active),`${c.unknown} 个会话的执行状态待确认`,"↗") + metric("需要你处理",fmt(attention),`${c.waiting} 个等待 · ${c.error || 0} 个错误 · 旧事件需确认`,"◷",attention ? "amber" : "");
  $("#waiting-tab-count").textContent = attention;
  const notices = [];
  if (data.scan.pending) notices.push(`正在回读历史记录，${data.scan.pending} 个文件尚未读完；统计会继续更新。`);
  if (c.unknown) notices.push("仅凭进程在线无法确认正在执行；缺少准确状态的会话标记为“待确认”。");
  if (c.unknown_roles && $("#role-filter").value === "main") notices.push(`${c.unknown_roles} 个今日会话的主子关系未知，可切换“关系未知”查看。`);
  if (data.scan.errors) notices.push("部分来源读取未完成，可刷新重试；已采集的数据仍可查看。");
  if (data.truncated) notices.push("当前展示最优先的 160 个会话，可缩小工具或项目范围。");
  $("#coverage-banner").hidden = !notices.length;
  $("#coverage-banner").textContent = notices.join(" ");
  const selected = $("#project-filter").value;
  const signature = JSON.stringify(data.projects.map(p => [p.id,p.name]));
  if ($("#project-filter").dataset.signature !== signature) {
    $("#project-filter").innerHTML = '<option value="all">全部项目</option>' + data.projects.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
    $("#project-filter").dataset.signature = signature;
    $("#project-filter").value = data.projects.some(p => p.id === selected) ? selected : "all";
  }
  renderCards();
  $("#activity-date").textContent = data.date;
  $("#activity").innerHTML = data.activity.length ? data.activity.map(event => `<div class="activity-row"><span class="activity-dot ${esc(event.kind)}"></span><time>${time(event.at)}</time><span class="activity-label">${esc(event.label)}</span><span class="activity-project">${esc(event.project_name)} · ${esc(toolName(event.tool))}</span></div>`).join("") : '<div class="empty-small">今天还没有可观测的状态变化</div>';
}
function contextMarkup(card) {
  const pct = card.context_percent;
  const label = card.context_used == null ? "等待有效快照" : pct == null ? `${compact(card.context_used)} token · 分母未知` : `${Math.min(pct,999).toFixed(1)}% · ${compact(card.context_used)} / ${compact(card.context_limit)}`;
  const note = card.context_used == null ? "来源未报告有效快照；缺失时不估算百分比" : `${card.context_estimated ? "最近请求的输入量" : "来源报告的最近快照"} · ${time(card.context_at)}${card.context_stale ? " · 快照较旧" : ""}`;
  return `<div class="context-row"><span>上下文</span><strong>${esc(label)}</strong></div><div class="context-track ${pct == null ? "unavailable" : ""}"><div class="context-fill" data-width="${pct == null ? 0 : Math.max(0,Math.min(100,pct))}"></div></div><div class="context-note">${esc(note)}</div>`;
}
function renderCards() {
  if (!state.snapshot) return;
  const cards = state.snapshot.sessions.filter(card => state.status === "all" || (state.status === "waiting" ? ["waiting_user","waiting_approval","error"].includes(card.state) : card.state === state.status));
  $("#session-count").textContent = cards.length;
  $("#sessions").innerHTML = cards.length ? cards.map(card => `<article class="session-card ${["waiting_user","waiting_approval","error"].includes(card.state) ? "needs-attention" : ""}">
    <div class="card-header"><div class="card-tool">${toolIcon(card.tool)}<span>${esc(toolName(card.tool))}<small>${esc(surfaceName(card))}</small></span></div><span class="status-chip ${esc(card.state)}">${esc(labels[card.state])}</span></div>
    <h3 class="card-title" title="${esc(card.alias || card.project_name)}">${esc(card.alias || card.project_name)}</h3><div class="card-meta"><span>${esc(card.source_id.slice(0,8))}</span><span>${esc(roles[card.role])}</span>${card.branch ? `<span title="${esc(card.branch)}">⑂ ${esc(card.branch)}</span>` : ""}</div><div class="card-separator"></div>
    <div class="card-stage"><span class="stage-dot ${esc(card.state)}"></span>${esc(card.stage)}${card.stale ? '<span class="stale-label">旧事件</span>' : ""}</div>
    ${contextMarkup(card)}<div class="card-usage"><div><span>今日 token</span><strong>${compact(card.usage.today)}</strong></div><div><span>${card.tool === "codex" ? "会话报告累计" : "已采集累计"}</span><strong>${compact(card.usage.cumulative)}</strong></div></div>
    <div class="card-footer"><span class="liveness ${card.liveness === "online" ? "online" : ""}"><i></i>${({online:"进程在线",offline:"进程已退出",unknown:"进程待匹配"})[card.liveness]}</span><div class="card-actions"><button class="icon-button" data-detail="${esc(card.id)}" aria-label="查看 ${esc(card.project_name)} 会话详情">···</button><button class="jump-button" data-jump="${esc(card.id)}" ${card.jump.available ? "" : "disabled"} title="${esc(card.jump.precision || "尚未识别可跳转入口")}">${esc(card.jump.label)} <span>↗</span></button></div></div>
  </article>`).join("") : `<div class="empty-state"><div class="empty-symbol">⌁</div><h3>${state.status === "all" ? "这个范围内还没有可见会话" : "当前没有符合此状态的会话"}</h3><p>${$("#role-filter").value === "main" ? "未报告主子关系的会话可在“关系未知”或“全部会话”中查看。" : "照常使用已接入的工具，或切换工具、项目和会话范围。"}</p></div>`;
  document.querySelectorAll("[data-width]").forEach(el => el.style.width = `${el.dataset.width}%`);
}
function renderSubscriptions(data) {
  const statusNames = {available:"已获取",stale:"旧快照",unavailable:"未获取",auth_required:"需授权",error:"更新失败"};
  $("#subscription-cards").innerHTML = data.platforms.map(platform => {
    const windows = platform.limits.map(limit => {
      const expired = limit.status === "expired";
      const stale = limit.status === "stale";
      const value = limit.remaining == null ? "—" : limit.unit === "percent" ? `${fmt(limit.remaining)}%` : `${fmt(limit.remaining)} 积分`;
      const reset = limit.resets_at ? `重置 ${dateTime(limit.resets_at)}` : "重置时间未报告";
      const detail = limit.unit === "credits" ? `已用 ${fmt(limit.used)} / 合计 ${fmt(limit.limit)}` : `${expired || stale ? "上次已用" : "已用"} ${fmt(limit.used)}%`;
      const caption = expired ? "已到重置时间，等待新快照" : stale ? "上次报告的剩余量" : "剩余";
      return `<div class="quota-window"><div class="quota-window-title"><span>${esc(limit.label)}</span>${limit.bucket !== platform.tool ? `<small>${esc(limit.bucket)}</small>` : ""}</div><div class="quota-value ${expired || stale ? "old" : (limit.remaining_percent != null && limit.remaining_percent <= 10 ? "low" : "")}"><strong>${esc(value)}</strong><span>${esc(caption)}</span></div><div class="quota-track"><i data-quota-width="${expired || limit.remaining_percent == null ? 0 : Math.max(0,Math.min(100,limit.remaining_percent))}"></i></div><div class="quota-detail">${esc(detail)} · ${esc(reset)}</div></div>`;
    }).join("");
    const action = platform.tool === "claude" && !platform.limits.length ? '<button class="quota-setup" data-quota-setup="claude">设置状态栏上报 ↗</button>' : "";
    return `<article class="quota-card"><div class="quota-heading">${toolIcon(platform.tool)}<h3>${esc(platform.name)}</h3><span class="quota-status ${esc(platform.status)}">${esc(data.demo ? "人工样例" : statusNames[platform.status] || "未获取")}</span></div>${windows || '<div class="quota-empty"><strong>—</strong><span>尚未取得订阅剩余量</span></div>'}<p class="quota-notice">${esc(platform.notice)}</p>${action}<div class="quota-updated">${platform.updated_at ? `来源更新 ${dateTime(platform.updated_at)}` : "等待来源数据"}${platform.refreshing ? " · 正在更新" : ""}</div></article>`;
  }).join("");
  document.querySelectorAll("[data-quota-width]").forEach(el => el.style.width = `${el.dataset.quotaWidth}%`);
}

async function refresh() {
  if (state.busy) return;
  state.busy = true;
  try {
    const [snapshot, subscriptions] = await Promise.allSettled([api(`/api/snapshot?${query()}`),api("/api/subscriptions")]);
    if (snapshot.status === "rejected") throw snapshot.reason;
    renderSnapshot(snapshot.value);
    if (subscriptions.status === "fulfilled") renderSubscriptions(subscriptions.value);
    else $("#subscription-cards").innerHTML = '<div class="empty-small">额度读取未完成，请更新后重试。会话数据仍可查看。</div>';
  } catch (error) {
    $("#connection-text").textContent = "采集服务未连接";
    $("#connection-dot").classList.add("offline");
    $("#snapshot-time").textContent = "数据未更新 · 请检查启动看板的终端";
    if (!state.snapshot) $("#sessions").innerHTML = `<div class="empty-state"><h3>暂时无法连接本机服务</h3><p>${esc(error.message)}。可重新双击“启动看板.command”。</p></div>`;
  } finally { state.busy = false; }
}

async function loadHistory() {
  const request = ++state.request;
  const result = await api(`/api/stats?${query({period:state.period,date:$("#stats-date").value})}`);
  if (request !== state.request) return;
  const total = result.totals;
  const end = new Date(`${result.end}T12:00:00+08:00`); end.setDate(end.getDate()-1);
  $("#period-range").textContent = `${result.start} — ${end.toLocaleDateString("sv-SE")}`;
  $("#history-metrics").innerHTML = metric("已采集 token",compact(total.total),result.has_usage ? "输入 + 输出，缓存不重复累加" : "这个周期未取得用量记录","↗","green") + metric("活跃会话",fmt(result.sessions),"在整个周期内按会话 ID 去重","⌁") + metric("记录轮次",fmt(result.turns),"Codex 已报告的活动轮次","↻") + metric("原生任务",fmt(result.tasks),result.task_scope,"✓");
  $("#chart-total").textContent = `${compact(total.total)} token`;
  const max = Math.max(1,...result.buckets.map(b => b.tokens));
  $("#chart").innerHTML = result.buckets.map(bucket => `<div class="chart-column" title="${esc(bucket.label)} · ${fmt(bucket.tokens)} 已采集 token"><div class="chart-bar ${bucket.tokens ? "" : "zero"}" data-height="${bucket.tokens / max * 100}"></div></div>`).join("");
  document.querySelectorAll("[data-height]").forEach(el => el.style.height = `${el.dataset.height}%`);
  const stride = Math.max(1,Math.ceil(result.buckets.length / 8));
  $("#chart-labels").innerHTML = result.buckets.map((b,i) => `<span>${i % stride === 0 || i === result.buckets.length-1 ? esc(b.label) : ""}</span>`).join("");
  $("#history-coverage").textContent = `${result.partial_sessions ? ` · ${result.partial_sessions} 个来源存在覆盖缺口` : ""}${result.first_usage_day ? ` · 最早可观测 ${result.first_usage_day}` : " · 尚无用量数据"}`;
  const breakdown = [["input","输入 token"],["output","输出 token"],["cache_read","缓存读取（输入子集）"],["cache_write","缓存写入（输入子集）"],["reasoning","推理（输出子集）"]];
  $("#breakdown").innerHTML = breakdown.map(([key,label]) => `<div class="breakdown-row"><span>${label}</span><strong>${fmt(total[key])}</strong></div>`).join("");
  $("#by-tool").innerHTML = Object.entries(result.by_tool).length ? Object.entries(result.by_tool).map(([tool,count]) => `<div class="breakdown-row"><span>${toolIcon(tool)} ${esc(toolName(tool))}</span><strong>${compact(count)}</strong></div>`).join("") : '<div class="empty-small">此周期暂无可观测用量</div>';
}

async function loadSettings() {
  const data = await api("/api/integrations");
  $("#demo-banner").hidden = !data.demo;
  $("#enable-claude").disabled = Boolean(data.demo);
  $("#restore-claude").disabled = Boolean(data.demo);
  $("#include-statusline").disabled = Boolean(data.demo);
  $("#source-panels").innerHTML = data.sources.map(source => `<div class="source-panel"><div class="source-heading">${toolIcon(source.tool,true)}<h3>${esc(source.name)}</h3><span class="status-chip ${source.present ? "idle" : "unknown"}">${source.present ? "发现本地目录" : "尚未发现"}</span></div><p>${esc(source.label)}</p><div class="source-precision">${esc(source.precision)}</div>${source.tool === "workbuddy" ? '<div class="source-precision">本地读取会话与 token；积分余额通过官方授权接口单独查询，不修改 WorkBuddy 设置。</div>' : ""}</div>`).join("");
  const config = data.claude;
  $("#enhance-status").textContent = config.error || (config.complete ? data.last_hook_at ? "已配置 · 已收到上报" : "已配置 · 等待实际状态上报" : config.enabled ? "部分事件已配置" : "可选增强 · 尚未启用");
  $("#enhance-status").className = `status-chip ${config.complete ? "idle" : "unknown"}`;
  $("#enable-claude").textContent = config.enabled ? "预览接入设置" : "预览并启用";
  $("#restore-claude").hidden = !config.enabled && !config.statusline_enabled;
  $("#include-statusline").checked = config.statusline_enabled;
  $("#hook-last-seen").textContent = `最近状态上报：${dateTime(data.last_hook_at)}`;
  $("#setup-dot").hidden = config.complete;
  $("#data-location").textContent = `看板数据：${data.data_label}`;
}
function modal(title,html,actions=[]) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  $("#modal-actions").replaceChildren();
  actions.forEach(({label,primary,run}) => {
    const button = document.createElement("button");
    button.className = `button ${primary ? "primary" : "secondary"}`;
    button.textContent = label;
    button.onclick = async () => {
      button.disabled = true;
      try { await run(); } catch (error) { toast(error.message); } finally { button.disabled = false; }
    };
    $("#modal-actions").append(button);
  });
  if (!$("#modal").open) $("#modal").showModal();
}
function closeModal() { $("#modal").close(); }
async function jumpTo(id,kind) {
  try { const result = await api("/api/jump",{id,...(kind ? {kind} : {})}); toast(result.message || "已发起跳转，请查看对应应用"); }
  catch (error) { toast(error.message); }
}
function details(id) {
  const card = state.snapshot?.sessions.find(c => c.id === id);
  if (!card) return;
  const entries = [["会话 ID",card.source_id],["所属项目",card.project_name],["工作目录",card.worktree_name || "未识别"],["角色",roles[card.role]],["父会话",card.parent_id || "—"],["客户端版本",card.version || "未知"],["最近状态",`${labels[card.state]} · ${dateTime(card.state_at)}`],["状态依据",card.state_source === "hook" ? "Claude 状态增强" : card.state_source === "process" ? "进程观测" : "本地事件记录"],["跳转精度",card.jump.precision || "未知"],["采集覆盖",({observed:"已观测记录",partial_baseline:"缺少早期用量基线",counter_reset:"来源累计计数曾重置",partial_usage:"部分用量字段缺失",partial_records:"部分记录无法读取",missing_time:"部分记录缺少时间",missing_request_id:"部分用量缺少请求身份"})[card.coverage] || card.coverage],["今日 token",fmt(card.usage.today)],["已采集累计 token",fmt(card.usage.observed_total)]];
  modal("会话详情",`<div class="detail-grid">${entries.map(([label,value]) => `<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("")}</div><p class="detail-note">${card.stale ? "当前状态来自较早的事件，请跳回原工具确认。" : "在线表示匹配到进程，未确认执行的会话保持待确认。"} 用量只覆盖可读取的来源；看板不会猜测缺失数据。</p><label class="alias-label" for="alias-input">给会话起一个名字</label><input class="text-input" id="alias-input" maxlength="80" placeholder="例如：AgentWatch 接入调试" value="${esc(card.alias)}">`,[
    {label:"保存名称",primary:true,run:async () => { await api("/api/alias",{id,alias:$("#alias-input").value}); closeModal(); await refresh(); toast("名称已保存"); }},
    ...(!state.snapshot.demo && card.tool === "codex" && card.surface === "vscode" ? [{label:"用 Codex 桌面打开",run:() => jumpTo(id,"codex")}] : []),
    {label:"关闭",run:closeModal}
  ]);
}
async function previewClaude() {
  try {
    const preview = await api("/api/claude/preview",{statusline:$("#include-statusline").checked});
    const sample = {hooks:{PermissionRequest:[{hooks:[{type:"command",command:preview.command,async:true,timeout:3}]}]}};
    modal("预览 Claude Code 接入",`<p>${esc(preview.effect)}</p><div class="detail-note">${esc(preview.privacy)}</div><div class="event-tags">${preview.events.map(e => `<span>${esc(e)}</span>`).join("")}</div><p>写入 ${esc(preview.settings_label)}，覆盖上面的 ${preview.events.length} 类事件。${preview.include_statusline ? "同时上报终端上下文占用，保留原状态栏输出。" : "此次只启用状态事件。"}</p><pre>${esc(JSON.stringify(sample,null,2))}</pre><p class="muted">上面展示一个事件的新增配置示例。保存前会检查原文件是否变化，并在本机保存原配置备份。</p>`,[
      {label:"确认启用",primary:true,run:async () => { await api("/api/claude/enable",{hash:preview.hash,statusline:preview.include_statusline}); closeModal(); await loadSettings(); toast("设置已保存。请新启动 Claude Code，再检查实际状态上报。"); }},
      {label:"取消",run:closeModal}
    ]);
  } catch (error) { toast(error.message); }
}
function restoreClaude() {
  modal("撤销 AgentWatch 接入",'<p>删除看板添加的状态 hooks；如果状态栏仍由看板管理，恢复启用前的状态栏。保留你后来添加的其他配置与本机备份。</p>',[
    {label:"确认撤销",primary:true,run:async () => { await api("/api/claude/restore",{}); closeModal(); await loadSettings(); toast("AgentWatch 配置已撤销"); }},
    {label:"取消",run:closeModal}
  ]);
}

$("#tool-filter").innerHTML = '<option value="all">全部工具</option>' + Object.entries(tools).map(([tool,meta]) => `<option value="${esc(tool)}">${esc(meta.name)}</option>`).join("");
$("#tool-sources").innerHTML = Object.entries(tools).map(([tool,meta]) => `<div class="tool-link">${toolIcon(tool)}<span>${esc(meta.name)}<small>${esc(meta.entry)}</small></span><span class="small-dot"></span></div>`).join("");

document.querySelectorAll("[data-view]").forEach(button => button.onclick = () => showView(button.dataset.view));
document.querySelectorAll("[data-state]").forEach(button => button.onclick = () => {
  state.status = button.dataset.state;
  document.querySelectorAll("[data-state]").forEach(el => el.classList.toggle("selected",el === button));
  renderCards();
});
document.querySelectorAll("[data-period]").forEach(button => button.onclick = () => {
  state.period = button.dataset.period;
  document.querySelectorAll("[data-period]").forEach(el => el.classList.toggle("selected",el === button));
  loadHistory().catch(error => toast(error.message));
});
document.querySelectorAll(".filterbar select").forEach(select => select.onchange = async () => {
  await refresh();
  if (state.view === "history") loadHistory().catch(error => toast(error.message));
});
$("#refresh").onclick = async () => {
  try { await api("/api/rescan",{}); await refresh(); if (state.view === "history") await loadHistory(); if (state.view === "settings") await loadSettings(); toast("已请求重新采集"); }
  catch (error) { toast(error.message); }
};
$("#stats-date").value = today();
$("#refresh-subscriptions").onclick = async () => {
  try { await api("/api/rescan",{}); await refresh(); toast("已请求更新额度；新数据到达后会自动显示。"); }
  catch (error) { toast(error.message); }
};
$("#subscription-cards").onclick = async event => {
  if (event.target.closest("[data-quota-setup]")) {
    await showView("settings");
    $("#include-statusline").checked = true;
  }
};
$("#stats-date").onchange = () => loadHistory().catch(error => toast(error.message));
function shiftDate(direction) {
  const d = new Date(`${$("#stats-date").value || today()}T12:00:00`);
  if (state.period === "month") { d.setDate(1); d.setMonth(d.getMonth()+direction); }
  else if (state.period === "year") d.setFullYear(d.getFullYear()+direction);
  else d.setDate(d.getDate()+direction*(state.period === "week" ? 7 : 1));
  $("#stats-date").value = d.toLocaleDateString("sv-SE");
  loadHistory().catch(error => toast(error.message));
}
$("#date-prev").onclick = () => shiftDate(-1);
$("#date-next").onclick = () => shiftDate(1);
$("#sessions").onclick = event => {
  const detail = event.target.closest("[data-detail]");
  const jump = event.target.closest("[data-jump]");
  if (detail) details(detail.dataset.detail);
  if (jump && !jump.disabled) jumpTo(jump.dataset.jump);
};
$("#modal-close").onclick = closeModal;
$("#modal").onclick = event => { if (event.target === $("#modal")) { const r = $("#modal").getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) closeModal(); } };
$("#enable-claude").onclick = previewClaude;
$("#restore-claude").onclick = restoreClaude;
refresh();
setInterval(() => { refresh(); if (state.view === "history") loadHistory().catch(() => {}); },4000);
document.addEventListener("visibilitychange",() => { if (!document.hidden) refresh(); });
