"""Explicit revision-checked Tool Setup writes; legacy operations are unchanged."""
import hashlib
import json
import re
try:
    import tomllib
except ModuleNotFoundError:
    # Computer Runtime also supports Python 3.9/3.10; keep the core dependency-free.
    import tomli as tomllib
import uuid


def _advance_fence(runtime, path, data):
    from .computer_runtime import RuntimeOperationError, _atomic_user_file_write
    try:
        namespace = uuid.UUID(str(data.get("namespace", "")))
        if str(namespace) != str(runtime.config.get("device_id", "")):
            raise ValueError()
        sequence = data.get("sequence")
        if type(sequence) is not int or not 1 <= sequence < 2**63:
            raise ValueError()
    except (ValueError, TypeError):
        raise RuntimeOperationError("TOOL_CONFIG_FENCE_INVALID", "Tool Setup fence is not bound to this Computer") from None
    record = path.with_name(path.name + ".nexus-fence-" + namespace.hex + ".json")
    if record.is_symlink():
        raise RuntimeOperationError("TOOL_CONFIG_UNSAFE_PATH", "Tool Setup fence path is unavailable")
    previous = 0
    if record.exists():
        try:
            with record.open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError()
            stored = json.loads(raw)
            previous = stored["sequence"]
            if stored.get("namespace") != str(namespace) or type(previous) is not int or not 1 <= previous < 2**63:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise RuntimeOperationError("TOOL_CONFIG_FENCE_INVALID", "Tool Setup fence cannot be verified; repair is required") from None
    if sequence <= previous:
        raise RuntimeOperationError("TOOL_CONFIG_STALE_WRITE", "This Tool Setup write was superseded; it will not be replayed")
    _atomic_user_file_write(record, json.dumps({"namespace":str(namespace), "sequence":sequence}).encode())
    return {"namespace":str(namespace), "sequence":sequence}


def compare_and_write_codex(runtime, data, *, fenced=False, barrier=False):
    from .computer_runtime import (
        MAX_TEXT_RESULT, NexusComputerRuntimeError, RuntimeOperationError,
        _RuntimeInstanceLock, _atomic_user_file_write,
    )
    content, expected = data.get("content"), data.get("expected_revision")
    if barrier:
        content, expected = "", hashlib.sha256(b"").hexdigest()
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_TEXT_RESULT:
        raise RuntimeOperationError("TOOL_CONFIG_TOO_LARGE", "Codex config exceeds the allowed size")
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise RuntimeOperationError("TOOL_CONFIG_INVALID", "A configuration revision is required")
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        raise RuntimeOperationError("TOOL_CONFIG_INVALID", "Codex configuration is not valid TOML") from None
    path = runtime._codex_path()
    lock_path = path.with_name(path.name + ".nexus-cas.lock")
    if path.is_symlink() or lock_path.is_symlink():
        raise RuntimeOperationError("TOOL_CONFIG_UNSAFE_PATH", "Codex configuration path must not be a symbolic link")
    lock = _RuntimeInstanceLock(lock_path)
    try:
        lock.acquire()
    except NexusComputerRuntimeError:
        raise RuntimeOperationError("TOOL_CONFIG_BUSY", "Another configuration update is in progress") from None
    except OSError:
        raise RuntimeOperationError("TOOL_CONFIG_WRITE_FAILED", "Codex configuration could not be safely updated") from None

    def read_bounded():
        if not path.exists():
            return b""
        with path.open("rb") as stream:
            value = stream.read(MAX_TEXT_RESULT + 1)
        if len(value) > MAX_TEXT_RESULT:
            raise RuntimeOperationError("TOOL_CONFIG_TOO_LARGE", "Existing Codex config exceeds the allowed size")
        return value

    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeOperationError("TOOL_CONFIG_UNSAFE_PATH", "Codex configuration path is unavailable")
        fence = _advance_fence(runtime, path, data) if fenced else {}
        before_bytes = read_bounded()
        # Match the existing read endpoint's universal-newline text response.
        before = before_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        if barrier:
            return {"exists":path.is_file(), "content":before,
                "revision":hashlib.sha256(before.encode()).hexdigest(), "fence":fence}
        if hashlib.sha256(before.encode()).hexdigest() != expected:
            raise RuntimeOperationError("TOOL_CONFIG_CONFLICT", "Configuration changed; reload and review again")
        # Detect non-cooperating editor changes immediately before replacement.
        if read_bounded() != before_bytes:
            raise RuntimeOperationError("TOOL_CONFIG_CONFLICT", "Configuration changed; reload and review again")
        if content != before:
            _atomic_user_file_write(path, content.encode("utf-8"))
        try:
            after = read_bounded().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except (RuntimeOperationError, OSError, UnicodeError):
            raise RuntimeOperationError("TOOL_CONFIG_VERIFICATION_FAILED", "Configuration write could not be verified; recovery is required") from None
        if after != content:
            raise RuntimeOperationError("TOOL_CONFIG_VERIFICATION_FAILED", "Configuration write could not be verified; recovery is required")
        return {"path": str(path), "exists": path.is_file(), "content": after,
                "revision": hashlib.sha256(after.encode()).hexdigest(), **({"fence":fence} if fenced else {})}
    except (OSError, UnicodeError):
        raise RuntimeOperationError("TOOL_CONFIG_WRITE_FAILED", "Codex configuration could not be safely updated") from None
    finally:
        lock.release()
