"""Direct public IPv6 target without Agent discovery or routed selection."""

import ipaddress
import os
import uuid
from typing import Any, Dict, Iterator, Mapping, Optional, Union

from .auth import TokenProvider
from .client import NexusAgentClient
from .models import PublicAgentEndpoint, SseEvent
from .security_profile import NexusSecurityProfile


class DirectIPv6Agent:
    """Invoke one exact router-managed or Agent-owned endpoint over IPv6.

    The TCP authority is the literal IPv6 address. ``server_identity`` is the
    stable certificate identity used for SNI and hostname verification. The
    client disables environment proxies so the request cannot silently leave
    the direct IPv6 path through an HTTP proxy.
    """

    def __init__(
        self,
        address: str,
        *,
        scheme: str = "https",
        server_identity: Optional[str] = None,
        port: Optional[int] = None,
        token: Optional[str] = None,
        token_provider: Optional[TokenProvider] = None,
        transaction_token: Optional[str] = None,
        ca_file: Optional[str] = None,
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
        security_profile: Optional[NexusSecurityProfile] = None,
        security_profile_file: Optional[Union[str, os.PathLike[str]]] = None,
        timeout: float = 10.0,
    ) -> None:
        if not isinstance(address, str) or not address:
            raise ValueError("address must be an IPv6 address")
        normalized = address[1:-1] if address.startswith("[") and address.endswith("]") else address
        if "%" in normalized:
            raise ValueError("scoped IPv6 addresses are not valid public Agent targets")
        try:
            parsed = ipaddress.ip_address(normalized)
        except ValueError as exc:
            raise ValueError("address must be an IPv6 address") from exc
        if not isinstance(parsed, ipaddress.IPv6Address):
            raise ValueError("address must be an IPv6 address")
        if scheme not in ("https", "http"):
            raise ValueError("scheme must be https or http")
        if security_profile is not None and security_profile_file is not None:
            raise ValueError("security_profile and security_profile_file are mutually exclusive")
        if scheme == "http" and any((
            server_identity, ca_file, cert_file, key_file,
            security_profile, security_profile_file,
        )):
            raise ValueError("plain HTTP mode does not accept TLS security settings")
        use_profile = scheme == "https" and (
            server_identity is None or security_profile is not None or security_profile_file is not None
        )
        if use_profile:
            profile = security_profile or NexusSecurityProfile.load(security_profile_file)
            if not isinstance(profile, NexusSecurityProfile):
                raise TypeError("security_profile must be a NexusSecurityProfile")
            resolved = profile.resolve(parsed)
            server_identity = server_identity or resolved.tls_server_name
            port = port if port is not None else resolved.port
            ca_file = ca_file or resolved.ca_file
            cert_file = cert_file or resolved.cert_file
            key_file = key_file or resolved.key_file
        if port is None:
            port = 7443
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if scheme == "https" and (
            not isinstance(server_identity, str)
            or not server_identity
            or server_identity != server_identity.strip()
            or len(server_identity) > 253
        ):
            raise ValueError(
                "server_identity must be a non-empty TLS identity up to 253 characters"
            )
        self.address = parsed.compressed
        self.port = port
        self.server_identity = server_identity
        self.scheme = scheme
        self.base_url = f"{scheme}://[{self.address}]:{port}"
        explicit_auth = {"auth": "none"} if token is None \
            and token_provider is None and transaction_token is None else {}
        self.client = NexusAgentClient(
            self.base_url,
            token=token,
            token_provider=token_provider,
            transaction_token=transaction_token,
            ca_file=ca_file,
            cert_file=cert_file,
            key_file=key_file,
            tls_server_name=server_identity,
            use_environment_proxy=False,
            timeout=timeout,
            **explicit_auth,
        )

    @classmethod
    def from_endpoint(
        cls,
        endpoint: PublicAgentEndpoint,
        **credentials: Any,
    ) -> "DirectIPv6Agent":
        if not isinstance(endpoint, PublicAgentEndpoint):
            raise TypeError("endpoint must be a PublicAgentEndpoint")
        return cls(
            endpoint.address,
            scheme=endpoint.scheme,
            port=endpoint.port,
            server_identity=endpoint.tls_server_name,
            **credentials,
        )

    @classmethod
    def plain_http(
        cls,
        address: str,
        *,
        token: Optional[str] = None,
        token_provider: Optional[TokenProvider] = None,
        transaction_token: Optional[str] = None,
        port: int = 7443,
        timeout: float = 10.0,
    ) -> "DirectIPv6Agent":
        """Connect without TLS; any supplied JWT and payload use cleartext."""
        return cls(
            address,
            scheme="http",
            port=port,
            token=token,
            token_provider=token_provider,
            transaction_token=transaction_token,
            timeout=timeout,
        )

    @staticmethod
    def _envelope(
        intent: str,
        payload: Any,
        *,
        tenant: str,
        source_agent: str,
        intent_version: int,
        task_id: Optional[str],
        hop_limit: int,
        constraints: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "version": "1.0",
            "intent": intent,
            "intent_version": intent_version,
            "task_id": task_id or str(uuid.uuid4()),
            "source_agent": source_agent,
            "tenant": tenant,
            "hop_limit": hop_limit,
            "constraints": dict(constraints or {}),
            "payload": payload,
        }

    def invoke(
        self,
        intent: str,
        payload: Any,
        *,
        tenant: str,
        source_agent: str,
        intent_version: int = 1,
        task_id: Optional[str] = None,
        hop_limit: int = 8,
        constraints: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.client.invoke(self._envelope(
            intent,
            payload,
            tenant=tenant,
            source_agent=source_agent,
            intent_version=intent_version,
            task_id=task_id,
            hop_limit=hop_limit,
            constraints=constraints,
        ))

    def invoke_intent(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return self.invoke(*args, **kwargs)

    def invoke_stream(
        self,
        intent: str,
        payload: Any,
        *,
        tenant: str,
        source_agent: str,
        intent_version: int = 1,
        task_id: Optional[str] = None,
        hop_limit: int = 8,
        constraints: Optional[Mapping[str, Any]] = None,
        resume: bool = True,
        last_event_id: int = 0,
        max_reconnects: int = 3,
        reconnect_delay: float = 0.25,
    ) -> Iterator[SseEvent]:
        envelope = self._envelope(
            intent,
            payload,
            tenant=tenant,
            source_agent=source_agent,
            intent_version=intent_version,
            task_id=task_id,
            hop_limit=hop_limit,
            constraints=constraints,
        )
        return self.client.invoke_stream(
            envelope,
            resume=resume,
            last_event_id=last_event_id,
            max_reconnects=max_reconnects,
            reconnect_delay=reconnect_delay,
        )
