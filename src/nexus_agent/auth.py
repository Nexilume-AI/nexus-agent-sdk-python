"""Credential providers and discovery for Nexus Agent Router authentication."""

import base64
import hashlib
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass
from typing import Any, Dict, Mapping, MutableMapping, Optional, Protocol, Sequence

from .errors import NexusAuthDiscoveryError, NexusTokenAcquisitionError


CANONICAL_TOKEN_ENV = "NEXUS_AGENT_TOKEN"
LEGACY_TOKEN_ENVS = ("NEXUS_JWT", "NEXUS_AGENT_JWT", "NEXUS_TOKEN")
_CLOUD_CA_MAX_BYTES = 65536
_PEM_CERTIFICATE_BUNDLE = re.compile(
    r"(?:\s*-----BEGIN CERTIFICATE-----\s+"
    r"[A-Za-z0-9+/=\r\n]+"
    r"-----END CERTIFICATE-----\s*)+\Z"
)


class CloudTrustUnavailableError(OSError):
    """The router did not provide trust for a private Cloud certificate."""


class CloudTrustVerificationError(OSError):
    """The enrolled router trust could not verify the Cloud peer."""


class CloudTrustOriginError(ValueError):
    """A Run context exchange escaped the enrolled Cloud origin."""


def _https_origin(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        return None
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname:
        return None
    try:
        hostname = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port not in (None, 443):
        authority = f"{authority}:{port}"
    return urllib.parse.urlunsplit(("https", authority, "", "", ""))


def _request_origin(value: str) -> Optional[str]:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return _https_origin(
        urllib.parse.urlunsplit(("https", parsed.netloc, "", "", ""))
    )


def _certificate_failure(exc: BaseException) -> bool:
    current: Optional[BaseException] = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException):
            current = reason
            continue
        current = current.__cause__ or current.__context__
    return False


class CloudTrustPolicy:
    """One in-memory, enrollment-scoped Cloud TLS policy."""

    def __init__(
        self,
        *,
        mode: str,
        origin: str = "",
        sha256: str = "",
        ca_pem: str = "",
        ca_file: Optional[str] = None,
    ) -> None:
        if mode not in ("system", "pinned-pem", "explicit-file"):
            raise NexusTokenAcquisitionError("router returned an unsupported Cloud trust mode")
        normalized_origin = _https_origin(origin) if origin else ""
        if mode in ("system", "pinned-pem") and not origin:
            raise NexusTokenAcquisitionError("router Cloud trust is missing its origin")
        if origin and normalized_origin is None:
            raise NexusTokenAcquisitionError("router returned an invalid Cloud trust origin")
        if mode == "pinned-pem":
            if not isinstance(ca_pem, str) or not ca_pem:
                raise NexusTokenAcquisitionError("router Cloud trust is missing its CA bundle")
            try:
                encoded = ca_pem.encode("ascii", "strict")
            except UnicodeEncodeError as exc:
                raise NexusTokenAcquisitionError(
                    "router Cloud CA bundle is unsafe or too large"
                ) from exc
            if (
                len(encoded) > _CLOUD_CA_MAX_BYTES
                or _PEM_CERTIFICATE_BUNDLE.fullmatch(ca_pem) is None
            ):
                raise NexusTokenAcquisitionError("router Cloud CA bundle is unsafe or too large")
            digest = hashlib.sha256(encoded).hexdigest()
            if not isinstance(sha256, str) or sha256.lower() != digest:
                raise NexusTokenAcquisitionError("router Cloud CA digest does not match its bundle")
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            try:
                context.load_verify_locations(cadata=ca_pem)
            except ssl.SSLError as exc:
                raise NexusTokenAcquisitionError("router Cloud CA bundle is not valid PEM") from exc
        elif mode == "explicit-file":
            if not ca_file:
                raise ValueError("cloud_ca_file must not be empty")
            context = ssl.create_default_context(cafile=ca_file)
        else:
            context = ssl.create_default_context()
        self.mode = mode
        self.origin = normalized_origin or ""
        self.sha256 = str(sha256 or "").lower()
        self._ca_pem = ca_pem
        self.ssl_context = context

    def validate_exchange_url(self, url: str) -> None:
        request_origin = self.validate_cloud_url(url)
        if self.origin and request_origin != self.origin:
            raise CloudTrustOriginError(
                "Run context exchange does not match the enrolled Cloud origin"
            )

    def validate_cloud_url(self, url: str) -> str:
        request_origin = _request_origin(url)
        if request_origin is None:
            raise CloudTrustOriginError("Nexus Cloud requests must use HTTPS")
        return request_origin


