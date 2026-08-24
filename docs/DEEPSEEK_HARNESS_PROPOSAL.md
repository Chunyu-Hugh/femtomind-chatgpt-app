# 第二阶段提案：FemtoMind 后端接入 DeepSeek Harness（API 使用）

*提案（未实现）。前提：第一阶段（ChatGPT 插件达标 + 十月 AMSC 评审）验收后再启动。
本提案的目的：向组里说明"同一套 MCP 后端，第二前端几乎零成本"。*

## 一句话

FemtoMind 的 22 个 MCP 工具 + fitpack-workflow 后端不动一行，通过 DeepSeek
Harness（dsh）自带的 MCP 客户端，以 API 方式接入——后台值班、无人值守监控、
自动推送这些 ChatGPT 平台给不了的能力，由 dsh 补上。

## 已验证的技术事实（2026-08-23，基于 dsh 0.1.0-rc.7 包清单与文档）

| 事实 | 证据 |
|---|---|
| dsh 自带 MCP 客户端，支持 `streamable-http` 传输 + 静态 Bearer 头 + HMR 热重载 | `@deepseek-ai/dsh-mcp-client` README |
| 接入即得 `mcp__femtomind__<工具>` 命名空间，22 个工具全部可用 | 同上（工具命名一节） |
| `@` 提及机制是插件框架的一部分 | `@deepseek-ai/dsh-client-ui-input-trigger`（`ctx.inputTriggers`） |
| 会话内定时唤醒（after/at/every，最短 5 分钟） | `@deepseek-ai/dsh-schedule` |
| 无人值守一次性任务（`dsh --profile headless "task"`，stdout 出结果、exit code） | `@deepseek-ai/dsh-headless` |
| 后台子代理 / 多智能体编排 / 后台 shell 任务 / 目标循环 | `dsh-tool-subagent` / `dsh-tool-workflow` / `dsh-tool-jobs` / `dsh-goal` |
| 凭据库 / 权限预设 / 技能 | `dsh-credentials` / `dsh-permission-presets` / `dsh-skill` |

## 接入方式（配置示例）

```yaml
# dsh 的 cordis.yml 中加一个插件行（agent preset 或 host 组合）
- id: mcp-femtomind
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: femtomind
    transport: streamable-http
    url: https://<FEMTOMIND_PUBLIC_HOST>/mcp
    headers:
      Authorization: !!js '`Bearer ${process.env.FEMTOMIND_TOKEN}`'
```

FemtoMind 的 OAuth 2.1 端点可脚本化一次性 mint（PKCE + 邀请码 → 90 天 token），
token 放进 `dsh-credentials` 或环境变量；不要求 dsh 做交互式 OAuth。

## 分层使用（对应会议记录需求）

1. **交互**：dsh 会话中直接调用 `mcp__femtomind__*` 工具，效果等同于 ChatGPT
   插件但没有面板 widget（工具卡渲染为结果文本/表格）。
2. **值班（N1）**：dsh-schedule 或 cron+headless 驱动只读工具轮询
   `get_fit_progress`/`list_campaigns`，收敛/失败时由 agent 最后一步
   `curl` ntfy/Telegram/Slack 推送——ChatGPT 给不了的"主动通知"。
3. **诊断**：失败时自动跑 `explain_failure`，产出自然语言日报写入
   FemtoMemory 仓库（记忆层的 git 约定复用）。
4. **安全边界（N3/N7）**：值班实例只挂只读工具面（`/mcp-ro` 或代理过滤），
   单写者约束保持不变；`dsh-permission-presets` 配置无人值守自动批准策略。

## 不做的事（本提案边界）

- 不移植 ChatGPT 的 ui:// widget（`window.openai.*` 桥是 ChatGPT 专有协议）；
  后续如需面板，按 dsh 的 `tool.call.toolview` slot 另写，不阻塞 API 使用。
- 不改变 fitpack-workflow 后端与 OAuth 体系——dsh 只是又一个 MCP 客户端。

## 启动条件（组内确认后再动手）

- [ ] 第一阶段验收通过（十月评审）。
- [ ] 定一个 dsh 部署位置（与 MCP 同机：AWS）。
- [ ] 确认值班推送渠道（ntfy / Telegram / 邮件）。
- [ ] 确认 token 管理方式（每协作者一 token，90 天轮换脚本）。
