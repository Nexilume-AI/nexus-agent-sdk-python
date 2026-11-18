"""Dependency-free command line for the OpenWrt SDK's model device flow."""
import argparse
import getpass
import json
import os
from pathlib import Path
import socket
import sys
import time

from .relay_models import RelayModelClient, RelayModelError
from . import __version__


class UsageError(ValueError):
    """Safe, authored diagnostics; never includes raw credentials or file data."""


class CLIParser(argparse.ArgumentParser):
    def error(self, message):
        if message.startswith("unrecognized arguments:"):
            message = "unrecognized arguments; see --help. Pass endpoint keys only via hidden prompt or stdin"
        super().error(message)


def _seconds(value):
    try:
        number = int(value)
        if 1 <= number <= 600:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("Use an integer from 1 to 600 seconds")


def parser():
    common = CLIParser(add_help=False)
    common.add_argument("--cloud", default=argparse.SUPPRESS, help="HTTPS Cloud origin (or NEXUS_RELAY_CLOUD)")
    common.add_argument("--device", default=argparse.SUPPRESS, help="Stable device name (or NEXUS_RELAY_DEVICE; default hostname)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Machine-readable JSON output")
    common.add_argument("--timeout", type=_seconds, default=argparse.SUPPRESS, help="HTTP timeout in seconds (default 90)")
    root = CLIParser(prog="nexus-relay", parents=[common],
        description="OpenWrt Python SDK device login and model registration. No nexus_cli required.")
    root.add_argument("--version", action="version", version="%(prog)s " + __version__)
    commands = root.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", parents=[common], help="Request browser-confirmed device authorization")
    login.add_argument("--model", help="Exact model ID to authorize")
    login.add_argument("--name", help="Display name; defaults to model ID")
    login.add_argument("--endpoint", help="Existing authenticated HTTPS model endpoint")
    login.add_argument("--protocol", choices=["system_one", "openai-compatible"], help="Default: openai-compatible")
    login.add_argument("--outbound", action="store_true", help="Forward the approved upstream through this device")
    login.add_argument("--provider-id", help="Adopt an existing Cloud Provider without changing its key or Router bindings")
    login.add_argument("--models-json", type=Path, help="Optional JSON array of model/endpoint declarations (not credentials)")
    login.add_argument("--resume", action="store_true", help="Continue a pending login without creating a new code")
    login.add_argument("--no-wait", action="store_true", help="Print the code/URL and exit; later use login --resume")
    login.add_argument("--wait-timeout", type=_seconds, default=600, help="Maximum wait for browser confirmation (default 600)")
    model = commands.add_parser("model", parents=[common], help="Manage approved model registration")
    actions = model.add_subparsers(dest="model_action", required=True)
    register = actions.add_parser("register", parents=[common], help="Idempotently register an approved model")
    register.add_argument("model", help="Exact browser-approved model ID")
    activate = actions.add_parser('activate', parents=[common], help='Verify a running tunnel and activate its registered model binding')
    activate.add_argument('model', help='An already registered outbound model')
    commands.add_parser('serve', parents=[common], help='Run outbound WSS forwarding with automatic reconnect (requires nexilume[relay])')
    keys = register.add_mutually_exclusive_group()
    keys.add_argument("--endpoint-key-stdin", action="store_true", help="Import the existing endpoint key once from stdin")
    keys.add_argument("--prompt-endpoint-key", action="store_true", help="Import the endpoint key once using a hidden terminal prompt")
    commands.add_parser("status", parents=[common], help="Show approved models and Cloud registration records")
    commands.add_parser("refresh", parents=[common], help="Rotate device credentials without printing them")
    revoke = commands.add_parser("revoke", parents=[common], help="Revoke this device and disable its model sources")
    revoke.add_argument("--yes", action="store_true", help="Confirm revocation without an interactive prompt")
    return root


def _manifest(args):
    inline = any(getattr(args, name) for name in ("model", "name", "endpoint", "protocol", "outbound", "provider_id"))
    if args.resume:
        if inline or args.models_json or args.no_wait:
            raise UsageError("--resume cannot be combined with new declarations or --no-wait")
        return None
    if args.models_json:
        if inline:
            raise UsageError("Use --models-json or inline model flags, not both")
        with args.models_json.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise UsageError("Model declarations exceed 64 KiB")
        try:
            models = json.loads(raw)
        except (ValueError, UnicodeError):
            raise UsageError("Model declarations must be valid JSON") from None
    else:
        if not args.model or not args.endpoint:
            raise UsageError("login requires --model and --endpoint, or --models-json, or --resume")
        models = [{"model": args.model, "name": args.name or args.model,
            "endpoint": args.endpoint, "protocol": args.protocol or "openai-compatible"}]
        if args.outbound:models[0]['transport'] = 'outbound'
        if args.provider_id:models[0]['provider_id'] = args.provider_id
    if not isinstance(models, list) or not 1 <= len(models) <= 32:
        raise UsageError("Provide an array of 1 to 32 model declarations")
    allowed = {"name", "model", "endpoint", "protocol"}
    for item in models:
        if (not isinstance(item, dict) or not allowed.issubset(item) or set(item) - allowed - {'transport', 'provider_id'}
                or not all(isinstance(value, str) and value.strip() for value in item.values())):
            raise UsageError("Each declaration needs only name, model, endpoint and protocol; no credentials")
        if item.get('transport') not in (None, 'outbound') or (item.get('provider_id') and item.get('transport') != 'outbound'):
            raise UsageError('Provider adoption requires outbound transport')
    if len({item["model"] for item in models}) != len(models):
        raise UsageError("Model IDs must be distinct")
    return models


