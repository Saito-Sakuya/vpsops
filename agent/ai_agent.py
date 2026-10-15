#!/usr/bin/env python3
"""ai-agent：每台 VPS 上唯一的执行入口。

由 authorized_keys 的 forced command 启动（sudo -n /usr/local/bin/ai-agent），
从 stdin 读取一个 JSON 请求，向 stdout 写一个 JSON 响应。
这台机器能做什么只由本机 root 维护的配置文件决定；不提供任意命令。

只依赖标准库。YAML 配置需要 python3-yaml，否则使用 /etc/ai-agent.json。
"""

import difflib
import hashlib
import json
import os
import re
import shlex
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    import fcntl
except ImportError:  # Windows（仅用于本地测试）
    fcntl = None
try:
    import syslog
except ImportError:
    syslog = None

VERSION = "0.1.0"
DEFAULT_CONFIG_PATHS = ("/etc/ai-agent.yaml", "/etc/ai-agent.json")
DEFAULT_STATE_DIR = "/var/lib/ai-agent"
MAX_REQUEST_BYTES = 1 << 20
MAX_CONFIG_BYTES = 512 << 10
BACKUP_KEEP = 20
MARKER = "[REDACTED"


class OpError(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


# ---------------------------------------------------------------- 配置

def load_config(path=None):
    if path is None:
        path = next((p for p in DEFAULT_CONFIG_PATHS if os.path.exists(p)), None)
        if path is None:
            raise OpError("no_config", "未找到 /etc/ai-agent.yaml 或 /etc/ai-agent.json")
    st = os.stat(path)
    # 以 root 运行时，配置必须归 root 且不可被他人写入，否则拒绝工作
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise OpError("insecure_config", f"{path} 必须归 root 所有且不可被 group/other 写入")
    with open(path, "rb") as f:
        text = f.read().decode("utf-8")
    if path.endswith(".json"):
        cfg = json.loads(text)
    else:
        try:
            import yaml
        except ImportError:
            raise OpError("no_yaml", "缺少 python3-yaml；请安装或改用 /etc/ai-agent.json")
        cfg = yaml.safe_load(text) or {}
    for key in ("logs", "configs", "services", "deploys", "compose_projects"):
        cfg.setdefault(key, {})
    cfg.setdefault("redact_extra", [])
    cfg.setdefault("max_output_kb", 64)
    cfg.setdefault("state_dir", DEFAULT_STATE_DIR)
    return cfg


def compose_ref(cfg, ref):
    """'app/api' -> ('/opt/app', 'api')"""
    project, _, service = ref.partition("/")
    directory = cfg["compose_projects"].get(project)
    if not directory or not service:
        raise OpError("bad_config", f"compose 引用无效：{ref}（需在 compose_projects 中登记 {project}）")
    return directory, service


def compose_cmd(directory, *args):
    return ["docker", "compose", "--project-directory", directory, *args]


# ---------------------------------------------------------------- 执行与输出

def run(argv, timeout=30, env=None):
    full_env = dict(os.environ, LC_ALL="C.UTF-8")
    if env:
        full_env.update(env)
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                           env=full_env)
    except FileNotFoundError:
        raise OpError("not_found", f"命令不存在：{argv[0]}")
    except subprocess.TimeoutExpired:
        raise OpError("timeout", f"命令超时（{timeout}s）：{argv[0]}")
    decode = lambda b: strip_ansi(b.decode("utf-8", "replace"))
    return p.returncode, decode(p.stdout), decode(p.stderr)


def run_template(template, timeout=60, **subst):
    """执行配置里由 root 写好的命令模板；占位符在分词之后替换，不经过 shell。"""
    argv = [part.format(**subst) for part in shlex.split(template)]
    return run(argv, timeout=timeout)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(s):
    return _ANSI.sub("", s)


