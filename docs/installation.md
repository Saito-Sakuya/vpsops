# 安装 vpsops

[返回 README](../README.md) · [添加服务器](../skills/vpsops-onboard/references/onboarding.md) · [日常使用](usage.md)

首次部署需要三个部分：你的电脑运行 Proxy，跳板机运行 Gateway，每台被管 VPS 安装 Agent。先准备 Gateway 和密钥，再配置 VPS，启动 Gateway，最后接入本机 AI 客户端。已有部署时只补缺少的部分。

一键脚本负责安装程序；实际主机、权限、证书和凭据仍需按你的环境配置。只新增 VPS 时直接看[接入指南](../skills/vpsops-onboard/references/onboarding.md)，无需重装 Gateway 或重新生成客户端 Token。

## 一键脚本安装

### 1. Windows 本机代理（`vpsops-proxy`）

打开 PowerShell 运行下面命令，脚本会自动装好 `uv`、Python 3.11 和 `vpsops-proxy`，不用提前装 Git：

```powershell
irm https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/deploy/install.ps1 | iex
```

装完后**重开一个终端窗口**，按你实际用的客户端（`codex`、`claude-code` 或 `devin-desktop`）登录并自检：

```powershell
vpsops-proxy --client codex login
vpsops-proxy --client codex check
```

想一条命令连安装带登录自检一起跑完，可以用：

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/deploy/install.ps1))) -Client codex -Login
```

如果你本机已经装了 `uv`，也可以不全局安装，直接用 `uvx` 在隔离环境里拉起代理（首次使用需在末尾加上 `login` 录入 Gateway 地址和 Token；平时不带子命令直接运行会进入 MCP stdio 监听模式，等待客户端发请求）：

```powershell
uvx --python 3.11 --from "vpsops[proxy] @ https://github.com/Saito-Sakuya/vpsops/archive/refs/heads/master.tar.gz" vpsops-proxy --client codex
```

### 2. Linux 跳板机（`vpsops-gateway`）

先在跳板机上装好 Tailscale 并连入你的 tailnet，然后执行（支持 Debian 12 / Ubuntu 24.04）：

```bash
curl -fsSL https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/deploy/install.sh | sudo sh -s -- gateway
```

脚本会自动配好系统依赖、Gateway 运行环境、SSH 密钥、配置模板和 `systemd` 服务，同时保留你已有的配置和密钥不被覆盖。

首次安装后，你还需要参考下文配置好 `/etc/vpsops-gateway/gateway.yaml`（填入 VPS 列表、客户端 Token 哈希、Telegram 与 TLS 证书），再启动服务：

```bash
sudo systemctl enable --now vpsops-gateway
```

后续更新代码和依赖时，可以重跑同一条安装命令；检查配置通过、现有部署任务结束后，再重启 `vpsops-gateway`。

### 3. 每台被管 Linux VPS（`ai-agent`）

在目标 VPS 上运行下面命令，注意把公钥和来源 IP 换成你跳板机的真实信息（如果已经在 `root` 下可去掉 `sudo`）：

```bash
curl -fsSL https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/deploy/install.sh | sudo sh -s -- agent 'ssh-ed25519 AAAA... vpsops-gateway' '100.64.0.5'
```

- **公钥**：去跳板机的 `/etc/vpsops-gateway/id_ed25519.pub` 里复制。
- **来源 IP**：填这台 VPS 实际看到的跳板机 SSH 来源 IP（走 Tailscale 内网的话就是跳板机的 Tailscale IP）。
- **后续动作**：装完后用 `root` 编辑 VPS 上的 `/etc/ai-agent.yaml` 填好白名单，再回到跳板机核对并保存该 VPS 的 SSH 主机指纹（具体步骤见[接入指南](../skills/vpsops-onboard/references/onboarding.md#主控机器完成登记与验收)）。

## 手动分步安装指南

以下手动步骤适用于首次部署。已有部署优先使用上面的一键脚本，并保留现有配置、密钥和授权。

源码仓库：[Saito-Sakuya/vpsops](https://github.com/Saito-Sakuya/vpsops)，Python 依赖通过 [uv](https://docs.astral.sh/uv/getting-started/installation/) 管理。

动手前先确认三件事：

- **网络连通**：本机能通过 Tailscale 访问跳板机，跳板机能 SSH 连上各台 VPS；建议在 Tailscale ACL 里只放行本机到跳板机 Gateway 端口（下文示例用 `8740`）。
- **跳板机环境**：Python **3.11+**、`systemd`、OpenSSH，以及已连入 tailnet 的 Tailscale（下文以 Debian 12 / Ubuntu 24.04 为例）。
- **VPS 环境**：Python 3、`python3-yaml`、`sudo`、OpenSSH。如果要让 Agent 管理 Docker Compose 或 Nginx，机器上得提前装好对应程序。

### 1. 在跳板机部署 Gateway

在跳板机上切到 `root` shell，后续安装和分发都在这个 shell 里操作：

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

> 注：这里指定 `/usr/bin/python3` 创建虚拟环境，是为了确保 `vpsops` 系统服务账号也能正常调用 Python 解释器。

接着为你要用的 AI 客户端分别生成 Token：

```bash
/opt/vpsops/.venv/bin/vpsops-gateway token new claude-code
/opt/vpsops/.venv/bin/vpsops-gateway token new codex
/opt/vpsops/.venv/bin/vpsops-gateway token new devin-desktop
```

命令会输出两样东西：**明文 Token** 留着一会儿在本机 `login` 时输入；**`token_sha256`** 填进跳板机的 `/etc/vpsops-gateway/gateway.yaml`。

打开 `/etc/vpsops-gateway/gateway.yaml` 修改以下核心配置：

- `listen`：填跳板机的 Tailscale IP 和监听端口，例如 `100.64.0.5:8740`。
- `hosts`：填入各台 VPS 的实际连接地址和所属环境（`dev` / `staging` / `prod`），删掉没用到的示例主机。
- `clients`：填入每个客户端的 `token_sha256` 和允许访问的环境列表，删掉不用的客户端条目。
- `telegram.chat_id`：填接收审批消息的 Telegram 会话 ID，并把 Bot Token 写进 `/etc/vpsops-gateway/tg.token`。先按 [Bot 接入指南](telegram.md)创建机器人、取得 ID 并发送测试消息；未配置时，`prod` 写操作无法审批通过。

然后配置 HTTPS 证书。先在 Tailscale 管理后台开启 [HTTPS 证书功能](https://tailscale.com/docs/how-to/set-up-https-certificates)，再把下面命令里的域名换成跳板机真实的 tailnet 域名来申请证书：

```bash
TAILNET_NAME=jump.your-tailnet.ts.net
tailscale cert --cert-file=/etc/vpsops-gateway/tls.crt --key-file=/etc/vpsops-gateway/tls.key "$TAILNET_NAME"
chown root:vpsops /etc/vpsops-gateway/tls.crt /etc/vpsops-gateway/tls.key
chmod 0640 /etc/vpsops-gateway/tls.crt /etc/vpsops-gateway/tls.key
```

稍后本机登录时，地址必须填这个域名（例如 `https://jump.your-tailnet.ts.net:8740`）才能通过证书校验。记得定期续期 `tailscale cert` 生成的证书，续期后在运行中的部署结束后重启 Gateway。

