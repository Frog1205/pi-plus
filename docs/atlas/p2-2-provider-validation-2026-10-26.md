# P2.2：供应商故障矩阵与人工对账验收

日期：2026-10-26（America/Los_Angeles）。P2.1 已发布至 `main`：`48a23af9d3458d5468297b7debc505ff58196eda`。发布前修复共享 SQLite 连接的并发读取问题，31/31 与完整代码检查通过，见 [发布复验](validation/p2-1-publication-2026-10-26.json)。

2026-10-29 补充：用户确认 MiniMax 中国站后，真实合成任务通过，P2.2 本轮受控范围已补齐。见 [新增实测与当前结论](p2-2-minimax-cn-validation-2026-10-29.md)；本文以下与原 validation 摘要保留 2026-10-26 的历史状态。

本阶段保留 P1/P2.0/P2.1 已发布的运行时与版本锁，新增显式 ProviderHost。默认 Atlas API 继续为 controlled_offline，没有给客户端新增 mode/provider/URL/headers 参数。

## 本次授权与验收分层

- 供应商：DeepSeek、MiniMax；一次任务只绑定一个 profile，不自动切换或跨供应商重试。
- 凭据：仅 Host 从用户确认的 DEEPSEEK_API_KEY / MINIMAX_API_KEY 读取。不从其他应用登录状态或密钥文件搜索凭据。
- 输入：只允许仓库随附的 [合成 README](../../integrations/fixtures/p22-project/README.md)，不发送实际 Atlas README.md、server.py 或其他私有资料。源文件 SHA 与任务快照绑定。
- 费用：合计 10 元人民币，每家最多 5 元；分别持久化独立批次上限，不通过新 request_id 清零历史批次预留。
- 确定性故障在本机假服务上制造，真实调用另行记录。夹具通过不计为真实模型成功。

## 协议选择与适配

