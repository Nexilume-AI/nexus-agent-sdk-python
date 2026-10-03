"""Bounded, integrity-checked binary transfers to an Attached Computer."""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import stat
from pathlib import Path
import tempfile
from typing import Any, BinaryIO, Union
from urllib.parse import urlencode

FILE_CHUNK_BYTES = 256 * 1024
FILE_MAX_BYTES = 1024 * 1024 * 1024
BYTES_MAX_BYTES = 16 * 1024 * 1024


class BinaryWorkspaceMixin:
    def _transfer(self, action: str, **values: Any) -> dict:
        from .reporting import NexusComputerError

        scope = "files.read" if action.startswith("read_") else "files.write"
        self.context._require_workspace_capability(scope)
        if not self.context.computer_enabled or not self.context.workspace_url:
            raise NexusComputerError("Computer is not enabled for this run")
        if action in {"read_open", "write_open"}:
            # Older Clouds treat any POST as a text write. Negotiate through a
            # read-only request first, so they can never erase a binary target.
            query = urlencode({"operation": "binary_capabilities", "access": "read" if scope == "files.read" else "write"})
            try:
                capability = self.context._internal_request(self.context.workspace_url + "?" + query,
                    method="GET", request_timeout=self._REQUEST_TIMEOUT)
            except NexusComputerError as exc:
                if exc.code.startswith("COMPUTER_"):
                    raise
                raise NexusComputerError("Attached Computer binary transfers require an updated Cloud and Computer Runtime",
                    code="WORKSPACE_BINARY_UNSUPPORTED") from None
            if capability.get("binary_transfer_version") != 1:
                raise NexusComputerError("Attached Computer binary transfers require an updated Cloud and Computer Runtime",
                    code="WORKSPACE_BINARY_UNSUPPORTED")
        return self.context._internal_request(
            self.context.workspace_url, method="POST",
            payload={"operation": "binary_transfer", "action": action, **values},
            request_timeout=self._REQUEST_TIMEOUT,
        )

    @staticmethod
    def _integrity_error(message: str):
        from .reporting import NexusComputerError
        return NexusComputerError(message, code="WORKSPACE_TRANSFER_INTEGRITY_FAILED")

    def _download_to(self, path: str, output: BinaryIO, *, max_bytes: int) -> dict:
        opened = self._transfer("read_open", path=str(path), max_bytes=max_bytes)
        transfer_id = str(opened.get("transfer_id") or "")
        try:
            size = opened.get("size_bytes")
            if (not transfer_id or type(size) is not int or size < 0 or size > max_bytes
                    or not re.fullmatch(r"[0-9a-f]{64}", str(opened.get("sha256") or ""))):
                raise self._integrity_error("Computer returned an invalid file size")
            offset, digest = 0, hashlib.sha256()
            while offset < size:
                chunk = self._transfer("read_chunk", transfer_id=transfer_id, offset=offset)
                try:
                    raw = base64.b64decode(chunk["content_base64"], validate=True)
                except (ValueError, KeyError, TypeError):
                    raise self._integrity_error("Computer returned invalid binary data") from None
                if (not raw or len(raw) > FILE_CHUNK_BYTES or offset + len(raw) > size
                        or chunk.get("offset") != offset
                        or hashlib.sha256(raw).hexdigest() != chunk.get("sha256")):
                    raise self._integrity_error("Computer file chunk failed integrity validation")
                output.write(raw)
                digest.update(raw)
                offset += len(raw)
            if digest.hexdigest() != opened.get("sha256"):
                raise self._integrity_error("Computer file failed SHA-256 validation")
            return {"path": str(path), "size_bytes": size, "sha256": digest.hexdigest()}
        finally:
            if transfer_id:
                try:
                    self._transfer("read_close", transfer_id=transfer_id)
                except Exception:
                    pass  # The Runtime also expires abandoned handles.

    def read_bytes(self, path: str, *, max_bytes: int = BYTES_MAX_BYTES) -> bytes:
        """Read binary data into memory (at most 16 MiB); use download for larger files."""
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 <= max_bytes <= BYTES_MAX_BYTES:
            raise ValueError("max_bytes must be between 0 and 16 MiB; use download for larger files")
        output = io.BytesIO()
        self._download_to(path, output, max_bytes=max_bytes)
        return output.getvalue()

    def download(self, path: str, destination: Union[str, Path], *, max_bytes: int = FILE_MAX_BYTES) -> dict:
        """Stream to an Agent-local file, replacing it only after integrity validation."""
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 <= max_bytes <= FILE_MAX_BYTES:
            raise ValueError("max_bytes must be between 0 and 1 GiB")
        target = Path(destination).expanduser().absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".nexus-download-", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as output:
                result = self._download_to(path, output, max_bytes=max_bytes)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            return result
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _upload_from(self, source: BinaryIO, path: str, *, size: int, sha256: str) -> dict:
        opened = self._transfer("write_open", path=str(path), size_bytes=size, sha256=sha256)
        transfer_id = str(opened.get("transfer_id") or "")
        if not transfer_id:
            raise self._integrity_error("Computer did not create a file transfer")
        committed = False
        try:
            offset = 0
            while True:
                raw = source.read(FILE_CHUNK_BYTES)
                if not raw:
                    break
                if offset + len(raw) > size:
                    raise self._integrity_error("Source file changed during upload")
                result = self._transfer("write_chunk", transfer_id=transfer_id, offset=offset,
                    content_base64=base64.b64encode(raw).decode("ascii"),
                    sha256=hashlib.sha256(raw).hexdigest())
                offset += len(raw)
                if result.get("offset") != offset:
                    raise self._integrity_error("Computer did not acknowledge the complete file chunk")
            if offset != size:
                raise self._integrity_error("Source file changed during upload")
            result = self._transfer("write_commit", transfer_id=transfer_id)
            if result.get("sha256") != sha256 or result.get("size_bytes") != size:
                raise self._integrity_error("Computer returned an invalid committed file digest")
            committed = True
            return result
        finally:
            if not committed:
                try:
                    self._transfer("write_abort", transfer_id=transfer_id)
                except Exception:
                    pass

    def write_bytes(self, path: str, content: bytes) -> dict:
        """Atomically write binary data (at most 16 MiB); use upload for larger files."""
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise TypeError("content must be bytes, bytearray or memoryview")
        if (content.nbytes if isinstance(content, memoryview) else len(content)) > BYTES_MAX_BYTES:
            raise ValueError("Binary content exceeds 16 MiB; use upload")
        raw = bytes(content)
        return self._upload_from(io.BytesIO(raw), path, size=len(raw), sha256=hashlib.sha256(raw).hexdigest())

    def upload(self, source: Union[str, Path], path: str) -> dict:
        """Stream an Agent-local file to the Attached Computer (at most 1 GiB)."""
        with Path(source).expanduser().open("rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Upload source must be a regular file")
            size = metadata.st_size
            if size > FILE_MAX_BYTES:
                raise ValueError("File exceeds the 1 GiB transfer limit")
            digest = hashlib.sha256()
            remaining = size
            while remaining:
                chunk = handle.read(min(FILE_CHUNK_BYTES, remaining))
                if not chunk:
                    raise self._integrity_error("Source file changed during upload")
                digest.update(chunk)
                remaining -= len(chunk)
            if handle.read(1):
                raise self._integrity_error("Source file changed during upload")
            handle.seek(0)
            return self._upload_from(handle, path, size=size, sha256=digest.hexdigest())
