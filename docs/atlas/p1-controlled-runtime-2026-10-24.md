# P1：Atlas Brain 单项目受控任务验收

日期：2026-10-24。结论：**本次 P1 受控范围通过验收**。用户已选择离线受控模式，所有任务和报告明确标注 `controlled_offline`、`model_connected=false`，无真实模型推理。

总体工程置信度 **90%**，只适用于当前 Windows、本机单用户、单项目、两种工具的受控闭环。这是依据测试覆盖作出的工程判断，不是统计可靠率或安全认证。开放网络、第三方扩展、跨项目写入和生产个人助理能力尚未验收。

## 实现与权限边界

```text
Atlas HTTP / SDK / CLI
    -> Bearer 认证与严格请求校验
    -> 可信 Python Host（项目授权、输入快照、SQLite 账本）
    -> Windows Job Object 中的 Node Worker
    -> 固定源码版本 pi-durable + 显式工具注册表
    -> JSONL 工具请求回到 Host -> 校验 -> 原子事务 -> 回执
```

1. 权限来自本机受信配置，调用方只能提供 `request_id / project_id / goal / documents`。身份、项目根目录、工具、模型和扩展不能由任务请求覆盖。
2. 每个 Host 只注册一个项目，默认授权 Atlas 的 `README.md` 和 `server.py`。每次任务最多四个 UTF-8 文档，单文档最多 64 KiB。目标长度最多 2000 字符，请求体最多 16 KiB，同时未结束任务最多 32 个，串行执行。
3. Worker 仅有 `read_document(document_id)`、`publish_report(content)`。它通过不透明文档 ID 取快照，不能传任意路径、执行 shell、加载项目扩展或直接写项目。
4. Host 持有真实打开的 Windows 文件句柄，校验该句柄的最终路径，再从同一个句柄读取。项目根句柄和文件句柄拒绝写入/删除共享；拒绝文件自身的 reparse point，父 junction 根据最终句柄路径检查。
5. SQLite 保存任务、输入快照、请求摘要、策略摘要、操作摘要、回执、报告及有序事件。`publish_report` 的报告、回执和事件在同一事务中提交，不依赖“先写文件，再记成功”。
6. 相同身份/项目/request_id 加相同规范化请求返回原任务；同 ID 换内容返回 409。输入在首次接纳时固定，恢复不重新读取变化后的项目文件。
7. Host 用操作语义 ID `read:d1`、`publish:report` 加参数摘要去重。已完成操作返回原回执；同操作 ID 换参数拒绝；遗留 `intent` 不自动重做，要求对账。
8. 只有 durable 状态 done、报告已存在且没有权限拒绝事件时，任务才标为 completed。引擎 done 不等于业务成功。
9. 同一状态目录只允许一个 Host 持有文件锁。启动时恢复 queued/interrupted，将遗留 running 记录为 interrupted；策略或引擎 pin 变化会阻止旧任务继续。运行期间可 cancel 或对 interrupted 执行 resume。

## 进程隔离

Node 进程先以 suspended 状态创建，成功分配 Windows Job Object 后才恢复执行。分配失败直接结束进程，禁止降级运行。

- Job 设置 kill-on-close、活动进程上限 1、进程内存上限 1 GiB，无 breakaway。
- Node 启用 `--permission`，只能读取代码依赖目录和自身运行目录，只能写自身运行目录；未放行 child_process、worker、addons 等能力。
- Worker 仅继承最小系统变量及独立 HOME/TEMP，不继承供应商密钥、代理或 Node 注入选项。认证 token 位于运行目录外，Worker 无读取权限。
- Host 执行预算默认 30 秒，工具请求最多 12 次、模型轮次最多 8 次，JSONL 单条最多 1 MiB，输出队列最多 32 条。超限结束任务并关闭 Job。
- Job 句柄不继承给 Worker。强杀 Host 后，操作系统关闭最后的 Job 句柄并结束 Worker。

这是一组针对可信 Worker 和不可信任务内容的约束，不是 AppContainer、虚拟机或恶意代码安全沙箱。Job Object 不提供网络隔离，Node 权限也没有在此配置网络白名单。可信 Host、配置、源码和依赖目录仍需由操作系统账号保护；本轮未验证恶意本机管理员、网络盘、任意原生模块或断电恢复。

## 固定版本

