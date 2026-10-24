# P2.0：假供应商协议与故障验证

日期：2026-10-24（America/Los_Angeles）。基于已发布的 `9e67d1a` 规划继续验证。

**结论：P2.0 的固定 Responses 协议路径通过验收。无真实模型推理、无真实供应商请求。** 正式 P1 入口仍为 controlled_offline；新增验证入口仅支持 model_fixture，不是完整 P2 生产实现。

工程置信度 **90%**，限于当前 Windows、Node 24.14.0、pi-durable/pi-ai 1.4.0 和本机 Responses 假服务。真实供应商、HTTPS/代理、费用治理和正式迁移仍未验收，不能由此推断真实模型兼容性或生产安全性。

## 本次实现

```text
合成项目 -> ProbeHost（显式实验入口）
  -> 原 P1 任务接纳、文档快照和工具网关
  -> Windows 单进程 Job + Node permission
  -> pi-durable + 原版 pi-ai Responses adapter
  -> 自定义 fetch -> JSONL v2 -> 宿主 FixtureBroker
  -> 127.0.0.1 随机端口假服务 -> 完整 SSE 校验并缓存
  -> 分帧 + 长度/序号/SHA 校验 -> adapter 解析
  -> read_document -> publish_report -> 一份事务报告
```

- Worker 只持有虚拟的 `https://atlas-fixture.invalid/v1` 地址和无权限占位值；实际端口与假服务凭据仅在 Host。假服务凭据是本次测试生成的数据，不是用户的供应商凭据。
- 原版 HTTP adapter 被真实执行，包括分段函数参数、文本、tool_call ID 与 usage。没有用 faux provider 冒充这一协议验证；假的是 HTTP 服务返回的模型内容。
- Worker 的默认 fetch 被 fail-closed 保护，正常链路记录 native_fetch_calls=0、broker fetch_calls=3；假服务记录三个由 Host 发出的请求。
- 先完整接收并校验 SSE，再保存响应和 usage，事务提交后才交给 Worker。半截响应不进入工具执行。
- JSONL v2 限制单记录 1 MiB、base64 数据帧 64 KiB、模型响应累计 256 KiB；验证多帧序号、长度、SHA-256 与跨 UTF-8 边界重组。
- 请求身份使用 `beforeRequest` 的 durable taskId，并通过 memo 持久化。本版本 taskId 为数字，协议显式转为字符串；恢复时不能重新分配随机请求身份。
- 实验账本 `probe_models` 以 task_id/generation_id 和 profile/payload 摘要关联响应，收到完整结果后可返回原缓存；同身份换内容冲突。遗留 dispatching 转为 outcome_unknown 并阻止再次发出。
- SDK 和 durable 两层自动重试都关闭；四次模型请求上限保存在账本，重启不会清零。缺 usage 保留 unknown，不以 adapter 的默认零值替代未知用量。
- AbortSignal 经 Worker -> JSONL model_abort -> Host socket shutdown 传播，假服务观察到连接关闭；随后回收 Worker。

## 保持的 P1 边界

以下正式文件与版本锁未修改：P1 Host、HTTP 入口、Windows 句柄/Job 实现、P1 Worker、P1 engine-lock、package-lock 及 ai/durable/chord/telemetry 内核源码。

仍只有 read_document 和 publish_report 两种工具；文件授权、同句柄读取、输入快照、报告事务和 Job 单进程限制继续使用 P1 实现。未增加 shell、项目写入、第三方扩展、远程 MCP 或动态工具发现。

ProbeHost 仅接受 project_id=fixture，FixtureProfile 只包含本机端口与明确的 fixture 凭据；服务端还需返回 fixture 标识头。P1 的正式配置和四字段任务请求都不能选择此实验模式。没有将实验 Host 注册到公开 HTTP API。

P2 使用单独的 `p2-probe-lock.json`（351 个文件），不刷新 P1 pin。pin 覆盖被使用的固定内核、P1 边界实现与新增模型传输/实验 Host。它是可信本机源码的一致性检查，不是对恶意本机修改的签名防护。

## 15 个测试用例

