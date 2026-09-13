"""Inbound authentication policies for directly callable Python Agents."""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple

from .models import AgentEnvelope


class ServerAuthenticationError(Exception):
    """Authentication failure suitable for a bounded HTTP response."""

    def __init__(self, code: str, message: str, *, status: int = 401) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AuthenticatedCaller:
    subject: Optional[str]
    scopes: Tuple[str, ...] = ()
    claims: Mapping[str, Any] = field(default_factory=dict)


class ServerAuthPolicy(Protocol):
    mode: str

    def authenticate(
        self,
        authorization: Optional[str],
        envelope: AgentEnvelope,
    ) -> AuthenticatedCaller:
        ...


class NoServerAuth:
    """Explicitly accept direct calls without an Authorization header."""

    mode = "none"

    def authenticate(
        self,
        authorization: Optional[str],
        envelope: AgentEnvelope,
    ) -> AuthenticatedCaller:
        del authorization
        return AuthenticatedCaller(subject=None)


def _unique_object(pairs):
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JWT object key")
        result[key] = value
    return result


def _decode_segment(value: str) -> bytes:
    if not value or any(character not in
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                        for character in value):
        raise ValueError("invalid JWT base64url segment")
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _encode_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _numeric_date(claims: Mapping[str, Any], name: str) -> float:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ServerAuthenticationError("INVALID_TOKEN", f"JWT {name} is required")
    return float(value)


