# vpsops

让 Claude Code、Codex、Devin Desktop 通过跳板机**安全受控地**打理你的 VPS：查日志、改配置、重启服务、发版本，同时把权限关进笼子里，不给 AI 留任何越权跑路的机会。

详细架构与安全设计见 [docs/design.md](docs/design.md)。

```
AI 客户端 ─stdio─► vpsops-proxy ──Tailscale/HTTPS──► vpsops-gateway ──SSH──► ai-agent（每台 VPS）
 （本机）           本地记账 + 对账                    鉴权 / 租约 / 审计       按白名单执行
```

### 核心防线

- **绝不开放任意 Shell 命令**：总共只暴露 9 个固定工具。每台 VPS 允许 AI 干什么，完全由那台机器上 `root` 维护的 `/etc/ai-agent.yaml` 白名单说了算。
- **跳板机沦陷也拿不到 VPS Shell**：SSH 密钥通过 `command=` 强制绑定到 `ai-agent` 进程，哪怕 Gateway 所在跳板机被攻破，攻击者也无法借此拿到目标 VPS 的交互式 Shell。
- **改配置、发版本自带防呆与自动回滚**：
  - 改配置走「精确匹配替换 → 语法校验 → 原子写入 → 重载生效 → 状态检查」完整流水线，任何一步失败都会自动还原备份。
  - 发版本如果健康检查不过，自动回滚到上一个可用版本。
- **生产环境（prod）写操作必须过 Telegram 审批**：操作 `prod` 机器前，你得先在 Telegram 机器人消息上点一下批个临时租约（可选 30 分钟 / 2 小时 / 4 小时），而且不同 AI 客户端的租约彼此独立。
- **本地与跳板机每 60 秒对账防盗用**：本机代理（`vpsops-proxy`）与跳板机（`vpsops-gateway`）每分钟核对一次调用流水，一旦发现某次调用不是从你本机发出的（例如 Token 泄露被盗刷），立刻自动吊销该 Token。

## 目录结构

| 路径 | 说明 |
|---|---|
| `agent/ai_agent.py` | VPS 端受控执行脚本，单文件，仅依赖 Python 标准库与 `python3-yaml` |
| `src/vpsops/gateway/` | 跳板机 Gateway 服务端（基于 `aiohttp` + `asyncssh`） |
| `src/vpsops/proxy/` | 本机 MCP stdio 代理（基于 `mcp` + `httpx` + `keyring`） |
| `src/vpsops/common/` | 公共工具定义、规范化 JSON 序列化与摘要计算 |
| `deploy/vps/` | VPS 端安装脚本、`sudoers` 规则、`sshd` 限制与配置示例 |
| `deploy/gateway/` | Gateway 配置模板与 `systemd` 服务单元 |
| `deploy/install.sh` / `deploy/install.ps1` | Linux / Windows 一键安装脚本 |

## 交给 AI Agent 自动安装

不想自己一行行敲命令？把下面这段话直接发给能跑终端命令的 AI Agent（如 Claude Code 或 Codex）：

> 请先阅读 https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/README.md，按文档在我的环境中安装配置 vpsops 并接入当前 AI 客户端。先检查并复用已有部署；如果缺少部署范围、主机 IP、SSH 或域名信息请先向我确认；涉及密码或 Token 等敏感凭据时停下来让我在对应机器上手动输入；装好后依次跑 `check`、`hosts` 和 `status` 实测验收，最后给我列一份已完成项与待办清单。

如果你已经有现成的跳板机 Gateway，只装本机代理就行；如果要让 AI 顺带把跳板机 Gateway 或 VPS Agent 也装好，需要给它对应机器的 SSH 和管理员权限。

