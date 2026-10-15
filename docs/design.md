# VPS Ops MCP Gateway 设计文档 v3.2

> 原则：**少即是安全**。工具少、组件少、没有任意命令。
> 场景：看日志、改配置、Docker、部署。客户端：Claude Code、Codex、Devin Desktop，都运行在你自己的电脑上。

---

## 1. 架构

```
你的电脑                                  跳板机                       VPS × 9
Claude Code ─stdio─► vpsops-proxy ─┐
Codex ───────stdio─► vpsops-proxy ─┼─ Tailscale ─► Gateway ──SSH──► ai-agent ← /etc/ai-agent.yaml
Devin Desktop─stdio─► vpsops-proxy ┘   HTTPS+Token  （HTTP API）  （只能运行     （root 维护）
        │                                  │         ai-agent）
        └ 本地调用账本 ◄── 每 60 秒核对 ──┘
                                           └─► Telegram：授权、通知、告警
```

三层各自负责的事：

| 层 | 职责 | 不负责 |
|---|---|---|
| **vpsops-proxy**（本机） | 对 AI 客户端提供 MCP（stdio）；从 Windows 凭据管理器读取 token；给每次调用编号并记账；和 Gateway 核对账本，发现异常时自动吊销 token | **不做任何权限判断** |
| **Gateway**（跳板机） | token 认证、可见主机过滤、prod 写操作租约、审计、SSH 转发 | 不决定某台机器能做什么 |
| **ai-agent**（VPS） | 按本机 yaml 白名单执行；校验配置、回滚、脱敏 | — |

相比 v3.1 的变化：MCP 协议只在本地代理里实现，Gateway 退化成一个普通的 HTTP JSON API，不再依赖 FastMCP，暴露面更小。

### 安全边界放在每台 VPS 上

VPS 的 `authorized_keys`：
```
restrict,from="<跳板机IP>",command="sudo -n /usr/local/bin/ai-agent" ssh-ed25519 AAAA... gateway
```
sudoers：
```
ai-ops ALL=(root) NOPASSWD: /usr/local/bin/ai-agent ""   # "" = 不允许带参数
```
- 这把 key **只能**启动 `ai-agent`，拿不到 shell，端口转发和 PTY 也被关掉了
- Gateway 通过 stdin 发送 JSON，例如 `{"op":"logs","source":"nginx","lines":200}`，**全程不拼接 shell 字符串**
- 一台机器能做什么，只由这台机器上 root 维护的 `/etc/ai-agent.yaml` 决定。**即使 Gateway 被攻破，也扩大不了权限**

---

## 2. 工具（9 个）

| 工具 | 类型 | 参数 | 说明 |
|---|---|---|---|
| `hosts` | 读 | — | 列出可见主机，以及每台主机可用的日志源、配置、服务、部署目标 |
| `status` | 读 | `host?` | 负载、内存、磁盘、失败的服务、容器健康状况；不传 host 就并发查询全部主机 |
| `logs` | 读 | `hosts[], source, lines≤1000, since?, grep?` | 支持多台主机同时查；`source` 只能是 yaml 里登记的名字 |
| `config_read` | 读 | `host, name` | 返回内容和 `sha256`；secret 类文件只显示 key，值脱敏 |
| `config_edit` | 写 | `host, name, edits[{old,new}], sha256, reason` | 精确替换 → 校验 → 生效 → 检查，任何一步失败自动还原 |
| `config_restore` | 写 | `host, name, backup?, reason` | 默认还原到上一版本 |
| `service` | 写 | `host, name, action: restart\|reload\|up, reason` | 只能操作登记过的 compose 服务或 systemd 单元 |
| `deploy` | 写 | `host, target, version, reason` | **异步执行**，立即返回 `job`；`version` 传 `"rollback"` 时回到上一版本 |
| `job` | 读 | `id` | 查询等待授权的请求，或正在执行的部署的进度和结果 |

`job` 合并了 v3.1 的 `approval`：等待授权和部署执行都是"需要过一会儿再来看"的事。部署改成异步，是因为各客户端对 MCP 工具调用都有超时（通常几十秒），而部署最长可能要 300 秒。

**刻意不提供：** `run_command`、任意 `read_file`、`docker exec/run`、删除、volume 操作、清理。

---

## 3. 每台 VPS 的配置 `/etc/ai-agent.yaml`

文件归 root 所有，权限 600，是这台机器能力的唯一来源。

