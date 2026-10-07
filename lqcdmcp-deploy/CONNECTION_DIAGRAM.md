# JAM Gateway 连接拓扑图

## 全景

```
[① 客户端]  你的电脑 / DeepSeek / agent_demo.py / FemtoMind app
     │
     │  HTTPS JSON-RPC (MCP 协议: tools/call)
     │  "connect_hpc" / "submit_jam_fit" / "read_chi2" ...
     ▼
[② LQCDMCP 平台]  lqcdmcp.jlab.org:8000   ← Jie Chen 的代理
     │  查注册表: jam-gateway → http://qcd24s100.jlab.org:8080/mcp
     │  纯转发,不碰 SSH、不管凭证
     ▼
[③ LQCD 集群节点]  qcd24s100   ← Slurm job 7326675(预约 chen_mcp_daily,6h)
     ┌──────────────────────────────────────────────┐
     │  jam-gateway MCP server (端口 8080)          │
     │  supervisor 循环: server 崩了 3 秒自动重启    │
     │                                              │
     │  ~/fm-cm/gateway = ControlMaster① (setsid)  │
     │  ~/fm-cm/ifarm   = ControlMaster② (setsid)  │
     └───────┬──────────────────────┬───────────────┘
             │                      │
     SSH ①:22 端口         SSH ②:本地 2222 端口
     凭证 = PIN+OTP        凭证 = ifarm 密码 ((密码))
             ▼                      │
   [④ scilogin.jlab.org] ──────────┘
     (跳板机;①顺手开了端口转发: 节点本地 2222 → ifarm:22)
                                           ▼
                              [⑤ ifarm 集群(科学计算农场)]
                              ┌──────────────────────────────┐
                              │ /w/ceph24 共享盘:             │
                              │   fitpack_legacy (拟合引擎)   │
                              │   jam24repro/... (卡片+结果)  │
                              │ sbatch → ifarm Slurm          │
                              │   → job 12367088 (apptainer)  │
                              │   → 写 progress.json + .msr   │
                              └──────────────────────────────┘
```

## 三个"为什么"

**1. 为什么要两条 SSH(①和②)?**
ifarm 的 22 端口对 LQCD 节点是防火墙关闭的;只有 scilogin 的 22 端口对
节点开放。所以只能走两步:节点 → scilogin(①),再借道 scilogin → ifarm(②)。

**2. 那个 2222 端口是什么?**
①连接 scilogin 时顺带开了一条端口转发:
`节点本地 127.0.0.1:2222 → ifarm.jlab.org:22`(数据从 scilogin 内部转发)。
于是②只需要 ssh 到"本机的 2222",实际就穿到了 ifarm。两跳变一条链,
ifarm 那边永远不需要再碰 OTP。

**3. 为什么计算在 ifarm,不在 LQCD 节点上?**
JAM 的 fitpack 引擎、数据卡片、/w/ceph24 全在农场(ifarm)一侧。
LQCD 节点只是"网关"——因为它上面跑着 Jie 平台派发的 MCP server job,
而 FemtoMind 的 agent 就是通过这个 MCP 接口说话。真正的科学计算
(sbatch → apptainer → maxlike_jam)全部发生在 ifarm。

## 凭证只出现一次

- OTP(PIN+8位码):只在每次 6 小时 job boot 里建 ControlMaster① 时用一次。
  之后 setsid 化的 master 常驻 `~/fm-cm`,server 重启/热更新都不丢。
- ifarm 密码:只在建 ControlMaster② 时用一次,同样常驻。
- `connect_hpc` 之后所有工具调用(提交/监控/评估)全部复用 master,
  零凭证。验证过:假凭证调用 connect 也返回 "reused"。

## 运维通道(两条,互不干扰)

```
[dashboard API]  lqcdmcp.jlab.org:8000/jlab/...  ← Bearer token
   作用: launch / cancel Slurm job、validate_user
   脚本: redeploy_jam.py

[云端 MCP 路由]  lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp
   作用: 所有 agent 工具调用
   脚本: agent_demo.py / hot_redeploy.py(经 node_diag 热更新代码)
```

## 热更新为什么不要新 OTP

job(cgroup)没死 → supervisor 重启的只是 MCP server 进程 →
`~/fm-cm` 里两个 master socket 还在 → 新 server 上来直接复用。
只有 scancel 整个 job 才需要下次 boot 重新输一次 OTP
(节点是 cgroup2 + proctrack/cgroup,setssid 逃不掉 job cgroup 的杀)。
