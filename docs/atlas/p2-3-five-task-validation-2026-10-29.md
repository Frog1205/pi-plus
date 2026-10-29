# P2.3：五任务真实验收（合成资料受控范围）

日期：2026-10-29（America/Los_Angeles）。结论：**5/5 通过**。按用户授权执行五个全新 request_id，DeepSeek 3 个、MiniMax 中国站 2 个，逐个运行；全部使用同一合成 README 和已固定的提示词。实际 Atlas 私有文件、默认 API 的 live 集成和生产可靠性不在本次范围内。

## P2.2 仓库交付

P2.2 代码、测试和文档共 24 个文件已提交并推送到 main：[705284bb2515621eac59abe9a962e18053ca8e5e](https://github.com/Frog1205/pi-plus/commit/705284bb2515621eac59abe9a962e18053ca8e5e)。推送后重新核对远端 SHA 一致，才开始 P2.3 付费调用。

本轮完整 `npm run check` exit 0：Biome 检查 1,569 个文件，未修正源码；固定依赖、运行时导入、TypeScript 导入、入口图、安装锁、tsc 与浏览器 smoke 全部完成。执行使用清除真实凭据的包装脚本 `node scripts/atlas-p0-run.mjs check`，证据为 `.artifacts/atlas-p0/run-M1WOOu/`。保留既有 npm min-release-age 配置告警，未升级 npm 或依赖来消除提示。

## 五个新任务

| request_id | 实际供应商/模型 | 完整响应 | 状态 | 费用上限估算（元） |
| --- | --- | --- | --- | --- |
| p23-2026-10-29-ds-01 | DeepSeek / deepseek-flash | 3 | completed | 0.0087400 |
| p23-2026-10-29-mm-01 | MiniMax 中国站 / MiniMax-M3 | 3 | completed | 0.0194334 |
| p23-2026-10-29-ds-02 | DeepSeek / deepseek-flash | 3 | completed | 0.0079960 |
| p23-2026-10-29-mm-02 | MiniMax 中国站 / MiniMax-M3 | 3 | completed | 0.0199668 |
| p23-2026-10-29-ds-03 | DeepSeek / deepseek-flash | 3 | completed | 0.0081400 |

五个 request_id 在批次开始时均不存在，最终生成五个不同 task_id、15 条完整供应商回执和五份报告，没有复用旧任务充数。每任务有 read_document 与 publish_report 各一条完成操作，报告与发布回执 SHA 一致。

逐份人工核对：至少两条可查事实对应合成 README，d1 和原样 SHA-256 正确，建议与事实分开，正文标注 model_live；没有将来源中的合成事实包装为生产部署结论。五个任务是同一流程的重复验证，不代表五类业务能力或复杂项目分析质量。

## 重复操作与重开复核

除每次原 CLI 的重复提交/再次运行及关闭重开检查外，又针对五个任务做一次统一复核：

| 复核项 | 结果 |
| --- | --- |
| Host 关闭重开后的任务、profile、mode、模型、预算与账本 | 5/5 一致 |
| 事件的内容、ID、序号与顺序 | 5/5 一致，均为连续的 21 条事件 |
| 工具操作与发布回执 | 5/5 一致，各两条完成操作 |
| 数据库正文、报告 SHA 与供应商原始回执 SHA | 5/5 正确 |
| 重开后重复 submit 与 run | 5/5 返回原任务，额外模型请求 0 |
| 同 request_id 修改 goal | 5/5 返回 request_content_conflict / 409，无账本、事件或费用变化 |
| 未知结果、缺失 usage | 均为 0 |

统一复核前后，两家整个任务清单、请求次数和累计预算完全一致。本次没有强杀付费中的模型请求；在途强杀、断流、取消与未知结果阻断仍以 P2.2 的确定性夹具故障证据为依据。

## 累计预算与边界

| 供应商 | P2.3 新增估算（元） | 包含 P2.2 的累计估算（元） | 原授权上限（元） |
| --- | --- | --- | --- |
| DeepSeek | 0.0248760 | 0.0401640 | 5 |
| MiniMax 中国站 | 0.0394002 | 0.0578676 | 5 |
| 合计 | 0.0642762 | 0.0980316 | 10 |

费用依据已保存 usage 与冻结上限价格计算，**不是实际扣款或供应商账单证明**。本次复核官方价格与协议后继续使用原 profile；不按缓存、时段或折扣降低预留以扩大预算。[DeepSeek 价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[MiniMax 中国站价格](https://platform.minimax.cn/docs/pricing/overview)。

沿用原状态目录与 batch_id，没有新建费用账本或清空历史预留。P2.3 前后，当前 runtime pin 均为 `d1a42c72ed6a626578be6a8120a10f8ddded15368488d09989f06313f46b88b0`，366 个文件一致；运行时代码、依赖与权限未改。

单 Host 单项目，每家使用已授权同一合成源的固定副本；仅 read_document、publish_report 两工具，60 秒任务期限和模型/工具上限保持。未外发 Atlas README.md、server.py 或其他私有资料；未启用 shell、项目写入、跨项目工具、浏览器、远程 MCP 或自动供应商切换，默认 API 仍为 controlled_offline。

## 证据与复现

- [机器可读验收摘要](validation/p2-3-2026-10-29.json)：五个真实任务、模型用量、费用、逐任务报告 SHA 和重开快照摘要。
- `.artifacts/atlas-p23/live-2026-10-29/<request_id>/`：逐任务 acceptance、private-evidence、原始 CRLF 导出、正文 SHA 一致的 report-lf.md、Host 输出与单步复核。
- 同目录 `plan.json` 记录批次前原账本状态与五个新 ID；`summary.json` 和 `private-final-snapshots.json` 保存统一复核结果。
- 同目录 `baseline/<provider>/` 保留旧 P2.2 元数据。每次取出 P2.3 证据后恢复原元数据导出，校验其字节未变；**没有恢复或替换 SQLite 数据库**。数据库持续保存全部旧任务和新增任务。
- 两家原状态目录保持 `.artifacts/atlas-p22/live-2026-10-26/<provider>/state/`，目前共 8 个已完成任务、24 条模型回执；其中本轮为 5 个/15 条。

五任务均调用已发布的 `integrations/run_p22_live.py`，例如：

```powershell
python integrations/run_p22_live.py --provider deepseek --live --request-id p23-2026-10-29-ds-01
python integrations/run_p22_live.py --provider minimax-cn --live --request-id p23-2026-10-29-mm-01
```

当前已完成任务使用相同 ID 复查不会产生新的模型发出；不要通过新 ID 把失败或未知结果伪装成恢复。单独查看任务账本可使用不需要模型密钥的 `reconcile_provider.py status`。

## 置信度与限制

**本轮受控范围工程置信度 90%。** 依据为五个新真实闭环、15 条完整回执、五次统一重开与冲突复核、全部冻结源码校验和本轮完整代码检查。安全故障依赖此前已通过且源码匹配的 33 项协议/故障与 4 项 CLI 对账证据；P2.1 31 项、P2.0 15 项、P1 20 项、P0/上游 109 项为历史回归，本轮未重复运行或累加测试数量。

五个样本不是统计可靠率。尚未核验供应商账单、远端查询/幂等、远端取消、厂商 SSE 流式、真实项目资料或多项目/长期记忆能力。个别模型标题沿用 P2.2 或出现 mode_live 字样，正文 model_live、来源与事实契约通过；阶段与实际 mode 由 Host 账本确定，不能依赖模型自己生成的标题。

P2.2 已发布；本报告、P2.3 摘要与导航更新纳入本次文档发布。JSON 的 limitations 保留完成验收时“文档尚未提交”的历史说明，不作为文档发布后的仓库状态。
