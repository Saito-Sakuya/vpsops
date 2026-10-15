# vpsops

让 Claude Code / Codex / Devin Desktop 通过跳板机，**受控地**操作 VPS：看日志、改配置、管理服务、部署。
设计说明见 [docs/design.md](docs/design.md)。

```
AI 客户端 ─stdio─► vpsops-proxy ──Tailscale/HTTPS──► vpsops-gateway ──SSH──► ai-agent（每台 VPS）
 （本机）           记账 + 核对                         认证/租约/审计        白名单执行
```

- **没有任意命令**。只有 9 个工具；每台 VPS 能做什么只由它自己的 `/etc/ai-agent.yaml`（root 维护）决定。
- SSH key 被 `command=` 强制绑定到 `ai-agent`，Gateway 被攻破也拿不到 shell。
- 改配置：精确替换 → 校验 → 原子替换 → 生效 → 检查，失败自动还原。部署：健康检查失败自动回滚。
- prod 写操作需要按客户端授予的**租约**（Telegram 上点一次，30m/2h/4h）。
- 本地代理与 Gateway 每 60 秒核对调用账本，发现非本机发起的调用立即吊销 token。

## 目录

| 路径 | 内容 |
|---|---|
| `agent/ai_agent.py` | VPS 端，单文件，只依赖标准库（YAML 配置需要 python3-yaml） |
| `src/vpsops/gateway/` | 跳板机端（aiohttp + asyncssh） |
| `src/vpsops/proxy/` | 本机 MCP stdio 代理（mcp + httpx + keyring） |
| `src/vpsops/common/` | 工具定义、规范化 JSON 与 digest |
| `deploy/vps/` | VPS 安装脚本、sudoers、sshd、配置示例 |
| `deploy/gateway/` | gateway 配置示例、systemd unit |
| `deploy/install.sh` / `deploy/install.ps1` | Linux / Windows 一键安装入口 |

## 交给 AI Agent 安装

把下面这一句话直接复制给能运行终端命令的 AI Agent：

> 请先阅读 https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/README.md，按文档在我的环境中安装配置 vpsops 并接入当前 AI 客户端，先检查并复用已有部署，缺少部署范围、主机、SSH 或域名信息时向我确认，敏感凭据由我在对应机器交互录入，最后用 check、hosts 和 status 实测验收并列出已完成与待处理项。

已有 Gateway 时可以只安装本机代理；需要部署 Gateway 或 VPS Agent 时，AI Agent 还需要对应主机的 SSH 和管理员权限。下面的安装章节提供了脚本、配置路径和客户端接入方式。

## 一键安装与运行

**Windows 本机代理**：在 PowerShell 执行，自动安装 uv、Python 3.11 和代理，无需先安装 Git：

```powershell
irm https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/deploy/install.ps1 | iex
```

