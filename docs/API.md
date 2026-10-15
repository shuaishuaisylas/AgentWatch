# 本地 API 约定

适用于原生 `web/` 前端与 `agentwatch/server.py`。本服务只监听 `127.0.0.1`；以启动时打印的端口为准，演示联调通常是 `http://127.0.0.1:8767`。示例均为人工数据，字段仅列与前端联调相关的部分；新增兼容字段不应使页面报错。

## 请求和安全校验

先访问同源首页 `/`：后端设置 `agentwatch` cookie，并把 CSRF 值注入 `<meta name="csrf">`。cookie 为 `HttpOnly; SameSite=Strict; Path=/`，每次服务重启都会更换。接口使用相对路径，前端发送 `credentials: "same-origin"`。

所有 `/api/` 请求都需要首页 cookie。POST 还需 `Content-Type: application/json` 和 `X-AgentWatch-CSRF`，值取首页 meta。POST body 必须是 JSON 对象，非空且不超过 16,384 字节；无参数操作传 `{}`。

Host 必须是服务端口对应的 `127.0.0.1` 或 `localhost`；存在 Origin 时必须与请求 Host 同源。服务没有开放跨域 API，不允许通过其他网站、另一个前端端口或 `file://` 连接；不要为开发移除这些校验。服务重启后刷新首页以重新取得 cookie 和 CSRF。

```js
const response = await fetch("/api/alias", {
  method: "POST",
  credentials: "same-origin",
  headers: {
    "Content-Type": "application/json",
    "X-AgentWatch-CSRF": document.querySelector('meta[name="csrf"]').content
  },
  body: JSON.stringify({id: "codex:demo-design", alias: "界面开发"})
});
const data = await response.json();
if (!response.ok) throw new Error(data.error || "请求失败");
```

## 公共数据语义

| 项目 | 约定 |
| --- | --- |
| 时间 | `at`、`now`、`state_at` 等数值为 Unix epoch **秒**，可能含小数；不是毫秒。`state_age` 是秒数 |
| 日期/分桶 | `YYYY-MM-DD`，按 `Asia/Shanghai`；周期起点包含、终点不包含；周从周一开始 |
| 缺失值 | `null` 表示未知或没有已采集数据，显示“—”或明确空态；`0` 只表示已知零值 |
| token | 总量为输入 + 输出；缓存和推理是已报告的分项，不能再次累加到总量 |
| 工具 | 当前 `codex`、`claude`、`workbuddy`；新工具保留名称回退，避免只处理两家 |
| 入口 | `desktop`、`vscode`、`terminal`、`unknown` |
| 角色 | `main`、`child`、`unknown`；未知不能归为主会话 |
| 执行状态 | `active`、`waiting_user`、`waiting_approval`、`idle`、`ended`、`unknown`、`error` |
| 在线状态 | `online`、`offline`、`unknown`；进程在线不代表执行中 |
| 演示标记 | `/api/snapshot` 和 `/api/integrations` 顶层 `demo` 为布尔值；演示 `true`，真实模式 `false` |

陈旧 active 在快照中会保守降为 `unknown`。旧等待仍可能保留等待状态，前端必须展示 `stale` 提示。`idle` 是本轮结束，`ended` 是运行结束，均不等于业务目标成功。

## 读取接口和筛选

| 方法/路径 | 内容 |
| --- | --- |
| `GET /api/snapshot` | 当前在线或今日活动会话、计数、动态、项目与采集状态 |
| `GET /api/stats` | 指定周期的用量、会话去重、轮次、任务与图表分桶 |
| `GET /api/integrations` | 数据来源发现情况、Claude 增强状态与演示标记 |
| `GET /api/subscriptions` | 独立的账号订阅/积分额度快照，不受会话筛选影响 |

snapshot/stats 共用筛选：

