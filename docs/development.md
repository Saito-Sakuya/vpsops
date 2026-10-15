# 本地开发

[返回 README](../README.md) · [架构与安全设计](design.md)

## 运行测试

```bash
git clone https://github.com/Saito-Sakuya/vpsops.git
cd vpsops
uv sync --locked
uv run pytest
```

现有测试包括以下核心场景：

- **`ai-agent` 单元测试**：敏感行脱敏、精确唯一替换、语法校验或重载失败自动还原、禁止修改脱敏行、发布失败自动回滚。
- **Gateway + Proxy 端到端测试**：Telegram 租约审批、拒绝、手动撤销、异步发布任务、模拟 Token 被盗另开新连接、序号（seq）异常插入触发熔断、Gateway 重启恢复。
- **真实通信测试**：MCP stdio 完整往返通信，以及基于本地 `asyncssh` 测试服务的 SSH 命令收发与主机指纹强校验。

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
