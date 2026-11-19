"""Media storage.

Keys are relative POSIX paths (``videos/<id>/source.mp4``). Today the backend is a local
directory shared by the API and workers (a Docker volume). The narrow interface — resolve
a key, check it, delete a prefix, read/write JSON — is what an S3/R2 adapter implements
when workers move to separate machines (see docs/ARCHITECTURE.md, "Scaling path").
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from clipforge.config import get_settings


class StorageError(ValueError):
    pass


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        if not key or key.startswith("/") or "\\" in key or ".." in key.split("/"):
            raise StorageError(f"invalid storage key: {key!r}")
        resolved = (self.root / key).resolve()
        if not resolved.is_relative_to(self.root):
            raise StorageError(f"storage key escapes root: {key!r}")
        return resolved

    def ensure_parent(self, key: str) -> Path:
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def exists(self, key: str | None) -> bool:
        return bool(key) and self.path(key).is_file()

    def size(self, key: str) -> int:
        return self.path(key).stat().st_size

    def write_json(self, key: str, data: Any) -> None:
        target = self.ensure_parent(key)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)  # atomic: readers never see a half-written file

    def read_json(self, key: str) -> Any:
        return json.loads(self.path(key).read_text(encoding="utf-8"))

    def delete_prefix(self, prefix: str) -> None:
        target = self.path(prefix.rstrip("/"))
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink(missing_ok=True)


# --- key layout ---------------------------------------------------------------------------


def video_prefix(video_id: uuid.UUID | str) -> str:
    return f"videos/{video_id}"


def video_key(video_id: uuid.UUID | str, name: str) -> str:
    return f"{video_prefix(video_id)}/{name}"


def render_key(render_id: uuid.UUID | str, ext: str) -> str:
    return f"renders/{render_id}.{ext}"


_storage: LocalStorage | None = None


def get_storage() -> LocalStorage:
    global _storage
    if _storage is None or _storage.root != get_settings().media_dir.resolve():
        _storage = LocalStorage(get_settings().media_dir)
    return _storage
