"""规范化 JSON 与 digest：proxy 和 gateway 必须用同一份实现，核对才有意义。"""

import hashlib
import json


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def call_digest(stream: str, seq: int, tool: str, args: dict) -> str:
    return hashlib.sha256(canonical({"stream": stream, "seq": seq, "tool": tool, "args": args})).hexdigest()


def set_hash(digests_by_seq: dict[int, str]) -> str:
    """按 seq 排序后拼接所有 digest 再求哈希；与调用到达顺序无关。"""
    h = hashlib.sha256()
    for seq in sorted(digests_by_seq):
        h.update(f"{seq}:{digests_by_seq[seq]}\n".encode())
    return h.hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
