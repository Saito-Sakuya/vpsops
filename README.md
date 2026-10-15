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
