class DomainError(Exception):
    """Base exception for expected business-rule failures."""


class NotFoundError(DomainError):
    """Requested domain object does not exist."""


class ConflictError(DomainError):
    """Operation conflicts with existing domain state."""


class AuthenticationError(DomainError):
    """Credentials or session are invalid."""


class AuthorizationError(DomainError):
    """Authenticated identity lacks a required permission."""