```yaml
logs:
  nginx:     file:/var/log/nginx/error.log
  access:    file:/var/log/nginx/access.log
  api:       compose:app/api
  ssh:       journald:ssh

configs:
  nginx:
    path: /etc/nginx/nginx.conf
    validate: nginx -t -c {tmp}          # 校验临时文件；无法单独校验的写 null
    apply: systemctl reload nginx
  app-env:
    path: /opt/app/.env
    secret: true                         # 读取时值脱敏；不能改已有的值，只能新增 key
    apply: compose:app/api restart

services:
  nginx:  systemd:nginx
  api:    compose:app/api
  worker: compose:app/worker

deploys:
  api:
    type: compose-image                  # 改 .env 里的 tag，然后 up -d
    compose: /opt/app
    env_var: API_TAG
    version_regex: '^v\d+\.\d+\.\d+$'
    health: http://127.0.0.1:8080/healthz
  blog:
    type: script                         # 运行你自己的脚本，版本通过 $VERSION 传入
    run: /opt/blog/deploy.sh
    version_regex: '^[0-9a-f]{7,40}$|^main$'
    health: http://127.0.0.1:3000/

redact_extra: ['INTERNAL_[A-Z]+=\S+']
```

---

## 4. ai-agent 内部流程

**config_edit**
```
1. 比对 sha256，不一致就拒绝（AI 看到的是旧内容）
2. 依次应用 old→new；每个 old 必须在文件中恰好出现一次
3. 备份到 /var/lib/ai-agent/backups/<name>/，保留最近 20 份
4. 写临时文件 → validate → 原子替换（保留属主和权限）→ apply
5. 检查服务是否 active
6. 第 4、5 步任一失败 → 还原备份并重新 apply → 返回错误和 diff
```

**deploy**（由 Gateway 发起后在后台执行，进度通过 `job` 查询）
```
1. version 必须匹配 version_regex
2. 记录当前版本（/var/lib/ai-agent/deploys/<target>）
3. 切换版本 → 健康检查（90 秒内）
4. 失败 → 自动切回旧版本
5. 文件锁：同一目标同一时间只能有一个部署
```

**输出处理**：超长时保留头 20% 和尾 80%；在 VPS 本机脱敏（私钥、`sk-`/`ghp_`/`AKIA`、`password=`、JWT、连接串密码）后再返回；每个操作都有超时（默认 30 秒，部署 300 秒）。

---

## 5. 授权：按客户端发租约

| 操作 | dev / staging | prod |
|---|---|---|
| 读 | 放行 | 放行 |
| 写 | 放行 | **需要该客户端持有有效租约** |

- 租约**按客户端计算**：Claude Code、Codex、Devin Desktop 各自独立
- 没有租约时，第一次 prod 写操作被挂起，返回一个 `job`，同时 Telegram 收到消息：
  ```
  🔐 codex 请求 prod 写权限
  首个操作：config_edit web-1/nginx
  reason：raise worker_connections
  [30 分钟]  [2 小时]  [4 小时]  [拒绝]
  ```
  你点选时长后，挂起的这次请求执行（冻结的原请求，参数不能改），此后该客户端的 prod 写操作直接放行
- 租约结束的条件（满足任一）：到期；空闲 30 分钟；你在 Telegram 点「撤销」；**本地代理核对失败**（见 §6）
- 租约期间每次写操作都发一条静默通知，带「撤销」按钮
- 租约结束时发送汇总，里面包含核对结果

---

## 6. 本地代理 `vpsops-proxy` 与核对

### 6.1 基本行为

- 每个 AI 客户端通过 stdio 启动自己的代理进程：`vpsops-proxy --client codex`
- token 存在 **Windows 凭据管理器**里（通过 `keyring`），**不写在任何客户端的配置文件里**
- 工具列表从 Gateway 的 `GET /v1/tools` 拉取。工具定义只维护在 Gateway 一处，代理不需要跟着改
- 代理本身不做权限判断，只做三件事：转发、记账、核对

### 6.2 流（stream）与编号

一个客户端可能同时开多个会话，比如 Claude Code 开了好几个窗口，每个会话都会启动一个代理进程。所以记账的单位是**流**，每个代理进程对应一条流：

```
启动：POST /v1/streams              → {stream: "s_4b1e"}
      写入本地流登记表 %LOCALAPPDATA%\vpsops\streams.json

调用：POST /v1/call
      {stream, seq, tool, args}       seq 在流内从 1 开始递增
      → 结果 + receipt {stream, seq, digest}
        digest = sha256(规范化 JSON{stream, seq, tool, args})

退出：最后核对一次 → 在登记表里标记为 closed → DELETE /v1/streams/s_4b1e
```

Gateway 对编号做三条检查：
- 同一条流里，同一个 `seq` 只能用一次
- `seq` 必须落在 `[当前最大值 − 64, 当前最大值 + 64]` 的窗口内
- 已关闭的流，或者空闲超过 30 分钟的流，拒绝继续调用

