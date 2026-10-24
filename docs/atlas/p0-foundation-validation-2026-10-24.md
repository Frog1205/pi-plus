# Pi Plus P0 底座验证报告

日期：2026-10-24（America/Los_Angeles）
基线：`f321e5bdd26b3624bfb2ab182a334d0a5edb698a`，附本地验证套件与一处 Windows 检查脚本修复。
结论：P0 重点验证完成，可以继续受控 P1；当前原生工具权限与进程隔离不满足无人值守个人助理的要求。

## 1. 结果摘要

最终选取的 **109 个不同测试断言全部通过**，完整 `npm run check` 退出码为 0。其中包含刻意复现越权和重复副作用的反例；断言通过表示行为已验证，不表示这些安全目标已经满足。

| 关注点 | 实测结论 | 接入要求 |
| --- | --- | --- |
| 工具权限 | 工具 allowlist 和执行前钩子有效；cwd 不是文件沙箱，扩展也不受工具名单约束 | 显式资源加载、宿主工具网关、可靠文件访问边界；不加载不可信可执行扩展 |
| 中断恢复 | SDK 可重开会话历史，但不自动恢复遗留任务；durable 可恢复已持久化任务 | 后台恢复优先选固定版本 durable，通过独立 Executor 适配层接入 |
| 重复执行 | durable 默认 unsafe 避免盲重放；误标 safe 能造成两次副作用 | 工具分级、稳定 operation_id、操作账本与对账；不能笼统承诺 exactly-once |
| 请求去重 | 同一会话中，相同 requestId 的并发提交和重开后提交会复用原记录 | 入口额外校验身份、项目与规范化请求摘要；同 ID 不同内容应返回冲突 |
| 外部进程 | 正常关闭能中止测试 Bash 进程树；强杀 Worker 后，刻意 detached 的子进程仍可写入 | 需要宿主管理进程归属和操作系统执行隔离；恢复机制不能替代进程约束 |
| 成功判定 | durable 的 submission 可为 done，同时包含工具错误 | Atlas 需要自己的业务完成条件，不能只检查引擎空闲或 done |

本轮没有接入现有 Atlas 工作台，也没有实现正式的工具网关、任务入口或操作账本。新增代码是可重复运行的验证套件与测试夹具。

## 2. 实验环境与方法

- Windows 原生执行；Node.js `v24.14.0`、npm `11.9.0`、Git `2.53.0.windows.1`。
- 按原 lockfile 执行 `npm ci --ignore-scripts --no-audit --no-fund`，安装 300 个包；package.json、package-lock.json、coding-agent install-lock 均未更改。
- 模型目录取自仓库 `nix/model-catalog.json` 固定 revision：`sha256-7fe1b41feaf43a7aad4bc832e6b306c7ced56e6d41bbc724b74e91eeacff6edf`；下载后验证 SHA-256，再使用上游 hydrate 工具生成被 Git 忽略的数据，模型数据校验通过。
- 模型请求只用 faux provider；没有调用真实模型、使用真实供应商凭据或消耗付费模型额度。
- 测试进程仅继承运行必需的环境白名单，使用独立 HOME、USERPROFILE、临时目录和 Git 配置；未改变当前用户的登录态或全局设置。
- 所有越权与写入反例均使用临时目录中的合成文件。“项目外文件”仍属于本次测试目录，没有读取真实私人文件。
- 崩溃实验在确定的阶段屏障收到后，对真实 Node 子进程执行 SIGKILL，再以另一进程重开会话文件或 SQLite。没有使用模拟 close 代替所有崩溃实验。
- durable 恢复实验重新打开数据库，并以原 requestId 提交/等待。生产宿主仍应明确调用 resume 或安排调度，不能假定仅导入模块就会恢复任务。
- 当前是源码运行验证；durable 测试配置合并仓库 source aliases，子进程预加载上游 source-resolver，避免落到已安装的其他 Pi 版本。

## 3. 工具权限实测

### 原生边界的反例

1. built-in read 能读取 cwd 外的绝对路径文件。
2. built-in write 能经 `../` 写入 cwd 外的临时兄弟目录。
3. built-in read 会跟随 junction 读取项目外文件。
4. 即使工具名单为空，已加载扩展仍可以在初始化时直接调用 Node 文件 API 写文件。
5. 即使 preflight 的 realpath 检查通过，检查后再把 junction 改指向外部，工具仍可读到外部内容。这是“检查与使用之间路径变化”的反例。

