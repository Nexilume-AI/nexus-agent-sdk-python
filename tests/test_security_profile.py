import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

SDK_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from nexus_agent import (  # noqa: E402
    DirectIPv6Agent,
    NexusSecurityConfigurationError,
    NexusSecurityProfile,
    install_descriptor,
)


class SecurityProfileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nexus-security-profile-")
        self.root = pathlib.Path(self.temp.name)
        for name in ("ca.crt", "caller.crt", "caller.key"):
            (self.root / name).write_text(f"test {name}\n", encoding="ascii")

    def tearDown(self):
        self.temp.cleanup()

    def payload(self):
        return {
            "version": 1,
            "caller_identity": {
                "cert_file": "caller.crt",
                "key_file": "caller.key",
            },
            "trust_bundles": {
                "corp-ca": {"ca_file": "ca.crt"},
            },
            "routes": [
                {
                    "ipv6_prefix": "2001:db8::/32",
                    "port": 7443,
                    "tls_server_name": "edge.example.test",
                    "ca_bundle_id": "corp-ca",
                },
                {
                    "ipv6_prefix": "2001:db8:1200::/48",
                    "port": 8443,
                    "tls_server_name": "router-12.example.test",
                    "ca_bundle_id": "corp-ca",
                },
            ],
        }

    def test_longest_prefix_selects_router_security_binding(self):
        profile = NexusSecurityProfile.from_dict(
            self.payload(), profile_directory=self.root
        )
        specific = profile.resolve("2001:db8:1200::42")
        broad = profile.resolve("2001:db8:9999::42")
        self.assertEqual(specific.ipv6_prefix, "2001:db8:1200::/48")
        self.assertEqual(specific.port, 8443)
        self.assertEqual(specific.tls_server_name, "router-12.example.test")
        self.assertEqual(broad.ipv6_prefix, "2001:db8::/32")
        self.assertEqual(pathlib.Path(specific.ca_file), self.root / "ca.crt")

    def test_environment_profile_enables_address_and_jwt_only_constructor(self):
        profile_file = self.root / "security.json"
        profile_file.write_text(json.dumps(self.payload()), encoding="utf-8")
        with mock.patch.dict(
            os.environ, {"NEXUS_AGENT_SECURITY_PROFILE": str(profile_file)}
        ), mock.patch("nexus_agent.direct_ipv6.NexusAgentClient") as client_class:
            target = DirectIPv6Agent("2001:db8:1200::42", token="invoke-jwt")
        self.assertEqual(target.port, 8443)
        self.assertEqual(target.server_identity, "router-12.example.test")
        client_class.assert_called_once_with(
            "https://[2001:db8:1200::42]:8443",
            token="invoke-jwt",
            token_provider=None,
            transaction_token=None,
            ca_file=str(self.root / "ca.crt"),
            cert_file=str(self.root / "caller.crt"),
            key_file=str(self.root / "caller.key"),
            tls_server_name="router-12.example.test",
            use_environment_proxy=False,
            timeout=10.0,
        )

    def test_missing_route_has_actionable_error(self):
        profile = NexusSecurityProfile.from_dict(
            self.payload(), profile_directory=self.root
        )
        with self.assertRaisesRegex(
            NexusSecurityConfigurationError, "no security route matches"
        ):
            profile.resolve("2001:db9::1")

    def test_duplicate_prefix_and_unknown_bundle_are_rejected(self):
        duplicate = self.payload()
        duplicate["routes"].append(dict(duplicate["routes"][0]))
        with self.assertRaisesRegex(NexusSecurityConfigurationError, "duplicate"):
            NexusSecurityProfile.from_dict(duplicate, profile_directory=self.root)

        unknown = self.payload()
        unknown["routes"][0]["ca_bundle_id"] = "missing"
        with self.assertRaisesRegex(NexusSecurityConfigurationError, "unknown trust bundle"):
            NexusSecurityProfile.from_dict(unknown, profile_directory=self.root)

    def test_no_installed_profile_fails_closed(self):
        missing = self.root / "missing.json"
        with self.assertRaisesRegex(
            NexusSecurityConfigurationError, "no local Nexus security profile"
        ):
            NexusSecurityProfile.load(missing)

    def test_descriptor_installer_creates_and_merges_profile(self):
        output = self.root / "config" / "security.json"
        first = self.root / "first.json"
        first.write_text(json.dumps({
            "version": 1,
            "address": "2001:db8:1200::11",
            "ipv6_prefix": "2001:db8:1200::/48",
            "port": 7443,
            "tls_server_name": "router-12.example.test",
            "ca_bundle_id": "corp-ca",
        }), encoding="utf-8")
        result = install_descriptor(
            first,
            ca_file=self.root / "ca.crt",
            client_cert_file=self.root / "caller.crt",
            client_key_file=self.root / "caller.key",
            output_file=output,
        )
        self.assertEqual(result, output)
        self.assertEqual(
            NexusSecurityProfile.load(output).resolve("2001:db8:1200::99").tls_server_name,
            "router-12.example.test",
        )

        second = self.root / "second.json"
        second.write_text(json.dumps({
            "address": "2001:db8:1300::11",
            "ipv6_prefix": "2001:db8:1300::/48",
            "port": 8443,
            "tls_server_name": "router-13.example.test",
            "ca_bundle_id": "corp-ca",
        }), encoding="utf-8")
        install_descriptor(
            second,
            ca_file=self.root / "ca.crt",
            client_cert_file=self.root / "caller.crt",
            client_key_file=self.root / "caller.key",
            output_file=output,
        )
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(len(document["routes"]), 2)
        self.assertEqual(
            NexusSecurityProfile.load(output).resolve("2001:db8:1300::99").port,
            8443,
        )

    def test_plain_http_descriptor_needs_no_security_profile(self):
        descriptor = self.root / "plain.json"
        descriptor.write_text(json.dumps({
            "address": "2001:db8:1400::11",
            "ipv6_prefix": "2001:db8:1400::/48",
            "scheme": "http",
            "port": 7443,
        }), encoding="utf-8")
        with self.assertRaisesRegex(
            NexusSecurityConfigurationError, "no TLS security profile is needed"
        ):
            install_descriptor(
                descriptor,
                ca_file=self.root / "ca.crt",
                client_cert_file=self.root / "caller.crt",
                client_key_file=self.root / "caller.key",
                output_file=self.root / "unused.json",
            )


if __name__ == "__main__":
    unittest.main()