窗口用来容纳客户端的并发调用（请求到达顺序可能乱序）。这也是 v3.1 的"哈希链"改成"按 seq 排序的集合哈希"的原因：严格的链要求调用串行执行，而并行调用会把链打断。

代理每收到一个 receipt，都会检查其中的 digest 是否和自己算的一致，确认 Gateway 收到的参数就是自己发出的参数。

### 6.3 核对

**触发时机**：代理空闲时每 60 秒一次；代理退出时；Gateway 在响应里告知租约结束时。

```
GET /v1/reconcile → 本客户端最近 24 小时内的所有流：
  [{stream, count, max_seq, set_hash}]
  set_hash = sha256(按 seq 排序后所有 digest 的拼接)

代理检查两件事：
  ① Gateway 列出的每条流，都必须出现在本地登记表里   → 防止有人用偷来的 token 另开一条流
  ② 自己这条流的 count 和 set_hash 必须和本地账本一致 → 防止有人往你的流里插入调用
```

如果有人往你的流里插入调用，他会占掉某个 `seq`。代理之后用到这个 `seq` 时，Gateway 会返回「seq 已被使用」，这一刻就能发现，不需要等到下一次定期核对。

### 6.4 核对失败时

```
1. 代理调用 POST /v1/revoke：吊销本客户端的 token，所有流立即失效，租约结束
2. 代理把本地账本（seq + digest 列表）发给 Gateway
3. Gateway 比对后发 Telegram 告警，列出 Gateway 收到、但本地账本里没有的调用（主机、工具、参数、时间、来源 IP）
4. 代理弹出 Windows 通知；之后所有工具调用都返回 "token 已吊销，请重新 login"
```

恢复步骤：在跳板机上执行 `gateway token rotate codex`，然后在本机执行 `vpsops-proxy login --client codex` 录入新 token。

### 6.5 能力边界

| 情况 | 结果 |
|---|---|
| token 被偷到别的设备上使用 | 首先被 Tailscale ACL 挡住；如果真的打进来，60 秒内被核对发现并自动吊销 |
| 有人在请求传输途中篡改参数 | receipt 里的 digest 对不上，立即发现 |
| 你电脑上的恶意程序直接调用代理，或读取凭据管理器 | **发现不了**：它和正常客户端在同一个用户身份下运行。同理，`--client` 参数只能标明调用来自哪个客户端，在同一台机器上它不是隔离边界 |
| AI 被日志内容诱导而误操作 | **发现不了**：调用确实是它发的。靠能力白名单、自动回滚和一键撤销来兜底 |

---

## 7. Gateway

### 7.1 HTTP API

| 接口 | 用途 |
|---|---|
| `GET  /v1/tools` | 当前客户端可用的工具定义（JSON Schema） |
| `POST /v1/streams` / `DELETE /v1/streams/{id}` | 开启或关闭一条流 |
| `POST /v1/call` | 调用工具 |
| `GET  /v1/reconcile` | 核对数据 |
| `POST /v1/revoke` | 吊销本客户端的 token（客户端只能吊销自己） |
| `POST /v1/ledger` | 核对失败后，代理上传本地账本 |

所有接口都要求 `Authorization: Bearer <token>`，并且只监听 Tailscale 地址。

### 7.2 配置 `gateway.yaml`

```yaml
listen: 100.x.y.z:8443

hosts:                          # 只记录地址和环境；能做什么由 VPS 上的 yaml 决定
  web-1:   { addr: 10.0.0.11, env: prod }
  web-2:   { addr: 10.0.0.12, env: prod }
  build-1: { addr: 10.0.0.21, env: dev }

clients:
  claude-code:   { token_sha256: "...", envs: [dev, staging, prod] }
  codex:         { token_sha256: "...", envs: [dev, staging, prod] }
  devin-desktop: { token_sha256: "...", envs: [dev, staging, prod] }

lease:
  choices: [30m, 2h, 4h]
  idle_timeout: 30m
  notify_each_write: true

streams:
  seq_window: 64
  idle_timeout: 30m

telegram:
  bot_token_file: /etc/gateway/tg.token
  chat_id: 123456               # 只响应这个 chat 的按钮回调；用轮询，不开入站端口
```

---

## 8. 客户端接入

先在本机录入一次 token：

```bash
vpsops-proxy login --client claude-code
```

**Claude Code**（CLI 和 Desktop 共用用户级配置）：

```bash
claude mcp add --scope user vpsops -- vpsops-proxy --client claude-code
```

