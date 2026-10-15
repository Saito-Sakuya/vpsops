"""Telegram：授权请求、写操作通知、告警。长轮询接收按钮回调，不需要入站端口。"""

import asyncio
import html
import json
import logging

import aiohttp

log = logging.getLogger("vpsops.telegram")


def esc(s) -> str:
    return html.escape(str(s), quote=False)


class Telegram:
    def __init__(self, token: str | None, chat_id: int | None):
        self.token, self.chat_id = token, chat_id
        self.enabled = bool(token and chat_id)
        self.on_callback = None  # async (data: str) -> str（回显给按钮的提示）
        self._session: aiohttp.ClientSession | None = None

    @property
    def _base(self):
        return f"https://api.telegram.org/bot{self.token}"

    async def _api(self, method: str, **params):
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        async with self._session.post(f"{self._base}/{method}", json=params) as r:
            body = await r.json()
            if not body.get("ok"):
                raise RuntimeError(f"telegram {method}: {body.get('description')}")
            return body["result"]

    async def send(self, text: str, buttons: list[list[tuple[str, str]]] | None = None,
                   silent: bool = False) -> int | None:
        if not self.enabled:
            log.info("[telegram 未配置] %s", text)
            return None
        params = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                  "disable_notification": silent}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": [
                [{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
        try:
            msg = await self._api("sendMessage", **params)
            return msg["message_id"]
        except Exception as e:
            log.error("telegram 发送失败：%s", e)
            return None

    async def edit(self, message_id: int | None, text: str):
        if not self.enabled or message_id is None:
            return
        try:
            await self._api("editMessageText", chat_id=self.chat_id, message_id=message_id,
                            text=text, parse_mode="HTML")
        except Exception as e:
            log.warning("telegram 编辑失败：%s", e)

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
            except Exception as e:
                log.warning("telegram 轮询失败：%s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                cq = u.get("callback_query")
                if not cq:
                    continue
                chat = (cq.get("message") or {}).get("chat", {}).get("id")
                if chat != self.chat_id:  # 只接受配置的那个会话里的按钮
                    log.warning("忽略来自 chat %s 的回调", chat)
                    continue
                note = "?"
                try:
                    note = await self.on_callback(cq.get("data", "")) if self.on_callback else "未处理"
                except Exception as e:
                    log.exception("回调处理失败")
                    note = f"失败：{e}"
                try:
                    await self._api("answerCallbackQuery", callback_query_id=cq["id"], text=note[:190])
                except Exception:
                    pass

    async def close(self):
        if self._session:
            await self._session.close()


def fmt_args(args: dict, limit: int = 1500) -> str:
    text = json.dumps(args, ensure_ascii=False, indent=1)
    return esc(text if len(text) <= limit else text[:limit] + " …")
