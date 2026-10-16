# 无痛胶囊实施交接

> 历史实施前方案：原生胶囊现已实现。协作者从 [当前交接](DESKTOP_CAPSULE_CURRENT_HANDOFF.md) 和 [协作指南](CONTRIBUTING.md) 开始，不执行下方旧的“从零实现”任务；本机绝对路径仅为原阶段记录。

更新时间：2026-10-15。仓库：`/Users/sylas/Desktop/AgentWatch`。

用户会自行新开会话实施。本轮只整理方案、交接和合成概念图，没有实现桌面程序、改采集逻辑、修改客户端配置或启动真实服务。

## 1 接续时先看什么

按顺序阅读 `AGENTS.md` → `docs/HANDOFF.md` → `docs/API.md` → [产品与交互方案](DESKTOP_CAPSULE_PLAN.md) → 本文，再查看相关源码。以本轮最新需求为准，保留已有改动。

本轮读取到的工作区起点：

- 分支 `main`，起始工作区干净。
- HEAD `3ea735c865a2322940de1d1197189a5b97571396`，提交信息是 `Merge pull request #1 from shuaishuaisylas/feat/subscription-balances`。
- 这只是本地 Git 核验，未重新查询远程 PR／CI；旧交接中 PR #1 未合并的说法属于历史快照。
- 本机 macOS `27.0.1`、arm64；能找到 Command Line Tools 的 Swift，`xcrun swift --version` 为 Apple Swift 6.4。未编译任何原生窗口，也没有据此验证 Xcode、SDK API 或打包链全部可用。

下一会话重新检查 HEAD、工作区与工具链，不能把这些记录当成永久状态。建议在 `codex/desktop-capsule` 这类小分支上实现；不要重新修改已合并 PR #1 来承载新功能。

## 2 最终需求不能退回旧稿

常驻层是无图标的单行 `3 执行 · 2 待处理`。不再显示 Agent／平台名称、token、额度百分比和第二行文字。图标设计不是当前实现依赖。

玻璃要混入窗口后面的实际内容，随壁纸或其他窗口变化，减少固定蓝灰填充。细边代表某个固定窗口的已用额度，完整闭环为已用 100%；它在任何执行状态都保留。确认执行时只让点亮额度段内部有微光，高光不能进入未使用区间，空闲时线长不变且停止动画。

产品原则是“不能影响正常工作”。正常更新、鼠标路过和待处理事件都不自动展开或激活应用。点击才展开，拖动和点击分开，主动暂隐可恢复。

最新图在 [最终概念图](design/desktop-capsule/concept-final.png)，详细状态与验收以方案正文为准。图片中的数据都是人工样例，图本身没有真正的窗口透明或执行动画。

## 3 推荐架构

```text
macOS 原生轻客户端
  NSPanel 胶囊与轻详情
  系统背景混合材质 + 额度描边 + 局部高光
  独立 APIClient 与状态映射
          |
          | 固定 loopback URL、首页 cookie、CSRF
          v
现有 agentwatch Python HTTP 服务
  /api/snapshot /api/subscriptions /api/children /api/jump
          |
          v
现有 collector / store / subscriptions / navigation
```

第一版增加独立原生客户端，不重写 `web/`，不把采集搬到 Swift，不另建能绕过 cookie／CSRF 的本地接口。当前接口已能支撑主要需求，默认不需要改 `docs/API.md` 的协议。

### 3.1 原生窗口与材质

推荐 AppKit 控制窗口、位置和事件；内部视图可用 AppKit 或按需嵌入 SwiftUI。优先选择能以现有 Command Line Tools 重复构建的最小工程，避免为几张视图新增大型框架。

