"""gateway.yaml 解析。"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_DUR = re.compile(r"^(\d+)([smhd])$")


def seconds(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    m = _DUR.match(str(value).strip())
    if not m:
        raise ValueError(f"时长格式无效：{value}（用 30m / 2h 这类写法）")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


@dataclass
class Host:
    name: str
    addr: str
    env: str
    port: int = 22
    user: str = "ai-ops"


@dataclass
class Client:
    name: str
    token_sha256: str
    envs: list[str]


@dataclass
class Config:
    listen_host: str
    listen_port: int
    hosts: dict[str, Host]
    clients: dict[str, Client]
    ssh_key: str
    known_hosts: str
    state_dir: Path
    audit_dir: Path
    lease_choices: list[int] = field(default_factory=lambda: [1800, 7200, 14400])
    lease_idle: int = 1800
    notify_each_write: bool = True
    approval_ttl: int = 600
    seq_window: int = 64
    stream_idle: int = 1800
    reconcile_window: int = 86400
    rate_per_min: int = 120
    max_pending: int = 3              # 每个客户端同时等待授权的请求上限（防审批轰炸）
    tg_token: str | None = None
    tg_chat: int | None = None
    tg_approvers: set[int] | None = None  # None = 私聊时默认 {chat_id}

    def client_by_hash(self, h: str) -> Client | None:
        return next((c for c in self.clients.values() if c.token_sha256 == h), None)


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    host, _, port = str(raw.get("listen", "127.0.0.1:8740")).rpartition(":")
    hosts = {}
    for name, h in (raw.get("hosts") or {}).items():
        if h.get("env") not in ("dev", "staging", "prod"):
            raise ValueError(f"主机 {name} 的 env 必须是 dev/staging/prod")
        hosts[name] = Host(name=name, addr=h["addr"], env=h["env"], port=int(h.get("port", 22)),
                           user=h.get("user", "ai-ops"))
    clients = {}
    for name, c in (raw.get("clients") or {}).items():
        h = str(c["token_sha256"]).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", h):
            raise ValueError(f"客户端 {name} 的 token_sha256 无效（用 vpsops-gateway token new {name} 生成）")
        clients[name] = Client(name=name, token_sha256=h, envs=list(c.get("envs", ["dev", "staging"])))
    ssh = raw.get("ssh") or {}
    lease = raw.get("lease") or {}
    streams = raw.get("streams") or {}
    tg = raw.get("telegram") or {}
    tg_token = None
    if tg.get("bot_token_file"):
        tg_token = Path(tg["bot_token_file"]).read_text().strip() or None
    tg_chat = int(tg["chat_id"]) if tg.get("chat_id") else None
    approvers = {int(x) for x in tg["approvers"]} if tg.get("approvers") else None
    if tg_chat is not None and tg_chat < 0 and not approvers:
        raise ValueError("telegram.chat_id 是群聊：必须配置 telegram.approvers（允许审批的用户 id 列表）")
    state_dir = Path(raw.get("state_dir", "/var/lib/vpsops-gateway"))
    return Config(
        listen_host=host or "127.0.0.1",
        listen_port=int(port),
        hosts=hosts,
        clients=clients,
        ssh_key=ssh.get("key", "/etc/vpsops-gateway/id_ed25519"),
        known_hosts=ssh.get("known_hosts", "/etc/vpsops-gateway/known_hosts"),
        state_dir=state_dir,
        audit_dir=Path(raw.get("audit_dir", "/var/log/vpsops-gateway")),
        lease_choices=[seconds(x) for x in lease.get("choices", ["30m", "2h", "4h"])],
        lease_idle=seconds(lease.get("idle_timeout", "30m")),
        notify_each_write=bool(lease.get("notify_each_write", True)),
        approval_ttl=seconds(lease.get("approval_ttl", "10m")),
        seq_window=int(streams.get("seq_window", 64)),
        stream_idle=seconds(streams.get("idle_timeout", "30m")),
        rate_per_min=int(raw.get("rate_per_min", 120)),
        max_pending=int(lease.get("max_pending", 3)),
        tg_token=tg_token,
        tg_chat=tg_chat,
        tg_approvers=approvers,
    )