- pi-durable / pi-ai / chord 包版本：精确 `1.4.0`。
- 源码基线：`f321e5bdd26b3624bfb2ab182a334d0a5edb698a`。
- Node：精确验证 `v24.14.0`；Python 本机验证 `3.13.12`。
- `packages/atlas-runtime/engine-lock.json`：346 个源码/关键元数据文件的 SHA-256，含 Worker、source resolver、package-lock 和 tsconfig。
- 启动和每次执行均检查 pin；完整 manifest 摘要纳入任务策略。显式审查升级后才能运行 `python integrations/lock_engine.py` 更新 pin；不能为绕过校验直接刷新。
- 使用锁定源码和 source resolver 运行，没有自动升级、动态发现项目扩展，也没有宣称已验证独立发布的 npm tarball。依赖安装需 `npm ci --ignore-scripts`，模型目录按 P0 已验证的 catalog pin 准备。
- 本次 package-lock 仅新增私有 Atlas workspace，原依赖版本、integrity 和平台 libc 约束均保留。pin 不替代供应链签名，也不对已安装 node_modules 每个文件做运行时校验。

## 测试结果

| 检查 | 结果 | 关键证据 |
| --- | --- | --- |
| P1 Host/账本/恢复测试 | 14/14 | 并发 16 次同请求、内容冲突、权限拒绝、快照、事务回滚、崩溃恢复 |
| Windows 隔离与 pin 测试 | 4/4 | 越界读写拒绝、Node 子进程拒绝、Job 独立拒绝子进程、强杀父进程结束 Worker、源码变更检测 |
| 实际 Atlas HTTP/SDK 测试 | 2/2 | 未认证 401、畸形/重复字段 JSON 拒绝、超限 413、任务端到端与重复请求 |
| pi-durable 原有回归 | 59/59 | submissions、generation/tool recovery、取消屏障、storage failure、read-only |
| 根检查 `npm run check` | exit 0 | Biome、精确依赖、runtime imports、entry graphs、install lock、tsc、browser smoke |
| 实际项目 HTTP + SDK + CLI 验收 | 通过 | 两份真实授权文档，3 笔完成操作，1 份报告，重试同任务，源文件未改变 |

20 个 P1 测试用例与 59 个上游回归均通过；表中每个测试用例可含多项断言，未将子断言重复计数。

本轮关键故障注入：

- 报告事务提交后、Worker 收到结果前杀死 Worker：恢复后同一报告、发布完成记录仍为一条。
- 同一位置 `os._exit` 强杀整个 Host：SQLite 已提交结果保留，新 Host 恢复完成，报告仍一份。
- 报告插入后故意令回执更新失败：整个事务回滚；修复故障后再次提交只生成一份报告。
- 文档句柄校验后切换父 junction：路径已指向外部内容，读回仍为原授权句柄内容。
- 单独关闭 Node permission 再尝试启动子进程：Job 的单进程限制仍阻止启动。
- 空认证 token：启动失败，不接受空 Bearer；不合法认证内容也按拒绝处理。

保留的环境提示：npm 11.9.0 对仓库原有 `min-release-age` 配置输出 warning；测试 Node SQLite 输出 experimental 提示。没有通过升级工具链或删除原配置消除这些提示；它们未造成检查失败，不能将本轮称为“零警告”。

## 实际单项目任务

- task_id：`d79342f0-7ff4-45fc-803c-82e148995ef8`。
- 项目：Atlas Brain；输入 `README.md`、`server.py`。
- 目标：核验项目说明与任务 API 入口，生成带来源摘要的受控报告。
- 结果：completed，2 次文档快照读取 + 1 次报告发布；10 条有序事件。
- 报告 SHA-256：`acb8b8cd97bf7b0f581515b89c2065c84500b690879f0bf2fa87c92cf6859d3b`。
- 再次提交返回同一 task_id；SDK 与 CLI 均能读回结果；未认证访问报告为 401。
- 输出为确定性的来源摘录与 SHA-256，不是 LLM 生成的分析或推断。每文档报告只展示前 1200 字符，快照保存完整授权输入。

本次验收服务使用临时本机端口，验收后已停止。持久状态位于 `C:\Users\Administrator\.atlas-brain\p1`，未安装后台服务或修改现有服务。

## 启动和复验

在 `D:\airesearch\AI原生开发\atlas-brain` 执行：

```powershell
python pi-plus/integrations/install_workbench.py --check
python pi-plus/integrations/configure_atlas.py
python server.py --p1-config pi-plus/.artifacts/atlas-p1/atlas-local.json
```

另一个 PowerShell：

```powershell
$env:ATLAS_TOKEN_FILE = "$env:USERPROFILE/.atlas-brain/p1/auth.token"
python atlas.py task submit p1-local-001 atlas-brain README.md "核验项目说明"
python atlas.py task get "<task_id>"
python atlas.py task get "<task_id>" events
python atlas.py task get "<task_id>" operations
python atlas.py task get "<task_id>" artifact
python atlas.py task cancel "<task_id>"
python atlas.py task resume "<interrupted_task_id>"
```