### 2. 往各台 VPS 部署 Agent

按[接入指南的主控机流程](../skills/vpsops-onboard/references/onboarding.md#a-主控机器远程操作)，完成 Agent 安装、白名单和本机检查；如果直接在目标 VPS 操作，使用[宿主机流程](../skills/vpsops-onboard/references/onboarding.md#b-agent-宿主机本机操作)。

然后完成指南中的[指纹核对、SSH 检查和主机登记](../skills/vpsops-onboard/references/onboarding.md#主控机器完成登记与验收)。首次部署此时尚未启动 Gateway，登记后回到下一步做配置检查并启动服务；MCP 验收留到本机代理连接好之后。

### 3. 自检并启动 Gateway

确认跳板机上的 `gateway.yaml`、Token 哈希、Telegram Token、TLS 证书和 `known_hosts` 都已就位，执行自检并启动服务：

```bash
sudo -u vpsops /opt/vpsops/.venv/bin/vpsops-gateway check -c /etc/vpsops-gateway/gateway.yaml
install -m 0644 /opt/vpsops/deploy/gateway/vpsops-gateway.service /etc/systemd/system/vpsops-gateway.service
systemctl daemon-reload
systemctl enable --now vpsops-gateway
systemctl status vpsops-gateway --no-pager
```

如果启动报错，用 `journalctl -u vpsops-gateway -n 100 --no-pager` 看日志排查。以后每次修改 `gateway.yaml` 后，先检查配置，等正在执行的部署任务结束后再重启 Gateway；重启会清空租约和内存中的任务记录。

### 4. 本机安装代理并接入 AI 客户端（Windows）

如果你不用前面的一键脚本，也可以手动装：先装好 [Git for Windows](https://git-scm.com/downloads/win) 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后在 PowerShell 里运行：

```powershell
uv tool install --python 3.11 "vpsops[proxy] @ git+https://github.com/Saito-Sakuya/vpsops.git"
uv tool update-shell
```

重开终端让环境变量生效，然后为每个要用的客户端分别登录并自检（按提示输入 `https://你的跳板机域名:8740` 和前面生成的明文 Token；用 Tailscale 证书的话，提示填自签 CA 路径时直接回车跳过；Token 会安全保存在 Windows 凭据管理器里）：

```powershell
vpsops-proxy --client claude-code login
vpsops-proxy --client claude-code check
vpsops-proxy --client codex login
vpsops-proxy --client codex check
```

如果还用 Devin Desktop，同样跑一遍 `vpsops-proxy --client devin-desktop login` 和 `check`。

最后把 MCP 服务配进对应的 AI 客户端；已有配置时只添加对应条目，保留其他 MCP 服务：

- **Claude Code**：

  ```powershell
  claude mcp add --scope user vpsops -- vpsops-proxy --client claude-code
  ```
- **Codex**（编辑 `~/.codex/config.toml`）：

  ```toml
  [mcp_servers.vpsops]
  command = "vpsops-proxy"
  args = ["--client", "codex"]
  ```
- **Devin Desktop**：打开 MCP 设置，新增一个 `stdio` 类型的服务，`command` 填 `vpsops-proxy`，`args` 填 `--client devin-desktop`。

配好后重启客户端，让 AI 先调一次 `hosts` 看看能不能正常列出你的机器。日后升级本机代理只需跑 `uv tool upgrade vpsops`（如果在本地克隆了源码，也可以在仓库根目录下跑 `uv tool install '.[proxy]'`），升级完重启一下 AI 客户端即可。
