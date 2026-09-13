# FemtoMind 工作流状态机（JAM 迭代方法论的形式化）

> 本文档是"如何把 JAM 方法论写进 UI 与 MCP"的唯一事实来源。
> 方法论：**跑一步 → 确认参数 → 加/去实验 → 往复，直到最佳组合**
> （JAM 多步策略，arXiv:2501.00665 Sec. IV.1；用户表述的迭代版）。

## 状态与转移

```
                    ┌────────────────────────────────────────────┐
                    ▼                                            │
  FRESH ──run──▶ STAGE_RUNNING ──complete──▶ STAGE_REVIEWED ─────┤
                                              (χ²/N + 参数确认)    │
                                              │   │   │   │       │
                    append(加实验) ◀──────────┘   │   │   │       │
                    adjust(去/调实验) ◀────────────┘   │   │       │
                    redo(重做本阶段) ◀──────────────────┘   │       │
                    finalize(定稿) ──────────────────────────┘       │
                                              │                    │
                                              ▼                    │
                                          FINALIZED ──iterate──▶ ──┘
```

- **FRESH**：无历史。`suggest_next_stage` 返回 Stage A = idis + 默认数据集。
- **STAGE_RUNNING**：作业在集群上。`get_task_status`/`sync_statuses` 监控。
- **STAGE_REVIEWED**：本阶段完成。**确认参数** = 逐数据集 χ²/N、Z-score
  （`compute_summary_bf`→`get_summary_bf_result`）+ 拟合后参数
  （`get_current_parameters`）+ 阶梯快照（`get_fitting_chain`，历史带 χ²）。
  人在此拍板：加 / 去 / 重做 / 定稿。
- **FINALIZED**：`compute_batch_prior_tool`（批次平均后验）或
  `assemble_paired_prior_tasks`（α+β Stage B 配对）；想继续迭代就回到循环。

## 每个转移的工具语义

| 转移 | 工具调用 | 数据集语义 |
|---|---|---|
| run | `run_fitting_job` fresh_start=true | 本阶段清单 |
| append（加实验） | `run_fitting_job` fresh_start=false | **完整累计清单**（旧+新）；后端继承上一阶段每副本后验 |
| adjust（去/调实验） | `run_fitting_job` fresh_start=false | **修改后的累计清单**；同一后验继承（往复的反向） |
| redo | `redo_reaction` / `apply_drop_replicas` 后再提交 | 单反应道重做或剔除坏副本 |
| finalize | `compute_batch_prior_tool` / `assemble_paired_prior_tasks` | 定稿 |
| reset | `reset_fitting_chain` | 推倒重来 |

## 方法论编码的三个层面

1. **MCP（agent 面）**
   - 状态：`get_fitting_chain`（历史每阶段带 χ²/N、副本数、summary 状态）
   - 决策：`suggest_next_stage`（方法论即函数：下一步反应道 + 累计清单 + 放开什么参数）
   - 执行：`run_fitting_job`（fresh/continue）+ drop/redo/reset/finalize 工具
   - 指引：FastMCP `instructions`（10 步工作流地图 + 迭代循环 + 护栏）
2. **助手（对话面）**
   - app 提示词：STAGED FITTING 段 + THE LADDER ITERATES 段
   - `ladder_status` / `next_stage_suggestion` 转发工具
3. **UI（人面）**
   - composer v4：六步向导 + Stage plan（每阶段注明放开什么参数）+ Review 页
     **Append stage** 勾选 + **阶梯状态行**（当前步 / 上一步 χ² / 建议下一步）
   - ops 面板：Campaign 页（含 JAM24 默认值）、Collections/Analysis 看结果
   - 独立网页兜底：`/app/composer`、`/app/panel`（Claude Code 等任意平台）

## 标准阶梯（suggest_next_stage 内置表）

| Stage | 反应道 | 轨道 | 本阶段放开 |
|---|---|---|---|
| A | DIS | α | 海对称（S₁=S₂, δ=0） |
| B | DY | α | δū, δd̄ |
| C | W/Z（wzrv/wasym/zrap） | α | W/Z 味分离 |
| D | jets | α | 大 x 胶子 |
| E | SIA | β | S₂, δs, δs̄ + 碎裂函数 |
| F | SIDIS | α | PDF×FF 多重数 |
| G | W+charm | α | 奇异夸克 |

（JAM24 卡片：`inputs/jam24/input_jam24_s{1..7}.py`，与上表一一对应。）