- 用 `NSPanel`／非激活面板承载胶囊；常驻层不作为主窗口，不因轮询或通知调用应用激活、`makeKeyAndOrderFront` 一类抢焦点路径。
- 保持透明窗口底层，并使用 `NSVisualEffectView` 的 `behindWindow` 混合，让窗口后方桌面／应用内容参与材质。只改整个窗口 alpha 会连文字与额度线一起变淡，不能代替背景材质。
- 对 macOS 26+ 可评估 `NSGlassEffectView`；以实际 SDK 可用性和真实窗口效果验证为准，第一版可先用成熟的 `NSVisualEffectView` 保证透明与可读。旧系统采用兼容材质回退，最低支持版本由实施时明确记录。
- 非激活面板只是不激活所属应用的工具；仍要单独检查 key window、键盘事件和面板关闭后的输入归属。不要只根据 style mask 就声称“不抢焦点”。
- 窗口矩形中胶囊外的透明角落不应成为无形点击遮挡区。为实际胶囊做命中区域，验证边角点击可穿过；不要直接将整个窗口设成忽略鼠标，导致拖动和展开不可用。
- 轻详情用用户点击打开的辅助面板；首版避免内嵌输入框。若已有组件必须成为 key 才能操作，把该行为限制在用户明确进入设置的路径。
- 可先用普通文字菜单栏项作为暂隐恢复与退出入口。常驻桌面胶囊仍保持无图标。

原生透明不依赖抓取屏幕像素。不要为了背景取色而请求屏幕录制、辅助功能或读取其他软件私有数据。

### 3.2 建议的新文件范围

以下是建议结构，尚不存在，可在实施时按实际工程调整：

```text
desktop/
  Package.swift 或最小原生工程文件
  Sources/AgentWatchCapsule/
    AppDelegate.swift
    CapsulePanel.swift
    CapsuleView.swift
    DetailsPanel.swift
    APIClient.swift
    Models.swift
    CapsuleState.swift
    PositionStore.swift
  Tests/AgentWatchCapsuleTests/
  Resources/Info.plist
  scripts/build.sh
  scripts/run-demo.sh
  README.md
```

这些名称只是模块边界，不要求机械地按文件数实现。新增原生程序不能改变原 Python／Web 的无 npm 构建约定，也不必更换仓库框架和许可证。

## 4 本地 API 接入

### 4.1 认证启动顺序

依据 `agentwatch/server.py` 的 `trusted_host`、`trust`、`authorized` 和首页响应：

1. 为本次连接固定 `http://127.0.0.1:<port>`，不在请求中来回换 `localhost`、主机或端口。
2. 先 GET `/`，从响应接受 `agentwatch` cookie，并从本项目首页 `<meta name="csrf">` 读取 CSRF 值。缺少预期字段视为未连接，不能生成假 token。
3. 所有 API 请求复用本连接的 cookie。POST 另带 `Content-Type: application/json`、`X-AgentWatch-CSRF` 和 JSON 对象；无参数 body 为 `{}`。
4. 当前服务仅在 Origin 存在时要求它等于 `http://` 加请求 Host。原生 URLSession 可不发送 Origin，或使用完全匹配的值；不要伪装其他浏览器来源或放宽服务校验。
5. 禁止请求重定向到其他主机、协议或端口。GET 遇到 401 可以重新 bootstrap 后重试一次；服务重启后的 cookie 与 CSRF 都需要重取。
6. POST 网络超时或结果不明确时不自动重试。重新登录、切环境或刷新界面不能暗中重复跳转、刷新采集或修改配置。

**每个 base URL 要有独立的内存会话。** Cookie 本身不按端口隔离，真实 8766 与演示 8767 都使用 `agentwatch; Path=/`。不要共用浏览器、系统共享 cookie 罐或写入持久 cookie 文件。可采用独立的临时 URLSession，关闭自动共享 cookie 处理，在 APIClient 内按完整 origin 保存并附加本次 cookie；具体实现必须用双端口测试验证。

切换服务地址时，清空旧 CSRF、cookie、快照与在途请求代次。禁止迟到的真实响应覆盖演示页面，或反过来。日志只记录路径、状态码和概括错误，不记录 cookie、token、问题、回复或工具正文。

HTTP 本机访问与 Info.plist 设置按目标 SDK 实测；需要本地网络例外时限定范围，不启用全局任意网络放行来解决联调。

