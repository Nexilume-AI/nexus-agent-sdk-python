"""Locally provisioned TLS material for direct IPv6 Agent calls.

Application code supplies only the destination IPv6 address and its JWT.  A
machine-local profile binds routed IPv6 prefixes to stable TLS names and trust
bundles, and supplies the caller workload identity.  Private keys are never
stored in a router-exported endpoint descriptor.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
import tempfile
import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple, Union

from .errors import NexusSecurityConfigurationError


PROFILE_ENVIRONMENT_VARIABLE = "NEXUS_AGENT_SECURITY_PROFILE"


def _required_text(value: Any, name: str, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise NexusSecurityConfigurationError(
            f"{name} must be a non-empty string up to {maximum} characters"
        )
    return value


def _resolve_file(profile_directory: Path, value: Any, name: str) -> str:
    raw = _required_text(value, name)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = profile_directory / path
    path = path.resolve()
    if not path.is_file():
        raise NexusSecurityConfigurationError(f"{name} does not exist: {path}")
    return str(path)


@dataclass(frozen=True)
class ResolvedIPv6Security:
    """Complete connection inputs selected for one literal IPv6 address."""

    ipv6_prefix: str
    port: int
    tls_server_name: str
    ca_bundle_id: str
    ca_file: str
    cert_file: str
    key_file: str


@dataclass(frozen=True)
class _RouteBinding:
    network: ipaddress.IPv6Network
    port: int
    tls_server_name: str
    ca_bundle_id: str


class NexusSecurityProfile:
    """Validated, immutable SDK security configuration.

    Route selection uses IPv6 longest-prefix matching.  The caller certificate
    is machine/workload identity and therefore global to the profile; target
    trust is selected by ``ca_bundle_id`` for each prefix.
    """

    def __init__(
        self,
        *,
        routes: Tuple[_RouteBinding, ...],
        trust_bundles: Mapping[str, str],
        cert_file: str,
        key_file: str,
        source_file: Optional[Path] = None,
    ) -> None:
        self._routes = routes
        self._trust_bundles = dict(trust_bundles)
        self.cert_file = cert_file
        self.key_file = key_file
        self.source_file = source_file

    @staticmethod
    def default_path() -> Path:
        configured = os.environ.get(PROFILE_ENVIRONMENT_VARIABLE)
        if configured:
            return Path(configured).expanduser()
        if sys.platform == "win32":
            root = os.environ.get("APPDATA")
            base = Path(root) if root else Path.home() / "AppData" / "Roaming"
            return base / "NexusAgent" / "security.json"
        root = os.environ.get("XDG_CONFIG_HOME")
        base = Path(root) if root else Path.home() / ".config"
        return base / "nexus-agent" / "security.json"

    @classmethod
    def load(cls, path: Optional[Union[str, os.PathLike[str]]] = None) -> "NexusSecurityProfile":
        profile_path = Path(path).expanduser() if path is not None else cls.default_path()
        profile_path = profile_path.resolve()
        try:
            raw = json.loads(profile_path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError as exc:
            raise NexusSecurityConfigurationError(
                "no local Nexus security profile is installed; expected "
                f"{profile_path} (or set {PROFILE_ENVIRONMENT_VARIABLE})"
            ) from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise NexusSecurityConfigurationError(
                f"cannot read Nexus security profile {profile_path}: {exc}"
            ) from exc
        return cls.from_dict(raw, profile_directory=profile_path.parent, source_file=profile_path)

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        profile_directory: Union[str, os.PathLike[str]],
        source_file: Optional[Union[str, os.PathLike[str]]] = None,
    ) -> "NexusSecurityProfile":
        if not isinstance(value, Mapping) or value.get("version") != 1:
            raise NexusSecurityConfigurationError("security profile version must be 1")
        base = Path(profile_directory).expanduser().resolve()
        identity = value.get("caller_identity")
        if not isinstance(identity, Mapping):
            raise NexusSecurityConfigurationError("caller_identity must be an object")
        cert_file = _resolve_file(base, identity.get("cert_file"), "caller_identity.cert_file")
        key_file = _resolve_file(base, identity.get("key_file"), "caller_identity.key_file")

        raw_bundles = value.get("trust_bundles")
        if not isinstance(raw_bundles, Mapping) or not raw_bundles:
            raise NexusSecurityConfigurationError("trust_bundles must be a non-empty object")
        bundles = {}
        for raw_name, raw_bundle in raw_bundles.items():
            name = _required_text(raw_name, "trust bundle id", 63)
            if not isinstance(raw_bundle, Mapping):
                raise NexusSecurityConfigurationError(f"trust bundle {name} must be an object")
            bundles[name] = _resolve_file(base, raw_bundle.get("ca_file"), f"trust_bundles.{name}.ca_file")

        raw_routes = value.get("routes")
        if not isinstance(raw_routes, list) or not raw_routes:
            raise NexusSecurityConfigurationError("routes must be a non-empty array")
        routes = []
        seen = set()
        for index, raw_route in enumerate(raw_routes):
            label = f"routes[{index}]"
            if not isinstance(raw_route, Mapping):
                raise NexusSecurityConfigurationError(f"{label} must be an object")
            prefix = _required_text(raw_route.get("ipv6_prefix"), f"{label}.ipv6_prefix", 64)
            try:
                network = ipaddress.ip_network(prefix, strict=True)
            except ValueError as exc:
                raise NexusSecurityConfigurationError(
                    f"{label}.ipv6_prefix must be a canonical IPv6 prefix"
                ) from exc
            if not isinstance(network, ipaddress.IPv6Network):
                raise NexusSecurityConfigurationError(f"{label}.ipv6_prefix must be IPv6")
            if network in seen:
                raise NexusSecurityConfigurationError(f"duplicate IPv6 prefix: {network}")
            seen.add(network)
            port = raw_route.get("port", 7443)
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise NexusSecurityConfigurationError(f"{label}.port must be between 1 and 65535")
            tls_name = _required_text(raw_route.get("tls_server_name"), f"{label}.tls_server_name", 253)
            bundle_id = _required_text(raw_route.get("ca_bundle_id"), f"{label}.ca_bundle_id", 63)
            if bundle_id not in bundles:
                raise NexusSecurityConfigurationError(
                    f"{label}.ca_bundle_id refers to unknown trust bundle {bundle_id}"
                )
            routes.append(_RouteBinding(network, port, tls_name, bundle_id))

        routes.sort(key=lambda route: route.network.prefixlen, reverse=True)
        return cls(
            routes=tuple(routes),
            trust_bundles=bundles,
            cert_file=cert_file,
            key_file=key_file,
            source_file=Path(source_file).resolve() if source_file is not None else None,
        )

    def resolve(self, address: Union[str, ipaddress.IPv6Address]) -> ResolvedIPv6Security:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise NexusSecurityConfigurationError("security target must be an IPv6 address") from exc
        if not isinstance(parsed, ipaddress.IPv6Address):
            raise NexusSecurityConfigurationError("security target must be an IPv6 address")
        for route in self._routes:
            if parsed in route.network:
                return ResolvedIPv6Security(
                    ipv6_prefix=str(route.network),
                    port=route.port,
                    tls_server_name=route.tls_server_name,
                    ca_bundle_id=route.ca_bundle_id,
                    ca_file=self._trust_bundles[route.ca_bundle_id],
                    cert_file=self.cert_file,
                    key_file=self.key_file,
                )
        source = f" in {self.source_file}" if self.source_file else ""
        raise NexusSecurityConfigurationError(
            f"no security route matches IPv6 address {parsed.compressed}{source}"
        )


def _absolute_cli_file(value: str, name: str) -> str:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise NexusSecurityConfigurationError(f"{name} does not exist: {path}")
    return str(path)


def install_descriptor(
    descriptor_file: Union[str, os.PathLike[str]],
    *,
    ca_file: Union[str, os.PathLike[str]],
    client_cert_file: Union[str, os.PathLike[str]],
    client_key_file: Union[str, os.PathLike[str]],
    output_file: Optional[Union[str, os.PathLike[str]]] = None,
) -> Path:
    """Install or update one LuCI-exported IPv6 route binding.

    This is an administrative provisioning operation.  It records absolute
    paths to existing credentials; it never copies or embeds private keys.
    Existing bindings for other prefixes and trust bundles are preserved.
    """

    source = Path(descriptor_file).expanduser().resolve()
    try:
        descriptor = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NexusSecurityConfigurationError(
            f"cannot read direct connection descriptor {source}: {exc}"
        ) from exc
    if not isinstance(descriptor, Mapping):
        raise NexusSecurityConfigurationError("direct connection descriptor must be an object")
    if descriptor.get("scheme", "https") != "https":
        raise NexusSecurityConfigurationError(
            "no TLS security profile is needed for a plain HTTP descriptor"
        )
    try:
        address = ipaddress.ip_address(descriptor["address"])
        raw_prefix = descriptor.get("ipv6_prefix") or f"{address.compressed}/128"
        network = ipaddress.ip_network(raw_prefix, strict=True)
        port = descriptor.get("port", 7443)
        tls_name = _required_text(descriptor.get("tls_server_name"), "tls_server_name", 253)
        bundle_id = _required_text(descriptor.get("ca_bundle_id"), "ca_bundle_id", 63)
    except (KeyError, ValueError) as exc:
        raise NexusSecurityConfigurationError("invalid direct connection descriptor") from exc
    if not isinstance(address, ipaddress.IPv6Address) or not isinstance(network, ipaddress.IPv6Network):
        raise NexusSecurityConfigurationError("descriptor address and prefix must be IPv6")
    if address not in network:
        raise NexusSecurityConfigurationError("descriptor address is outside ipv6_prefix")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise NexusSecurityConfigurationError("descriptor port must be between 1 and 65535")

    output = Path(output_file).expanduser() if output_file else NexusSecurityProfile.default_path()
    output = output.resolve()
    if output.exists():
        try:
            document = json.loads(output.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise NexusSecurityConfigurationError(
                f"cannot update existing security profile {output}: {exc}"
            ) from exc
        if not isinstance(document, dict) or document.get("version") != 1:
            raise NexusSecurityConfigurationError("existing security profile version must be 1")
        if not isinstance(document.get("trust_bundles"), dict) or not isinstance(document.get("routes"), list):
            raise NexusSecurityConfigurationError("existing security profile has an invalid structure")
    else:
        document = {"version": 1, "trust_bundles": {}, "routes": []}

    document["caller_identity"] = {
        "cert_file": _absolute_cli_file(str(client_cert_file), "client certificate"),
        "key_file": _absolute_cli_file(str(client_key_file), "client private key"),
    }
    document["trust_bundles"][bundle_id] = {
        "ca_file": _absolute_cli_file(str(ca_file), "CA bundle")
    }
    binding = {
        "ipv6_prefix": str(network),
        "port": port,
        "tls_server_name": tls_name,
        "ca_bundle_id": bundle_id,
    }
    document["routes"] = [
        route for route in document["routes"]
        if not isinstance(route, Mapping) or route.get("ipv6_prefix") != str(network)
    ]
    document["routes"].append(binding)

    # Validate the complete merged file before the atomic replacement.
    NexusSecurityProfile.from_dict(document, profile_directory=output.parent)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent,
            prefix=f".{output.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary_name = handle.name
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, output)
        temporary_name = None
        if os.name != "nt":
            output.chmod(0o600)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
    return output


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nexus-agent-security",
        description="Provision direct IPv6 TLS identity outside Agent application code.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install", help="install a LuCI connection descriptor")
    install.add_argument("descriptor")
    install.add_argument("--ca-file", required=True)
    install.add_argument("--client-cert", required=True)
    install.add_argument("--client-key", required=True)
    install.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        result = install_descriptor(
            args.descriptor,
            ca_file=args.ca_file,
            client_cert_file=args.client_cert,
            client_key_file=args.client_key,
            output_file=args.output,
        )
    except NexusSecurityConfigurationError as exc:
        parser.error(str(exc))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
