"""Custom exceptions for the eKI API."""

from typing import Any


class EKIException(Exception):
    """Base exception for eKI API."""

    status_code: int = 400

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class ValidationException(EKIException):
    """Raised when request validation fails."""

    status_code = 422


class NotFoundException(EKIException):
    """Raised when a resource is not found."""

    status_code = 404


class WorkflowException(EKIException):
    """Raised when a Temporal workflow fails."""

    status_code = 502


class ServiceUnavailableException(EKIException):
    """Raised when a required service is unavailable."""

    status_code = 503


class RateLimitException(EKIException):
    """Raised when rate limit is exceeded."""

    status_code = 429


class AuthenticationException(EKIException):
    """Raised when authentication fails."""

    status_code = 401


class ParsingException(EKIException):
    """Raised when script parsing fails."""

    pass


class LLMException(EKIException):
    """Raised when LLM provider interaction fails."""

    pass


class EProException(EKIException):
    """Raised when eProjekt integration fails."""

    pass