若 8765 已占用，在启动命令加 `--port 8766`，客户端设置 `$env:ATLAS_BASE_URL='http://127.0.0.1:8766'`。不要输出、提交或把 token 放入 URL。恢复前若收到 `task_still_stopping`，待进程清理完成再重试；请求内容变更使用新的 request_id；不要清空账本来绕过去重。

在 `pi-plus` 下复验：

```powershell
python integrations/run_p1_checks.py
node scripts/atlas-p0-run.mjs durable
node scripts/atlas-p0-run.mjs check
python integrations/accept_atlas.py
```

API：`POST/GET /api/v1/tasks`，`GET /api/v1/tasks/{id}` 及其 `/events /operations /artifact`，`POST /cancel /resume`。上述端点都要求 Bearer；接口与旧的 `/api/v1/runs` 模拟运行分开，网页和 MCP 尚未接入受控任务展示。

Atlas 父目录目前不是 Git 仓库。为避免只有本机父文件修改，`integrations/workbench/` 保存本次 server/SDK/CLI/README 适配器及基线/安装后摘要；`install_workbench.py` 仅接受匹配基线或已安装内容，拒绝覆盖有漂移的文件，安装前创建备份。当前安装一致性检查通过。适配器随 P1 源码交付，提交与远端状态以 Git 历史为准。

## 仓库交付复验

发布前使用 Git 暂存区导出代码，确认没有 Atlas 父项目、没有旧 `.artifacts`，重新执行 `npm ci --ignore-scripts --no-audit --no-fund` 安装 301 个锁定依赖，复制相同固定 catalog 的已验证模型定义，再跑 P1 **20/20** 测试通过。该验证包括真实 Node 进程、Job Object 与 SQLite；HTTP 测试使用仓库内适配器和惰性的旧 runtime 占位对象。原始实际 Atlas 项目验收仍由上文的独立记录支撑。

本次同时复跑 P0/上游回归 **109/109**、根检查 exit 0。固定依赖安装输出既有 transitive dependency 弃用提示；未借打包工作升级依赖。Node 的文件权限拒绝指向仓库外部的整目录 node_modules junction，因此依赖应安装在本 checkout，不应通过扩大读权限绕过。

P1 测试运行器现在会创建自身证据目录，测试不再依赖未版本化的父项目。取消测试等待其实际故障注入事件，等待上限对齐 Host 执行预算；失败会输出状态和诊断。生产执行预算、工具名单和权限策略均未放宽。故障注入子进程即使测试失败也会回收。

随仓库保存的 [验收摘要](validation/p1-2026-10-24.json) 记录测试计数、复验条件与交付源码摘要；运行数据库、认证 token 和原始日志继续留在本机。

## 原始证据

- P1 最终测试：`.artifacts/atlas-p1/verification-7mi0h6ap/summary.json` 及同目录三个日志。
- 项目首次完整验收：`.artifacts/atlas-p1/acceptance-srg2xth7/summary.json`、`report.md`、`operations.json`、`events.json`。
- 重复运行验证：`.artifacts/atlas-p1/acceptance-bmjk7gnq/summary.json`，同一个任务 ID。
- durable 回归：`.artifacts/atlas-p0/run-LAZ6uD/durable.json`。
- 最终根检查：`.artifacts/atlas-p0/run-PKwLz8/summary.json`、`check.log`，exit 0；Biome 无需修改。
- 汇总索引：`.artifacts/atlas-p1/evidence-index-2026-10-24.json`。

`.artifacts` 被 Git 忽略，包含本机运行证据，不是源码交付的一部分；测试和复验脚本均保存在仓库中。

## 置信度与下一阶段边界

| 结论 | 工程置信度 | 剩余不确定性 |
| --- | --- | --- |
| 请求校验与身份/项目约束符合本轮契约 | 95% | 不是公网 API 压力或渗透测试；标准库 HTTP 服务只用于本机 |
| 受控文档权限与 junction 竞态防护有效 | 90% | 未覆盖网络盘、恶意本机用户和全部 NTFS 特殊情形 |
| 本地报告发布重放不重复、断点恢复有效 | 93% | 只保证当前 SQLite 事务内效果；没有验证断电、磁盘损坏和外部服务 |
| Worker 生命周期与子进程限制有效 | 90% | 已测当前 Windows/Node；没有恶意原生代码、网络隔离或其他 OS 的结论 |

后续接入真实模型时应保持这些权限、账本和完成条件；向外部系统写入前先增加连接器级幂等键、查询对账能力及不确定结果处理。没有这些能力的外部工具不能简单标为 replay safe。本轮不扩展长期记忆、跨项目执行或任意命令能力。
