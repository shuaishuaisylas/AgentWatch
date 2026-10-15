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
const labels = {active:"执行中",waiting_user:"等待回复",waiting_approval:"等待批准",idle:"空闲",ended:"运行结束",unknown:"待确认",error:"出现错误"};
const roles = {main:"主会话",child:"子 Agent",unknown:"关系未知"};
const state = {view:"live",status:"all",snapshot:null,snapshotRequest:0,request:0,history:null,historyTool:null,heatmapFocus:null,detail:null};
let toastTimer;

async function api(path, body, extra={}) {
  const options = {credentials:"same-origin",cache:"no-store",...extra};
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
  return new URLSearchParams({tool:$("#tool-filter").value,role:"main",...extra});
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
    history:["历史统计","A YEAR IN TOKENS","把用量，放进时间里。","用一年的日历，看见每天的 Agent 用量。"],
    settings:["接入设置","CONNECTED, ON YOUR TERMS","接入一次，照常使用。","查看采集范围，按需启用各平台状态上报。"]
  }[view];
  ["breadcrumb","eyebrow","page-title","page-description"].forEach((id,i) => $(`#${id}`).textContent = headings[i]);
  $("#filter-scope").textContent = view === "history" ? "年度合计 · 含所有会话角色" : "主会话 · 点击查看子 Agent";
  hideHeatmapTooltip();
  if (view === "history") loadHistory().catch(() => {});
  if (view === "settings") return loadSettings().catch(error => toast(error.message));
}

