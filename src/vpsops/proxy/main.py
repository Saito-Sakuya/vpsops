"""vpsops-proxy：AI 客户端通过 stdio 启动的本地 MCP 服务。

  vpsops-proxy login --client codex      录入 gateway 地址与 token（存凭据管理器）
  vpsops-proxy --client codex            作为 MCP stdio server 运行
  vpsops-proxy check --client codex      手动核对一次
"""

import argparse
import asyncio
import getpass
import logging
import subprocess
import sys

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from . import creds
from .core import Core, Revoked, to_text
from .ledger import data_dir

log = logging.getLogger("vpsops.proxy")


def windows_notify(title: str, message: str):
    if sys.platform != "win32":
        return
    # 用 msg.exe 弹窗：无需额外依赖；失败不影响主流程
    try:
        subprocess.Popen(["msg", "*", "/TIME:0", f"{title}\n{message}"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass


def build_core(client: str) -> Core:
    url, token = creds.get_url(), creds.get_token(client)
    if not url or not token:
        sys.exit(f"未登录：先执行 vpsops-proxy login --client {client}")
    ca = creds.get_ca()
    return Core(client, url, token, data_dir(), notify=windows_notify, verify=ca or True)


async def serve(client: str):
    core = build_core(client)
    tool_cache: list[types.Tool] = []

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        if not tool_cache:
            for t in await core.tools():
                tool_cache.append(types.Tool(name=t["name"], description=t["description"],
                                             inputSchema=t["inputSchema"]))
        return types.ListToolsResult(tools=tool_cache)

    async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        is_error = False
        try:
            result = await core.call(params.name, params.arguments or {})
            is_error = isinstance(result, dict) and (bool(result.get("error")) or result.get("ok") is False)
        except Revoked as e:
            result, is_error = {"error": "revoked", "message": str(e)}, True
        return types.CallToolResult(content=[types.TextContent(type="text", text=to_text(result))],
                                    isError=is_error)

    server = Server("vpsops", version="0.1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    await core.start()
    loop_task = asyncio.create_task(core.reconcile_loop())
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        loop_task.cancel()
        await core.close()


def cmd_login(args):
    url = input(f"Gateway 地址 [{creds.get_url() or 'https://100.x.y.z:8740'}]：").strip() or creds.get_url()
    if not url:
        sys.exit("需要 Gateway 地址")
    creds.set_url(url)
    ca = input("自签证书的 CA 文件路径（没有直接回车）：").strip()
    if ca:
        creds.set_ca(ca)
    token = getpass.getpass(f"{args.client} 的 token：").strip()
    if not token.startswith("vpo_"):
        sys.exit("token 格式不对（应以 vpo_ 开头）")
    creds.set_token(args.client, token)
    print("已保存到系统凭据管理器。")


async def cmd_check(args):
    core = build_core(args.client)
    try:
        await core.start()
        tools = await core.tools()
        ok = await core.reconcile_once()
        print(f"连接正常，工具 {len(tools)} 个；核对：{'一致' if ok else '不一致 → ' + str(core.alarm)}")
    finally:
        await core.close()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Windows 控制台默认 GBK
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="vpsops-proxy")
    p.add_argument("--client", required=True, help="客户端名称，需与 gateway.yaml 中一致")
    p.add_argument("action", nargs="?", choices=["serve", "login", "check"], default="serve")
    args = p.parse_args(argv)
    # stdout 是 MCP 通道，日志只能写 stderr
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    if args.action == "login":
        cmd_login(args)
    elif args.action == "check":
        asyncio.run(cmd_check(args))
    else:
        asyncio.run(serve(args.client))


if __name__ == "__main__":
    main()
