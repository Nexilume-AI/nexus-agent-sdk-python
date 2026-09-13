"""Deployment-owned Cloud trust; never accept CA material from MCP headers."""
import json
from functools import lru_cache
from urllib.request import HTTPRedirectHandler, HTTPSHandler, build_opener

from .auth import CloudTrustPolicy, _certificate_failure


TRUST_ENV = "NEXUS_HOSTED_CLOUD_TRUST"


class HostedCloudTrustError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Delegate tokens must never follow a redirect, even to another trusted origin.
        raise HostedCloudTrustError("RUN_CONTEXT_ORIGIN_MISMATCH", "Cloud callback redirect was refused.")


@lru_cache(maxsize=4)
def _resolver(raw):
    try:
        if len(raw.encode("utf-8")) > 24576:
            raise ValueError()
        value = json.loads(raw)
        origins = value["origins"]
        if value.get("schema_version") != 1 or value.get("mode") not in {"system", "pinned-pem"}:
            raise ValueError()
        if not isinstance(origins, list) or not 1 <= len(origins) <= 4:
            raise ValueError()
        policies = [CloudTrustPolicy(mode=value["mode"], origin=origin,
                    ca_pem=value.get("ca_pem", ""), sha256=value.get("sha256", "")) for origin in origins]
    except Exception:
        raise HostedCloudTrustError("RUN_CONTEXT_TRUST_UNAVAILABLE", "Managed Cloud trust configuration is invalid.") from None
    policy = policies[0]
    allowed = {item.origin for item in policies}
    opener = build_opener(HTTPSHandler(context=policy.ssl_context), _NoRedirect())

    def open_cloud_request(request, *, timeout, exchange=False):
        try:
            origin = policy.validate_cloud_url(request.full_url)
            if origin not in allowed:
                raise ValueError()
        except ValueError:
            raise HostedCloudTrustError("RUN_CONTEXT_ORIGIN_MISMATCH", "Cloud callback does not match the managed Cloud origins.") from None
        try:
            # No retry: the request may carry a one-time token or a side effect.
            return opener.open(request, timeout=timeout)
        except Exception as exc:
            if _certificate_failure(exc):
                raise HostedCloudTrustError("RUN_CONTEXT_TLS_FAILED", "Cloud certificate or hostname could not be verified.") from None
            raise
    return open_cloud_request


def hosted_cloud_opener(environ):
    raw = environ.get(TRUST_ENV, "")
    return _resolver(raw) if raw else None
