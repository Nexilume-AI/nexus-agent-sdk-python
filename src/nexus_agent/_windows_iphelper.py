"""Read one Windows IPv6 address without localized netsh output or subprocesses."""

import ctypes
import ipaddress
from functools import lru_cache
from typing import Optional


# Fixed-width Win32 ABI types: ctypes.c_ulong has a different size on POSIX.
# https://learn.microsoft.com/windows/win32/api/netioapi/ns-netioapi-mib_unicastipaddress_row
class _SockaddrIn6(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [
        ("family", ctypes.c_uint16),
        ("port", ctypes.c_uint16),
        ("flowinfo", ctypes.c_uint32),
        ("address", ctypes.c_ubyte * 16),
        ("scope_id", ctypes.c_uint32),
    ]


class _SockaddrInet(ctypes.Union):
    _layout_ = "ms"
    _fields_ = [("ipv6", _SockaddrIn6), ("ipv4", ctypes.c_ubyte * 16)]


class _UnicastAddressRow(ctypes.Structure):
    _layout_ = "ms"
    _fields_ = [
        ("address", _SockaddrInet),
        ("interface_luid", ctypes.c_uint64),
        ("interface_index", ctypes.c_uint32),
        ("prefix_origin", ctypes.c_uint32),
        ("suffix_origin", ctypes.c_uint32),
        ("valid_lifetime", ctypes.c_uint32),
        ("preferred_lifetime", ctypes.c_uint32),
        ("prefix_length", ctypes.c_uint8),
        ("skip_as_source", ctypes.c_uint8),
        ("dad_state", ctypes.c_uint32),
        ("scope_id", ctypes.c_uint32),
        ("created_at", ctypes.c_int64),
    ]


@lru_cache(maxsize=1)
def _get_unicast_entry():
    # Load only from System32; never search an Agent's working directory.
    api = ctypes.WinDLL("iphlpapi.dll", winmode=0x00000800)
    function = api.GetUnicastIpAddressEntry
    function.argtypes = [ctypes.POINTER(_UnicastAddressRow)]
    function.restype = ctypes.c_uint32
    return function


def address_state(interface_index: int, address: ipaddress.IPv6Address) -> Optional[str]:
    """Return NL_DAD_STATE, or None only if this address is absent on this NIC."""
    row = _UnicastAddressRow()
    row.interface_index = interface_index
    row.address.ipv6.family = 23  # Windows AF_INET6 (not the host test OS constant).
    row.address.ipv6.address[:] = address.packed
    result = _get_unicast_entry()(ctypes.byref(row))
    if result == 1168:  # ERROR_NOT_FOUND: address not on the specified interface.
        return None
    if result:
        # Do not confuse interface disappearance, access denial, or IPv6 disabled
        # with an absent address. Preserve the returned Win32 code.
        raise OSError(result, f"GetUnicastIpAddressEntry failed (WinError {result})")
    states = {0: "Invalid", 1: "Tentative", 2: "Duplicate", 3: "Deprecated", 4: "Preferred"}
    return states.get(row.dad_state, f"Unknown({row.dad_state})")