class StaticCloudTrustResolver:
    """Advanced explicit Cloud CA override for non-bootstrap deployments."""

    def __init__(self, ca_file: str) -> None:
        self._policy = CloudTrustPolicy(mode="explicit-file", ca_file=ca_file)

    def open_cloud_request(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
        exchange: bool = False,
    ):
        self._policy.validate_cloud_url(request.full_url)
        if exchange:
            self._policy.validate_exchange_url(request.full_url)
        return urllib.request.urlopen(
            request, timeout=timeout, context=self._policy.ssl_context
        )


class TokenProvider(Protocol):
    """Return an access token without persisting it to disk."""

    @property
    def refreshable(self) -> bool:
        ...

    def get_token(self, *, force_refresh: bool = False) -> Optional[str]:
        ...


def resolve_environment_token(
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Read the canonical token variable and bounded legacy aliases."""

    source = os.environ if environ is None else environ
    configured = {
        name: value.strip()
        for name in (CANONICAL_TOKEN_ENV,) + LEGACY_TOKEN_ENVS
        if (value := source.get(name, "")).strip()
    }
    if not configured:
        return None
    values = set(configured.values())
    if len(values) != 1:
        raise NexusTokenAcquisitionError(
            "conflicting Nexus access tokens are configured in environment variables"
        )
    for name in LEGACY_TOKEN_ENVS:
        if name in configured:
            warnings.warn(
                f"{name} is deprecated; use {CANONICAL_TOKEN_ENV}",
                DeprecationWarning,
                stacklevel=2,
            )
    return next(iter(values))


class StaticTokenProvider:
    """Advanced/testing provider for one caller-supplied access token."""

    refreshable = False

    def __init__(self, token: str) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token must be a non-empty string")
        self._token = token.strip()

    def get_token(self, *, force_refresh: bool = False) -> str:
        del force_refresh
        return self._token


class NoTokenProvider:
    """Explicitly suppress Agent JWT and transaction-token headers."""

    refreshable = False

    def get_token(self, *, force_refresh: bool = False) -> None:
        del force_refresh
        return None


class EnvironmentTokenProvider:
    """Compatibility provider using NEXUS_AGENT_TOKEN and legacy aliases."""

    refreshable = False

    def __init__(
        self,
        *,
        required: bool = True,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._token = resolve_environment_token(environ)
        if required and self._token is None:
            raise NexusTokenAcquisitionError(
                f"{CANONICAL_TOKEN_ENV} is required but is not set"
            )

    def get_token(self, *, force_refresh: bool = False) -> Optional[str]:
        del force_refresh
        return self._token


@dataclass(frozen=True)
class RouterBootstrapMetadata:
    type: str
    endpoint: str
    token_ttl_seconds: int


@dataclass(frozen=True)
class RouterCloudTransportMetadata:
    direct_ipv6: bool
    relay: bool
    auto: bool


@dataclass(frozen=True)
class RouterCloudMetadata:
    connector_enabled: bool
    enrolled: bool
    status_endpoint: str
    transports: RouterCloudTransportMetadata
    manifest_schema_version: int = 1
    run_context_trust_delivery: str = ""


@dataclass(frozen=True)
class RouterAuthMetadata:
    schema_version: int
    required: bool
    auth_type: str
    issuer: Optional[str]
    audience: Optional[str]
    required_scopes: Mapping[str, Sequence[str]]
    bootstrap: Optional[RouterBootstrapMetadata] = None
    cloud: Optional[RouterCloudMetadata] = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RouterAuthMetadata":
        try:
            schema_version = int(value["schema_version"])
            authentication = value["authentication"]
            if not isinstance(authentication, Mapping):
                raise TypeError("authentication must be an object")
            required = authentication["required"]
            auth_type = authentication["type"]
            issuer = authentication.get("issuer")
            audience = authentication.get("audience")
            scopes = value["required_scopes"]
            bootstrap = value.get("bootstrap")
            cloud = value.get("cloud")
        except (KeyError, TypeError, ValueError) as exc:
            raise NexusAuthDiscoveryError(
                "router authentication metadata is incomplete"
            ) from exc
        if schema_version not in (1, 2) or not isinstance(required, bool):
            raise NexusAuthDiscoveryError(
                "router authentication metadata has an unsupported schema"
            )
        if auth_type not in ("none", "oauth2"):
            raise NexusAuthDiscoveryError(
                "router returned an unsupported authentication type"
            )
        if not isinstance(scopes, Mapping):
            raise NexusAuthDiscoveryError("required_scopes must be an object")
        normalized: Dict[str, Sequence[str]] = {}
        for operation, operation_scopes in scopes.items():
            if not isinstance(operation, str) or not isinstance(operation_scopes, list) \
                    or not all(isinstance(item, str) and item for item in operation_scopes):
                raise NexusAuthDiscoveryError("required_scopes contains invalid values")
            normalized[operation] = tuple(operation_scopes)
        if required:
            if auth_type != "oauth2" or not _absolute_url(issuer, https_only=True) \
                    or not isinstance(audience, str) or not audience:
                raise NexusAuthDiscoveryError(
                    "authenticated router metadata requires HTTPS issuer and audience"
                )
        bootstrap_metadata: Optional[RouterBootstrapMetadata] = None
        if bootstrap is not None:
            if schema_version < 2 or not isinstance(bootstrap, Mapping):
                raise NexusAuthDiscoveryError(
                    "bootstrap metadata requires schema_version 2 and an object"
                )
            bootstrap_type = bootstrap.get("type")
            endpoint = bootstrap.get("endpoint")
            ttl = bootstrap.get("token_ttl_seconds")
            endpoint_parts = urllib.parse.urlsplit(endpoint) \
                if isinstance(endpoint, str) else None
            if bootstrap_type != "trusted-lan" or endpoint_parts is None or (
                endpoint_parts.scheme
                or endpoint_parts.netloc
                or endpoint_parts.query
                or endpoint_parts.fragment
                or not endpoint_parts.path.startswith("/")
                or endpoint_parts.path.startswith("//")
            ):
                raise NexusAuthDiscoveryError(
                    "router returned invalid trusted-LAN bootstrap metadata"
                )
            if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 300:
                raise NexusAuthDiscoveryError(
                    "bootstrap token_ttl_seconds must be between 1 and 300"
                )
            bootstrap_metadata = RouterBootstrapMetadata(
                type=bootstrap_type,
                endpoint=endpoint_parts.path,
                token_ttl_seconds=ttl,
            )
        cloud_metadata: Optional[RouterCloudMetadata] = None
        if cloud is not None:
            if schema_version < 2 or not isinstance(cloud, Mapping):
                raise NexusAuthDiscoveryError(
                    "Cloud metadata requires schema_version 2 and an object"
                )
            connector_enabled = cloud.get("connector_enabled")
            enrolled = cloud.get("enrolled")
            status_endpoint = cloud.get("status_endpoint")
            transports = cloud.get("transports")
            manifest_schema_version = cloud.get("manifest_schema_version", 1)
            run_context_trust = cloud.get("run_context_trust")
            endpoint_parts = urllib.parse.urlsplit(status_endpoint) \
                if isinstance(status_endpoint, str) else None
            if (
                not isinstance(connector_enabled, bool)
                or not isinstance(enrolled, bool)
                or endpoint_parts is None
                or endpoint_parts.scheme
                or endpoint_parts.netloc
                or endpoint_parts.query
                or endpoint_parts.fragment
                or not endpoint_parts.path.startswith("/")
                or endpoint_parts.path.startswith("//")
                or not isinstance(transports, Mapping)
                or not isinstance(manifest_schema_version, int)
                or isinstance(manifest_schema_version, bool)
                or manifest_schema_version not in (1, 2, 3, 4)
            ):
                raise NexusAuthDiscoveryError(
                    "router returned invalid Cloud capability metadata"
                )
            trust_delivery = ""
            if run_context_trust is not None:
                if not isinstance(run_context_trust, Mapping) or (
                    run_context_trust.get("delivery") != "bootstrap-response"
                ):
                    raise NexusAuthDiscoveryError(
                        "router returned invalid Cloud Run context trust metadata"
                    )
                trust_delivery = "bootstrap-response"
            availability = {
                name: transports.get(name)
                for name in ("direct_ipv6", "relay", "auto")
            }
            if not all(isinstance(item, bool) for item in availability.values()):
                raise NexusAuthDiscoveryError(
                    "router returned invalid Cloud transport availability"
                )
            cloud_metadata = RouterCloudMetadata(
                connector_enabled=connector_enabled,
                enrolled=enrolled,
                status_endpoint=endpoint_parts.path,
                transports=RouterCloudTransportMetadata(**availability),
                manifest_schema_version=manifest_schema_version,
                run_context_trust_delivery=trust_delivery,
            )
        return cls(
            schema_version=schema_version,
            required=required,
            auth_type=auth_type,
            issuer=issuer if isinstance(issuer, str) else None,
            audience=audience if isinstance(audience, str) else None,
            required_scopes=normalized,
            bootstrap=bootstrap_metadata,
            cloud=cloud_metadata,
        )


def _absolute_url(value: Any, *, https_only: bool) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in (("https",) if https_only else ("http", "https")):
        return False
    return bool(parsed.netloc) and not parsed.username and not parsed.password \
        and not parsed.query and not parsed.fragment


def _json_request(
    request: urllib.request.Request,
    *,
    timeout: float,
    ssl_context: Optional[ssl.SSLContext],
    opener: Optional[Any] = None,
    error_type: type,
) -> Mapping[str, Any]:
    try:
        response = opener.open(request, timeout=timeout) if opener is not None else \
            urllib.request.urlopen(request, timeout=timeout, context=ssl_context)
        with response:
            raw = response.read(262145)
        if len(raw) > 262144:
            raise error_type("authentication response exceeds 256 KiB")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("response is not an object")
        return value
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(16385)
            detail = ""
            if len(raw) <= 16384:
                try:
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        detail = str(payload.get("error_description") or payload.get("error") or "")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
        finally:
            exc.close()
        suffix = f": {detail}" if detail else ""
        raise error_type(f"authentication endpoint returned HTTP {exc.code}{suffix}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise error_type(f"authentication endpoint is unavailable: {reason}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, error_type):
            raise
        raise error_type("authentication endpoint returned invalid JSON") from exc


def discover_router_auth(
    router_url: str,
    *,
    timeout: float = 10.0,
    ca_file: Optional[str] = None,
    use_environment_proxy: bool = True,
) -> RouterAuthMetadata:
    if not _absolute_url(router_url, https_only=False):
        raise ValueError("router_url must be an absolute http(s) URL without credentials")
    context = ssl.create_default_context(cafile=ca_file) \
        if router_url.startswith("https://") else None
    opener = None if use_environment_proxy else urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        *([urllib.request.HTTPSHandler(context=context)] if context is not None else []),
    )
    request = urllib.request.Request(
        router_url.rstrip("/") + "/agent/v1/authentication",
        headers={"Accept": "application/json", "User-Agent": "nexus-agent-sdk-python/0.35.0"},
        method="GET",
    )
    value = _json_request(
        request,
        timeout=timeout,
        ssl_context=context,
        opener=opener,
        error_type=NexusAuthDiscoveryError,
    )
    return RouterAuthMetadata.from_dict(value)


class OIDCClientCredentialsProvider:
    """Acquire and refresh short-lived access tokens using OIDC discovery."""

    refreshable = True

    def __init__(
        self,
        *,
        issuer: str,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        client_id_env: str = "NEXUS_AGENT_CLIENT_ID",
        client_secret_env: str = "NEXUS_AGENT_CLIENT_SECRET",
        audience: Optional[str] = None,
        scopes: Sequence[str] = ("agent.route", "agent.invoke", "agent.register"),
        token_endpoint: Optional[str] = None,
        timeout: float = 10.0,
        refresh_skew_seconds: int = 30,
        ca_file: Optional[str] = None,
        allow_insecure_http: bool = False,
        use_environment_proxy: bool = True,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        source = os.environ if environ is None else environ
        client_id = client_id or source.get(client_id_env)
        client_secret = client_secret or source.get(client_secret_env)
        if not isinstance(client_id, str) or not client_id:
            raise NexusTokenAcquisitionError(f"OIDC client ID is missing ({client_id_env})")
        if not isinstance(client_secret, str) or not client_secret:
            raise NexusTokenAcquisitionError(
                f"OIDC client secret is missing ({client_secret_env})"
            )
        if not _absolute_url(issuer, https_only=not allow_insecure_http):
            raise ValueError("issuer must be an absolute HTTPS URL")
        if token_endpoint is not None and not _absolute_url(
            token_endpoint, https_only=not allow_insecure_http
        ):
            raise ValueError("token_endpoint must be an absolute HTTPS URL")
        if not isinstance(refresh_skew_seconds, int) or not 0 <= refresh_skew_seconds <= 300:
            raise ValueError("refresh_skew_seconds must be between 0 and 300")
        if not scopes or not all(isinstance(scope, str) and scope.strip() for scope in scopes):
            raise ValueError("scopes must contain non-empty strings")
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self._client_secret = client_secret
        self.audience = audience
        self.scopes = tuple(dict.fromkeys(scope.strip() for scope in scopes))
        self._token_endpoint = token_endpoint
        self.timeout = timeout
        self.refresh_skew_seconds = refresh_skew_seconds
        self.ssl_context = ssl.create_default_context(cafile=ca_file) \
            if self.issuer.startswith("https://") else None
        self._opener = None if use_environment_proxy else urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            *([urllib.request.HTTPSHandler(context=self.ssl_context)]
              if self.ssl_context is not None else []),
        )
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def _discover_token_endpoint(self) -> str:
        if self._token_endpoint is not None:
            return self._token_endpoint
        request = urllib.request.Request(
            self.issuer + "/.well-known/openid-configuration",
            headers={"Accept": "application/json"},
            method="GET",
        )
        value = _json_request(
            request,
            timeout=self.timeout,
            ssl_context=self.ssl_context,
            opener=self._opener,
            error_type=NexusAuthDiscoveryError,
        )
        endpoint = value.get("token_endpoint")
        if not _absolute_url(
            endpoint,
            https_only=self.issuer.startswith("https://"),
        ):
            raise NexusAuthDiscoveryError("OIDC discovery returned an invalid token endpoint")
        self._token_endpoint = str(endpoint)
        return self._token_endpoint

    def _acquire(self) -> None:
        fields: MutableMapping[str, str] = {
            "grant_type": "client_credentials",
            "scope": " ".join(self.scopes),
        }
        if self.audience:
            fields["audience"] = self.audience
        encoded_id = urllib.parse.quote_plus(self.client_id)
        encoded_secret = urllib.parse.quote_plus(self._client_secret)
        basic = base64.b64encode(f"{encoded_id}:{encoded_secret}".encode()).decode("ascii")
        request = urllib.request.Request(
            self._discover_token_endpoint(),
            data=urllib.parse.urlencode(fields).encode("ascii"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": "Basic " + basic,
            },
            method="POST",
        )
        value = _json_request(
            request,
            timeout=self.timeout,
            ssl_context=self.ssl_context,
            opener=self._opener,
            error_type=NexusTokenAcquisitionError,
        )
        token = value.get("access_token")
        token_type = value.get("token_type", "Bearer")
        try:
            expires_in = int(value["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NexusTokenAcquisitionError(
                "OIDC token response is missing a valid expires_in"
            ) from exc
        if not isinstance(token, str) or not token or str(token_type).lower() != "bearer" \
                or not 1 <= expires_in <= 86400:
            raise NexusTokenAcquisitionError("OIDC token response is invalid")
        self._token = token
        self._expires_at = time.monotonic() + expires_in

    def get_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            fresh = self._token is not None and time.monotonic() < (
                self._expires_at - self.refresh_skew_seconds
            )
            if force_refresh or not fresh:
                self._acquire()
            assert self._token is not None
            return self._token


class RouterLanSessionProvider:
    """Acquire source-bound, short-lived credentials from a trusted LAN router."""

    refreshable = True

    def __init__(
        self,
        *,
        router_url: str,
        endpoint: str,
        advertised_ttl_seconds: int,
        tenant: str,
        origin: str,
        scopes: Sequence[str],
        timeout: float = 10.0,
        ca_file: Optional[str] = None,
        use_environment_proxy: bool = True,
    ) -> None:
        if not _absolute_url(router_url, https_only=False):
            raise ValueError("router_url must be an absolute http(s) URL")
        endpoint_parts = urllib.parse.urlsplit(endpoint)
        if (
            endpoint_parts.scheme
            or endpoint_parts.netloc
            or endpoint_parts.query
            or endpoint_parts.fragment
            or not endpoint_parts.path.startswith("/")
            or endpoint_parts.path.startswith("//")
        ):
            raise ValueError("bootstrap endpoint must be a same-origin absolute path")
        if not isinstance(advertised_ttl_seconds, int) or not (
            1 <= advertised_ttl_seconds <= 300
        ):
            raise ValueError("advertised_ttl_seconds must be between 1 and 300")
        if not isinstance(tenant, str) or not tenant or not isinstance(origin, str) or not origin:
            raise ValueError("tenant and origin must be non-empty strings")
        if not scopes or not all(isinstance(scope, str) and scope.strip() for scope in scopes):
            raise ValueError("scopes must contain non-empty strings")
        router_parts = urllib.parse.urlsplit(router_url)
        self.endpoint = urllib.parse.urlunsplit(
            (router_parts.scheme, router_parts.netloc, endpoint_parts.path, "", "")
        )
        self.advertised_ttl_seconds = advertised_ttl_seconds
        self.tenant = tenant
        self.origin = origin
        self.scopes = tuple(dict.fromkeys(scope.strip() for scope in scopes))
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context(cafile=ca_file) \
            if router_parts.scheme == "https" else None
        self._opener = None if use_environment_proxy else urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            *([urllib.request.HTTPSHandler(context=self.ssl_context)]
              if self.ssl_context is not None else []),
        )
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._refresh_skew_seconds = 1.0
        self._cloud_trust: Optional[CloudTrustPolicy] = None
        self._cloud_trust_error = ""
        self._lock = threading.Lock()

    @property
    def cloud_trust(self) -> Optional[CloudTrustPolicy]:
        return self._cloud_trust

    @staticmethod
    def _parse_cloud_trust(value: Any) -> Optional[CloudTrustPolicy]:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise NexusTokenAcquisitionError("LAN bootstrap Cloud trust is invalid")
        mode = value.get("mode")
        origin = value.get("origin")
        if mode == "system":
            if value.get("ca_pem") not in (None, "") or value.get("sha256") not in (None, ""):
                raise NexusTokenAcquisitionError(
                    "system Cloud trust must not include a CA bundle"
                )
            return CloudTrustPolicy(mode="system", origin=str(origin or ""))
        if mode == "pinned-pem":
            return CloudTrustPolicy(
                mode="pinned-pem",
                origin=str(origin or ""),
                sha256=str(value.get("sha256") or ""),
                ca_pem=str(value.get("ca_pem") or ""),
            )
        raise NexusTokenAcquisitionError("LAN bootstrap Cloud trust mode is invalid")

    def _acquire(self) -> None:
        body = json.dumps(
            {
                "tenant": self.tenant,
                "origin": self.origin,
                "scopes": list(self.scopes),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "nexus-agent-sdk-python/0.35.0",
            },
            method="POST",
        )
        value = _json_request(
            request,
            timeout=self.timeout,
            ssl_context=self.ssl_context,
            opener=self._opener,
            error_type=NexusTokenAcquisitionError,
        )
        cloud_trust = None
        cloud_trust_error = ""
        try:
            cloud_trust = self._parse_cloud_trust(value.get("cloud_trust"))
        except NexusTokenAcquisitionError as exc:
            # Cloud publication is optional; malformed Cloud trust must not
            # prevent a source-bound LAN session from registering locally.
            cloud_trust_error = str(exc)
        token = value.get("access_token")
        token_type = value.get("token_type")
        identity = value.get("identity")
        returned_scope = value.get("scope")
        try:
            expires_in = int(value["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NexusTokenAcquisitionError(
                "LAN bootstrap response is missing a valid expires_in"
            ) from exc
        returned_scopes = set(returned_scope.split()) \
            if isinstance(returned_scope, str) else set()
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or not isinstance(identity, Mapping)
            or identity.get("tenant") != self.tenant
            or identity.get("origin") != self.origin
            or not set(self.scopes).issubset(returned_scopes)
            or not 1 <= expires_in <= self.advertised_ttl_seconds
        ):
            raise NexusTokenAcquisitionError("LAN bootstrap response is invalid")
        self._token = token
        self._expires_at = time.monotonic() + expires_in
        self._refresh_skew_seconds = min(30.0, max(1.0, expires_in / 5.0))
        self._cloud_trust = cloud_trust
        self._cloud_trust_error = cloud_trust_error

    def get_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            fresh = self._token is not None and time.monotonic() < (
                self._expires_at - self._refresh_skew_seconds
            )
            if force_refresh or not fresh:
                self._acquire()
            assert self._token is not None
            return self._token


class AutoTokenProvider:
    """Use explicit token, trusted-LAN bootstrap, OIDC, then router no-auth."""

    def __init__(
        self,
        router_url: str,
        *,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        scopes: Sequence[str] = ("agent.route", "agent.invoke", "agent.register"),
        timeout: float = 10.0,
        router_ca_file: Optional[str] = None,
        issuer_ca_file: Optional[str] = None,
        use_environment_proxy: bool = True,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.router_url = router_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes
        self.timeout = timeout
        self.router_ca_file = router_ca_file
        self.issuer_ca_file = issuer_ca_file
        self.use_environment_proxy = use_environment_proxy
        self.environ = os.environ if environ is None else environ
        token = resolve_environment_token(self.environ)
        self._provider: Optional[TokenProvider] = (
            StaticTokenProvider(token) if token is not None else None
        )
        self._metadata: Optional[RouterAuthMetadata] = None
        self._identity: Optional[tuple[str, str]] = None
        self._lock = threading.Lock()

    @property
    def refreshable(self) -> bool:
        return bool(self._provider is None or getattr(self._provider, "refreshable", False))

    @property
    def metadata(self) -> Optional[RouterAuthMetadata]:
        """Return metadata captured by the same discovery used for auto auth."""

        with self._lock:
            self._initialize()
            return self._metadata

    def bind_registration(self, *, tenant: str, origin: str) -> None:
        """Bind this provider to the first Agent identity registered through it."""

        if not isinstance(tenant, str) or not tenant or not isinstance(origin, str) or not origin:
            raise NexusTokenAcquisitionError(
                "automatic authentication requires non-empty registration tenant and origin"
            )
        identity = (tenant, origin)
        with self._lock:
            if self._identity is not None and self._identity != identity:
                raise NexusTokenAcquisitionError(
                    "one automatic authentication client cannot mix tenant/origin identities"
                )
            self._identity = identity

    def _initialize(self) -> None:
        if self._provider is not None:
            return
        metadata = discover_router_auth(
            self.router_url,
            timeout=self.timeout,
            ca_file=self.router_ca_file,
            use_environment_proxy=self.use_environment_proxy,
        )
        self._metadata = metadata
        if metadata.bootstrap is not None:
            if self._identity is None:
                raise NexusTokenAcquisitionError(
                    "trusted-LAN bootstrap requires registration identity binding first"
                )
            tenant, origin = self._identity
            self._provider = RouterLanSessionProvider(
                router_url=self.router_url,
                endpoint=metadata.bootstrap.endpoint,
                advertised_ttl_seconds=metadata.bootstrap.token_ttl_seconds,
                tenant=tenant,
                origin=origin,
                scopes=self.scopes,
                timeout=self.timeout,
                ca_file=self.router_ca_file,
                use_environment_proxy=self.use_environment_proxy,
            )
            return
        if not metadata.required:
            self._provider = NoTokenProvider()
            return
        assert metadata.issuer is not None
        operation_scopes = set(self.scopes)
        self._provider = OIDCClientCredentialsProvider(
            issuer=metadata.issuer,
            client_id=self.client_id,
            client_secret=self.client_secret,
            audience=metadata.audience,
            scopes=tuple(sorted(operation_scopes)),
            timeout=self.timeout,
            ca_file=self.issuer_ca_file,
            use_environment_proxy=self.use_environment_proxy,
            environ=self.environ,
        )

    def get_token(self, *, force_refresh: bool = False) -> Optional[str]:
        with self._lock:
            self._initialize()
            assert self._provider is not None
            provider = self._provider
        return provider.get_token(force_refresh=force_refresh)

    def _cloud_policy(self, *, force_refresh: bool = False) -> Optional[CloudTrustPolicy]:
        with self._lock:
            self._initialize()
            provider = self._provider
        if not isinstance(provider, RouterLanSessionProvider):
            return None
        provider.get_token(force_refresh=force_refresh)
        return provider.cloud_trust

    def open_cloud_request(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
        exchange: bool = False,
    ):
        policy = self._cloud_policy()
        if policy is not None:
            policy.validate_cloud_url(request.full_url)
            if exchange:
                policy.validate_exchange_url(request.full_url)
        try:
            return urllib.request.urlopen(
                request,
                timeout=timeout,
                context=policy.ssl_context if policy is not None else None,
            )
        except (urllib.error.URLError, OSError) as exc:
            if not _certificate_failure(exc):
                raise
            if policy is None:
                raise CloudTrustUnavailableError(
                    "Cloud TLS trust is unavailable; upgrade the OpenWrt Router "
                    "or configure NexusAgent(cloud_ca_file=...)"
                ) from exc
            refreshed = self._cloud_policy(force_refresh=True)
            if refreshed is None:
                raise CloudTrustUnavailableError(
                    "Cloud TLS trust is unavailable after Router refresh; upgrade "
                    "the OpenWrt Router or configure NexusAgent(cloud_ca_file=...)"
                ) from exc
            refreshed.validate_cloud_url(request.full_url)
            if exchange:
                refreshed.validate_exchange_url(request.full_url)
            try:
                return urllib.request.urlopen(
                    request, timeout=timeout, context=refreshed.ssl_context
                )
            except (urllib.error.URLError, OSError) as retry_exc:
                if _certificate_failure(retry_exc):
                    raise CloudTrustVerificationError(
                        "the Router-provided Cloud CA or hostname could not verify "
                        "Nexus Cloud"
                    ) from retry_exc
                raise
