"""Model-scoped Cloud device login for Linux/OpenWrt hosts. No nexus_cli required.

Device grants and endpoint keys live in a private SDK-managed store. Inference
callers continue using Router API keys; these device credentials cannot infer.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager
import urllib.error
import urllib.parse
import urllib.request


class RelayModelError(RuntimeError):
    def __init__(self, code, status=0):
        self.code, self.status = code, status
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RelayModelClient:
    """Bind once, register approved endpoints, refresh credentials, or revoke.

    `storage_dir` is optional for tests/isolated deployments; users never need
    key-file/state-file arguments. Automatic private storage requires POSIX.
    """
    def __init__(self, cloud_url, device_name, *, storage_dir=None, timeout=90, transport=None):
        parsed = urllib.parse.urlsplit(cloud_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Cloud URL must be an HTTPS origin")
        self.cloud_url, self.device_name = cloud_url.rstrip("/"), device_name
        if not device_name.strip() or len(device_name) > 100:
            raise ValueError("Specify a device name up to 100 characters")
        identity = hashlib.sha256((self.cloud_url + "\n" + device_name).encode()).hexdigest()[:24]
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        self.directory = Path(storage_dir) if storage_dir else root / "nexilume/relay" / identity
        self.timeout, self._transport = timeout, transport
        self._opener = urllib.request.build_opener(_NoRedirect())

    @contextmanager
    def _store(self):
        if os.name != "posix":
            raise RelayModelError("Device credential storage currently requires Linux/OpenWrt")
        import fcntl
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RelayModelError("SDK credential directory must be owned by you with mode 0700")
        lock = os.open(str(self.directory / "lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.directory / "device.json"
            try:
                fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                state = {}
            else:
                with os.fdopen(fd) as stream:
                    meta = os.fstat(stream.fileno())
                    if meta.st_uid != os.getuid() or meta.st_mode & 0o077 or not stat.S_ISREG(meta.st_mode):
                        raise RelayModelError("SDK credentials must have mode 0600 and be owned by you")
                    state = json.load(stream)
            if state and state.get("cloud") != self.cloud_url:
                raise RelayModelError("Credential store belongs to another Cloud")
            state["cloud"] = self.cloud_url
            yield state
        finally:
            os.close(lock)

    def _save(self, state):
        fd, name = tempfile.mkstemp(prefix=".device-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.directory / "device.json")
            directory_fd = os.open(str(self.directory), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _http(self, path, data=None, token=None):
        if self._transport:
            return self._transport(path, data, token)
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        body = None if data is None else json.dumps(data).encode()
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.cloud_url + "/api/v1/auth/relay/" + path,
            data=body, headers=headers)
        try:
            response = self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, urllib.error.URLError):
            raise RelayModelError("Cloud connection failed; no mutation was automatically replayed") from None
        with response:
            raw = response.read(1024 * 1024 + 1)
            status_code = response.code
        if len(raw) > 1024 * 1024:
            raise RelayModelError("Cloud response too large")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise RelayModelError("Cloud returned invalid JSON", status_code) from None
        if not isinstance(value, dict):
            raise RelayModelError("Cloud returned an invalid response shape", status_code)
        if value.get("ok") is True:
            value = value["data"]
        if status_code >= 400:
            detail = value.get("error")
            if isinstance(detail, dict):
                detail = detail.get("code")
            if detail not in {"authorization_pending", "slow_down", "access_denied", "expired_token"}:
                detail = "Cloud rejected request (HTTP %s)" % status_code
            raise RelayModelError(detail, status_code)
        return value

    def begin_login(self, models):
        """Return URL/short code for human confirmation; keep device secret private."""
        with self._store() as state:
            if state.get("tokens"):
                try:
                    self._http("models/", token=self._token(state))
                except RelayModelError as error:
                    if error.status not in (401, 403):
                        raise
                    # An explicit new login may replace invalid/revoked local
                    # credentials, but never silently replaces a working grant.
                    state.pop("tokens", None)
                    self._save(state)
                else:
                    raise RelayModelError("Device already connected; revoke before replacing its approved scope")
            result = self._http("start/", {"name": self.device_name, "models": models})
            state["pending"] = {"device_code": result["device_code"], "until": time.time() + result["expires_in"],
                "interval": result["interval"], "next_poll": 0}
            self._save(state)
            return {k: v for k, v in result.items() if k != "device_code"}

    def _accept(self, state, result):
        state["tokens"] = result
        state["registrations"] = result.get("registrations", {})
        state["access_until"] = time.time() + result["expires_in"]
        state.pop("pending", None)
        self._save(state)

    def poll_login(self):
        """Nonblocking: returns False while awaiting confirmation, True when bound."""
        with self._store() as state:
            if state.get("tokens"):
                return True
            pending = state.get("pending")
            if not pending or time.time() >= pending["until"]:
                raise RelayModelError("Login expired; begin a new login")
            if time.time() < pending["next_poll"]:
                return False
            pending["next_poll"] = time.time() + pending["interval"]
            self._save(state)
            try:
                result = self._http("token/", {"device_code": pending["device_code"]})
            except RelayModelError as error:
                if error.code in {"authorization_pending", "slow_down"}:
                    if error.code == "slow_down":
                        pending["interval"] += 5
                        pending["next_poll"] = time.time() + pending["interval"]
                        self._save(state)
                    return False
                raise
            self._accept(state, result)
            return True

    def _token(self, state, force=False):
        tokens = state.get("tokens")
        if not tokens:
            raise RelayModelError("Device is not connected; call begin_login first")
        if force or time.time() >= state["access_until"] - 60:
            result = self._http("token/", {"refresh_token": tokens["refresh_token"]})
            self._accept(state, result)
        return state["tokens"]["access_token"]

    def refresh(self):
        with self._store() as state:
            self._token(state, force=True)
            return {"device_id": state["tokens"]["device_id"], "status": "connected"}

    def _authorized(self, state, path, data=None):
        token = self._token(state)
        try:
            return self._http(path, data, token)
        except RelayModelError as error:
            if error.status != 401:
                raise
            # A 401 means device authentication rejected the request before
            # dispatch. Never replay a timeout, 5xx, or business operation error.
            return self._http(path, data, self._token(state, force=True))

    def endpoint_api_key(self, model):
        """For the endpoint's auth middleware only. Never log or return to callers."""
        with self._store() as state:
            if model not in [s["model"] for s in state.get("tokens", {}).get("models", [])]:
                raise RelayModelError("Model is not approved")
            keys = state.setdefault("endpoint_keys", {})
            if model not in keys:
                keys[model] = "nrelay_up_" + secrets.token_urlsafe(40)
                self._save(state)
            return keys[model]

    def register_model(self, model, *, endpoint_api_key=None):
        """Register only an approved model. Receipts persist automatically on Cloud.

        Existing endpoints may supply a key from their own secret manager once.
        Otherwise use endpoint_api_key(model) to configure endpoint middleware.
        """
        with self._store() as state:
            self._token(state)
            if model not in [s["model"] for s in state["tokens"]["models"]]:
                raise RelayModelError("Model is not approved")
            keys = state.setdefault("endpoint_keys", {})
            if endpoint_api_key:
                if model in keys and keys[model] != endpoint_api_key:
                    raise RelayModelError("Endpoint credential already exists; rotate explicitly")
                keys[model] = endpoint_api_key
            if model not in keys:
                raise RelayModelError("Configure endpoint authentication with endpoint_api_key(model) first")
            self._save(state)
            receipt = self._authorized(state, "models/", {"model": model, "endpoint_api_key": keys[model]})
            state.setdefault("registrations", {})[model] = receipt
            self._save(state)
            return receipt

    def status(self):
        with self._store() as state:
            return self._authorized(state, "models/")

    def revoke(self):
        with self._store() as state:
            result = self._authorized(state, "revoke/", {})
            state.pop("tokens", None)
            state.pop("endpoint_keys", None)
            state["revoked"] = True
            self._save(state)
            return result