class HmacJwtServerAuth:
    """Dependency-free HS256 validation for a self-hosted direct Agent.

    This mode is intended for deployments where the caller and Agent share a
    dedicated 256-bit secret. OIDC/JWKS asymmetric validation remains a future
    server policy rather than silently treating an access token as trusted.
    """

    mode = "jwt-hs256"

    def __init__(
        self,
        secret: str,
        *,
        issuer: str,
        audience: str,
        required_scope: str = "agent.invoke",
        clock_skew_seconds: int = 30,
        max_lifetime_seconds: int = 3600,
        bind_tenant: bool = True,
        bind_source_agent: bool = True,
    ) -> None:
        if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
            raise ValueError("JWT secret must contain at least 32 UTF-8 bytes")
        if not issuer or not audience:
            raise ValueError("JWT issuer and audience are required")
        if not 0 <= clock_skew_seconds <= 300:
            raise ValueError("clock_skew_seconds must be between 0 and 300")
        if not 1 <= max_lifetime_seconds <= 86400:
            raise ValueError("max_lifetime_seconds must be between 1 and 86400")
        self._secret = secret.encode("utf-8")
        self.issuer = issuer
        self.audience = audience
        self.required_scope = required_scope
        self.clock_skew_seconds = clock_skew_seconds
        self.max_lifetime_seconds = max_lifetime_seconds
        self.bind_tenant = bool(bind_tenant)
        self.bind_source_agent = bool(bind_source_agent)

    def authenticate(
        self,
        authorization: Optional[str],
        envelope: AgentEnvelope,
    ) -> AuthenticatedCaller:
        if not authorization or not authorization.startswith("Bearer "):
            raise ServerAuthenticationError(
                "AUTHENTICATION_REQUIRED", "a Bearer JWT is required"
            )
        token = authorization[7:]
        if not token or len(token) > 8192 or token != token.strip():
            raise ServerAuthenticationError("INVALID_TOKEN", "Bearer JWT is invalid")
        parts = token.split(".")
        if len(parts) != 3:
            raise ServerAuthenticationError("INVALID_TOKEN", "JWT must have three segments")
        try:
            header = json.loads(
                _decode_segment(parts[0]).decode("utf-8"),
                object_pairs_hook=_unique_object,
            )
            claims = json.loads(
                _decode_segment(parts[1]).decode("utf-8"),
                object_pairs_hook=_unique_object,
            )
            signature = _decode_segment(parts[2])
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise ServerAuthenticationError("INVALID_TOKEN", "JWT encoding is invalid") from exc
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise ServerAuthenticationError("INVALID_TOKEN", "JWT objects are invalid")
        if header.get("alg") != "HS256" or header.get("typ", "JWT") != "JWT":
            raise ServerAuthenticationError("INVALID_TOKEN", "JWT algorithm must be HS256")
        signed = f"{parts[0]}.{parts[1]}".encode("ascii")
        expected = hmac.new(self._secret, signed, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ServerAuthenticationError("INVALID_TOKEN", "JWT signature is invalid")

        now = time.time()
        issued = _numeric_date(claims, "iat")
        expires = _numeric_date(claims, "exp")
        not_before = claims.get("nbf", issued)
        if isinstance(not_before, bool) or not isinstance(not_before, (int, float)):
            raise ServerAuthenticationError("INVALID_TOKEN", "JWT nbf is invalid")
        skew = float(self.clock_skew_seconds)
        if issued > now + skew or float(not_before) > now + skew or expires <= now - skew:
            raise ServerAuthenticationError("TOKEN_EXPIRED", "JWT is outside its valid time window")
        if expires <= issued or expires - issued > self.max_lifetime_seconds:
            raise ServerAuthenticationError("INVALID_TOKEN", "JWT lifetime is invalid")
        if claims.get("iss") != self.issuer:
            raise ServerAuthenticationError("INVALID_TOKEN", "JWT issuer does not match")
        audience = claims.get("aud")
        audiences = (audience,) if isinstance(audience, str) else audience
        if not isinstance(audiences, (list, tuple)) or self.audience not in audiences:
            raise ServerAuthenticationError("INVALID_TOKEN", "JWT audience does not match")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise ServerAuthenticationError("INVALID_TOKEN", "JWT subject is invalid")
        scope_value = claims.get("scope", "")
        if not isinstance(scope_value, str):
            raise ServerAuthenticationError("INVALID_TOKEN", "JWT scope is invalid")
        scopes = tuple(sorted(set(scope_value.split())))
        if self.required_scope and self.required_scope not in scopes:
            raise ServerAuthenticationError(
                "INSUFFICIENT_SCOPE",
                f"JWT requires scope {self.required_scope}",
                status=403,
            )
        if self.bind_tenant and claims.get("tenant") != envelope.tenant:
            raise ServerAuthenticationError(
                "CALLER_MISMATCH", "JWT tenant does not match the Envelope", status=403
            )
        if self.bind_source_agent and claims.get("source_agent") != envelope.source_agent:
            raise ServerAuthenticationError(
                "CALLER_MISMATCH",
                "JWT source_agent does not match the Envelope",
                status=403,
            )
        return AuthenticatedCaller(subject=subject, scopes=scopes, claims=dict(claims))

    def issue(
        self,
        *,
        subject: str,
        tenant: str,
        source_agent: str,
        expires_in: int = 300,
        scopes: Tuple[str, ...] = ("agent.invoke",),
        now: Optional[int] = None,
    ) -> str:
        """Issue a bounded HS256 token for local/testing deployments."""

        if not 1 <= expires_in <= self.max_lifetime_seconds:
            raise ValueError("expires_in exceeds the configured JWT lifetime")
        issued = int(time.time()) if now is None else int(now)
        header = {"alg": "HS256", "typ": "JWT"}
        claims = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": subject,
            "iat": issued,
            "exp": issued + expires_in,
            "scope": " ".join(scopes),
            "tenant": tenant,
            "source_agent": source_agent,
        }
        encoded_header = _encode_segment(json.dumps(
            header, separators=(",", ":"), sort_keys=True
        ).encode("utf-8"))
        encoded_claims = _encode_segment(json.dumps(
            claims, separators=(",", ":"), sort_keys=True
        ).encode("utf-8"))
        signed = f"{encoded_header}.{encoded_claims}"
        signature = hmac.new(
            self._secret, signed.encode("ascii"), hashlib.sha256
        ).digest()
        return f"{signed}.{_encode_segment(signature)}"
