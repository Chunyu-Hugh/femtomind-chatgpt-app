# JAM25 App 复现记录（运行中）

固定 seed=140701，partition=priority。仅通过 `http://127.0.0.1:8000` 的授权 App 工具代理计算，未直接访问 HPC/fitpack，未修改 App 代码，未启动系综波。首次 step 1 返回于 2026-09-11 14:17:39 UTC（文件时间）；6 小时上限为 20:17:39 UTC。后续编排调用在上次返回后至少等待 300 秒。

## 当前状态

前三步已完成；2026-09-11 14:42:59 UTC 提交 Step 4，job_id=10803995，stage=s4_running。尚未取得最终 step-4 msr，所有验收值待实测，不得以历史链结果替代。

## 验收表（待 compare_with_paper）

| 指标 | 论文目标 | seed 140701 实测 | 偏差 / z | 结论 |
|---|---:|---:|---:|---|
| 实验 χ²/N，231 点 | 0.80 | 待计算 | 待计算 | 待验收 |
| DY χ²/N，117 点 | 0.78 | 待计算 | 待计算 | 待验收 |
| LN χ²/N，108 点 | 0.87 | 待计算 | 待计算 | 待验收 |
| NA3 χ²/N，6 点 | 0.08 | 待计算 | 待计算 | 待验收 |
| 格点 χ²/N，15 点 | 0.65 | 待计算 | 待计算 | 待验收 |
| 总 χ²/N，246 点 | 0.79 | 待计算 | 待计算 | 待验收 |
| Table 1 格点矩 | 见 paper_spec.md | 待计算 | 待计算 | 待验收 |
| β π ū | 1.16 ± 0.04 | 待计算 | 待计算 | 待验收 |
| β K ū | 1.6 ± 0.2 | 待计算 | 待计算 | 待验收 |
| β K s | 1.2 ± 0.4 | 待计算 | 待计算 | 待验收 |
| PDF | 论文 member-0 | 待计算 | 待计算 | 待验收 |

矩与 β 的 z 定义为 (本次值−论文中心值)/论文所报 σ，|z|≤1 判为该论文误差尺度内。单链没有可估计的系综误差，不能将此解释为两系综差异的联合显著性。格点数据的误差与论文 JAM 后验误差不是同一分母，若工具给出前者须另行标识。χ² 目标没有所报 σ，不强行判断 1σ。

## 时间线与完整返回

- 初始本地读文件与 overview 请求均被默认命令沙箱阻止（`bwrap: setting up uid map: Permission denied`），没有执行。经自动权限审查通过后，使用同一 localhost API。
- `ladder_overview({})` 成功；首个调用未记录精确墙钟时间，发生在 step 1 之前。完整 JSON 值从工具输出保存在 [logs/overview_001.json](logs/overview_001.json)。
- 2026-09-11 14:17:39 UTC：`orchestrate_jam25({"seed":140701,"partition":"priority"})` 返回 `{"stage":"s1_running","step":1,"job_id":"10803224","hint":"call again to continue"}`。
- 首次日志写入命令 `python` 不存在，改用 `python3` 修复；该错误未阻止随后 App 调用，原始返回已保存。
- App 仓库下 `repro-jam20/stage_logs/JAM25` 不存在；定位到 `/root/femtomind/repro-jam20/stage_logs/JAM25` 并读取全部 Markdown。
- 后续所有 App 调用的开始/结束 UTC、参数、返回码、stderr 和完整响应实时追加至 [logs/timeline.jsonl](logs/timeline.jsonl)，原始响应分别存于 `logs/orchestrate_NNN.json`。

本地文献依据：`/root/femtomind/repro-jam20/stage_logs/JAM25/paper_spec.md`、`L2_DESIGN.md`、`L2_RUNLOG.md` 与 App `ladder_overview`。旧日志中“π β=1.01 与 1.16(4) 在 1σ 内”等表述算术不成立，本次不继承其验收判断。本文不把历史运行视为本 seed 结果。副任务见 [JAM24_feasibility.md](JAM24_feasibility.md)。

## 编排调用时间线（截至 step 4 提交）

| 次数 | 返回 UTC | stage | 作业号 | 说明 |
|---|---|---|---|---|
| 1 | 2026-09-11T14:17:39.893462+00:00 | s1_running | 10803224 | call again to continue |
| 2 | 2026-09-11T14:22:41.406167+00:00 | s1_running | 10803224 | still running |
| 3 | 2026-09-11T14:27:45.448583+00:00 | s2_running | 10803669 | call again to continue |
| 4 | 2026-09-11T14:32:47.043600+00:00 | s2_running | 10803669 | still running |
| 5 | 2026-09-11T14:37:50.428760+00:00 | s3_running | 10803802 | call again to continue |
| 6 | 2026-09-11T14:42:59.636113+00:00 | s4_running | 10803995 | call again to continue |
