"""Typed, secret-safe records for Nexus caller-owned Computers."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


class _Record(Mapping[str, Any]):
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)  # type: ignore[arg-type]

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


@dataclass(frozen=True)
class SSHWorkspaceConnection(_Record):
    id: str
    name: str
    ssh_host: str
    ssh_port: int
    ssh_user: str
    auth_mode: str
    workspace_root: str
    status: str
    last_test_status: str = ""
    last_test_error: str = ""
    last_test_at: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    connection_type: str = "ssh"
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SSHWorkspaceConnection":
        return cls(
            id=str(value.get("id") or ""),
            name=str(value.get("name") or ""),
            ssh_host=str(value.get("ssh_host") or ""),
            ssh_port=int(value.get("ssh_port") or 22),
            ssh_user=str(value.get("ssh_user") or ""),
            auth_mode=str(value.get("auth_mode") or "private_key"),
            workspace_root=str(value.get("workspace_root") or "~/.nexus"),
            status=str(value.get("status") or ""),
            last_test_status=str(value.get("last_test_status") or ""),
            last_test_error=str(value.get("last_test_error") or ""),
            last_test_at=str(value["last_test_at"]) if value.get("last_test_at") else None,
            metadata=dict(value.get("metadata") or {}),
            connection_type=str(value.get("connection_type") or "ssh"),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
        )


@dataclass(frozen=True, repr=False)
class SSHWorkspaceConnectionCreate:
    name: str
    ssh_host: str
    ssh_user: str
    auth_mode: str = "private_key"
    ssh_port: int = 22
    workspace_root: str = "~/.nexus"
    private_key: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            "SSHWorkspaceConnectionCreate("
            f"name={self.name!r}, ssh_host={self.ssh_host!r}, "
            f"ssh_user={self.ssh_user!r}, auth_mode={self.auth_mode!r})"
        )


@dataclass(frozen=True, repr=False)
class SSHWorkspaceConnectionUpdate:
    name: Optional[str] = None
    ssh_host: Optional[str] = None
    ssh_port: Optional[int] = None
    ssh_user: Optional[str] = None
    auth_mode: Optional[str] = None
    workspace_root: Optional[str] = None
    private_key: Optional[str] = field(default=None, repr=False)
    password: Optional[str] = field(default=None, repr=False)
    metadata: Optional[Mapping[str, Any]] = None

    def __repr__(self) -> str:
        fields = [name for name in (
            "name", "ssh_host", "ssh_port", "ssh_user", "auth_mode",
            "workspace_root", "private_key", "password", "metadata",
        ) if getattr(self, name) is not None]
        return f"SSHWorkspaceConnectionUpdate(fields={fields!r})"


@dataclass(frozen=True)
class SSHTestResult(_Record):
    status: str
    facts: Mapping[str, Any] = field(default_factory=dict)
    checks: tuple[Mapping[str, Any], ...] = ()
    error: str = ""

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SSHTestResult":
        return cls(
            status=str(value.get("status") or ""),
            facts=dict(value.get("facts") or {}),
            checks=tuple(dict(item) for item in value.get("checks") or ()),
            error=str(value.get("error") or ""),
        )


@dataclass(frozen=True)
class WorkspaceEntry(_Record):
    name: str
    path: str
    kind: str
    size: int = 0
    modified_at: Optional[str] = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkspaceEntry":
        return cls(
            name=str(value.get("name") or ""),
            path=str(value.get("path") or value.get("name") or ""),
            kind=str(value.get("kind") or value.get("type") or "file"),
            size=int(value.get("size") or value.get("size_bytes") or 0),
            modified_at=str(value.get("modified_at")) if value.get("modified_at") else None,
        )


class CommandResult(dict):
    """Typed command result that remains ``dict`` compatible with SDK 0.24."""

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CommandResult":
        result = cls(value)
        result["command_id"] = str(value.get("command_id") or "")
        result["exit_code"] = int(value.get("exit_code") or 0)
        result["stdout"] = str(value.get("stdout") or "")
        result["stderr"] = str(value.get("stderr") or "")
        result["duration_ms"] = int(value.get("duration_ms") or 0)
        result["status"] = str(value.get("status") or "completed")
        result["displayed"] = bool(value.get("displayed", True))
        return result

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


__all__ = [
    "CommandResult",
    "SSHTestResult",
    "SSHWorkspaceConnection",
    "SSHWorkspaceConnectionCreate",
    "SSHWorkspaceConnectionUpdate",
    "WorkspaceEntry",
]
