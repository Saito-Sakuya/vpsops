"""Gateway + Proxy 端到端：真实 HTTP，假执行器替代 SSH。"""

import asyncio

import httpx
import pytest
from aiohttp.test_utils import TestServer

from vpsops.common.canon import token_hash
from vpsops.gateway.app import make_app
from vpsops.gateway.config import Client, Config, Host
from vpsops.gateway.service import Gateway
from vpsops.proxy.core import Core, Revoked

TOKENS = {"codex": "vpo_codex", "claude-code": "vpo_claude"}


class FakeTelegram:
    enabled = True

    def __init__(self):
        self.sent = []
        self.on_callback = None

    async def send(self, text, buttons=None, silent=False):
        self.sent.append((text, buttons))
        return len(self.sent)

    async def poll(self):
        await asyncio.Event().wait()

    async def close(self):
        pass


@pytest.fixture
async def env(tmp_path):
    calls = []

    async def executor(host, payload, timeout):
        calls.append((host.name, payload))
        if payload["op"] == "describe":
            return {"ok": True, "data": {"logs": ["nginx"]}}
        return {"ok": True, "data": {"op": payload["op"], "host": host.name}}

    cfg = Config(
        listen_host="127.0.0.1", listen_port=0,
        hosts={"web-1": Host("web-1", "10.0.0.1", "prod"), "dev-1": Host("dev-1", "10.0.0.2", "dev")},
        clients={n: Client(n, token_hash(t), ["dev", "staging", "prod"]) for n, t in TOKENS.items()},
        ssh_key="", known_hosts="", state_dir=tmp_path / "gw-state", audit_dir=tmp_path / "gw-audit",
    )
    tg = FakeTelegram()
    gw = Gateway(cfg, executor, tg)
    server = TestServer(make_app(gw))
    await server.start_server()
    url = str(server.make_url(""))
    cores = []

    def make_core(client="codex", root=None):
        c = Core(client, url, TOKENS[client], root or tmp_path / f"local-{client}", reconcile_every=3600)
        (c.root / "ledger").mkdir(parents=True, exist_ok=True)
        cores.append(c)
        return c

    yield {"gw": gw, "tg": tg, "calls": calls, "url": url, "make_core": make_core, "tmp": tmp_path}
    for c in cores:
        if not c.http.is_closed:
            await c.http.aclose()
    await server.close()


async def test_read_and_reconcile(env):
    core = env["make_core"]()
    await core.start()
    assert len(await core.tools()) == 9
    r = await core.call("hosts", {})
    assert set(r) == {"web-1", "dev-1"} and r["web-1"]["env"] == "prod"
    await core.call("logs", {"hosts": ["web-1", "dev-1"], "source": "nginx"})
    assert await core.reconcile_once()
    # 并发调用不破坏核对
    await asyncio.gather(*(core.call("status", {"host": "dev-1"}) for _ in range(10)))
    assert await core.reconcile_once()
    assert len(core.ledger.digests) == 12


async def test_dev_write_passes_prod_write_needs_lease(env):
    core = env["make_core"]()
    await core.start()
    r = await core.call("service", {"host": "dev-1", "name": "api", "action": "restart", "reason": "test"})
    assert r["ok"]
    r = await core.call("service", {"host": "web-1", "name": "api", "action": "restart", "reason": "fix 502"})
    assert r["status"] == "pending_approval"
    job = r["job"]
    text, buttons = env["tg"].sent[-1]
    assert "codex" in text and "prod" in text
    grant = buttons[0][0][1]
    assert await env["gw"].on_telegram(grant) == "已授权 30m"
    await asyncio.sleep(0.05)
    j = await core.call("job", {"id": job})
    assert j["status"] == "done"
    # 租约内直接放行
    r = await core.call("service", {"host": "web-1", "name": "api", "action": "reload", "reason": "again"})
    assert r["ok"]
    # 另一个客户端没有租约
    other = env["make_core"]("claude-code")
    await other.start()
    r = await other.call("service", {"host": "web-1", "name": "api", "action": "reload", "reason": "x"})
    assert r["status"] == "pending_approval"
    assert await core.reconcile_once()


