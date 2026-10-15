"""Gateway 核心逻辑：流与 seq 校验、可见性、租约与授权、任务、核对、吊销。

与 HTTP 层解耦：HTTP 层只做认证和 JSON 编解码，这里返回 (status, body)。
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from vpsops.common.canon import call_digest
from vpsops.common.schema import BY_NAME, WRITE, public_tools, validate

from .config import Client, Config, Host
from .state import Job, Lease, State, Stream, new_id
from .telegram import Telegram, esc, fmt_args

log = logging.getLogger("vpsops.gateway")

# (host, payload, timeout) -> agent 响应
Executor = Callable[[Host, dict, int], Awaitable[dict]]

AGENT_OP = {"hosts": "describe", "status": "status", "logs": "logs", "config_read": "config_read",
            "config_edit": "config_edit", "config_restore": "config_restore", "service": "service",
            "deploy": "deploy"}
TIMEOUT = {"deploy": 420, "config_edit": 240, "config_restore": 240, "service": 200}


def fmt_dur(sec: int) -> str:
    return f"{sec // 3600}h" if sec % 3600 == 0 else f"{sec // 60}m"


class Audit:
    def __init__(self, directory: Path):
        self.dir = directory
        directory.mkdir(parents=True, exist_ok=True)

    def write(self, **rec):
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **rec}
        path = self.dir / f"{rec['ts'][:10]}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


class Gateway:
    def __init__(self, cfg: Config, executor: Executor, telegram: Telegram | None = None):
        self.cfg = cfg
        self.exec = executor
        self.tg = telegram or Telegram(None, None)
        self.tg.on_callback = self.on_telegram
        self.state = State(cfg.state_dir)
        self.audit = Audit(cfg.audit_dir)
        self.calls: dict[tuple[str, int], dict] = {}  # (stream, seq) -> 调用摘要，用于告警时列明细
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ 认证

    def authenticate(self, token_sha256: str) -> tuple[Client | None, bool]:
        """返回 (client, revoked)。"""
        client = self.cfg.client_by_hash(token_sha256)
        return client, bool(client and token_sha256 in self.state.revoked)

    def visible(self, client: Client) -> dict[str, Host]:
        return {n: h for n, h in self.cfg.hosts.items() if h.env in client.envs}

    # ------------------------------------------------------------ 流

    def open_stream(self, client: Client, ip: str) -> dict:
        now = time.time()
        s = Stream(id=new_id("s"), client=client.name, ip=ip, opened=now, last_seen=now)
        self.state.streams[s.id] = s
        self.audit.write(event="stream_open", client=client.name, stream=s.id, ip=ip)
        return {"stream": s.id, "seq_window": self.cfg.seq_window}

    def close_stream(self, client: Client, sid: str) -> tuple[int, dict]:
        s = self.state.streams.get(sid)
        if not s or s.client != client.name:
            return 404, {"error": "no_stream"}
        s.closed = True
        self.audit.write(event="stream_close", client=client.name, **s.summary())
        return 200, s.summary()

    def _check_stream(self, client: Client, sid: str, seq) -> tuple[int, dict] | Stream:
        s = self.state.streams.get(sid)
        if not s or s.client != client.name:
            return 410, {"error": "stream_gone", "message": "流不存在，请重新打开"}
        if s.closed or time.time() - s.last_seen > self.cfg.stream_idle:
            return 410, {"error": "stream_gone", "message": "流已关闭或空闲超时"}
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            return 400, {"error": "bad_seq"}
        if seq in s.digests:
            return 409, {"error": "seq_conflict", "message": f"seq {seq} 已被使用", "stream": sid}
        if abs(seq - s.max_seq) > self.cfg.seq_window:
            return 409, {"error": "seq_window", "message": f"seq {seq} 超出窗口（当前 {s.max_seq}）"}
        return s

    # ------------------------------------------------------------ 调用

    async def call(self, client: Client, body: dict, ip: str) -> tuple[int, dict]:
        sid, seq, tool, args = body.get("stream"), body.get("seq"), body.get("tool"), body.get("args") or {}
        if not isinstance(tool, str) or not isinstance(args, dict):
            return 400, {"error": "bad_request"}
        checked = self._check_stream(client, sid, seq)
        if isinstance(checked, tuple):
            self.audit.write(event="rejected", client=client.name, stream=sid, seq=seq, tool=tool,
                             ip=ip, error=checked[1].get("error"))
            if checked[1].get("error") == "seq_conflict":
                await self.tg.send(f"⚠️ <b>{esc(client.name)}</b> 流 {esc(sid)} seq {seq} 重复（来源 {esc(ip)}）。"
                                   f"可能有人在用同一 token。")
            return checked
        s = checked
        digest = call_digest(sid, seq, tool, args)
        # 先登记再执行：只要 Gateway 收到了，这次调用就计入核对
        s.digests[seq] = digest
        s.last_seen = time.time()
        self.calls[(sid, seq)] = {"tool": tool, "host": args.get("host") or args.get("hosts"),
                                  "ip": ip, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        receipt = {"stream": sid, "seq": seq, "digest": digest}

        if not self.state.rate_ok(client.name, self.cfg.rate_per_min):
            status, data = 429, {"error": "rate_limited", "message": "调用过于频繁"}
        elif tool not in BY_NAME:
            status, data = 400, {"error": "unknown_tool"}
        elif err := validate(BY_NAME[tool]["inputSchema"], args):
            status, data = 400, {"error": "bad_args", "message": err}
        else:
            try:
                status, data = await self._dispatch(client, s, seq, tool, args)
            except Exception as e:
                log.exception("工具执行异常")
                status, data = 500, {"error": "internal", "message": str(e)}
        self.audit.write(event="call", client=client.name, stream=sid, seq=seq, digest=digest, tool=tool,
                         args=args, ip=ip, status=status,
                         ok=status == 200 and not (isinstance(data, dict) and data.get("error")),
                         error=data.get("error") if isinstance(data, dict) else None)
        return status, {"result": data, "receipt": receipt}

    async def _dispatch(self, client: Client, s: Stream, seq: int, tool: str, args: dict):
        visible = self.visible(client)
        if tool == "job":
            job = self.state.jobs.get(args.get("id", ""))
            if not job or job.client != client.name:
                return 404, {"error": "no_job"}
            return 200, job.view()
        if tool == "hosts":
            return 200, await self._fan_out(visible, client, s, seq, "describe", {},
                                            extra=lambda h: {"env": h.env})
        if tool == "status":
            name = args.get("host")
            if name:
                host = visible.get(name)
                if not host:
                    return 404, {"error": "unknown_host", "available": sorted(visible)}
                return 200, await self._agent(host, client, s, seq, "status", {})
            return 200, await self._fan_out(visible, client, s, seq, "status", {})
        if tool == "logs":
            names = args.get("hosts") or []
            missing = [n for n in names if n not in visible]
            if missing or not names:
                return 404, {"error": "unknown_host", "missing": missing, "available": sorted(visible)}
            agent_args = {k: v for k, v in args.items() if k != "hosts"}
            return 200, await self._fan_out({n: visible[n] for n in names}, client, s, seq, "logs", agent_args)

        # 单主机工具
        host = visible.get(args.get("host", ""))
        if not host:
            return 404, {"error": "unknown_host", "available": sorted(visible)}
        if BY_NAME[tool]["kind"] != WRITE:
            return 200, await self._agent(host, client, s, seq, AGENT_OP[tool], self._agent_args(args))

        job = Job(id=new_id("j"), client=client.name, tool=tool, args=args, host=host.name, stream=s.id,
                  seq=seq, status="running", created=time.time(), updated=time.time())
        if host.env == "prod":
            lease = self.state.active_lease(client.name, self.cfg.lease_idle)
            if not lease:
                if not self.tg.enabled:
                    return 403, {"error": "approval_unavailable", "message": "Gateway 未配置 Telegram，无法授权 prod 写操作"}
                if self.state.pending_count(client.name) >= self.cfg.max_pending:
                    return 429, {"error": "too_many_pending",
                                 "message": f"已有 {self.cfg.max_pending} 个请求在等待授权，先用 job 查询它们"}
                job.status = "pending_approval"
                self.state.jobs[job.id] = job
                if not await self._ask_approval(client, job):
                    job.status, job.updated = "failed", time.time()
                    job.result = {"ok": False, "error": "approval_unavailable", "message": "审批消息发送失败"}
                    return 503, job.result
                return 200, {"status": "pending_approval", "job": job.id,
                             "message": "prod 写操作需要授权，已推送到 Telegram。稍后用 job 工具查询。",
                             "expires_in": self.cfg.approval_ttl}
            lease.last_used = time.time()
            lease.calls += 1
            lease.writes.append(f"{time.strftime('%H:%M')} {tool} {host.name} {self._target_of(args)}")
            if self.cfg.notify_each_write:
                mid = await self.tg.send(f"✏️ <b>{esc(client.name)}</b> {esc(tool)} {esc(host.name)} "
                                         f"{esc(self._target_of(args))}\nreason：{esc(args.get('reason', ''))}",
                                         buttons=[[("撤销租约", f"revoke:{client.name}")]], silent=True)
                if mid is not None:
                    self.state.revoke_buttons[mid] = client.name
        self.state.jobs[job.id] = job
        if tool == "deploy":
            self._spawn(self._run_job(job))
            return 200, {"status": "running", "job": job.id, "message": "部署已开始，用 job 工具查询结果"}
        await self._run_job(job)
        return 200, job.result

    @staticmethod
    def _agent_args(args: dict) -> dict:
        return {k: v for k, v in args.items() if k not in ("host", "reason")}

    @staticmethod
    def _target_of(args: dict) -> str:
        t = args.get("name") or args.get("target") or ""
        return f"{t} {args['version']}" if args.get("version") else t

    async def _agent(self, host: Host, client: Client, s: Stream, seq: int, op: str, args: dict,
                     reason: str | None = None, job: str | None = None) -> dict:
        payload = {"op": op, "args": args,
                   "meta": {"client": client.name, "stream": s.id, "seq": seq, "reason": reason, "call_id": job}}
        return await self.exec(host, payload, TIMEOUT.get(op, 60))

    async def _fan_out(self, hosts: dict[str, Host], client, s, seq, op, args, extra=None) -> dict:
        names = list(hosts)
        results = await asyncio.gather(*(self._agent(hosts[n], client, s, seq, op, args) for n in names))
        out = {}
        for n, r in zip(names, results):
            if extra:
                r = {**extra(hosts[n]), **r}
            out[n] = r
        return out

    async def _run_job(self, job: Job):
        client = self.cfg.clients[job.client]
        s = self.state.streams.get(job.stream)
        host = self.cfg.hosts[job.host]
        job.status, job.updated = "running", time.time()
        payload = {"op": AGENT_OP[job.tool], "args": self._agent_args(job.args),
                   "meta": {"client": client.name, "stream": job.stream, "seq": job.seq,
                            "reason": job.args.get("reason"), "call_id": job.id}}
        result = await self.exec(host, payload, TIMEOUT.get(job.tool, 60))
        job.result = result
        job.status = "done" if result.get("ok") else "failed"
        job.updated = time.time()
        self.audit.write(event="job_done", client=client.name, job=job.id, tool=job.tool, host=job.host,
                         stream=job.stream, seq=job.seq, ok=result.get("ok"), error=result.get("error"))
        if s:
            s.last_seen = max(s.last_seen, time.time() - 1)

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------ 授权

    def _card(self, job: Job) -> str:
        return (f"<code>{esc(job.tool)}</code> {esc(job.host)} {esc(self._target_of(job.args))}\n"
                f"reason：{esc(job.args.get('reason', ''))}")

    async def _ask_approval(self, client: Client, job: Job) -> bool:
        choices = [(fmt_dur(sec), f"grant:{job.id}:{sec}") for sec in self.cfg.lease_choices]
        text = (f"🔐 <b>{esc(client.name)}</b> 请求 prod 写权限\n"
                f"首个操作：{self._card(job)}\n<pre>{fmt_args(self._agent_args(job.args))}</pre>\n"
                f"⚠️ 选择时长 = 该时长内 <b>{esc(client.name)}</b> 的<b>所有</b> prod 写操作直接放行"
                f"（首个操作立即执行）。")
        job.message_id = await self.tg.send(text, buttons=[choices, [("拒绝", f"reject:{job.id}")]])
        self.audit.write(event="approval_requested", client=client.name, job=job.id, tool=job.tool,
                         host=job.host, delivered=job.message_id is not None)
        return job.message_id is not None

    async def on_telegram(self, data: str, user_id: int, message_id: int) -> str:
        """Telegram 层已校验 chat 和审批人；这里校验按钮属于哪条消息、请求状态与时效。"""
        kind, _, rest = data.partition(":")
        if kind == "revoke":
            if self.state.revoke_buttons.get(message_id) != rest:
                self.audit.write(event="callback_rejected", user=user_id, data=data[:64], why="message_mismatch")
                return "按钮无效"
            self.audit.write(event="lease_revoke_clicked", client=rest, user=user_id)
            return await self.end_lease(rest, "手动撤销") or "没有生效中的租约"
        job_id, _, sec = rest.partition(":")
        job = self.state.jobs.get(job_id)
        if not job or job.message_id is None or job.message_id != message_id:
            self.audit.write(event="callback_rejected", user=user_id, data=data[:64], why="message_mismatch")
            return "按钮无效"
        if job.status != "pending_approval":
            return "该请求已处理"
        now = time.time()
        if now - job.created > self.cfg.approval_ttl:
            job.status, job.updated = "expired", now
            job.result = {"ok": False, "error": "approval_expired", "message": "授权超时未处理"}
            await self.tg.edit(job.message_id, f"⌛ 已过期：{esc(job.client)} {self._card(job)}")
            return "已过期"
        if kind == "reject":
            job.status, job.updated = "rejected", now
            job.result = {"ok": False, "error": "rejected", "message": "授权被拒绝"}
            self.audit.write(event="approval_rejected", client=job.client, job=job.id, user=user_id)
            await self.tg.edit(job.message_id, f"❌ 已拒绝：{esc(job.client)} {self._card(job)}")
            return "已拒绝"
        if kind == "grant":
            try:
                duration = int(sec)
            except ValueError:
                return "时长无效"
            if duration not in self.cfg.lease_choices:
                return "时长无效"
            # 在任何 await 之前改状态：重复点击/重放的回调看到的已不是 pending
            job.status, job.updated = "approved", now
            lease = Lease(id=new_id("L"), client=job.client, granted=now, expires=now + duration, last_used=now,
                          calls=1, writes=[f"{time.strftime('%H:%M')} {job.tool} {job.host} {self._target_of(job.args)}"])
            self.state.leases[job.client] = lease
            self.audit.write(event="lease_granted", client=job.client, lease=lease.id, duration=duration,
                             job=job.id, user=user_id)
            self._spawn(self._run_job(job))
            # 同一客户端的其他待审卡片不自动执行（你没看过）；它们照常可点或过期，AI 重新发起时会被租约放行
            await self.tg.edit(job.message_id, f"✅ 已授权 {fmt_dur(duration)}：{esc(job.client)} {self._card(job)}")
            return f"已授权 {fmt_dur(duration)}"
        return "未知操作"

    async def end_lease(self, client_name: str, why: str) -> str | None:
        lease = self.state.leases.pop(client_name, None)
        if not lease:
            return None
        await self.tg.send(f"📋 <b>{esc(client_name)}</b> 租约已结束（{esc(why)}）\n"
                           f"本次写操作：{len(lease.writes)} 次", silent=True)
        self.audit.write(event="lease_end", client=client_name, lease=lease.id, why=why, writes=lease.writes)
        return "租约已撤销"

    # ------------------------------------------------------------ 核对与吊销

    def reconcile(self, client: Client, own_stream: str | None) -> dict:
        streams = []
        for s in self.state.client_streams(client.name, self.cfg.reconcile_window):
            item = s.summary()
            if s.id == own_stream:
                item["digests"] = {str(k): v for k, v in sorted(s.digests.items())}
            streams.append(item)
        lease = self.state.active_lease(client.name, self.cfg.lease_idle)
        return {"streams": streams,
                "lease": {"id": lease.id, "expires": lease.expires} if lease else None}

    async def revoke(self, client: Client, reason: str, ip: str) -> dict:
        self.state.revoke(client.token_sha256)
        for s in self.state.client_streams(client.name, self.cfg.reconcile_window):
            s.closed = True
        await self.end_lease(client.name, "token 吊销")
        self.audit.write(event="token_revoked", client=client.name, reason=reason, ip=ip)
        await self.tg.send(f"🚨 <b>{esc(client.name)}</b> 的 token 已被吊销\n原因：{esc(reason)}\n"
                           f"恢复：在跳板机上执行 <code>vpsops-gateway token new {esc(client.name)}</code>")
        return {"revoked": True}

    async def ledger(self, client: Client, body: dict) -> dict:
        """核对失败后 proxy 上传本地账本，列出 Gateway 有而本地没有的调用。"""
        sid = body.get("stream")
        local = {int(k): v for k, v in (body.get("digests") or {}).items()}
        known_streams = set(body.get("known_streams") or [])
        extra = []
        for s in self.state.client_streams(client.name, self.cfg.reconcile_window):
            if s.id == sid:
                extra += [(s.id, q) for q, d in s.digests.items() if local.get(q) != d]
            elif s.id not in known_streams and s.digests:
                extra += [(s.id, q) for q in s.digests]
        detail = [{"stream": st, "seq": q, **self.calls.get((st, q), {})} for st, q in sorted(extra)]
        self.audit.write(event="ledger_diff", client=client.name, extra=detail)
        if detail:
            lines = "\n".join(f"  {d['stream']}#{d['seq']} {d.get('ts', '')} {d.get('tool')} {d.get('host')} "
                              f"from {d.get('ip')}" for d in detail[:30])
            await self.tg.send(f"🚨 <b>{esc(client.name)}</b> 本地账本之外的调用 {len(detail)} 次：\n{esc(lines)}")
        return {"extra": detail}

    # ------------------------------------------------------------ 后台

    async def sweeper(self):
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for name, lease in list(self.state.leases.items()):
                if now >= lease.expires:
                    await self.end_lease(name, "到期")
                elif now - lease.last_used >= self.cfg.lease_idle:
                    await self.end_lease(name, "空闲超时")
            for job in list(self.state.jobs.values()):
                if job.status == "pending_approval" and now - job.created > self.cfg.approval_ttl:
                    job.status, job.updated = "expired", now
                    job.result = {"ok": False, "error": "approval_expired", "message": "授权超时未处理"}
                    await self.tg.edit(job.message_id, f"⌛ 已过期：{esc(job.client)} {self._card(job)}")
            self.state.prune(self.cfg.reconcile_window)
            for key in [k for k in self.calls if k[0] not in self.state.streams]:
                del self.calls[key]

    def tools_for(self, client: Client) -> list[dict]:
        return public_tools()
