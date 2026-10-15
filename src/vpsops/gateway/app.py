"""HTTP 层（aiohttp）：认证、JSON 编解码、路由。"""

import asyncio
import logging

from aiohttp import web

from vpsops.common.canon import token_hash

from .service import Gateway

log = logging.getLogger("vpsops.http")
CLIENT = web.RequestKey("client", object)
GATEWAY = web.AppKey("gateway", Gateway)


@web.middleware
async def auth_mw(request: web.Request, handler):
    gw: Gateway = request.app[GATEWAY]
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return web.json_response({"error": "unauthorized"}, status=401)
    client, revoked = gw.authenticate(token_hash(header[7:].strip()))
    if not client:
        gw.audit.write(event="auth_failed", ip=request.remote, path=request.path)
        await asyncio.sleep(0.5)
        return web.json_response({"error": "unauthorized"}, status=401)
    if revoked:
        return web.json_response({"error": "revoked", "message": "token 已吊销"}, status=403)
    request[CLIENT] = client
    return await handler(request)


async def _json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text='{"error":"bad_json"}', content_type="application/json")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text='{"error":"bad_json"}', content_type="application/json")
    return body


async def tools(request):
    gw: Gateway = request.app[GATEWAY]
    return web.json_response({"tools": gw.tools_for(request[CLIENT])})


async def open_stream(request):
    gw: Gateway = request.app[GATEWAY]
    return web.json_response(gw.open_stream(request[CLIENT], request.remote or "?"))


async def close_stream(request):
    gw: Gateway = request.app[GATEWAY]
    status, body = gw.close_stream(request[CLIENT], request.match_info["sid"])
    return web.json_response(body, status=status)


async def call(request):
    gw: Gateway = request.app[GATEWAY]
    status, body = await gw.call(request[CLIENT], await _json(request), request.remote or "?")
    return web.json_response(body, status=status)


async def reconcile(request):
    gw: Gateway = request.app[GATEWAY]
    return web.json_response(gw.reconcile(request[CLIENT], request.query.get("stream")))


async def revoke(request):
    gw: Gateway = request.app[GATEWAY]
    body = await _json(request)
    return web.json_response(await gw.revoke(request[CLIENT], str(body.get("reason", ""))[:500],
                                             request.remote or "?"))


async def ledger(request):
    gw: Gateway = request.app[GATEWAY]
    return web.json_response(await gw.ledger(request[CLIENT], await _json(request)))


def make_app(gw: Gateway) -> web.Application:
    app = web.Application(middlewares=[auth_mw], client_max_size=1 << 20)
    app[GATEWAY] = gw
    app.router.add_get("/v1/tools", tools)
    app.router.add_post("/v1/streams", open_stream)
    app.router.add_delete("/v1/streams/{sid}", close_stream)
    app.router.add_post("/v1/call", call)
    app.router.add_get("/v1/reconcile", reconcile)
    app.router.add_post("/v1/revoke", revoke)
    app.router.add_post("/v1/ledger", ledger)

    async def background(app):
        tasks = [asyncio.create_task(gw.sweeper()), asyncio.create_task(gw.tg.poll())]
        yield
        for t in tasks:
            t.cancel()
        await gw.tg.close()

    app.cleanup_ctx.append(background)
    return app
