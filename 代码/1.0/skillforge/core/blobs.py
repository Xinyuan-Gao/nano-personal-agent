from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from .errors import InvalidInput, StorageError
from .ids import sha256_hex


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- 写入
    def path_for(self, digest: str) -> Path:
        if not digest or len(digest) < 4 or not all(c in "0123456789abcdef" for c in digest):
            raise InvalidInput(f"非法制品 hash: {digest!r}", field_errors={"hash": "invalid"})
        return self.root / digest[:2] / digest

    def exists(self, digest: str) -> bool:
        try:
            return self.path_for(digest).is_file()
        except InvalidInput:
            return False

    def put_bytes(self, data: bytes) -> str:
        digest = sha256_hex(data)
        target = self.path_for(digest)
        if target.exists():
            if target.stat().st_size != len(data):  # pragma: no cover - 损坏检测
                raise StorageError(f"制品库损坏: {digest}")
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, target)
            self._protect(target)
        except OSError as exc:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise StorageError(f"写入制品失败: {exc}") from exc
        return digest

    def put_text(self, text: str) -> str:
        return self.put_bytes(text.encode("utf-8"))

    def put_file(self, path: Path) -> str:
        with open(path, "rb") as fh:
            return self.put_bytes(fh.read())

    # ------------------------------------------------------------- 读取
    def get_bytes(self, digest: str) -> bytes:
        target = self.path_for(digest)
        if not target.exists():
            from .errors import EvidenceUnavailable

            raise EvidenceUnavailable(f"制品不存在或已被清除: {digest}")
        try:
            return target.read_bytes()
        except OSError as exc:  # pragma: no cover
            raise StorageError(f"读取制品失败: {exc}") from exc

    def get_text(self, digest: str, encoding: str = "utf-8") -> str:
        return self.get_bytes(digest).decode(encoding, errors="replace")

    def verify(self, digest: str) -> bool:
        target = self.path_for(digest)
        if not target.is_file():
            return False
        return sha256_hex(target.read_bytes()) == digest

    # ------------------------------------------------------------- 维护
    def write_to(self, digest: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and sha256_hex(dest.read_bytes()) == digest:
            return dest
        with open(dest, "wb") as fh:
            fh.write(self.get_bytes(digest))
        return dest

    def copy_tree(self, dest: Path) -> Path:
        dest = Path(dest)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.root, dest)
        return dest

    def usage(self) -> dict:
        count = 0
        total = 0
        for root, _dirs, files in os.walk(self.root):
            for name in files:
                count += 1
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
        return {"blob_count": count, "blob_bytes": total}

    @staticmethod
    def _protect(path: Path) -> None:
        try:
            os.chmod(path, 0o444)
        except OSError:  # pragma: no cover
            pass
