# 协作开发

AgentWatch 的代码可以共同开发，真实会话、账号和配置留在各自的 Mac。开发界面与接口默认使用人工演示数据，不需要维护者的账号或数据库。

## 先取得正确版本

先读 [AGENTS.md](../AGENTS.md)、[最新交接](HANDOFF.md)、[API 约定](API.md)，再查看相关源码。涉及胶囊时，另读 [当前桌面交接](DESKTOP_CAPSULE_CURRENT_HANDOFF.md) 与 [桌面运行说明](../desktop/README.md)。交接保留了历史记录，以顶部最新状态为准。

共同开发以 `main` 为基线。本批胶囊与 Cursor 功能通过 [PR #2](https://github.com/shuaishuaisylas/AgentWatch/pull/2) 合入，合并记录与状态以该 PR 为准；不要依据旧交接中的功能分支或提交号开工。新克隆默认取得 `main`，每项任务再从 `origin/main` 创建独立分支：

```sh
git clone https://github.com/shuaishuaisylas/AgentWatch.git
cd AgentWatch
git switch -c codex/your-change origin/main
```

最后一行换成自己的变更分支名。已有工作区先检查 `git status`、当前分支与 remote，再 fetch 并保留改动；不要重置、清理或覆盖其他人的工作。直接向原仓库推送前，由仓库 owner 邀请协作者并赋予 Write 权限；无写权限时可使用 fork 提交 PR。本文不代表邀请、权限或分支保护已经配置，不需要交换 Agent 平台账号。

## 准备与启动

| 开发范围 | 环境 |
| --- | --- |
| Python 后端 / Web | Python 3.9+；只用标准库，无需安装第三方 Python 包 |
| JavaScript 检查 | Node.js，只运行语法检查；Web 无 npm 依赖和构建步骤 |
| macOS 胶囊 | 产品目标 macOS 13+；构建需 Swift 5.9+ 的 Xcode / Command Line Tools。单测另需工具链提供 Swift Testing，以测试脚本检测结果为准 |

在仓库根目录启动 Web 演示：

```sh
python3 -m agentwatch --demo --port 8767 --open
```

页面通过这个 Python 服务访问，使用同源 cookie 与 CSRF。不要直接打开 HTML、另起前端服务器或放宽安全校验。修改 `web/` 后刷新；修改 Python 后重启自己的演示服务。端口占用就换空闲端口，不停止未知进程。

胶囊开发使用独立人工演示：

```sh
./desktop/scripts/run-demo.sh
```

它会创建自己的临时服务。启动、构建或切换环境前，先从菜单栏退出已运行的胶囊；不要覆盖运行中的可执行文件。真实服务与演示服务分开，退出胶囊不等于关闭真实服务。构建产物留在忽略的 `desktop/.build/`，不提交 `.app`。

## 分工与接口

| 范围 | 主要文件与责任 |
| --- | --- |
| 后端 | `agentwatch/`：发现与采集、存储、HTTP、安全、平台配置、额度及跳转 |
| Web | `web/`：原生 HTML/CSS/JavaScript，看板、筛选、图表与设置界面 |
| 胶囊 | `desktop/Sources/`：AppKit 窗口、状态映射、额度、传输与交互；复用同一 Python API |
| 验证与约定 | `tests/`、`desktop/Tests/`、`desktop/QA/`、`docs/API.md` |

开工时说明本次负责范围。前端需要新字段时，先说明名称、类型、可空性、单位及兼容方式；接口改变同时更新 API 文档、调用方、人工样例与测试。优先增加兼容字段，不顺手改写其他平台的采集、配置或导航。

保留这些语义：未知值不是零，在线不是执行，未知角色不归为主会话，缓存/推理分项不重复计入总量，跳转遵循服务端提供的精度。胶囊执行只计明确主会话，待处理使用独立根会话并集；仅正数显示执行／待处理文字，无任务时文字留空，胶囊与额度描边仍保持可见、可点击，方便调试。手动暂隐仍优先，未知数据不补零。详细规则以 [API](API.md) 为准。

## 每台机器独立接入

演示不接真实来源目录、不启用真实 hooks、不打开真实 Agent；别名和本地提醒操作只写临时库。

真实客户端验收由各自在自己的 Mac 上按需启用：先看新增配置预览，再备份、合并，保留撤销路径。不要复制维护者的 hooks、账号设置或备份。Codex 的信任由用户在客户端亲自审阅；不能自动写信任记录、改变批准策略或代替用户回答/批准。

不提交或共享真实数据库、会话、提示词、回复、工具参数/结果、凭据、配置备份和个人截图。新增 fixture 必须人工构造；排查报告只保留复现条件与必要的脱敏结论。

订阅额度与会话 token 独立。Cursor 额度是用户核对官网后显式保存的页面快照，保留池、周期、读取时间与显示精度；不自动刷新，不代表实时余额，也不从 token 推算。其它平台同样保留各自来源、更新时间、旧值和重置后未知状态。

## 提交前最小验收

在仓库根目录运行相关检查：

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
git diff --check
```

涉及胶囊时再运行：

```sh
./desktop/scripts/test.sh
./desktop/scripts/test-transport.sh
./desktop/scripts/test-controller-read.sh
./desktop/scripts/test-lifecycle.sh --build-only
```

工具不可用或环境阻止执行时，写明实际限制。`--build-only` 仅证明编译，不能记为窗口验收；退出已有胶囊后，用 `./desktop/scripts/test-lifecycle.sh --run` 执行人工窗口检查。更多入口见 [桌面说明](../desktop/README.md)。

CI 中的 Python、JavaScript 与 Swift 核心/控制器检查用于代码回归；原生窗口、透明材质、焦点及真实客户端行为仍需在自己的 Mac 验收，不能用自动检查替代。

- Web 变更：用演示检查工具筛选、空态、主会话/子 Agent 详情、年度热力图、接入禁用与窄屏。
- 胶囊变更：检查正数执行/待处理文字、无任务时文字留空但胶囊与额度仍可见可点击、手动暂隐优先、不抢焦点，以及受影响的点击、已读、右键与额度行为。
- 来源、hooks 或跳转变更：人工回归后，按实际客户端版本与入口单独验收，记录未覆盖项。配置保存、收到一次事件、编译或 CI 通过都不等于完整真实验收。参考 [客户端验收](CLIENT_ACCEPTANCE.md) 与 [桌面验收记录](../desktop/ACCEPTANCE.md)。

## 用小分支与 PR 交付

一项变更一个从最新 `origin/main` 创建的 `codex/<task>` 分支，避免多人直接推同一分支，不直接向 `main` 提交或推送。依赖尚未合并功能时，先与维护者确认集成分支与 PR 基线；之后以实际合并状态更新，不根据旧交接猜测。

按 [PR 模板](../.github/pull_request_template.md) 写清问题、改后行为、涉及范围、实际验证与剩余限制。只提交本次负责的改动，审阅暂存内容，确认没有本机数据；不要强制覆盖他人分支或擅自合并 PR。许可证尚未指定，不自行添加许可证或更换框架。

## 可直接交给开发 Agent 的开工提示

复制下面内容，并补上本次任务。使用协作者自己机器上的 checkout，不复制维护者的本机路径、账号或数据。

```text
请在我自己的 AgentWatch checkout 中完成下面的任务。
目标仓库：https://github.com/shuaishuaisylas/AgentWatch
共同开发基线：origin/main。本批功能的合并记录见 https://github.com/shuaishuaisylas/AgentWatch/pull/2；开工时核对远程最新状态。
本次任务：[由用户填写具体目标、负责范围和验收要求]

以用户当前任务为准。先只读检查 git status --short、git branch --show-current、git remote -v，确认 remote 对应目标仓库后再 git fetch origin。
有本地改动或未跟踪文件，先记录范围并报告，保留它们，不覆盖、不自动 stash、不混入本次提交。不盲目 checkout，不运行 reset --hard 或 clean。需要切换时，只有确认不会影响已有工作才继续；否则先做可独立完成的只读检查并说明阻碍。
从已 fetch 的 origin/main 创建自己的 codex/任务名分支。例如 git switch -c codex/your-task origin/main；示例分支名须换成本次任务名，已存在时先核对用途。不要误用 sylas 等他人现有分支，不直接向 main 或共享分支提交、推送。

按顺序读 AGENTS.md、docs/HANDOFF.md 顶部最新状态、docs/API.md；桌面任务再读 docs/DESKTOP_CAPSULE_CURRENT_HANDOFF.md、desktop/README.md，验收记录看末尾。不要按历史“未实现”记录重做现有功能。
Web/接口开发先用 python3 -m agentwatch --demo --port 8767 --open；胶囊使用 ./desktop/scripts/run-demo.sh。演示只用人工临时来源，不接真实目录，不改真实 hooks，不打开真实 Agent。页面通过 Python 同源服务访问，保留 cookie/CSRF，不另起前端服务绕过校验。
按约定分工修改 backend、web 或 desktop；接口变化同步 docs/API.md、调用端、人工样例与测试。未知不补零，进程在线不当执行，角色未知不当主会话；胶囊执行与待处理保持独立，仅显示正数文字，无任务时文字留空但胶囊与额度保持可见可点击，手动暂隐仍优先。跳转服从精度。Cursor 官网额度只是显式读取的页面快照，不是自动实时余额。

运行相关 Python 全套、node --check web/app.js 与 git diff --check；胶囊变更再运行 desktop/scripts/test.sh、test-transport.sh、test-controller-read.sh 和 test-lifecycle.sh --build-only。窗口行为另用人工演示验收；--build-only 不能写成窗口通过。退出胶囊后才运行窗口检查或构建，不停止未知进程。
记录工具缺失、未执行项目与未知边界。真实 hooks、信任、回复/批准及跳转按客户端版本和入口分别验收；不代替用户操作，不更改批准策略。不得提交真实会话、数据库、凭据、配置备份或个人截图。
只提交本次任务文件，使用小 PR；推送前确认 Write 权限或采用 fork PR，不擅自合并。完成后按下列格式报告实际结果。
```

完成报告格式：

```text
分支 / 基线：实际任务分支、起始分支或提交。
提交：实际 commit SHA；未提交则明确写“未提交”。
改动：解决的问题、最终行为与修改范围。
验证：实际执行的命令及结果；演示、原生窗口和真实客户端分别说明。
未验证 / 限制：缺失工具、未覆盖入口、未知数据及待确认事项。
PR / CI：实际 PR 链接或“未创建”；CI 只报告对应提交的真实结果。
保留的本地改动：开工前已有改动是否保留，以及交接时尚未完成的工作。
```
