"""Private file data plane. All I/O is bounded; tokens never enter references."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request
from urllib.parse import urljoin
import uuid


class NexusFileError(RuntimeError):
    def __init__(self, message, *, file_id=None):
        super().__init__(message)
        self.file_id = file_id


class RunFiles:
    def __init__(self, context):
        self.context = context

    def _url(self, file_id=""):
        if not self.context.display_asset_url or not self.context._interaction_token:
            raise NexusFileError("Run file access is unavailable outside a hosted invocation")
        suffix = str(uuid.UUID(str(file_id))) + "/" if file_id else ""
        return urljoin(self.context.display_asset_url.rstrip("/") + "/", "../files/" + suffix)

    def _request(self, file_id="", *, method="GET", payload=None, body=None, headers=None):
        data = body if body is not None else json.dumps(payload).encode() if payload is not None else None
        request = Request(self._url(file_id), data=data, method=method, headers={
            "X-Nexus-Interaction-Token": self.context._interaction_token,
            "Content-Type": "application/octet-stream" if body is not None else "application/json", **(headers or {})})
        attempts = 1 if method == "POST" and not file_id else 3
        for attempt in range(attempts):
            try:
                with self.context._open_cloud(request, timeout=60) as response:
                    value = json.loads(response.read(256 * 1024))
                if isinstance(value, dict) and "ok" in value:
                    if not value["ok"]:
                        raise NexusFileError("Run file request was rejected")
                    return value["data"]
                return value
            except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                if attempt == attempts - 1:
                    raise NexusFileError("Run file transfer failed or access was rejected; use exception.file_id to resume", file_id=file_id or None) from None
                time.sleep(0.25 * 2**attempt)

    def list(self):
        """List the current Run's input references (no file bytes)."""
        return self._request()

    def iter_bytes(self, reference, *, offset=0, chunk_size=256 * 1024):
        """Read a private input/output with bounded chunks. Caller owns iteration."""
        file_id = reference["file_id"] if isinstance(reference, dict) else reference
        if not 0 < chunk_size <= 1024 * 1024 or offset < 0:
            raise ValueError("Invalid file range or chunk size")
        headers = {"X-Nexus-Interaction-Token": self.context._interaction_token}
        if offset:
            headers["Range"] = "bytes=%d-" % offset
        request = Request(self._url(file_id) + "?download=1", headers=headers)
        try:
            with self.context._open_cloud(request, timeout=60) as response:
                if offset and (response.status != 206 or not response.headers.get("Content-Range", "").startswith("bytes %d-" % offset)):
                    raise NexusFileError("Server did not honor the requested resume offset")
                while True:
                    data = response.read(chunk_size)
                    if not data:
                        break
                    yield data
        except (HTTPError, URLError, TimeoutError, OSError):
            raise NexusFileError("Run file download interrupted or access was rejected") from None

    def download(self, reference, destination, *, resume=True, overwrite=False, progress=None):
        """Download to a file, verify SHA-256, then atomically publish the result."""
        file_id = reference["file_id"] if isinstance(reference, dict) else reference
        info = self._request(file_id)
        if info["state"] != "ready":
            raise NexusFileError("Run file is not ready")
        path = Path(destination)
        if path.exists() and not overwrite:
            raise FileExistsError("Download destination already exists")
        part = path.with_name(path.name + "." + str(uuid.UUID(str(file_id))) + ".part")
        path.parent.mkdir(parents=True, exist_ok=True)
        if part.is_symlink():
            raise NexusFileError("Download staging path must not be a symlink")
        offset = part.stat().st_size if resume and part.exists() else 0
        if offset > info["size_bytes"]:
            raise NexusFileError("Partial download exceeds the expected file size")
        with part.open("ab" if offset else "wb") as stream:
            if offset < info["size_bytes"]:
                for data in self.iter_bytes(info, offset=offset):
                    stream.write(data)
                    offset += len(data)
                    if offset > info["size_bytes"]:
                        raise NexusFileError("File exceeds its declared size")
                    if progress:
                        progress(offset, info["size_bytes"])
        digest = hashlib.sha256()
        with part.open("rb") as stream:
            for data in iter(lambda: stream.read(256 * 1024), b""):
                digest.update(data)
        if offset != info["size_bytes"] or digest.hexdigest() != info["sha256"]:
            raise NexusFileError("File integrity verification failed; final destination was not replaced")
        if overwrite:
            os.replace(str(part), str(path))
        else:
            os.link(str(part), str(path))  # exclusive publication, never overwrite in a race
            part.unlink()
        return path

    def upload(self, path, *, content_type="application/octet-stream", resume_id=None, progress=None, timeout=1800):
        """Upload an output in idempotent chunks; return its immutable reference.

        Supply resume_id after a process restart to continue a prior upload in
        the same active Run. The source file must not change during upload.
        """
        path = Path(path)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for data in iter(lambda: stream.read(256 * 1024), b""):
                digest.update(data)
        size, sha = path.stat().st_size, digest.hexdigest()
        create_payload = {
            "name": path.name,
            "size_bytes": size,
            "content_type": content_type,
            "sha256": sha,
        }
        if resume_id:
            info = self._request(resume_id)
        elif self.context.recovery.managed:
            info = self.context.recovery.call(
                "run_file.create",
                create_payload,
                lambda idempotency_key: self._request(
                    method="POST",
                    payload={**create_payload, "idempotency_key": idempotency_key},
                ),
                can_reconcile=True,
            )
        else:
            info = self._request(method="POST", payload=create_payload)
        if info["size_bytes"] != size or info["name"] != path.name:
            raise NexusFileError("Resume reference does not match this file")
        file_id = info["file_id"]
        with path.open("rb") as stream:
            stream.seek(info["received_bytes"])
            while info["received_bytes"] < size:
                offset = info["received_bytes"]
                data = stream.read(info["chunk_bytes"])
                if not data:
                    raise NexusFileError("Source file changed during upload")
                info = self._request(file_id, method="PUT", body=data, headers={
                    "X-Nexus-Upload-Offset": str(offset), "X-Nexus-Chunk-SHA256": hashlib.sha256(data).hexdigest()})
                if progress:
                    progress(info["received_bytes"], size)
        info = self._request(file_id, method="POST", payload={})
        deadline = time.monotonic() + timeout
        while info["state"] in {"queued", "processing"} and time.monotonic() < deadline:
            time.sleep(1)
            info = self._request(file_id)
        if info["state"] != "ready":
            raise NexusFileError("File finalization incomplete; resume with file_id=" + file_id, file_id=file_id)
        return info


class AsyncRunFiles:
    def __init__(self, files):
        self.files = files

    async def list(self):
        return await asyncio.to_thread(self.files.list)

    async def download(self, reference, destination, **kwargs):
        return await asyncio.to_thread(self.files.download, reference, destination, **kwargs)

    async def upload(self, path, **kwargs):
        return await asyncio.to_thread(self.files.upload, path, **kwargs)
