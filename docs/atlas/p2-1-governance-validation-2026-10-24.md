# P2.1：宿主模型治理与假供应商验收

日期：2026-10-24（America/Los_Angeles）。P2.0 已先提交并推送到 `main`：`8241566d538b6b9aad3d3d32afc415b319c30300`。

本阶段实现正式治理逻辑，并通过独立的 `GovernedHost` 入口以 `model_fixture` 模式验收。真实模型请求为零，无真实推理；默认公开 API 仍使用 P1 `controlled_offline`。真实供应商启用、真实费用策略与正式 HTTPS 联调属于后续阶段。

## 实现与边界

```text
可信 ModelProfile + 显式本机 fixture grant
  -> GovernedHost 四字段任务接纳 / 快照 / 原子 provenance
  -> 原 P1 Windows Job + Node permission
  -> 固定 Responses adapter / durable generation memo / JSONL v2
  -> ModelBroker 重建请求上下文与工具 schema
  -> ModelLedger prepared / 预算预留 -> dispatching
  -> 本机假供应商 -> SSE 与增量一致性检查
  -> 响应、usage、预算结算同事务提交
  -> 原 P1 工具网关与报告事务 -> 完成契约
```

- `model_policy.py`：不可变 profile，包括模式、endpoint、固定模型/协议、来源范围、提示版本、完整 schema 指纹、凭据引用、网络策略、任务/批次预算、价格版本及期限。供应商实现还由完整 runtime pin 固定。
- `model_broker.py`：不信任 Worker 发来的整个请求；根据已接纳目标、授权来源清单、历史模型响应和已完成的工具回执重建上下文及 schema。只允许 JSON 文本序列化空白差异；额外 system 消息、伪造结果、URL/headers 覆盖均拒绝。
- `model_ledger.py`：请求身份由 task_id/generation_id 唯一确定，另有持久化 sequence、dispatch_count、profile/runtime/payload 摘要、response/request ID、响应哈希、usage、预算预留及错误类别。缓存先检查摘要，再返回相同响应。
- `governed_host.py`：只允许已提交模型响应中的原样工具调用进入原 P1 gateway；仍只开放 `read_document` 和 `publish_report`。新增报告来源 SHA 检查、持久化 12 次工具尝试上限、单任务执行槽和跨重启期限。
- `governed-worker.ts`：固定 registry、无 SDK/durable 隐式重试、无 compaction、禁止默认 fetch 回退；持久化 generation 身份。Worker 不持有实际端口或假服务凭据。

未修改 P1 Host、Windows 文件/Job 实现、HTTP 路由、P1 Worker、P1 engine-lock、P2.0 pin、package-lock 或固定内核。P2.1 独立 `governance-lock.json` 覆盖 356 个运行时文件；测试不能自行更新 pin。

## 请求、预算和恢复规则

| 状态/事件 | 规则 |
| --- | --- |
| prepared | 已原子预留预算、尚未发送；恢复只能继续同一个身份，不能换 generation 绕过 |
| dispatching | 先落账再发送；重启一律保守转为 outcome_unknown |
| received | 完整 SSE、usage 和预算结算同事务保存；之后才交给 Worker |
| outcome_unknown | 阻断该任务所有自动重发及新 generation，保留预算，需要对账 |
| rejected | 不自动重试，不能作为任务成功；保守保留预算 |
| 401/403 | 认证错误，停止；不向 Worker 暴露上游原始错误 |
| 429/5xx | 不能证明未执行，记 outcome_unknown；本阶段不自动重试 |
| 3xx | 拒绝，不跟随 Location |
| usage 缺失 | 显示 unknown，保留最坏预留；有剩余预算才可继续 |
| usage 超过预留 | 保存已知用量并拒绝执行；保留至少原预留与报告用量中的较大值，不假称远端硬限额生效 |

