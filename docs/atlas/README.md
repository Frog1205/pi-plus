# Atlas Brain / Pi Plus

当前交付为 P1：Windows 本机单项目受控执行，模式 `controlled_offline`，**无真实模型推理**。只开放读取授权文档快照、向 SQLite 发布任务报告两种工具。源码基线及运行版本由 `packages/atlas-runtime/engine-lock.json` 固定；不随 Git HEAD 自动升级。

## 文档

- [总体路线与边界](pi-plus-foundation-plan.md)：初始设计；P0/P1 报告中的已验证结论优先。
- [P0 权限、恢复与重复执行实测](p0-foundation-validation-2026-10-24.md)。
- [P1 实现、验收与启动](p1-controlled-runtime-2026-10-24.md)。
- [P1 仓库交付验收摘要](validation/p1-2026-10-24.json)。

## 独立运行 P1 测试

环境：Windows，Node **24.14.0**，Python **3.13.12**。执行前安装锁定依赖，并按 P0 的固定 catalog revision 准备模型元数据。安装不运行依赖生命周期脚本。

```powershell
npm ci --ignore-scripts
$pin = Get-Content nix/model-catalog.json | ConvertFrom-Json
$catalog = Join-Path $env:TEMP ($pin.revision + '-models.all.json')
Invoke-WebRequest -Uri ("https://pi.dev/api/models/revisions/" + $pin.revision + "?types=chat,image,classifier") -OutFile $catalog
if ((Get-FileHash $catalog -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pin.revision.Substring(7)) { throw 'Catalog SHA-256 mismatch' }
node packages/ai/scripts/hydrate-model-catalog.ts $catalog
python integrations/run_p1_checks.py
node scripts/atlas-p0-run.mjs durable
node scripts/atlas-p0-run.mjs check
```

上述下载使用仓库 `nix/model-catalog.json` 中的固定 revision，并先校验 SHA-256；下载的是模型定义，不会调用模型推理。离线机器应提前准备相同版本目录文件，并用同一校验与 hydration 命令加载。

P1 的 20 个测试使用合成项目和随仓库提交的 HTTP/SDK 适配器，不要求旁边存在完整 Atlas Workbench。HTTP 测试用一个惰性的旧 runtime 占位对象，仅测试受控任务路由；实际 Atlas 服务集成由单独的 `accept_atlas.py` 验证。运行证据写入被 Git 忽略的 `.artifacts/`，凭据、状态数据库和测试日志不提交。

## 接入现有 Atlas

把本仓库放在现有 Atlas 目录的 `pi-plus` 子目录。执行 `python integrations/install_workbench.py --check` 检查适配器是否匹配；需要安装时执行 `python integrations/install_workbench.py`。安装仅接受已记录基线，先备份再写入，发现父项目文件有其他修改会停止。

适配器不是完整 Atlas 应用副本；原有 `runtime.py`、静态工作台及其他父项目文件需要由 Atlas 自身提供。安装不会覆盖漂移文件来强行接入。启动、认证及实际项目验收命令见 P1 报告。

## 保留的边界

单项目、固定工具注册表、无 shell、无项目扩展加载、无项目写权限；Host 校验请求和每次工具操作，报告与回执在同一数据库事务提交。Windows Job Object 和 Node 权限共同约束 Worker。没有据此宣称网络隔离、外部系统 exactly-once 或生产沙箱能力。
