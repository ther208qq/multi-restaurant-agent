"""FastAPI exception handlers mapping domain exceptions to HTTP responses.

The API layer installs a single handler so that any :class:`DomainError` raised
by a Service is translated into a consistent ``{"detail": ...}`` JSON response.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from backend.app.exceptions import (
    AuthenticationError,
    ConflictError,
    DomainError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)

logger = logging.getLogger("restaurant_ai_api")

# Ordered so the most specific mapping wins; anything unmatched falls back to 500.
_STATUS_BY_EXCEPTION = (
    (NotFoundError, 404),
    (ValidationError, 400),
    (ConflictError, 409),
    (AuthenticationError, 401),
    (ForbiddenError, 403),
)


def status_for_exception(exc: DomainError) -> int:
    """Return the HTTP status code for a domain exception (500 if unmapped)."""
    for exc_type, status_code in _STATUS_BY_EXCEPTION:
        if isinstance(exc, exc_type):
            return status_code
    return 500


def register_exception_handlers(app: FastAPI) -> None:
    """Install the domain-exception handler on ``app``."""

    @app.exception_handler(DomainError)
    async def _handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        status_code = status_for_exception(exc)
        headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
        if status_code >= 500:
            logger.exception("Unhandled domain error: %s", exc)
        return JSONResponse(
            status_code=status_code,
            content={"detail": exc.message},
            headers=headers,
        )
