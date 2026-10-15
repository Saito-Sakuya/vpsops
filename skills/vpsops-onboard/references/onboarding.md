# 添加服务器

这份流程同时供人工操作和 [vpsops-onboard Skill](../SKILL.md) 使用。

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

Agent 由受限 SSH 强制调用，读取一次 JSON、返回结果后退出，无需启动常驻服务。安装和改白名单用管理员入口，`ai-ops` 账号只用于调用 Agent。密码和 Token 由你在对应机器交互输入；Gateway 私钥留在主控机。

**白名单要求：** 修改前备份已有配置和授权；按已确认的范围登记真实日志源、配置路径、服务和部署目标，删除新装模板中不适用的示例。白名单是整台 VPS 共用的，已有能力超出本次授权范围时先确认如何处理。`/etc/ai-agent.yaml` 保持 root 所有、权限 600。

## A. 主控机器远程操作

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
printf '%s\n%s\n' "$GATEWAY_PUBKEY" "$JUMP_IP" |
  ssh -p "$VPS_PORT" "root@$VPS_HOST" 'IFS= read -r pubkey && IFS= read -r jump_ip &&
    sh /tmp/vpsops/deploy/vps/install.sh "$pubkey" "$jump_ip"'
```

**2. 通过管理员 SSH 配置目标机白名单并做本机检查。** 按前述要求编辑目标 VPS 的 `/etc/ai-agent.yaml`。

在目标 VPS 上运行以下检查，通过控制台或已验证的管理连接取得实际结果：

```bash
printf '%s\n' '{"op":"describe"}' | sudo /usr/local/bin/ai-agent
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

核对 `describe` 的能力清单，保存真实指纹供后续核对。它表明 Agent 能读取白名单，实际日志、服务等能力仍需在连通后查询。

**3. 在主控机继续完成下方的[登记与验收](#主控机器完成登记与验收)。**

**给 AI：** 先让它读取 [vpsops-onboard](../SKILL.md)，再说：

> 从主控机远程接入这台 VPS，复用现有部署，完成白名单、Gateway 登记和 MCP 验收。

## B. Agent 宿主机本机操作

适合 AI 的当前终端就在新 VPS 上，或你只给了它这台 VPS 的管理权限。先从主控机取得公钥和真实来源 IPv4；如果走 Tailscale，先确认新 VPS 已接入对应 tailnet，且主控机到该 VPS 的 SSH 网络可达。

**1. 在新 VPS 上安装 Agent。** 以下命令使用 sudo，已在 root shell 时可省略 sudo；将公钥和 IP 换成实际值。

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates
GATEWAY_PUBKEY='ssh-ed25519 AAAA... vpsops-gateway'
JUMP_IP=100.64.0.5
curl -fsSL https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/deploy/install.sh | sudo sh -s -- agent "$GATEWAY_PUBKEY" "$JUMP_IP"
```

**2. 配置白名单并做本机检查。** 以 root 按前述要求编辑 `/etc/ai-agent.yaml`，然后运行：

```bash
printf '%s\n' '{"op":"describe"}' | sudo /usr/local/bin/ai-agent
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

**3. 把实际结果交给主控机。** 提供主机别名、环境、Gateway 可达地址、SSH 端口、`ai-ops` 用户、ED25519 主机指纹、已登记的能力及本机 `describe` 的结果。接下来由有主控机权限的人或 AI 完成下方的登记与验收；本机检查通过说明 Agent 能执行，还需要验证 Gateway 到它的 SSH 通道。

**给 AI：** 先让它读取 [vpsops-onboard](../SKILL.md)，再说：

> 当前终端就是新 VPS。安装 Agent、配置白名单并做本机检查，整理交给主控机的接入信息；没有主控权限的步骤列为待办。

## 主控机器完成登记与验收

A、B 两种方式最后都在已有 Gateway 上完成以下步骤。示例在主控机的 root shell 中执行，使用同一台新 VPS 的实际地址和端口。

**1. 核对并保存主机公钥。**

```bash
VPS_HOST=10.0.0.13
VPS_PORT=22
VPS_HOST_KEY_FILE=$(mktemp /tmp/vpsops-hostkey.XXXXXX)
ssh-keyscan -p "$VPS_PORT" -t ed25519 "$VPS_HOST" > "$VPS_HOST_KEY_FILE"
ssh-keygen -lf "$VPS_HOST_KEY_FILE"
```

把扫描结果与目标 VPS 通过控制台或已验证管理连接提供的指纹逐项核对。指纹不一致或与已有记录冲突时先查清原因，不绕过 SSH 主机校验。匹配后再追加到现有的 `known_hosts`，保留其他服务器的条目；相同记录已存在时无需重复追加：

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

客户端的 `envs` 需要包含这台服务器的环境才能看见它；按已确认的权限范围设置，继续使用原有客户端 Token。如果需要扩大客户端权限，先确认授权。

**3. 检查配置并重启 Gateway。** 重启会清空现有租约和内存中的任务记录，应在正在执行的部署任务结束后进行。

```bash
sudo -u vpsops /opt/vpsops/.venv/bin/vpsops-gateway check -c /etc/vpsops-gateway/gateway.yaml &&
  systemctl restart vpsops-gateway &&
  systemctl is-active vpsops-gateway
```

**4. 回到已接入 MCP 的 AI 客户端验收。** 调用 `hosts`，确认新别名和预期能力出现在结果中，再用 `status` 的 `host` 参数指定该别名检查实际状态；按需要读取一个已登记的日志源。`check`、本机 `describe`、Gateway SSH 检查和 MCP 查询应分别报告实际结果，未执行的项目列为待办。