### 宿主控制点的正例

- 明确注入空资源加载器和空工具名单的 SDK 会话，没有导入项目 AGENTS 标记，并完成了 faux 请求。
- 不在 allowlist 的工具，即使被模型点名调用和尝试动态激活，实际执行次数仍为 0。
- 静态路径 preflight 对中文合法文件放行，对路径穿越、绝对外部路径、相似目录名前缀和 junction 外部路径阻断；被阻断时读取实现调用次数为 0。
- durable 的 beforeTool 宿主钩子也可阻断工具，实际执行和副作用次数均为 0。

这些正例证明控制点可用。静态路径钩子只是验证原型，不能解决上面的路径竞争反例，也不能约束扩展绕过工具系统直接执行代码。

## 4. 中断与恢复实测

| 引擎与故障窗口 | 重开后的观察 |
| --- | --- |
| SDK：进入工具后、写入前 | 历史包含助手工具调用，但没有工具结果；副作用 0；会话不处于 streaming，也未自动执行遗留调用 |
| SDK：写入后、结果记录前 | 副作用 1；历史缺工具结果；不自动重放，也没有自动任务对账 |
| SDK：工具结果记录后、最终回答前 | 副作用和工具结果各 1；不自动继续完成最终回答 |
| durable：模型生成中 | 重新打开并提交/等待原请求后继续，输入只有 1 条，副作用 1 |
| durable：unsafe 工具进入后、写入前 | 不重放；副作用 0，记录 1 个工具错误，需要上层处理未完成动作 |
| durable：unsafe 写入后、结果提交前 | 不重放；副作用 1，记录“可能部分执行”的工具错误 |
| durable：工具结果已提交 | 已完成工具不再执行，副作用仍为 1，继续完成任务 |

SDK 的这三个恢复实验先完成一轮 warmup，确保会话已落盘；不将其推广为首次响应任意时刻都不会丢失。

unsafe 的保护是保守停止，不是保证业务已完成。即便实际上还没写入，工具一旦进入不确定执行窗口，也可能需要查询结果或人工核实。

正常关闭与强制终止也不同：测试 Bash 的延迟写入在 Harness.close 后没有发生；工具刻意以 detached 子进程启动时，强杀 Worker 后子进程仍写出标记，重开 durable 只报告工具中断。这一反例针对测试中的 detached 启动方式，不声称所有附属进程都会存活。

## 5. 去重与重复副作用实测

| 场景 | 工具进入次数 | 副作用次数 | 结论 |
| --- | --- | --- | --- |
| SDK 相同 prompt 调用两次 | 2 | 2 | SDK 不提供宿主业务请求去重 |
| durable 同 requestId 并发提交三次、完成后重开再提交 | 1 | 1 | 在同一会话与单所有者场景下复用同一 submission |
| durable unsafe：写后回执前被杀 | 1 | 1 | 不盲重放，但结果不确定 |
| durable safe：非幂等工具写后被杀 | 2 | 2 | safe 是工具作者声明，不会自动实现幂等 |
| durable safe：用独占创建文件实现幂等的夹具 | 2 | 1 | 这一个受控文件操作可承受重放；不能推广为任意外部工具恰好执行一次 |

同一个 requestId 改成另一份 content 时，durable 仍返回原 submission。该行为可以去重，但不能替代请求内容一致性校验。入口应把 requestId 与可信身份、项目和请求摘要绑定，防止误复用。

## 6. 验证记录与复现

| 测试组 | 最终通过数 | 采用的证据批次 |
| --- | --- | --- |
| 上游 coding-agent 权限、事件、RPC 定向回归 | 23 | run-OSKoXT/coding.json |
| 上游 durable 恢复、提交、取消、存储失败与只读回归 | 59 | run-OSKoXT/durable.json |
| 新增 SDK/权限/真实进程中断验证 | 17 | run-GicnjE/coding-p0.json |
| 新增 durable 真实进程、权限、去重与子进程验证 | 10 | run-MjRBJ0/durable-p0.json |
| 合计 | **109** | 不重复计算多次复验的同一测试 |

最终完整检查证据：`.artifacts/atlas-p0/run-MjRBJ0/check.log`，退出码 0。检查包含格式与 lint、依赖声明、相对导入、入口依赖图、install-lock、TypeScript 和 browser smoke。