已有部署、只想再接入一台 VPS 时，使用下文[后续添加服务器](#后续添加服务器)中的专用步骤和 AI 提示词。

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

后续更新代码和依赖时，直接重跑同一条安装命令，跑完重启一下 `vpsops-gateway` 服务即可。

### 3. 每台被管 Linux VPS（`ai-agent`）

在目标 VPS 上运行下面命令，注意把公钥和来源 IP 换成你跳板机的真实信息（如果已经在 `root` 下可去掉 `sudo`）：

```bash
curl -fsSL https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/deploy/install.sh | sudo sh -s -- agent 'ssh-ed25519 AAAA... vpsops-gateway' '100.64.0.5'
```

- **公钥**：去跳板机的 `/etc/vpsops-gateway/id_ed25519.pub` 里复制。
- **来源 IP**：填这台 VPS 实际看到的跳板机 SSH 来源 IP（走 Tailscale 内网的话就是跳板机的 Tailscale IP）。
- **后续动作**：装完后用 `root` 编辑 VPS 上的 `/etc/ai-agent.yaml` 填好白名单，再回到跳板机核对并保存该 VPS 的 SSH 主机指纹（具体步骤见下文）。

## 手动分步安装指南

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
- `telegram.chat_id`：填接收审批消息的 Telegram 会话 ID，并把 Bot Token 写进 `/etc/vpsops-gateway/tg.token`（如果不配 Telegram，所有 `prod` 环境的写操作都无法审批通过）。

然后配置 HTTPS 证书。先在 Tailscale 管理后台开启 [HTTPS 证书功能](https://tailscale.com/docs/how-to/set-up-https-certificates)，再把下面命令里的域名换成跳板机真实的 tailnet 域名来申请证书：

```bash
TAILNET_NAME=jump.your-tailnet.ts.net
tailscale cert --cert-file=/etc/vpsops-gateway/tls.crt --key-file=/etc/vpsops-gateway/tls.key "$TAILNET_NAME"
chown root:vpsops /etc/vpsops-gateway/tls.crt /etc/vpsops-gateway/tls.key
chmod 0640 /etc/vpsops-gateway/tls.crt /etc/vpsops-gateway/tls.key
```

稍后本机登录时，地址必须填这个域名（例如 `https://jump.your-tailnet.ts.net:8740`）才能通过证书校验。记得定期续期 `tailscale cert` 生成的证书，续期后重启一下 Gateway。

### 2. 从跳板机往各台 VPS 部署 Agent

继续在跳板机的 `root` shell 中操作，每接入一台新 VPS 就跑一遍。把 `VPS_HOST` 换成目标 VPS 的 SSH 地址，`JUMP_IP` 换成 **VPS 视角下看到的跳板机来源 IP**（走 Tailscale 的话一般就是跳板机的 Tailscale IP）：

```bash
cd /opt/vpsops
VPS_HOST=10.0.0.11
JUMP_IP=100.64.0.5

ssh "root@$VPS_HOST" 'apt-get update && apt-get install -y python3 python3-yaml sudo && mkdir -p /tmp/vpsops'
scp -r agent deploy "root@$VPS_HOST:/tmp/vpsops/"
GATEWAY_PUBKEY=$(cat /etc/vpsops-gateway/id_ed25519.pub)
ssh "root@$VPS_HOST" "sh /tmp/vpsops/deploy/vps/install.sh '$GATEWAY_PUBKEY' '$JUMP_IP'"
```

脚本跑完后，到那台 VPS 上用 `root` 编辑 `/etc/ai-agent.yaml`，按需配置允许 AI 操作的日志源、配置文件路径、系统服务和发布目标（如果用 Docker Compose，记得把 Compose 目录映射也填对）。建议同时收紧 VPS 防火墙，只允许跳板机 IP 和你自己的运维入口连 SSH。

接着回到跳板机，抓取这台 VPS 的 SSH 主机公钥指纹：

```bash
ssh-keyscan -t ed25519 "$VPS_HOST" > /tmp/vpsops-host-key
ssh-keygen -lf /tmp/vpsops-host-key
```

去 VPS 控制台跑一下 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`，肉眼核对两边指纹一致后，把它写进跳板机的 `known_hosts` 并测试连通性：

```bash
cat /tmp/vpsops-host-key >> /etc/vpsops-gateway/known_hosts
chown root:vpsops /etc/vpsops-gateway/known_hosts
chmod 0640 /etc/vpsops-gateway/known_hosts
printf '%s\n' '{"op":"describe"}' | sudo -u vpsops ssh -T \
  -i /etc/vpsops-gateway/id_ed25519 \
  -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/etc/vpsops-gateway/known_hosts "ai-ops@$VPS_HOST"
```

如果终端顺利返回一段描述该机器白名单能力的 JSON，说明通道打通了，记得把这台机器加进跳板机的 `/etc/vpsops-gateway/gateway.yaml`。

### 3. 自检并启动 Gateway

确认跳板机上的 `gateway.yaml`、Token 哈希、Telegram Token、TLS 证书和 `known_hosts` 都已就位，执行自检并启动服务：

```bash
sudo -u vpsops /opt/vpsops/.venv/bin/vpsops-gateway check -c /etc/vpsops-gateway/gateway.yaml
install -m 0644 /opt/vpsops/deploy/gateway/vpsops-gateway.service /etc/systemd/system/vpsops-gateway.service
systemctl daemon-reload
systemctl enable --now vpsops-gateway
systemctl status vpsops-gateway --no-pager
```

如果启动报错，用 `journalctl -u vpsops-gateway -n 100 --no-pager` 看日志排查。以后每次修改 `gateway.yaml` 后都要跑一次 `systemctl restart vpsops-gateway`。

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

最后把 MCP 服务配进对应的 AI 客户端：

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

## 后续添加服务器

已有 Gateway 和本机代理时，新增服务器复用现有 Gateway SSH 密钥及客户端 Token。需要完成两部分：在新 VPS 上安装并配置 Agent，再到 Gateway 登记该主机、核对 SSH 指纹并验证实际连通性。

下文的**主控机器**指运行 `vpsops-gateway` 的跳板机，**Agent 宿主机**指这次要加入的新 VPS。AI 在你的电脑上运行时，可先通过已有管理 SSH 登录相应机器，再执行对应步骤；安装和白名单配置需要管理员权限。

接入前先确定以下信息，示例值均需换成实际值：

| 信息 | 示例与说明 |
|---|---|
| 主机别名与环境 | `web-3`、`prod`；环境只能是 `dev` / `staging` / `prod`，按实际用途确定。 |
| Gateway 可达的 SSH 地址、端口 | `10.0.0.13`、`22`；同时确认用于初次安装的管理员 SSH 入口。 |
| 跳板机公钥 | 主控机 `/etc/vpsops-gateway/id_ed25519.pub` 的完整单行内容；私钥保留在主控机。 |
| 跳板机来源 IPv4 | 新 VPS 实际看到的跳板机 SSH 来源，如 `100.64.0.5`；当前一键脚本的该参数只接受 IPv4。 |
| 允许管理的能力 | 这台机器实际需要开放的日志、配置、服务和部署目标。 |

先检查新 VPS 是否已有 Agent、白名单及 `ai-ops` 授权，只做本次接入需要的变更。现有 VPS 安装脚本会重写 `ai-ops` 的 `authorized_keys`，重用已有安装前应核对并保留仍需使用的授权。

### A. 主控机器远程操作

适合 AI 可以管理 Gateway，并且能通过管理员 SSH 访问新 VPS 的情况。下面的命令在主控机器的 `root` shell 中执行，示例使用目标 VPS 的 `root` SSH；使用普通管理账户时，需按该账户的 sudo 权限完成目标机安装。

**1. 读取现有公钥，把安装文件分发到新 VPS 并安装。**

```bash
cd /opt/vpsops
VPS_HOST=10.0.0.13
VPS_PORT=22
JUMP_IP=100.64.0.5
GATEWAY_PUBKEY=$(cat /etc/vpsops-gateway/id_ed25519.pub)

ssh -p "$VPS_PORT" "root@$VPS_HOST" 'apt-get update && apt-get install -y python3 python3-yaml sudo && mkdir -p /tmp/vpsops'
scp -P "$VPS_PORT" -r agent deploy "root@$VPS_HOST:/tmp/vpsops/"
ssh -p "$VPS_PORT" "root@$VPS_HOST" "sh /tmp/vpsops/deploy/vps/install.sh '$GATEWAY_PUBKEY' '$JUMP_IP'"
```

**2. 通过管理员 SSH 配置目标机白名单。** 编辑目标 VPS 的 `/etc/ai-agent.yaml`，按已确认的范围登记真实日志源、配置路径、服务和部署目标；保留已有配置。通过 VPS 控制台或已经验证的管理连接取得 `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` 的输出，供后面的指纹核对使用。

**3. 在主控机继续完成下方的[登记与验收](#主控机器完成登记与验收)。**

**可直接交给 AI 的提示词：**

> 请阅读 https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/README.md 的“后续添加服务器”，通过主控机器的管理员 SSH 为现有 vpsops 接入一台新 VPS；先核对我提供的别名、环境、SSH 地址与端口、管理入口和能力范围，缺失的信息再向我确认，复用现有 Gateway 密钥和客户端 Token，完成远程安装、白名单配置、可信主机指纹核对、Gateway 主机登记及配置检查，在现有部署任务结束后重启 Gateway，保留既有配置和其他服务器，最后通过 MCP 的 hosts 与指定主机的 status 实测验收；敏感凭据由我交互输入，无法完成的步骤明确列为待办。

### B. Agent 宿主机本机操作

适合 AI 的当前终端就在新 VPS 上，或你只给了它这台 VPS 的管理权限。先从主控机取得公钥和真实来源 IPv4；如果走 Tailscale，先确认新 VPS 已接入对应 tailnet，且主控机到该 VPS 的 SSH 网络可达。

**1. 在新 VPS 上安装 Agent。** 以下命令使用 sudo，已在 root shell 时可省略 sudo；将公钥和 IP 换成实际值。

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates
GATEWAY_PUBKEY='ssh-ed25519 AAAA... vpsops-gateway'
JUMP_IP=100.64.0.5
curl -fsSL https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/deploy/install.sh | sudo sh -s -- agent "$GATEWAY_PUBKEY" "$JUMP_IP"
```

**2. 配置白名单并做本机检查。** 以 root 编辑 `/etc/ai-agent.yaml`，登记已经确认的能力，然后运行：

```bash
printf '%s\n' '{"op":"describe"}' | sudo /usr/local/bin/ai-agent
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

**3. 把实际结果交给主控机。** 提供主机别名、环境、Gateway 可达地址、SSH 端口、`ai-ops` 用户、ED25519 主机指纹、已登记的能力及本机 `describe` 的结果。接下来由有主控机权限的人或 AI 完成下方的登记与验收；本机检查通过说明 Agent 能执行，还需要验证 Gateway 到它的 SSH 通道。

**可直接交给 AI 的提示词：**

> 请阅读 https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/README.md 的“后续添加服务器”，当前终端就是待接入的 Agent 宿主机；先核对我提供的主控机公钥、真实来源 IPv4、服务器别名、环境、SSH 地址与端口和能力范围，缺失的信息再向我确认，在这台 VPS 上安装或核对 ai-agent、保留现有配置与必要授权并配置白名单，执行本机 describe 检查并取得真实 ED25519 主机指纹，最后输出给主控机的接入信息和实测结果；如果没有主控机权限，将 Gateway 登记、指纹核对和 MCP 验收明确列为待办，敏感凭据由我交互输入。

### 主控机器完成登记与验收

A、B 两种方式最后都在已有 Gateway 上完成以下步骤。示例在主控机的 root shell 中执行，使用同一台新 VPS 的实际地址和端口。

**1. 核对并保存主机公钥。**

```bash
VPS_HOST=10.0.0.13
VPS_PORT=22
VPS_HOST_KEY_FILE=$(mktemp /tmp/vpsops-hostkey.XXXXXX)
ssh-keyscan -p "$VPS_PORT" -t ed25519 "$VPS_HOST" > "$VPS_HOST_KEY_FILE"
ssh-keygen -lf "$VPS_HOST_KEY_FILE"
```

把扫描结果与目标 VPS 通过控制台或已验证管理连接提供的指纹逐项核对。匹配后再追加到现有的 `known_hosts`，保留其他服务器的条目：

```bash
cat "$VPS_HOST_KEY_FILE" >> /etc/vpsops-gateway/known_hosts
chown root:vpsops /etc/vpsops-gateway/known_hosts
chmod 0640 /etc/vpsops-gateway/known_hosts
printf '%s\n' '{"op":"describe"}' | sudo -u vpsops ssh -T -p "$VPS_PORT" \
  -i /etc/vpsops-gateway/id_ed25519 \
  -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/etc/vpsops-gateway/known_hosts "ai-ops@$VPS_HOST"
```

确认返回 `ok: true` 及预期白名单能力，再继续登记。

**2. 追加 Gateway 主机条目。** 在 `/etc/vpsops-gateway/gateway.yaml` 的现有 `hosts:` 下追加新条目，其他主机和配置保持原样；下面展示追加后该部分的结构：

```yaml
hosts:
  web-3:
    addr: 10.0.0.13
    env: prod
    port: 22
    user: ai-ops
```

客户端的 `envs` 需要包含这台服务器的环境才能看见它；按已确认的权限范围设置，继续使用原有客户端 Token。

**3. 检查配置并重启 Gateway。** 重启会清空现有租约和内存中的任务记录，应在正在执行的部署任务结束后进行。

```bash
sudo -u vpsops /opt/vpsops/.venv/bin/vpsops-gateway check -c /etc/vpsops-gateway/gateway.yaml &&
  systemctl restart vpsops-gateway &&
  systemctl is-active vpsops-gateway
```

**4. 回到已接入 MCP 的 AI 客户端验收。** 调用 `hosts`，确认新别名和预期能力出现在结果中，再用 `status` 的 `host` 参数指定该别名检查实际状态；按需要读取一个已登记的日志源。`check`、本机 `describe`、Gateway SSH 检查和 MCP 查询应分别报告实际结果，未执行的项目列为待办。

## 日常怎么用

### 首次连通自检

1. 在终端跑 `vpsops-proxy --client <客户端名> login` 录入跳板机 HTTPS 地址和明文 Token（客户端名填 `codex`、`claude-code` 或 `devin-desktop`）。
2. 跑 `vpsops-proxy --client <客户端名> check`，确认能连上 Gateway、拿到 9 个工具，且本地与远端账本核对通过。
3. 重启接入了 MCP 的 AI 客户端，在对话框里让 AI 调 `hosts` 看看能刷出哪些机器和白名单能力，再让它调 `status` 巡检一下各机器状态。

> 注：`check` 只验证你电脑到跳板机 Gateway 这一段是否正常；跳板机到各台 VPS 的 SSH 连通性和具体业务状态，要靠 `hosts` 和 `status` 来验证。另外，直接在终端里光跑 `vpsops-proxy` 不带 `login`/`check` 子命令时，它会卡住等待标准输入——这是 MCP stdio 服务的正常表现，日常操作都在 AI 客户端聊天框里发起。

### 常用指令示例（可直接复制给 AI）

表里的主机名（`web-1`）、日志名、配置名、服务名都是举例，实际用的时候换成 `hosts` 查出来的真实名字即可：

| 场景 | 你可以直接对 AI 说的话 | 底层调用的工具 |
|---|---|---|
| 盘点可用机器 | 用 vpsops 看看我现在能管哪些机器、分别是什么环境，每台机器开放了哪些操作权限。 | `hosts` |
| 巡检机器状态 | 帮我巡检所有可见主机的负载、内存、磁盘、挂掉的服务和容器状态，把有异常的机器单列出来。 | `status` |
| 跨机器查日志 | 查一下 web-1 和 web-2 上 api 最近 15 分钟的最后 200 行日志，过滤出带 ERROR 或 Exception 的报错并汇总。 | `logs` |
| 查看配置文件 | 读取 web-1 上的 nginx 配置，给我讲讲现在的设置，并记下返回的 sha256。 | `config_read` |
| 修改配置文件 | 把 web-1 的 nginx 配置里的 worker_connections 改成 2048。先读一下最新内容再做精确替换，写明修改原因并检查生效结果。 | `config_read` → `config_edit` |
| 回滚配置文件 | 把 web-1 的 nginx 配置还原到最近一次备份，原因是撤回刚才的调整，还原后检查一下配置和服务状态。 | `config_restore` |
| 重启/重载服务 | 重启一下 web-1 上的 api 服务（原因：刚更新了应用配置），完事后告诉我服务状态。 | `service` |
| 发布新版本 | 把 web-1 上的 api 部署到 v1.2.3（原因：发布 v1.2.3 版本），拿到任务 ID 后帮我盯着进度直到跑完。 | `deploy` → `job` |
| 紧急回滚版本 | web-1 上的 api 当前版本有问题，帮我回滚到上一版，跟进任务状态并告诉我回滚结果。 | `deploy`（`version="rollback"`）→ `job` |

**使用小贴士**：
- **查日志（`logs`）**：支持文件日志、`journald` 和 Docker Compose 服务日志。其中 `since`（按时间范围过滤）只对 `journald` 和 Compose 生效；纯文件日志请用 `lines` 和 `grep` 过滤。
- **管服务（`service`）**：支持 `restart`、`reload`、`up` 三种动作，但 Docker Compose 服务不支持 `reload`。
- **发版本（`deploy`）**：传入的版本号必须符合该目标在 VPS 白名单里配置的正则格式；回滚（`version="rollback"`）要求机器上确实存在上一次成功部署的记录。

### 生产环境审批（Telegram 租约）与任务查询

- **权限分级**：只要客户端有某台机器的访问权限，**读操作**（看状态、查日志、读配置）随时直接放行；`dev` / `staging` 环境的**写操作**也直接执行；唯独 `prod` 环境的**写操作**，要求当前客户端手里必须有未过期的「租约」。
- **怎么批租约**：当 AI 发起 `prod` 写操作而手头没租约时，接口会返回 `pending_approval` 和一个 `job` ID，同时你的 Telegram 会收到一条审批消息。你在 Telegram 上点选授权时长（**30 分钟 / 2 小时 / 4 小时**）后，Gateway 就会自动开始执行刚才挂起的那条请求。
- **租约有效期与撤销**：在租约有效期内，同一个客户端再发 `prod` 写操作会直接放行（并在 Telegram 上发通知留痕）；不同客户端之间的租约完全独立。你可以随时在 Telegram 上点撤销租约，或者等它到期 / 空闲超时自动失效。撤销租约只会拦住后续的新写操作，不会回滚已经跑完的操作。
- **怎么查异步任务（`job`）**：发布任务（`deploy`）和等待审批的操作都会异步返回一个 `job` 字符串。查进度时，把这个值传给 `job` 工具的 **`id` 参数**即可。如果正在等 Telegram 审批，先去手机上点完同意再让 AI 查；如果任务正在跑（`running`），让 AI 隔 30 秒轮询一次就行，千万别重复提交同一个写请求。

| `job` 状态码 | 代表什么意思 / 下一步该干嘛 |
|---|---|
| `pending_approval` | 卡在 Telegram 审批了，操作还没执行。先去 Telegram 点同意，再回来查。 |
| `running` | 正在后台执行中，等个 30 秒再查。 |
| `done` | 执行成功，具体输出看返回里的 `result` 字段。 |
| `failed` | 执行失败，仔细看报错信息和自动回滚结果，修好问题再决定要不要重试。 |
| `rejected` | 你在 Telegram 上点了拒绝，本次操作作废。 |
| `expired` | 审批超时没点，本次操作自动作废。 |

### 改配置时的防呆机制

修改配置必须先调 `config_read` 拿到当前文本和 `sha256`，再把 `sha256` 连同要替换的 `edits` 列表传给 `config_edit`：
- 每一处修改的 `old` 字符串必须在原文件里**恰好唯一匹配一次**，匹配不到或匹配到多处都会直接拒改。
- 如果改之前文件被别人动过导致 `sha256` 对不上，需要先重新 `config_read`。
- 被白名单规则脱敏隐藏的敏感行（secret 行）严禁通过 AI 修改。
- 万一修改或重载失败，仔细看返回结构体里的报错和 `rolled_back` 字段，确认是否真的回滚成功，别光看一句“已尝试恢复”就以为万事大吉。

### MCP 工具传参示例

下面是 AI 调用 MCP 工具时传的底层 JSON 格式参考：

**查日志（`logs`）**：

```json
{
  "hosts": ["web-1", "web-2"],
  "source": "api",
  "lines": 200,
  "since": "15m",
  "grep": "ERROR|Exception"
}
```

**发版本（`deploy`）**：

```json
{
  "host": "web-1",
  "target": "api",
  "version": "v1.2.3",
  "reason": "发布版本 v1.2.3"
}
```

调用后会返回一个 `job` ID，紧接着把它填进 `job` 工具的 `id` 参数里查进度即可。

### 常见问题排查（FAQ）

- **AI 客户端报错找不到 `vpsops-proxy` 命令**：
  先在终端跑一次 `uv tool update-shell`，然后关掉终端和 AI 客户端重新打开。如果还找不到，跑 `uv tool dir --bin` 查出可执行文件的完整路径，把 MCP 配置里的 `command` 直接改成绝对路径。
- **连不上 Gateway 或报 TLS 证书域名不匹配**：
  检查本机和跳板机的 Tailscale 是否在线、ACL 端口有没放行、Gateway 服务有没跑起来；`login` 时输入的地址必须是跟 TLS 证书一致的 tailnet 域名（不能直接填 IP）。跳板机端日志可以用 `journalctl -u vpsops-gateway -n 100 --no-pager` 查看。
- **刷新不出某台主机，或者提示某个日志/服务名不存在**：
  如果连主机都看不到，去跳板机检查 `/etc/vpsops-gateway/gateway.yaml` 里该客户端的 `envs` 权限和 `hosts` 列表；如果主机能看到但提示没某项能力，去对应 VPS 上检查 `/etc/ai-agent.yaml` 有没有把那个日志、配置、服务或部署目标加进白名单。
- **`prod` 写操作一直卡在等待授权（`pending_approval`）**：
  去看看 Telegram 有没有收到机器人发来的审批卡片，点选租约时长后再查 `job`。如果跳板机没配好 Telegram，所有 `prod` 写操作都拿不到租约。
- **提示 Token 已被吊销（Revoked）**：
  说明对账机制发现了异常调用并触发了熔断。需要去跳板机重新跑 `/opt/vpsops/.venv/bin/vpsops-gateway token new <客户端名>` 生成新 Token，把新的 `token_sha256` 更新到 `gateway.yaml` 并重启 `vpsops-gateway`，最后回本机重新 `login` 和 `check`（光在本机重登旧 Token 是没用的）。

## 本地开发与测试

```bash
git clone https://github.com/Saito-Sakuya/vpsops.git
cd vpsops
uv sync --locked
uv run pytest
```

测试套件完整覆盖了以下核心场景：
- **`ai-agent` 单元测试**：敏感行脱敏、精确唯一替换、语法校验或重载失败自动还原、禁止修改脱敏行、发布失败自动回滚。
- **Gateway + Proxy 端到端测试**：Telegram 租约审批、拒绝、手动撤销、异步发布任务、模拟 Token 被盗另开新连接、序号（seq）异常插入触发熔断、Gateway 重启恢复。
- **真实通信测试**：MCP stdio 完整往返通信，以及基于本地 `asyncssh` 测试服务的 SSH 命令收发与主机指纹强校验。

## 许可证

本项目基于 [MIT License](LICENSE) 开源，Copyright (c) 2026 Saito-Sakuya。
