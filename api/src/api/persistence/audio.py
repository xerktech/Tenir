"""Full-audio store: every conversation's audio is retained so a stored session
can be listened to and re-downloaded later. The bytes live behind the
``AudioStore`` seam, addressed by an opaque object key: an in-memory backend for
the simulator/CI and a local-disk backend for the single-host deployment.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Protocol


def _reject_unsafe(label: str, part: str) -> None:
    """Refuse a key segment that would not stay one path segment.

    The disk backend turns the household into a directory, so a segment
    carrying a separator or a ``..`` silently re-points the key at a DIFFERENT
    household's object — still inside the store root, so a root-escape check
    alone does not catch it. Callers pass a server-issued UUID and a
    token-derived household, so anything else is a bug or an attack; raise
    rather than sanitize, because rewriting a segment could collide two
    distinct conversations onto one key (XERK-236).
    """
    if not part or part in (".", "..") or "/" in part or "\\" in part or "\x00" in part:
        raise ValueError(f"unsafe {label} for audio key: {part!r}")


def audio_key(household: str, conversation_id: str) -> str:
    """Object key for a conversation's full audio, namespaced by household."""
    _reject_unsafe("household", household)
    _reject_unsafe("conversation id", conversation_id)
    return f"{household}/{conversation_id}.wav"


class AudioStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes | None: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> bool: ...
    def ready(self) -> None:
        """Raise if the backing store is unreachable (deployment readiness probe)."""
        ...


class InMemoryAudioStore:
    """Thread-safe in-memory ``AudioStore`` (default backend)."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, key: str, data: bytes) -> None:
        with self._lock:
            self._blobs[key] = bytes(data)

    def get(self, key: str) -> bytes | None:
        with self._lock:
            return self._blobs.get(key)

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._blobs

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._blobs.pop(key, None) is not None

    def ready(self) -> None:
        # In-process: always reachable.
        return None


class LocalDiskAudioStore:
    """Filesystem-backed ``AudioStore`` for the single-host deployment.

    Each object key (``{household}/{conversation_id}.wav``) maps to a file under
    ``root``; the household segment becomes a subdirectory. Writes are atomic (a
    temp file in the target directory is ``os.replace``d into place) so a reader
    never sees a half-written WAV, and a ``put`` that races another for the same
    key leaves one intact file. This is the drop-in replacement for S3/MinIO when
    a single api process owns the disk — it does not share across api replicas.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        # Keys are exactly "{household}/{conversation_id}.wav". Checking only that
        # the resolved path stays INSIDE root is not enough: "beta/../alpha/x.wav"
        # never leaves the root and still lands in another household's directory,
        # and `resolve()` collapses the ".." away before any depth check could see
        # it (XERK-236). So validate the raw segments FIRST, then pin the depth.
        parts = key.split("/")
        if len(parts) != 2 or any(p in ("", ".", "..") or "\\" in p or "\x00" in p for p in parts):
            raise ValueError(f"audio key is not household-scoped: {key!r}")
        path = (self._root / key).resolve()
        if path.parent.parent != self._root:
            raise ValueError(f"audio key is not household-scoped: {key!r}")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file in the same directory, then atomically rename.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            # Best-effort cleanup; the temp file is invisible to readers regardless.
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def get(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> bool:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            return False
        return True

    def ready(self) -> None:
        # Readiness probe: the root must exist (create it) and be writable.
        self._root.mkdir(parents=True, exist_ok=True)
        if not os.access(self._root, os.W_OK):
            raise PermissionError(f"audio store root is not writable: {self._root}")