预算使用整数 `FIXTURE_MICRO` 测试单位，输入 1 单位/token、输出 2 单位/token，**不是任何供应商报价或实际账单**。输入使用完整重建请求的 UTF-8 字节数作为本夹具的保守计数上界；未将它当作任意真实模型的已验证 tokenizer。

每任务最多 4 次模型请求、每次 1024 输出 token、累计最多 4096；输入、任务费用、批次费用和 12 次工具尝试均持久化。批次身份绑定其总额、币种和价格版本，不能在同一批次 ID 下提高限额。预留在同一数据库事务内串行执行，防止并发透支。

期限绑定任务原始接纳时间，重启不重新计时。取消沿用 AbortSignal/IPC/socket shutdown 和 Job 回收；本地关闭连接不等于远端未推理或未计费。

## 迁移和历史状态

数据库迁移使用 SQLite backup API 生成含 WAL 的迁移前备份，并做 integrity_check；DDL、历史来源记录和版本号在同一事务内提交。故障时回滚，未来未知 schema 版本拒绝启动。

新表为附加结构，未修改 P1 原有表格式。历史任务明确标记 `controlled_offline`，provenance 为旧模型能力未验证；P2.0 实验任务也不能转成治理验收成功。新任务的 profile/mode/runtime 与 admitted 事件在原任务接纳事务中保存；换 profile 后旧任务保留原元数据，并拒绝继续运行或复用原 request_id。

治理入口使用独立状态目录，勿将 P1/P2.0 运行目录直接作为治理任务的运行目录。迁移测试用于证明保留与拒绝能力，不代表迁移会自动重新执行旧任务。迁移备份和任务数据库均留在本机，不提交 Git。

## 网络控制的验收范围

实际 HTTP 请求只发到可信配置指定的 `127.0.0.1` 假服务，要求单独的 fixture grant 和响应标识头；真实项目 ID、live 模式和客户端模式覆盖均不能启用此入口。凭据格式严格校验，响应反射凭据时在 Host 拒绝。

另实现 HTTPS 目标解析、DNS 全地址检查和连接固定：拒绝非 HTTPS/异常端口、userinfo/query/fragment、私网/loopback/link-local/metadata/multicast/IPv4-mapped IPv6；连接使用已校验数字 IP，同时 TLS 校验原 hostname。它采用直连原语，不读取环境代理。这部分只做确定性解析与 mock socket/TLS 验证，**没有发起真实 HTTPS 请求，未验证代理或真实证书链**；它尚未开放为 live 通道。

Job Object 与 Node permission 的能力边界不变，不宣称它们是完整网络沙箱或恶意原生代码沙箱。

## 复验与证据

```powershell
python integrations/run_p21_checks.py
python integrations/run_p2_probe.py
python integrations/run_p1_checks.py
node scripts/atlas-p0-run.mjs coding durable check
```

运行器使用最小环境白名单，不继承供应商密钥。P2.1 的日志、逐用例 HTTP 计数、账本摘要、示例报告和源码哈希写入被忽略的 `.artifacts/atlas-p21/verification-*/`。正常验收不会修改任何版本锁；审查运行时源码变化后才可显式执行 `python integrations/lock_governance.py`。

### 最终验收记录

| 验证组 | 结果 |
| --- | --- |
| P2.1 治理 | 30/30，其中 28 项宿主治理、2 项网络策略单元测试 |
| P2.0 协议回归 | 15/15 |
| P1 权限/隔离/API 回归 | 20/20 |
| P0 与上游回归 | 109/109：coding 23、durable 59、coding-p0 17、durable-p0 10 |
| 根目录完整代码检查 | exit 0，Biome 无修正 |
| P2.1 假服务 HTTP 请求 | 44 次，认证全部正确 |
| 真实供应商请求 | 0 |

30 项按 unittest 测试用例计数，未将 subtest 或反复复验加到总数。P0 的预期反例继续通过断言，不能把它们误读为底层已具有完整权限或远端幂等能力。根检查仍有既有 npm min-release-age 配置提示，运行时仍有 Node SQLite experimental 提示；未更新依赖。

