# 本地 API 约定

适用于 `web/` 前端、`desktop/` 原生客户端与 `agentwatch/server.py`。本服务只监听 `127.0.0.1`；以启动时打印的端口为准，演示联调通常是 `http://127.0.0.1:8767`。示例均为人工数据，字段仅列与前端联调相关的部分；新增兼容字段不应使页面报错。

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
| 工具 | 当前 `codex`、`claude`、`workbuddy`、`cursor`；新工具保留名称回退，避免只处理两家 |
| 入口 | `desktop`、`vscode`、`terminal`、`unknown` |
| 角色 | `main`、`child`、`unknown`；未知不能归为主会话 |
| 执行状态 | `active`、`waiting_user`、`waiting_approval`、`idle`、`ended`、`unknown`、`error` |
| 在线状态 | `online`、`offline`、`unknown`；进程在线不代表执行中 |
| 演示标记 | `/api/snapshot`、`/api/heatmap`、`/api/children` 和 `/api/integrations` 顶层 `demo` 为布尔值；演示 `true`，真实模式 `false` |

超过 120 秒没有新执行事件的 active 在快照中会保守降为 `unknown`，长工具调用也可能被降级。旧等待仍可能保留等待状态，前端必须展示 `stale` 提示。`idle` 是本轮结束后的空闲，`ended` 是运行结束，均不等于业务目标成功。

## 读取接口和筛选

| 方法/路径 | 内容 |
| --- | --- |
| `GET /api/snapshot` | 当前在线或今日活动会话、计数、动态、项目与采集状态 |
| `GET /api/stats` | 指定周期的用量、会话去重、轮次、任务与图表分桶 |
| `GET /api/heatmap` | 北京时间当前自然年的逐日 token 热力图，仅支持工具筛选 |
| `GET /api/children` | 指定可见主会话的全部明确子 Agent 后代及状态 |
| `GET /api/integrations` | 数据来源发现情况、各平台状态增强配置与最近上报、演示标记 |
| `GET /api/subscriptions` | 独立的账号订阅/积分额度快照，不受会话筛选影响 |

snapshot/stats 共用筛选：

| 参数 | 值/默认值 | 说明 |
| --- | --- | --- |
| `tool` | `all`（默认）或工具标识 | 按工具筛选 |
| `project` | `all`（默认）或 `project_id` | 用接口返回的项目身份，不传项目名称 |
| `surface` | `all`（默认）或入口标识 | 当前页面未提供入口筛选控件，接口支持 |
| `role` | **`main`（API 默认）**、`child`、`unknown`、`all` | 会话卡片 UI 显式传 `main`；未知角色不能冒充主会话，API 保留其他角色查询兼容性 |

stats 另支持 `period=day|week|month|year`（默认 day）、`date=YYYY-MM-DD`（默认北京时间今天）。非法日期或周期回落到服务端默认值，前端仍应传有效值。snapshot 固定为今日/在线范围，不接收历史周期；`role=main`（包括省略角色）时，也保留拥有今日活动或在线明确子 Agent 后代的旧主会话入口。只沿可见、明确 child 的父链回溯，不越过 unknown 或隐藏节点。主卡片按根会话工具/项目筛选，子树不受该筛选切断。其他角色查询保留原有今日/在线范围；历史数据到 stats 查询。

Codex 已归档会话及其所有后代不进入实时会话、计数或动态，即使共享进程仍在线；内部审核会话（`guardian_review` / `subagent.other=guardian`）同样隐藏，正常子 Agent 保留。归档以本地索引的明确标记为准；索引缺少此字段时可从 `archived_sessions` 目录识别。索引暂时不可用时，不撤销上次已确认的归档。常规每 20 秒重新发现，`/api/rescan` 可请求提前刷新；恢复归档后在下一次发现时重新展示。来源数据库与记录均保持只读。

隐藏只影响实时可见性，不删除会话、别名、采集游标或用量；`stats` 和 `snapshot.today` 保留已归档会话的历史统计。`projects` 继续提供历史项目，方便按项目查历史；不能用历史会话数判断当前可见数量。

配置的 Codex 数据目录中 `memories` 及其子目录可能产生只有状态上报的临时 ID。仅当其角色未知、未在本地线程索引登记、从未关联已解析的会话文件时，隐藏此记录及子 Agent 后代；已登记或有记录的用户会话、其他路径中名为 `memories` 的项目不受影响。以后发现索引或会话记录会重新展示；已采集过文件但后来文件消失，只禁用跳转，不按临时记录隐藏。临时记忆记录若没有历史用量，也不加入项目列表；历史用量与别名仍保留。

