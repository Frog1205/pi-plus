# P2.2：MiniMax 中国站真实验收补齐

日期：2026-10-29（America/Los_Angeles）。结论：**P2.2 本轮受控范围通过**。两家协议故障矩阵与本地对账已有通过证据，现已补齐 MiniMax 中国站真实合成任务。P2.3 五任务、真实 Atlas 项目资料接入和生产可用性尚未验收。

## 实际运行结果

用户确认中国站后，使用既有 MINIMAX_API_KEY，通过固定 `https://api.minimax.cn/anthropic/v1/messages` 调用 `MiniMax-M3`。当前官方文档确认该端点与模型，并支持关闭 thinking；本轮继续使用完整 Messages JSON、`thinking.type=disabled` 和 `max_tokens=1024`。[官方协议](https://platform.minimax.cn/docs/api-reference/text-anthropic-api)

| 验收项 | 实测 |
| --- | --- |
| 新增真实任务 | 1 个，completed，model_live / model_connected=true |
| 发出与完整响应 | 3 次确认发出，3 条完整回执，均为 MiniMax-M3 |
| 工具与报告 | read_document、publish_report 各 1 条完成记录，报告 1 份 |
| 来源正确性 | 人工核对两条事实，均对应合成 README 的 FACT-1 / FACT-2；来源 d1 与 SHA 原样正确 |
| 建议标识 | 单独“建议”段，明确说明不是来源事实 |
| 重复提交、再次 run | 同一任务与账本，未增加发出次数 |
| Host 关闭重开 | 账本、用量与回执保持一致 |
| 未知结果/用量 | 0；usage 可用 |
| 资料与权限 | 仅合成资料；项目源文件未改变；两个工具及 Worker/Host 边界保持 |
| 凭据 | Worker 文件、事件、诊断和公开证据未包含真实供应商密钥 |

数据库报告正文 SHA-256：`294e8fc29c87b5a4245049b80081f6aea54f0d52f927b0c3dcd532c91d625104`。

固定合成来源 SHA-256：`9894a42243bada062703e0539c54f64e65827399de6e5e2dc23505fc3e2828f1`。

## 费用与版本

| 供应商 | 累计已完成任务 | 完整响应 | 本地费用上限估算（人民币） |
| --- | --- | --- | --- |
| DeepSeek | 2 | 6 | 0.015288 |
| MiniMax 中国站 | 1 | 3 | 0.0184674 |
| 合计 | 3 | 9 | 0.0337554 |

估算来自已落账 usage 与冻结上限单价，不是供应商实际扣款证明。两家均低于各自 5 元与合计 10 元授权上限，没有清空历史账本或建立新批次重置额度。

MiniMax 中国站仍按每百万 tokens 输入 4.2 元、输出 16.8 元的未折扣标准价计算上限。2026-10-29 核对的当前标准档显示五折价 2.1/8.4 元；本轮输入远低于 512k 档位，不请求 priority，不将折扣节省作为增加预算的依据。[官方定价](https://platform.minimax.cn/docs/pricing/overview)

本轮未修改运行时代码、依赖、工具权限或版本锁。当前 provider pin 仍覆盖 366 个文件，摘要：`d1a42c72ed6a626578be6a8120a10f8ddded15368488d09989f06313f46b88b0`。

当前 pin 下有 DeepSeek 与 MiniMax 各一个已完成真实任务；另外保留安全补充前 DeepSeek 一个任务的独立原始 pin，不把该旧任务重新解释为当前版本验证。P2.1 的本地与远端 main 仍为 `48a23af9d3458d5468297b7debc505ff58196eda`。P2.2 尚未提交。

## 故障与回归证据

2026-10-26 已完成协议/故障 33/33、操作员 CLI 4/4、P2.1 31/31、P2.0 15/15、P1 20/20、P0/上游 109/109 及完整代码检查。本次重新逐文件核对 33 项与 4 项测试记录的全部源码 SHA、366 文件运行时 pin，以及既有阶段文件未改；**没有将历史测试写成今天重新执行**。

实际服务强杀、非法调用、断流、429/5xx、事务失败等故障由可控本机夹具制造。真实调用用于验证供应商协议、工具闭环、usage、重复提交与持久化读取；未向厂商主动制造错误来推断远端计费行为。

## 证据与复核

- [当前机器可读验收摘要](validation/p2-2-2026-10-29.json)：3 个任务、9 条回执、费用累计、账本和源码复核。
- [2026-10-26 历史报告](p2-2-provider-validation-2026-10-26.md)与 [历史摘要](validation/p2-2-2026-10-26.json)保留当时 MiniMax 未执行的状态。
- 当前真实证据：`.artifacts/atlas-p22/live-2026-10-26/minimax-cn/acceptance.json`、`private-evidence.json`、`report.md` 与 `state/atlas.sqlite`。
- 新证据索引：`.artifacts/atlas-p22/evidence-index-2026-10-29.json`。路径中的 `live-2026-10-26` 是原授权批次，实际执行日期见 acceptance 的 `time`；保留批次标识以延续预算。
- 账本复核为 SQLite 只读打开，完整性检查通过；全部 9 条供应商原始回执摘要正确，报告正文摘要与发布回执一致。

重新查看该任务无需模型密钥，亦不会发出模型请求：

```powershell
python integrations/reconcile_provider.py status --state .artifacts/atlas-p22/live-2026-10-26/minimax-cn/state --task-id 0bcae333-99d8-45ae-a925-c26354e6d0f3
```

本次执行命令如下；当前已完成批次下，相同 request_id 会返回原任务：

```powershell
python integrations/run_p22_live.py --provider minimax-cn --live --request-id p22-live-1
```

本机 Windows 导出的 report.md 使用 CRLF。acceptance 的 report_sha256 对应数据库内 UTF-8 正文，导出文件的物理字节摘要为 `c8a8f3b72a6b798aca1cd795048020614222630d21b4dc51d4830ac4561dd23d`；将换行标准化为 LF 后与数据库正文一致。两种摘要已分别记录，不能直接把物理文件摘要与正文摘要比较为“内容损坏”。本轮保留原始证据，未改动已固定的导出程序。

## 置信度与未验收范围

- **90%**：固定本机版本下的故障处理、权限边界与本地对账。依据为确定性故障测试与当前源码匹配。
- **85%**：两家供应商的受控真实联调。MiniMax 只有一个任务样本，不能据此估计长期成功率。
- 尚未验证厂商账单、远端结果查询/幂等、远端取消、厂商 SSE 流式、P2.3 五个新任务或实际 Atlas 私有文件分析。默认 API 仍为 controlled_offline。
- 本次 MiniMax 三条账本记录均未获取到可用的 HTTP provider_request_id，response_id 均已记录；不能假定每家都有可供结果查询的请求头标识。本轮无未知结果需要人工对账。
- 保留上一阶段 Windows 超大 HTTP 用例一次 WinError 10053 的观察记录；当时未改代码的完整复验 20/20，不能据此消除长期兼容性不确定性。
