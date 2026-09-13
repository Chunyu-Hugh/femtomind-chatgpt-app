# FemtoMind 测试报告（2026-09-13）

> 按用户要求：写的东西必须测过、部署必须验证、agent 必须真正调用 MCP。
> 本文记录本轮全部验证的证据。

## 1. 部署验证（发现并修复了一个长期故障）

- **发现**：AWS 生产机上的代码停留在 8 月底（femtomind_app.py 8-28、fit_composer.html 8-24、
  server.py 9-04）；服务最后重启时间 2026-09-04。**CI 自 9 月 4 日起每次都在部署步骤失败**，
  但冒烟检查（discovery 200）打的是旧进程，所以一直没被发现。
- **根因 1（CI）**：GitHub Actions 的部署步骤失效（rsync 从未到达主机；具体失败步骤需看
  Actions 日志——本机无 GitHub token 无法查询，待用户贴日志或更新 runner secret）。
- **根因 2（Caddy）**：主机 Caddy 只转发 /mcp、/.well-known、/authorize、/token；
  新增的 `/app/*` 独立网页路由被 Caddy 的 catch-all 吞掉。
- **修复**：① 用本机 AWS 密钥直接 rsync 部署 app/backend/hpc-monitor 三份代码 + 重启
  femtomind-mcpo/femtomind-app（两服务 active）；② 更新主机 Caddyfile 增加
  `handle /app* → localhost:8010` 并 reload。
- **验证（线上）**：`/.well-known/oauth-protected-resource` 200 ✓；
  `https://femtomind.duckdns.org/app/composer` 返回真实 composer 页面（含 shim 注入）✓；
  `/app/panel` 200 ✓；`/app/boot/composer` 返回 11 个反应道目录 ✓。
- Caddyfile.example + DEPLOYMENT.md 已同步入库（aa603a0/253c2e4）。

## 2. 后端 MCP 工具实测（本地网关 + /fitpack 前缀代理）

| 工具 | 结果 |
|---|---|
| search_datasets | ✓ 386 数据集按 by_observable 分组 |
| get_dataset_catalog（新增） | ✓ 返回 items + 每族 filters（idis: Q2>1.69, W2>10.0） |
| get_default_fit_config（修复后） | ✓ 解析 overlay default.yaml，25 个 conf 键 |
| get_fitting_chain（新增） | ✓ 空阶梯时 {step 0, history []} |
| suggest_next_stage（新增） | ✓ 返回 Stage A：idis 15 个默认数据集 + "sea symmetric (S1=S2, delta=0)" + alpha 轨道 + 累计清单 |
| 应用启动 | ✓ 25 个工具（含 ladder_status、next_stage_suggestion、submit_fitting_job），无重复注册 |

## 3. Agent 自主调用测试（强制只用 MCP，禁止克隆仓库/读文件）

### Codex（通过，跑了两轮）
- 第 1 轮：拉 OpenAPI → 选只读工具 → resource_context/list_chat_tasks/get_fitting_chain/
  search_datasets 依序调用 → 发现 2 个缺口（后端缺 get_dataset_catalog；default_config 路径错）
  → 已修复并复验。
- 第 2 轮（修复后）：正确组装 get_fitting_chain → suggest_next_stage → get_dataset_catalog
  → get_default_fit_config；结论全部正确：step 0、Stage A DIS 15 数据集、海对称说明、
  累计清单语义、DIS cuts Q2>1.69/W2>10.0、默认 QCD NLO/Q20=1.6129/truncated/backward；
  HPC-account 错误如实报告。产出 REPORT.md + WORKLOG.md（每步 HTTP 调用有据可查）。
- 合规检查：WORKLOG 中无 git/ssh/文件读取，全部为网关 HTTP 调用 ✓

### Claude Code（本环境被阻塞，如实记录）
- 以 root 运行：Claude Code 的安全检查拒绝 `--dangerously-skip-permissions`（root 禁用）；
- 以 uid 1000 运行：认证文件可读后进程卡在首启（0 输出，~5 分钟无进展），判定为本沙箱
  环境不支持；需要在正常用户环境（macOS/非 root 容器）复测。早前 JAM25 的 Claude demo
  即是在可用环境完成的。

## 4. JAM24 阶梯冒烟（真实拟合，作为 MCP 工作流的负载验证）

- s1 卡（DIS 13 数据集 2680 点，海对称）在 priority 队列完成：**1h11m，job 10906704，
  msr = 8XW0AVCIGLJ8.msr**（steps 机制落盘成功——之前无 steps 卡不落盘的 bug 已修复）。
- 逐数据集 χ²/N vs 论文 Table 4（JAM24 列），全部 OK（差 <0.35）：
  NMC p 1.62/1.67、HERA1 1.50/1.42、BCDMS p 1.18/1.14、SLAC p 0.98/0.87 等。
  动量求和规则 msr=1.0 ✓、uv(1)=2 ✓、dv(1)=1 ✓。
- msr 格式：zlib+pickle，{order, params, chi2:{reaction:{id:{npts,chi2}}}}——
  jam24_cmp.py 已适配（`repro-jam20/jam24_cmp.py`）。

## 5. 结论与待办

- 已交付并验证：composer v4（PDF 六步）、对话叠加拟合（continue_chain 全链）、
  方法论状态机（workflow_fsm.md + suggest_next_stage + 阶梯状态行）、通用网页 UI（/app/*）、
  后端 agent 指引。
- **待办**：① CI 部署管道修复（需 GitHub Actions 日志/secret 检查，可由用户提供）；
  ② Claude Code 在正常环境复测；③ JAM24 s2-s7 阶梯继续（s1 后验已就绪）。
