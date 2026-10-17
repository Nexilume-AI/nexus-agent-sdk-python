import io
import json
import pathlib
import sys
import unittest
from unittest import mock
from urllib.request import Request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from nexus_agent.reporting import NexusReportingConfig, NexusRunContext


class RunContextUserAgentTest(unittest.TestCase):
    def test_exchange_identifies_sdk_with_and_without_trust_opener(self):
        for trusted in (False, True):
            with self.subTest(trusted=trusted):
                opener = mock.Mock(return_value=io.BytesIO(json.dumps({"context": {}}).encode()))
                context = mock.Mock(enabled=True)
                with mock.patch.object(NexusRunContext, "from_env", return_value=context), mock.patch(
                    "nexus_agent.reporting.urlopen", opener
                ):
                    self.assertIs(NexusRunContext.from_exchange(
                        "https://cloud.example.test/exchange/", "single-use-token",
                        cloud_opener=opener if trusted else None,
                    ), context)
                request = opener.call_args.args[0]
                self.assertTrue(request.get_header("User-agent").startswith("nexus-agent-sdk-python/"))
                self.assertEqual(request.get_header("X-nexus-run-context-token"), "single-use-token")
                self.assertEqual(request.get_method(), "POST")

    def test_followup_requests_identify_sdk_and_preserve_custom_agent(self):
        for trusted in (False, True):
            for custom in (None, "custom-nexus-client/1"):
                with self.subTest(trusted=trusted, custom=custom):
                    opener = mock.Mock()
                    context = object.__new__(NexusRunContext)
                    context.config = NexusReportingConfig()
                    context._cloud_opener = opener if trusted else None
                    request = Request("https://cloud.example.test/events/", headers={"User-Agent": custom} if custom else {})
                    with mock.patch("nexus_agent.reporting.urlopen", opener):
                        context._open_cloud(request)
                    sent = opener.call_args.args[0].get_header("User-agent")
                    if custom:
                        self.assertEqual(sent, custom)
                    else:
                        self.assertTrue(sent.startswith("nexus-agent-sdk-python/"))


if __name__ == "__main__":
    unittest.main()