证据索引：`.artifacts/atlas-p0/evidence-index-2026-10-24-p0.json`。索引列出采用的报告、文件哈希、源文件哈希与原始进程观察记录。旧失败尝试仍保留；run-OSKoXT 与 run-GicnjE 的整批 summary 含后来修复的验证夹具失败，因此这里只采用其中明确通过的分组，不宣称那些整批运行全通过。

源码位置：

- `scripts/atlas-p0-run.mjs`：隔离环境、运行目标组并保存完整日志和 JSON。
- `scripts/atlas-p0-durable.config.ts`：为 durable 定向测试接入源码 aliases。
- `packages/coding-agent/test/suite/atlas-p0-boundaries.test.ts`：权限正例、反例、取消、重复 prompt。
- `packages/coding-agent/test/atlas-p0-sdk-process.test.ts`：SDK 三个真实中断窗口。
- `packages/durable/test/atlas-p0-process-recovery.test.ts` 及 support/fixtures：进程屏障、强杀、SQLite 重开与副作用观察。

模型数据已按上述 pin hydration 后，在仓库根目录复现：

```powershell
node scripts/atlas-p0-run.mjs check coding durable coding-p0 durable-p0
```

脚本每次新建 `.artifacts/atlas-p0/run-*`。运行记录与合成会话被 Git 忽略，源代码可随仓库维护。

## 7. 修复与运行限制

- 修复 `scripts/check-entry-graphs.mjs`：将 Windows 相对路径统一成 `/` 后再匹配规则。原脚本因 `\` 与 allowlist 的 `/` 不一致，把允许的 telemetry 入口误判为违规；修复后检查通过，未放宽规则。
- 本机 PATH 会先发现 Windows WSL bash 启动器，而 Git 装在非默认目录。测试脚本仅在子进程环境中优先选择验证存在的 Git Bash；未修改系统 PATH。正式 Worker 应显式配置 shell。
- npm 11.9 对仓库 min-release-age 配置发出 Unknown config 警告；不能把该设置当作已生效的供应链控制。未升级系统 npm 或更改全局配置。
- Node 的 SQLite 提示 Experimental，pi-durable 本身也明确标注 Experimental。当前通过的是该 SHA 的源码契约验证。
- 未验证：断电/主机故障、多写入者或多设备并发、真实模型接口与效果、网络和凭据隔离、生产沙箱、已构建安装包、完整 Python 到正式 CLI 的端到端集成。RPC 的帧与受理/完成语义有定向回归覆盖，但不能代替后一项部署验收。

## 8. P1 选型与必须补齐的边界

根据恢复实测，调整初始规划中的 SDK 优先假设：**后台个人助理任务建议采用 TypeScript Worker + 固定 SHA 的 pi-durable，通过 Atlas 自有 Executor 适配层封装。** SDK/CLI 保留为交互使用与诊断入口，不把 SDK 会话循环和 durable Harness 叠成两套调度器。

P1 优先交付四项：

1. **受控工具网关**：只加载可信代码，显式工具与资料范围；先做受控资料读取，可靠隔离之前不开放任意 shell、代码执行和第三方扩展。
2. **任务入口**：可信身份/项目、requestId 与规范化 payload 摘要绑定；相同 ID 不同内容拒绝为冲突。
3. **操作账本**：执行前 intent、稳定 operation_id、实际结果和不确定状态；unsafe 外部动作不自动重试，使用下游幂等键或结果查询完成对账。
4. **业务状态与进程归属**：工具中断或结果不确定时进入待核实状态；done 不直接映射业务成功；执行子进程需要宿主隔离与终止机制。

这四项是后续实施要求，本轮没有将其伪装成已完成的生产能力。

## 9. 置信度

以下为基于实测覆盖的工程置信度，不是统计准确率。

| 评估 | 置信度 | 依据与限制 |
| --- | --- | --- |
| 原生 cwd/工具名单不足以提供通用权限隔离 | **95%** | 已复现路径、junction、扩展和检查后换路径反例 |
| durable 在本机进程崩溃下的保守恢复与请求去重行为 | **90%** | 上游定向回归与真实进程屏障实验；不含断电、多所有者 |
| safe 声明不能替代工具本身的幂等性 | **95%** | 同一崩溃窗口分别观察到 2 次与 1 次副作用 |
| 使用隔离适配层、固定 durable 版本继续受控 P1 | **80%** | 恢复路径已实跑，但实验性 API、生产权限和真实连接器仍需实施 |
| 直接开展无人值守、任意系统写入 | **当前不满足进入条件** | 权限与进程隔离仍有已验证缺口 |
