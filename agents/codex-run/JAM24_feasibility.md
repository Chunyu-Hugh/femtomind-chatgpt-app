# JAM24 复现可行性（限定当前 App 工具与本地文档）

结论：**当前授权工具集不足以独立完成 JAM24 复现；未找到现成 JAM24 配方的文档证据。** 这不等同于认定底层物理代码绝不支持 JAM24；本次仅做文档调查，未提交 JAM24 计算，未修改任何代码。

`ladder_overview` 明确给出 JAM25（arXiv:2510.11979v2）的四步 π DY → LN → π 格点 → NA3/K 阶梯、246 点及相应论文验收值。`orchestrate_jam25` 的授权参数只有 seed/partition，不能选择 JAM24 配方；状态与对照工具也没有文档说明可切换至 JAM24。系综波不解决论文配方缺失，且本任务禁止使用。

要复现 JAM24，尚需：

- 明确 JAM24 的具体论文标识、版本、数据选择、截断、参数化、理论阶数与先验；所读资料未给出 JAM24 规格，不能仅凭年份推断研究对象。
- 经验证的 JAM24 输入卡和阶梯，以及配套数据库、理论模块、网格和运行环境的可用性证明。
- App 中可选择并提交该配方、追踪并收集各步结果的受支持接口；当前获准的 JAM25 编排接口未说明这种能力。
- JAM24 专属论文 χ²、矩/PDF 等适用观测量与误差基准，以及可复核的自动对照流程。JAM25 目标不能替代。

证据：`fitpack-workflow/README.md` 描述通用提交、追踪、χ²/PDF 后端架构，但未声明 JAM24 支持；`docs/OCTOBER_REVIEW_CHECKLIST.md` 与 `docs/DEEPSEEK_HARNESS_PROPOSAL.md` 是部署/演示与第二前端规划，未提供 JAM24 科学配方。`/root/femtomind/repro-jam20/stage_logs/JAM25/{paper_spec,L2_DESIGN,L2_RUNLOG,REPRODUCTION}.md` 记录 JAM25 配方与版本差异，说明即便同一论文也必须区别发表版 246 点和内部 308 点配置，不能直接改年份复用。

路径说明：任务所列 `repro-jam20` 不在 App 仓库内，实际文档位于上面的同级仓库路径；已读取该目录全部 Markdown。调查范围内没有足够证据证明 JAM24 可直接运行。补齐上述条件后可重新评估，当前不阻塞 JAM25。
