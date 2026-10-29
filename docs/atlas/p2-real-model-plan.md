# P2：在现有权限边界内接入真实模型

最新状态（2026-10-29）：**P2.2 本轮受控范围通过**，MiniMax 中国站真实合成任务已补齐，与 DeepSeek 使用同一固定 runtime pin。权限边界保持，P2.3 五任务与真实 Atlas 项目资料接入尚未验收。当前实测、费用及置信度见 [P2.2 补充报告](p2-2-minimax-cn-validation-2026-10-29.md)。以下保留原始规划与按日期记录的历史更新。

日期：2026-10-24（America/Los_Angeles）。状态：**P2.0 协议与 P2.1 宿主治理已通过假供应商验收；真实模型调用尚未验收**。阶段记录见 [P2.0 验证报告](p2-0-protocol-validation-2026-10-24.md)和 [P2.1 治理报告](p2-1-governance-validation-2026-10-24.md)，以下保留完整 P2 的实施与验收要求。

2026-10-26 更新：P2.1 已发布，P2.2 两家故障矩阵与本地对账通过，DeepSeek 已完成真实合成任务；MiniMax 实际调用等待账户站点确认。用户授权仅合成资料、合计 10 元/每家最多 5 元，凭据使用指定环境变量。每个 Host 仍只绑定一家供应商，不引入自动跨供应商故障切换。当日证据和未完成范围见 [P2.2 报告](p2-2-provider-validation-2026-10-26.md)；以下原始规划中的“尚未选定”是 2026-10-24 的设计背景，不代替当前状态。