安装完成后重新打开终端，为需要使用的客户端执行 `vpsops-proxy --client codex login` 和 `vpsops-proxy --client codex check`。客户端名可以替换为 `claude-code` 或 `devin-desktop`。也可以一条命令完成安装、交互登录和连接检查：

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/deploy/install.ps1))) -Client codex -Login
```

**已安装 uv 时直接运行**：以下命令在隔离环境中启动 MCP stdio 代理，首次需要先将命令末尾加上 `login` 录入 Gateway 地址和 token。正常启动后会等待 MCP 客户端通过标准输入发送请求：

```powershell
uvx --python 3.11 --from "vpsops[proxy] @ https://github.com/Saito-Sakuya/vpsops/archive/refs/heads/master.tar.gz" vpsops-proxy --client codex
```

**Linux 跳板机**：先安装并连接 Tailscale，然后运行（Debian 12 / Ubuntu 24.04）：

```bash
curl -fsSL https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/deploy/install.sh | sudo sh -s -- gateway
```

脚本准备依赖、Gateway、SSH key、配置模板和 systemd unit，保留已有配置与密钥。首次仍需按下文填写主机、客户端 token、Telegram 和 TLS；配置完成后运行 `sudo systemctl enable --now vpsops-gateway`。已有安装可重复执行以更新源码和依赖，运行中的服务需要在更新完成后重启。

**每台 Linux VPS**：在 VPS 上运行，把公钥和来源 IP 替换为跳板机的实际值：

```bash
curl -fsSL https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/deploy/install.sh | sudo sh -s -- agent 'ssh-ed25519 AAAA... vpsops-gateway' '100.64.0.5'
```

公钥取自跳板机 `/etc/vpsops-gateway/id_ed25519.pub`，IP 是 VPS 实际看到的跳板机 SSH 来源。安装后以 root 编辑 VPS 的 `/etc/ai-agent.yaml`，再按下文核对 SSH 主机指纹。已在 root shell 中时，可省略上述命令的 `sudo`。

## 安装

源码仓库：[Saito-Sakuya/vpsops](https://github.com/Saito-Sakuya/vpsops)。Python 包使用 [uv](https://docs.astral.sh/uv/getting-started/installation/) 管理；下面的手动 Git 安装方式需要 Git，上面的一键客户端入口使用源码压缩包。

- 跳板机需要 Python **3.11+**、systemd、OpenSSH 和已接入 tailnet 的 Tailscale。下面的 Linux 命令以 Debian 12 / Ubuntu 24.04 为例。
- 每台 VPS 需要 Python 3、`python3-yaml`、sudo 和 OpenSSH；登记了 Docker Compose、nginx 等能力时，还需要对应程序。
- 先确认本机能通过 Tailscale 访问跳板机，跳板机能 SSH 到各 VPS；tailnet 访问规则只放行本机到 Gateway 的端口（示例为 `8740`）。

### 1. 在跳板机准备 Gateway

以下命令在跳板机执行。先进入 root shell，后续 Gateway 安装与 VPS 分发步骤都在该 shell 中完成：

```bash
sudo -i
apt-get update
apt-get install -y git curl ca-certificates python3 python3-venv openssh-client sudo
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

id -u vpsops >/dev/null 2>&1 || useradd --system --user-group --home-dir /opt/vpsops --shell /usr/sbin/nologin vpsops
git clone https://github.com/Saito-Sakuya/vpsops.git /opt/vpsops
cd /opt/vpsops
uv sync --locked --no-dev --extra gateway --python /usr/bin/python3

install -d -m 0750 -o root -g vpsops /etc/vpsops-gateway
if [ ! -f /etc/vpsops-gateway/id_ed25519 ]; then
  ssh-keygen -t ed25519 -N "" -C vpsops-gateway -f /etc/vpsops-gateway/id_ed25519
fi
chown root:vpsops /etc/vpsops-gateway/id_ed25519
chmod 0640 /etc/vpsops-gateway/id_ed25519
install -m 0640 -o root -g vpsops deploy/gateway/gateway.example.yaml /etc/vpsops-gateway/gateway.yaml
install -m 0640 -o root -g vpsops /dev/null /etc/vpsops-gateway/tg.token
```

使用系统 Python 创建虚拟环境，确保 systemd 服务账号也能访问解释器。上面的命令用于首次安装；已有 `vpsops` 用户或 SSH key 时保留它们。

为每个需要接入的客户端分别生成 token：

```bash
/opt/vpsops/.venv/bin/vpsops-gateway token new claude-code
/opt/vpsops/.venv/bin/vpsops-gateway token new codex
/opt/vpsops/.venv/bin/vpsops-gateway token new devin-desktop
```

明文 token 用于稍后的本机 `login`；`gateway.yaml` 只填写命令输出的 `token_sha256`。编辑 `/etc/vpsops-gateway/gateway.yaml`：

- `listen`：跳板机真实的 Tailscale IP 和端口，例如 `100.64.0.5:8740`。
- `hosts`：各 VPS 的实际地址和环境；删除没有使用的示例主机。
- `clients`：填入各客户端的 token hash 和可访问环境；删除没有使用的客户端条目。
- `telegram.chat_id`：授权消息所在会话的 ID；将 Bot token 写入 `/etc/vpsops-gateway/tg.token`。未配置 Telegram 时，prod 写操作无法获得授权。

在 Tailscale 管理后台启用 [HTTPS 证书](https://tailscale.com/docs/how-to/set-up-https-certificates)，然后申请证书。把下面的域名改为跳板机实际的完整 tailnet 域名：

```bash
TAILNET_NAME=jump.your-tailnet.ts.net
tailscale cert --cert-file=/etc/vpsops-gateway/tls.crt --key-file=/etc/vpsops-gateway/tls.key "$TAILNET_NAME"
chown root:vpsops /etc/vpsops-gateway/tls.crt /etc/vpsops-gateway/tls.key
chmod 0640 /etc/vpsops-gateway/tls.crt /etc/vpsops-gateway/tls.key
```

本机登录时使用 `https://jump.your-tailnet.ts.net:8740`，域名必须与证书一致。通过 `tailscale cert` 保存的证书文件需要定期续期，替换证书后重启 Gateway。