临时记忆判断需要成功读取本地线程索引；索引缺失或不可读时不新增此类隐藏，重启也保留先前可见性。明确的新索引登记、角色或已解析文件可以撤销临时隐藏。

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
    "worktree_name": null,
    "working_directory": "/example/projects/演示项目"
  }],
  "truncated": false,
  "activity": [],
  "projects": [{"id": "demo-project", "name": "演示项目", "sessions": 1}],
  "scan": {"running": false, "files": 0, "pending": 0, "read_bytes": 0, "last_scan": 1792029600, "errors": 0},
  "today": {}
}
```

示例省略了若干会话元数据；`today` 的实际内容与同筛选的当日 stats 结构相同。会话名称元数据兼容增加以下可空字段，旧调用端可忽略：

- `session_title`：现成的客户端会话名，Codex 优先只读 `session_index.jsonl` 中匹配身份的 `thread_name`；旧版本兼容线程 SQLite 的 `title`。两者并非总是相同，不能把旧 `title` 是否非空当作与侧栏一致的证据。不从首条消息、对话正文或摘要生成名称，不覆盖人工 `alias`。
- `project_display_name`：客户端已保存的项目显示名；Codex 根据正式项目身份或标准化完整根路径匹配读取，不能按目录 basename 猜测。原 `project_id` / `project_name` 仍表示既有工作目录项目聚合，不因此改写 Web 筛选或导航。

名称规范空白和 NUL、最多保留 160 字；来源缺失或不可读时保留已知值，明确清空时回退 null。原生优先正式 `project_display_name`，Codex 没有正式项目名时优先会话名（非空人工 `alias` → `session_title`），其他平台保留原项目名优先；均无名称时可用可读目录兜底，最后显示“未命名会话”。明确未知占位与内部 `.chatgpt-projects/g-p-<32位hex>` 标识不作为目录名称兜底。原生名称完整显示并按面板宽度自然换行，不另设 10/50 字符显示上限，不提供鼠标悬停浮层；接口与来源安全长度约束不变。

- `counts` 基于筛选后、截断前的可见会话计算；`role=main` 时仍只计算主会话自身，不把子 Agent 状态改写成主会话状态。最多返回最优先的 160 张卡片，`truncated=true` 时提示缩小范围。主卡片按自身与子摘要中最高的状态优先级排序（等待、错误、执行、未知、空闲、结束），子 Agent 的等待/错误会把根卡片提前，之后才截断。`unknown_roles` 是全局今日可见且关系未知的计数，不代表当前工具/项目内的数量。
- 主会话卡片兼容新增 `children_summary`，结构为 `{"total":0,"active":0,"waiting":0,"error":0,"unknown":0}`，统计其全部明确、可见的子 Agent 后代，不受今日、160 张卡片或主卡片工具/项目筛选限制。包括孙辈，使用下述 children 接口的相同关系及状态规则；没有子 Agent 时各项为 0。旧后端可能缺少该字段，前端应容忍。主会话自身状态不被子 Agent 覆盖，摘要用于保留子 Agent 的待处理提示。
- 顶层兼容新增 `children_counts`，字段与 `children_summary` 相同，聚合筛选后全部候选主会话的后代，包含未返回的第 161 张以后的主卡片。`counts` 的原有语义保持不变；仅主会话页面可以将二者的 `active` 或 `waiting` 相加，并明确标注包含子 Agent。`role=all` 等兼容查询已在 `counts` 中包含子会话，不能再次相加。旧后端缺少字段时显示旧计数，不从截断卡片推测完整子 Agent 数量。
- `counts.active` 计入未陈旧的 active：进程 online，或来源为 hook/event 且没有明确 offline。精确事件可确认工作，但不会把 `liveness=unknown` 改成 online；在线数量和执行数量因此可以不同。
- 桌面胶囊的执行数只使用显式 `role=main` 查询的 `counts.active`，不加 `children_counts.active`。待处理使用下述独立 `inbox_counts.pending`（人工操作与未读完成的根并集），不再把旧 `counts.waiting/error` 相加；`attention_counts` 继续只表示人工请求。旧计数与状态语义保留，供现有 Web 界面兼容；执行与人工待处理可以同时存在。
- `projects` 是已采集项目列表，不限当前筛选；`activity` 返回当前筛选内今日最近 24 条，字段含 `id`、`at`、`kind`、`label`、`tool`、`project_name`、`session_id`。
- `usage.today` 是北京时间当天已采集总量；`observed_total` 为看板已采集累计。`cumulative` 优先显示来源报告累计，可能高于 `observed_total`，两者不能互相替代。
- 仅当 `context_percent` 有效时绘制百分比；`context_used` 有值但分母未知时只展示 token 数。WorkBuddy 不提供上下文快照。
- `state_source` 包括本地记录 `record`、明确事件 `event`、Codex / Claude / WorkBuddy / Cursor 上报 `hook`、进程观测 `process`；演示可能为 `demo`，不能把它当真实来源。来源已使用 hooks 后，原始记录不覆盖其状态。
- Claude 的 `AskUserQuestion` / `ExitPlanMode` 在 `PreToolUse`、`PermissionRequest` 及明确带工具名的权限通知中归为 `waiting_user`。VS Code 等宿主可能稍后发出不带工具身份的 `permission_prompt`；仅在仍有已确认提问关联且当前状态未离开等待回复时保留原分类和原状态时间，不把通知当作新的提问证据。新工具、明确命令审批、回复结束或其他明确状态转换清除关联，真正的权限请求仍为 `waiting_approval`。关联只含工具名、工具 ID 和时间，支持看板重启，不保存问题或回答。没有工具 ID 的迟到请求及任意并行交互不能据此宣称全部解决。字段依据见 [Claude 官方 hooks](https://code.claude.com/docs/en/hooks#permissionrequest)。
- WorkBuddy 桌面沙箱批准通过 SDK 状态机日志补充，来源为 `event`。只接受明确的转换信号，`PERMISSION_REQUESTED` 对应待批准，`PERMISSION_RESOLVED` 对应继续执行，`TURN_COMPLETED` 对应本轮空闲；不能把工具返回或 `PHASE_IDLE` 当成整轮结束。提问工具的 hook 可细分为等待回复。已收到明确 hook/event 后，普通记录不能降级覆盖它；桌面会话有原生状态来源时，CLI 工作进程退出不会将整条会话改成运行结束。
- `coverage` 当前可能为 `observed`、`status_only`、`partial_baseline`、`counter_reset`、`partial_usage`、`partial_records`、`missing_time`、`missing_request_id`；新增值应有通用显示回退。`status_only` 表示 Codex 已上报状态，但尚未关联已解析的会话记录，不表示 token 为 0，也不证明会话可恢复。
- `working_directory` 是识别到的项目根目录或工作树路径，字符串或 null；本机用户主目录缩为 `~`。它不是每次命令执行的当前子目录。`worktree_name` 保持兼容，前端优先显示新字段，旧后端缺失时回退到工作树名或未知。
- `jump` 使用服务端给出的按钮文字、可用性和精度；真实模式可能为 `codex`、`codex_vscode`、`terminal`、`project`、`claude`、`workbuddy` 或 `none`。可选 `reason` 是禁用原因，字符串；前端容忍字段缺失。打开应用与定位会话是不同精度。
- Codex 跳转需要采集器已关联的非空、仍存在的本地 `.jsonl` 会话文件；只有 hook ID 或线程索引不启用跳转。缺失时 `available=false`、`kind=none`、`precision=null`。`POST /api/jump` 的默认和显式 Codex 入口均执行同一校验，并拒绝实时隐藏的会话及后代；打开前再次核验文件存在。校验只能确认本地记录存在，不能保证客户端恢复成功。

#### 独立人工待处理与最近完成（兼容新增）

`snapshot` 顶层新增：

```json
{
  "attention_counts": {"pending": 2, "waiting_user": 2, "waiting_approval": 1, "error": 0, "unknown": 1},
  "recent_completed": []
}
```

`attention_counts` 按筛选后全部候选明确主会话计算，在 160 张卡片截断之前完成。`pending` 是主会话自身或其明确、可见子树存在未解决、且未由用户核对后消除本地提醒的人工请求的**根主会话去重数**；同一个根有多个问题、多个子 Agent 请求仍只计 1。`waiting_user`、`waiting_approval`、`error`、`unknown` 也各自按根去重，分类可以重叠，不能相加得到 pending。异常只进入 error，不自动成为人工待处理。疑似审批、无法关联的通知、尚未回放完毕或损坏来源等只进入 unknown，不能冒充“已确认无人需要处理”。未知角色、未知关系链、归档和内部审核 Agent 不被收编到根；原有未知角色计数继续说明关系覆盖缺口。

每张卡片兼容新增：

```json
{
  "attention": {"pending": true, "waiting_user": 3, "waiting_approval": 0, "error": 0, "unknown": 0, "oldest_at": 1792029600, "dismiss_version": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "last_completed_at": null
}
```

主卡 `attention` 汇总该根及全部明确、可见后代；`children[]` 卡片只说明本卡。各数字分别为尚未解决且未手动消除本地提醒的提问项、人审请求、异常会话和覆盖缺口数量；`pending` 仅由明确人工提问或人审请求决定。`oldest_at` 是这些交互的最早来源时间，可能为 null；不以轮询时间刷新旧问题的年龄。旧后端缺少此结构或顶层聚合时，客户端显示待处理未知，不从旧 state、截断卡片或子摘要反推全量。

主卡 `attention.dismiss_version` 兼容新增，为 64 位小写十六进制 SHA-256 字符串或 null。它表示完整明确、可见根子树中尚未解决且未手动消除本地提醒的 `waiting_user` / `waiting_approval` 请求批次，包含不在截断列表内的明确后代；unknown 不参与批次，也不会因手动消除而清零。没有可消除的明确请求时为 null；旧后端缺少字段时，客户端不提供该操作，不自行生成版本。示例版本仅展示格式，实际请求须原样使用当前卡片返回值。

Codex 内置 `request_user_input` / `request_user_input_async` 及已知 `functions.`、`functions__` 命名空间从 `response_item/function_call` 的工具身份与 `questions` 数量建立逐项账本。仅保留会话／调用／来源 item 的关联身份、问题索引、类型和时间，不保存问题、选项、答案、参数或输出正文。未知第三方同名工具只提示覆盖缺口。异步调用与 PreToolUse 先记投递待确认（unknown）；只有同调用的结构化 `accepted=true` 才将已知问题索引升级为人工待答。该成功投递与 `PostToolUse` **不能清除未答问题**；该工具由宿主应用定义，尚无可靠取消／失败结果契约，不凭任意 error/isError/accepted 字段猜测清账，因此无匹配回复或明确清理证据时可能保守残留。同步工具需匹配结构化实际答案或明确工具结束。用户回复只接受单个 user text/input_text 的完整 `<send_user_message_question_reply>…</send_user_message_question_reply>` 包装，按其中 JSON `questionItemId` 的 `["request_user_input_async", 已知调用或来源item身份, 问题索引]` 严格同会话、同已知索引关联，逐项解决；普通消息、新一轮开始、别的工具返回和错误身份均不能清空问题。

`PermissionRequest` hook 发生在审核之前，也可能自动允许或拒绝，因此单独事件只表示 unknown，不作为人审待处理。只有能关联已知调用的真实 `permission_prompt` 通知等明确面向用户的证据才确认人审；无可靠身份关联仍为 unknown。WorkBuddy 原生 `PERMISSION_REQUESTED` 还可能覆盖问答或其它确认，当前只保留旧状态与覆盖提示，不据此捏造精确人工请求或清除其它异步问题。没有更强证据时不承诺自动审核与人工批准均已完整覆盖。

升级已有数据库时，采集器使用独立版本和文件偏移回放 Codex 交互／明确完成元数据，按原顺序处理历史请求和回复；不重置 usage 游标、不重算 token、不用历史记录覆盖当前 hook 执行状态。回放未完成、坏行、超大行或缺时间等保留 unknown。已解决身份跨重启与重复回放保留，不重新挂成待处理。原始来源始终只读。

`last_completed_at` 只由明确 `task_complete`、没有取消／中断／错误原因的 Stop，或 WorkBuddy `TURN_COMPLETED` 写入，为 epoch 秒或 null。普通 idle、工具返回、进程退出、SessionEnd、Interrupt、turn_aborted 和 TURN_CANCELLED 都不生成完成时间；该字段指已观察到本轮结束，不代表业务目标验收成功。

`recent_completed` 独立查询当前工具／项目／入口筛选内的可见明确主会话，取最近 24 小时且不晚于 now 的可靠、尚未确认看过的完成时间，按时间降序最多 5 张卡片，不受主列表 160 张截断影响。先排除已读、当前不是 idle/ended、仍有人工待处理或待处理覆盖未知的候选，再取 5；新一轮 unknown 不把旧轮完成冒充当前完成。每项复用完整卡片与 attention 结构。旧后端缺字段表示尚未取得这份独立列表，不从主列表拼凑。演示响应中的最近完成卡片同样禁用真实跳转。

#### 用户核对后消除本地人工提醒（兼容新增）

实际回复、批准或可靠清理信号继续按原链路解决人工请求。用户在人工待办主卡上右键一次即消除该批本地提醒，不再显示二次点击按钮；原生端调用 `POST /api/attention/dismiss`，body 为 `{"id":"codex:demo-design","version":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`。需要现有首页 cookie、JSON 与 CSRF；`version` 必须是当前主卡返回的非空 `dismiss_version`，不可用时间、标题或截断列表推算。普通左键跳转、轮询和打开详情均不触发此操作；完成卡右键一次使用独立的已读接口。

会话不存在、缺少 ID、已隐藏、child 或 unknown 角色返回 404；版本格式无效返回 400；请求批次已变化或已无该批请求返回 409，客户端重新读取后由用户决定是否再次核对。成功返回 `{"dismissed":true}`。操作只在独立 `attention_dismissals` 保存本地提醒所需的最少身份、`opened_at` 与 `dismissed_at`；不修改 `interaction_requests.resolved_at`，不表示原客户端问题已回答或审批已通过，也不改写来源记录、Agent 状态、信任或批准策略。

消除记录按请求身份与版本隔离：旧来源回放不会使已消除的本地提醒复活，后续新请求仍会提醒，旧卡片的迟到确认不能吞掉新请求。unknown 覆盖提示始终保留。成功后，当前快照仍为同一批次时原生端立即移除该根的人工提醒并更新计数，保留执行、未知和独立未读完成；批次不同则不清新提醒。随后丢弃确认前在途的旧 GET 并重新读取完整聚合。该 POST 与其他写操作一样不自动重试，超时或结果不明时不假装清除。演示模式仅写临时数据库。

#### 待查看完成收件箱与已读回执（兼容新增）

`snapshot` 新增 `inbox_counts:{"pending":3,"action_required":2,"unread_completed":1,"unknown":0}`。这些数值均按完整候选明确主会话根去重，在卡片截断之前计算；`action_required` 等于纯人工的 `attention_counts.pending`，`unread_completed` 是合格未读完成根数，`pending` 是两者根集合的并集，分类不能自行求和。错误不直接变成人工待处理；覆盖缺口继续进入 unknown。完整主会话候选还会保留昨天但仍在滚动 24 小时内的未读完成入口，不只查询北京时间今天。

每卡新增 `inbox:{"unread_completed":true,"completed_at":1792029600}`。`completed_at` 等于本卡可靠 `last_completed_at`，未取得时为 null；`unread_completed` 为必填布尔值，仅对可见明确 main、近 24 小时且不未来、当前 idle/ended、人工待操作与覆盖未知均为零、且本地回执版本较旧时为 true。子 Agent 不独立制造主会话完成提醒。缺少 inbox 或 inbox_counts 的旧后端表示没有取得这个独立口径，不能用旧 waiting/error 或截断卡片补造完整数。

首次启用不批量把旧完成改为已读：仅将已有可靠证据、当前合格且仍在近 24 小时范围内的完成列为未读，更早记录不提示。`recent_completed` 只包含合格未读根，过滤之后取最多 5；确认一条后，后面的合格条目会补位。普通 GET、轮询、显示详情、完成回放和进程退出都不会写已读回执。

客户端显式确认看过时调用 `POST /api/inbox/read`，body 为 `{"id":"codex:demo-complete","completed_at":"1792029600"}`。完成待查看卡右键一次即发送，不再显示二次点击按钮；成功后立即移除对应旧完成并减数，再读取完整聚合。需要现有首页 cookie、JSON 与 CSRF；`completed_at` 必须是有限、正数的数值字符串，不晚于服务器 now，也不得超过该可见明确主会话当前已知 last_completed_at。无会话或隐藏／非主会话为 404，无效版本为 400；合法结果为 `{"read_completed":true}`，未写入时为 false。仅在本地保存会话身份与已读到的完成时间，回执单调推进；旧卡片的完成版本不会把后来新完成预先销账。该操作不改变任何人工请求、Agent 状态、配置或原始来源。

跳转与已读采用两个显式步骤：`POST /api/jump` 保持原导航行为，**完全不写回执**，原 Web 不带新动作也不会自动销账。原生客户端只有收到 `opened=true`，且确认实际 kind 与指定会话／会话路由精度相符后，才另行 POST `/api/inbox/read` 确认卡片中的完成版本；跳转失败或结果不明时不发第二步。应用、项目和原终端标签页精度不证明已看过指定会话，不能自动清除；用户已在原客户端看完时可以显式选择“已看过”。第二步结果不明时不自动重放，可通过之后 GET 回读回执状态。演示中这份本地确认只写临时库，真实跳转继续禁止。

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

### `GET /api/heatmap?tool=all`

固定使用请求当时 **Asia/Shanghai 的当前自然年**，从 1 月 1 日到次年 1 月 1 日（结束不含）。此接口仅接受 `tool`（默认 `all`，表示合计）；其他筛选参数、`date` 和 `period` 不改变范围。它包含各角色以及已归档、实时隐藏会话的保留用量，与 `stats?role=all&period=year` 的输入 + 输出口径一致；会话卡片只显示主会话不会减少 token 合计。旧 `/api/stats` 及其日、周、月、年行为保持兼容。

| 字段 | 类型与含义 |
| --- | --- |
| `demo` | 布尔值，人工演示数据为 true |
| `year`、`date` | 北京时间当年数字、今天的 `YYYY-MM-DD` |
| `start`、`end`、`timezone` | 年初和次年年初日期、`Asia/Shanghai`；结束不含 |
| `tool` | 实际工具筛选值，默认 `all` |
| `tools` | 全部已发现会话的工具标识，去重排序；无用量或只有历史/子会话的工具也保留，不限当前筛选 |
| `totals` | 与 stats 相同的 `input`、`output`、`cache_read`、`cache_write`、`reasoning`、`total`；没有已采集值时为 null，缓存及推理不重复相加 |
| `has_usage` | 所选工具当年是否有用量记录；真实零值记录也为 true |
| `first_usage_day` | 全库最早用量日，不限年份与工具；无记录为 null |
| `partial_sessions` | 所选工具存在覆盖缺口的会话数，不限年份与角色 |
| `days` | 按日期升序完整 365/366 项，每项 `{"date":"2026-10-15","tokens":1500,"has_usage":true}` |

`days[].tokens` 为该日已采集 total 的合计，整数或 null。没有记录（包含未来日期）返回 `tokens=null, has_usage=false`；取得真实零值记录时返回 `tokens=0, has_usage=true`。没有记录不能描述成已确认零消耗，热力图需区分未知与已知零值。所有分桶均按北京时间。

### `GET /api/children?id=codex:demo-design`

`id` 必须是当前可见且 `role=main` 的主会话 ID；不存在、已隐藏、非主会话或缺少 ID 时返回 404。此接口不执行原应用跳转，也不读取对话正文。

响应为 `{"demo":true,"now":1792029600,"parent_id":"codex:demo-design","children":[],"counts":{"total":0,"active":0,"waiting":0,"error":0,"unknown":0},"truncated":false}`。

- `children[]` 复用 snapshot 卡片的完整安全元数据、用量、状态及 `jump` 结构，另加整数 `depth`：直系为 1、孙辈为 2，依此类推。先返回父节点再返回其后代；同级按最近活动降序，ID 作为稳定排序。演示中的所有 jump 均不可用。
- 只沿明确 `parent_id` 且 `role=child` 的关系遍历；不把 unknown 归为主会话或子 Agent，也不越过 unknown 节点收编后代。隐藏节点及其后代不返回，沿用归档、内部审核和临时记忆隐藏规则。遍历可安全处理错误循环。
- 包含符合上述关系的历史/离线后代，不受今日、主卡片 160 条上限、工具、项目、入口或角色筛选参数截断。当前完整返回，`truncated=false`；不能用主列表是否存在子卡片推断为空。
- `counts` 基于完整子列表：`total` 是全部后代数；`active`、`waiting`、`error`、`unknown` 与 snapshot 使用同一状态规则。新鲜 hook/event 即使进程未知也能确认执行，明确 offline 不计执行；超过 120 秒的 active 降为 unknown，旧等待保留状态和 `stale`。这些分类不一定相加等于 total，因为空闲和结束也包含在 total 中。

### `GET /api/integrations`

主要字段：

- `demo`：用于演示标识和禁用真实操作。
- `sources[]`：`tool`、`name`、`present`、`label`、`precision`；`present` 表示发现来源目录，不代表准确状态已验收。
- `claude`：`enabled`、`installed_events`、`complete`、`statusline_enabled`、`backup`、`error`；配置读取失败时可能没有 complete。配置完成不等于收到真实上报。
- `last_hook_at`：Claude 最近上报 epoch 秒或 null；`desktop_code_sessions`：已发现的桌面 Code 会话数。
- `lifecycles`：以 `codex`、`workbuddy`、`cursor` 为键。每项含 `enabled`（布尔）、`installed_events`（整数）、`complete`（布尔）、`review_required`（布尔）、`error`（文本或 null）、`last_hook_at`（epoch 秒或 null）。`review_required=true` 表示该平台要求客户端审阅信任，不是读取了当前信任状态；目前仅 Codex 为 true，Cursor 为 false。`complete` 仅表示所需配置齐全，不能推断已信任、每种事件已触发或所有入口都支持。
- `deferred`、`data_label`、`privacy`：尚未接入来源、数据位置展示文字和隐私说明；前端把位置当文本，不据此访问文件。

## 订阅与积分额度

### `GET /api/subscriptions`

响应为 `{"demo":false,"now":1792029600,"platforms":[...]}`。接口需要同源首页 cookie，没有查询参数；工具、项目、角色筛选不改变账号额度。每个 `platforms[]` 包含：

| 字段 | 类型与含义 |
| --- | --- |
| `tool`、`name` | 平台标识与名称；当前四家均返回，即使没有数据。Cursor 未显式导入官网页面快照时为 `status=unavailable, updated_at=null, limits=[]` |
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

Claude 当前仅采集来源报告的 `five_hour`、`seven_day` 订阅窗口；未报告时保持未知。API/第三方余额及网关 `spend_limit` 尚未接入，不能由会话 token 估算，也不能混为订阅额度。

“更新额度”调用已有 `POST /api/rescan`，仅表示已请求采集/后台查询；WorkBuddy 最短间隔 30 秒，常规每 300 秒查询，页面持续读取缓存。缺少 WorkBuddy 凭证不发请求，也不复用之前进程的积分缓存。凭证配置和官方链接见 README。

## 写入与操作接口

下列接口均为 POST，均需 cookie 和 CSRF。不要把读取页面变成自动执行写操作。

| 路径 | JSON body | 成功结果/行为 | 演示模式 |
| --- | --- | --- | --- |
| `/api/rescan` | `{}` | `{"requested":true}`，请求重新采集；不是采集完成保证 | 不采集真实来源；下一次 snapshot 请求刷新人工样例 |
| `/api/attention/dismiss` | `{"id":"codex:demo-design","version":"当前 attention.dismiss_version"}` | `{"dismissed":true}`，用户核对后仅消除当前批次的本地人工提醒，不替原客户端处理 | 仅写临时数据库 |
| `/api/inbox/read` | `{"id":"codex:demo-complete","completed_at":"1792029600"}` | `{"read_completed":true}`，仅确认指定本地完成版本；不解决人工请求 | 仅写临时数据库 |
| `/api/alias` | `{"id":"codex:demo-design","alias":"界面开发"}` | `{"saved":true}`；名称会规范空白、最长 80 字符；空字符串清除 | 仅写临时数据库 |
| `/api/jump` | `{"id":"codex:demo-design"}`，可选 `kind` | `{"opened":true,"kind":"codex","message":"…"}`；只允许服务端确定的入口 | 403 禁止 |
| `/api/claude/preview` | `{"statusline":false}` | 返回新增配置预览，无配置写入 | 403 禁止 |
| `/api/claude/enable` | `{"hash":"预览返回的hash","statusline":false}` | 备份后合并 Claude 配置 | 403 禁止 |
| `/api/claude/restore` | `{}` | 移除看板添加的配置，保留其他修改 | 403 禁止 |
| `/api/codex/preview`、`/api/workbuddy/preview` | `{}` | 返回新增状态配置预览，不写入配置 | 403 禁止 |
| `/api/codex/enable`、`/api/workbuddy/enable` | `{"hash":"预览返回的hash"}` | 备份后合并状态 hooks，不修改客户端信任与批准策略 | 403 禁止 |
| `/api/codex/restore`、`/api/workbuddy/restore` | `{}` | 仅移除 AgentWatch 状态 hooks，保留其他设置 | 403 禁止 |
| `/api/cursor/preview` | `{}` | 返回 Cursor 原生观察 hooks 的预览；不写配置 | 403 禁止 |
| `/api/cursor/enable` | `{"hash":"预览返回的hash"}` | 备份后合并用户级观察 hooks；不返回或修改批准决定 | 403 禁止 |
| `/api/cursor/restore` | `{}` | 仅移除自有 Cursor 命令，保留第三方 hooks 与后续修改 | 403 禁止 |
| `/api/cursor/subscription` | `{"captured_at":1792029600,"limits":[{"key":"cursor_models","used_percent":38,"period":"monthly","reset_on":null,"reset_label":null}]}`（人工示例） | 显式导入已核对的官网页面读数，返回 `recorded` 与 `captured_at`；不访问网站或读取凭据 | 403 禁止 |

jump 的 `kind` 通常省略，使用服务端默认；Codex 会话详情允许显式请求 `kind="codex"` 作为桌面备用入口。其他任意路径、命令或 URL 不被接受。

Claude preview 返回 `hash`、`events`、`add_events`、`include_statusline`、`has_statusline`、`command`、`settings_label`、`effect`、`privacy`。前端先展示预览，由用户操作后再 enable；`hash` 来自本次预览，`statusline` 与预览选择一致。服务端检测设置变化会返回 409，重新预览后处理。真实模式中的配置操作不属于纯前端开发验收。

enable 成功返回当前 Claude 配置状态，字段与 integrations 中的 `claude` 一致；restore 返回 `removed`（移除 hook 数）、`enabled` 和 `message`。配置保存成功仍需真实客户端事件验证。

Codex / WorkBuddy preview 返回 `hash`、`tool`、`events`（事件名数组）、`command`、`settings_label`、`effect`、`privacy`、`next_step`。enable 使用本次预览的 hash，返回对应 lifecycles 配置状态（不含 last_hook_at）；restore 返回 `removed`、`enabled`。预览后配置变化返回 409，需重新预览。状态传输仅包含允许的身份、时间、事件和进程元数据；启用后新开或继续真实客户端会话，检查上报时间，不能用保存成功代替验收。

### Cursor 首版覆盖

Cursor preview 兼容返回上述生命周期字段，另加 `configuration` 对象，仅包含本次新增定义：`{"version":1,"hooks":{"stop":[{"command":"观察桥接命令","timeout":3,"failClosed":false}]}}`。不返回用户已有第三方命令。用户级配置默认 `~/.cursor/hooks.json`，`--cursor-home` 可改变目录。仅在用户明确启用时写入；预览、启用、撤销沿用 cookie、CSRF 与防竞态规则。Cursor 配置为原生直接命令数组，不使用 Claude 的嵌套结构。

订阅 `sessionStart`、`sessionEnd`、`beforeSubmitPrompt`、`postToolUse`、`postToolUseFailure`、`stop`、`preCompact`、`afterAgentThought`。桥接不输出 `permission`、`continue`、`followup_message` 或注入内容；命令保持静默、正常退出，并显式设置 `failClosed=false`，不参与用户的批准决定。数据经本机私有传输采集后清空，不读取 Cursor transcript 或账号凭据。

卡片使用 `tool=cursor, role=main, surface=desktop, coverage=status_only, liveness=unknown`（明确关闭会话后为 offline）。主会话身份来自官方 composer `conversation_id`；显式子上下文（含 `parent_tool_call_id`）不收编为主会话。以 `generation_id` 关联轮次，重复、已关闭轮次或旧轮迟到事件不能激活或结束新一轮；缺少可靠轮次身份的工作事件忽略。`sessionStart`、`beforeSubmitPrompt` 仅表示接入或提交准备，状态为 unknown；思考完成、工具返回及压缩事件可确认 active，超过 120 秒无新执行证据仍降为待确认。工具失败表示 Agent 可继续处理，不推断整轮失败或人审。

仅 `stop.status=completed` 写入可靠 `last_completed_at` 并置 idle；`aborted` 为中断 idle，`error` 为 error，缺失／未知状态为 unknown，均不伪造完成。`sessionEnd` 关闭会话不生成完成证据。该时间仍只表示本轮结束，不能证明业务目标成功。

首版没有人工审批、问答与子 Agent 的完整覆盖，`attention_covered=0`，卡片与聚合保留 `attention.unknown`；不会把缺失交互当成已确认零待处理。现有完成收件箱要求人工覆盖已知，因此 Cursor 即使有可靠完成时间，也不制造未读完成提醒。token、上下文保持未知，年度图中无用量的 Cursor 日期为 null。订阅额度仅通过下述独立页面快照取得，不从状态事件推算。`jump.kind=cursor, precision=应用` 仅打开 Cursor，不承诺指定会话、不触发自动已读；演示中仍禁用真实跳转。

### Cursor 官网额度页面快照

`POST /api/cursor/subscription` 接受已登录官方页面的显式观察结果。它不是向 Cursor 的自动查询接口，不提取 cookie、token、Keychain 或客户端私有认证数据；普通 GET 与“更新额度”不会重新登录或刷新官网。接收字段严格为 `captured_at`（实际读取的 epoch 秒）与非空、最多 8 个 `limits`；每个窗口只允许 `key`、`used_percent`、`period`、可空 `reset_on` 和 `reset_label`。额外字段、非法/未来时间、越界数字、无效日期或自由文本拒绝且不覆盖旧快照。

`key` 仅允许 `cursor_models`、`other_models`、`grok_bot`、`total`，池名由服务端固定；`total` 不与前两个分池混用，Grok Bot 独立。`period` 仅 `monthly` 或 `weekly`，同池同周期不可重复；每次导入完整替换本次观察的 Cursor 窗口，缺池保持未知，不补零。旧读取时间拒绝，同刻相同内容幂等，不同内容拒绝。`used_percent` 为 0–100 的数值，剩余按页面显示百分比取补数；`display_precision=integer_percent` 提醒这只是网页显示精度，不能据此宣称精确耗尽。

输出仍兼容 `platforms[].limits[]`：`tool=cursor`、`bucket=额度池`、`key=周期`、`source=cursor_dashboard_ui`、`unit=percent`。周窗 `window_minutes=10080`，月窗为 null；不同池和周期不能相加。平台新增 `captured_at`，`updated_at` 与 `source_updated_at` 均为 null；窗口 `observed_at/captured_at` 仅表示实际页面读取时刻，不冒充官方更新时间。`notice`、Web 与原生描述明确为页面快照，不自动刷新。

`reset_on` 为日精度 ISO 日期，`reset_label` 仅允许与其一致的安全日期文字；标签包含完整年份时必须同时提供 `reset_on`，不能留下已知日期却没有截止。没有精确时刻时 `resets_at=null, reset_precision=day`。窗口 `valid_until` 是该日期在 UTC+14 最早可能开始的保守有效截止，`valid_until_basis=reset_date_earliest_utc_plus_14`，**不是确切重置时间**，不得把它格式化成“官方重置于几点”。超过读取 900 秒标 stale 并保留上次值；到保守截止为 expired，remaining/remaining_percent 为 null，等待重新读取，禁止补满。缺少重置日期不推断重置。来源权限或账号切换后的适用性仍须以官网为准。

## 错误和联调检查

失败使用非 2xx 状态，响应形如 `{"error":"请从看板首页重新打开"}`。前端先解析 JSON，再按 `response.ok` 判断，向用户展示 `error`，不依赖完整文案做逻辑分支。

| 状态码 | 常见原因 |
| --- | --- |
| 400 | body、参数或跳转入口无效，或应用无法打开 |
| 401 | 缺少/过期 cookie；访问同源首页重新初始化 |
| 403 | Host/Origin/CSRF 校验失败，或演示模式禁止真实操作 |
| 404 | 接口或会话不存在 |
| 409 | Agent 配置在预览后改变或不能安全合并；人工提醒批次已变化或已无该批请求 |
| 500 | 读取或操作未完成；页面保留已有数据并明确未更新 |

接口变更时同步本文、前端调用、人工演示数据和相关测试；记录字段类型、可空性、单位与兼容处理。不要用关掉校验、补零、猜状态或复制真实会话来解决联调问题。
