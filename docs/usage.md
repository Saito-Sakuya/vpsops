# 日常使用

[返回 README](../README.md) · [安装与客户端配置](installation.md) · [添加服务器](../skills/vpsops-onboard/references/onboarding.md)

连接好 MCP 后，直接在 AI 客户端中描述要做的事。先查 `hosts` 确认真实主机名和可用能力，再执行操作。

## 首次连通自检

1. 在终端跑 `vpsops-proxy --client <客户端名> login` 录入跳板机 HTTPS 地址和明文 Token（客户端名填 `codex`、`claude-code` 或 `devin-desktop`）。
2. 跑 `vpsops-proxy --client <客户端名> check`，确认能连上 Gateway、拿到 9 个工具，且本地与远端账本核对通过。
3. 重启接入了 MCP 的 AI 客户端，在对话框里让 AI 调 `hosts` 看看能刷出哪些机器和白名单能力，再让它调 `status` 巡检一下各机器状态。

> 注：`check` 只验证你电脑到跳板机 Gateway 这一段是否正常；跳板机到各台 VPS 的 SSH 连通性和具体业务状态，要靠 `hosts` 和 `status` 来验证。另外，直接在终端里光跑 `vpsops-proxy` 不带 `login`/`check` 子命令时，它会卡住等待标准输入——这是 MCP stdio 服务的正常表现，日常操作都在 AI 客户端聊天框里发起。

## 常用指令示例（可直接复制给 AI）

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
- **发版本（`deploy`）**：传入的版本号必须符合该目标在 VPS 白名单里配置的正则格式；回滚（`version="rollback"`）要求机器上确实存在上一次成功部署的记录；脚本型部署首次执行时没有旧版本记录，失败后也无法自动回滚到一个未知版本。

## 生产环境审批（Telegram 租约）与任务查询

机器人随 Gateway 运行，先按 [Bot 接入指南](telegram.md)完成创建与消息收发检查。它处理审批按钮，不处理 `/start` 等聊天命令；打开对话后没有欢迎回复是当前实现的正常表现。

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

## 改配置时的防呆机制

修改配置必须先调 `config_read` 拿到当前文本和 `sha256`，再把 `sha256` 连同要替换的 `edits` 列表传给 `config_edit`：

- 每一处修改的 `old` 字符串必须在原文件里**恰好唯一匹配一次**，匹配不到或匹配到多处都会直接拒改。
- 如果改之前文件被别人动过导致 `sha256` 对不上，需要先重新 `config_read`。
- 被白名单规则脱敏隐藏的敏感行（secret 行）严禁通过 AI 修改。
- 万一修改或重载失败，仔细看返回结构体里的报错和 `rolled_back` 字段，确认是否真的回滚成功，别光看一句“已尝试恢复”就以为万事大吉。

## MCP 工具传参示例

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

## 常见问题排查（FAQ）

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