### 2. 从跳板机安装每台 VPS 的 Agent

仍在跳板机的 root shell 中执行，每台 VPS 重复一次。`VPS_HOST` 填该 VPS 的 SSH 地址，`JUMP_IP` 填 **VPS 实际看到的跳板机 SSH 来源 IP**（走 Tailscale 时通常为跳板机的 Tailscale IP）：

```bash
cd /opt/vpsops
VPS_HOST=10.0.0.11
JUMP_IP=100.64.0.5

ssh "root@$VPS_HOST" 'apt-get update && apt-get install -y python3 python3-yaml sudo && mkdir -p /tmp/vpsops'
scp -r agent deploy "root@$VPS_HOST:/tmp/vpsops/"
GATEWAY_PUBKEY=$(cat /etc/vpsops-gateway/id_ed25519.pub)
ssh "root@$VPS_HOST" "sh /tmp/vpsops/deploy/vps/install.sh '$GATEWAY_PUBKEY' '$JUMP_IP'"
```

在该 VPS 上以 root 编辑 `/etc/ai-agent.yaml`，只登记实际需要的日志源、配置、服务和部署目标；Compose 路径映射也需按实际填写。安装脚本只安装 Agent 与 SSH/sudo 限制，应用程序需预先安装。VPS 防火墙应限制 SSH 来源为跳板机，并保留所需的管理员维护入口。

回到跳板机，采集主机公钥：

```bash
ssh-keyscan -t ed25519 "$VPS_HOST" > /tmp/vpsops-host-key
ssh-keygen -lf /tmp/vpsops-host-key
```

通过 VPS 控制台运行 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`，核对指纹一致后再保存并测试：

```bash
cat /tmp/vpsops-host-key >> /etc/vpsops-gateway/known_hosts
chown root:vpsops /etc/vpsops-gateway/known_hosts
chmod 0640 /etc/vpsops-gateway/known_hosts
printf '%s\n' '{"op":"describe"}' | sudo -u vpsops ssh -T \
  -i /etc/vpsops-gateway/id_ed25519 \
  -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/etc/vpsops-gateway/known_hosts "ai-ops@$VPS_HOST"
