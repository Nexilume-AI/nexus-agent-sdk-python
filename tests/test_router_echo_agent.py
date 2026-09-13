"""Offline checks for the live Router echo Agent example."""

import importlib.util
from pathlib import Path
import unittest


EXAMPLE = Path(__file__).parents[1] / "examples" / "router_echo_agent.py"
SPEC = importlib.util.spec_from_file_location("router_echo_agent", EXAMPLE)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RouterEchoAgentTest(unittest.TestCase):
    def test_echo_returns_payload_unchanged(self) -> None:
        payload = {"message": "hello", "values": [1, 2, 3]}

        self.assertEqual(MODULE.echo(payload), payload)

    def test_default_router_matches_lan_sdk_listener(self) -> None:
        self.assertEqual(
            MODULE.DEFAULT_ROUTER_URL,
            "http://192.168.250.1:7446",
        )

    def test_echo_publishes_demo_descriptor_with_direct_ipv6_transport(self) -> None:
        agent = MODULE.build_agent(router=MODULE.DEFAULT_ROUTER_URL, auth="auto")

        self.assertTrue(agent.cloud_publish)
        capability = agent._specs["demo.echo"]
        self.assertTrue(capability.public_ipv6)
        self.assertEqual(capability.tool.name, "demo.echo")
        self.assertTrue(capability.tool.demo)
        self.assertEqual(capability.tool.input_schema["type"], "object")
        self.assertTrue(capability.tool.input_schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
