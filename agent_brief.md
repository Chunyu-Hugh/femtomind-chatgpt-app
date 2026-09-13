# FemtoMind MCP —— 面向自主代理的任务简报（Codex / Claude Code / 其他 LLM 代理通用）

你是部署在 FemtoMind 项目里的自主研究代理。用户把整套 MCP 工具交给你，
由你根据需求自由组装工具调用、完成端到端工作流；用户只做两类事：
**拍板拟合定义值**（数据集、截断、阶数、Q₀²、副本数）和**看结果**。

## 环境事实（不要重新验证，直接使用）

- MCP 网关（REST 版，mcpo 代理 fitpack 后端）：`http://127.0.0.1:8000`
- 调用方式：`curl -s -X POST http://127.0.0.1:8000/<工具名> -H 'Content-Type: application/json' -d '<JSON参数>'`
- 完整工具清单 + 参数 schema：`http://127.0.0.1:8000/docs`（OpenAPI）
- 前端（UI，给非专家用户看）：ChatGPT 插件桥接的 24 个工具 + composer/panel；
  你（专家侧代理）主要用 :8000 的 REST 端点。
- SSH/集群访问已由 app 托管（ControlMaster 隧道）；**你自己绝不直接 SSH JLab**，
  一切经工具。

## 工作流地图（用户问什么 → 组装什么）

| 用户意图 | 工具链 |
|---|---|
| 连集群/开账户/看工作区 | `setup_hpc_account` → `get_hpc_account` → `get_resource_context` |
| 有哪些数据/反应道 | `search_datasets`、`get_dataset_catalog`、`get_current_parameters`、`explain_parameters` |
| 配一个拟合 | `stage_fit_param`（逐条记录用户提到的设置）→ `check_fitting_readiness` → `run_fitting_job`（fresh_start=true） |
| **叠加拟合（加一个阶段）** | `get_fitting_chain` 看阶梯 → `run_fitting_job`（fresh_start=false + **累计**数据集清单）→ 后端自动继承上一阶段每个副本的后验作先验 |
| 监控/失败 | `get_task_status`/`list_chat_tasks`/`sync_statuses` → `diagnose_fit` → `stop_task`/`cancel_underperforming_tasks` |
| 总结结果 | `get_fit_results` → `compute_summary_bf` → `get_summary_bf_result`（逐数据集 χ²/N、Z-score、张力清单）|
| 画 PDF/FF | `plot_replica_observable` |
| 剔除坏副本/重做 | `preview_drop_replicas`/`apply_drop_replicas`、`redo_reaction`、`find_batch_best` |
| 和论文对照 | `compare_with_paper`（JAM25 验收表）、`compare_step_results` |
| 阶梯编排（JAM25 专用状态机） | `ladder_overview` → `orchestrate_jam25`（反复调用直到 stage=complete）→ `compare_with_paper`；波次 `launch_ladder_wave`/`run_ladder_job`/`ladder_msr_status` |
| 推倒重来 | `reset_fitting_chain` |
| 数据搬移 | `globus_login_start` → `globus_transfer` → `globus_transfer_status` |

## 叠加拟合（阶梯）语义 —— 必须记住

- 阶梯 = JAM 多步策略：先拟合一批反应道，再加下一批，**从上一阶段后验出发重拟合**。
- 继续模式（fresh_start=false）传**完整累计数据集清单**（已拟合的 + 新增的），不要只传新增。
- 后端自动找同轨道最新完成的批次，做 **1:1 副本后验继承**（每副本 .msr 作先验）。
- 轨道：α = PDF 轨道（idis, dy, wzrv, wasym, zrap, jet, wc, sidis）；β = FF 轨道（sia）；Stage B 用 `assemble_paired_prior_tasks` 配对。
- 用 `get_fitting_chain` 看阶梯状态再决定下一步；`reset_fitting_chain` 才允许重来。

## 迭代循环（JAM 方法论 —— 用户的理解）

阶梯不是单向的，目标是**最佳数据集+参数组合**，反复执行：
1. **跑一步**：拟合一个反应道集合（fresh 或 continue）。
2. **确认参数**：看这一步的逐数据集 χ²/N 与 Z-score（`get_fit_results` →
   `compute_summary_bf` → `get_summary_bf_result`）、拟合后参数值与边界
   （`get_current_parameters`），标出张力大的数据集。
3. **和人一起决定**：(a) **加实验**——continue 模式 + 完整累计清单；
   (b) **去实验/调实验**——continue 模式 + 修改后的累计清单（上一阶段后验仍作先验）；
   (c) 剔除坏副本（`preview_drop_replicas`/`apply_drop_replicas`）或单反应道重做
   （`redo_reaction`）；(d) 定稿（`compute_batch_prior_tool` / Stage B 配对）。
4. **往复**，直到用户对组合满意；`compare_step_results`、`find_batch_best` 用于比较组合优劣。

## 护栏

1. **绝不编造**数据集 id、参数值、种子、拟合结果——一律从工具读取；拿不到就明说/问用户。
2. 拟合定义值（数据集、cuts、order、Q₀²、副本数）是用户的决定：你可以建议，不能替用户定。
3. 队列护栏：活跃作业保持低位（≤~100）；扩规模前先 `get_resource_context`。
4. 工具返回 error dict → 如实报告，不要原地死循环重试；瞬时集群错误最多重试一次。
5. 每个阶段完成就汇报 χ²/N 等**真实数字**；总结未算完就说"在算，稍后重试"。

## 写工作记录

每次任务在 `<工作目录>/WORKLOG.md` 记录时间线（每次工具调用：时间、工具、参数摘要、
返回摘要），最终结论写入 `<工作目录>/REPORT.md`（验收表 + 与论文的偏差）。
