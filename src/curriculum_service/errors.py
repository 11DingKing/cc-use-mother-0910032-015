"""领域错误类型。"""
from __future__ import annotations

from typing import Any


class ValidationIssue:
    """发布前校验发现的单个问题。"""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        data = {"code": self.code, "message": self.message}
        data.update(self.detail)
        return data


class ServiceError(Exception):
    """业务错误，携带机器可读错误码、HTTP 状态码与校验问题列表。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        issues: list[ValidationIssue] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.issues = issues or []

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.issues:
            body["issues"] = [issue.to_dict() for issue in self.issues]
        return body