### 4.2 调用与本地状态

| 接口 | 使用方式 |
| --- | --- |
| `GET /api/snapshot?tool=all&project=all&role=main` | 获取主会话和完整聚合；常态独立轮询，使用顶层 `counts` 与 `children_counts` |
| `GET /api/subscriptions` | 独立账号额度；与 snapshot 成败分别处理，不按会话筛选重新解释 |
| `GET /api/children?id=…` | 用户查看某主会话时请求其明确子树，不从主列表拼子树 |
| `POST /api/jump` | 用户明确点跳转时发送 `{id}`；复用后端导航，不自己拼会话链接或命令 |
| `POST /api/rescan` | 仅在用户明确点击刷新时调用；表示已请求，不表示已经采集完成 |
| `GET /api/integrations` | 如需来源说明可读取；真实增强设置仍留在 Web 中 |

建议状态与订阅各 4 秒读取一次，允许两个不同接口并行，但同一个接口不积压在途请求。面板展开时才读取子树；用请求代次／取消机制拒绝迟到结果。

默认请求超时 6 秒，并以单调时钟维护最后成功状态响应时间。即使网络请求挂住，距最后成功响应超过约 15 秒也要进入“状态待更新”并停流光。超时与新鲜期均为可调整的默认提案；休眠恢复后先刷新，不能因客户端还在运行就视状态为新鲜。订阅来源的 `observed_at / status / resets_at` 另行判断，不能用这次 GET 成功时间代替来源时间。

执行与待处理公式见方案第 4 节。缺少 `children_counts` 不从截断卡片回推。当前 `web/app.js` 的 `renderSnapshot` 存在从 `children_summary` 汇总的兼容分支；原生客户端应遵循 API 文档的全量计数约定，不照搬这个分支，也不顺手在本任务改写它。

面板逐条区分已加载的待回复、待批准与异常；现有全量聚合只有等待／错误等合计，不提供完整待回复／待批准分类数。主列表截断与子树未加载要明确提示；如以后确需全量分类计数，再增加兼容 API 字段与测试，不能从可见行数推断。

额度来源身份记录 `tool + bucket + key`。先检查平台与窗口 `status`、来源时间和重置时间，再使用数值。特别注意 `agentwatch/subscriptions.py` 中 `expired` 清空了 `remaining / remaining_percent`，但旧 `used` 仍可能存在。不要忽略状态直接除以分母。

状态读取失败时立即停止执行流光，并按方案显示未更新状态；额度接口失败只影响额度部分。保留最后成功数据供用户主动查看，不能用错误结果把任何一端补成 0。

## 5 服务与进程生命周期

- 真实服务默认端口是 8766，演示通常 8767；以实际启动输出为准。先检查目标地址与本服务的响应，不能盲目另启同端口进程或终止占用者。
- 先完成原生客户端连接已运行服务的路径，再整理一键启动。如果后续启动器代为启动 Python，只记录和停止自己创建的子进程；复用既有服务时退出挂件不能关闭该服务。
- 演示启动建议由测试启动器显式执行 `python3 -m agentwatch --demo --port 8767`，不要加真实来源目录，不启用真实 hooks，不打开真实 Agent。
- 演示启动器只能连接自己成功启动的演示实例；端口占用时报告冲突或改用新端口，不能静默复用占用者。自动测试可用 `--demo --port 0` 并读取打印出的实际地址，连接后校验 `demo=true`；绑定失败或模式不符就停止这条演示路径。
- 将 `demo=true` 标为演示模式。主画面至少用临时的 `演示 · 3 执行 · 2 待处理` 或同等可见标记区别人工数据；此为开发模式例外，不把演示标记做成真实常态品牌文字。
- 真实模式不通过挂件自动改信任、批准策略、OAuth 或客户端配置。继续保留原 Web 启动器。
- 构建产物、运行日志、个人偏好和缓存不提交。偏好只需要屏幕位置、是否暂隐和选定额度身份，不存会话正文或凭据。

