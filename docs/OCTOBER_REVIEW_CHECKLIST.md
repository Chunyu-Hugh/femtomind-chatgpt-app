# ChatGPT 侧达标清单 — 对照 FemtoMemory 会议记录

*整理于 2026-08-23。目标：十月 AMSC 现场评审（Rockville）前，ChatGPT 插件满足
FemtoMemory 各次会议记录中的全部要求。状态：✅ 完成 / 🟡 仓库内可做 / 🟣 需外部协调。*

## A. 会议记录要求 → 落点

| 会议要求 | 出处 | 状态 | 落点/责任人 |
|---|---|---|---|
| ChatGPT 插件分发（"第一个 DOE 格点 QCD AI app"），OAuth 鉴权 | Aug 7 | ✅ | 插件本体 + OAuth 2.1 已就绪 |
| 复用用户既有 SSH 会话调度作业（JLab/ALCF/NERSC） | Aug 14 | ✅（JLab）/🟣（ALCF/NERSC） | 两跳 ControlMaster 已实现；多设施是 Nesar/Felix 的 IRI 路线 |
| app 启动时自动 clone 仓库 + 建环境，免终端 | Aug 14 | ✅ | `setup_workspace` + `_push_overlay`（overlay 改动需重推老工作区） |
| 常驻后端 + 轮询，用户笔记本不必在线 | Jul 24 | ✅（后端常驻）/🟡（主动推送） | ChatGPT 侧已满足；**推送**属值班层，见 §C |
| 持久、外部可达的 MCP 服务器（年度交付物） | Aug 21 | 🟣 | JLab 网络安全审批（Robert/Jie ↔ Andy Koskin/Roger）+ AWS 托管（Chunyu） |
| 单写者 job-status DB | Aug 21 | ✅ | 后端单写者未破坏；修复保持只读边界 |
| 安全从"信 prompt"到基础设施级 | Aug 14 | ✅/🟡 | OAuth+PKCE+身份作用域已就绪；本轮补了 auth 限流、XSS 转义、后端归属校验；**公开发布的隐私政策/合规**待 Robert 定 |
| token 节省作为成功指标（position paper） | Aug 7/14 | 🟡 | 本轮加了 `FEMTOMIND_USAGE_LOG` 调用计数；出数需要真实使用一段时间 |
| 九月进度报告（Overleaf+Git） | Aug 7 | 🟣 | 团队（Gustav 要） |
| 十月评审演示（部署的应用 + 自动化流程 + 安全协议 + token 指标） | Aug 14/21 | 🟡 | 见 §B 演示脚本 |

## B. 十月评审演示脚本（待彩排）

一条 15 分钟可中断、可复述的链路，全部在 ChatGPT 内完成：

1. **Connect**：面板 Connect 页输入集群账号 + 两跳凭据 → "HPC connected"。（若评审现场无网，用提前录屏 + 实时 `check_hpc_connection` 兜底。）
2. **Project**：展示"未选工作区 → 列出候选 → 一键选定"的护栏（新 `check_project` 流程）。
3. **Campaign**：选 idis（SLAC/BCDMS/NMC…）+ dy（E866…）混合反应道 → Review 显示分组（idis: n / dy: m）→ Submit → 真实提交 10 副本小 fit。
4. **Collections**：看进度推进；故意提交一个黑名单数据集被前端拒绝的演示（护栏可见）；对一个失败副本点 "Why?" 展示 `explain_failure` 从集群日志抓原因。
5. **Analysis**：完成后画 68% 分位带 + 分位数摘要（`band`），指出 "N still fitting" 标注与战役选择器。
6. **收尾指标**：展示 `FEMTOMIND_USAGE_LOG` 汇总（每步调用数/延迟）作为 token 节省素材。

**评审前必须完成的外部件**：AWS 公网部署 + 证书（FEMTOMIND_PUBLIC_HOST 真实域名）、OAuth 流程在公网域名下走通一遍、评审账号的邀请码、以及 JLab 审批的进展说辞（即使未批，也要能讲清当前"每用户 ControlMaster"如何兜底）。

## C. 值班/推送层（N1 的"主动通知"部分）— 会后做，不阻塞评审

会议记录（Jul 24）的"常驻+轮询"在 ChatGPT 侧已经成立：后端常驻、用户笔记本离线、状态靠轮询恢复。缺的是"收敛/失败时主动推送到手机"。ChatGPT 平台无法承载（查证结论：Tasks 不调用用户 MCP connector、无 webhook）。方案已写：
`chunyu/2026-08-23_184352_background_oncall_layer_needs.md`（FemtoMemory）。**执行顺序在十月评审之后**，用 cron 小脚本起步，评审演示中可一句话带过"已完成设计"。

## D. DeepSeek Harness 提案（第二阶段）

见 `docs/DEEPSEEK_HARNESS_PROPOSAL.md`——同一后端接入 DSH 的 API 使用方案。**本阶段只写提案，不动手实现**，待 ChatGPT 侧验收后再向组里提出。
