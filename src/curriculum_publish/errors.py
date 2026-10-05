"""领域错误类型。"""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload = {"error": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class NotFoundError(AppError):
    status = 404
    code = "not_found"


class ConflictError(AppError):
    status = 409
    code = "conflict"


class GraphValidationError(AppError):
    status = 422
    code = "graph_invalid"


class BadRequestError(AppError):
    status = 400
    code = "bad_request"
