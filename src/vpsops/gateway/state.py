"""Gateway 内存状态：流、租约、任务、吊销名单（吊销名单持久化）。

流和租约只放在内存里：Gateway 重启后所有流失效、租约清空，这是安全的默认值，
proxy 收到 410 会自动开新流。
"""

import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from vpsops.common.canon import set_hash


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


@dataclass
class Stream:
    id: str
    client: str
    ip: str
    opened: float
    last_seen: float
    closed: bool = False
    digests: dict[int, str] = field(default_factory=dict)

    @property
    def max_seq(self) -> int:
        return max(self.digests, default=0)

    def summary(self) -> dict:
        return {"stream": self.id, "opened": self.opened, "closed": self.closed,
                "count": len(self.digests), "max_seq": self.max_seq, "set_hash": set_hash(self.digests)}


@dataclass
class Lease:
    id: str
    client: str
    granted: float
    expires: float
    last_used: float
    calls: int = 0
    writes: list[str] = field(default_factory=list)


@dataclass
class Job:
    id: str
    client: str
    tool: str
    args: dict
    host: str
    stream: str
    seq: int
    status: str  # pending_approval | approved | running | done | failed | rejected | expired
    created: float
    updated: float
    result: dict | None = None
    message_id: int | None = None  # 审批卡片的消息 id：只接受来自这条消息的按钮

    def view(self) -> dict:
        v = {"id": self.id, "tool": self.tool, "host": self.host, "status": self.status,
             "age_sec": int(time.time() - self.created)}
        if self.result is not None:
            v["result"] = self.result
        return v


class State:
    def __init__(self, state_dir: Path):
        self.streams: dict[str, Stream] = {}
        self.leases: dict[str, Lease] = {}
        self.jobs: dict[str, Job] = {}
        self.revoke_buttons: dict[int, str] = {}  # 写操作通知消息 id -> 客户端（撤销按钮只在这些消息上有效）
        self._rate: dict[str, list[float]] = {}
        self._revoked_path = state_dir / "revoked.json"
        state_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.revoked: set[str] = set(json.loads(self._revoked_path.read_text()))
        except (OSError, ValueError):
            self.revoked = set()

    def revoke(self, token_sha256: str):
        self.revoked.add(token_sha256)
        tmp = self._revoked_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self.revoked)))
        tmp.replace(self._revoked_path)

    def rate_ok(self, client: str, per_min: int) -> bool:
        now = time.time()
        window = [t for t in self._rate.get(client, []) if now - t < 60]
        ok = len(window) < per_min
        if ok:
            window.append(now)
        self._rate[client] = window
        return ok

    def active_lease(self, client: str, idle: int) -> Lease | None:
        lease = self.leases.get(client)
        now = time.time()
        if lease and now < lease.expires and now - lease.last_used < idle:
            return lease
        return None

    def client_streams(self, client: str, window: int) -> list[Stream]:
        cutoff = time.time() - window
        return [s for s in self.streams.values() if s.client == client and s.last_seen >= cutoff]

    def prune(self, window: int):
        cutoff = time.time() - window
        for sid in [k for k, s in self.streams.items() if s.last_seen < cutoff]:
            del self.streams[sid]
        for jid in [k for k, j in self.jobs.items() if j.updated < cutoff]:
            del self.jobs[jid]
        if len(self.revoke_buttons) > 500:
            for mid in sorted(self.revoke_buttons)[:-500]:
                del self.revoke_buttons[mid]

    def pending_count(self, client: str) -> int:
        return sum(1 for j in self.jobs.values() if j.client == client and j.status == "pending_approval")
