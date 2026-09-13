import asyncio
import hashlib
from io import BytesIO
import json
import pathlib
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from nexus_agent import NexusRunContext, NexusFileError


class GuardedResponse(BytesIO):
    def read(self, size=-1):
        assert 0 <= size <= 1024 * 1024, "unbounded read"
        return super().read(size)


class RunFileTests(unittest.TestCase):
    def setUp(self):
        self.ctx = NexusRunContext(run_id="run-files", events_url="", token="",
            display_asset_url="https://cloud.example/api/v1/internal/agent-runs/run-files/display-assets/",
            interaction_token="private-file-token")
        self.addCleanup(self.ctx.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file_id = str(uuid.uuid4())
        self.data = b"ab" * (1024 * 1024 + 17)
        self.info = {"file_id": self.file_id, "name": "file.bin", "size_bytes": len(self.data), "received_bytes": 0,
            "sha256": hashlib.sha256(self.data).hexdigest(), "state": "ready", "chunk_bytes": 1024 * 1024}
        self.requests = []

    def cloud(self, request, **kwargs):
        self.requests.append(request)
        self.assertEqual(request.get_header("X-nexus-interaction-token"), "private-file-token")
        self.assertTrue(request.full_url.startswith("https://cloud.example/api/v1/internal/agent-runs/run-files/files/"))
        if "?download=1" in request.full_url:
            offset = int(request.get_header("Range", "bytes=0-")[6:-1])
            response = GuardedResponse(self.data[offset:])
            response.status = 206 if offset else 200
            response.headers = {"Content-Range": f"bytes {offset}-{len(self.data)-1}/{len(self.data)}"}
            return response
        if request.method == "PUT":
            self.assertLessEqual(len(request.data), 1024 * 1024)
            offset = int(request.get_header("X-nexus-upload-offset"))
            self.assertEqual(request.data, self.data[offset:offset+len(request.data)])
            self.assertEqual(request.get_header("X-nexus-chunk-sha256"), hashlib.sha256(request.data).hexdigest())
            self.info["received_bytes"] = offset + len(request.data)
        return GuardedResponse(json.dumps({"ok": True, "data": self.info}).encode())

    def test_download_resume_hash_and_bounded_cloud_reads(self):
        destination = pathlib.Path(self.temp.name) / "download.bin"
        partial = destination.with_name(destination.name + "." + self.file_id + ".part")
        partial.write_bytes(self.data[:123])
        with patch.object(self.ctx, "_open_cloud", side_effect=self.cloud):
            self.ctx.files.download({"file_id": self.file_id, "url": "https://evil.example/"}, destination)
        self.assertEqual(destination.read_bytes(), self.data)
        self.assertEqual(self.requests[-1].get_header("Range"), "bytes=123-")
        self.assertFalse(partial.exists())

    def test_bad_hash_does_not_publish_and_existing_destination_not_overwritten(self):
        destination = pathlib.Path(self.temp.name) / "download.bin"
        self.info["sha256"] = "0" * 64
        with patch.object(self.ctx, "_open_cloud", side_effect=self.cloud), self.assertRaises(NexusFileError):
            self.ctx.files.download(self.file_id, destination)
        self.assertFalse(destination.exists())
        destination.write_bytes(b"existing")
        with patch.object(self.ctx, "_open_cloud", side_effect=self.cloud), self.assertRaises(FileExistsError):
            self.ctx.files.download(self.file_id, destination)
        self.assertEqual(destination.read_bytes(), b"existing")

    def test_output_upload_chunks_progress_and_alias(self):
        source = pathlib.Path(self.temp.name) / "file.bin"
        source.write_bytes(self.data)
        progress = []
        with patch.object(self.ctx, "_open_cloud", side_effect=self.cloud):
            result = self.ctx.output.upload_file(source, progress=lambda sent,total: progress.append((sent,total)))
        self.assertEqual(result["file_id"], self.file_id)
        self.assertEqual(progress[-1], (len(self.data), len(self.data)))
        self.assertEqual(len([request for request in self.requests if request.method == "PUT"]), 3)

    def test_output_resume_only_sends_remaining_chunks(self):
        source = pathlib.Path(self.temp.name) / "file.bin"
        source.write_bytes(self.data)
        self.info["received_bytes"] = 1024 * 1024
        with patch.object(self.ctx, "_open_cloud", side_effect=self.cloud):
            asyncio.run(self.ctx.aio.files.upload(source, resume_id=self.file_id))
        puts = [request for request in self.requests if request.method == "PUT"]
        self.assertEqual(len(puts), 2)
        self.assertEqual(puts[0].get_header("X-nexus-upload-offset"), "1048576")

    def test_managed_output_create_uses_the_operation_idempotency_key(self):
        source = pathlib.Path(self.temp.name) / "file.bin"
        source.write_bytes(self.data)
        self.ctx.recovery_url = "https://cloud.example/operations/"
        self.ctx.recovery.managed = True
        prepared = {
            "id": str(uuid.uuid4()),
            "status": "running",
            "execute": True,
            "result": None,
            "idempotency_key": "nxo_run_file_create",
        }
        with (
            patch.object(self.ctx, "_open_cloud", side_effect=self.cloud),
            patch.object(
                self.ctx,
                "_interaction_request_url",
                side_effect=[prepared, {"status": "succeeded"}],
            ),
        ):
            self.ctx.files.upload(source)
        create_request = next(request for request in self.requests if request.method == "POST" and request.data)
        self.assertEqual(json.loads(create_request.data)["idempotency_key"], "nxo_run_file_create")

    def test_transport_error_has_safe_resume_id_not_token(self):
        with patch.object(self.ctx, "_open_cloud", side_effect=OSError("private-file-token")), patch("nexus_agent.files.time.sleep"):
            with self.assertRaises(NexusFileError) as caught:
                self.ctx.files._request(self.file_id)
        self.assertEqual(caught.exception.file_id, self.file_id)
        self.assertNotIn("private-file-token", repr(caught.exception))

    def test_no_hosted_context_and_arbitrary_urls_rejected_without_request(self):
        with patch.object(self.ctx, "_open_cloud") as opener:
            with self.assertRaises(ValueError):
                list(self.ctx.files.iter_bytes("https://evil.example/"))
            self.ctx.display_asset_url = ""
            with self.assertRaises(NexusFileError):
                self.ctx.files.list()
            opener.assert_not_called()