function renderSnapshot(data) {
  state.snapshot = data;
  $("#demo-banner").hidden = !data.demo;
  const c = data.counts;
  const children = data.children_counts || (data.sessions || []).reduce((sum,card) => {
    for (const key of ["total","active","waiting","error","unknown"]) sum[key] += card.children_summary?.[key] || 0;
    return sum;
  },{total:0,active:0,waiting:0,error:0,unknown:0});
  const mainAttention = c.waiting + (c.error || 0);
  const childAttention = children.waiting + children.error;
  const attention = mainAttention + childAttention;
  const active = c.active + children.active;
  $("#connection-text").textContent = data.demo ? "演示数据" : "本机已连接";
  $("#connection-dot").classList.remove("offline");
  $("#nav-count").textContent = c.observed;
  $("#island-online").textContent = `主会话 ${c.online} 个在线`;
  $("#island-waiting").textContent = attention ? `${attention} 个需要你 · 主 ${mainAttention} / 子 ${childAttention}` : `执行中 ${active} · 主 ${c.active} / 子 ${children.active}`;
  $(".island").classList.toggle("attention", attention > 0);
  $("#snapshot-time").textContent = data.demo ? "人工样例 · 刷新保留演示状态" : `${time(data.scan.last_scan)} 更新 · 每 3 秒采集`;
  $("#live-metrics").innerHTML = metric("今日主会话",fmt(c.observed),"今日活动或当前在线的主会话","⌁") + metric("主会话在线",fmt(c.online),"已匹配到本机运行进程","●","green") + metric("确认执行中",fmt(active),`主 ${c.active} · 子 ${children.active}，${c.unknown + children.unknown} 个待确认`,"↗") + metric("需要你处理",fmt(attention),`主 ${mainAttention} · 子 ${childAttention}，含等待与错误`,"◷",attention ? "amber" : "");
  const waitingCards = (data.sessions || []).filter(card => card.role === "main" && (["waiting_user","waiting_approval","error"].includes(card.state) || (card.children_summary?.waiting || 0) + (card.children_summary?.error || 0) > 0)).length;
  $("#waiting-tab-count").textContent = waitingCards;
  $("#waiting-tab-count").parentElement.title = "包含自身或子 Agent 需要处理的主会话";
  const notices = [];
  if (data.scan.pending) notices.push(`正在回读历史记录，${data.scan.pending} 个文件尚未读完；统计会继续更新。`);
  if (c.unknown + children.unknown) notices.push("仅凭进程在线无法确认正在执行；缺少准确状态的会话标记为“待确认”。");
  if (c.unknown_roles) notices.push(`全局还有 ${c.unknown_roles} 个今日会话的关系未知，未计入主会话。`);
  if (data.scan.errors) notices.push("部分来源读取未完成，可刷新重试；已采集的数据仍可查看。");
  if (data.truncated) notices.push("当前展示最优先的 160 个主会话，可按工具缩小范围。");
  $("#coverage-banner").hidden = !notices.length;
  $("#coverage-banner").textContent = notices.join(" ");
  updateToolOptions([...(data.sessions || []).map(card => card.tool),...(data.activity || []).map(event => event.tool)]);
  renderCards();
  refreshDetails();
  $("#activity-date").textContent = data.date;
  $("#activity").innerHTML = data.activity.length ? data.activity.map(event => `<div class="activity-row"><span class="activity-dot ${esc(event.kind)}"></span><time>${time(event.at)}</time><span class="activity-label">${esc(event.label)}</span><span class="activity-project">${esc(event.project_name)} · ${esc(toolName(event.tool))}</span></div>`).join("") : '<div class="empty-small">今天还没有可观测的状态变化</div>';
}
function contextMarkup(card) {
  const pct = card.context_percent;
  const label = card.context_used == null ? "等待有效快照" : pct == null ? `${compact(card.context_used)} token · 分母未知` : `${Math.min(pct,999).toFixed(1)}% · ${compact(card.context_used)} / ${compact(card.context_limit)}`;
  const note = card.context_used == null ? "来源未报告有效快照；缺失时不估算百分比" : `${card.context_estimated ? "最近请求的输入量" : "来源报告的最近快照"} · ${time(card.context_at)}${card.context_stale ? " · 快照较旧" : ""}`;
  return `<div class="context-row"><span>上下文</span><strong>${esc(label)}</strong></div><div class="context-track ${pct == null ? "unavailable" : ""}"><div class="context-fill" data-width="${pct == null ? 0 : Math.max(0,Math.min(100,pct))}"></div></div><div class="context-note">${esc(note)}</div>`;
}
function childrenSummary(card) {
  const summary = card.children_summary;
  if (!summary) return `<button type="button" class="card-children" data-detail="${esc(card.id)}"><span>查看子 Agent</span><span>↗</span></button>`;
  const attention = (summary.waiting || 0) + (summary.error || 0);
  return `<button type="button" class="card-children" data-detail="${esc(card.id)}" aria-label="查看 ${esc(card.alias || card.project_name)} 的 ${fmt(summary.total)} 个子 Agent"><span>子 Agent <strong>${fmt(summary.total)}</strong></span><span>${attention ? `<em>${fmt(attention)} 个需要处理</em>` : summary.active ? `${fmt(summary.active)} 个执行中` : "查看列表与状态"} ↗</span></button>`;
}
function renderCards() {
  if (!state.snapshot) return;
  const focused = document.activeElement?.closest("[data-detail],[data-jump]");
  const focusKind = focused?.hasAttribute("data-jump") ? "jump" : "detail";
  const focusId = focused?.dataset[focusKind];
  const focusClass = ["card-open","card-children","icon-button","jump-button"].find(name => focused?.classList.contains(name));
  const cards = state.snapshot.sessions.filter(card => {
    if (card.role !== "main") return false;
    const child = card.children_summary || {};
    if (state.status === "all") return true;
    if (state.status === "waiting") return ["waiting_user","waiting_approval","error"].includes(card.state) || (child.waiting || 0) + (child.error || 0) > 0;
    return card.state === state.status || (child[state.status] || 0) > 0;
  });
  $("#session-count").textContent = cards.length;
  $("#sessions").innerHTML = cards.length ? cards.map(card => `<article class="session-card ${["waiting_user","waiting_approval","error"].includes(card.state) || (card.children_summary?.waiting || 0) + (card.children_summary?.error || 0) > 0 ? "needs-attention" : ""}">
    <div class="card-header"><div class="card-tool">${toolIcon(card.tool)}<span>${esc(toolName(card.tool))}<small>${esc(surfaceName(card))}</small></span></div><span class="status-chip ${esc(card.state)}">${esc(labels[card.state] || card.state || "待确认")}</span></div>
    <h3 class="card-title"><button class="card-open" data-detail="${esc(card.id)}" aria-label="查看 ${esc(card.alias || card.project_name)} 主会话详情与子 Agent">${esc(card.alias || card.project_name)}</button></h3><div class="card-meta"><span>${esc((card.source_id || "").slice(0,8))}</span><span>主会话</span>${card.branch ? `<span title="${esc(card.branch)}">⑂ ${esc(card.branch)}</span>` : ""}</div><div class="card-separator"></div>
    <div class="card-stage"><span class="stage-dot ${esc(card.state)}"></span>${esc(card.stage)}${card.stale ? '<span class="stale-label">旧事件</span>' : ""}</div>
    ${contextMarkup(card)}<div class="card-usage"><div><span>今日 token</span><strong>${compact(card.usage.today)}</strong></div><div><span>${card.tool === "codex" ? "会话报告累计" : "已采集累计"}</span><strong>${compact(card.usage.cumulative)}</strong></div></div>
    ${childrenSummary(card)}
    <div class="card-footer"><span class="liveness ${card.liveness === "online" ? "online" : ""}"><i></i>${({online:"进程在线",offline:"进程已退出",unknown:"进程待匹配"})[card.liveness] || "进程待匹配"}</span><div class="card-actions"><button class="icon-button" data-detail="${esc(card.id)}" aria-label="查看 ${esc(card.project_name)} 会话详情">···</button><button class="jump-button" data-jump="${esc(card.id)}" ${card.jump.available ? "" : "disabled"} title="${esc((!card.jump.available && card.jump.reason) || card.jump.precision || "尚未识别可跳转入口")}">${esc(card.jump.label)} <span>↗</span></button></div></div>
  </article>`).join("") : `<div class="empty-state"><div class="empty-symbol">⌁</div><h3>${state.status === "all" ? "这个范围内还没有主会话" : "当前没有符合此状态的主会话"}</h3><p>照常使用已接入的工具，或切换工具和状态。子 Agent 在所属主会话内查看，关系未知的会话不计入此列表。</p></div>`;
  document.querySelectorAll("[data-width]").forEach(el => el.style.width = `${el.dataset.width}%`);
  if (focusId) [...document.querySelectorAll(`#sessions [data-${focusKind}]`)].find(el => el.dataset[focusKind] === focusId && (!focusClass || el.classList.contains(focusClass)))?.focus({preventScroll:true});
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
  const request = ++state.snapshotRequest;
  try {
    const [snapshot, subscriptions] = await Promise.allSettled([api(`/api/snapshot?${query()}`),api("/api/subscriptions")]);
    if (request !== state.snapshotRequest) return;
    if (snapshot.status === "rejected") throw snapshot.reason;
    renderSnapshot(snapshot.value);
    if (subscriptions.status === "fulfilled") renderSubscriptions(subscriptions.value);
    else $("#subscription-cards").innerHTML = '<div class="empty-small">额度读取未完成，请更新后重试。会话数据仍可查看。</div>';
  } catch (error) {
    if (request !== state.snapshotRequest) return;
    $("#connection-text").textContent = "采集服务未连接";
    $("#connection-dot").classList.add("offline");
    $("#snapshot-time").textContent = "数据未更新 · 请检查启动看板的终端";
    if (!state.snapshot) $("#sessions").innerHTML = `<div class="empty-state"><h3>暂时无法连接本机服务</h3><p>${esc(error.message)}。可重新双击“启动看板.command”。</p></div>`;
  }
}
function updateToolOptions(ids=[]) {
  const select = $("#tool-filter");
  const selected = select.value || "all";
  const known = new Set([...Array.from(select.options).map(option => option.value),...ids].filter(id => typeof id === "string" && id && id !== "all"));
  const signature = JSON.stringify([...known]);
  if (select.dataset.signature === signature) return;
  select.innerHTML = '<option value="all">合计</option>' + [...known].map(tool => `<option value="${esc(tool)}">${esc(toolName(tool))}</option>`).join("");
  select.value = known.has(selected) ? selected : "all";
  select.dataset.signature = signature;
}
function heatmapTip(day,todayDate) {
  if (day.date > todayDate) return `${day.date} · 未来日期`;
  if (!day.has_usage || day.tokens == null) return `${day.date} · 未取得用量记录`;
  return `${day.date} · ${fmt(day.tokens)} 已采集 token`;
}
function hideHeatmapTooltip() {
  $("#heatmap-tooltip").hidden = true;
  document.querySelectorAll(".heatmap-day[aria-describedby]").forEach(cell => cell.removeAttribute("aria-describedby"));
}
function inspectHeatmapDay(cell,tooltip=true) {
  if (!cell) return;
  state.heatmapFocus = cell.dataset.date;
  document.querySelectorAll(".heatmap-day").forEach(day => day.tabIndex = day === cell ? 0 : -1);
  $("#heatmap-selection").textContent = cell.dataset.tip;
  if (!tooltip) return;
  hideHeatmapTooltip();
  const box = $("#heatmap-tooltip");
  box.textContent = cell.dataset.tip;
  box.hidden = false;
  cell.setAttribute("aria-describedby","heatmap-tooltip");
  const rect = cell.getBoundingClientRect();
  const width = box.offsetWidth;
  box.style.left = `${Math.max(8,Math.min(window.innerWidth - width - 8,rect.left + rect.width / 2 - width / 2))}px`;
  box.style.top = `${Math.max(8,rect.top - box.offsetHeight - 10)}px`;
}
function renderHeatmap(result) {
  const focusedDate = document.activeElement?.closest(".heatmap-day")?.dataset.date;
  const dates = result.days || [];
  const start = new Date(`${result.start}T00:00:00Z`);
  const offset = (start.getUTCDay()+6)%7;
  const weeks = Math.ceil((dates.length+offset)/7);
  const values = dates.filter(day => day.has_usage && day.tokens != null && day.date <= result.date).map(day => day.tokens);
  const maximum = Math.max(0,...values);
  const months = dates.map((day,index) => ({day,index})).filter(({day}) => day.date.endsWith("-01")).map(({day,index}) => `<span data-column="${Math.floor((index+offset)/7)+1}">${Number(day.date.slice(5,7))} 月</span>`).join("");
  const focusDate = dates.some(day => day.date === state.heatmapFocus) ? state.heatmapFocus : result.date;
  hideHeatmapTooltip();
  $("#heatmap").innerHTML = `<div class="heatmap-months" aria-hidden="true" data-weeks="${weeks}">${months}</div><div class="heatmap-calendar"><div class="heatmap-weekdays" aria-hidden="true"><span>一</span><span></span><span>三</span><span></span><span>五</span><span></span><span>日</span></div><div class="heatmap-grid" data-weeks="${weeks}" role="group" aria-label="${esc(result.year)} 年每日已采集 Token，方向键浏览">${dates.map((day,index) => {
    const future = day.date > result.date;
    const missing = !day.has_usage || day.tokens == null;
    const level = !day.tokens || !maximum ? 0 : Math.min(4,Math.max(1,Math.ceil(Math.sqrt(day.tokens/maximum)*4)));
    const kind = future ? "heatmap-future" : missing ? "heatmap-missing" : `heatmap-level-${level}`;
    const tip = heatmapTip(day,result.date);
    return `<button type="button" class="heatmap-day ${kind}${day.date === result.date ? " heatmap-today" : ""}" data-date="${esc(day.date)}" data-tip="${esc(tip)}" tabindex="${day.date === focusDate ? 0 : -1}" aria-label="${esc(tip)}" ${day.date === result.date ? 'aria-current="date"' : ""} data-column="${Math.floor((index+offset)/7)+1}" data-row="${(index+offset)%7+1}"></button>`;
  }).join("")}</div></div>`;
  document.querySelectorAll("#heatmap [data-weeks]").forEach(el => el.style.setProperty("--weeks",el.dataset.weeks));
  document.querySelectorAll("#heatmap [data-column]").forEach(el => el.style.gridColumn = el.dataset.column);
  document.querySelectorAll("#heatmap [data-row]").forEach(el => el.style.gridRow = el.dataset.row);
  if (focusedDate) [...document.querySelectorAll(".heatmap-day")].find(el => el.dataset.date === focusedDate)?.focus({preventScroll:true});
  const selected = dates.find(day => day.date === state.heatmapFocus);
  $("#heatmap-selection").textContent = selected ? heatmapTip(selected,result.date) : "悬停或点选日期查看用量，方向键可浏览日期。";
}
async function loadHistory() {
  const request = ++state.request;
  const tool = $("#tool-filter").value;
  if (state.historyTool !== tool) {
    state.historyTool = tool;
    state.history = null;
    state.heatmapFocus = null;
    $("#history-metrics").innerHTML = '<div class="loading-block">正在读取年度用量…</div>';
    $("#heatmap").innerHTML = '<div class="empty-small">正在准备日历…</div>';
    $("#chart-total").textContent = "—";
    $("#breakdown").innerHTML = "";
    $("#history-status").textContent = "正在读取本年度记录…";
    $("#heatmap-selection").textContent = "悬停或点选日期查看用量，方向键可浏览日期。";
    hideHeatmapTooltip();
  }
  try {
    const result = await api(`/api/heatmap?${new URLSearchParams({tool})}`);
    if (request !== state.request || tool !== $("#tool-filter").value) return;
    updateToolOptions(result.tools || []);
    const signature = JSON.stringify(result);
    if (state.history?.signature === signature) {
      $("#history-status").textContent = state.history.notice;
      $("#history-status").classList.remove("failed");
      return;
    }
    const total = result.totals;
    const recorded = result.days.filter(day => day.has_usage && day.tokens != null && day.date <= result.date).length;
    $("#heatmap-year").textContent = `${result.year} 年`;
    $("#period-range").textContent = `${result.start} — ${result.year}-12-31 · 北京时间`;
    $("#history-metrics").innerHTML = metric("年度已采集 Token",compact(total.total),result.has_usage ? "输入 + 输出，缓存不重复累加" : "本年度未取得用量记录","↗","green") + metric("输入 Token",compact(total.input),"含来源已报告的缓存用量","↘") + metric("输出 Token",compact(total.output),"含来源已报告的推理用量","↗") + metric("有记录的日期",`${fmt(recorded)}<span class="metric-unit">天</span>`,"未记录的日期不视为零消耗","▦");
    $("#chart-total").textContent = compact(total.total);
    const notice = [result.has_usage ? `截至 ${result.date} · ${tool === "all" ? "全部工具合计" : toolName(tool)}` : `截至 ${result.date}，本年度尚未取得${tool === "all" ? "" : toolName(tool)}用量记录；日历保留未记录状态。`,result.partial_sessions ? `${result.partial_sessions} 个会话存在采集覆盖缺口` : "",result.first_usage_day ? `最早可观测 ${result.first_usage_day}` : ""].filter(Boolean).join(" · ");
    $("#history-status").textContent = notice;
    $("#history-status").classList.remove("failed");
    renderHeatmap(result);
    const breakdown = [["cache_read","缓存读取 · 输入子集"],["cache_write","缓存写入 · 输入子集"],["reasoning","推理 · 输出子集"]];
    $("#breakdown").innerHTML = breakdown.map(([key,label]) => `<div><span>${label}</span><strong>${fmt(total[key])}</strong></div>`).join("");
    state.history = {signature,notice};
  } catch (error) {
    if (request !== state.request || tool !== $("#tool-filter").value) return;
    $("#history-status").textContent = `年度用量未更新：${error.message}。可点击“刷新数据”重试。${state.history ? " 下方保留上次结果。" : ""}`;
    $("#history-status").classList.add("failed");
    if (!state.history) {
      $("#history-metrics").innerHTML = '<div class="loading-block">年度用量暂不可用 · —</div>';
      $("#heatmap").innerHTML = '<div class="empty-small">尚未取得年度日历，请刷新重试。</div>';
    }
    throw error;
  }
}

