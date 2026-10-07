# 给 Jie 的需求:LQCDMCP 平台加 MCP OAuth,让 ChatGPT 直连

## 📋 可直接复制发给 Jie 的消息

> Jie,想给 LQCDMCP 平台提一个需求:让 ChatGPT 能直接连接平台上的
> MCP server(比如我们的 jam-gateway)。现在 `/cloud/<name>/mcp` 外网
> 可达,但 ChatGPT 连远程 MCP 必须先走 OAuth 2.1(找
> `/.well-known/oauth-protected-resource` 元数据 → 授权页 → token),
> 平台目前没有这套端点,所以 ChatGPT 会拒绝连接。
>
> 需要的就几样东西(都在 lqcdmcp.jlab.org:8000 这个 origin 上):
> 1. `/.well-known/oauth-protected-resource` 元数据
> 2. 授权服务器元数据端点(authorization/token 端点)
> 3. 授权页 — 可以直接复用平台现有的 Globus 登录
> 4. token 端点(PKCE)
>
> 顺带一个安全理由:现在 `/cloud` 路由是无鉴权的,谁拿到 URL 都能调
> (包括能触发 HPC 提交的工具),加 OAuth 等于给平台补上这层。
> 做完之后,所有 ChatGPT 用户连接平台上任何 MCP server 都是一条龙:
> 填链接 → Globus 授权一次 → 直接用。平台上的所有 server 都受益。

## 目标

让 ChatGPT(及任何支持远程 MCP 的客户端)能**直接连接**平台上的 MCP
server,比如 `https://lqcdmcp.jlab.org:8000/cloud/jam-gateway/mcp`,
不需要用户自己搭任何服务器。

## 现状(2026-10-05 实测)

- `/cloud/<name>/mcp` 路由是**无鉴权**的裸 JSON-RPC(我们全程没带 token
  就调通了)。外网可达 ✅
- ChatGPT 连远程 MCP 只认 **OAuth 2.1**(MCP 授权规范):先取
  `/.well-known/oauth-protected-resource` 元数据 → 发现授权服务器 →
  浏览器跳授权页 → PKCE 换 code → 换 bearer token → 带 token 调 MCP。
- 平台目前只有 dashboard 用的 Globus 登录 + Bearer token(面向人/脚本),
  没有 MCP 客户端要的那套元数据端点,所以 ChatGPT 会拒绝连接。

## 需要加的东西(都在 lqcdmcp.jlab.org:8000 这个 origin 上)

1. `GET /.well-known/oauth-protected-resource`
   返回类似:
   ```json
   {
     "resource": "https://lqcdmcp.jlab.org:8000",
     "authorization_servers": ["https://lqcdmcp.jlab.org:8000/oauth"]
   }
   ```
2. `GET /oauth/.well-known/oauth-authorization-server`(授权服务器元数据:
   authorization_endpoint、token_endpoint、registration_endpoint、
   response_types_supported=["code"]、code_challenge_methods_supported=["S256"])
3. 授权端点(浏览器授权页):**可直接复用平台现有的 Globus OAuth 登录页** —
   用户用 Globus 账号登录即视为授权,带 PKCE code challenge。
4. token 端点:code + code_verifier → 短时 bearer token。
5. (可选)动态客户端注册端点,或预注册 OpenAI 的 client_id。

## 鉴权接线

`/cloud/<name>/mcp` 拿到 bearer token 后,按 username → 该用户注册的
server 校验即可(平台已有 owner 字段,`jam-gateway` 的 owner=chunyu)。

## 对我们/平台的额外好处

- 现在的 `/cloud` 路由**谁拿到 URL 都能调**(包括能触发 HPC 提交的工具),
  加 OAuth 顺便补上安全 — 这对平台本身也是必要的事。
- 一个功能,平台上的所有 MCP server 都受益:之后任何 ChatGPT 用户
  "连接平台上的 xx-server" 都是一条龙体验。

## 参考

- MCP Authorization spec(OAuth 2.1 / RFC 9728 profile)
- 我们已就绪的部分:jam-gateway 本身无需改动;OAuth 只需包在代理层。
