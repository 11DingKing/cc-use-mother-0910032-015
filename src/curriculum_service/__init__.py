"""主题课程依赖发布后端。

对外暴露领域服务 :class:`CurriculumService` 与持久化层 :class:`Store`，
HTTP 装配见 :mod:`curriculum_service.server`。
"""
from .errors import ServiceError, ValidationIssue
from .service import CurriculumService
from .store import Store

__all__ = ["CurriculumService", "Store", "ServiceError", "ValidationIssue"]
