"""Domain exceptions, independent of any web framework.

Raised by the Service layer to express business failures. The API layer maps
these to HTTP status codes (see ``backend.app.error_handlers``); the Agent
layer converts them into user-facing messages.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all business/domain errors."""

    def __init__(self, message: str = "发生了一个业务错误，请稍后重试。") -> None:
        self.message = message
        super().__init__(message)


class NotFoundError(DomainError):
    """Requested entity does not exist. Maps to HTTP 404."""


class ValidationError(DomainError):
    """Request violates a business rule or is malformed. Maps to HTTP 400."""


class ConflictError(DomainError):
    """Operation conflicts with current state (e.g. stock/time). Maps to HTTP 409."""


class AuthenticationError(DomainError):
    """Credentials are missing or invalid. Maps to HTTP 401."""


class ForbiddenError(DomainError):
    """Caller is not allowed to perform the operation. Maps to HTTP 403."""


class ServiceError(DomainError):
    """Unclassified internal service failure. Maps to HTTP 500."""


class TableUnavailableError(ConflictError):
    """No eligible table matches the requested slot/area. Maps to HTTP 409.

    Distinct from a plain :class:`ConflictError` so callers (the Agent booking
    adapter) can tell "nothing free in that area" apart from "lost a race".
    """


class ReservationRaceError(ConflictError):
    """A concurrent booking won the DB exclusion-constraint race. Maps to HTTP 409."""


__all__ = [
    "DomainError",
    "NotFoundError",
    "ValidationError",
    "ConflictError",
    "TableUnavailableError",
    "ReservationRaceError",
    "AuthenticationError",
    "ForbiddenError",
    "ServiceError",
]
