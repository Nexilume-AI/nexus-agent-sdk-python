"""Windows Named Pipe transport for the privileged addressd service."""

import json
import os
import threading
import time
from typing import Any, Dict, Mapping, Optional

from .host_alias import HostAliasError

DEFAULT_PIPE_NAME = r"\\.\pipe\nexus-agent-addressd"
DEFAULT_PIPE_GROUP = "Nexus Agent Users"
_MAX_REQUEST = 16384
_MAX_RESPONSE = 65536


def _pywin32():
    if os.name != "nt":
        raise HostAliasError("Windows Named Pipe transport is available only on Windows")
    try:
        import ntsecuritycon
        import pywintypes
        import win32api
        import win32con
        import win32file
        import win32pipe
        import win32security
    except ImportError as exc:
        raise HostAliasError(
            "Windows host-alias mode requires: pip install 'nexus-openwrt-agent-sdk[windows]'"
        ) from exc
    return (
        ntsecuritycon,
        pywintypes,
        win32api,
        win32con,
        win32file,
        win32pipe,
        win32security,
    )


def validate_pipe_name(value: str) -> str:
    text = str(value)
    if (
        not text.lower().startswith("\\\\.\\pipe\\")
        or len(text) > 240
        or any(character in "\r\n\0" for character in text)
    ):
        raise ValueError("pipe name must start with \\\\.\\pipe\\")
    return text


def _security_attributes(allowed_group: Optional[str]):
    (
        ntsecuritycon,
        pywintypes,
        win32api,
        win32con,
        _win32file,
        _win32pipe,
        win32security,
    ) = _pywin32()
    system_sid = win32security.CreateWellKnownSid(
        win32security.WinLocalSystemSid, None
    )
    administrators_sid = win32security.CreateWellKnownSid(
        win32security.WinBuiltinAdministratorsSid, None
    )
    if allowed_group:
        try:
            caller_sid = win32security.LookupAccountName(None, allowed_group)[0]
        except Exception as exc:
            raise HostAliasError(
                f"Windows local group does not exist: {allowed_group}"
            ) from exc
        caller_access = (
            ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE
        )
    else:
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
        )
        caller_sid = win32security.GetTokenInformation(
            token, win32security.TokenUser
        )[0]
        caller_access = ntsecuritycon.FILE_ALL_ACCESS

    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        ntsecuritycon.FILE_ALL_ACCESS,
        system_sid,
    )
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        ntsecuritycon.FILE_ALL_ACCESS,
        administrators_sid,
    )
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        caller_access,
        caller_sid,
    )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(True, dacl, False)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    return attributes


def protect_admin_file(path: str) -> None:
    """Restrict an addressd secret/state file to SYSTEM and Administrators."""

    (
        ntsecuritycon,
        _pywintypes,
        _win32api,
        _win32con,
        _win32file,
        _win32pipe,
        win32security,
    ) = _pywin32()
    system_sid = win32security.CreateWellKnownSid(
        win32security.WinLocalSystemSid, None
    )
    administrators_sid = win32security.CreateWellKnownSid(
        win32security.WinBuiltinAdministratorsSid, None
    )
    dacl = win32security.ACL()
    for sid in (system_sid, administrators_sid):
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
    win32security.SetNamedSecurityInfo(
        path,
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )


def protect_admin_tree(path: str) -> None:
    """Restrict a directory tree root to SYSTEM and Administrators.

    New children inherit the protected DACL.  The installer calls this before
    creating the isolated Windows Service runtime so LocalSystem never imports
    executable Python code from a user-writable directory.
    """

    (
        ntsecuritycon,
        _pywintypes,
        _win32api,
        _win32con,
        _win32file,
        _win32pipe,
        win32security,
    ) = _pywin32()
    system_sid = win32security.CreateWellKnownSid(
        win32security.WinLocalSystemSid, None
    )
    administrators_sid = win32security.CreateWellKnownSid(
        win32security.WinBuiltinAdministratorsSid, None
    )
    inheritance = (
        ntsecuritycon.OBJECT_INHERIT_ACE | ntsecuritycon.CONTAINER_INHERIT_ACE
    )
    dacl = win32security.ACL()
    for sid in (system_sid, administrators_sid):
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION,
            inheritance,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
    win32security.SetNamedSecurityInfo(
        path,
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )


