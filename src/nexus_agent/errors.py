class NexusAgentError(Exception):
    """Base SDK error."""


class NexusSecurityConfigurationError(NexusAgentError):
    """The local SDK security profile is missing or invalid."""


class NexusAuthDiscoveryError(NexusAgentError):
    """Router or OIDC authentication metadata is unavailable or invalid."""


class NexusTokenAcquisitionError(NexusAgentError):
    """A short-lived access token could not be acquired safely."""


class NexusHttpError(NexusAgentError):
    """Agent Access Proxy returned a non-success HTTP response."""

    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"{status} {code}: {message}")


class NexusAuthenticationError(NexusHttpError):
    """The router rejected or could not validate caller authentication."""


class NexusAuthorizationError(NexusHttpError):
    """The authenticated caller lacks permission for this operation."""


class NexusCloudRegistrationError(NexusAgentError):
    """The router could not publish this LAN Agent to Nexus Cloud."""

    def __init__(self, state: str, message: str) -> None:
        self.state = str(state or "rejected")
        self.message = str(message or "Cloud Agent registration failed")
        super().__init__(f"{self.state}: {self.message}")