def _endpoint_key(args):
    if args.endpoint_key_stdin:
        if sys.stdin.isatty():
            raise UsageError("Pipe a key from your secret manager, or use --prompt-endpoint-key")
        key = sys.stdin.read(8194).strip()
    elif args.prompt_endpoint_key:
        if not sys.stdin.isatty():
            raise UsageError("The hidden key prompt requires an interactive terminal")
        key = getpass.getpass("Existing endpoint API key (hidden): ", stream=sys.stderr).strip()
    else:
        return None
    if not key or len(key) > 8192 or any(ord(char) < 33 for char in key):
        raise UsageError("Invalid endpoint key; it must be nonempty and contain no whitespace")
    return key


def _print(value, machine=False):
    print(json.dumps(value, ensure_ascii=True, indent=None if machine else 2), flush=True)


def main(argv=None):
    args = parser().parse_args(argv)
    machine = getattr(args, "json", False)
    try:
        # Validate before creating any SDK storage or contacting Cloud.
        models = _manifest(args) if args.command == "login" else None
        cloud = getattr(args, "cloud", os.environ.get("NEXUS_RELAY_CLOUD", "https://cloud.nexilume.com"))
        device = getattr(args, "device", os.environ.get("NEXUS_RELAY_DEVICE", socket.gethostname()))
        client = RelayModelClient(cloud, device, timeout=getattr(args, "timeout", 90))
        if args.command == "login":
            if not args.resume:
                login = client.begin_login(models)
                public = {key: login[key] for key in ("device_id", "user_code", "verification_uri",
                    "verification_uri_complete", "expires_in", "interval") if key in login}
                if args.no_wait:
                    _print({"status": "pending", **public}, machine)
                    return 0
                print("Open %s\nDevice code: %s\nConfirm the device and Organization in your browser."
                    % (public.get("verification_uri_complete", public["verification_uri"]), public["user_code"]),
                    file=sys.stderr, flush=True)
            deadline = time.monotonic() + args.wait_timeout
            while not client.poll_login():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _print({"status": "pending", "message": "Wait timed out. Continue with login --resume before the code expires."}, machine)
                    return 3
                time.sleep(min(5, remaining))
            result = {"status": "connected", **client.status()}
        elif args.command == "model":
            if args.model_action == 'activate':result = client.activate_model(args.model)
            else:
                key = _endpoint_key(args)
                result = client.register_model(args.model, **({"endpoint_api_key": key} if key is not None else {}))
        elif args.command == 'serve':
            if sys.version_info < (3, 11):raise UsageError('Model forwarding requires Python 3.11 or newer')
            try:
                import asyncio
                from .model_relay import serve
            except ImportError:raise UsageError('Install nexilume[relay] to enable model forwarding') from None
            asyncio.run(serve(client))
            return 0
        elif args.command == "revoke":
            if not args.yes:
                if not sys.stdin.isatty():
                    raise UsageError("Revocation disables this device's model sources. Confirm using --yes")
                print("Revoke device %s and disable its registered model sources? [y/N] " % device,
                    end="", file=sys.stderr, flush=True)
                if input().strip().lower() not in {"y", "yes"}:
                    _print({"status": "cancelled"}, machine)
                    return 0
            result = client.revoke()
        elif args.command == "refresh":
            result = client.refresh()
        else:
            result = client.status()
        _print(result, machine)
        return 0
    except KeyboardInterrupt:
        print("Interrupted. A pending login can be continued with login --resume.", file=sys.stderr)
        return 130
    except RelayModelError as error:
        result = {"error": error.code, "http_status": error.status}
        if "begin_login" in error.code or "Login expired" in error.code:
            result["hint"] = "Use nexus-relay login with your model declarations and the same --cloud / --device."
        elif "Configure endpoint authentication" in error.code:
            result["hint"] = "For an existing endpoint, use model register MODEL --prompt-endpoint-key or --endpoint-key-stdin once."
        _print(result, machine)
        return 1
    except UsageError as error:
        _print({"error": "invalid_arguments", "message": str(error)}, machine)
        return 2
    except (OSError, ValueError):
        # Do not echo invalid manifest/key contents, credential JSON, or paths.
        _print({"error": "invalid_input_or_storage", "message": "Check command flags, model declarations and private storage permissions; no credentials are printed."}, machine)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