def _peer_sid(pipe_handle: Any) -> str:
    (
        _ntsecuritycon,
        _pywintypes,
        win32api,
        win32con,
        _win32file,
        _win32pipe,
        win32security,
    ) = _pywin32()
    win32security.ImpersonateNamedPipeClient(pipe_handle)
    try:
        token = win32security.OpenThreadToken(
            win32api.GetCurrentThread(), win32con.TOKEN_QUERY, True
        )
        sid = win32security.GetTokenInformation(
            token, win32security.TokenUser
        )[0]
        return "sid:" + win32security.ConvertSidToStringSid(sid)
    finally:
        win32security.RevertToSelf()


def _response(
    ok: bool,
    *,
    result: Optional[Mapping[str, Any]] = None,
    error: Optional[str] = None,
) -> bytes:
    payload: Dict[str, Any] = {"version": 1, "ok": ok}
    if ok:
        payload["result"] = dict(result or {})
    else:
        payload["error"] = error or "request failed"
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


class WindowsNamedPipeTransport:
    """One-request-per-connection addressd transport for Windows Agents."""

    def __init__(
        self,
        pipe_name: Optional[str] = None,
        *,
        timeout: float = 5.0,
    ) -> None:
        self.pipe_name = validate_pipe_name(
            pipe_name
            or os.environ.get("NEXUS_AGENT_ADDRESSD_PIPE", DEFAULT_PIPE_NAME)
        )
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = float(timeout)

    def call(self, method: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        (
            _ntsecuritycon,
            pywintypes,
            _win32api,
            win32con,
            win32file,
            win32pipe,
            _win32security,
        ) = _pywin32()
        request = json.dumps(
            {"version": 1, "method": method, "params": dict(parameters)},
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(request) > _MAX_REQUEST:
            raise HostAliasError("addressd request is too large")
        handle = None
        deadline = time.monotonic() + self.timeout
        try:
            while handle is None:
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                try:
                    win32pipe.WaitNamedPipe(self.pipe_name, remaining_ms)
                    handle = win32file.CreateFile(
                        self.pipe_name,
                        win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                        0,
                        None,
                        win32con.OPEN_EXISTING,
                        0,
                        None,
                    )
                except pywintypes.error as exc:
                    if (
                        exc.winerror not in (2, 231)  # FILE_NOT_FOUND, PIPE_BUSY
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(0.01)
            win32pipe.SetNamedPipeHandleState(
                handle, win32pipe.PIPE_READMODE_MESSAGE, None, None
            )
            win32file.WriteFile(handle, request)
            _status, raw = win32file.ReadFile(handle, _MAX_RESPONSE + 1)
        except pywintypes.error as exc:
            raise HostAliasError(
                f"cannot connect to nexus-agent-addressd at {self.pipe_name}"
            ) from exc
        finally:
            if handle is not None:
                win32file.CloseHandle(handle)
        if len(raw) > _MAX_RESPONSE:
            raise HostAliasError("addressd response is too large")
        try:
            payload = json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HostAliasError("addressd returned an invalid response") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise HostAliasError("addressd response version is invalid")
        if payload.get("ok") is not True:
            raise HostAliasError(str(payload.get("error", "addressd request failed")))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise HostAliasError("addressd response result is invalid")
        return result


class AddressdNamedPipeServer:
    """Threaded Windows Named Pipe server with SID-bound lease ownership."""

    def __init__(
        self,
        pipe_name: str,
        application: Any,
        *,
        allowed_group: Optional[str] = DEFAULT_PIPE_GROUP,
        sweep_interval: float = 5.0,
    ) -> None:
        self.pipe_name = validate_pipe_name(pipe_name)
        if sweep_interval <= 0:
            raise ValueError("sweep_interval must be positive")
        self.application = application
        self.allowed_group = allowed_group
        self._security = _security_attributes(allowed_group)
        self._sweep_interval = float(sweep_interval)
        self._stop = threading.Event()
        self._threads = set()
        self._threads_lock = threading.Lock()
        self._sweep_thread = threading.Thread(
            target=self._sweep_worker,
            name="nexus-addressd-sweeper",
            daemon=True,
        )
        self._sweep_thread.start()

    def _new_pipe(self):
        (
            _ntsecuritycon,
            _pywintypes,
            _win32api,
            _win32con,
            _win32file,
            win32pipe,
            _win32security,
        ) = _pywin32()
        return win32pipe.CreateNamedPipe(
            self.pipe_name,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_MESSAGE
            | win32pipe.PIPE_READMODE_MESSAGE
            | win32pipe.PIPE_WAIT,
            win32pipe.PIPE_UNLIMITED_INSTANCES,
            _MAX_RESPONSE,
            _MAX_REQUEST,
            0,
            self._security,
        )

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        del poll_interval
        (
            _ntsecuritycon,
            pywintypes,
            _win32api,
            _win32con,
            win32file,
            win32pipe,
            _win32security,
        ) = _pywin32()
        while not self._stop.is_set():
            handle = self._new_pipe()
            try:
                try:
                    win32pipe.ConnectNamedPipe(handle, None)
                except pywintypes.error as exc:
                    if exc.winerror != 535:  # ERROR_PIPE_CONNECTED
                        raise
                if self._stop.is_set():
                    win32file.CloseHandle(handle)
                    break
                thread = threading.Thread(
                    target=self._serve_client,
                    args=(handle,),
                    name="nexus-addressd-client",
                    daemon=True,
                )
                with self._threads_lock:
                    self._threads.add(thread)
                thread.start()
            except BaseException:
                try:
                    win32file.CloseHandle(handle)
                except Exception:
                    pass
                if self._stop.is_set():
                    break
                raise

    def _serve_client(self, handle: Any) -> None:
        (
            _ntsecuritycon,
            pywintypes,
            _win32api,
            _win32con,
            win32file,
            win32pipe,
            _win32security,
        ) = _pywin32()
        try:
            try:
                _status, raw = win32file.ReadFile(handle, _MAX_REQUEST + 1)
                if len(raw) > _MAX_REQUEST or not raw.endswith(b"\n"):
                    reply = _response(False, error="request is empty or too large")
                else:
                    request = json.loads(raw.decode("utf-8"))
                    if not isinstance(request, dict):
                        raise ValueError("request must be an object")
                    result = self.application.dispatch(
                        request, peer_owner=_peer_sid(handle)
                    )
                    reply = _response(True, result=result)
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
                HostAliasError,
            ) as exc:
                reply = _response(False, error=str(exc)[:512])
            except Exception:
                reply = _response(False, error="internal addressd failure")
            win32file.WriteFile(handle, reply)
            win32file.FlushFileBuffers(handle)
        except pywintypes.error:
            pass
        finally:
            try:
                win32pipe.DisconnectNamedPipe(handle)
            except pywintypes.error:
                pass
            win32file.CloseHandle(handle)
            with self._threads_lock:
                self._threads.discard(threading.current_thread())

    def _sweep_worker(self) -> None:
        while not self._stop.wait(self._sweep_interval):
            try:
                self.application.sweep()
            except Exception:
                pass

    def shutdown(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            transport = WindowsNamedPipeTransport(self.pipe_name, timeout=0.5)
            # Opening the pipe wakes a blocking ConnectNamedPipe. No request is needed.
            (
                _ntsecuritycon,
                _pywintypes,
                _win32api,
                win32con,
                win32file,
                win32pipe,
                _win32security,
            ) = _pywin32()
            win32pipe.WaitNamedPipe(self.pipe_name, 500)
            handle = win32file.CreateFile(
                transport.pipe_name,
                win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                0,
                None,
                win32con.OPEN_EXISTING,
                0,
                None,
            )
            win32file.CloseHandle(handle)
        except Exception:
            pass

    def server_close(self) -> None:
        self.shutdown()
        self._sweep_thread.join(timeout=max(1.0, self._sweep_interval * 2))
        with self._threads_lock:
            threads = tuple(self._threads)
        for thread in threads:
            thread.join(timeout=2.0)


__all__ = [
    "AddressdNamedPipeServer",
    "DEFAULT_PIPE_GROUP",
    "DEFAULT_PIPE_NAME",
    "WindowsNamedPipeTransport",
    "protect_admin_file",
]
