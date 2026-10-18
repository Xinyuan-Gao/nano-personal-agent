from __future__ import annotations

import hashlib
import json
import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """时间有序 ID：``run_01JC8ZK3M4Q7X2``。

    前缀与实体绑定，便于日志与审计定位。
    """
    millis = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(6), "big")
    return f"{prefix}_{_encode(millis, 10)}{_encode(rand, 8)}"


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def canonical_hash(obj) -> str:
    return sha256_hex(canonical_json(obj))


def hash_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def short_hash(value: str, n: int = 12) -> str:
    return (value or "")[:n]


def package_hash(manifest: list[dict]) -> str:
    """完整包 hash：对 (path, sha256, size) 清单按 path 排序后取 canonical hash。"""
    entries = sorted(
        (
            {
                "path": e["path"],
                "sha256": e.get("sha256") or e.get("content_hash"),
                "size": int(e.get("size") or 0),
            }
            for e in manifest
        ),
        key=lambda e: e["path"],
    )
    return canonical_hash(entries)
