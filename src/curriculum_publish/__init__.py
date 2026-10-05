"""主题课程依赖发布后端。

- checks：图谱校验（循环/缺失条件/授权期限）、冻结指纹、版本比较
- store：SQLite 持久化、版本链、乐观并发发布
- service：课程包比较与受影响未来预约
- api：零第三方依赖的 JSON HTTP 接口
"""
from .checks import compare_graphs, graph_fingerprint, validate_graph
from .service import CurriculumService
from .store import Store

__all__ = [
    "Store",
    "CurriculumService",
    "validate_graph",
    "graph_fingerprint",
    "compare_graphs",
]
