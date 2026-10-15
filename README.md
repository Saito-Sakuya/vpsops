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

## 安装

### 1. 跳板机

```bash
sudo useradd --system --home /opt/vpsops vpsops
sudo git clone <本仓库> /opt/vpsops && cd /opt/vpsops
sudo uv venv && sudo uv pip install '.[gateway]'
sudo install -d -m 750 -o root -g vpsops /etc/vpsops-gateway
sudo ssh-keygen -t ed25519 -N "" -C vpsops-gateway -f /etc/vpsops-gateway/id_ed25519
sudo chgrp vpsops /etc/vpsops-gateway/id_ed25519 && sudo chmod 640 /etc/vpsops-gateway/id_ed25519
sudo cp deploy/gateway/gateway.example.yaml /etc/vpsops-gateway/gateway.yaml   # 按实际修改
sudo /opt/vpsops/.venv/bin/vpsops-gateway token new claude-code   # 每个客户端各一次，hash 写进 yaml
```

TLS 证书：`tailscale cert <跳板机的 tailnet 域名>` 可直接拿到受信任证书，放到 `/etc/vpsops-gateway/tls.{crt,key}`。

```bash
sudo cp deploy/gateway/vpsops-gateway.service /etc/systemd/system/
sudo systemctl enable --now vpsops-gateway
```

### 2. 每台 VPS

```bash
scp -r agent deploy root@vps:/tmp/vpsops/
ssh root@vps 'sh /tmp/vpsops/deploy/vps/install.sh "$(cat id_ed25519.pub)" <跳板机IP>'
# 然后编辑 /etc/ai-agent.yaml，并把该主机写进跳板机的 gateway.yaml
```

在跳板机上把主机指纹加入 `known_hosts`（人工核对），并测试：

```bash
echo '{"op":"describe"}' | sudo -u vpsops ssh -i /etc/vpsops-gateway/id_ed25519 ai-ops@10.0.0.11
```

### 3. 本机（Windows）

```bash
uv tool install "git+<本仓库>#egg=vpsops[proxy]"     # 或在仓库目录：uv tool install '.[proxy]'
vpsops-proxy --client claude-code login            # 录入 gateway 地址和 token，存入 Windows 凭据管理器
vpsops-proxy --client claude-code check
```

接入客户端（每个客户端用自己的名称和 token）：

```bash
claude mcp add --scope user vpsops -- vpsops-proxy --client claude-code
```

Codex `~/.codex/config.toml`：

```toml
[mcp_servers.vpsops]
command = "vpsops-proxy"
args = ["--client", "codex"]
```

Devin Desktop：在其 MCP 设置中添加 stdio server，command `vpsops-proxy`，args `--client devin-desktop`。

## 开发

```bash
uv sync
uv run pytest
```

测试覆盖：ai-agent（脱敏、精确替换、校验失败/生效失败自动还原、secret 行不可改、部署回滚），
Gateway+Proxy 端到端（租约、拒绝、撤销、异步部署、偷 token 开新流、插入 seq、gateway 重启），
真实 MCP stdio 往返，以及本地 asyncssh 服务器上的 SSH 收发与主机密钥校验。
