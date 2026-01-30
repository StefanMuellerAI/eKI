"""Custom exceptions for the eKI API."""

from typing import Any


class EKIException(Exception):
    """Base exception for eKI API."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class ValidationException(EKIException):
    """Raised when request validation fails."""

    pass


class NotFoundException(EKIException):
    """Raised when a resource is not found."""

    pass


class WorkflowException(EKIException):
    """Raised when a Temporal workflow fails."""

    pass


class ServiceUnavailableException(EKIException):
    """Raised when a required service is unavailable."""

    pass


class RateLimitException(EKIException):
    """Raised when rate limit is exceeded."""

    pass


class AuthenticationException(EKIException):
    """Raised when authentication fails."""

    pass


class ParsingException(EKIException):
    """Raised when script parsing fails."""

    pass


class LLMException(EKIException):
    """Raised when LLM provider interaction fails."""

    pass


class EProException(EKIException):
    """Raised when eProjekt integration fails."""

    pass
