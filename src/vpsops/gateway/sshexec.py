"""到 VPS 的 SSH 通道：只发送 JSON 到 ai-agent（远端 forced command 决定实际执行什么）。"""

import asyncio
import json
import logging

import asyncssh

from .config import Config, Host

log = logging.getLogger("vpsops.ssh")


class SSHPool:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._conns: dict[str, asyncssh.SSHClientConnection] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _connect(self, host: Host) -> asyncssh.SSHClientConnection:
        lock = self._locks.setdefault(host.name, asyncio.Lock())
        async with lock:
            conn = self._conns.get(host.name)
            if conn is not None and not conn.is_closed():
                return conn
            conn = await asyncssh.connect(
                host.addr, port=host.port, username=host.user,
                client_keys=[self.cfg.ssh_key], known_hosts=self.cfg.known_hosts,
                agent_path=None, keepalive_interval=30, connect_timeout=10,
            )
            self._conns[host.name] = conn
            return conn

    async def __call__(self, host: Host, payload: dict, timeout: int) -> dict:
        data = json.dumps(payload, ensure_ascii=False)
        for attempt in (1, 2):
            try:
                conn = await self._connect(host)
                proc = await asyncio.wait_for(conn.run("ai-agent", input=data, check=False), timeout)
                break
            except (asyncssh.Error, OSError) as e:
                self._conns.pop(host.name, None)
                if attempt == 2:
                    return {"ok": False, "error": "ssh_failed", "message": str(e)}
                log.warning("ssh %s 失败，重连：%s", host.name, e)
            except asyncio.TimeoutError:
                return {"ok": False, "error": "timeout", "message": f"{timeout}s 内无响应"}
        try:
            return json.loads(proc.stdout)
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad_agent_output",
                    "message": (proc.stderr or proc.stdout or "")[-1000:]}

    async def close(self):
        for conn in self._conns.values():
            conn.close()
