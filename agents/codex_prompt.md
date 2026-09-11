# 任务简报：通过 FemtoMind App 自主复现 JAM25（并评估 JAM24 可行性）

你是被部署在 FemtoMind 项目中的自主研究代理。你的任务是通过
**FemtoMind App 的 MCP 工具（HTTP REST 接口）** 完成论文复现，不要直接
接触 HPC 或 fitpack——所有计算由 app 工具代理。

## 环境事实（不要重新验证，直接使用）

- App 的 MCP 工具网关：`http://127.0.0.1:8000`（正在运行）
- 工具调用方式：`curl -s -X POST http://127.0.0.1:8000/<工具名> -H 'Content-Type: application/json' -d '<JSON参数>'`
- 可用的复现工具（详见 `/docs` 的 OpenAPI）：
  1. `orchestrate_jam25`：编排器状态机。每次调用推进一格（提交一步 / 收集并
     推进下一步）。参数 `{"seed": <整数>, "partition": "priority"}`。
     必须**反复调用**直到返回 `"stage":"complete"`。中途返回
     `"status":"still running"` 或 `"hint":"call again later"` 时，等几分钟再调。
     若返回 `"diverged"`，换一个种子重新开始并报告。
  2. `ladder_msr_status`：查看阶梯 msr 状态。参数 `{}`。
  3. `ladder_overview`：查看阶梯配方与论文验收目标。参数 `{}`。
  4. `compare_with_paper`：对指定 msr 做论文对照（χ² 表 / 格点矩 / PDF / β）。
     参数 `{"msr": "<完整 msr 路径>"}`。
  5. `launch_ladder_wave`：系综波（本任务不使用，除非显式要求）。
- 你的种子：**140701**（与并行运行的其他代理隔离，勿改动）。
- app 已配置好 SSH（复用现有 ControlMaster 隧道），你**不需要也不允许**自己
  SSH 到 JLab。

## 主任务：复现 JAM25（arXiv:2510.11979）

1. 调 `ladder_overview` 了解阶梯与论文目标（χ²/N 总 0.79、DY 0.78、LN 0.87、
   NA3 0.08、格点 0.65；Table 1 矩；β 指数 1.16(4)/1.6(2)/1.2(4)）。
2. 反复调 `orchestrate_jam25`（seed=140701）直到 `stage=complete`，
   拿到最终 step-4 msr 路径。全程记录每次调用的返回（存到工作日志）。
3. 调 `compare_with_paper`（用第 2 步的 msr），收集四组结果。
4. 对照论文目标写验收结论：exp χ²/N 是否 ≈0.80？NA3、格点、矩、β 各自
   与论文的偏差？哪些在 1σ 内？
5. 把完整记录写入 `agents/codex-run/REPORT.md`（含每次工具调用的时间线摘要与
   最终验收表）。

## 副任务（尽力而为，不阻塞主任务）：评估 JAM24 复现可行性

- 仅用 `ladder_overview` 与 repo 内文档（`docs/`、`fitpack-workflow/README.md`、
  `repro-jam20/stage_logs/JAM25/*.md`）调查：当前 app 工具是否足以复现
  JAM24（若无，缺什么）。把结论写入 `agents/codex-run/JAM24_feasibility.md`，
  篇幅 ≤1 页，不得修改任何代码。

## 硬性规则

- 只调用上述 app 工具与本地读文件；不修改 app 代码、不投新工具、不 SSH。
- 每次工具调用之间若有等待，等待 5 分钟后重试；总时长上限 6 小时。
- 诚实报告：任何失败、重试、发散都要记录，不得编造结果。
- 工作目录：`agents/codex-run`（所有产物写入这里）。

## 完成定义

- `agents/codex-run/REPORT.md` 存在，含完整验收表与时间线；
- REPORT.md 中明确写出：exp χ²/N 的值、与论文 0.80 的偏差、格点矩 z 值、
  β 指数与论文的 σ 差距；
- `agents/codex-run/JAM24_feasibility.md` 存在（允许结论为"不可行"）。
