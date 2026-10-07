# 连接 ChatGPT 到 LQCDMCP 网关 — 操作手册(照着做)

目标形态(不需要 AWS):

```
ChatGPT ──OAuth──▶ 前门(femtomind_app.py,一台常开机器)
                    └──转发──▶ lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp
                                 └──▶ qcd24s100 网关 ──▶ ifarm 真拟合
```

---

## 第 0 步:选一台常开机器

要求很低:Linux、python 3.10+、能出网(HTTPS 443)。**不需要公网 IP**
(第 3 步用 cloudflared 隧道)。实验室机器 / 你现在这台 / 学校 VM 都行。
唯一硬要求:**别关机**。

## 第 1 步:装代码

```bash
git clone git@github.com:Chunyu-Hugh/femtomind-chatgpt-app.git
cd femtomind-chatgpt-app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 第 2 步:配置

```bash
cp .env.example env.conf    # 如果还没有
# 至少确认:
#   FEMTOMIND_PUBLIC_HOST 留空即可(tunnel 模式下按请求域名自适应,不行就填 tunnel 域名)
#   FEMTOMIND_AUTH_DB 指向 auth.db(要保留老用户就拷贝过来)
```

造一个新邀请码(给每个人发一个):

```bash
.venv/bin/python scripts/femtoadmin.py invite "你的名字"
# 会打印:邀请码(形如 FM-XXXX-XXXX-XXXX)+ 服务器 URL
```

## 第 3 步:起服务 + 开隧道

```bash
./scripts/run.sh tunnel
```

它会:
1. 启动 `femtomind_app.py` → `http://localhost:8010/mcp`
2. 开一个 cloudflared 快速隧道,输出形如
   `https://xxxx-xxxx.trycloudflare.com` 的**公网 URL**

> 快速隧道每次重启 URL 会变。演示够用;长期用就注册个 cloudflare
> 域名绑固定 tunnel(或让 IT 给域名+Caddy,见 deploy/MIGRATION.md)。

## 第 4 步:在 ChatGPT 里注册连接器

1. 打开 ChatGPT(网页或桌面 App)→ 设置 → **Connectors / MCP 连接器**
   → 添加新连接器
2. **Server URL** 填:`https://xxxx-xxxx.trycloudflare.com/mcp`
   (第 3 步拿到那个 URL + `/mcp`)
3. **Authentication** 选 **OAuth**
4. 保存 → ChatGPT 自动做 7 步 OAuth 流程 → 浏览器弹出
   **"FemtoMind access" 授权页**
5. 在页面上粘贴第 2 步的**邀请码** → Continue
6. 回到 ChatGPT:连接成功,37 个工具出现(含 7 个 `jam_*`)

> 如果第 4 步报 "Couldn't create MCP app" 或元数据错误,多半是
> ChatGPT 探测不到 `/.well-known/openid-configuration` — 确认隧道 URL
> 后面带 `/mcp` 且前门服务在跑(`tail /tmp/femtomind-chatgpt-app.log`)。

## 第 5 步:开面板,签进集群(每次网关 job 重启做一次)

1. 在 ChatGPT 对话里让模型打开 **Operations/FemtoMind 面板**
   (或者直接调用工具)
2. **Connect 标签**:填
   - `gateway_code` = PIN + 一次性码(如 `2026` + 8 位)
   - `ifarm_password` = 集群密码
3. 提交 → 面板显示两条链都 open(主链 + `lqcdmcp_gateway`)

> LQCD 网关那边每个 6 小时 job boot 需要一次新 OTP;
> 且网关 job 必须在 RUNNING 状态(预约窗口 ~每天 10:00 开始,
> `python3 redeploy_jam.py status` 可查)。

## 第 6 步:对话里直接用

- "看一下网关状态" → `jam_gateway_status`
- "提交 step7 warm 拟合" → `jam_submit_fit(card=input_jam24_s7_warm.py, outdir=..., prior_msr=...)`
- "现在 χ² 多少" → `jam_read_chi2`
- "对比论文 band" → `jam_evaluate_fit`

## 常见问题

| 现象 | 处理 |
|---|---|
| 授权页打不开 | 隧道没起/URL 打错;`run.sh tunnel` 重跑拿新 URL |
| ChatGPT 报 OAuth 错误 | 确认 Server URL 是 `.../mcp`;确认 auth.db 可写 |
| 面板 Connect 失败 | OTP 过期(30s 级)或网关 job 不在 RUNNING |
| jam_* 报 disconnected | 网关 job 到期了 → dashboard 重新 launch → 面板重新 Connect |
| 隧道 URL 变了 | ChatGPT 里删除旧连接器,用新 URL 重加 |

