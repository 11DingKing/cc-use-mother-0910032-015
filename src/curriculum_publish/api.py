"""基于标准库 http.server 的 JSON HTTP 接口。

路由：
  POST   /courses                                  创建课程（含初始草稿）
  GET    /courses                                  课程列表
  GET    /courses/{cid}                            课程详情与版本链摘要
  GET    /courses/{cid}/versions                   版本链
  POST   /courses/{cid}/revisions                  基于当前版本创建修订草稿
  PUT    /courses/{cid}/versions/{vid}/graph       覆盖草稿图谱
  POST   /courses/{cid}/versions/{vid}/validate    发布前校验
  POST   /courses/{cid}/versions/{vid}/publish     冻结发布（支持 expected_version_id 乐观锁）
  POST   /courses/{cid}/emergency-replace          紧急替代
  POST   /courses/{cid}/suspensions                局部停用
  POST   /courses/{cid}/copy                       课程复制
  GET    /courses/{cid}/packages/{vid}             读取冻结课程包
  GET    /courses/{cid}/compare?from=&to_course=&to=   比较课程包
  GET    /courses/{cid}/affected-reservations      受影响的未来预约
  POST   /courses/{cid}/reservations               创建预约（锁定冻结版本）
  GET    /courses/{cid}/events                     版本链事件流
  GET    /health
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse, parse_qs

from .errors import AppError, BadRequestError
from .service import CurriculumService
from .store import Store


class _Handler(BaseHTTPRequestHandler):
    server_version = "CurriculumPublish/1.0"

    @property
    def service(self) -> CurriculumService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # ---- 工具 ----

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BadRequestError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(value, dict):
            raise BadRequestError("请求体必须是 JSON 对象")
        return value

    def _require(self, body: dict[str, Any], key: str) -> Any:
        if key not in body:
            raise BadRequestError(f"缺少必填字段：{key}")
        return body[key]

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            body = self._read_json() if method in ("POST", "PUT") else {}
            route = self._match(method, path)
            if route is None:
                self._json({"error": "not_found", "message": f"无此路由：{method} {path}"}, 404)
                return
            handler, kwargs = route
            handler(method, path, query, body, **kwargs)
        except AppError as exc:
            self._json(exc.to_dict(), exc.status)
        except Exception as exc:  # pragma: no cover - 兜底
            self._json({"error": "internal_error", "message": str(exc)}, 500)

    def _match(self, method: str, path: str):
        rules: list[tuple[str, str, Callable]] = [
            ("GET",    r"^/health$", self.h_health),
            ("GET",    r"^/courses$", self.h_courses),
            ("POST",   r"^/courses$", self.h_courses),
            ("POST",   r"^/courses/(?P<cid>[\w-]+)/copy$", self.h_copy),
            ("GET",    r"^/courses/(?P<cid>[\w-]+)$", self.h_course_detail),
            ("GET",    r"^/courses/(?P<cid>[\w-]+)/versions$", self.h_versions),
            ("POST",   r"^/courses/(?P<cid>[\w-]+)/revisions$", self.h_revision),
            ("PUT",    r"^/courses/(?P<cid>[\w-]+)/versions/(?P<vid>[\w-]+)/graph$", self.h_graph),
            ("POST",   r"^/courses/(?P<cid>[\w-]+)/versions/(?P<vid>[\w-]+)/validate$", self.h_validate),
            ("POST",   r"^/courses/(?P<cid>[\w-]+)/versions/(?P<vid>[\w-]+)/publish$", self.h_publish),
            ("POST",   r"^/courses/(?P<cid>[\w-]+)/emergency-replace$", self.h_replace),
            ("POST",   r"^/courses/(?P<cid>[\w-]+)/suspensions$", self.h_suspend),
            ("GET",    r"^/courses/(?P<cid>[\w-]+)/packages/(?P<vid>[\w-]+)$", self.h_package),
            ("GET",    r"^/courses/(?P<cid>[\w-]+)/compare$", self.h_compare),
            ("GET",    r"^/courses/(?P<cid>[\w-]+)/affected-reservations$", self.h_affected),
            ("POST",   r"^/courses/(?P<cid>[\w-]+)/reservations$", self.h_reservations),
            ("GET",    r"^/courses/(?P<cid>[\w-]+)/events$", self.h_events),
        ]
        for rule_method, pattern, fn in rules:
            if rule_method != method:
                continue
            m = re.match(pattern, path)
            if m:
                return fn, m.groupdict()
        return None

    # ---- 处理器 ----

    def h_health(self, method, path, query, body):
        store = self.service.store
        count = len(store.list_courses())
        self._json({"status": "ok", "courses": count})

    def h_courses(self, method, path, query, body):
        if method == "GET":
            self._json({"courses": self.service.store.list_courses()})
            return
        code = self._require(body, "code")
        title = self._require(body, "title")
        graph = self._require(body, "graph")
        self._validate_graph_shape(graph)
        result = self.service.store.create_course(code, title, graph, body.get("created_by"))
        self._json(result, 201)

    def h_course_detail(self, method, path, query, body, cid):
        conn = self.service.store._connect()  # noqa: SLF001
        try:
            course = dict(self.service.store.get_course(conn, cid))
        finally:
            if self.service.store._mem is None:  # noqa: SLF001
                conn.close()
        course["versions"] = self.service.store.list_versions(cid)
        self._json(course)

    def h_versions(self, method, path, query, body, cid):
        self._json({"course_id": cid, "versions": self.service.store.list_versions(cid)})

    def h_revision(self, method, path, query, body, cid):
        vid = self.service.store.create_revision(cid, body.get("created_by"))
        self._json({"draft_version_id": vid}, 201)

    def h_graph(self, method, path, query, body, cid, vid):
        graph = self._require(body, "graph")
        self._validate_graph_shape(graph)
        self.service.store.replace_draft_graph(cid, vid, graph)
        self._json({"ok": True, "version_id": vid})

    def h_validate(self, method, path, query, body, cid, vid):
        self._json(self.service.store.validate_draft(cid, vid))

    def h_publish(self, method, path, query, body, cid, vid):
        result = self.service.store.publish_draft(
            cid, vid,
            expected_version_id=body.get("expected_version_id"),
            actor=body.get("created_by"),
        )
        self._json(result)

    def h_replace(self, method, path, query, body, cid):
        node_id = self._require(body, "node_id")
        replacement = self._require(body, "replacement")
        if "authorization_valid_until" not in replacement:
            raise BadRequestError("替代节点必须提供 authorization_valid_until")
        vid = self.service.store.emergency_replace(
            cid, node_id, replacement, body.get("created_by"))
        self._json({"draft_version_id": vid,
                    "change": f"emergency_replace:{node_id}"}, 201)

    def h_suspend(self, method, path, query, body, cid):
        node_id = self._require(body, "node_id")
        reason = self._require(body, "reason")
        vid = self.service.store.suspend_node(
            cid, node_id, reason, body.get("created_by"))
        self._json({"draft_version_id": vid,
                    "change": f"suspension:{node_id}"}, 201)

    def h_copy(self, method, path, query, body, cid):
        new_code = self._require(body, "code")
        new_title = self._require(body, "title")
        result = self.service.store.copy_course(
            cid, new_code, new_title, body.get("created_by"))
        self._json(result, 201)

    def h_package(self, method, path, query, body, cid, vid):
        self._json(self.service.store.get_manifest(cid, vid))

    def h_compare(self, method, path, query, body, cid):
        from_v = query.get("from", [None])[0]
        if not from_v:
            raise BadRequestError("查询参数 from=<version_id> 必填")
        result = self.service.compare_packages(
            cid, from_v,
            course_id_b=query.get("to_course", [None])[0],
            version_id_b=query.get("to", [None])[0],
        )
        self._json(result)

    def h_affected(self, method, path, query, body, cid):
        self._json(self.service.affected_future_reservations(cid))

    def h_reservations(self, method, path, query, body, cid):
        contact = self._require(body, "school_contact")
        sched = self._require(body, "scheduled_date")
        size = self._require(body, "party_size")
        if not isinstance(size, int) or size <= 0:
            raise BadRequestError("party_size 必须为正整数")
        result = self.service.store.create_reservation(
            cid, body.get("version_id"), contact, sched, size)
        self._json(result, 201)

    def h_events(self, method, path, query, body, cid):
        self._json({"course_id": cid, "events": self.service.store.list_events(cid)})

    @staticmethod
    def _validate_graph_shape(graph: Any) -> None:
        if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) \
                or not isinstance(graph.get("edges"), list):
            raise BadRequestError("graph 必须包含 nodes 与 edges 列表")
        for n in graph["nodes"]:
            if not isinstance(n, dict) or not isinstance(n.get("id"), str):
                raise BadRequestError("每个节点必须包含字符串 id")
        for e in graph["edges"]:
            if not isinstance(e, dict) or not isinstance(e.get("source"), str) \
                    or not isinstance(e.get("target"), str):
                raise BadRequestError("每条边必须包含 source 与 target")


def build_server(db_path: str = ":memory:", host: str = "127.0.0.1",
                 port: int = 8080, verbose: bool = True) -> ThreadingHTTPServer:
    store = Store(db_path)
    server = ThreadingHTTPServer((host, port), _Handler)
    server.service = CurriculumService(store)  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def main() -> None:  # pragma: no cover
    import argparse
    import os

    parser = argparse.ArgumentParser(description="主题课程依赖发布后端")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--db", default=os.environ.get("DB_PATH", "curriculum.db"))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    server = build_server(args.db, args.host, args.port, verbose=not args.quiet)
    print(f"课程发布后端已启动：http://{args.host}:{args.port}  数据库：{args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()