| 参数 | 值/默认值 | 说明 |
| --- | --- | --- |
| `tool` | `all`（默认）或工具标识 | 按工具筛选 |
| `project` | `all`（默认）或 `project_id` | 用接口返回的项目身份，不传项目名称 |
| `surface` | `all`（默认）或入口标识 | 当前页面未提供入口筛选控件，接口支持 |
| `role` | **`main`（API 默认）**、`child`、`unknown`、`all` | UI 初始显式传 `all`，避免隐藏关系未知来源 |

stats 另支持 `period=day|week|month|year`（默认 day）、`date=YYYY-MM-DD`（默认北京时间今天）。非法日期或周期回落到服务端默认值，前端仍应传有效值。snapshot 固定为今日/在线范围，不接收历史周期；历史数据到 stats 查询。

### `GET /api/snapshot?tool=all&project=all&role=all`

主要结构：

```json
{
  "demo": true,
  "now": 1792029600,
  "date": "2026-10-15",
  "timezone": "Asia/Shanghai",
  "counts": {"observed": 1, "online": 0, "active": 0, "waiting": 0, "error": 0, "unknown": 1, "unknown_roles": 1},
  "sessions": [{
    "id": "workbuddy:demo-research",
    "tool": "workbuddy",
    "source_id": "demo-research",
    "surface": "desktop",
    "project_id": "demo-project",
    "project_name": "演示项目",
    "role": "unknown",
    "parent_id": null,
    "alias": "",
    "branch": "",
    "version": "",
    "state": "unknown",
    "stage": "最近阶段：工具返回",
    "state_at": 1792029540,
    "state_source": "record",
    "state_age": 60,
    "stale": false,
    "liveness": "unknown",
    "context_used": null,
    "context_limit": null,
    "context_percent": null,
    "context_at": null,
    "context_estimated": 0,
    "context_stale": false,
    "coverage": "observed",
    "usage": {"input": 1200, "output": 300, "cache_read": 400, "cache_write": null, "observed_total": 1500, "today": 1500, "cumulative": 1500},
    "jump": {"kind": "none", "label": "演示模式", "precision": null, "available": false},
    "worktree_name": null
  }],
  "truncated": false,
  "activity": [],
  "projects": [{"id": "demo-project", "name": "演示项目", "sessions": 1}],
  "scan": {"running": false, "files": 0, "pending": 0, "read_bytes": 0, "last_scan": 1792029600, "errors": 0},
  "today": {}
}
```

示例省略了若干会话元数据；`today` 的实际内容与同筛选的当日 stats 结构相同。会话标题优先使用人工 `alias`，否则显示项目名；不要从原始对话生成标题。

- `counts` 基于筛选后、截断前的会话计算；最多返回最优先的 160 张卡片，`truncated=true` 时提示缩小范围。`unknown_roles` 是全局今日关系未知计数，不代表当前工具/项目内的数量。
- `projects` 是已采集项目列表，不限当前筛选；`activity` 返回当前筛选内今日最近 24 条，字段含 `id`、`at`、`kind`、`label`、`tool`、`project_name`、`session_id`。
- `usage.today` 是北京时间当天已采集总量；`observed_total` 为看板已采集累计。`cumulative` 优先显示来源报告累计，可能高于 `observed_total`，两者不能互相替代。
- 仅当 `context_percent` 有效时绘制百分比；`context_used` 有值但分母未知时只展示 token 数。WorkBuddy 不提供上下文快照。
- `state_source` 包括本地记录 `record`、明确事件 `event`、Claude 上报 `hook`、进程观测 `process`；演示可能为 `demo`，不能把它当真实来源。
- `coverage` 当前可能为 `observed`、`partial_baseline`、`counter_reset`、`partial_usage`、`partial_records`、`missing_time`、`missing_request_id`；新增值应有通用显示回退。
- `jump` 使用服务端给出的按钮文字、可用性和精度；真实模式可能为 `codex`、`codex_vscode`、`terminal`、`project`、`claude`、`workbuddy` 或 `none`。打开应用与定位会话是不同精度。

