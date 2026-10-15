"""Telegram：授权请求、写操作通知、告警。长轮询接收按钮回调，不需要入站端口。

回调鉴权（三项都必须满足）：
  1. 来自配置的 chat_id；
  2. 点击者 from.id 在 approvers 名单内（群聊里其他成员点击无效）；
  3. 按钮所在消息就是 Gateway 发出该按钮的那条消息（防伪造 callback_data）。
"""

import asyncio
import html
import json
import logging
import re

import aiohttp

log = logging.getLogger("vpsops.telegram")


def esc(s) -> str:
    return html.escape(str(s), quote=False)


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str | None, chat_id: int | None, approvers: set[int] | None = None):
        self.token, self.chat_id = token, chat_id
        # 私聊的 chat_id 就是用户 id；群聊必须显式配置 approvers
        if approvers is None and chat_id is not None and chat_id > 0:
            approvers = {chat_id}
        self.approvers: set[int] = set(approvers or ())
        self.enabled = bool(token and chat_id and self.approvers)
        self.on_callback = None  # async (data: str, user_id: int, message_id: int) -> str
        self._session: aiohttp.ClientSession | None = None

    def _scrub(self, text: str) -> str:
        """异常信息里可能带完整 URL（含 bot token），写日志前去掉。"""
        text = str(text)
        if self.token:
            text = text.replace(self.token, "<bot-token>")
        return re.sub(r"bot\d+:[A-Za-z0-9_-]{20,}", "bot<bot-token>", text)

    async def _api(self, method: str, **params):
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        try:
            async with self._session.post(f"https://api.telegram.org/bot{self.token}/{method}",
                                          json=params) as r:
                try:
                    body = await r.json(content_type=None)
                except ValueError:
                    raise TelegramError(f"{method}: HTTP {r.status}，响应不是 JSON")
        except TelegramError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raise TelegramError(f"{method}: {type(e).__name__}: {self._scrub(e)}") from None
        if not isinstance(body, dict) or not body.get("ok"):
            desc = body.get("description") if isinstance(body, dict) else "?"
            raise TelegramError(f"{method}: {self._scrub(desc)}")
        return body["result"]

    async def send(self, text: str, buttons: list[list[tuple[str, str]]] | None = None,
                   silent: bool = False) -> int | None:
        if not self.enabled:
            log.info("[telegram 未配置] %s", text)
            return None
        params = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                  "disable_notification": silent, "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": [
                [{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
        try:
            msg = await self._api("sendMessage", **params)
            return msg["message_id"]
        except TelegramError as e:
            log.error("telegram 发送失败：%s", e)
            return None

    async def edit(self, message_id: int | None, text: str):
        """更新消息文字并移除按钮（处理过的卡片不能再点）。"""
        if not self.enabled or message_id is None:
            return
        try:
            await self._api("editMessageText", chat_id=self.chat_id, message_id=message_id,
                            text=text, parse_mode="HTML", reply_markup={"inline_keyboard": []})
        except TelegramError as e:
            log.warning("telegram 编辑失败：%s", e)

    def authorize(self, cq: dict) -> tuple[bool, str]:
        msg = cq.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        user = (cq.get("from") or {}).get("id")
        if chat != self.chat_id:
            return False, f"chat {chat} 不是配置的会话"
        if user not in self.approvers:
            return False, f"用户 {user} 不在 approvers 中"
        if not isinstance(msg.get("message_id"), int):
            return False, "缺少 message_id"
        return True, ""

    async def poll(self):
        if not self.enabled:
            return
        offset = None
        while True:
            try:
                params = {"timeout": 30, "allowed_updates": ["callback_query"]}
                if offset is not None:
                    params["offset"] = offset
                updates = await self._api("getUpdates", **params)
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                log.warning("telegram 轮询失败：%s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                cq = u.get("callback_query")
                if not cq:
                    continue
                ok, why = self.authorize(cq)
                if not ok:
                    log.warning("拒绝 Telegram 回调：%s", why)
                    note = "无权操作"
                else:
                    try:
                        note = await self.on_callback(str(cq.get("data", "")), cq["from"]["id"],
                                                      cq["message"]["message_id"]) \
                            if self.on_callback else "未处理"
                    except Exception as e:
                        log.error("回调处理失败：%s", self._scrub(e))
                        note = "处理失败，详见 Gateway 日志"
                try:
                    await self._api("answerCallbackQuery", callback_query_id=cq["id"], text=note[:190])
                except TelegramError:
                    pass

    async def close(self):
        if self._session:
            await self._session.close()


def fmt_args(args: dict, limit: int = 1500) -> str:
    text = json.dumps(args, ensure_ascii=False, indent=1)
    return esc(text if len(text) <= limit else text[:limit] + " …")
