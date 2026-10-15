# 连接 Telegram 审批机器人

[返回 README](../README.md) · [安装 Gateway](installation.md) · [审批与任务查询](usage.md#生产环境审批telegram-租约与任务查询)

Bot 内置在 Gateway 中，随 `vpsops-gateway` 启停，通过长轮询接收按钮回调，不需要单独的服务、Webhook 或入站端口。跳板机需要能访问 `https://api.telegram.org`。

它负责生产写操作的租约审批、拒绝与撤销，以及租约内写操作通知、租约结束摘要和 Token 吊销告警。服务器操作仍在 MCP 客户端发起；目前没有聊天命令、启动欢迎消息或“部署完成”自动推送，任务结果用 `job` 查询。

## 1. 创建你自己的 Bot

在 Telegram 打开官方 [@BotFather](https://t.me/BotFather)，发送 `/newbot`，按提示设置名字和用户名，保存它给你的 Bot Token。打开新机器人的私聊，点击 **Start**，再发送一条 `vpsops setup`。

建议使用专用 Bot 和自己的私聊。Gateway 对每个按钮回调做三项校验，任何一项不满足都会拒绝并写审计日志：

1. 回调来自配置的 `chat_id`；
2. 点击者的用户 id 在 `approvers` 中。私聊时默认就是 `chat_id` 本身；**`chat_id` 为群聊（负数）时必须显式配置 `approvers`，否则 Gateway 拒绝启动**；
3. 按钮所在的消息正是 Gateway 为该请求发出的那条卡片（防止伪造 callback_data）。

授权后卡片按钮会被移除，重复点击不会让同一操作执行两次。Bot 不处理普通消息，所以此时没有回复也正常。

以下命令在已安装 Gateway 的跳板机上运行。用编辑器把 Token 写成一行，避免放进聊天、命令历史或仓库；这是 Telegram Bot Token，与各 AI 客户端的 vpsops Token 不同。

```bash
sudoedit /etc/vpsops-gateway/tg.token
sudo chown root:vpsops /etc/vpsops-gateway/tg.token
sudo chmod 0640 /etc/vpsops-gateway/tg.token
```

## 2. 取得接收审批的会话 ID

在首次启动 Gateway 前执行下面的查询。若 Gateway 已运行，先等正在执行的部署结束、再停服务，避免两个 `getUpdates` 轮询互相冲突；停机和后续重启会清空租约和内存任务记录。

```bash
sudo systemctl stop vpsops-gateway
sudo -u vpsops /opt/vpsops/.venv/bin/python - <<'PY'
import asyncio
from pathlib import Path
from vpsops.gateway.telegram import Telegram

async def main():
    token = Path('/etc/vpsops-gateway/tg.token').read_text().strip()
    if not token:
        raise SystemExit('请先写入 Bot Token')
    bot = Telegram(token, None)
    try:
        me = await bot._api('getMe')
        hook = await bot._api('getWebhookInfo')
        if hook.get('url'):
            raise SystemExit('此 Bot 已配置 Webhook，请使用专用 Bot 或先处理原部署')
        updates = await bot._api('getUpdates', timeout=0, allowed_updates=['message'])
        chats = {u['message']['chat']['id']: u['message']['chat']['type']
                 for u in updates if 'message' in u}
        print('机器人：@' + me['username'])
        for chat_id, kind in chats.items():
            print(f'chat_id: {chat_id}（{kind}）')
        if not chats:
            raise SystemExit('没有消息：请给上面这个 Bot 再发一条 vpsops setup，然后重试')
    except Exception as exc:
        raise SystemExit(f'查询失败（{type(exc).__name__}）；检查 Token、网络和其他轮询进程') from None
    finally:
        await bot.close()

asyncio.run(main())
PY
```

把你自己私聊对应的 ID 填入 `/etc/vpsops-gateway/gateway.yaml` 的现有 `telegram` 段，保留其他配置：

```yaml
telegram:
  bot_token_file: /etc/vpsops-gateway/tg.token
  chat_id: 123456789  # 换成刚查到的真实 ID
```

## 3. 发一条测试消息

下面的命令读取实际 Gateway 配置，验证 Token，并向配置的会话发送一条测试消息。它不调用 VPS、不创建租约、不执行业务操作。

```bash
sudo -u vpsops /opt/vpsops/.venv/bin/python - <<'PY'
import asyncio
from vpsops.gateway.config import load
from vpsops.gateway.telegram import Telegram

async def main():
    cfg = load('/etc/vpsops-gateway/gateway.yaml')
    bot = Telegram(cfg.tg_token, cfg.tg_chat)
    try:
        if not bot.enabled:
            raise SystemExit('Telegram 配置不完整：需要 Bot Token 和真实 chat_id')
        me = await bot._api('getMe')
        msg = await bot._api('sendMessage', chat_id=cfg.tg_chat,
                             text='vpsops：Telegram 测试消息，审批通知将发送到这里。')
        print(f"已由 @{me['username']} 发送测试消息，message_id={msg['message_id']}")
    except Exception as exc:
        raise SystemExit(f'测试失败（{type(exc).__name__}）；检查 Token、chat_id、网络及是否已点击 Start') from None
    finally:
        await bot.close()

asyncio.run(main())
PY
```

确认手机上收到消息。然后回到[安装指南](installation.md#3-自检并启动-gateway)检查并启动 Gateway；已有部署按计划重启服务，查看 `journalctl -u vpsops-gateway -n 100 --no-pager` 是否有发送或轮询失败。

`vpsops-gateway check` 显示“Telegram：已配置”只代表配置字段存在，不会请求 Telegram API。测试消息成功也只证明发消息的通道可用；按钮回调是否生效，需要在后续一次已授权的生产操作中验证。不要为了验收随意重启或发布业务。

## 日常会看到什么

- AI 首次申请生产写权限：收到操作详情和 **30 分钟 / 2 小时 / 4 小时 / 拒绝** 按钮。授权的是该客户端的临时写权限，批准后挂起的操作会继续执行。
- 租约有效期内继续写入：默认收到静默通知，并可点击 **撤销租约**。
- 租约到期、空闲超时或撤销：仅发送结束原因和写操作次数，详细流水保留在 Gateway 审计日志；Token 异常吊销时收到告警。

若 `pending_approval` 一直不动，先确认收件会话、Bot 是否被屏蔽、跳板机网络及 Gateway 日志；若已经收到卡片但按钮无效，检查 `chat_id`、请求是否已处理或过期、Gateway 是否重启，以及是否有其他进程占用此 Bot 的轮询。

创建流程和 API 参数参照 [Telegram 官方教程](https://core.telegram.org/bots/tutorial)、[Bot API](https://core.telegram.org/bots/api#getupdates)与[长轮询说明](https://core.telegram.org/bots/faq#how-do-i-get-updates)。
