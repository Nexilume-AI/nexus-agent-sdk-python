"""Windows address readiness regressions, runnable without changing a NIC."""

import ctypes
import ipaddress
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from nexus_agent.host_alias import HostAliasAllocator, HostAliasError, WindowsAddressBackend
from nexus_agent import _windows_iphelper as iphelper


ADDRESS = ipaddress.IPv6Address("2001:db8::1234")
INTERFACE = "Test Ethernet"


def windows_error(code):
    error = OSError(code, "test socket error")
    error.winerror = code
    return error


class WindowsReadinessTest(unittest.TestCase):
    def setUp(self):
        self.backend = WindowsAddressBackend(timeout=0.2)
        self.elapsed = 0.0
        self.probe = mock.Mock()
        self.run = self.patch.object(
            self.backend, "_run",
            return_value=f"Address {ADDRESS} Parameters\nDAD State: Preferred",
        )
        self.patch.object(self.backend, "interface_index", return_value=27)
        self.state = self.patch.object(
            self.backend, "_address_state", return_value="Preferred", create=True,
        )
        self.remove = self.patch.object(self.backend, "_delete_address")
        self.socket = self.patch("socket.socket", return_value=self.probe)
        self.patch("nexus_agent.host_alias.time.monotonic", side_effect=lambda: self.elapsed)
        self.patch("nexus_agent.host_alias.time.sleep", side_effect=self.advance)

    class _Patch:
        def __init__(self, case):
            self.case = case

        def __call__(self, *args, **kwargs):
            return self.start(mock.patch(*args, **kwargs))

        def object(self, *args, **kwargs):
            return self.start(mock.patch.object(*args, **kwargs))

        def start(self, patcher):
            result = patcher.start()
            self.case.addCleanup(patcher.stop)
            return result

    @property
    def patch(self):
        return self._Patch(self)

    def advance(self, seconds):
        self.elapsed += seconds

    def test_permission_denied_is_not_reported_as_dad_timeout(self):
        error = windows_error(10013)
        self.probe.bind.side_effect = error
        with self.assertRaisesRegex(HostAliasError, r"IPV6_BIND_FAILED.*10013") as raised:
            self.backend.add_address(INTERFACE, ADDRESS)
        self.assertIs(raised.exception.__cause__, error)
        self.assertEqual(self.elapsed, 0)
        self.remove.assert_called_once_with("27", ADDRESS)
        self.probe.close.assert_called_once()

    def test_unrelated_duplicate_on_interface_does_not_reject_target(self):
        self.run.return_value += "\nAddress 2001:db8::9999 Parameters\nDAD State: Duplicate"
        self.backend.add_address(INTERFACE, ADDRESS)
        self.state.assert_called_once_with(27, ADDRESS)
        self.remove.assert_not_called()

    def test_tentative_address_is_not_treated_as_ready(self):
        self.state.side_effect = ["Tentative", "Tentative", "Preferred"]
        self.backend.add_address(INTERFACE, ADDRESS)
        self.assertEqual(self.state.call_count, 3)
        self.probe.bind.assert_called_once_with((str(ADDRESS), 0))

    def test_target_duplicate_has_distinct_error_and_is_removed(self):
        self.state.return_value = "Duplicate"
        with self.assertRaisesRegex(HostAliasError, "IPV6_DAD_DUPLICATE"):
            self.backend.add_address(INTERFACE, ADDRESS)
        self.remove.assert_called_once_with("27", ADDRESS)
        self.socket.assert_not_called()

    def test_allocator_recovers_from_real_windows_duplicate_signal(self):
        self.patch.object(self.backend, "prefix_ready", return_value=True)
        self.patch.object(self.backend, "has_address", return_value=False)
        self.state.side_effect = ["Duplicate", "Preferred"]
        allocator = HostAliasAllocator(
            backend=self.backend, interface=INTERFACE,
            prefix="2606:4700:4700:1200::/64", allocation_secret=b"s" * 32,
        )
        first = allocator._address("uid:1000", "demo", "agent-a", 0)
        second = allocator._address("uid:1000", "demo", "agent-a", 1)
        lease = allocator.allocate(owner="uid:1000", tenant="demo", agent_id="agent-a")
        self.assertEqual(lease.address, str(second))
        self.remove.assert_called_once_with("27", first)
        self.probe.bind.assert_called_once_with((str(second), 0))
        self.assertEqual(len(allocator.list()), 1)

    def test_duplicate_rollback_failure_is_not_retried_by_allocator(self):
        self.patch.object(self.backend, "prefix_ready", return_value=True)
        self.patch.object(self.backend, "has_address", return_value=False)
        self.state.return_value = "Duplicate"
        self.remove.side_effect = HostAliasError("cleanup denied")
        allocator = HostAliasAllocator(
            backend=self.backend, interface=INTERFACE,
            prefix="2606:4700:4700:1200::/64", allocation_secret=b"s" * 32,
        )
        with self.assertRaisesRegex(HostAliasError, "IPV6_DAD_DUPLICATE.*rollback failed"):
            allocator.allocate(owner="uid:1000", tenant="demo", agent_id="agent-a")
        self.run.assert_called_once()
        self.remove.assert_called_once()
        self.assertEqual(allocator.list(), ())

    def test_only_bind_propagation_error_is_retried(self):
        self.probe.bind.side_effect = [windows_error(10049), None]
        self.backend.add_address(INTERFACE, ADDRESS)
        self.assertEqual(self.probe.bind.call_count, 2)
        self.assertEqual(self.probe.close.call_count, 2)
        self.remove.assert_not_called()

    def test_other_socket_errors_fail_immediately(self):
        for code in (10048, 10050, 10022):
            with self.subTest(code=code):
                self.probe.bind.side_effect = windows_error(code)
                with self.assertRaisesRegex(HostAliasError, f"IPV6_BIND_FAILED.*{code}"):
                    self.backend.add_address(INTERFACE, ADDRESS)
                self.assertEqual(self.elapsed, 0)

    def test_socket_creation_failure_also_rolls_back_address(self):
        self.socket.side_effect = windows_error(10013)
        with self.assertRaisesRegex(HostAliasError, "IPV6_BIND_FAILED.*10013"):
            self.backend.add_address(INTERFACE, ADDRESS)
        self.probe.close.assert_not_called()
        self.remove.assert_called_once_with("27", ADDRESS)

    def test_tentative_timeout_has_state_interface_and_bound(self):
        self.state.return_value = "Tentative"
        with self.assertRaisesRegex(HostAliasError, "IPV6_DAD_TIMEOUT") as raised:
            self.backend.add_address(INTERFACE, ADDRESS)
        self.assertIn(INTERFACE, str(raised.exception))
        self.assertIn("state=Tentative", str(raised.exception))
        self.assertEqual(self.elapsed, self.backend.timeout)
        self.remove.assert_called_once_with("27", ADDRESS)
        self.socket.assert_not_called()

    def test_preferred_bind_timeout_is_not_dad_timeout(self):
        error = windows_error(10049)
        self.probe.bind.side_effect = error
        with self.assertRaisesRegex(HostAliasError, "IPV6_BIND_TIMEOUT.*10049") as raised:
            self.backend.add_address(INTERFACE, ADDRESS)
        self.assertIs(raised.exception.__cause__, error)
        self.assertEqual(self.elapsed, self.backend.timeout)
        self.assertEqual(self.probe.bind.call_count, self.probe.close.call_count)
        self.remove.assert_called_once_with("27", ADDRESS)

    def test_missing_address_can_appear_then_complete_dad(self):
        self.state.side_effect = [None, "Tentative", "Preferred"]
        self.backend.add_address(INTERFACE, ADDRESS)
        self.probe.bind.assert_called_once()
        self.remove.assert_not_called()

    def test_missing_address_timeout_is_not_dad_timeout(self):
        self.state.return_value = None
        with self.assertRaisesRegex(HostAliasError, "IPV6_ADDRESS_NOT_READY.*state=Missing"):
            self.backend.add_address(INTERFACE, ADDRESS)
        self.socket.assert_not_called()
        self.remove.assert_called_once_with("27", ADDRESS)

    def test_unusable_and_unknown_states_fail_closed(self):
        for state in ("Invalid", "Deprecated", "Unknown(99)"):
            with self.subTest(state=state):
                self.state.return_value = state
                with self.assertRaisesRegex(HostAliasError, "IPV6_ADDRESS_UNUSABLE"):
                    self.backend.add_address(INTERFACE, ADDRESS)
                self.assertEqual(self.elapsed, 0)
        self.socket.assert_not_called()

    def test_query_failure_rolls_back_without_querying_again(self):
        self.state.side_effect = HostAliasError("IPV6_STATE_QUERY_FAILED")
        with self.assertRaisesRegex(HostAliasError, "IPV6_STATE_QUERY_FAILED"):
            self.backend.add_address(INTERFACE, ADDRESS)
        self.state.assert_called_once()
        self.remove.assert_called_once_with("27", ADDRESS)

    def test_rollback_failure_keeps_original_reason(self):
        self.probe.bind.side_effect = windows_error(10013)
        self.remove.side_effect = HostAliasError("cleanup denied")
        with self.assertRaisesRegex(HostAliasError, "IPV6_BIND_FAILED.*10013.*rollback failed") as raised:
            self.backend.add_address(INTERFACE, ADDRESS)
        self.assertIn("cleanup denied", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, HostAliasError)

    def test_failed_add_does_not_delete_an_address_it_did_not_create(self):
        self.run.side_effect = HostAliasError("address already exists")
        with self.assertRaisesRegex(HostAliasError, "already exists"):
            self.backend.add_address(INTERFACE, ADDRESS)
        self.remove.assert_not_called()
        self.state.assert_not_called()

    def test_add_uses_exact_interface_active_store_and_128(self):
        self.backend.add_address(INTERFACE, ADDRESS)
        self.run.assert_called_once_with([
            "netsh", "interface", "ipv6", "add", "address", "interface=27",
            f"address={ADDRESS}/128", "type=unicast", "store=active",
        ])