### `GET /api/stats?period=week&date=2026-10-15&role=all`

| 字段 | 含义 |
| --- | --- |
| `period`、`start`、`end`、`timezone` | 实际周期；`end` 为不包含的结束日期 |
| `totals` | `input`、`output`、`cache_read`、`cache_write`、`reasoning`、`total`；没有已采集值的分项为 null |
| `sessions` | 周期内有可观测活动的会话数，按会话 ID 去重 |
| `turns` | Codex 已报告活动轮次数，不是业务任务 |
| `tasks`、`task_scope` | 有可靠 ID 的 Claude 原生任务数及覆盖说明；没有可靠覆盖时 tasks=null |
| `by_tool` | 工具标识到已采集 total token 的映射，按返回键动态渲染 |
| `buckets` | 每项含 epoch 秒 `start`、`end`，以及 `label`、`tokens` |
| `first_usage_day` | 整个数据库最早用量日，不限当前筛选；可为 null |
| `partial_sessions` | 当前筛选内存在覆盖缺口的会话数，不限选定周期 |
| `has_usage` | 所选周期是否取得用量记录 |

没有用量时 `totals` 分项为 null，不能仅因空图的 bucket 值为 0 就展示“已确认零消耗”。统计不提供订阅周额度或积分余额。

### `GET /api/integrations`

主要字段：

- `demo`：用于演示标识和禁用真实操作。
- `sources[]`：`tool`、`name`、`present`、`label`、`precision`；`present` 表示发现来源目录，不代表准确状态已验收。
- `claude`：`enabled`、`installed_events`、`complete`、`statusline_enabled`、`backup`、`error`；配置读取失败时可能没有 complete。配置完成不等于收到真实上报。
- `last_hook_at`：最近上报 epoch 秒或 null；`desktop_code_sessions`：已发现的桌面 Code 会话数。
- `deferred`、`data_label`、`privacy`：尚未接入来源、数据位置展示文字和隐私说明；前端把位置当文本，不据此访问文件。

## 订阅与积分额度

### `GET /api/subscriptions`

响应为 `{"demo":false,"now":1792029600,"platforms":[...]}`。接口需要同源首页 cookie，没有查询参数；工具、项目、角色筛选不改变账号额度。每个 `platforms[]` 包含：

| 字段 | 类型与含义 |
| --- | --- |
| `tool`、`name` | 平台标识与名称；当前三家均返回，即使没有数据 |
| `status` | `available` 已取得至少一个有效窗口、`stale` 仅有旧或过期窗口、`unavailable` 未报告、`auth_required` WorkBuddy 需要凭证/授权、`error` 查询失败 |
| `updated_at` | 来源上次报告的 epoch 秒或 null；GET 请求时间不能当作来源更新时间 |
| `notice` | 口径和接入条件，按文本展示，不解析成页面代码 |
| `refreshing` | WorkBuddy 后台是否正在查询；其余为 false |
| `limits[]` | 实际取得的窗口列表；空列表表示未知，不能补造 100% |

每个窗口含 `key`、`bucket`、`label`、`source`、`observed_at`，以及：

| 字段 | 类型与含义 |
| --- | --- |
| `unit` | `percent`（Codex/Claude）或 `credits`（WorkBuddy） |
| `limit`、`used`、`remaining` | 数值；百分比 limit=100；积分按官方资源包总额计算，剩余不小于 0 |
| `remaining_percent` | 百分比数值或 null；积分总额为 0 时为 null，禁止除零 |
| `window_minutes` | 来源实际窗口长度或 null；10080 为周、300 为 5 小时；不能硬编码 Codex primary=5小时 |
| `resets_at` | 下一次重置 epoch 秒或 null；WorkBuddy 总包积分不提供此字段 |
| `status` | `available`、`stale`（超过 900 秒或来源时间异常）、`expired`（已到重置时间） |

