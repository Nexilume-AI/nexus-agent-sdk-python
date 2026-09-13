import io
import json
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

from nexus_agent.browser import (
    NexusBrowserError, NexusBrowserComputerRequired, NexusBrowserPermissionRequired,
    NexusBrowserTunnelUnavailable,
)
from nexus_agent.reporting import NexusComputerError, NexusRunContext
from nexus_agent.server import _managed_service_error


def context():
    return NexusRunContext(run_id="run", browser_enabled=True,
        browser_delegate_url="https://cloud.test/browser/", browser_delegate_token="private-browser-token",
        workspace_delegate_url="https://cloud.test/workspace/", workspace_delegate_token="private-workspace-token")


@pytest.mark.parametrize("code", ["COMPUTER_RUNTIME_OFFLINE", "COMPUTER_RUNTIME_REVOKED", "COMPUTER_CAPABILITY_UNAVAILABLE"])
@pytest.mark.parametrize("operation", ["browser", "workspace"])
def test_computer_error_survives_delegate_and_handler_wrappers(code, operation):
    ctx = context()
    error = HTTPError("https://cloud.test", 503, "unavailable", {}, io.BytesIO(json.dumps({
        "ok": False, "error": {"code": code, "message": "secret endpoint diagnostic"},
    }).encode()))
    with patch.object(ctx, "_open_cloud", side_effect=error):
        with pytest.raises((NexusBrowserError, NexusComputerError)) as result:
            if operation == "browser":
                ctx._browser_request("observe", {})
            else:
                ctx._delegate_request("files/", method="GET")
    actual, message = _managed_service_error(result.value)
    assert actual == code
    assert "secret" not in message
    assert "private-browser-token" not in message


@pytest.mark.parametrize("operation", ["browser", "workspace"])
def test_cloud_delegate_network_failure_is_not_labeled_device_failure(operation):
    ctx = context()
    with patch.object(ctx, "_open_cloud", side_effect=URLError("private network detail")):
        with pytest.raises((NexusBrowserError, NexusComputerError)) as result:
            if operation == "browser":
                ctx._browser_request("observe", {})
            else:
                ctx._delegate_request("files/", method="GET")
    assert _managed_service_error(result.value) == ("RUN_DELEGATE_UNAVAILABLE", "The Run delegate operation is unavailable.")


@pytest.mark.parametrize("kind,code", [
    (NexusBrowserComputerRequired, "BROWSER_COMPUTER_REQUIRED"),
    (NexusBrowserPermissionRequired, "BROWSER_PERMISSION_REQUIRED"),
    (NexusBrowserTunnelUnavailable, "BROWSER_TUNNEL_UNAVAILABLE"),
])
def test_specific_browser_recovery_is_not_shadowed_by_unavailable_parent(kind, code):
    assert _managed_service_error(kind("safe message"))[0] == code


def test_arbitrary_exception_cannot_claim_a_trusted_component_code():
    error = RuntimeError("secret")
    error.code = "COMPUTER_RUNTIME_OFFLINE"
    assert _managed_service_error(error) == ("", "")
    assert _managed_service_error(NexusComputerError("safe", code="UNRECOGNIZED"))[0] == "WORKSPACE_UNAVAILABLE"
