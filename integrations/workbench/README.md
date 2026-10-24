# Atlas Brain Workbench

Atlas Brain 的最小可运行工作台：网页控制台、HTTP API、CLI 和 MCP stdio 适配器共享同一个事件驱动运行时。

## 启动

```powershell
cd D:\airesearch\AI原生开发\atlas-brain
python server.py
```

打开 <http://127.0.0.1:8765>。

对外产品说明书：`docs/Atlas-Brain-产品说明书.md`。

工作台首屏是 Atlas Map：架构主链、事件/记忆/蒸馏支撑层和节点 Inspector。图谱状态明确区分 `live`、`defined`、`simulated`、`planned`，不会把矩阵定义冒充成已上线能力。系统架构模式带有可暂停的分段讲解：播放、暂停、单步、速度和阶段拖拽都可控，流动轨迹只表示当前架构关系的说明，不代表一次真实模型运行。

`brain_catalog.json` 是从用户提供的认知矩阵生成的只读投影，包含 S0-S8、50 个操作、6 个质量门、domain slots、layer contract 和 corpus distillation 规则；不包含个人 signature 实例内容。

选择一次模拟实验后，运行流程模式会从 `/api/v1/runs/{run_id}/timeline` 读取带事件 ID、序号和 UTC 时间戳的记录，并按事件顺序回放。它展示的是当前运行时实际写入的模拟步骤，不代表真实模型推理。

## API

```powershell
curl http://127.0.0.1:8765/api/v1/status
curl http://127.0.0.1:8765/api/v1/brain
curl http://127.0.0.1:8765/api/v1/runs/{run_id}/timeline
curl -X POST http://127.0.0.1:8765/api/v1/runs -H "Content-Type: application/json" -d '{"goal":"归纳一个自主学习运行时"}'
```

## CLI

```powershell
python atlas.py status
python atlas.py run "归纳一个自主学习运行时"
python atlas.py timeline "<run_id>"
python atlas.py memories
```

## SDK

```python
from sdk import AtlasClient

atlas = AtlasClient()
run = atlas.run("分析任务")
atlas.timeline(run["run_id"])
```

## MCP

将 `mcp_server.py` 作为 stdio MCP 进程启动。当前提供 `atlas_status`、`atlas_run`、`atlas_run_timeline`、`atlas_memory_search` 四个工具。

以上模拟运行的数据暂存内存，升级只生成候选，不自动修改认知核心。

## P1 受控任务底座

P1 通过固定版本 pi-durable 接入独立的 `/api/v1/tasks`，使用 SQLite 保存任务、输入快照、操作账本和报告。当前模式为 `controlled_offline`，明确无真实模型推理；原模拟运行与网页回放继续使用原接口。

```powershell
python pi-plus/integrations/configure_atlas.py
python server.py --p1-config pi-plus/.artifacts/atlas-p1/atlas-local.json
$env:ATLAS_TOKEN_FILE = "$env:USERPROFILE/.atlas-brain/p1/auth.token"
python atlas.py task submit my-first-task atlas-brain README.md "核验项目说明"
python atlas.py task get "<task_id>" artifact
```

授权来自本机配置，仅开放两种工具：读取授权文档快照、向任务账本发布报告。Worker 使用 Windows Job Object 和 Node 权限限制。详细边界、测试与恢复流程见 `pi-plus/docs/atlas/p1-controlled-runtime-2026-10-24.md`。这尚不是任意扩展、跨项目或联网个人助理的生产底座。