`expired` 窗口的 remaining 与 remaining_percent 为 null，等待新报告；不得自行恢复为 100%。`stale` 保留最后值，但必须标成旧快照，不承诺实时。WorkBuddy 更新失败时，可保留内存中的最后值并标为旧值；平台级错误提示不能被窗口旧值覆盖。不同 bucket 不能相加，Codex 模型专项额度须保留 bucket 标识。

Codex 来源为 `codex_record`，Claude 为 `claude_statusline`，WorkBuddy 正式数据为 `workbuddy_openapi`。它们是最近本机会话/正式凭证对应的账号快照，无法保证切换账号后仍代表当前账号。演示顶层 demo=true，所有数据为人工样例，凭证被忽略且不联网。

“更新额度”调用已有 `POST /api/rescan`，仅表示已请求采集/后台查询；WorkBuddy 最短间隔 30 秒，常规每 300 秒查询，页面持续读取缓存。缺少 WorkBuddy 凭证不发请求，也不复用之前进程的积分缓存。凭证配置和官方链接见 README。

## 写入与操作接口

下列接口均为 POST，均需 cookie 和 CSRF。不要把读取页面变成自动执行写操作。

| 路径 | JSON body | 成功结果/行为 | 演示模式 |
| --- | --- | --- | --- |
| `/api/rescan` | `{}` | `{"requested":true}`，请求重新采集；不是采集完成保证 | 不采集真实来源；下一次 snapshot 请求刷新人工样例 |
| `/api/alias` | `{"id":"codex:demo-design","alias":"界面开发"}` | `{"saved":true}`；名称会规范空白、最长 80 字符；空字符串清除 | 仅写临时数据库 |
| `/api/jump` | `{"id":"codex:demo-design"}`，可选 `kind` | `{"opened":true,"kind":"codex","message":"…"}`；只允许服务端确定的入口 | 403 禁止 |
| `/api/claude/preview` | `{"statusline":false}` | 返回新增配置预览，无配置写入 | 403 禁止 |
| `/api/claude/enable` | `{"hash":"预览返回的hash","statusline":false}` | 备份后合并 Claude 配置 | 403 禁止 |
| `/api/claude/restore` | `{}` | 移除看板添加的配置，保留其他修改 | 403 禁止 |

jump 的 `kind` 通常省略，使用服务端默认；Codex 会话详情允许显式请求 `kind="codex"` 作为桌面备用入口。其他任意路径、命令或 URL 不被接受。

Claude preview 返回 `hash`、`events`、`add_events`、`include_statusline`、`has_statusline`、`command`、`settings_label`、`effect`、`privacy`。前端先展示预览，由用户操作后再 enable；`hash` 来自本次预览，`statusline` 与预览选择一致。服务端检测设置变化会返回 409，重新预览后处理。真实模式中的配置操作不属于纯前端开发验收。

enable 成功返回当前 Claude 配置状态，字段与 integrations 中的 `claude` 一致；restore 返回 `removed`（移除 hook 数）、`enabled` 和 `message`。配置保存成功仍需真实客户端事件验证。

## 错误和联调检查

失败使用非 2xx 状态，响应形如 `{"error":"请从看板首页重新打开"}`。前端先解析 JSON，再按 `response.ok` 判断，向用户展示 `error`，不依赖完整文案做逻辑分支。

| 状态码 | 常见原因 |
| --- | --- |
| 400 | body、参数或跳转入口无效，或应用无法打开 |
| 401 | 缺少/过期 cookie；访问同源首页重新初始化 |
| 403 | Host/Origin/CSRF 校验失败，或演示模式禁止真实操作 |
| 404 | 接口或会话不存在 |
| 409 | Claude 配置在预览后改变，或不能安全合并 |
| 500 | 读取或操作未完成；页面保留已有数据并明确未更新 |

接口变更时同步本文、前端调用、人工演示数据和相关测试；记录字段类型、可空性、单位与兼容处理。不要用关掉校验、补零、猜状态或复制真实会话来解决联调问题。
