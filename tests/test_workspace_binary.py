"""Real local files and HTTP asset transport; not an Attached hardware E2E."""
import asyncio
import base64
import hashlib
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nexus_agent.computer_runtime import NexusComputerRuntime, RuntimeOperationError, NexusComputerRuntimeError, _download_bytes, _upload_bytes
from nexus_agent.reporting import NexusRunContext, NexusComputerError
from nexus_agent.workspace_files import FILE_CHUNK_BYTES


class BinaryWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.workspace = self.root / "computer"
        self.workspace.mkdir()
        self.runtime = NexusComputerRuntime(self.root / "runtime")
        self.assets, self.frames = {}, []
        self.fail_chunk = False
        self.corrupt_chunk = False
        self.old_cloud = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def response(self, result, status=200):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())

            def do_GET(self):
                if self.path.endswith("/redirect/"):
                    self.send_response(302)
                    self.send_header("Location", "/outside-runtime")
                    self.end_headers()
                    return
                if self.path.startswith("/workspace/"):
                    self.response({"items": []} if owner.old_cloud else {"binary_transfer_version": 1})
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(owner.assets[self.path])

            def do_PUT(self):
                if self.path.endswith("/redirect/"):
                    self.send_response(307)
                    self.send_header("Location", "/outside-runtime")
                    self.end_headers()
                    return
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                owner.assets[self.path] = raw
                self.response({"upload_id": self.path, "sha256": hashlib.sha256(raw).hexdigest()})

            def do_POST(self):
                owner.frames.append(dict(self.headers))
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                action = data["action"]
                if action == "write_chunk" and owner.fail_chunk:
                    self.response({"error": {"message": "Transfer interrupted"}}, 503)
                    return
                command = uuid.uuid4().hex
                endpoint = "/api/v1/computer-runtime/v1/commands/" + command
                if "content_base64" in data:
                    owner.assets[endpoint + "/download/"] = base64.b64decode(data.pop("content_base64"))
                data.update(run_id="run-a", caller_subject_hash="caller-a", workspace_root=str(owner.workspace),
                    _nexus_upload={"endpoint": endpoint + "/upload/", "token": "asset-token"},
                    _nexus_download={"endpoint": endpoint + "/download/", "token": "asset-token"},
                    _nexus_command_id=command)
                try:
                    result = owner.runtime.dispatch("workspace.transfer_read" if action.startswith("read_")
                        else "workspace.transfer_write", "files.read" if action.startswith("read_") else "files.write", data)
                    if "binary_upload" in result:
                        raw = owner.assets.pop(result.pop("binary_upload")["upload_id"])
                        if owner.corrupt_chunk:
                            raw = bytes([raw[0] ^ 255]) + raw[1:]
                        result["content_base64"] = base64.b64encode(raw).decode()
                    self.response(result)
                except RuntimeOperationError as exc:
                    self.response({"error": {"code": exc.code, "message": str(exc)}, "detail": str(exc)}, 400)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.runtime.close)
        origin = f"http://127.0.0.1:{self.server.server_port}"
        self.runtime.config = {"workspace_root": str(self.workspace), "cloud_origin": origin}
        self.ctx = NexusRunContext(run_id="run-a", events_url="", token="", computer_enabled=True,
            workspace_url=origin + "/workspace/", workspace_token="delegate-token")

    def test_sync_binary_empty_non_utf8_unicode_and_multiple_chunks(self):
        for raw in (b"", b"\x00\xff\x80\r\n", os.urandom(FILE_CHUNK_BYTES * 3 + 1)):
            result = self.ctx.workspace.write_bytes("文件/image.bin", raw)
            self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual((self.workspace / "文件/image.bin").read_bytes(), raw)
            self.assertEqual(self.ctx.workspace.read_bytes("文件/image.bin"), raw)
        self.assertFalse(self.runtime._file_transfers.handles)
        self.assertTrue(all(row.get("X-Nexus-Workspace-Token") == "delegate-token" for row in self.frames))

    def test_same_live_probe_handles_rejected_commit_and_async_transfers(self):
        source = Path(__file__).resolve().parents[1] / "examples/workspace_binary_probe.py"
        spec = importlib.util.spec_from_file_location("binary_acceptance_example", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.binary_probe({"marker": "regression"}, self.ctx)
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["async_roundtrip"])
        self.assertTrue(result["atomic_original_preserved"])
        self.assertEqual(len(result["rejected"]), 4)
        self.assertFalse(self.runtime._file_transfers.handles)
        self.assertFalse(list(self.workspace.rglob(".nexus-transfer-*")))

    def test_upload_download_stream_without_unbounded_file_reads(self):
        source, target = self.root / "source.zip", self.root / "download.zip"
        raw = os.urandom(FILE_CHUNK_BYTES * 5 + 23)
        source.write_bytes(raw)
        target.write_bytes(b"previous")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("Unbounded read")):
            self.ctx.workspace.upload(source, "large.zip")
            result = self.ctx.workspace.download("large.zip", target)
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(result["size_bytes"], len(raw))
        self.assertFalse(list(self.root.glob(".nexus-download-*")))

    def test_async_counterparts(self):
        async def exercise():
            await self.ctx.aio.workspace.write_bytes("async.bin", b"\x00\xff")
            self.assertEqual(await self.ctx.aio.workspace.read_bytes("async.bin"), b"\x00\xff")
            await self.ctx.aio.workspace.download("async.bin", self.root / "download.bin")
            await self.ctx.aio.workspace.upload(self.root / "download.bin", "again.bin")
        asyncio.run(exercise())
        self.assertEqual((self.workspace / "again.bin").read_bytes(), b"\x00\xff")

    def test_interrupted_upload_keeps_original_and_removes_temporary(self):
        target = self.workspace / "existing.bin"
        target.write_bytes(b"original")
        self.fail_chunk = True
        with self.assertRaises(NexusComputerError):
            self.ctx.workspace.write_bytes("existing.bin", b"replacement")
        self.assertEqual(target.read_bytes(), b"original")
        self.assertFalse(list(self.workspace.glob(".nexus-transfer-*")))

    def test_corrupt_download_does_not_replace_existing_local_file(self):
        (self.workspace / "source.bin").write_bytes(b"original")
        target = self.root / "existing.bin"
        target.write_bytes(b"preserve")
        self.corrupt_chunk = True
        with self.assertRaises(NexusComputerError) as error:
            self.ctx.workspace.download("source.bin", target)
        self.assertEqual(error.exception.code, "WORKSPACE_TRANSFER_INTEGRITY_FAILED")
        self.assertEqual(target.read_bytes(), b"preserve")

    def operate(self, action, **values):
        return self.runtime.dispatch("workspace.transfer_read" if action.startswith("read_") else "workspace.transfer_write",
            "files.read" if action.startswith("read_") else "files.write",
            {"action": action, "run_id": "run-a", "caller_subject_hash": "caller-a",
             "workspace_root": str(self.workspace), **values})

    def test_path_escape_scope_and_cross_run_handles_are_denied(self):
        with self.assertRaises(RuntimeOperationError):
            self.operate("write_open", path="../escape.bin", size_bytes=0, sha256=hashlib.sha256(b"").hexdigest())
        with self.assertRaises(RuntimeOperationError):
            self.runtime.dispatch("workspace.transfer_write", "files.read", {"action": "write_open"})
        opened = self.operate("write_open", path="safe.bin", size_bytes=0, sha256=hashlib.sha256(b"").hexdigest())
        for values in ({"run_id": "run-b"}, {"caller_subject_hash": "caller-b"}):
            with self.assertRaises(RuntimeOperationError):
                self.operate("write_commit", transfer_id=opened["transfer_id"], **values)
        self.operate("write_abort", transfer_id=opened["transfer_id"])

    def test_incomplete_digest_expiry_and_restart_cleanup(self):
        (self.workspace / "safe.bin").write_bytes(b"preserve")
        opened = self.operate("write_open", path="safe.bin", size_bytes=1, sha256=hashlib.sha256(b"a").hexdigest())
        with self.assertRaises(RuntimeOperationError):
            self.operate("write_commit", transfer_id=opened["transfer_id"])
        self.assertEqual((self.workspace / "safe.bin").read_bytes(), b"preserve")
        opened = self.operate("write_open", path="safe.bin", size_bytes=1, sha256=hashlib.sha256(b"a").hexdigest())
        self.runtime._file_transfers.handles[opened["transfer_id"]]["deadline"] = time.monotonic() - 1
        self.runtime._file_transfers.expire()
        self.assertFalse(list(self.workspace.glob(".nexus-transfer-*")))
        self.operate("write_open", path="safe.bin", size_bytes=1, sha256=hashlib.sha256(b"a").hexdigest())
        # Simulate process exit without normal shutdown, then startup with persisted config.
        for item in self.runtime._file_transfers.handles.values():
            item["file"].close()
        self.runtime._file_transfers.handles.clear()
        self.runtime.config_path.write_text(json.dumps(self.runtime.config))
        restarted = NexusComputerRuntime(self.runtime.root)
        restarted.close()
        self.assertFalse(list(self.workspace.glob(".nexus-transfer-*")))

    def test_source_change_size_limits_and_symlink_escape(self):
        (self.workspace / "source.bin").write_bytes(b"first")
        opened = self.operate("read_open", path="source.bin", max_bytes=16)
        (self.workspace / "source.bin").write_bytes(b"second")
        with self.assertRaises(RuntimeOperationError):
            self.operate("read_chunk", transfer_id=opened["transfer_id"], offset=0)
        with self.assertRaises(ValueError):
            self.ctx.workspace.read_bytes("source.bin", max_bytes=17 * 1024 * 1024)
        with self.assertRaises(TypeError):
            self.ctx.workspace.write_bytes("source.bin", "not bytes")
        try:
            (self.workspace / "link").symlink_to(self.root, target_is_directory=True)
        except OSError:
            return  # Windows needs symlink privilege; POSIX exercises this guard.
        with self.assertRaises(RuntimeOperationError):
            self.operate("read_open", path="link/outside", max_bytes=16)

    def test_old_cloud_never_receives_a_write_and_cloud_opener_is_reused(self):
        (self.workspace / "existing.bin").write_bytes(b"preserve")
        self.old_cloud = True
        with self.assertRaises(NexusComputerError) as error:
            self.ctx.workspace.write_bytes("existing.bin", b"new")
        self.assertEqual(error.exception.code, "WORKSPACE_BINARY_UNSUPPORTED")
        self.assertFalse(self.frames)
        self.assertEqual((self.workspace / "existing.bin").read_bytes(), b"preserve")
        self.old_cloud = False
        import urllib.request
        seen = []
        def cloud_opener(request, *, timeout, exchange=False):
            seen.append((request.full_url, exchange, request.get_header("Authorization")))
            return urllib.request.urlopen(request, timeout=timeout)
        self.ctx._cloud_opener = cloud_opener
        self.ctx.workspace.write_bytes("tls-path.bin", b"new")
        self.assertEqual(self.ctx.workspace.read_bytes("tls-path.bin"), b"new")
        self.assertTrue(seen)
        self.assertTrue(all(not exchange and auth == "Bearer delegate-token" for _, exchange, auth in seen))

    def test_chunk_replay_and_hash_rejection_preserve_file(self):
        raw = b"chunk"
        sha = hashlib.sha256(raw).hexdigest()
        opened = self.operate("write_open", path="replay.bin", size_bytes=len(raw), sha256=sha)
        with patch("nexus_agent.computer_runtime._download_bytes", return_value=raw):
            values = dict(transfer_id=opened["transfer_id"], offset=0, sha256=sha)
            self.assertEqual(self.operate("write_chunk", **values)["offset"], len(raw))
            self.assertEqual(self.operate("write_chunk", **values)["offset"], len(raw))
            with self.assertRaises(RuntimeOperationError):
                self.operate("write_chunk", **{**values, "sha256": "0" * 64})
        self.operate("write_commit", transfer_id=opened["transfer_id"])
        self.assertEqual((self.workspace / "replay.bin").read_bytes(), raw)

    def test_asset_redirects_and_non_cloud_endpoints_are_rejected(self):
        origin = self.runtime.config["cloud_origin"]
        endpoint = "/api/v1/computer-runtime/v1/commands/test/redirect/"
        with self.assertRaises(NexusComputerRuntimeError):
            _download_bytes(origin, endpoint, token="asset-token")
        with self.assertRaises(NexusComputerRuntimeError):
            _upload_bytes(origin, endpoint, token="asset-token", content=b"private", content_type="application/octet-stream")
        with self.assertRaises(NexusComputerRuntimeError):
            _download_bytes(origin, "https://untrusted.example/download", token="asset-token")
