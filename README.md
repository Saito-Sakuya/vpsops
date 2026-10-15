# vpsops

连接本地 AI 与远程服务器的受控 MCP 桥梁：免去繁琐的日志搬运，拒绝失控的越权执行。
让 AI 帮你管理 VPS：查日志、改配置、重启服务、发布版本。支持 Claude Code、Codex 等 MCP 客户端。

```text
你的 AI 客户端 → 本机 Proxy → 跳板机 Gateway → 各台 VPS 的 Agent
```

你决定每台服务器开放哪些操作，AI 通过固定工具执行。生产环境的写操作需要在 Telegram 授权，本机和 Gateway 会定期核对调用记录。

## AI 快速开始

### 第一次安装

把下面这句话发给能运行终端命令的 AI：

> 请阅读 https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/docs/installation.md，检查并复用已有部署，在我的环境中安装 vpsops 并接入当前 AI 客户端；缺少主机、权限等信息时向我确认，敏感凭据由我在对应机器交互输入，完成后用 check、hosts 和 status 实测，列出结果和待办。

已有 Gateway 时，只需安装本机代理；要部署 Gateway 或 VPS Agent，还需要对应机器的管理员权限。自己操作可看[一键安装与完整步骤](docs/installation.md)。

### 再加一台服务器

先让 AI 读取 [vpsops-onboard Skill](skills/vpsops-onboard/SKILL.md)，再按它所在的位置说一句：

- **能管理主控机**：从主控机远程接入这台 VPS，复用现有密钥，完成 Agent 安装、白名单、Gateway 登记和 MCP 验收。
- **就在新 VPS 上**：在当前 VPS 安装 Agent、配置白名单并做本机检查，整理交给主控机的接入信息；没有权限完成的步骤列为待办。

也可以直接发：

> 请读取 https://raw.githubusercontent.com/Saito-Sakuya/vpsops/master/skills/vpsops-onboard/SKILL.md，按我的当前环境为现有 vpsops 接入一台新 VPS；先确认操作位置和管理范围，再完成安装、登记或交接。

Skill 和[人工接入指南](skills/vpsops-onboard/references/onboarding.md)共用一份流程，覆盖主控机远程操作与 Agent 宿主机操作。支持 Skill 的客户端可加载整个 `skills/vpsops-onboard` 目录；也可以直接让 AI 读取文件。

### 装好后怎么用

直接在 AI 对话框里说，例如：

- “看看我能管理哪些机器，巡检一下状态。”
- “查 web-1 的 api 最近 15 分钟日志，汇总报错。”
- “把 web-1 的 api 发布到 v1.2.3，原因是发布新版本，跟进到任务结束。”

主机名和能力名以 `hosts` 返回的结果为准。生产写操作需要你在 Telegram 批准；等待审批时继续查询原任务即可。

## 文档

| 想做什么 | 看这里 |
|---|---|
| 首次部署、一键安装、连接 AI 客户端 | [安装指南](docs/installation.md) |
| 给现有部署添加服务器 | [接入指南](skills/vpsops-onboard/references/onboarding.md) · [AI Skill](skills/vpsops-onboard/SKILL.md) |
| 查日志、改配置、发布、审批与排错 | [使用指南](docs/usage.md) |
| 了解代码结构、运行测试 | [开发指南](docs/development.md) |
| 了解架构与权限设计 | [设计说明](docs/design.md) |

## 许可证

[MIT](LICENSE) · Copyright (c) 2026 Saito-Sakuya