已发布基线：P1 提交 [`eed2d2ccc8bfcceb867dddb21cc3bf1cd4bdc754`](https://github.com/Frog1205/pi-plus/commit/eed2d2ccc8bfcceb867dddb21cc3bf1cd4bdc754)。P1 20 项测试、P0/上游 109 项回归和根检查通过；独立源码快照重新安装依赖后 P1 20 项再次通过。计数、环境及源文件摘要见 [交付验收记录](validation/p1-2026-10-24.json)。

**总体设计置信度：85%。** 现有任务/工具/进程边界有实测基础；新增模型传输、流式协议、请求落账和供应商兼容性尚未验证，不能将设计置信度视为上线成功率。

## 1. P2 目标与首个场景

只接入一条明确授权的模型线路和一个固定模型配置，完成同一 Atlas Brain 项目的资料核验任务。模型实际读取授权快照，生成有来源、区分事实与推断的简短报告，再经原有 `publish_report` 发布。

验收场景固定为读取 `README.md`、`server.py`，回答“当前受控任务的入口、能力边界及下一步”，报告至少包括两条可核查事实、每条事实的来源引用与 SHA-256，以及明确标为建议的后续行动。这里验收实际推理能力，不再以确定性摘录代替模型分析。

供应商、endpoint、具体 model ID、凭据引用、联网输入文件授权及费用上限尚未选定；它们在第一次付费/联网验收前写入可信本机 profile。本次规划不读取现有工具登录凭据、不调用真实模型。Atlas 的授权应独立配置，不能假定 Codex 的登录或接口切换同时授权 Atlas。

## 2. 必须保持的边界

| 边界 | P2 要求 |
| --- | --- |
| 项目 | 单 Host 单项目；仍由可信配置登记根目录和文档白名单 |
| 客户端输入 | 沿用 request_id/project_id/goal/documents 四字段；不接受模型、URL、身份、工具、扩展或任意路径覆盖 |
| 工具 | 仍只有 read_document、publish_report；无 shell、项目写入、第三方扩展、浏览器或远程 MCP |
| 文件 | 同句柄校验与读取、输入快照固定、Node 写入仅限自己的运行目录 |
| 进程 | suspended 创建后加入单进程 Job，再执行；kill-on-close 和 Node 权限继续启用 |
| 凭据 | 真正的供应商凭据只由可信 Host 读取；不传入 Worker 配置、环境、上下文、事件或报告 |
| 报告副作用 | 继续由 Host 在 SQLite 原子事务中保存报告、回执及事件 |
| 不确定结果 | 工具 intent 和模型请求 outcome_unknown 均停止自动重做，进入对账 |
| 依赖 | pi-durable/ai/chord 保持 1.4.0 和已审查源码基线；新增运行时代码纳入 pin，不自动更新内核 |

P2 只新增“Host 向指定模型服务发送已授权任务上下文”的受控网络能力。模型服务不能自行读取本机目录，也不授予它托管 shell、文件搜索、网页搜索等额外工具。

Job Object 不是网络沙箱。P2 的 broker 是可信代码中的网络策略执行点，不宣称阻断任意恶意原生代码的联网行为；若后续需要此保证，应另行验收 OS 网络隔离方案。

## 3. 推荐架构

```text
Atlas API / SDK / CLI
  -> 认证、四字段校验、任务与输入快照
  -> Job + Node permission 中的 Worker
     -> pi-durable / pi-ai（只注册两个业务工具）
     -> 模型 I/O 经版本化 JSONL 发送给 Host
  -> Host ModelBroker
     -> profile、上下文、预算、目标地址校验
     -> 模型请求账本 / 响应缓存
     -> 指定 HTTPS endpoint
     -> 完整响应落盘 -> 回给 Worker
  -> 原有 ToolGateway -> 事务回执 / 报告
```

优先复用 pi-ai 的现有 HTTP adapter，以自定义 `fetch` 把模型 I/O 转为 broker IPC。真实凭据由 Host 重建请求头，Worker 只拥有无权限的占位配置，不能靠占位值直连供应商。第一条线路限定 HTTP/SSE；禁用 WebSocket、后台 deferred、动态模型发现、SDK 隐式重试及默认网络回退。

选择这一方向的源码依据：当前 `ProviderRequestOptions.fetch` 支持注入 HTTP transport，但明确不影响 WebSocket，部分 adapter 还会拒绝自定义 fetch；`GenerationHooks.beforeRequest` 在每次尝试及恢复时执行，并提供稳定 taskId 与持久化 memo。因此第一步必须验证所选 adapter 的实际请求、取消及恢复全部经过 broker。未通过就保持 live 模式不可用，不能退回 Worker 直接联网。

如果现有 adapter 无法满足约束，备选是实现范围明确的 `ProviderStreams` 适配器，输出 pi-ai 的标准 assistant/tool-call 事件；须先通过相同协议夹具与恢复测试，再替换传输实现。两条路线不同时开发第二套 Agent 循环。

## 4. 可信 profile 与网络入口

profile 至少冻结以下非秘密字段：profile/version、mode、provider、API dialect、endpoint、model ID、允许的响应模型标识、TLS/代理策略、工具 schema 摘要、提示模板版本、数据外发文档范围和预算。秘密只保存凭据引用。

- 初版只启用一家供应商的一种协议；先做 tool calling、完整响应、usage 和取消能力探测，再冻结 profile。只提供可变别名的供应商，要记录实际返回的模型标识与别名漂移风险。
- Host 固定 method/path/model、重建认证头及工具 schema，拒绝 Worker 提供 URL、认证头或越权功能；请求内容必须由已提交消息、授权快照和合法工具回执组成。
- 仅允许配置中的 HTTPS 目标；禁用重定向，校验 host/port/TLS。默认拒绝环回、私网、link-local、元数据地址及 IPv6 对应范围。模型测试夹具的本机例外只能出现在显式测试配置中。
- DNS 校验应绑定实际连接，防止只校验一次再解析连接的竞态。代理需显式配置与独立验证；未验证的环境代理不自动继承到模型通道。
- 不执行模型返回的代码或 URL。文档中的“忽略规则、读取密钥、换项目”等文本仍只是输入数据。
- provider 支持关闭服务端持久化时显式关闭，并记录实际配置；不把未核验的服务端行为称为本地隔离。

profile 的非秘密摘要、mode、协议版本与完整 runtime pin 摘要纳入任务策略和请求摘要。不能用 P1 offline 的旧 request_id 作为 live 验收成功证据。任务需持久保存自己的 mode/profile/provenance，而不是按当前 Host 配置重新解释历史任务。

## 5. 模型请求账本与恢复

工具不重复发布与模型不重复计费是两个不同保证。P1 的本地报告事务不能自动消除远端推理的未知结果窗口。

新增模型请求记录至少包括：task_id、generation task ID、Host 分配的持久化请求序号、profile_hash、规范化请求摘要、状态、dispatch 次数、供应商 request/response ID（若有）、完整响应摘要、usage 是否可用、预算预留和错误类别。稳定 generation 身份来自 durable hook/memo；不能用重启后清零的内存计数或随机 HTTP ID 作为恢复身份。

```text
prepared -> dispatching -> received
                       -> rejected
                       -> outcome_unknown
```

| 中断窗口 | 必须行为 |
| --- | --- |
| prepared，尚未进入 dispatching | 可以在剩余预算内继续；先持久化 dispatching 再发送 |
| dispatching，但尚未完整落盘响应 | 保守标记 outcome_unknown，任务 interrupted；重启、resume 或新 generation 都不能绕过阻断再发 |
| 完整响应已落盘，Worker 尚未收到 | 按同一请求摘要返回缓存响应，供应商请求次数不增加 |
| Worker 已写 durable 响应，工具正在处理 | 沿用 P1 工具回执恢复；不重新生成另一份模型答案 |
| 报告已提交，Worker 尚未收到工具回执 | 返回原报告回执，发布完成记录仍为一条 |

完整响应与 usage/预算结算应同事务落盘，再向 Worker 返回已确认结果；响应缓存保留能使同一 adapter 重建相同工具调用 ID 和参数的必要数据。若同一逻辑请求对应不同 payload/profile，返回冲突，不使用旧缓存冒充新请求。

默认不对未知结果自动重试。明确未发送的失败可按预算重试；401/403、schema 错误或模型标识不符直接失败。429/5xx 也不能假定绝未生成或计费，只有该供应商已验证的契约允许安全重试时才启用。引擎自身 retry 与 SDK retry 都必须受 Host 总账本控制。

供应商 request ID 仅作关联，不能未经验证当作幂等键。若支持结果查询/幂等，独立验证后再作为对账路径；否则展示未知状态并停止，本轮不承诺远端推理 exactly-once 或强杀即不计费。

## 6. 流式响应、工具参数与业务完成

第一版采用 Host 有限缓存：供应商响应完整接收并落账后，才回传为可执行的 assistant 响应。可展示“正在推理”等状态，但不把未落盘 token 当作已确认结果。JSONL 继续有版本和记录大小限制；大响应分帧，限制累计字节、帧序号和连接期限，不能通过单个超大字段绕过 P1 的 1 MiB 限制。

SSE 断开、半截 UTF-8/JSON、未结束的 tool arguments、finish reason 为截断或响应 schema 不符，均不得执行工具或标为 completed。完整函数参数仍需经原有 Host 权限和 schema 检查；严格输出格式不替代本地授权校验。此处按官方 [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling) 和 [Claude streaming](https://platform.claude.com/docs/en/build-with-claude/streaming) 的协议形态设计，具体 dialect 在选定供应商后只实现其中一种。

报告应使用结构化来源清单与事实/推断标记，再渲染为 Markdown。Host 验证来源 ID/SHA 是否属于任务快照，以及是否已成功读取全部必要来源。文本事实正确性仍需用固定题目和人工核对验收，不能仅因结构合格就称为分析正确。

completed 必须同时满足：durable 正常结束；真实模型至少一次完整有效响应已落账；没有未知模型结果、未处理权限拒绝、取消或预算错误；必要来源读完；报告事务完成；报告契约校验通过。失败不得静默切回 faux，再把任务标成真实模型成功。

对外事件只记录状态、摘要、用量和来源映射；不输出认证头或原始敏感错误。完整上下文与响应仅保存在受权限保护的任务存储中。输出 mode、模型标识、已确认推理状态和 usage 可用性，缺失的 usage/费用显示 unknown，不能填零。

## 7. 首版预算建议

以下是待实施的可信配置，不能由 goal 或客户端扩大；不是已启用能力。

| 项目 | 建议 |
| --- | --- |
| 并发 | 沿用一次一个执行中的任务 |
| 总期限 | 仍默认 30 秒；若模型延迟需要，可在现有可信配置允许的 5–60 秒范围内选 60 秒，并写入 profile |
| 模型发出次数 | 每任务最多 4 次，所有重试都计数；记录跨 Worker/Host 重启持久化 |
| 输出 | 每次最多 1024 output tokens；累计最多 4096；保留报告 32 KiB 上限 |
| 工具 | 继续最多 12 次，仅两个原有工具；模型轮次不得超过原 8 次上限 |
| 费用 | 第一次付费验收前明确币种、价格版本与任务费用上限；没有预算不启动付费验收 |
| 未知 usage | 保留最坏预算预留；不自动释放，不按零费用继续调用 |

输入做字节数与上下文预算预检，超限显式失败，不偷偷扩大上下文或省略授权来源。token 与费用的累计账本必须跨恢复保留，禁止重启后清零。价格不能确认时标为未知，首次验收先确认可执行的费用控制方式。

五任务验收还需设置整个批次的总费用上限，不能仅靠每任务上限；任一未知结果保留预留费用后，剩余额度不足就停止后续任务。

## 8. 实施顺序与产物

1. **P2.0 接口验证**：选定单一 profile；验证 custom fetch/ProviderStreams、稳定 generation ID、SSE 完整性和 AbortSignal。产物为最小协议夹具与选择记录，不接真实业务工具。
2. **P2.1 Host 治理**：实现 ModelBroker、请求/响应账本、缓存、预算、未知结果阻断、profile 指纹和数据库迁移。保留 offline 默认；升级 pin 必须审查新增运行时代码。
3. **P2.2 确定性故障测试**：用本机假供应商制造错误与强杀窗口，观察服务端请求计数。模型夹具标为测试模式，不能计入真实模型成功数量。
4. **P2.3 单项目真实验收**：在已授权的供应商、文档和预算下运行五个全新 request_id，并执行成功任务的重复提交/重启读取，生成完整证据与限制说明。

预计修改点：`packages/atlas-runtime/src/` 的模型传输、模式与 generation 关联；`integrations/atlas_bridge/` 的 broker/账本/迁移/完成契约；`integrations/tests/` 的假供应商与故障夹具；SDK/CLI 的状态和对账展示；runtime pin、验收脚本与文档。此清单是规划，本次不修改这些运行时代码。

## 9. P2 验收标准

所有强制项都通过，才可标为 P2 通过；“大部分成功”不等于验收完成。

| 编号 | 场景 | 通过条件 |
| --- | --- | --- |
| A1 | 基线回归 | P1 20/20、现有 P0/上游 109/109、根检查通过；工具/路径/进程限制不回退 |
| A2 | 模型配置与凭据 | 客户端无法改 provider/model/URL/headers；Worker 环境、配置、文件、日志、事件、报告均无真实凭据；非法/缺失凭据失败 |
| A3 | 网络通道 | 非白名单、重定向、私网/metadata、未授权代理全部拒绝；所选 adapter 的模型请求全部经过 broker，无隐式网络回退 |
| A4 | 工具越权与注入 | 假供应商强制返回 bash、其他项目路径、附加参数等调用均被拒绝，无越权副作用；不依赖模型自愿遵守提示 |
| A5 | SSE 与参数 | 断流、半截 JSON/UTF-8、超大帧、重复/乱序帧、截断 finish reason 均无工具执行、无伪 completed |
| A6 | 模型响应提交后强杀 | Host 已落账、Worker 未收到时强杀并重开；返回相同响应/工具参数，假供应商请求总数不增加 |
| A7 | 请求发出后结果未知 | 请求已受理而响应未落账时强杀；显示 outcome_unknown；连续两次重启和 resume 均不产生第二次发出，预算不重置 |
| A8 | 报告提交后强杀 | 只存在一份报告、一条发布完成记录；重复操作回执一致；新内容冲突拒绝 |
| A9 | 重复任务与指纹 | 并发 16 次同请求仅一个任务；换 goal/profile/model/mode 冲突；offline 旧缓存不成为 live 成功证据 |
| A10 | 预算、取消与错误 | 超时、费用/用量上限、401/403/429/5xx 均按分类停止或受控处理；Host 取消/强杀回收 Worker，本地停止不冒称远端未计费 |
| A11 | 真实模型任务 | 五个新任务各有真实完整模型响应证据，5/5 达到 completed、来源正确、至少两条事实可人工核查、建议标识明确；每任务只一份报告且项目文件不变 |
| A12 | 恢复与观测 | 重开后 profile/模型/模式、事件序号、回执、用量和报告哈希一致；缺 usage 明示 unknown；无静默 faux 降级 |

五次真实任务不是可靠率估计；它们是功能与证据验收。安全故障必须用可控制夹具确定性复现，不能以五次未触发问题代替权限测试。

## 10. 回退与置信度

默认保持 P1 offline profile，live 必须显式启用。可以停止新 live 任务，并使用原 offline profile 和新的 request_id 做诊断；不能把 live 失败/未知结果改写为 offline completed。回退保留原任务、请求账本、预算预留和未知状态，不清库重试。P2 数据迁移需有备份和校验；P1 代码不直接打开不兼容的新 schema。

| 判断 | 设计置信度 | 依据与缺口 |
| --- | --- | --- |
| 保持 P1 工具与进程边界 | 90% | 既有测试可复用；新增网络通道仍需验证 |
| 请求缓存与未知结果阻断设计 | 85% | 明确本地事务和远端窗口；尚未完成模型故障注入 |
| 供应商/adapter 兼容性 | 75% | 源码有 transport 与 hook 扩展点；具体供应商、模型和协议尚未选定 |
| 整体实施方案 | 85% | 范围受控、验收可测；不代表真实模型接入已经成功 |

进入实现前需补齐的选择：已授权供应商与 model ID、凭据引用方式、允许外发的两份输入及费用上限。其余工作可先以假供应商完成，不依赖真实密钥或付费请求。

## 参考依据

- 仓库源码：`packages/ai/src/types.ts` 的 ProviderRequestOptions/ProviderStreams，`packages/ai/src/models.ts` 的 createProvider，`packages/durable/src/harness/types.ts` 的 GenerationHooks/HookApi，`packages/durable/src/harness/generation.ts` 的恢复与 retry 行为。
- [OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling)、[Responses streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events)。
- [Claude Streaming messages](https://platform.claude.com/docs/en/build-with-claude/streaming)。

官方文档只用于核对协议形态，不意味着已选择该供应商、开通账户或验证其幂等与计费行为。