async def test_reject_and_revoke_lease(env):
    core = env["make_core"]()
    await core.start()
    r = await core.call("deploy", {"host": "web-1", "target": "api", "version": "v1", "reason": "rel"})
    assert await env["gw"].on_telegram(f"reject:{r['job']}") == "已拒绝"
    assert (await core.call("job", {"id": r["job"]}))["status"] == "rejected"
    r = await core.call("deploy", {"host": "web-1", "target": "api", "version": "v1", "reason": "rel"})
    await env["gw"].on_telegram(f"grant:{r['job']}:1800")
    assert env["gw"].state.active_lease("codex", 1800)
    assert await env["gw"].on_telegram("revoke:codex") == "租约已撤销"
    r = await core.call("deploy", {"host": "web-1", "target": "api", "version": "v2", "reason": "rel"})
    assert r["status"] == "pending_approval"


async def test_async_deploy(env):
    core = env["make_core"]()
    await core.start()
    r = await core.call("deploy", {"host": "dev-1", "target": "api", "version": "v1", "reason": "rel"})
    assert r["status"] == "running"
    await asyncio.sleep(0.05)
    assert (await core.call("job", {"id": r["job"]}))["status"] == "done"


async def test_stolen_token_new_stream_detected(env):
    core = env["make_core"]()
    await core.start()
    await core.call("hosts", {})
    # 攻击者拿 token 另开一条流
    async with httpx.AsyncClient(base_url=env["url"], trust_env=False, headers={"Authorization": "Bearer vpo_codex"}) as h:
        sid = (await h.post("/v1/streams")).json()["stream"]
        await h.post("/v1/call", json={"stream": sid, "seq": 1, "tool": "status", "args": {"host": "web-1"}})
    assert not await core.reconcile_once()
    assert core.revoked and "不是本机开启的流" in core.alarm
    # token 被吊销，攻击者和自己都无法再调用
    async with httpx.AsyncClient(base_url=env["url"], trust_env=False, headers={"Authorization": "Bearer vpo_codex"}) as h:
        assert (await h.post("/v1/streams")).status_code == 403
    with pytest.raises(Revoked):
        await core.call("hosts", {})
    alerts = [t for t, _ in env["tg"].sent if "本地账本之外" in t]
    assert alerts and "status" in alerts[0]


async def test_injected_seq_detected_immediately(env):
    core = env["make_core"]()
    await core.start()
    await core.call("hosts", {})
    sid = core.ledger.stream
    async with httpx.AsyncClient(base_url=env["url"], trust_env=False, headers={"Authorization": "Bearer vpo_codex"}) as h:
        r = await h.post("/v1/call", json={"stream": sid, "seq": 2, "tool": "status", "args": {}})
        assert r.status_code == 200
    with pytest.raises(Revoked):
        await core.call("hosts", {})  # 本地下一个 seq 正好是 2 → 冲突
    assert core.revoked


async def test_injected_ahead_detected_by_reconcile(env):
    core = env["make_core"]()
    await core.start()
    await core.call("hosts", {})
    sid = core.ledger.stream
    async with httpx.AsyncClient(base_url=env["url"], trust_env=False, headers={"Authorization": "Bearer vpo_codex"}) as h:
        await h.post("/v1/call", json={"stream": sid, "seq": 30, "tool": "status", "args": {}})
    assert not await core.reconcile_once()
    assert "异常 seq [30]" in core.alarm


async def test_registry_shared_between_processes(env):
    """同一客户端开两个代理进程（两个会话）：彼此的流都在本机登记表里，不误报。"""
    root = env["tmp"] / "shared"
    a, b = env["make_core"](root=root), env["make_core"](root=root)
    await a.start()
    await b.start()
    await a.call("hosts", {})
    await b.call("hosts", {})
    assert await a.reconcile_once() and await b.reconcile_once()


async def test_stream_reopen_after_gateway_restart(env):
    core = env["make_core"]()
    await core.start()
    old = core.ledger.stream
    env["gw"].state.streams.clear()  # 模拟 gateway 重启
    await core.call("hosts", {})
    assert core.ledger.stream != old and await core.reconcile_once()


async def test_bad_token_and_invisible_host(env):
    async with httpx.AsyncClient(base_url=env["url"], trust_env=False, headers={"Authorization": "Bearer nope"}) as h:
        assert (await h.get("/v1/tools")).status_code == 401
    env["gw"].cfg.clients["codex"].envs = ["dev"]
    core = env["make_core"]()
    await core.start()
    r = await core.call("status", {"host": "web-1"})
    assert r["error"] == "unknown_host" and r["available"] == ["dev-1"]
    assert set(await core.call("hosts", {})) == {"dev-1"}