两家当前均提供 Anthropic Messages 兼容接口。选用 DeepSeek 的 deepseek-flash 和 MiniMax 的 MiniMax-M3，显式 `thinking.type=disabled`、`max_tokens=1024`；MiniMax-M3 支持关闭 thinking，未选择不能关闭 thinking 的模型来绕过既有输出上限。[DeepSeek 协议](https://api-docs.deepseek.com/guides/anthropic_api/)、[MiniMax 协议](https://platform.minimax.cn/docs/api-reference/text-anthropic-api)

首条真实线路采用 **HTTP 完整 JSON 响应**，不是厂商 SSE 流式联调。Host 校验完整 JSON、模型标识、stop_reason、工具参数与 usage 后，将结果转换成固定 Pi Responses 事件。pi-ai 1.4.0 的原版 Responses adapter 和 pi-durable 仍实际执行，未新增另一套 Agent 循环。

引擎内部保留固定代理模型代号；它不代表实际远端模型。对外 provenance、请求账本和完整供应商回执记录真正 provider/model。Pi 内部代理模型的零价格不是费用证据，预算只以 Host 账本为准。

供应商返回的完整 assistant content 留在私有回执中；后续请求由 Host 用原始 content 和真正工具回执重建。Worker 不能伪造历史消息或工具输出。未开放服务端搜索、代码执行、图片、网页、MCP 或动态工具发现。

```text
四字段任务 + 授权合成快照
  -> Windows Job / Node permission 中的 Pi Worker
  -> 既有封闭 registry 与稳定 generation memo
  -> Host 重建 canonical 请求及 Messages 上下文
  -> prepared / 预算预留 -> dispatching
  -> 白名单 HTTPS 供应商（或显式本机夹具）
  -> 完整响应 + provider receipt + usage/预算同事务提交
  -> 规范化 Responses 帧 -> 原版 Pi adapter
  -> 原 P1 两工具网关 -> 报告与回执同事务提交
```

## 权限、网络与凭据

仍只允许 read_document 和 publish_report；只读授权快照，报告写入状态数据库，不写项目目录。单进程 Job、kill-on-close、Node 写入自己的运行目录、12 次持久化工具尝试、4 次模型请求、1024 单次输出、4096 累计输出和原始接纳时间起算的 60 秒上限保持。

Worker 只有不可路由的虚拟配置和占位凭据，默认 fetch 回退被禁止。真正 endpoint、x-api-key 和 TLS 连接由 Host 重建。既检查原始响应，也检查 JSON 解码后的凭据反射；明文与 JSON 转义形式都不能进入 Worker 上下文。测试和真实验收另外扫描 Worker 文件、事件与公开结果。

本机系统 DNS 实测把三个官方 API 域名解析到 Fake-IP/非公网地址，既有策略正确拒绝，未为此放开私网。P2.2 新增固定 Cloudflare DoH 控制通道：仅向 `1.1.1.1:443` 连接并验证 `cloudflare-dns.com` 证书，只查询白名单模型域名。全部 A 记录均需为公网地址；拒绝私网、metadata、benchmark/Fake-IP、mapped IPv6 等地址。

供应商 TCP 连接使用已校验数字 IP，TLS 仍验证原始官方 hostname，避免二次系统 DNS 解析；不读取环境代理、不关闭证书验证、不跟随重定向。三个域名的直连证书预检均通过，预检没有发送认证头或模型请求。DoH 只携带域名，不携带文档、模型正文或供应商密钥。

这些是可信宿主代码中的网络策略，不是任意恶意原生代码的操作系统网络沙箱。

## 预算与费用证据

账本单位为 CNY_NANO_UPPER_ESTIMATE，即人民币十亿分之一的本地上限估算单位。当前冻结：

| 线路 | 冻结上限单价（每百万 tokens） | 依据 |
| --- | --- | --- |
| DeepSeek | 输入 2 元，输出 8 元 | 使用高峰、未命中缓存价格；不按更便宜的缓存价预留 |
| MiniMax 中国站 | 输入 4.2 元，输出 16.8 元 | 使用页面列示的未折扣标准价作为上限，而非五折显示价 |
| MiniMax 国际站 | 输入 4.8 元，输出 19.2 元 | 未折扣 0.60/2.40 美元乘预算换算上限 8；这是保守预算参数，不是市场汇率 |

来源核对日为本报告日期：[DeepSeek 定价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[MiniMax 中国站定价](https://platform.minimax.cn/docs/pricing/overview)、[MiniMax 国际站定价](https://platform.minimax.io/docs/pricing/overview)。

输入预留使用完整重建请求的 UTF-8 字节数乘二并加固定余量，不把通用 token 估算当作已验证的厂家 tokenizer。响应 usage 缺失保留最坏预留；超过预留则拒绝工具执行，并保留至少原预留与已知用量中的较大值。

已知 usage 按冻结上限单价结算仍是**本地保守估算，不是供应商实际扣款或账单核验**。完整原始 usage 留在私有回执中，可供后续人工对账。401/403 停止；429/5xx、在途断开和强杀按未知结果保留预算，不自动重发。

## 故障矩阵与对账规则

按每家协议分别执行以下故障，实际请求计数来自本机服务端：

| 场景 | 必须结果 |
| --- | --- |
| 正常闭环、重复任务和重开读取 | 三次模型响应、一份报告，重复操作不增加请求 |
| 响应提交后杀 Worker / Host | 恢复使用原完整回执与缓存，整任务仍为三次请求 |
| 报告提交后杀 Worker | 原报告与发布回执一致，不产生第二份报告 |
| 在途杀 Host / 断流 | outcome_unknown；两次重启或换 generation 均不再发送，预算保留 |
| 取消 | 关闭本地连接、回收 Worker，未知成本不被清零 |
| 401/403/429/500/重定向 | 各只发一次；不跟随地址、不隐式重试，按分类停止 |
| JSON/UTF-8/重复键/超大响应/错误模型/截断 | 不执行工具，不标为 completed |
| 超用量、非法 usage、并行调用 | 拒绝；不扩大现有预算或工具执行范围 |
| 明文/JSON 转义凭据反射 | 在 Host 拦截，未进入 Worker |
| bash、额外路径参数、提前发布 | 无越权副作用 |
| 上下文/schema/URL/headers 覆盖、非授权快照 | 发出前拒绝 |
| 缺 usage | 保留 unknown 与最坏预留，不按零费用继续 |

对账仅允许可信本机操作员提供完整响应证据，必须匹配 task_id、generation_id、profile_hash、request_hash，以及已知的 provider request ID。成功后响应、供应商回执、usage、预算与对账回执同事务提交；相同证据重复提交返回原回执，换证据冲突。

错误身份或事务中断会保留原 outcome_unknown 和预算。对账不会自动 resume，不会发出任何新模型请求，也没有“清空未知状态并重试”按钮。尚未验证两家支持远端结果查询或幂等键，不能把操作员证据当作自动完成的供应商账单认证。

## 运行与对账命令

零真实调用的故障验收：

```powershell
python integrations/run_p22_checks.py
python integrations/run_p22_checks.py --operator
```

真实验收入口要求显式 `--live`；脚本只发送固定合成文件，分别读取已授权的供应商环境变量。选择 MiniMax 的已确认站点，不在两站间自动切换：

```powershell
python integrations/run_p22_live.py --provider deepseek --live
# 按已确认账户地区，二选一：minimax-cn 或 minimax-io
```

状态查看是只读操作，不需要模型密钥，不创建网络连接：

```powershell
python integrations/reconcile_provider.py status --state <state-directory> --task-id <task-id>
```

操作员核对完整证据后可执行 apply。approved-profile.json 与证据文件放在本机受控状态目录，不提交 Git，密钥仍从环境变量读取：

```powershell
python integrations/reconcile_provider.py apply --profile <approved-profile.json> --evidence <evidence.json> --task-id <task-id> --generation-id <generation-id>
```

原始数据库、供应商正文、运行日志和实际凭据均不提交仓库。当前 provider-lock 单独覆盖 366 个文件，正常测试不自行刷新；审查源码变化后才运行 `python integrations/lock_provider.py`。

## 验收记录与限制

最终源码复验和真实调用记录整理在同目录 validation 摘要中。首轮完整矩阵 33 项与操作员 CLI 4 项通过；补充 JSON 转义反射用例后重新跑完整矩阵。最终结果以摘要中的当前源码哈希为准，不将重复复验次数累加成独立测试数量。

| 最终验收 | 结果 |
| --- | --- |
| 两家协议/故障矩阵 | 33/33，含 28 项供应商测试与 5 项策略测试；本机服务共收到 86 次请求 |
| 操作员 CLI 对账 | 4/4，CLI 本身未发模型请求 |
| P2.1 发布复验 | 31/31 |
| P2.0 回归 | 15/15 |
| P1 回归 | 20/20（保留此前一次 Windows 连接中止记录） |
| P0/上游回归 | 109/109 |
| 完整代码检查 | exit 0，Biome 无修正 |
| DeepSeek 真实合成任务 | 2/2，6 条完整模型响应；最终版本下的任务 3 次，重复提交与重开不增加调用 |
| MiniMax 真实任务 | 尚未执行，等待账户站点选择；协议夹具与 TLS 预检通过 |

当前整体状态为 **部分完成：MiniMax 实际调用未验收**。两条已完成 DeepSeek 任务的本地费用上限估算累计约 **0.015288 元**，计入同一批次；不是供应商账单扣款证明。最终版本任务的两条事实、来源 SHA 与明确建议已人工核对合成源文件。

- [版本化验收摘要](validation/p2-2-2026-10-26.json)：当前状态、每家逐项计数、两次真实任务与源码哈希。
- 最终矩阵：`.artifacts/atlas-p22/verification-m59ij4kz/`；CLI：`.artifacts/atlas-p22/verification-2hpat874/`。
- P2.0：`.artifacts/atlas-p2/verification-bjylscie/`；P1：`.artifacts/atlas-p1/verification-m2gt9r2l/`；检查与 109 回归：`.artifacts/atlas-p0/run-kWeF1t/`。
- DeepSeek 最新报告、ledger 与来源证据：`.artifacts/atlas-p22/live-2026-10-26/deepseek/`；首次任务快照保留在该目录 `runs/p22-live-1/`，原始任务与回执也保存在数据库中。
- 综合本地索引：`.artifacts/atlas-p22/evidence-index-2026-10-26.json`，逐一重算所有当前测试和真实运行的源码 SHA，校验 P1/P2.0/P2.1 的运行时与 pin 未改。

P2.2 源码、测试和文档当前留在本机，未混入已发布的 P2.1 提交。

DeepSeek 首个真实合成任务已完成，三条完整供应商回执、usage 可用，重复提交与重启读取未增加调用；该任务按冻结上限单价估算约 0.00765 元，账单未核验。安全补充前的已完成任务保留其原 runtime pin；不会在新 pin 下重新解释原任务。

MiniMax 已通过协议夹具与 TLS 预检，真实验收需以确认的账户站点执行后更新记录。

P1 基线一次 HTTP 超大请求用例遇到 Windows WinError 10053，服务端日志已有 413；保持原代码的完整复验随后 20/20 通过。保留首次失败证据，将该现象列为 Windows HTTP 兼容性观察项，不据此宣布完全稳定。

本阶段设计/工程置信度 **90%（故障与本地对账，限固定 Windows / Node 24.14.0 / Pi 1.4.0）**；DeepSeek 受控真实联调 **85%**，MiniMax 真实能力暂不评分。以上是工程判断，不是统计可靠率；尚未核验供应商账单、远端取消或任意远端副作用 exactly-once，也未完成两家厂商 SSE 流式或 P2.3 五任务验收。实际 Atlas 私有资料未授权外发。