def truncate(text, max_kb):
    data = text.encode("utf-8")
    limit = max_kb * 1024
    if len(data) <= limit:
        return text, False
    head = data[: limit // 5].decode("utf-8", "ignore")
    tail = data[-(limit - limit // 5):].decode("utf-8", "ignore")
    return f"{head}\n... [truncated {len(data) - limit} bytes] ...\n{tail}", True


# ---------------------------------------------------------------- 脱敏（逐行，保持行数不变）

_REDACT_RULES = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws_key]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "[REDACTED:github_token]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), "[REDACTED:api_key]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED:jwt]"),
    (re.compile(r"(://[^:/\s@]+:)[^@\s/]+@"), r"\1[REDACTED:password]@"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)(\"?\s*[=:]\s*\"?)[^\s\"',;]+"),
     r"\1\2[REDACTED:secret]"),
]
_SECRET_KEY = re.compile(r"(?i)(pass|secret|token|key|dsn|credential|auth|private|salt)")
_ENV_LINE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*?)(\s*)$")


def redact_lines(text, extra=(), secret_env=False):
    out, in_pem = [], False
    extra_rules = [(re.compile(p), "[REDACTED:custom]") for p in extra]
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        nl = line[len(body):]
        if "-----BEGIN" in body and "PRIVATE KEY" in body:
            in_pem = True
        if in_pem:
            if "-----END" in body:
                in_pem = False
            out.append("[REDACTED:private_key]" + nl)
            continue
        if secret_env:
            m = _ENV_LINE.match(body)
            if m and m.group(4) and _SECRET_KEY.search(m.group(2)):
                body = f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED len={len(m.group(4))}]{m.group(5)}"
        for rule, repl in (*_REDACT_RULES, *extra_rules):
            body = rule.sub(repl, body)
        out.append(body + nl)
    return "".join(out)


def redact(cfg, text):
    return redact_lines(text, cfg["redact_extra"])


# ---------------------------------------------------------------- 基础工具

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def open_regular(path):
    """以 O_NOFOLLOW 打开普通文件：配置/日志所在目录可能由非 root 用户控制，
    不能让它通过符号链接把 root 引向 /etc/shadow 之类的文件。"""
    check_real_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | O_NOFOLLOW)
    except FileNotFoundError:
        raise OpError("not_found", f"文件不存在：{path}")
    except OSError as e:
        if os.path.islink(path):
            raise OpError("symlink", f"{path} 是符号链接，拒绝访问")
        raise OpError("io_error", f"{path}：{e.strerror}")
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise OpError("not_regular", f"{path} 不是普通文件")
    if st.st_nlink > 1 and hasattr(os, "geteuid") and os.geteuid() == 0 and st.st_uid != 0:
        os.close(fd)
        raise OpError("hardlink", f"{path} 有多个硬链接且不归 root，拒绝访问")
    return os.fdopen(fd, "rb")


def check_real_path(path):
    """O_NOFOLLOW 只管最后一段；中间目录被换成链接时 realpath 会和登记路径不一致。"""
    if os.path.lexists(path) and os.path.realpath(path) != os.path.abspath(path):
        raise OpError("symlink", f"{path} 的路径中包含符号链接，拒绝访问")


def ensure_not_symlink(path):
    check_real_path(path)
    if os.path.islink(path):
        raise OpError("symlink", f"{path} 是符号链接，拒绝写入")


def read_text(path, limit=MAX_CONFIG_BYTES):
    with open_regular(path) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise OpError("too_large", f"{path} 超过 {limit // 1024}KB")
    return data


def atomic_write(path, data, like=None):
    """写同目录临时文件 → 复制原文件属主/权限 → os.replace（替换的是目录项，不会写穿符号链接）。"""
    ensure_not_symlink(path)
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".ai-agent-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        copy_owner_mode(like or path, tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_tmp_like(path, data):
    ensure_not_symlink(path)
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".ai-agent-", dir=directory)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    copy_owner_mode(path, tmp)
    return tmp


def copy_owner_mode(src, dst):
    if not os.path.lexists(src) or os.path.islink(src):
        return
    st = os.lstat(src)
    os.chmod(dst, st.st_mode & 0o7777)
    if hasattr(os, "chown"):
        try:
            os.chown(dst, st.st_uid, st.st_gid)
        except PermissionError:
            pass