class WindowsAddressQueryTest(unittest.TestCase):
    def test_win32_abi_layout(self):
        self.assertEqual(ctypes.sizeof(iphelper._SockaddrIn6), 28)
        self.assertEqual(ctypes.sizeof(iphelper._UnicastAddressRow), 80)
        self.assertEqual(iphelper._UnicastAddressRow.interface_luid.offset, 32)
        self.assertEqual(iphelper._UnicastAddressRow.interface_index.offset, 40)
        self.assertEqual(iphelper._UnicastAddressRow.dad_state.offset, 64)

    def test_native_query_uses_exact_binary_address_and_interface(self):
        def query(pointer):
            row = ctypes.cast(pointer, ctypes.POINTER(iphelper._UnicastAddressRow)).contents
            self.assertEqual(row.interface_index, 27)
            self.assertEqual(row.interface_luid, 0)
            self.assertEqual(row.address.ipv6.family, 23)
            self.assertEqual(bytes(row.address.ipv6.address), ADDRESS.packed)
            row.dad_state = 4
            return 0

        with mock.patch.object(iphelper, "_get_unicast_entry", return_value=query):
            self.assertEqual(iphelper.address_state(27, ipaddress.IPv6Address(ADDRESS.exploded)), "Preferred")

    def test_all_dad_states_are_locale_independent(self):
        for number, expected in enumerate(("Invalid", "Tentative", "Duplicate", "Deprecated", "Preferred")):
            def query(pointer):
                ctypes.cast(pointer, ctypes.POINTER(iphelper._UnicastAddressRow)).contents.dad_state = number
                return 0

            with self.subTest(state=expected), mock.patch.object(iphelper, "_get_unicast_entry", return_value=query):
                self.assertEqual(iphelper.address_state(27, ADDRESS), expected)

    def test_absent_address_is_not_confused_with_query_errors(self):
        api = mock.Mock(return_value=1168)
        with mock.patch.object(iphelper, "_get_unicast_entry", return_value=api):
            self.assertIsNone(iphelper.address_state(27, ADDRESS))
            for code in (2, 5, 50, 87):
                with self.subTest(code=code):
                    api.return_value = code
                    with self.assertRaisesRegex(OSError, f"WinError {code}"):
                        iphelper.address_state(27, ADDRESS)

    def test_query_os_error_retains_code_and_cause(self):
        error = windows_error(5)
        with mock.patch.object(iphelper, "address_state", side_effect=error):
            with self.assertRaisesRegex(HostAliasError, "IPV6_STATE_QUERY_FAILED.*5") as raised:
                WindowsAddressBackend()._address_state(27, ADDRESS)
        self.assertIs(raised.exception.__cause__, error)

    def test_has_address_and_remove_do_not_use_substring_matching(self):
        backend = WindowsAddressBackend()
        with mock.patch.object(backend, "interface_index", return_value=27), mock.patch.object(
            backend, "_address_state", return_value=None,
        ) as state, mock.patch.object(backend, "_run") as run:
            self.assertFalse(backend.has_address(INTERFACE, ADDRESS))
            backend.remove_address(INTERFACE, ADDRESS)
            run.assert_not_called()
            self.assertEqual(state.call_args_list, [mock.call(27, ADDRESS)] * 2)
            state.return_value = "Preferred"
            self.assertTrue(backend.has_address(INTERFACE, ADDRESS))
            backend.remove_address(INTERFACE, ADDRESS)
            run.assert_called_once_with([
                "netsh", "interface", "ipv6", "delete", "address", "interface=27",
                f"address={ADDRESS}", "store=active",
            ])

    @unittest.skipUnless(os.name == "nt", "Windows IP Helper")
    def test_real_read_only_loopback_query(self):
        self.assertEqual(iphelper.address_state(1, ipaddress.IPv6Address("::1")), "Preferred")
        self.assertIsNone(iphelper.address_state(1, ADDRESS))


if __name__ == "__main__":
    unittest.main()