覆盖要点：正常任务与重开元数据一致；16 并发同请求去重；profile/内容冲突；13 类请求/上下文/schema 覆盖拒绝；未提交模型调用和伪造工具回执拒绝；任务请求/输入/输出/费用/批次/工具预算跨重启；并发批次预留；prepared 恢复和新身份阻断；强杀 Worker/Host；报告提交后恢复；缺 usage；HTTP 分类；重定向、错误 usage、增量不一致、缺标识、凭据反射；取消、取消错任务隔离、期限与超时；迁移备份/回滚/未知版本；默认入口不能启用 live；DNS/IP/TLS 原语。

关键计数：响应提交后杀 Worker、杀 Host，或报告提交后杀 Worker，恢复后的整任务均只有 3 次模型请求，报告只有一份。在途强杀 Host 后两次重启及新 generation 尝试，假服务始终只有 1 次请求，预算预留仍在。prepared 尚未发送时恢复，只发 1 次并复用原预留。

- [版本化验收摘要](validation/p2-1-2026-10-24.json)：逐用例请求次数、源码哈希、固定 pin 与置信度范围。
- P2.1：`.artifacts/atlas-p21/verification-0mu75x9v/`，包含 `governance.log`、`observations.jsonl`、`summary.json` 与 `fixture-report.md`。
- P2.0：`.artifacts/atlas-p2/verification-pu3b7xxe/`；P1：`.artifacts/atlas-p1/verification-tk809ixx/`。
- 上游：`.artifacts/atlas-p0/run-ST1V4K/`；P0：`.artifacts/atlas-p0/run-EEB2DW/`；根检查：`.artifacts/atlas-p0/run-iZB4ny/`。
- 本机综合证据索引：`.artifacts/atlas-p21/evidence-index-2026-10-24.json`，对测试源码与当前文件逐一重算 SHA，并确认原 P1/P2.0 运行时文件和 pin 未变。

P2.0 提交已核对远端 `main` SHA。P2.1 源码、测试和文档本轮留在本机，未混入 P2.0 的提交。

## 置信度与后续

阶段工程置信度为 **90%**，限于当前 Windows、Node 24.14.0、Pi 1.4.0 与假供应商，以确定性故障、原边界回归及完整代码检查为依据，不是统计成功率或安全认证。真实供应商推理、实际费用硬限额、远端取消/幂等、HTTPS/代理端到端联调均未验收。

后续 P2.2 继续扩充故障矩阵和人工对账流程；P2.3 才在明确授权供应商、模型、资料外发范围和预算后做五个真实任务验收。当前实现不会通过更换 URL 或删除 fixture 判断直接启用真实调用。

## 2026-10-26 发布前修复与复验

本节更新上述 2026-10-24 本地阶段记录。发布前复验暴露了并发连接上的未加锁 deadline 查询，曾产生 SQLite InterfaceError 和错误行值。已将 deadline、完成条件读取纳入同一 Host 锁，并为模型线程存储错误增加有界错误返回；新增确定性的事务并发读取回归。

测试运行器原 240 秒总上限在本机触发，现保留逐行输出与部分日志、使用 900 秒测试进程上限；任务自身仍为 5–60 秒，不扩大运行时期限。根导入检查此前扫描了 `.artifacts` 中 3322 份临时 TS 源码并超时，已排除该 Git 忽略目录。

修复后 P2.1 **31/31**、完整代码检查 **exit 0**，P1/P2.0 运行时文件及其 pin 未变；P2.1 pin 经显式审查更新。失败尝试和成功证据分别保留，不覆盖为成功。[发布复验摘要](validation/p2-1-publication-2026-10-26.json)记录当前源码哈希及路径。

此快照用于提交 P2.1；真实模型仍未启用。后续 P2.2 将按另行确认的 DeepSeek/MiniMax、合成输入与预算推进，不改变本文夹具结论的范围。