---

# 附:用 Claude Code 连接(已实测通过 2026-10-05)

Claude Code(命令行)接受裸 HTTP MCP,**一条命令,URL 直连**:

```bash
claude mcp add --transport http jam-gateway \
  https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp
claude mcp list          # 应显示 ✔ Connected
claude                   # 会话里问 "list your MCP tools" / "ping the jam gateway"
```

实测:✔ Connected,pong 返回正常,12 个工具全部可见。

要点:
- 不需要 Authorization header(/cloud 路由不查鉴权)
- 网关需在 RUNNING(6h job 窗口;`redeploy_jam.py status` 可查)
- HPC 工具(connect_hpc/submit_jam_fit/read_chi2/evaluate_fit)需要先
  `connect_hpc` 建两跳链(每条 OTP 管一个 boot)
- Claude Desktop 的"连接器"界面对裸 HTTP 服务器预检不稳定;
  Claude Code CLI 是同一条 URL 的可靠路径

## 方式 B — Claude Desktop(已实测通过 2026-10-06,走 443 隧道)

**重要结论(实锤):Claude Desktop 的出网策略会拦 `lqcdmcp.jlab.org:8000`
(非标端口)—— 请求根本到不了网关(网关日志里零记录),而同一台 Mac 上
curl / Claude Code 直连都通。换 443 端口的隧道 URL 后立即可用。**

步骤:
1. 起一条 443 隧道直连网关:
   ```bash
   cloudflared tunnel --url https://lqcdmcp.jlab.org:8000 --no-autoupdate
   ```
   记下输出的 `https://xxxx.trycloudflare.com`
2. Claude Desktop → Add custom connector:
   - URL: `https://xxxx.trycloudflare.com/cloud/jam-gateway/mcp`
   - 预检报 Couldn't reach → 点 **Continue anyway**
   - Authentication → **No sign-in**,headers 留空
3. 对话里启用该连接器 → "ping the jam gateway" → 回 pong 即通

注意:trycloudflare 快速隧道 URL 重启会变;正式使用建议换成固定域名
(Cloudflare 命名隧道 / Jie 平台挂 443),否则每次重启要重加连接器。

1. 打开 Claude Desktop → Settings → Developer → **Edit Config**
   (macOS 是 `~/Library/Application Support/Claude/claude_desktop_config.json`;
   Windows 是 `%APPDATA%\Claude\claude_desktop_config.json`)
2. 粘贴:

```json
{
  "mcpServers": {
    "jam-gateway": {
      "type": "http",
      "url": "https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp"
    }
  }
}
```

   (Authorization header 可以不加 — `/cloud` 路由不查鉴权,今天实测过。
   要加的话 Bearer token 会过期,反而添乱。)
3. 保存 → 重启 Claude Desktop
4. 点聊天框下方的工具图标(🔧),应看到 12 个工具:
   `ping / connect_hpc / check_hpc / submit_jam_fit / read_chi2 / evaluate_fit …`

## 方式 B — Claude Code(命令行)

```bash
claude mcp add --transport http jam-gateway \
  https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp
```

验证:

```bash
claude mcp list        # 应列出 jam-gateway
claude                 # 进入会话后说 "ping the jam gateway"
```

## Claude 下的使用流程

1. 先确保网关 job RUNNING(`redeploy_jam.py status`)
2. 对话里让 Claude 调 `connect_hpc`(需要 PIN+一次性码 + ifarm 密码,
   会写在对话里 — 注意这会经过 Anthropic 服务器,演示可以,生产要换
   面板式输入)
3. 之后 `submit_jam_fit` / `read_chi2` / `evaluate_fit` 随便用,
   OTP 只在每个 6h boot 要一次


---

# 附:DeepSeek Harness 注册 jam-gateway(实测通过 2026-10-07)

在 `~/.dsh/profiles/web/cordis.patch.yml` 和 `headless/` 同名文件里追加:

```yaml
- insert:
    - id: mcp-jam-gateway
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: jam-gateway
        transport: streamable-http
        url: 'https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp'
        toolCallTimeoutMs: 600000
        failOnStartupError: false
```

然后 `sudo systemctl restart dsh-web`(或对应 profile 的服务),新会话里即出现
`mcp__jam-gateway__*` 工具。实测:ping/check_hpc/cluster_status/chain_state 全部可用。
