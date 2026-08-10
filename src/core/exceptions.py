class DomainError(Exception):
    """Base exception for expected business-rule failures."""


class NotFoundError(DomainError):
    """Requested domain object does not exist."""


class ConflictError(DomainError):
    """Operation conflicts with existing domain state."""


class StaleVersionError(ConflictError):
    """The write was built on a version someone else has already superseded.

    Its own class because sync tells this apart from every other refusal
    (Plan 0004 §8): a stale `base_version` is the one case a person resolves by
    looking at both versions side by side, so it comes back as `conflict`.
    Everything else a domain rule says no to — no stock, a closed day, a serial
    already used — is a `rejected` operation, which the review queue presents
    differently because there is nothing to compare.

    Still a `ConflictError`, so an ordinary HTTP request keeps answering 409.
    """


class AuthenticationError(DomainError):
    """Credentials or session are invalid."""


class AuthorizationError(DomainError):
    """Authenticated identity lacks a required permission."""