```

返回 JSON 后，将该主机的地址与环境填入跳板机的 `/etc/vpsops-gateway/gateway.yaml`。

### 3. 启动 Gateway

在跳板机确认配置、token hash、Telegram、TLS 证书和主机指纹都已填写，再执行：

```bash
sudo -u vpsops /opt/vpsops/.venv/bin/vpsops-gateway check -c /etc/vpsops-gateway/gateway.yaml
install -m 0644 /opt/vpsops/deploy/gateway/vpsops-gateway.service /etc/systemd/system/vpsops-gateway.service
systemctl daemon-reload
systemctl enable --now vpsops-gateway
systemctl status vpsops-gateway --no-pager
```

启动异常时查看 `journalctl -u vpsops-gateway -n 100 --no-pager`。修改 `gateway.yaml` 后需要 `systemctl restart vpsops-gateway`。

### 4. 本机安装与客户端接入（Windows）

先安装 [Git for Windows](https://git-scm.com/downloads/win) 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后在 PowerShell 中安装代理。`--python 3.11` 让 uv 选择或自动安装 Python 3.11：

```powershell
uv tool install --python 3.11 "vpsops[proxy] @ git+https://github.com/Saito-Sakuya/vpsops.git"
uv tool update-shell
```

重新打开终端，使 PATH 生效。每个客户端分别登录，输入上面配置的 HTTPS 域名地址和对应的明文 token，token 保存在 Windows 凭据管理器中：

```powershell
vpsops-proxy --client claude-code login
vpsops-proxy --client claude-code check
vpsops-proxy --client codex login
vpsops-proxy --client codex check
```

如果使用 Devin Desktop，同样执行 `vpsops-proxy --client devin-desktop login` 和 `check`。使用 Tailscale 证书时，登录中的自签 CA 路径直接回车即可。

Claude Code 接入：

```powershell
claude mcp add --scope user vpsops -- vpsops-proxy --client claude-code
```

Codex `~/.codex/config.toml`：

```toml
[mcp_servers.vpsops]
command = "vpsops-proxy"
args = ["--client", "codex"]
```

Devin Desktop：在其 MCP 设置中添加 stdio server，command `vpsops-proxy`，args `--client devin-desktop`。

重启对应客户端后，先调用 `hosts` 检查可见主机及能力。代理更新使用 `uv tool upgrade vpsops`，更新后重启客户端；已有本地源码时，也可在仓库根目录执行 `uv tool install '.[proxy]'`。

## 使用方式

### 首次连接

1. 用当前客户端对应的名称执行 `vpsops-proxy --client codex login`，录入 Gateway 的 HTTPS 地址和该客户端的 token；Claude Code 使用 `claude-code`，Devin Desktop 使用 `devin-desktop`。
2. 执行 `vpsops-proxy --client codex check`，确认连接正常、可取得 9 个工具且账本核对一致；其他客户端替换相应名称。
3. 按上面的 MCP 配置接入客户端并重启，然后让 AI 调用 `hosts`，确认能看到预期主机及其登记的能力，再用 `status` 检查实际主机状态。

`check` 检查代理与 Gateway 的连接和核对机制；VPS 的 SSH、Agent 和业务状态还需要通过 `hosts`、`status` 及返回的错误确认。终端中直接运行代理后等待输入是 MCP stdio 服务的正常行为，日常操作应在已接入 MCP 的 AI 客户端里发起。

### 可以直接对 AI 说

下面的主机名、日志源、配置名、服务名和部署目标都是示例，使用时替换为 `hosts` 返回的实际名称。AI 应先确认相应能力已经登记。

| 任务 | 可复制给 AI 的请求 | 使用的工具 |
|---|---|---|
| 查看可用主机 | 用 vpsops 列出我能访问的主机、环境，以及每台主机登记的能力。 | `hosts` |
| 检查运行状态 | 检查所有可见主机的负载、内存、磁盘、失败服务和容器状态，汇总异常及对应主机。 | `status` |
| 多主机排查日志 | 查看 web-1 和 web-2 的 api 日志源，读取最近 15 分钟的 200 行日志并筛选 ERROR 或 Exception，汇总错误。 | `logs` |
| 阅读配置 | 读取 web-1 登记的 nginx 配置，说明当前设置并保留返回的 sha256。 | `config_read` |
| 修改配置 | 把 web-1 的 nginx 配置中 worker_connections 调整为 2048，先读取实际内容再精确替换，填写改动原因并复查结果。 | `config_read` → `config_edit` |
| 恢复配置 | 将 web-1 的 nginx 配置恢复到最近备份，原因是撤销刚才的配置调整，完成后复查配置和服务状态。 | `config_restore` |
| 管理服务 | 重启 web-1 登记的 api 服务，原因是应用配置已更新，完成后报告服务状态。 | `service` |
| 发布版本 | 将 web-1 登记的 api 目标部署到 v1.2.3，原因是发布该版本，返回任务后跟踪到结束并报告结果。 | `deploy` → `job` |
| 回滚版本 | 将 web-1 的 api 部署目标回滚到上一版本，原因是当前版本出现异常，查询任务并报告回滚结果。 | `deploy`（`version="rollback"`）→ `job` |

日志源可以是文件、journald 或 Compose 服务；`since` 仅适用于后两种，文件日志使用 `lines` 和 `grep` 筛选。服务操作支持 `restart`、`reload`、`up`，其中 Compose 不支持 `reload`。部署版本必须满足目标的版本正则，回滚需要存在已知的上一版本。

### 生产授权与任务查询

读操作可访问该客户端有权限查看的主机；dev/staging 写操作直接执行，prod 写操作需要当前客户端持有有效租约。没有有效租约时，请求返回 `pending_approval` 和 `job`，并通过 Telegram 发送授权消息；你选择 30 分钟、2 小时或 4 小时后，Gateway 执行已经提交的原请求。

授权期间，当前客户端后续的 prod 写操作会直接执行并按配置发送通知；其他客户端的租约独立计算。可以在 Telegram 撤销租约，租约也会在到期或空闲超时后失效；撤销租约影响后续写操作，不会撤回已完成的操作。

`deploy` 异步返回 `job`；查询时把这个值作为 `job` 工具的 **`id` 参数**。等待授权时先完成 Telegram 操作再查询；运行中可相隔 30 秒查询一次，避免重复提交同一写请求。

| `job` 返回状态 | 含义与下一步 |
|---|---|
| `pending_approval` | 等待 Telegram 授权；当前写操作尚未执行。 |
| `running` | 任务正在执行，稍后再次查询。 |
| `done` | 任务成功，查看 `result` 中的实际结果。 |
| `failed` | 任务失败，查看错误及回滚结果；修正原因后再决定是否重试。 |
| `rejected` | 授权已被拒绝，本次请求不执行。 |
| `expired` | 授权请求已过期，本次请求不执行。 |

配置修改先调用 `config_read`，使用返回的 `sha256` 和实际内容构造 `edits`；每个 `old` 必须恰好匹配一次。哈希过期时重新读取再修改，脱敏行不可编辑。失败时检查返回的错误与 `rolled_back` 等字段，不要把“已尝试恢复”当作恢复成功。

### MCP 参数示例

这些 JSON 是传给 MCP 工具的参数，供 AI 调用时参考。

`logs`：

```json
{
  "hosts": ["web-1", "web-2"],
  "source": "api",
  "lines": 200,
  "since": "15m",
  "grep": "ERROR|Exception"
}
```

`deploy`：

```json
{
  "host": "web-1",
  "target": "api",
  "version": "v1.2.3",
  "reason": "发布版本 v1.2.3"
}
```

随后把响应中的 `job` 值填入 `job` 工具的 `id` 参数。填写名称前先检查 `hosts`，填写版本前确认部署目标允许的版本格式。

### 常见问题

- **客户端找不到 `vpsops-proxy`**：执行 `uv tool update-shell`，重新打开终端并重启 AI 客户端；也可将 MCP command 设置为 `uv tool dir --bin` 目录中的代理可执行文件完整路径。
- **连接失败或证书域名不匹配**：检查本机和跳板机的 Tailscale 连接、访问规则、Gateway 服务和监听端口；登录时使用与 TLS 证书一致的域名。跳板机日志可用 `journalctl -u vpsops-gateway -n 100 --no-pager` 查看。
- **看不到主机或能力名称不存在**：先查 Gateway 配置中该客户端的 `envs` 和 `hosts` 条目；具体日志、配置、服务及部署目标由各 VPS 的 `/etc/ai-agent.yaml` 登记，修改这些登记需要对应的管理员权限。
- **prod 操作一直等待授权**：检查 Telegram 配置与通知，在授权消息中选择租约时长，再查询原 `job`；Gateway 未配置 Telegram 时无法授予 prod 租约。
- **token 已吊销**：在跳板机运行 `/opt/vpsops/.venv/bin/vpsops-gateway token new <客户端名称>`，把新 hash 写入 `gateway.yaml` 并重启 Gateway，然后在本机对该客户端重新 `login`、`check`。重新登录本身不会恢复被吊销的旧 token。

## 开发

```bash
git clone https://github.com/Saito-Sakuya/vpsops.git
cd vpsops
uv sync --locked
uv run pytest
```

测试覆盖：ai-agent（脱敏、精确替换、校验失败/生效失败自动还原、secret 行不可改、部署回滚），
Gateway+Proxy 端到端（租约、拒绝、撤销、异步部署、偷 token 开新流、插入 seq、gateway 重启），
真实 MCP stdio 往返，以及本地 asyncssh 服务器上的 SSH 收发与主机密钥校验。

## 许可证

本项目采用 [MIT License](LICENSE)，Copyright (c) 2026 Saito-Sakuya。
