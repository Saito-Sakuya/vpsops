---
name: vpsops-onboard
description: 将新 VPS 接入已有 vpsops，支持从 Gateway 主控机远程安装，或在 Agent 宿主机本机安装并交接；适用于新增受管服务器、登记主机和接入验收。
---

# 接入 vpsops 服务器

主控机是运行 `vpsops-gateway` 的跳板机；Agent 宿主机是新 VPS。Agent 是被 SSH 强制命令调用的单次 JSON 执行器，安装位置为 `/usr/local/bin/ai-agent`，没有常驻 Agent 服务。

## 确定操作位置

根据当前终端、现有管理入口及用户授权选择路径；AI 在个人电脑上也可通过管理员 SSH 完成远程操作。

- 能管理主控机和新 VPS：读[主控机远程操作](references/onboarding.md#a-主控机器远程操作)，接着完成[登记与验收](references/onboarding.md#主控机器完成登记与验收)。
- 只能管理新 VPS：读[宿主机本机操作](references/onboarding.md#b-agent-宿主机本机操作)，完成本机验证并输出交接信息；取得主控权限后再继续登记。
- VPS 已经安装 Agent：先核对现状，只补缺少的配置、登记或验证。

这是已有部署的接入流程。若没有 Gateway，说明还缺首次部署，先确定用户要完成的部署范围，再查[安装指南](https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/docs/installation.md)。

## 先核对必要信息

复用已知信息，只确认影响当前步骤的缺项：主机别名、用途对应的 `dev` / `staging` / `prod`、Gateway 可达的 SSH 地址与端口、管理员入口、Gateway 公钥、新 VPS 看到的 Gateway 来源 IPv4，以及允许开放的日志、配置、服务和发布目标。不要为省去审批而选择更低的环境级别。

## 执行约束

- 复用官方安装脚本和现有 Gateway 密钥、客户端 Token；私钥留在主控机，只传公钥。敏感凭据由用户在对应机器交互输入。
- 安装和白名单配置走管理员入口；受限 `ai-ops` 账号只用于 Agent 调用。先检查已有白名单和授权：VPS 安装脚本会重写 `ai-ops` 的 `authorized_keys`，保留仍需使用的受限授权，配置变更前备份相关文件。
- 仅开放用户确认的真实能力，参照官方 `deploy/vps/ai-agent.example.yaml` 字段，删除新装模板中不适用的示例；`/etc/ai-agent.yaml` 保持 root 所有、权限 600。白名单是整台 VPS 共用的；已有能力超出本次授权范围时，先确认如何处理，不能声称不同 Gateway 或客户端有各自的能力白名单。
- 将扫描到的 SSH 指纹与 VPS 控制台或已验证管理连接取得的指纹核对后，再更新主控机 `known_hosts`；保留其他主机记录，指纹冲突时先查清原因。
- 在现有 `hosts` 下追加条目；按用户已授权的范围检查客户端 `envs`，不自行扩大权限。配置检查通过、运行中的部署结束后再重启 Gateway，说明重启会清空租约和内存任务记录。

## 分阶段验收与交接

依次记录实际结果：新 VPS 本机 `describe` → 主控机以 `vpsops` 身份执行受限 SSH `describe` → Gateway 配置检查和服务状态 → 实际 MCP `hosts` 与指定 `host` 的 `status`。最后两项需要已连接 MCP 的客户端；缺少执行条件就保留为待办。

`vpsops-proxy --client <客户端名> check` 只验证本机到 Gateway，不代表目标 VPS 接入成功。验收不需要试发版本或重启业务服务。

报告已完成步骤及证据、未完成项和下一步。只有宿主机权限时，交接包包含别名、环境、Gateway 可达地址、SSH 端口、`ai-ops` 用户、真实 ED25519 指纹、能力清单和本机检查结果；将主控登记与 MCP 验收列为待办，避免把“Agent 已装好”写成“接入完成”。
