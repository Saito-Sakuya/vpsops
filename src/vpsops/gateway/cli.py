"""vpsops-gateway 命令行：serve / token new / check。"""

import argparse
import logging
import secrets
import ssl
import sys

from vpsops.common.canon import token_hash


def cmd_serve(args):
    from aiohttp import web

    from . import config
    from .app import make_app
    from .service import Gateway
    from .sshexec import SSHPool
    from .telegram import Telegram

    cfg = config.load(args.config)
    gw = Gateway(cfg, SSHPool(cfg), Telegram(cfg.tg_token, cfg.tg_chat, cfg.tg_approvers))
    ssl_ctx = None
    if args.tls_cert:
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(args.tls_cert, args.tls_key)
    if not cfg.tg_token:
        logging.warning("未配置 Telegram：prod 写操作将无法被授权")
    web.run_app(make_app(gw), host=cfg.listen_host, port=cfg.listen_port, ssl_context=ssl_ctx,
                access_log=None)


def cmd_token_new(args):
    token = "vpo_" + secrets.token_urlsafe(32)
    print(f"客户端：{args.client}")
    print(f"token（只显示一次，在本机执行 vpsops-proxy login --client {args.client} 时粘贴）：")
    print(f"  {token}")
    print("写入 gateway.yaml：")
    print(f"  {args.client}: {{ token_sha256: \"{token_hash(token)}\", envs: [dev, staging, prod] }}")
    print("如果这是替换被吊销的 token，修改 yaml 后重启 gateway。")


def cmd_check(args):
    from . import config

    cfg = config.load(args.config)
    print(f"监听 {cfg.listen_host}:{cfg.listen_port}")
    print(f"主机 {len(cfg.hosts)}：" + ", ".join(f"{h.name}({h.env})" for h in cfg.hosts.values()))
    print(f"客户端 {len(cfg.clients)}：" + ", ".join(cfg.clients))
    approvers = cfg.tg_approvers or ({cfg.tg_chat} if cfg.tg_chat and cfg.tg_chat > 0 else set())
    print("Telegram：" + ("已配置" if cfg.tg_token and cfg.tg_chat else "未配置")
          + (f"，审批人 {sorted(approvers)}" if approvers else ""))


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Windows 控制台默认 GBK
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="vpsops-gateway")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("-c", "--config", default="/etc/vpsops-gateway/gateway.yaml")
    s.add_argument("--tls-cert")
    s.add_argument("--tls-key")
    s.set_defaults(func=cmd_serve)
    t = sub.add_parser("token").add_subparsers(dest="sub", required=True).add_parser("new")
    t.add_argument("client")
    t.set_defaults(func=cmd_token_new)
    c = sub.add_parser("check")
    c.add_argument("-c", "--config", default="/etc/vpsops-gateway/gateway.yaml")
    c.set_defaults(func=cmd_check)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
