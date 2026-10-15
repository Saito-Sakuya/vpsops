"""本地账本：每个代理进程一条流，记录 seq → digest；流登记表让核对能识别"不是我开的流"。"""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


def data_dir() -> Path:
    base = os.environ.get("VPSOPS_HOME") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share"), "vpsops")
    p = Path(base)
    (p / "ledger").mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def _locked(path: Path):
    """跨进程互斥（同一客户端可能同时开多个代理进程）。"""
    lock = path.with_suffix(".lock")
    deadline = time.monotonic() + 5
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() > deadline:  # 残留锁：持有者已崩溃
                try:
                    os.unlink(lock)
                except FileNotFoundError:
                    pass
                continue
            time.sleep(0.02)
    try:
        yield
    finally:
        os.close(fd)
        os.unlink(lock)


class Registry:
    """%LOCALAPPDATA%/vpsops/streams.json：本机开过的所有流。"""

    def __init__(self, root: Path):
        self.path = root / "streams.json"

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, client: str, stream: str):
        with _locked(self.path):
            data = self._load()
            data[stream] = {"client": client, "opened": time.time(), "closed": False}
            # 只保留最近 7 天
            cutoff = time.time() - 7 * 86400
            data = {k: v for k, v in data.items() if v["opened"] >= cutoff}
            self._save(data)

    def close(self, stream: str):
        with _locked(self.path):
            data = self._load()
            if stream in data:
                data[stream]["closed"] = True
                self._save(data)

    def known(self, client: str) -> set[str]:
        return {k for k, v in self._load().items() if v["client"] == client}


class Ledger:
    """单条流的账本，同时追加写入 jsonl 作为本地审计。"""

    def __init__(self, root: Path, client: str, stream: str):
        self.client, self.stream = client, stream
        self.digests: dict[int, str] = {}
        self.next_seq = 1
        self.file = root / "ledger" / f"{time.strftime('%Y-%m-%d')}-{client}-{stream}.jsonl"

    def take_seq(self) -> int:
        seq = self.next_seq
        self.next_seq += 1
        return seq

    def record(self, seq: int, digest: str, tool: str, args: dict, status: str):
        self.digests[seq] = digest
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "stream": self.stream, "seq": seq,
               "digest": digest, "tool": tool, "args": args, "status": status}
        with open(self.file, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