| 用例 | 通过条件与结果 |
| --- | --- |
| 正常 HTTP adapter / generation memo | 三个模型请求完成读取、发布和结束；来源 SHA 正确；身份与 durable taskId 一致；一份报告 |
| 响应提交后杀 Worker | 第一请求只收到一次；恢复用缓存；整任务服务端总请求为 3，而不是 4 |
| 响应提交后强杀整个 Host | 新 Host 恢复相同 generation；总请求仍为 3 |
| 在途强杀 Host | 服务端已受理一次；连续两次重启仍为一次；任务 interrupted/outcome_unknown，无工具副作用 |
| 报告提交后杀 Worker | 重开后报告与回执一致，产物仍一份，总模型请求 3 次 |
| AbortSignal | 收到 model_abort、宿主连接关闭、Worker 回收、无报告、服务端仅一次请求 |
| 9 种非法响应 | 无终止事件、未完成 item、非法/重复键 JSON、非法 UTF-8、截断、错误模型、终止后追加事件、超大响应全部无工具执行 |
| 4 种 HTTP 错误 | 401/403/429/500 各只请求一次，不隐式重试，不产生报告 |
| 3 类工具越权 | bash、附加路径参数、未读来源就发布全部失败且无副作用 |
| 3 类 IPC 损坏 | 错序、错误校验和、超大帧均失败，不执行工具 |
| 多帧合法 UTF-8 | 每个模型响应超过一帧，重组和中文报告正确 |
| usage 缺失 | 三条缓存记录均保留 unknown |
| 请求内容冲突 | URL/header 覆盖、模型变更、相同 generation 换 input 被拒；服务端计数不增加 |
| 请求预算持久化 | 4 次请求后重开，第 5 次拒绝；服务端仍只有 4 次 |
| 正式入口不能启用实验 | mode/provider/fixture_profile 配置拒绝，任务附加 mode 字段拒绝，真实项目 ID 不能打开 ProbeHost |

测试用例中包含多项 subtest，表中仍按 **15 个测试用例**计数，不将子场景或重复复验累加成独立测试数。强杀/畸形响应导致任务 failed/interrupted 是预期断言，不是把失败任务计作成功任务。

## 验收证据

| 最终复验 | 结果 |
| --- | --- |
| P2.0 协议与故障测试 | 15/15，假服务共收到 47 次 HTTP 请求 |
| P1 边界与适配回归 | 20/20 |
| pi-durable 回归 | 59/59 |
| 根目录完整代码检查 | exit 0 |
| 真实供应商请求 | 0 |

- [可随仓库复查的验收摘要](validation/p2-0-2026-10-24.json)包含逐用例请求次数、源码摘要、固定版本及未验收范围。
- P2.0 最终协议测试：`.artifacts/atlas-p2/verification-b70fuzld/summary.json`、`protocol.log`、`observations.jsonl`，15/15。
- 示例产物：同目录 `fixture-report.md`，明确标注 model_fixture / 无真实模型推理；输入是合成项目，不包含实际 Atlas 私有资料。
- P1 回归：`.artifacts/atlas-p1/verification-p7u87ndn/summary.json`，20/20。
- durable 和根检查：`.artifacts/atlas-p0/run-Pj9X70/summary.json`，分别为 59/59、exit 0。最终复验路径汇总在 `.artifacts/atlas-p2/evidence-index-2026-10-24.json`。
- 版本化测试入口：`integrations/run_p2_probe.py` 与 `integrations/tests/test_p2_protocol.py`；运行证据及数据库仍由 Git 忽略。

## 复验

在已准备好 P1 固定依赖和 catalog 数据的 `pi-plus` 下执行：

```powershell
python integrations/run_p2_probe.py
python integrations/run_p1_checks.py
node scripts/atlas-p0-run.mjs durable check
```

测试运行器只把最小白名单环境传入子进程，不读取用户真实供应商凭据。源码变更后的 fixture pin 必须审查再由 `python integrations/lock_p2_probe.py` 显式更新；正常测试不会自动重置 pin。

## 已知限制及后续

1. FixtureBroker 是本机实验实现，不能通过移除 fixture 限制直接当作正式 ModelBroker。P2.1 仍需生产 profile、完整上下文/schema 重建、TLS/域名/DNS/代理控制、迁移与密钥管理。
2. 本轮验证响应缓存、未知结果阻断和持久化请求次数。没有实现正式 token/费用预留、实际账单对账或远端取消保证；一次 HTTP 请求不等于远端只计费一次。
3. 坏 SSE 在 Host 的完整缓存校验阶段被拒，未交给工具执行。正常协议确实经过上游 adapter，但没有据此宣称覆盖所有供应商的事件变体。
4. Job Object 与 Node 权限不构成网络/恶意原生代码沙箱。实验使用可信固定代码、合成输入和本机假服务，未测试真实云端、其他 OS、断电或磁盘损坏。
5. 正式 Atlas 的 P1 仍为 controlled_offline；网页/MCP 未增加 P2 模式。本轮不等于 P2 真实模型五任务验收通过。
6. 根检查保留既有 npm min-release-age 配置 warning 和 Node SQLite experimental 提示；未更新任何依赖。

下一步应进入 P2.1，将已验证的传输/身份/缓存行为迁移到正式治理层；继续用假服务验收完整 profile、迁移、预算及错误分类，再选择供应商进行真实模型验收。

## 置信度

| 结论 | 工程置信度 |
| --- | --- |
| 当前固定 adapter 的 IPC/HTTP/SSE 正常路径 | 95% |
| 已测强杀窗口的缓存与未知结果处理 | 90% |
| P1 原工具、文件与进程边界保持 | 90%，以回归与原文件差异校验为依据 |
| P2.0 阶段结论 | 90% |
| 任意真实供应商或正式生产 P2 能力 | 尚未验收，不赋予成功率 |

这些是基于本机测试覆盖的工程判断，不是统计可靠率或安全认证。