class Lock:
    def __init__(self, cfg, name):
        d = os.path.join(cfg["state_dir"], "locks")
        os.makedirs(d, exist_ok=True)
        self.path = os.path.join(d, name + ".lock")

    def __enter__(self):
        self.f = open(self.path, "w")
        if fcntl:
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.f.close()
                raise OpError("locked", "同一目标已有操作在进行")
        return self

    def __exit__(self, *exc):
        self.f.close()


def parse_since(since):
    if not since:
        return None
    m = re.fullmatch(r"(\d+)([smhd])", since.strip())
    if m:
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
        return datetime.now(timezone.utc) - timedelta(**{unit: int(m.group(1))})
    try:
        dt = datetime.fromisoformat(since)
    except ValueError:
        raise OpError("bad_args", f"since 格式无效：{since}（用 15m/2h/1d 或 ISO 时间）")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def http_ok(url, expect=None, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        return False
    return code == expect if expect else 200 <= code < 400


def wait_http(url, expect=None, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if http_ok(url, expect):
            return True
        time.sleep(2)
    return False


# ---------------------------------------------------------------- describe / status

def op_describe(cfg, args):
    deploys = {}
    for name, d in cfg["deploys"].items():
        state = load_deploy_state(cfg, name)
        deploys[name] = {"type": d.get("type"), "version_regex": d.get("version_regex"),
                         "current": state.get("current"), "previous": state.get("previous")}
    return {
        "agent_version": VERSION,
        "hostname": os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME"),
        "logs": sorted(cfg["logs"]),
        "configs": {n: {"path": c["path"], "secret": bool(c.get("secret"))}
                    for n, c in cfg["configs"].items()},
        "services": sorted(cfg["services"]),
        "deploys": deploys,
    }


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def op_status(cfg, args):
    result, warnings = {}, []
    loadavg = _read("/proc/loadavg")
    if loadavg:
        result["load"] = [float(x) for x in loadavg.split()[:3]]
        result["cpus"] = os.cpu_count()
    uptime = _read("/proc/uptime")
    if uptime:
        result["uptime_hours"] = round(float(uptime.split()[0]) / 3600, 1)
    meminfo = _read("/proc/meminfo")
    if meminfo:
        kv = {l.split(":")[0]: int(l.split()[1]) for l in meminfo.splitlines() if l.split()[1:]}
        total, avail = kv.get("MemTotal", 0), kv.get("MemAvailable", 0)
        if total:
            pct = round(100 * (total - avail) / total, 1)
            result["memory"] = {"total_mb": total // 1024, "used_pct": pct}
            if pct > 90:
                warnings.append(f"内存使用 {pct}%")
    mounts = _read("/proc/mounts")
    if mounts and hasattr(os, "statvfs"):
        disks, seen = [], set()
        for line in mounts.splitlines():
            dev, mnt, fstype = line.split()[:3]
            if fstype not in ("ext4", "ext3", "xfs", "btrfs", "zfs") or dev in seen:
                continue
            seen.add(dev)
            s = os.statvfs(mnt)
            total = s.f_blocks * s.f_frsize
            if not total:
                continue
            pct = round(100 * (1 - s.f_bavail / s.f_blocks), 1)
            disks.append({"mount": mnt, "total_gb": round(total / 2**30, 1), "used_pct": pct})
            if pct > 85:
                warnings.append(f"磁盘 {mnt} 使用 {pct}%")
        result["disks"] = disks
    if shutil.which("systemctl"):
        rc, out, _ = run(["systemctl", "--failed", "--plain", "--no-legend", "--no-pager"])
        failed = [l.split()[0] for l in out.splitlines() if l.strip()]
        result["failed_units"] = failed
        warnings += [f"systemd 失败：{u}" for u in failed]
    if shutil.which("docker"):
        rc, out, _ = run(["docker", "ps", "-a", "--format", "{{json .}}"])
        containers = []
        for line in out.splitlines():
            try:
                c = json.loads(line)
            except ValueError:
                continue
            containers.append({"name": c.get("Names"), "image": c.get("Image"),
                               "state": c.get("State"), "status": c.get("Status")})
            status = (c.get("Status") or "").lower()
            if c.get("State") in ("exited", "restarting", "dead") or "unhealthy" in status:
                warnings.append(f"容器 {c.get('Names')}：{c.get('Status')}")
        result["containers"] = containers
    result["warnings"] = warnings
    return result


# ---------------------------------------------------------------- logs

def tail_file(path, n):
    with open_regular(path) as f:
        f.seek(0, os.SEEK_END)
        pos, chunks, newlines = f.tell(), [], 0
        while pos > 0 and newlines <= n:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            chunk = f.read(step)
            chunks.insert(0, chunk)
            newlines += chunk.count(b"\n")
    lines = b"".join(chunks).decode("utf-8", "replace").splitlines()
    return lines[-n:]


def op_logs(cfg, args):
    source = args.get("source")
    ref = cfg["logs"].get(source)
    if not ref:
        raise OpError("unknown_source", f"未登记的日志源：{source}", available=sorted(cfg["logs"]))
    lines = max(1, min(int(args.get("lines", 200)), 1000))
    grep = args.get("grep")
    pattern = None
    if grep:
        if len(grep) > 200:
            raise OpError("bad_args", "grep 最长 200 字符")
        try:
            pattern = re.compile(grep)
        except re.error as e:
            raise OpError("bad_args", f"grep 正则无效：{e}")
    since = parse_since(args.get("since"))
    fetch = 20000 if pattern else lines
    kind, _, target = ref.partition(":")
    note = None
    if kind == "file":
        text_lines = tail_file(target, fetch)
        if since:
            note = "文件类日志不支持 since 过滤，已忽略"
    elif kind == "journald":
        argv = ["journalctl", "-u", target, "-n", str(fetch), "--no-pager", "-o", "short-iso"]
        if since:
            argv += ["--since", f"@{int(since.timestamp())}"]
        rc, out, err = run(argv)
        if rc:
            raise OpError("command_failed", err.strip()[-2000:])
        text_lines = out.splitlines()
    elif kind == "compose":
        directory, service = compose_ref(cfg, target)
        argv = compose_cmd(directory, "logs", "--no-color", "--no-log-prefix", "--timestamps",
                           "--tail", str(fetch))
        if since:
            argv += ["--since", since.strftime("%Y-%m-%dT%H:%M:%SZ")]
        rc, out, err = run(argv + [service])
        if rc:
            raise OpError("command_failed", err.strip()[-2000:])
        text_lines = (out + err).splitlines()
    else:
        raise OpError("bad_config", f"日志源类型无效：{ref}")
    if pattern:
        text_lines = [l for l in text_lines if pattern.search(l[:4096])]
    text_lines = text_lines[-lines:]
    text, truncated = truncate(redact(cfg, "\n".join(text_lines)), cfg["max_output_kb"])
    res = {"source": source, "count": len(text_lines), "text": text, "truncated": truncated}
    if note:
        res["note"] = note
    return res


# ---------------------------------------------------------------- configs

def get_config_entry(cfg, name):
    entry = cfg["configs"].get(name)
    if not entry:
        raise OpError("unknown_config", f"未登记的配置：{name}", available=sorted(cfg["configs"]))
    return entry


def config_view(cfg, entry, raw):
    text = raw.decode("utf-8", "replace")
    return redact_lines(text, cfg["redact_extra"], secret_env=bool(entry.get("secret")))


def backup_dir(cfg, name):
    d = os.path.join(cfg["state_dir"], "backups", name)
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def list_backups(cfg, name):
    d = backup_dir(cfg, name)
    return sorted((f[:-4] for f in os.listdir(d) if f.endswith(".bak")), reverse=True)


def make_backup(cfg, name, raw):
    d = backup_dir(cfg, name)
    bid = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{sha256_bytes(raw)[:8]}"
    path = os.path.join(d, bid + ".bak")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    for old in list_backups(cfg, name)[BACKUP_KEEP:]:
        os.unlink(os.path.join(d, old + ".bak"))
    return bid


def op_config_read(cfg, args):
    name = args.get("name")
    entry = get_config_entry(cfg, name)
    raw = read_text(entry["path"])
    view = config_view(cfg, entry, raw)
    return {"name": name, "path": entry["path"], "sha256": sha256_bytes(raw), "content": view,
            "redacted": view != raw.decode("utf-8", "replace"),
            "backups": list_backups(cfg, name)[:5]}


def apply_edits(view, edits):
    if not isinstance(edits, list) or not edits:
        raise OpError("bad_args", "edits 不能为空")
    for i, e in enumerate(edits):
        old, new = e.get("old"), e.get("new")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise OpError("bad_args", f"edits[{i}] 需要非空 old 和字符串 new")
        if MARKER in old or MARKER in new:
            raise OpError("redacted_line", f"edits[{i}] 涉及脱敏内容，不允许修改")
        n = view.count(old)
        if n != 1:
            raise OpError("old_not_unique" if n else "old_not_found",
                          f"edits[{i}].old 在文件中出现 {n} 次，必须恰好 1 次")
        view = view.replace(old, new, 1)
    return view


def rebuild(raw_text, old_view, new_view):
    """把对脱敏视图的修改映射回真实内容；未改动的行原样保留真实值，脱敏行不可被改动。"""
    raw_lines = raw_text.splitlines(keepends=True)
    ov = old_view.splitlines(keepends=True)
    nv = new_view.splitlines(keepends=True)
    if len(raw_lines) != len(ov):
        raise OpError("internal", "脱敏视图行数不一致")
    out = []
    sm = difflib.SequenceMatcher(a=ov, b=nv, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out += raw_lines[i1:i2]
            continue
        if any(ov[i] != raw_lines[i] for i in range(i1, i2)):
            raise OpError("redacted_line", "修改波及了含脱敏内容的行，不允许")
        if any(MARKER in l for l in nv[j1:j2]):
            raise OpError("redacted_line", "新内容不能包含脱敏标记")
        out += nv[j1:j2]
    return "".join(out)


def run_apply(cfg, apply):
    if not apply:
        return True, ""
    if apply.startswith("compose:"):
        ref, _, action = apply[len("compose:"):].partition(" ")
        directory, service = compose_ref(cfg, ref)
        argv = compose_cmd(directory, *(["up", "-d"] if action.strip() == "up" else ["restart"]),
                           service)
        rc, out, err = run(argv, timeout=120)
    else:
        rc, out, err = run_template(apply, timeout=120)
    return rc == 0, (out + err).strip()[-2000:]


def check_config(cfg, entry):
    if entry.get("check"):
        ok, state = wait_service(cfg, entry["check"], timeout=30)
        if not ok:
            return False, f"服务 {entry['check']} 状态：{state}"
    if entry.get("health") and not wait_http(entry["health"], timeout=30):
        return False, f"健康检查失败：{entry['health']}"
    return True, ""


def install_config(cfg, name, entry, old_raw, new_raw):
    """备份 → 校验 → 原子替换 → 生效 → 检查；任何一步失败都还原并重新生效。"""
    path = entry["path"]
    bid = make_backup(cfg, name, old_raw)
    validate = entry.get("validate")
    steps = {"backup": bid}

    def restore(stage, detail):
        atomic_write(path, old_raw)
        ok, out = run_apply(cfg, entry.get("apply"))
        raise OpError(f"{stage}_failed", detail, rolled_back=True, backup=bid,
                      reapply_ok=ok, reapply_output=out)

    if validate and "{tmp}" in validate:
        tmp = write_tmp_like(path, new_raw)
        try:
            rc, out, err = run_template(validate, tmp=tmp, path=path)
            if rc:
                raise OpError("validate_failed", (out + err).strip()[-2000:], rolled_back=False,
                              note="文件未改动")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    else:
        atomic_write(path, new_raw)
        if validate:
            rc, out, err = run_template(validate, path=path)
            if rc:
                restore("validate", (out + err).strip()[-2000:])
    steps["validate"] = "ok" if validate else "skipped"
    ok, out = run_apply(cfg, entry.get("apply"))
    if not ok:
        restore("apply", out)
    steps["apply"] = out or "ok"
    ok, detail = check_config(cfg, entry)
    if not ok:
        restore("check", detail)
    steps["check"] = "ok"
    return steps


def op_config_edit(cfg, args):
    name = args.get("name")
    entry = get_config_entry(cfg, name)
    with Lock(cfg, "config-" + name):
        raw = read_text(entry["path"])
        current = sha256_bytes(raw)
        if args.get("sha256") != current:
            raise OpError("sha_mismatch", "文件已变化，请重新 config_read", sha256=current)
        raw_text = raw.decode("utf-8")
        old_view = config_view(cfg, entry, raw)
        new_view = apply_edits(old_view, args.get("edits"))
        new_text = rebuild(raw_text, old_view, new_view)
        if new_text == raw_text:
            return {"changed": False}
        new_raw = new_text.encode("utf-8")
        steps = install_config(cfg, name, entry, raw, new_raw)
    diff = "".join(difflib.unified_diff(old_view.splitlines(True), new_view.splitlines(True),
                                        fromfile=entry["path"], tofile=entry["path"]))
    return {"changed": True, "diff": truncate(diff, 16)[0], "sha256": sha256_bytes(new_raw), **steps}


def op_config_restore(cfg, args):
    name = args.get("name")
    entry = get_config_entry(cfg, name)
    backups = list_backups(cfg, name)
    bid = args.get("backup") or (backups[0] if backups else None)
    if not bid or bid not in backups:
        raise OpError("no_backup", f"备份不存在：{bid}", available=backups[:10])
    with open_regular(os.path.join(backup_dir(cfg, name), bid + ".bak")) as f:
        target = f.read()
    with Lock(cfg, "config-" + name):
        raw = read_text(entry["path"])
        if raw == target:
            return {"changed": False, "restored": bid}
        steps = install_config(cfg, name, entry, raw, target)
    return {"changed": True, "restored": bid, "sha256": sha256_bytes(target), **steps}


# ---------------------------------------------------------------- services

def service_state(cfg, ref):
    kind, _, target = ref.partition(":")
    if kind == "systemd":
        _, out, _ = run(["systemctl", "is-active", target])
        state = out.strip()
        return state == "active", state in ("activating", "reloading"), state
    if kind == "compose":
        directory, service = compose_ref(cfg, target)
        _, out, _ = run(compose_cmd(directory, "ps", "--all", "--format", "json", service))
        out = out.strip()
        items = json.loads(out) if out.startswith("[") else [json.loads(l) for l in out.splitlines() if l]
        if not items:
            return False, False, "not_found"
        c = items[0]
        state, health = c.get("State", ""), c.get("Health", "")
        ok = state == "running" and health in ("", "healthy")
        pending = state in ("created", "restarting") or health == "starting"
        return ok, pending, f"{state} {health}".strip()
    raise OpError("bad_config", f"服务引用无效：{ref}")


def wait_service(cfg, name, timeout=60):
    ref = cfg["services"].get(name)
    if not ref:
        raise OpError("unknown_service", f"未登记的服务：{name}")
    start = time.monotonic()
    while True:
        ok, pending, state = service_state(cfg, ref)
        elapsed = time.monotonic() - start
        # 前 5 秒总是重试（重启后状态可能短暂异常）；之后只在仍处于过渡状态时继续等
        if ok or elapsed > timeout or (elapsed > 5 and not pending):
            return ok, state
        time.sleep(2)


def op_service(cfg, args):
    name, action = args.get("name"), args.get("action")
    ref = cfg["services"].get(name)
    if not ref:
        raise OpError("unknown_service", f"未登记的服务：{name}", available=sorted(cfg["services"]))
    if action not in ("restart", "reload", "up"):
        raise OpError("bad_args", "action 只能是 restart/reload/up")
    kind, _, target = ref.partition(":")
    if kind == "systemd":
        argv = ["systemctl", {"restart": "restart", "reload": "reload", "up": "start"}[action], target]
    elif kind == "compose":
        if action == "reload":
            raise OpError("bad_args", "compose 服务不支持 reload")
        directory, service = compose_ref(cfg, target)
        argv = compose_cmd(directory, *(["restart"] if action == "restart" else ["up", "-d"]), service)
    else:
        raise OpError("bad_config", f"服务引用无效：{ref}")
    with Lock(cfg, "service-" + name):
        rc, out, err = run(argv, timeout=120)
        if rc:
            raise OpError("command_failed", (out + err).strip()[-2000:])
        ok, state = wait_service(cfg, name, timeout=60)
    return {"service": name, "action": action, "healthy": ok, "state": state}


# ---------------------------------------------------------------- deploy

def deploy_state_path(cfg, target):
    d = os.path.join(cfg["state_dir"], "deploys")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, target + ".json")


def load_deploy_state(cfg, target):
    try:
        with open(deploy_state_path(cfg, target)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_deploy_state(cfg, target, state):
    state["history"] = state.get("history", [])[-20:]
    path = deploy_state_path(cfg, target)
    atomic_write(path, json.dumps(state, indent=1).encode(), like=path if os.path.exists(path) else None)


_ENV_VAR = r"^(\s*{}\s*=\s*)(.*?)\s*$"


def env_get(text, var):
    m = re.search(_ENV_VAR.format(re.escape(var)), text, re.M)
    return m.group(2).strip("'\"") if m else None


def env_set(text, var, value):
    pattern = re.compile(_ENV_VAR.format(re.escape(var)), re.M)
    if pattern.search(text):
        return pattern.sub(lambda m: m.group(1) + value, text, count=1)
    return text + ("" if text.endswith("\n") or not text else "\n") + f"{var}={value}\n"


def check_root_owned(path):
    """以 root 执行的脚本：它和所在目录都必须归 root 且不可被他人写，否则等于把 root 交给了写入者。"""
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        return
    for p in (path, os.path.dirname(os.path.abspath(path))):
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise OpError("insecure_script", f"{p} 必须归 root、不是符号链接、且不可被 group/other 写")


def deploy_switch(cfg, d, version):
    """切换到 version，返回 (ok, output)。"""
    if d["type"] == "compose-image":
        env_path = os.path.join(d["compose"], d.get("env_file", ".env"))
        text = read_text(env_path).decode() if os.path.exists(env_path) else ""
        atomic_write(env_path, env_set(text, d["env_var"], version).encode(),
                     like=env_path if os.path.exists(env_path) else None)
        argv = compose_cmd(d["compose"], "up", "-d", "--pull", "missing", *d.get("services", []))
        rc, out, err = run(argv, timeout=d.get("timeout", 300))
        return rc == 0, (out + err).strip()[-3000:]
    if d["type"] == "script":
        check_root_owned(d["run"])
        rc, out, err = run([d["run"]], timeout=d.get("timeout", 300), env={"VERSION": version})
        return rc == 0, (out + err).strip()[-3000:]
    raise OpError("bad_config", f"部署类型无效：{d.get('type')}")


def deploy_current(cfg, d, state):
    if d["type"] == "compose-image":
        env_path = os.path.join(d["compose"], d.get("env_file", ".env"))
        if os.path.exists(env_path):
            return env_get(read_text(env_path).decode(), d["env_var"])
    return state.get("current")


def op_deploy(cfg, args):
    target, version = args.get("target"), args.get("version")
    d = cfg["deploys"].get(target)
    if not d:
        raise OpError("unknown_target", f"未登记的部署目标：{target}", available=sorted(cfg["deploys"]))
    with Lock(cfg, "deploy-" + target):
        state = load_deploy_state(cfg, target)
        current = deploy_current(cfg, d, state)
        if version == "rollback":
            version = state.get("previous")
            if not version:
                raise OpError("no_previous", "没有可回滚的上一版本")
        elif not isinstance(version, str) or not re.fullmatch(d["version_regex"], version):
            raise OpError("bad_version", f"版本不匹配 {d['version_regex']}：{version}")
        if version == current:
            return {"changed": False, "version": current}
        record = {"ts": now_iso(), "from": current, "to": version}
        ok, output = deploy_switch(cfg, d, version)
        stage = "switch"
        if ok and d.get("health"):
            ok = wait_http(d["health"], d.get("health_expect"), d.get("health_timeout", 90))
            stage = "health"
        if ok:
            state.update(previous=current, current=version)
            record["result"] = "ok"
            state.setdefault("history", []).append(record)
            save_deploy_state(cfg, target, state)
            return {"changed": True, "from": current, "to": version, "output": output}
        rb_ok, rb_out = (False, "无旧版本，无法回滚")
        if current:
            rb_ok, rb_out = deploy_switch(cfg, d, current)
        record["result"] = f"{stage}_failed, rolled_back={rb_ok}"
        state.setdefault("history", []).append(record)
        save_deploy_state(cfg, target, state)
        raise OpError(f"{stage}_failed", f"部署 {version} 失败（{stage}）", output=output,
                      rolled_back=rb_ok, rollback_output=rb_out, version=current)


# ---------------------------------------------------------------- main

OPS = {
    "describe": op_describe,
    "status": op_status,
    "logs": op_logs,
    "config_read": op_config_read,
    "config_edit": op_config_edit,
    "config_restore": op_config_restore,
    "service": op_service,
    "deploy": op_deploy,
}


def audit(req, resp):
    if not syslog:
        return
    meta = req.get("meta") or {}
    line = json.dumps({"op": req.get("op"), "client": meta.get("client"), "stream": meta.get("stream"),
                       "seq": meta.get("seq"), "call_id": meta.get("call_id"),
                       "reason": meta.get("reason"), "ok": resp.get("ok"),
                       "error": resp.get("error")}, ensure_ascii=False)
    syslog.openlog("ai-agent", 0, syslog.LOG_AUTH)
    syslog.syslog(syslog.LOG_INFO, line)


def handle(req, config_path=None):
    op = req.get("op")
    try:
        if op not in OPS:
            raise OpError("unknown_op", f"不支持的操作：{op}")
        cfg = load_config(config_path)
        args = req.get("args") or {}
        if not isinstance(args, dict):
            raise OpError("bad_args", "args 必须是对象")
        return {"ok": True, "data": OPS[op](cfg, args)}
    except OpError as e:
        return {"ok": False, "error": e.code, "message": e.message, **e.extra}
    except Exception as e:  # 不返回 traceback，避免泄露路径以外的细节
        return {"ok": False, "error": "internal", "message": f"{type(e).__name__}: {e}"}


HARD_TIMEOUT = {"deploy": 900, "config_edit": 300, "config_restore": 300, "service": 300}


def arm_watchdog(op):
    """整个进程的硬超时：兜住灾难性正则回溯等任何卡死情况（Gateway 断开后 root 进程不能一直留着）。"""
    if hasattr(signal, "alarm"):
        signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
        signal.alarm(HARD_TIMEOUT.get(op, 90))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    config_path = None
    if len(argv) == 2 and argv[0] == "--config":
        # 只用于本地测试：以 root 运行时不允许指定配置，防止绕过 /etc 下的白名单
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            print(json.dumps({"ok": False, "error": "forbidden", "message": "root 下不允许 --config"}))
            return 1
        config_path = argv[1]
    elif argv == ["--version"]:
        print(VERSION)
        return 0
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        resp = {"ok": False, "error": "too_large", "message": "请求过大"}
        req = {}
    else:
        try:
            req = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(req, dict):
                raise ValueError
            arm_watchdog(req.get("op"))
            resp = handle(req, config_path)
        except ValueError:
            req, resp = {}, {"ok": False, "error": "bad_request", "message": "请求不是合法 JSON"}
    audit(req, resp)
    sys.stdout.buffer.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
