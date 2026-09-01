from __future__ import annotations

import hashlib
import heapq
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Protocol

from research_harness.data_adapters import AdapterError

from .model import (
    AcquisitionConflictError,
    AcquisitionCommand,
    AcquisitionContractError,
    CacheObject,
    PinnedNodeManifest,
    canonical_json_bytes,
    parse_manifest,
    parse_command,
    serialize_command,
    serialize_manifest,
)


_MANIFEST_ID_RE = re.compile(r"^acqmanifest_[a-f0-9]{64}$")
_COMMAND_ID_RE = re.compile(r"^acqcmd_[a-f0-9]{64}$")


class AcquisitionStorageError(OSError):
    pass


class AcquisitionDeadlineExceeded(TimeoutError):
    pass


class AcquisitionDownloadExceeded(ValueError):
    pass


class BinaryReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


def _check_deadline(
    deadline: float | None,
    *,
    monotonic: Callable[[], float],
) -> None:
    if deadline is not None and monotonic() >= deadline:
        raise AcquisitionDeadlineExceeded("acquisition wall-time budget exhausted")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_file(
    source: Path,
    target: Path,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    with source.open("rb") as reader, target.open("xb") as writer:
        while True:
            _check_deadline(deadline, monotonic=monotonic)
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            writer.write(chunk)
            _check_deadline(deadline, monotonic=monotonic)
        writer.flush()
        os.fsync(writer.fileno())
        _check_deadline(deadline, monotonic=monotonic)


def _copy_directory(
    source: Path,
    target: Path,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    target.mkdir()
    for entry in _walk_files(source, deadline=deadline, monotonic=monotonic):
        destination = target / entry.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_file(
            entry,
            destination,
            deadline=deadline,
            monotonic=monotonic,
        )
    _fsync_directory_tree(target, deadline=deadline, monotonic=monotonic)


def _directory_entries(
    directory: Path,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> list[os.DirEntry[str]]:
    entries: list[os.DirEntry[str]] = []
    with os.scandir(directory) as scanner:
        for entry in scanner:
            _check_deadline(deadline, monotonic=monotonic)
            entries.append(entry)
    entries.sort(key=lambda item: item.name)
    _check_deadline(deadline, monotonic=monotonic)
    return entries


def _walk_files(
    directory: Path,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> Iterator[Path]:
    frontier: list[tuple[str, bool, Path]] = []

    def enqueue(children: Path, prefix: str) -> None:
        for entry in _directory_entries(
            children,
            deadline=deadline,
            monotonic=monotonic,
        ):
            path = Path(entry.path)
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            if entry.is_symlink():
                raise AdapterError(f"directory source contains a symlink: {path}")
            if entry.is_dir(follow_symlinks=False):
                heapq.heappush(frontier, (relative, True, path))
                continue
            if not entry.is_file(follow_symlinks=False):
                raise AdapterError(
                    f"directory source contains a special file: {path}"
                )
            heapq.heappush(frontier, (relative, False, path))

    enqueue(directory, "")
    while frontier:
        _check_deadline(deadline, monotonic=monotonic)
        relative, is_directory, path = heapq.heappop(frontier)
        if is_directory:
            enqueue(path, relative)
            continue
        yield path


def _fsync_directory_tree(
    directory: Path,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    for entry in _directory_entries(
        directory,
        deadline=deadline,
        monotonic=monotonic,
    ):
        _check_deadline(deadline, monotonic=monotonic)
        if entry.is_dir(follow_symlinks=False):
            _fsync_directory_tree(
                Path(entry.path),
                deadline=deadline,
                monotonic=monotonic,
            )
    _fsync_directory(directory)
    _check_deadline(deadline, monotonic=monotonic)


def _fingerprint_file(
    path: Path,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            _check_deadline(deadline, monotonic=monotonic)
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            _check_deadline(deadline, monotonic=monotonic)
    return digest.hexdigest(), size


def _fingerprint_path(
    path: Path,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
) -> tuple[str, int, int]:
    path = path.expanduser()
    if path.is_symlink():
        raise AdapterError(f"source is a symlink: {path}")
    path = path.resolve()
    if not path.exists():
        raise AdapterError(f"source does not exist or is a symlink: {path}")
    if path.is_file():
        digest, size = _fingerprint_file(
            path,
            deadline=deadline,
            monotonic=monotonic,
        )
        return digest, size, 1
    if not path.is_dir():
        raise AdapterError(f"source is not a regular file or directory: {path}")
    tree = hashlib.sha256()
    total_size = 0
    count = 0
    for entry in _walk_files(path, deadline=deadline, monotonic=monotonic):
        file_digest, size = _fingerprint_file(
            entry,
            deadline=deadline,
            monotonic=monotonic,
        )
        relative = entry.relative_to(path).as_posix()
        tree.update(f"{relative}\0{size}\0{file_digest}\n".encode("utf-8"))
        total_size += size
        count += 1
    return tree.hexdigest(), total_size, count


def _remove_temporary(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


class ContentAddressedCache:
    def __init__(
        self,
        root: Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.root = root.resolve()
        self.objects = self.root / "objects" / "sha256"
        self._monotonic = monotonic
        self.objects.mkdir(parents=True, exist_ok=True)

    def object_path(self, value: CacheObject) -> Path:
        suffix = ".file" if value.object_kind == "file" else ".directory"
        return self.objects / f"{value.content_sha256[7:]}{suffix}"

    def put_stream(
        self,
        reader: BinaryReader,
        *,
        max_bytes: int,
        deadline: float | None,
    ) -> CacheObject:
        temporary = self.objects / f".incoming.{uuid.uuid4().hex}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as writer:
                while True:
                    _check_deadline(deadline, monotonic=self._monotonic)
                    chunk = reader.read(min(1024 * 1024, max_bytes - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise AcquisitionDownloadExceeded(
                            "response exceeds the cycle download budget"
                        )
                    digest.update(chunk)
                    writer.write(chunk)
                    _check_deadline(deadline, monotonic=self._monotonic)
                writer.flush()
                os.fsync(writer.fileno())
            value = CacheObject(
                content_sha256=f"sha256:{digest.hexdigest()}",
                size_bytes=size,
                entry_count=1,
                object_kind="file",
            )
            target = self.object_path(value)
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.verify(value, deadline=deadline)
            temporary.unlink()
            _fsync_directory(self.objects)
            _check_deadline(deadline, monotonic=self._monotonic)
            return value
        except (AcquisitionDeadlineExceeded, AcquisitionDownloadExceeded):
            _remove_temporary(temporary)
            raise
        except OSError as exc:
            _remove_temporary(temporary)
            raise AcquisitionStorageError(str(exc)) from exc

    def put_registered(
        self,
        source: Path,
        *,
        expected_digest: str,
        expected_size: int,
        expected_entry_count: int,
        deadline: float | None = None,
    ) -> CacheObject:
        source = source.expanduser()
        try:
            digest, size, count = _fingerprint_path(
                source,
                deadline=deadline,
                monotonic=self._monotonic,
            )
        except AcquisitionDeadlineExceeded:
            raise
        except (AdapterError, OSError) as exc:
            raise AcquisitionStorageError(str(exc)) from exc
        expected_raw = expected_digest[7:]
        if (digest, size, count) != (
            expected_raw,
            expected_size,
            expected_entry_count,
        ):
            raise AcquisitionConflictError(
                "registered source no longer matches its pinned snapshot"
            )
        object_kind = "directory" if source.is_dir() else "file"
        value = CacheObject(
            content_sha256=expected_digest,
            size_bytes=size,
            entry_count=count,
            object_kind=object_kind,
        )
        target = self.object_path(value)
        if target.exists():
            self.verify(value, deadline=deadline)
            return value
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            if object_kind == "directory":
                _copy_directory(
                    source,
                    temporary,
                    deadline=deadline,
                    monotonic=self._monotonic,
                )
            else:
                _copy_file(
                    source,
                    temporary,
                    deadline=deadline,
                    monotonic=self._monotonic,
                )
            actual, actual_size, actual_count = _fingerprint_path(
                temporary,
                deadline=deadline,
                monotonic=self._monotonic,
            )
            if (actual, actual_size, actual_count) != (digest, size, count):
                raise AcquisitionStorageError("cache copy changed the registered content")
            try:
                os.rename(temporary, target)
            except FileExistsError:
                _remove_temporary(temporary)
                self.verify(value, deadline=deadline)
            _fsync_directory(self.objects)
            _check_deadline(deadline, monotonic=self._monotonic)
            return value
        except AcquisitionDeadlineExceeded:
            _remove_temporary(temporary)
            raise
        except (AdapterError, OSError) as exc:
            _remove_temporary(temporary)
            if isinstance(exc, AcquisitionStorageError):
                raise
            raise AcquisitionStorageError(str(exc)) from exc

    def verify(
        self,
        value: CacheObject,
        *,
        deadline: float | None = None,
    ) -> Path:
        path = self.object_path(value)
        try:
            digest, size, count = _fingerprint_path(
                path,
                deadline=deadline,
                monotonic=self._monotonic,
            )
        except AcquisitionDeadlineExceeded:
            raise
        except (AdapterError, OSError) as exc:
            raise AcquisitionStorageError(str(exc)) from exc
        if (f"sha256:{digest}", size, count) != (
            value.content_sha256,
            value.size_bytes,
            value.entry_count,
        ):
            raise AcquisitionConflictError("content-addressed cache object was modified")
        return path


class ManifestStore:
    def __init__(self, root: Path, cache: ContentAddressedCache) -> None:
        self.root = root.resolve()
        self.cache = cache
        self.manifests = self.root / "manifests" / "sha256"
        self.commands = self.root / "commands" / "sha256"
        self.pins = self.root / "pins"
        self.manifests.mkdir(parents=True, exist_ok=True)
        self.commands.mkdir(parents=True, exist_ok=True)
        self.pins.mkdir(parents=True, exist_ok=True)

    def _manifest_path(self, manifest_id: str) -> Path:
        if not isinstance(manifest_id, str) or _MANIFEST_ID_RE.fullmatch(manifest_id) is None:
            raise AcquisitionContractError("manifest id is invalid")
        return self.manifests / f"{manifest_id.removeprefix('acqmanifest_')}.json"

    def _pin_path(self, node_id: str) -> Path:
        digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()
        return self.pins / f"{digest}.json"

    def _command_path(self, command_id: str) -> Path:
        if not isinstance(command_id, str) or _COMMAND_ID_RE.fullmatch(command_id) is None:
            raise AcquisitionContractError("acquisition command id is invalid")
        return self.commands / f"{command_id.removeprefix('acqcmd_')}.json"

    def _write_once(self, path: Path, payload: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing = path.read_bytes()
                if existing != payload:
                    raise AcquisitionConflictError(
                        f"immutable record conflict at {path.name}"
                    )
            temporary.unlink()
        except AcquisitionConflictError:
            _remove_temporary(temporary)
            raise
        except OSError as exc:
            _remove_temporary(temporary)
            raise AcquisitionStorageError(str(exc)) from exc
        _fsync_directory(path.parent)

    def _read_pin(self, node_id: str) -> dict[str, str] | None:
        pin_path = self._pin_path(node_id)
        if not pin_path.exists():
            return None
        try:
            pin = json.loads(pin_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AcquisitionStorageError(str(exc)) from exc
        if set(pin) != {"manifest_id", "node_id"} or pin["node_id"] != node_id:
            raise AcquisitionConflictError("node pin is malformed or bound elsewhere")
        return pin

    def pin(self, manifest: PinnedNodeManifest) -> None:
        existing = self._read_pin(manifest.node_id)
        if existing is not None and existing["manifest_id"] != manifest.manifest_id:
            raise AcquisitionConflictError("node is already pinned to another manifest")
        manifest_bytes = canonical_json_bytes(serialize_manifest(manifest)) + b"\n"
        self._write_once(self._manifest_path(manifest.manifest_id), manifest_bytes)
        pin = canonical_json_bytes(
            {"manifest_id": manifest.manifest_id, "node_id": manifest.node_id}
        ) + b"\n"
        self._write_once(self._pin_path(manifest.node_id), pin)

    def record_command(self, command: AcquisitionCommand) -> None:
        payload = canonical_json_bytes(serialize_command(command)) + b"\n"
        self._write_once(self._command_path(command.command_id), payload)

    def load_command(self, command_id: str) -> AcquisitionCommand:
        path = self._command_path(command_id)
        try:
            raw = path.read_bytes()
            command = parse_command(json.loads(raw))
        except (OSError, json.JSONDecodeError) as exc:
            raise AcquisitionStorageError(str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise AcquisitionConflictError(str(exc)) from exc
        if command.command_id != command_id:
            raise AcquisitionConflictError("acquisition command identity changed")
        if raw != canonical_json_bytes(serialize_command(command)) + b"\n":
            raise AcquisitionConflictError("acquisition command encoding is not canonical")
        return command

    def load_for_node(
        self,
        node_id: str,
        *,
        deadline: float | None = None,
    ) -> PinnedNodeManifest | None:
        pin = self._read_pin(node_id)
        if pin is None:
            return None
        return self.load(pin["manifest_id"], node_id=node_id, deadline=deadline)

    def load(
        self,
        manifest_id: str,
        *,
        node_id: str,
        deadline: float | None = None,
    ) -> PinnedNodeManifest:
        path = self._manifest_path(manifest_id)
        pin = self._read_pin(node_id)
        if pin is None or pin["manifest_id"] != manifest_id:
            raise AcquisitionConflictError("manifest is not the authoritative node pin")
        try:
            raw = path.read_bytes()
            document = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise AcquisitionStorageError(str(exc)) from exc
        try:
            manifest = parse_manifest(document)
        except (ValueError, TypeError) as exc:
            raise AcquisitionConflictError(str(exc)) from exc
        if manifest.manifest_id != manifest_id or manifest.node_id != node_id:
            raise AcquisitionConflictError("manifest identity or node binding changed")
        canonical = canonical_json_bytes(serialize_manifest(manifest)) + b"\n"
        if raw != canonical:
            raise AcquisitionConflictError("manifest encoding is not canonical")
        for acquired in manifest.acquired_needs:
            self.cache.verify(acquired.receipt.cache_object, deadline=deadline)
        return manifest
