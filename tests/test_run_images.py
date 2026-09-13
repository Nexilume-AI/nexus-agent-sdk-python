import asyncio
from io import BytesIO
import pathlib
import sys
import unittest
import uuid
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from nexus_agent import NexusRunContext, NexusImageReference, NexusChatUnavailable


class RunImageTests(unittest.TestCase):
    def context(self):
        ctx = NexusRunContext(run_id="run-image", events_url="", token="",
            display_asset_url="https://cloud.example/api/v1/internal/agent-runs/run-image/display-assets/",
            interaction_token="private-run-token")
        self.addCleanup(ctx.close)
        return ctx

    def response(self, data=b"image-bytes", mime="image/png"):
        response = BytesIO(data)
        response.headers = {"Content-Type": mime}
        return response

    def test_read_uses_run_token_and_shared_cloud_tls_opener(self):
        ctx = self.context()
        ref = NexusImageReference(asset_id=str(uuid.uuid4()), content_type="image/png")
        with patch.object(ctx, "_open_cloud", return_value=self.response()) as opener:
            self.assertEqual(ctx.media.read_image(ref), b"image-bytes")
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, ctx.display_asset_url + ref["asset_id"] + "/")
        self.assertEqual(request.get_header("X-nexus-interaction-token"), "private-run-token")

    def test_arbitrary_urls_are_not_fetched(self):
        ctx = self.context()
        with patch.object(ctx, "_open_cloud") as opener:
            for ref in ({}, {"asset_id": "../other-run"}, {"asset_id": "https://private.example/"}):
                with self.assertRaises(ValueError):
                    ctx.media.read_image(ref)
            opener.assert_not_called()

    def test_read_bounds_mime_and_redacts_failure(self):
        ctx = self.context()
        ref = {"asset_id": str(uuid.uuid4())}
        for data, mime in [(b"<svg/>", "image/svg+xml"), (b"x" * (2 * 1024 * 1024 + 1), "image/png"), (b"", "image/png")]:
            with patch.object(ctx, "_open_cloud", return_value=self.response(data, mime)), self.assertRaises(NexusChatUnavailable):
                ctx.media.read_image(ref)
        with patch.object(ctx, "_open_cloud", side_effect=OSError("secret private-run-token")):
            with self.assertRaises(NexusChatUnavailable) as caught:
                ctx.media.read_image(ref)
            self.assertNotIn("private-run-token", str(caught.exception))

    def test_output_is_small_protected_reference_not_base64(self):
        ctx = self.context()
        asset_id = str(uuid.uuid4())
        with patch.object(ctx, "_upload_display_asset", return_value={"id": asset_id}), patch.object(ctx, "emit") as emit:
            result = ctx.output.image(b"image-bytes", title="Result")
        self.assertEqual(result["asset_id"], asset_id)
        self.assertEqual(emit.call_args.args[0]["name"], "nexus.image.created")
        self.assertNotIn("data", result)
        with self.assertRaises(ValueError):
            ctx.output.image(b"x" * (2 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            ctx.output.image(b"<svg/>", content_type="image/svg+xml")

    def test_async_helpers_and_legacy_cloud(self):
        ctx = self.context()
        with patch.object(ctx, "_open_cloud", return_value=self.response()):
            self.assertEqual(asyncio.run(ctx.aio.media.read_image({"asset_id": str(uuid.uuid4())})), b"image-bytes")
        with patch.object(ctx.output, "image", return_value={"type": "image"}) as upload:
            self.assertEqual(asyncio.run(ctx.aio.output.image(b"image")), {"type": "image"})
            upload.assert_called_once()
        ctx.display_asset_url = ""
        with self.assertRaises(NexusChatUnavailable):
            ctx.media.read_image({"asset_id": str(uuid.uuid4())})