async function loadSettings() {
  const data = await api("/api/integrations");
  updateToolOptions(data.sources.map(source => source.tool));
  $("#demo-banner").hidden = !data.demo;
  $("#enable-claude").disabled = Boolean(data.demo);
  $("#restore-claude").disabled = Boolean(data.demo);
  $("#include-statusline").disabled = Boolean(data.demo);
  $("#source-panels").innerHTML = data.sources.map(source => {
    const config = data.lifecycles?.[source.tool];
    const status = config?.error || (config?.complete ? config.last_hook_at ? "已配置 · 已收到上报" : "已配置 · 等待首次上报" : config?.enabled ? "部分事件已配置" : "状态增强尚未配置");
    const lifecycle = config ? `<div class="source-precision">${esc(status)}<br>最近状态上报：${esc(dateTime(config.last_hook_at))}${source.tool === "codex" && config.enabled && !config.last_hook_at ? "<br>首次上报前，请确认已在 Codex CLI 的 /hooks 中审阅并信任新增项。" : ""}</div><div class="enhance-actions"><button class="button secondary" data-lifecycle-enable="${esc(source.tool)}" ${data.demo ? "disabled" : ""}>预览状态接入</button>${config.enabled ? `<button class="button secondary" data-lifecycle-restore="${esc(source.tool)}" ${data.demo ? "disabled" : ""}>撤销状态接入</button>` : ""}</div>` : "";
    return `<div class="source-panel"><div class="source-heading">${toolIcon(source.tool,true)}<h3>${esc(source.name)}</h3><span class="status-chip ${source.present ? "idle" : "unknown"}">${source.present ? "发现本地目录" : "尚未发现"}</span></div><p>${esc(source.label)}</p><div class="source-precision">${esc(source.precision)}</div>${lifecycle}</div>`;
  }).join("");
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
function stopDetails() {
  state.detail?.controller?.abort();
  state.detail = null;
}
function setModalActions(actions) {
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
}
function modal(title,html,actions=[]) {
  stopDetails();
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  setModalActions(actions);
  if (!$("#modal").open) $("#modal").showModal();
}
function closeModal() { stopDetails(); $("#modal").close(); }
async function jumpTo(id,kind) {
  try { const result = await api("/api/jump",{id,...(kind ? {kind} : {})}); toast(result.message || "已发起跳转，请查看对应应用"); }
  catch (error) { toast(error.message); }
}
function sourceLabel(card) {
  return ({hook:`${toolName(card.tool)} 状态上报`,process:"进程观测",event:"客户端状态事件",record:"本地事件记录",demo:"人工演示事件"})[card.state_source] || card.state_source || "未知";
}
function detailMarkup(card) {
  const entries = [["会话 ID",card.source_id],["所属项目",card.project_name],["工作目录",card.working_directory || card.worktree_name || "未识别"],["角色",roles[card.role] || "关系未知"],["父会话",card.parent_id || "—"],["客户端版本",card.version || "未知"],["最近状态",`${labels[card.state] || card.state || "待确认"} · ${dateTime(card.state_at)}`],["状态依据",sourceLabel(card)],["进程状态",({online:"进程在线",offline:"进程已退出",unknown:"进程待匹配"})[card.liveness] || "进程待匹配"],["跳转精度",card.jump.precision || "未知"],["采集覆盖",({observed:"已观测记录",status_only:"仅状态上报 · 未发现会话记录",partial_baseline:"缺少早期用量基线",counter_reset:"来源累计计数曾重置",partial_usage:"部分用量字段缺失",partial_records:"部分记录无法读取",missing_time:"部分记录缺少时间",missing_request_id:"部分用量缺少请求身份"})[card.coverage] || card.coverage || "未知"],["今日 token",fmt(card.usage.today)],["已采集累计 token",fmt(card.usage.observed_total)]];
  return `<div class="detail-grid">${entries.map(([label,value]) => `<div><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("")}</div><p class="detail-note">${card.stale ? "当前状态来自较早的事件，请跳回原工具确认。" : "在线表示匹配到进程，未确认执行的会话保持待确认。"} 用量只覆盖可读取的来源；看板不会猜测缺失数据。${!card.jump.available && card.jump.reason ? " " + esc(card.jump.reason) : ""}</p>`;
}
function selectDetail(card) {
  const detail = state.detail;
  if (!detail || !card) return;
  detail.selected = card;
  $("#detail-content").innerHTML = detailMarkup(card);
  $("#detail-selected-title").innerHTML = `${toolIcon(card.tool)}<span>${esc(card.alias || card.project_name)}<small>${esc(roles[card.role] || "关系未知")} · ${esc((card.source_id || "").slice(0,8))}</small></span>${card.id !== detail.parentId ? '<button class="button secondary" data-parent-detail>返回主会话</button>' : ""}`;
  $("#alias-input").value = card.alias || "";
  $("#detail-freshness").hidden = true;
  const id = card.id;
  setModalActions([
    {label:"保存名称",primary:true,run:async () => {
      if (state.detail !== detail || detail.selected.id !== id) return;
      const submittedAlias = $("#alias-input").value;
      await api("/api/alias",{id,alias:submittedAlias});
      if (state.detail === detail && detail.selected.id === id && $("#alias-input").value === submittedAlias) closeModal();
      await refresh();
      toast("名称已保存");
    }},
    ...(card.jump.available ? [{label:card.jump.label,run:() => jumpTo(id)}] : []),
    ...(!state.snapshot?.demo && card.tool === "codex" && card.surface === "vscode" && card.jump.available ? [{label:"用 Codex 桌面打开",run:() => jumpTo(id,"codex")}] : []),
    {label:"关闭",run:closeModal}
  ]);
}
function renderChildren(data) {
  const focused = document.activeElement?.closest("[data-child-detail],[data-child-jump]");
  const focusKind = focused?.hasAttribute("data-child-jump") ? "childJump" : "childDetail";
  const focusId = focused?.dataset[focusKind];
  const detail = state.detail;
  if (!detail) return;
  const c = data.counts;
  const summary = [`${fmt(c.total)} 个子 Agent`,`${fmt(c.active)} 个执行中`,`${fmt(c.waiting)} 个等待`,`${fmt(c.error)} 个错误`,`${fmt(c.unknown)} 个待确认`];
  $("#children-summary").textContent = summary.join(" · ");
  $("#children-list").innerHTML = data.children.length ? data.children.map(card => `<div class="child-row" data-depth="${Math.min(3,Math.max(0,(card.depth || 1)-1))}"><button class="child-open" data-child-detail="${esc(card.id)}" aria-label="查看 ${esc(card.alias || card.project_name)} 子 Agent 详情"><span class="child-heading">${toolIcon(card.tool)}<span class="child-title">${esc(card.alias || card.project_name)}<small>${esc(toolName(card.tool))} · ${esc((card.source_id || "").slice(0,8))} · 第 ${fmt(card.depth || 1)} 层</small></span><span class="status-chip ${esc(card.state)}">${esc(labels[card.state] || card.state || "待确认")}</span></span><span class="child-stage">${esc(card.stage || "状态待确认")}${card.stale ? '<em>旧事件 · 需确认</em>' : ""}</span><span class="child-evidence">${esc(sourceLabel(card))} · ${esc(({online:"进程在线",offline:"进程已退出",unknown:"进程待匹配"})[card.liveness] || "进程待匹配")}</span></button><button class="jump-button child-jump" data-child-jump="${esc(card.id)}" ${card.jump.available ? "" : "disabled"} title="${esc((!card.jump.available && card.jump.reason) || card.jump.precision || "尚未识别可跳转入口")}">${esc(card.jump.label)} ↗</button></div>`).join("") : '<div class="children-empty">尚未发现这个主会话的子 Agent。</div>';
  document.querySelectorAll(".child-row[data-depth]").forEach(el => el.style.setProperty("--depth",el.dataset.depth));
  if (focusId) [...document.querySelectorAll("#children-list [data-child-detail],#children-list [data-child-jump]")].find(el => el.dataset[focusKind] === focusId)?.focus({preventScroll:true});
  $("#children-note").textContent = data.truncated ? "子 Agent 较多，当前仅展示部分可见记录。状态自动更新；等待与旧事件请在原工具确认。" : "含后续层级，状态自动更新；等待与旧事件请在原工具确认。";
}
async function loadChildren(force=false) {
  const detail = state.detail;
  if (!detail || !$("#modal").open || (detail.loading && !force)) return;
  detail.controller?.abort();
  const controller = new AbortController();
  detail.controller = controller;
  detail.loading = true;
  if (force) {
    detail.signature = null;
    $("#children-summary").textContent = "正在重新读取…";
    $("#children-list").innerHTML = '<div class="empty-small">正在读取子 Agent…</div>';
  }
  try {
    const data = await api(`/api/children?${new URLSearchParams({id:detail.parentId})}`,undefined,{signal:controller.signal});
    if (state.detail !== detail || controller.signal.aborted || !$("#modal").open) return;
    detail.children = data.children;
    updateToolOptions(data.children.map(card => card.tool));
    $("#detail-freshness").hidden = true;
    const signature = JSON.stringify(data.children.map(({state_age,...card}) => card)) + JSON.stringify(data.counts) + data.truncated;
    if (detail.signature !== signature) {
      renderChildren(data);
      detail.signature = signature;
    }
    if (detail.selected.id !== detail.parentId) {
      const selected = data.children.find(card => card.id === detail.selected.id);
      if (selected) { detail.selected = selected; $("#detail-content").innerHTML = detailMarkup(selected); }
      else {
        $("#detail-freshness").textContent = "当前子 Agent 已不在可见列表，无法确认其最新状态。已保留选中信息和名称输入，可返回主会话查看。";
        $("#detail-freshness").hidden = false;
      }
    }
  } catch (error) {
    if (state.detail !== detail || controller.signal.aborted || !$("#modal").open) return;
    detail.signature = null;
    $("#children-summary").textContent = "子 Agent 状态未更新";
    $("#children-list").innerHTML = `<div class="children-error"><p>${esc(error.message)}。尚无法确认当前子 Agent 列表。</p><button class="button secondary" data-children-retry>重新读取</button></div>`;
    $("#children-note").textContent = "读取失败不表示没有子 Agent，请重试。";
    $("#detail-freshness").textContent = "子 Agent 状态未更新，下方选中信息可能较旧；名称输入已保留。";
    $("#detail-freshness").hidden = false;
  } finally {
    if (state.detail === detail && detail.controller === controller) detail.loading = false;
  }
}
function refreshDetails() {
  const detail = state.detail;
  if (!detail || !$("#modal").open) return;
  const parent = state.snapshot?.sessions.find(card => card.id === detail.parentId);
  if (parent) {
    detail.parent = parent;
    if (detail.selected.id === detail.parentId) {
      detail.selected = parent;
      $("#detail-content").innerHTML = detailMarkup(parent);
    }
  }
  loadChildren();
}
function details(id) {
  const card = state.snapshot?.sessions.find(c => c.id === id && c.role === "main");
  if (!card) return;
  modal("主会话与子 Agent",'<div class="detail-selected-title" id="detail-selected-title"></div><div class="detail-freshness" id="detail-freshness" role="status" hidden></div><section class="children-panel" aria-labelledby="children-title"><div class="children-section-heading"><h3 id="children-title">子 Agent</h3><span class="muted" id="children-summary">正在读取…</span></div><div id="children-list"><div class="empty-small">正在读取子 Agent…</div></div><p class="children-note" id="children-note">只展示明确关联的子 Agent，不展示对话内容。</p></section><details class="session-metadata" open><summary>会话信息与用量</summary><div id="detail-content"></div></details><label class="alias-label" for="alias-input">给当前选中的会话起一个名字</label><input class="text-input" id="alias-input" maxlength="80" placeholder="例如：AgentWatch 接入调试">');
  state.detail = {parentId:id,parent:card,selected:card,children:[],loading:false,controller:null,signature:null};
  selectDetail(card);
  loadChildren();
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

async function previewLifecycle(tool) {
  const preview = await api(`/api/${tool}/preview`,{});
  const sample = {hooks:{PermissionRequest:[{hooks:[{type:"command",command:preview.command,timeout:3,async:false}]}]}};
  modal(`预览 ${toolName(tool)} 状态接入`,`<p>${esc(preview.effect)}</p><div class="detail-note">${esc(preview.privacy)}</div><div class="event-tags">${preview.events.map(e => `<span>${esc(e)}</span>`).join("")}</div><p>写入 ${esc(preview.settings_label)}，保留已有 hooks，并在本机备份。</p><pre>${esc(JSON.stringify(sample,null,2))}</pre><p class="detail-note">${esc(preview.next_step)}</p>`,[
    {label:"保存状态接入",primary:true,run:async () => { await api(`/api/${tool}/enable`,{hash:preview.hash}); closeModal(); await loadSettings(); toast(preview.next_step); }},
    {label:"取消",run:closeModal}
  ]);
}
function restoreLifecycle(tool) {
  modal(`撤销 ${toolName(tool)} 状态接入`,"<p>移除 AgentWatch 添加的状态 hooks，保留其他设置、历史用量和本机备份。</p>",[
    {label:"确认撤销",primary:true,run:async () => { await api(`/api/${tool}/restore`,{}); closeModal(); await loadSettings(); toast("状态接入配置已撤销"); }},
    {label:"取消",run:closeModal}
  ]);
}

$("#tool-filter").innerHTML = '<option value="all">合计</option>' + Object.entries(tools).map(([tool,meta]) => `<option value="${esc(tool)}">${esc(meta.name)}</option>`).join("");
$("#tool-sources").innerHTML = Object.entries(tools).map(([tool,meta]) => `<div class="tool-link">${toolIcon(tool)}<span>${esc(meta.name)}<small>${esc(meta.entry)}</small></span><span class="small-dot"></span></div>`).join("");

document.querySelectorAll("[data-view]").forEach(button => button.onclick = () => showView(button.dataset.view));
document.querySelectorAll("[data-state]").forEach(button => button.onclick = () => {
  state.status = button.dataset.state;
  document.querySelectorAll("[data-state]").forEach(el => el.classList.toggle("selected",el === button));
  renderCards();
});
document.querySelectorAll(".filterbar select").forEach(select => select.onchange = async () => {
  if (state.view === "history") loadHistory().catch(() => {});
  await refresh();
});
$("#refresh").onclick = async () => {
  try { await api("/api/rescan",{}); await refresh(); if (state.view === "history") await loadHistory(); if (state.view === "settings") await loadSettings(); toast("已请求重新采集"); }
  catch (error) { toast(error.message); }
};
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
$("#sessions").onclick = event => {
  const detail = event.target.closest("[data-detail]");
  const jump = event.target.closest("[data-jump]");
  if (detail) details(detail.dataset.detail);
  if (jump && !jump.disabled) jumpTo(jump.dataset.jump);
};
$("#modal-close").onclick = closeModal;
$("#modal").addEventListener("close",stopDetails);
$("#modal-body").onclick = event => {
  const detail = state.detail;
  if (!detail) return;
  const child = event.target.closest("[data-child-detail]");
  const jump = event.target.closest("[data-child-jump]");
  if (child) selectDetail(detail.children.find(card => card.id === child.dataset.childDetail));
  if (jump && !jump.disabled) jumpTo(jump.dataset.childJump);
  if (event.target.closest("[data-parent-detail]")) selectDetail(detail.parent);
  if (event.target.closest("[data-children-retry]")) loadChildren(true);
};
$("#heatmap").addEventListener("pointerover",event => inspectHeatmapDay(event.target.closest(".heatmap-day")));
$("#heatmap").addEventListener("pointerout",event => { if (!event.relatedTarget?.closest?.(".heatmap-day")) hideHeatmapTooltip(); });
$("#heatmap").addEventListener("focusin",event => inspectHeatmapDay(event.target.closest(".heatmap-day")));
$("#heatmap").addEventListener("focusout",hideHeatmapTooltip);
$("#heatmap").addEventListener("click",event => inspectHeatmapDay(event.target.closest(".heatmap-day")));
$("#heatmap").addEventListener("keydown",event => {
  const cell = event.target.closest(".heatmap-day");
  if (!cell) return;
  if (event.key === "Escape") { hideHeatmapTooltip(); return; }
  const cells = [...document.querySelectorAll(".heatmap-day")];
  const index = cells.indexOf(cell);
  const offset = {ArrowLeft:-7,ArrowRight:7,ArrowUp:-1,ArrowDown:1}[event.key];
  if (offset === undefined && !["Home","End"].includes(event.key)) return;
  event.preventDefault();
  const target = event.key === "Home" ? 0 : event.key === "End" ? cells.length-1 : Math.max(0,Math.min(cells.length-1,index+offset));
  cells[target]?.focus({preventScroll:true});
  cells[target]?.scrollIntoView({block:"nearest",inline:"nearest"});
});
$("#heatmap-scroll").addEventListener("scroll",hideHeatmapTooltip);
window.addEventListener("resize",hideHeatmapTooltip);
window.addEventListener("scroll",hideHeatmapTooltip,{passive:true});
$("#modal").onclick = event => { if (event.target === $("#modal")) { const r = $("#modal").getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) closeModal(); } };
$("#enable-claude").onclick = previewClaude;
$("#restore-claude").onclick = restoreClaude;
$("#source-panels").onclick = event => {
  const enable = event.target.closest("[data-lifecycle-enable]");
  const restore = event.target.closest("[data-lifecycle-restore]");
  if (enable) previewLifecycle(enable.dataset.lifecycleEnable).catch(error => toast(error.message));
  if (restore) restoreLifecycle(restore.dataset.lifecycleRestore);
};
api("/api/integrations").then(data => updateToolOptions(data.sources.map(source => source.tool))).catch(() => {});
refresh();
setInterval(() => { refresh(); if (state.view === "history") loadHistory().catch(() => {}); },4000);
document.addEventListener("visibilitychange",() => { if (!document.hidden) refresh(); });
