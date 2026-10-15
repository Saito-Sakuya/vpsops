"""SSH 层：本地起一个 asyncssh 服务器模拟 VPS 的 forced command，验证 SSHPool 的收发和重连。"""

import json

import asyncssh
import pytest

from vpsops.gateway.config import Config, Host
from vpsops.gateway.sshexec import SSHPool


@pytest.fixture
async def ssh_server(tmp_path):
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    (tmp_path / "id").write_bytes(client_key.export_private_key())
    seen = []

    async def forced_command(process: asyncssh.SSHServerProcess):
        # 模拟 authorized_keys 的 command=：不管客户端请求什么命令，都只运行 "ai-agent"
        seen.append(process.command)
        req = json.loads(await process.stdin.read())
        process.stdout.write(json.dumps({"ok": True, "data": {"echo": req["op"], "meta": req["meta"]}}))
        process.exit(0)

    class Srv(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def public_key_auth_supported(self):
            return True

        def validate_public_key(self, username, key):
            return username == "ai-ops" and key.public_data == client_key.public_data

    server = await asyncssh.create_server(Srv, "127.0.0.1", 0, server_host_keys=[host_key],
                                          process_factory=forced_command)
    port = server.sockets[0].getsockname()[1]
    (tmp_path / "known_hosts").write_text(f"[127.0.0.1]:{port} {host_key.export_public_key().decode()}")
    yield {"port": port, "seen": seen, "tmp": tmp_path, "server": server}
    server.close()


def _cfg(tmp, known_hosts=None):
    return Config(listen_host="", listen_port=0, hosts={}, clients={}, ssh_key=str(tmp / "id"),
                  known_hosts=str(known_hosts or tmp / "known_hosts"), state_dir=tmp, audit_dir=tmp)


async def test_ssh_roundtrip_and_reuse(ssh_server):
    pool = SSHPool(_cfg(ssh_server["tmp"]))
    host = Host("v1", "127.0.0.1", "dev", port=ssh_server["port"])
    meta = {"client": "codex", "stream": "s_1", "seq": 3}
    r = await pool(host, {"op": "status", "args": {}, "meta": meta}, timeout=10)
    assert r == {"ok": True, "data": {"echo": "status", "meta": meta}}
    r = await pool(host, {"op": "logs", "args": {}, "meta": meta}, timeout=10)
    assert r["data"]["echo"] == "logs"
    assert ssh_server["seen"] == ["ai-agent", "ai-agent"]
    assert len(pool._conns) == 1
    await pool.close()


async def test_ssh_rejects_unknown_host_key(ssh_server, tmp_path):
    bad = tmp_path / "bad_known_hosts"
    other = asyncssh.generate_private_key("ssh-ed25519")
    bad.write_text(f"[127.0.0.1]:{ssh_server['port']} {other.export_public_key().decode()}")
    pool = SSHPool(_cfg(ssh_server["tmp"], known_hosts=bad))
    r = await pool(Host("v1", "127.0.0.1", "dev", port=ssh_server["port"]), {"op": "status"}, timeout=10)
    assert r["ok"] is False and r["error"] == "ssh_failed"