## 6 实施和测试顺序

1. 检查并保留工作区改动，建立适合的新分支，核对当前 API 与 Swift SDK。
2. 做原生透明与非激活窗口小验证，先用人工数据。必须真正拖过壁纸与其他窗口、在输入过程中观察焦点，不能用网页透明 CSS 或概念图代替。
3. 完成可测试的纯数据状态映射：完整计数、异常短句、独立接口状态、额度有效／旧值／重置／未知、已用 0%／100%。
4. 用演示 Python 服务接 API，验证首页 bootstrap、cookie／CSRF、服务重启、双端口隔离与迟到响应。
5. 完成拖动吸附、点击详情、稳定额度选择、暂隐恢复、多屏与全屏；跳转只通过既有后端。
6. 检查辅助功能、微光节奏、静置资源占用和既有 Web 页面；记录实际通过与未通过项目。

至少运行仓库约定的：

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
git diff --check
```

原生客户端增加适合所选工程的构建与测试命令，写入 `desktop/README.md`；现在不能假定尚不存在的 `swift test` 已经可执行。测试重点应是状态映射、cookie 隔离、过期额度、请求代次和动作不重复执行，而不是只验证视图创建。

桌面验证完整清单在产品方案第 6 节。最终交付记录必须区分：自动检查、人工演示桌面验收、真实客户端 hooks 与原应用跳转。后两者不能互相代替。

## 7 当前证据和剩余工作

本轮已核对本地 Git、源码中的 API 校验和工具链版本，并保存最终人工概念图。没有运行桌面程序、申请系统权限、抓取真实桌面、改变后端接口或完成真实透明／焦点／动画验收。

本轮文档交付的检查结果写在本节下方；下一会话改代码后要按新 HEAD 重新验证，不沿用文档阶段的成绩。

- `python3 -m unittest discover -s tests -v`：129 项通过。第一次在默认沙箱内有 1 项 HTTP 测试因临时端口绑定被拒绝而报错；允许隔离测试绑定本机临时端口后完整重跑，通过 129 项。
- `node --check web/app.js`：通过。
- `git diff --check`：通过；新增文档另做本地链接与空白检查。
- 没有执行原生构建或桌面验收，现有回归通过不代表透明材质、焦点、全屏、动画和应用跳转已实现。

## 8 新会话可直接使用的任务

> 在 `/Users/sylas/Desktop/AgentWatch` 实现 macOS 桌面无痛胶囊。按顺序阅读 `AGENTS.md`、`docs/HANDOFF.md`、`docs/API.md`、`docs/DESKTOP_CAPSULE_PLAN.md` 和 `docs/DESKTOP_CAPSULE_HANDOFF.md`。检查并保留工作区改动，使用合适的新分支。按已确认设计实施：单行无图标，只显示执行与待处理数，真实透明并随窗口后面的内容变化，额度仅由边缘线常驻表示，执行时高光只在已用额度段内流动。点击才展开，保护当前焦点。复用现有 Python API 与安全校验，先做人工演示，再做真实桌面验收；不要重写采集、修改客户端信任或批准策略。先交付能运行、能验证的原生客户端、启动说明和测试记录。遇到文档中的默认提案可按实际实现做合理取舍并记录，不要重新询问用户已经确认的方向。

## 9 官方技术依据

- [NSVisualEffectView](https://developer.apple.com/documentation/appkit/nsvisualeffectview)
- [behindWindow](https://developer.apple.com/documentation/appkit/nsvisualeffectview/blendingmode-swift.enum/behindwindow)
- [nonactivatingPanel](https://developer.apple.com/documentation/appkit/nswindow/stylemask-swift.struct/nonactivatingpanel)
- [NSGlassEffectView](https://developer.apple.com/documentation/appkit/nsglasseffectview)，使用前核对本机 SDK 与目标系统可用性
- [Apple 材质指南](https://developer.apple.com/design/human-interface-guidelines/materials)
