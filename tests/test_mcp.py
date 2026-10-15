"""MCP 层：用真实 MCP 客户端通过 stdio 启动 vpsops-proxy 子进程，连到测试 gateway。"""

import sys
import threading

import pytest
from aiohttp import web
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from vpsops.common.canon import token_hash
from vpsops.gateway.app import make_app
from vpsops.gateway.config import Client, Config, Host
from vpsops.gateway.service import Gateway


@pytest.fixture
def gateway_url(tmp_path):
    """gateway 跑在独立线程的事件循环里，避免和 MCP 客户端的循环互相阻塞。"""
    import asyncio

    async def executor(host, payload, timeout):
        return {"ok": True, "data": {"op": payload["op"], "host": host.name}}

    cfg = Config(listen_host="127.0.0.1", listen_port=0, hosts={"dev-1": Host("dev-1", "x", "dev")},
                 clients={"codex": Client("codex", token_hash("vpo_t"), ["dev"])}, ssh_key="", known_hosts="",
                 state_dir=tmp_path / "s", audit_dir=tmp_path / "a")
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    box = {}

    async def start():
        runner = web.AppRunner(make_app(Gateway(cfg, executor)))
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        box["port"] = site._server.sockets[0].getsockname()[1]
        box["runner"] = runner
        ready.set()

    t = threading.Thread(target=lambda: (loop.run_until_complete(start()), loop.run_forever()), daemon=True)
    t.start()
    ready.wait(10)
    yield f"http://127.0.0.1:{box['port']}"
    asyncio.run_coroutine_threadsafe(box["runner"].cleanup(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)


async def test_mcp_stdio_roundtrip(gateway_url, tmp_path):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "vpsops.proxy.main", "--client", "codex"],
        env={"VPSOPS_URL": gateway_url, "VPSOPS_TOKEN": "vpo_t", "VPSOPS_HOME": str(tmp_path / "home"),
             "SYSTEMROOT": "C:\\Windows", "PATH": ""},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} >= {"hosts", "logs", "config_edit", "deploy", "job"}
            r = await session.call_tool("status", {"host": "dev-1"})
            assert '"host": "dev-1"' in r.content[0].text
    ledgers = list((tmp_path / "home" / "ledger").glob("*.jsonl"))
    assert ledgers and '"tool": "status"' in ledgers[0].read_text(encoding="utf-8")