在 `settings.json` 里可以把只读工具设为 allow：`mcp__vpsops__hosts`、`mcp__vpsops__status`、`mcp__vpsops__logs`、`mcp__vpsops__config_read`、`mcp__vpsops__job`。写工具保持询问，作为客户端这一侧的第一道确认。

**Codex** `~/.codex/config.toml`：
```toml
[mcp_servers.vpsops]
command = "vpsops-proxy"
args = ["--client", "codex"]
```

**Devin Desktop**：在它的 MCP 设置里添加一个 stdio server，command 填 `vpsops-proxy`，args 填 `--client devin-desktop`。具体的配置入口和格式以 Devin Desktop 的文档为准。

---

## 9. 网络

- **Tailscale ACL**：只允许你的电脑访问 `跳板机:8443`
- VPS 防火墙：22 端口只对跳板机开放；`authorized_keys` 的 `from=` 再限制一次来源
- 整套系统没有任何公网入口

---

## 10. 审计（三份）

| 位置 | 内容 |
|---|---|
| 本机 `%LOCALAPPDATA%\vpsops\ledger\*.jsonl` | 代理的账本：stream、seq、digest、工具、参数、结果状态 |
| 跳板机 `/var/log/gateway/*.jsonl` | 客户端、stream、seq、digest、租约、工具、主机、参数、reason、结果、来源 IP |
| 每台 VPS 的 syslog（`ai-agent` tag） | 操作、stream、seq、结果 |

三份记录分别在三台不同的机器上，攻击者要同时篡改三处才能抹掉痕迹。

---

## 11. 威胁对照

| 威胁 | 对策 |
|---|---|
| 提示注入或 AI 误操作 | 没有任意命令；配置改坏自动还原；部署失败自动回滚；写操作通知 + 一键撤销 |
| 命令注入 | 全程 JSON，没有 shell 拼接；version 必须匹配正则 |
| token 泄露 | 不以明文出现在配置文件里（存在凭据管理器）；Tailscale ACL；核对失败自动吊销 |
| 传输中被篡改 | TLS + receipt digest 比对 |
| Gateway 被攻破 | key 只能启动 ai-agent，能力由 VPS 上 root 维护的 yaml 决定；VPS 和本机各有一份审计记录 |
| 租约被滥用 | 按客户端隔离；空闲自动失效；随时撤销；核对失败立即终止 |
| 本机被入侵 | 无法完全防御；靠租约时长短、写操作通知和一键撤销缩小损害窗口 |

---

## 12. 代码结构与技术栈

全部使用 Python，三个组件放在一个仓库里，规范化 JSON 和 digest 的代码共用。

```
vpsops/
  common/      canon.py（规范化 JSON + digest）  schema.py（工具定义）
  proxy/       main.py（MCP stdio：mcp SDK）  ledger.py  reconcile.py  creds.py（keyring）
  gateway/     app.py（aiohttp）  auth.py  lease.py  streams.py  ssh.py（asyncssh）  telegram.py  audit.py
  agent/       ai_agent.py（单文件，只用标准库，部署到 VPS）
  deploy/      authorized_keys.tpl  sudoers.ai-ops  ai-agent.example.yaml
```

| 组件 | 估计规模 | 依赖 |
|---|---|---|
| proxy | 约 200 行 | `mcp`、`httpx`、`keyring` |
| gateway | 约 350 行 | `aiohttp`、`asyncssh`、`pyyaml` |
| ai-agent | 约 400 行 | 只用标准库 |

proxy 用 `uv tool install` 安装到本机，安装后 `vpsops-proxy` 即可在 PATH 中直接调用。

---

## 13. 落地步骤

| 步骤 | 内容 | 验收标准 |
|---|---|---|
| 1 | `ai-agent` 的读操作；在 dev 机器上用 `echo '{"op":"status"}' \| ssh ...` 手动测试 | 返回 JSON；在 VPS 上尝试拿 shell 会失败 |
| 2 | Gateway 的读接口 + proxy 的转发功能；配置 Tailscale ACL；接入三个客户端；推到全部 9 台机器 | 三个客户端都能跨主机查日志 |
| 3 | proxy 的账本和核对；`revoke` | 用 curl 拿 token 手动发一个请求，60 秒内 token 被吊销，Telegram 收到告警 |
| 4 | `config_edit`、`config_restore`、`service`；租约和 Telegram 授权 | 改 prod 配置时只授权一次；故意改坏配置能自动还原 |
| 5 | `deploy` + `job` | 健康检查失败能自动回滚 |

每一步都能单独使用。第 3 步刻意排在写操作之前：先确认核对机制可靠，再开放写权限。
