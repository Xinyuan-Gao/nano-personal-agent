from __future__ import annotations

from .clock import new_correlation_id


class AppError(Exception):
    code = "internal_error"
    http_status = 500
    default_retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        field_errors: dict | None = None,
        current_revision: int | None = None,
        correlation_id: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.retryable = self.default_retryable if retryable is None else retryable
        self.field_errors = field_errors or {}
        self.current_revision = current_revision
        self.correlation_id = correlation_id or new_correlation_id()
        if http_status:
            self.http_status = http_status

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "correlation_id": self.correlation_id,
            "field_errors": self.field_errors,
            "current_revision": self.current_revision,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class InvalidInput(AppError):
    code = "invalid_input"
    http_status = 400


class PermissionDenied(AppError):
    code = "permission_denied"
    http_status = 403


class MissingDependency(AppError):
    code = "missing_dependency"
    http_status = 409


class ModelError(AppError):
    code = "model_error"
    http_status = 502
    default_retryable = True


class BudgetExceeded(AppError):
    code = "budget_exceeded"
    http_status = 429


class BaselineConflict(AppError):
    code = "baseline_conflict"
    http_status = 409


class InvalidState(AppError):
    code = "invalid_state"
    http_status = 409


class StorageError(AppError):
    code = "storage_error"
    http_status = 500


class EvidenceUnavailable(AppError):
    code = "evidence_unavailable"
    http_status = 410


def to_error_dict(exc: Exception) -> dict:
    if isinstance(exc, AppError):
        return exc.to_dict()
    return {
        "code": "storage_error",
        "message": str(exc) or type(exc).__name__,
        "retryable": False,
        "correlation_id": new_correlation_id(),
        "field_errors": {},
        "current_revision": None,
    }
