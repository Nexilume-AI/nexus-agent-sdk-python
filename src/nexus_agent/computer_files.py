"""Computer-local binary transfers; handles never escape their Run and root."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from .workspace_files import FILE_CHUNK_BYTES, FILE_MAX_BYTES


class ComputerFileTransfers:
    TTL_SECONDS = 900
    MAX_ACTIVE = 8

    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = threading.RLock()
        self.handles = {}
        self.journal = runtime.root / "file-transfers.json"
        # Only files recorded by this registration are eligible for recovery.
        for value in runtime._load_json(self.journal).get("temporary_files", []):
            try:
                root = runtime._resolved_root()
                lexical = Path(str(value)).absolute()
                path = runtime._safe_path(str(lexical), root=root)
                if path == lexical and re.fullmatch(r"\.nexus-transfer-[0-9a-f]{32}", path.name) and not lexical.is_symlink():
                    path.unlink(missing_ok=True)
            except (OSError, RuntimeError):
                pass
        if self.journal.exists():
            self._save()

    def _save(self):
        from .computer_runtime import _atomic_write
        import json
        paths = [str(item["temporary"]) for item in self.handles.values() if item["mode"] == "write"]
        _atomic_write(self.journal, json.dumps({"temporary_files": paths}).encode("utf-8"))

    @staticmethod
    def fail(code, message):
        from .computer_runtime import RuntimeOperationError
        raise RuntimeOperationError(code, message)

    def _drop(self, key):
        item = self.handles.pop(key, None)
        if item:
            item["file"].close()
            if item["mode"] == "write":
                # A changed parent symlink must not turn cleanup into an
                # unlink outside the authorized root.
                try:
                    safe = self.runtime._safe_path(str(item["temporary"]), root=item["root"])
                    if safe == item["temporary"]:
                        safe.unlink(missing_ok=True)
                except (OSError, RuntimeError):
                    pass  # Never follow a moved parent outside the Workspace.
                self._save()

    def expire(self):
        with self.lock:
            for key, item in list(self.handles.items()):
                if item["deadline"] <= time.monotonic():
                    self._drop(key)

    def close(self):
        with self.lock:
            for key in list(self.handles):
                self._drop(key)

    @staticmethod
    def _signature(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def operate(self, data):
        with self.lock:
            return self._operate(data)

    def _operate(self, data):
        self.expire()
        action = str(data.get("action") or "")
        mode = "read" if action.startswith("read_") else "write"
        identity = str(data.get("run_id") or "")
        caller = str(data.get("caller_subject_hash") or "")
        if not identity or not caller:
            self.fail("WORKSPACE_TRANSFER_CONTEXT_REQUIRED", "Binary transfers require a caller-bound Run")
        if action in {"read_open", "write_open"}:
            if len(self.handles) >= self.MAX_ACTIVE:
                self.fail("WORKSPACE_TRANSFER_BUSY", "Computer file transfer limit reached")
            root = self.runtime._resolved_root(str(data.get("workspace_root") or ""))
            path = self.runtime._safe_path(str(data.get("path") or "."), root=root)
            key = uuid.uuid4().hex
            item = {"mode": mode, "run_id": identity, "caller": caller, "path": path,
                    "root": root, "deadline": time.monotonic() + self.TTL_SECONDS}
            if mode == "read":
                maximum = data.get("max_bytes", FILE_MAX_BYTES)
                if type(maximum) is not int or not 0 <= maximum <= FILE_MAX_BYTES:
                    self.fail("WORKSPACE_FILE_TOO_LARGE", "Invalid file transfer size limit")
                if not path.is_file():
                    self.fail("WORKSPACE_NOT_FOUND", "Workspace file was not found")
                handle = path.open("rb")
                try:
                    signature = self._signature(path.stat())
                    if self._signature(os.fstat(handle.fileno()))[:2] != signature[:2]:
                        self.fail("WORKSPACE_FILE_CHANGED", "Workspace file was replaced")
                    if signature[2] > maximum:
                        self.fail("WORKSPACE_FILE_TOO_LARGE", "Workspace file exceeds the allowed transfer size")
                    digest = hashlib.sha256()
                    remaining = signature[2]
                    while remaining:
                        chunk = handle.read(min(FILE_CHUNK_BYTES, remaining))
                        if not chunk:
                            self.fail("WORKSPACE_FILE_CHANGED", "Workspace file changed during transfer")
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if self._signature(path.stat()) != signature or handle.read(1):
                        self.fail("WORKSPACE_FILE_CHANGED", "Workspace file changed during transfer")
                    item.update(file=handle, signature=signature, size_bytes=signature[2], sha256=digest.hexdigest())
                except BaseException:
                    handle.close()
                    raise
            else:
                size = data.get("size_bytes")
                sha = str(data.get("sha256") or "")
                if type(size) is not int or not 0 <= size <= FILE_MAX_BYTES or not re.fullmatch(r"[0-9a-f]{64}", sha):
                    self.fail("WORKSPACE_TRANSFER_INVALID", "Invalid file size or SHA-256 digest")
                path.parent.mkdir(parents=True, exist_ok=True)
                # Revalidate after mkdir (including existing parent symlinks).
                path = self.runtime._safe_path(str(path), root=root)
                if sum(row["size_bytes"] for row in self.handles.values() if row["mode"] == "write") + size > 2 * FILE_MAX_BYTES:
                    self.fail("WORKSPACE_TRANSFER_BUSY", "Computer pending file transfer quota reached")
                if shutil.disk_usage(path.parent).free < size:
                    self.fail("WORKSPACE_STORAGE_FULL", "Computer does not have enough free disk space")
                temporary = path.with_name(".nexus-transfer-" + key)
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                item.update(path=path, file=os.fdopen(fd, "w+b"), temporary=temporary,
                            size_bytes=size, sha256=sha, offset=0, digest=hashlib.sha256(), last=None)
            self.handles[key] = item
            if mode == "write":
                try:
                    self._save()
                except BaseException:
                    self.handles.pop(key)
                    item["file"].close()
                    item["temporary"].unlink(missing_ok=True)
                    raise
            return {"transfer_id": key, "size_bytes": item["size_bytes"], "sha256": item["sha256"]}

        key = str(data.get("transfer_id") or "")
        item = self.handles.get(key)
        if not item or item["mode"] != mode or item["run_id"] != identity or item["caller"] != caller:
            self.fail("WORKSPACE_TRANSFER_NOT_FOUND", "File transfer was not found or expired")
        item["deadline"] = time.monotonic() + self.TTL_SECONDS
        if action in {"read_close", "write_abort"}:
            self._drop(key)
            return {"closed": True}
        offset = data.get("offset")
        if action == "read_chunk":
            path = self.runtime._safe_path(str(item["path"]), root=item["root"])
            if (not path.is_file() or self._signature(path.stat()) != item["signature"]
                    or self._signature(os.fstat(item["file"].fileno()))[:2] != item["signature"][:2]):
                self._drop(key)
                self.fail("WORKSPACE_FILE_CHANGED", "Workspace file changed during transfer")
            if type(offset) is not int or not 0 <= offset < item["size_bytes"]:
                self.fail("WORKSPACE_TRANSFER_INVALID", "Invalid file chunk offset")
            item["file"].seek(offset)
            raw = item["file"].read(min(FILE_CHUNK_BYTES, item["size_bytes"] - offset))
            from .computer_runtime import _upload_bytes
            upload = data.get("_nexus_upload") or {}
            uploaded = _upload_bytes(str(self.runtime.config.get("cloud_origin") or ""),
                str(upload.get("endpoint") or ""), token=str(upload.get("token") or ""),
                content=raw, content_type="application/octet-stream",
                ca_file=str(self.runtime.config.get("ca_file") or ""))
            return {"offset": offset, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                    "binary_upload": {"command_id": str(data.get("_nexus_command_id") or ""),
                                      "upload_id": uploaded["upload_id"]}}
        if action == "write_chunk":
            from .computer_runtime import _download_bytes
            download = data.get("_nexus_download") or {}
            raw = _download_bytes(str(self.runtime.config.get("cloud_origin") or ""),
                str(download.get("endpoint") or ""), token=str(download.get("token") or ""),
                ca_file=str(self.runtime.config.get("ca_file") or ""))
            sha = hashlib.sha256(raw).hexdigest()
            if not raw or len(raw) > FILE_CHUNK_BYTES or sha != data.get("sha256") or type(offset) is not int:
                self.fail("WORKSPACE_TRANSFER_INTEGRITY_FAILED", "File chunk failed integrity validation")
            if item["last"] == (offset, len(raw), sha):
                return {"offset": item["offset"]}  # Same chunk replay never writes twice.
            if offset != item["offset"] or offset + len(raw) > item["size_bytes"]:
                self.fail("WORKSPACE_TRANSFER_INVALID", "File chunks must be written in order")
            item["file"].write(raw)
            item["digest"].update(raw)
            item["offset"] += len(raw)
            item["last"] = (offset, len(raw), sha)
            return {"offset": item["offset"]}
        if action == "write_commit":
            if item["offset"] != item["size_bytes"] or item["digest"].hexdigest() != item["sha256"]:
                self._drop(key)
                self.fail("WORKSPACE_TRANSFER_INTEGRITY_FAILED", "File size or SHA-256 did not match; original file preserved")
            path = self.runtime._safe_path(str(item["path"]), root=item["root"])
            temporary = self.runtime._safe_path(str(item["temporary"]), root=item["root"])
            if temporary != item["temporary"] or path != item["path"] or temporary.is_symlink():
                self.fail("WORKSPACE_PATH_OUTSIDE_ROOT", "File transfer target changed")
            item["file"].flush()
            os.fsync(item["file"].fileno())
            if self._signature(temporary.stat())[:2] != self._signature(os.fstat(item["file"].fileno()))[:2]:
                self.fail("WORKSPACE_TRANSFER_INTEGRITY_FAILED", "Temporary file was replaced")
            # Verify bytes on disk as well as the incoming stream. Windows
            # caches timestamps differently for open descriptors and paths.
            item["file"].seek(0)
            actual = hashlib.sha256()
            remaining = item["size_bytes"]
            while remaining:
                chunk = item["file"].read(min(FILE_CHUNK_BYTES, remaining))
                if not chunk:
                    self.fail("WORKSPACE_TRANSFER_INTEGRITY_FAILED", "Temporary file was truncated")
                actual.update(chunk)
                remaining -= len(chunk)
            if actual.hexdigest() != item["sha256"] or item["file"].read(1):
                self.fail("WORKSPACE_TRANSFER_INTEGRITY_FAILED", "Temporary file integrity check failed")
            item["file"].close()
            os.replace(temporary, path)
            self.handles.pop(key)
            try:
                self._save()
            except (OSError, RuntimeError):
                pass  # Publication succeeded; stale cleanup metadata is safe.
            return {"path": str(path.relative_to(item["root"])).replace("\\", "/"),
                    "size_bytes": item["size_bytes"], "sha256": item["sha256"]}
        self.fail("COMPUTER_OPERATION_UNSUPPORTED", "Unsupported binary transfer action")
