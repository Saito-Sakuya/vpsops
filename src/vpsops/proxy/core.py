"""代理核心：开流、编号、转发、校验回执、定期核对、发现异常时自动吊销。与 MCP 无关，便于测试。"""

import asyncio
import json
import logging

import httpx

from vpsops.common.canon import call_digest, set_hash

from .ledger import Ledger, Registry

log = logging.getLogger("vpsops.proxy")


class Revoked(Exception):
    pass


class Core:
    def __init__(self, client: str, url: str, token: str, root, http: httpx.AsyncClient | None = None,
                 reconcile_every: float = 60, notify=None, verify=True):
        self.client = client
        self.root = root
        # trust_env=False：Gateway 只在 Tailscale 内网，不能经过系统里的 HTTP(S)_PROXY
        self.http = http or httpx.AsyncClient(base_url=url.rstrip("/"), verify=verify, timeout=60,
                                              trust_env=False,
                                              headers={"Authorization": f"Bearer {token}"})
        self.registry = Registry(root)
        self.ledger: Ledger | None = None
        self.reconcile_every = reconcile_every
        self.notify = notify or (lambda title, msg: None)
        self.revoked = False
        self.alarm: str | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 流

    async def _open(self):
        r = await self.http.post("/v1/streams")
        self._raise_for(r)
        sid = r.json()["stream"]
        self.registry.add(self.client, sid)
        self.ledger = Ledger(self.root, self.client, sid)
        log.info("打开流 %s", sid)

    async def start(self):
        await self._open()

    async def tools(self) -> list[dict]:
        r = await self.http.get("/v1/tools")
        self._raise_for(r)
        return r.json()["tools"]

    def _raise_for(self, r: httpx.Response):
        if r.status_code == 403 and r.json().get("error") == "revoked":
            self.revoked = True
            raise Revoked("token 已吊销")
        if r.status_code == 401:
            raise RuntimeError("Gateway 拒绝认证：token 无效")
        if r.status_code >= 500:
            raise RuntimeError(f"Gateway 错误 {r.status_code}")

    # ------------------------------------------------------------ 调用

    async def call(self, tool: str, args: dict) -> dict:
        if self.revoked:
            raise Revoked(f"token 已吊销（{self.alarm or '核对失败'}）。请在跳板机生成新 token 后执行 "
                          f"vpsops-proxy login --client {self.client}")
        if self.ledger is None:
            await self._open()
        for attempt in (1, 2):
            async with self._lock:
                seq = self.ledger.take_seq()
                stream = self.ledger.stream
            digest = call_digest(stream, seq, tool, args)
            r = await self.http.post("/v1/call", json={"stream": stream, "seq": seq, "tool": tool, "args": args})
            self._raise_for(r)
            body = r.json()
            if r.status_code == 410 and attempt == 1:  # 流失效（gateway 重启或空闲超时）：开新流重试
                self.registry.close(stream)
                await self._open()
                continue
            if r.status_code == 409 and body.get("error") == "seq_conflict":
                await self._alarm(f"seq {seq} 已被别人使用：有人在用这个客户端的 token 往本流插入调用")
                raise Revoked(self.alarm)
            receipt = body.get("receipt")
            if receipt:
                if receipt.get("digest") != digest or receipt.get("seq") != seq:
                    await self._alarm(f"回执不一致：seq {seq} 本地 {digest[:12]} / gateway "
                                      f"{str(receipt.get('digest'))[:12]}")
                    raise Revoked(self.alarm)
                self.ledger.record(seq, digest, tool, args, str(r.status_code))
            return body.get("result", body)
        raise RuntimeError("流重开失败")

    # ------------------------------------------------------------ 核对

    async def reconcile_once(self) -> bool:
        if self.revoked or self.ledger is None:
            return not self.revoked
        r = await self.http.get("/v1/reconcile", params={"stream": self.ledger.stream})
        self._raise_for(r)
        data = r.json()
        known = self.registry.known(self.client)
        foreign = [s for s in data["streams"] if s["stream"] not in known and s["count"] > 0]
        if foreign:
            ids = ", ".join(f"{s['stream']}({s['count']} 次)" for s in foreign)
            await self._alarm(f"发现不是本机开启的流：{ids}")
            return False
        async with self._lock:
            local = dict(self.ledger.digests)
            stream, next_seq = self.ledger.stream, self.ledger.next_seq
        mine = next((s for s in data["streams"] if s["stream"] == stream), None)
        if mine:
            remote = {int(k): v for k, v in (mine.get("digests") or {}).items()}
            # 本地已分配但还没拿到回执的 seq（并发进行中，或网络中断）是自己的；
            # 超出本地已分配范围的 seq，或 digest 不同的，一定是别人发的
            bad = [q for q, d in remote.items()
                   if (q in local and local[q] != d) or (q not in local and q >= next_seq)]
            if bad:
                await self._alarm(f"流 {stream} 账本不一致：gateway 有 {len(remote)} 次 / 本地 {len(local)} 次，"
                                  f"异常 seq {sorted(bad)[:10]}")
                return False
            # 没有进行中的调用时，两边的集合哈希必须完全一致
            if len(remote) == len(local) and mine["set_hash"] != set_hash(local):
                await self._alarm(f"流 {stream} 集合哈希不一致")
                return False
        return True

    async def _alarm(self, why: str):
        if self.revoked:
            return
        self.revoked = True
        self.alarm = why
        log.error("核对失败：%s —— 吊销 token", why)
        try:
            body = {"stream": self.ledger.stream if self.ledger else None,
                    "digests": {str(k): v for k, v in (self.ledger.digests if self.ledger else {}).items()},
                    "known_streams": sorted(self.registry.known(self.client))}
            await self.http.post("/v1/ledger", json=body)
            await self.http.post("/v1/revoke", json={"reason": why})
        except Exception as e:
            log.error("吊销请求失败：%s", e)
        self.notify("vpsops：token 已吊销", f"{self.client}：{why}")

    async def reconcile_loop(self):
        while not self.revoked:
            await asyncio.sleep(self.reconcile_every)
            try:
                await self.reconcile_once()
            except Revoked:
                return
            except Exception as e:
                log.warning("核对失败（网络）：%s", e)

    async def close(self):
        try:
            if self.ledger and not self.revoked:
                await self.reconcile_once()
                await self.http.delete(f"/v1/streams/{self.ledger.stream}")
        except Exception as e:
            log.warning("关闭流失败：%s", e)
        finally:
            if self.ledger:
                self.registry.close(self.ledger.stream)
            await self.http.aclose()


def to_text(result) -> str:
    return json.dumps(result, ensure_ascii=False, indent=1)
