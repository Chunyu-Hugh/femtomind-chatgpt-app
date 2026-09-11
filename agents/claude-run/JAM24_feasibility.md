# JAM24 复现可行性评估（仅调研，未改任何代码）

> 作者：Claude 自主代理（seed 140702），2026-09-11。
> 依据：`ladder_overview` 工具返回、`/openapi.json` 工具清单、
> `fitpack-workflow/README.md`、`fitpack-workflow/campaign_tools.py`、
> `dataset_catalog.py`、`docs/`、`repro-jam20/stage_logs/JAM25/*.md`。

## 0. 先说口径问题（这是第一个缺口）

仓库里**没有任何文件把 "JAM24" 绑定到具体的 arXiv 编号**。唯一一处出现是
`fitpack-workflow/server.py:2249` 的路线图文案：把 JAM24 与 CT18、NNPDF4.0
并列为"从 DIS 起步的主流分析"。因此本评估针对的是**质子扇区的 JAM24 类全局分析**
（非极化 PDF，可能与 FF 联立），即 app 内 `get_analysis_roadmap` 的 Stage A→B。
若目标其实是别的论文，下面的结论需按其数据/观测量清单重算。

## 1. 从头复现需要的 7 件材料（从 JAM25 L2 复现总结出的模板）

参照 `L2_DESIGN.md` + `REPRODUCTION.md`，一次"从论文从头爬"的复现要齐备：
论文规格书 → database 数据 → obslib/qcdlib 理论模块 → 输入卡与阶梯设计 →
编排器工具 → 验收对照工具（含论文发表数字）→ 基准网格/系综。

## 2. 逐项盘点

| # | 材料 | JAM25 现状 | JAM24 现状 | 判定 |
|---|---|---|---|---|
| 1 | 论文规格书 | `paper_spec.md`（数据表/参数计数/验收数字全部提取） | 无对应文件，连 arXiv 号都未锁定 | ❌ 缺 |
| 2 | 实验数据 | dy-pion 4 / ln 5 / dy-kaon | 目录齐全：idis 53、dy 6、sia 64、sidis 30、jet 8、wzrv 17、zrap 9、wasym 2、wc 5、pidis 43、psidis 27、pjet 8、dy_qT 55（`search_datasets` 共 383 条），且 idis/pidis 已带 `Q2>1.69, W2>10` 标准切 | ✅ 基本够 |
| 3 | 理论模块 | 需自写 `obslib/lqcd_mom` + `qcdlib/pdfkaon`（overlay） | 质子扇区反应道属组内 fitpack 常规通道，**不需要 overlay 新模块** | ✅ 大概率够（须冒烟验证，见 §4） |
| 4 | 输入卡/阶梯 | 自研 4 步卡 `input_l2_s{1,3,4}.py` | 无卡。但通用链路可替代：`compose_dataset_config` + `generate_random_tasks` + `run_fitting_job` + posterior→prior 串链（`compute_batch_prior_tool`、Stage B 的 `assemble_paired_prior_tasks` 随机配对） | 🟡 可绕过 |
| 5 | 编排器 | `orchestrate_jam25` | **硬编码**：`campaign_tools.py:21-34` 写死 `L2=/w/ceph24/.../jam25repro/l2` 与 4 张卡名；无通用版 | ❌ 缺 |
| 6 | 验收对照 | `compare_with_paper` | **硬编码**：`PAPER` 字典（χ²/矩/β）+ 4 个 JAM25 专用集群脚本（`analyze_msr_remote.py`、`test_lqcd_mom.py`、`l2_pdf_cmp.py`、`beta_cmp.py`）。通用替代只覆盖 χ²：`compute_summary_bf`（bootstrap=False 的发表口径 χ²）+ `get_fit_results` + `diagnose_fit` + `plot_replica_observable` | 🟡 χ² 可，矩/β/网格不可 |
| 7 | 基准网格/系综 | JAM25pion/kaon_nlonll 701 成员 + 998 msr | 仓库内未见 JAM24 LHAPDF 网格；集群 `LHAPDF_DATA_PATH` 只挂 EPPS16 与 jam25repro 表 | ❌ 缺（带级验收无法做） |

## 3. 结论

**"有条件可行"，但今天用现有 app 工具达不到 JAM25 那一级的验收严格度。**

- **今天就能做**：用通用链路（`compose_dataset_config` → `run_fitting_job` →
  `compute_summary_bf`）跑 Stage A-α 的非极化质子 PDF 拟合，做到**χ²/N 级别**的
  对照。这不需要任何新工具、新代码。
- **今天做不到**：(a) 一键式、可断点续跑的 JAM24 编排器；(b) 矩 / 有效指数 β /
  LHAPDF 逐点网格三类验收；(c) 与论文系综的带级比较。
- 因此完成定义若按本次 JAM25 任务的标准（四组对照 + σ 级偏差表）来衡量，
  **JAM24 当前不可行**。

## 4. 补齐所需的最小动作（按优先级）

1. **锁定论文 + 写 `paper_spec.md`**：arXiv 号、数据集与点数、参数计数、切、
   发表 χ² 表与 Table 级目标数。没有这一步后面全是无锚点的。
2. **把 `campaign_tools.py` 的 JAM25 常量参数化**：`L2` / `LADDER` / `PAPER`
   改成"campaign 配置"入参，`orchestrate_jam25` 即可退化为通用
   `orchestrate_campaign`。工作量小，收益最大。
3. **写质子扇区的对照脚本**：现有 4 个远端脚本是 π/K 专用（格点矩、kaon β、
   pion 网格），JAM24 需要质子 PDF 的对应物。
4. **取得 JAM24 发表网格与 replica 系综**并挂到集群 `LHAPDF_DATA_PATH`。
5. **冒烟验证 sidis/sia/jet 通道在当前容器里真能跑**。
   JAM25 复现的 6 条障碍（`REPRODUCTION.md` §5：resman 里被注释掉的
   `obslib.DYpT` 导入、容器 `PYTHONPATH` 被 `--env` 覆盖、`LeadingBaryon` 依赖旧
   `_get_FXN` 等）说明"模块在仓库里"不等于"能跑"。**必须实测，不能假定。**

*本文件为纯调研产物；未修改任何代码，未提交任何作业。*
