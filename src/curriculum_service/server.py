"""HTTP JSON API（仅依赖标准库）。

路由
----
GET    /health
POST   /courses                          保存/更新课程草稿
GET    /courses                          课程系列列表
GET    /courses/{code}                   读取草稿
POST   /courses/{code}/validate          发布前校验（?as_of=YYYY-MM-DD）
POST   /courses/{code}/publish           校验通过后冻结发布（?expected_version=n）
GET    /courses/{code}/packages          版本列表
GET    /packages/{id}                    课程包完整快照
GET    /packages/{id}/chain              版本链（沿 parent 回溯）
GET    /packages/{id}/compare?with={id}  比较两个课程包
POST   /courses/{code}/emergency-replace 紧急替代节点并发布新版本
POST   /courses/{code}/suspensions       局部停用节点并发布新版本
POST   /courses/{code}/copy              复制为新课程
POST   /reservations                     创建预约
POST   /reservations/{id}/cancel         取消预约
GET    /packages/{id}/affected-reservations  受影响的未来预约
"""
from __future__ import annotations

import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .errors import ServiceError
from .service import CurriculumService
from .store import Store


def _as_of(query: dict[str, list[str]]) -> date | None:
    raw = query.get("as_of", [None])[0]
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ServiceError("invalid_request", "as_of 必须是 YYYY-MM-DD", status=400) from exc


class Handler(BaseHTTPRequestHandler):
    server_version = "CurriculumService/1.0"

    # 由 make_server 注入
    service: CurriculumService

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静模式
        return

    # ------------------------------------------------------------ 基础框架

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ServiceError("invalid_json", "请求体不是合法 JSON", status=400) from exc
        if not isinstance(body, dict):
            raise ServiceError("invalid_request", "请求体必须是 JSON 对象", status=400)
        return body

    def _dispatch(
        self, method: str, pattern: str, handler: Callable[..., Any]
    ) -> bool:
        match = re.fullmatch(pattern, self.path_parsed)
        if not match:
            return False
        try:
            result = handler(**match.groupdict())
        except ServiceError as exc:
            self._send(exc.status, exc.to_dict())
            return True
        if result is None:
            self._send(204, {})
        else:
            status, body = result if isinstance(result, tuple) else (200, result)
            self._send(status, body)
        return True

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        parts = urlsplit(self.path)
        self.path_parsed = parts.path
        self.query = {k: v for k, v in parse_qs(parts.query).items()}
        svc = self.service
        try:
            if method == "GET":
                if self.path_parsed == "/health":
                    self._send(200, {"status": "ok"})
                    return
                if self.path_parsed == "/courses":
                    self._send(200, {"series": svc.store.list_series()})
                    return
                if self._dispatch("GET", r"/courses/(?P<code>[^/]+)", self._get_draft):
                    return
                if self._dispatch("GET", r"/courses/(?P<code>[^/]+)/packages", self._list_packages):
                    return
                if self._dispatch("GET", r"/packages/(?P<pid>\d+)/chain", self._chain):
                    return
                if self._dispatch(
                    "GET", r"/packages/(?P<pid>\d+)/affected-reservations", self._affected
                ):
                    return
                if self._dispatch("GET", r"/packages/(?P<pid>\d+)/compare", self._compare):
                    return
                if self._dispatch("GET", r"/packages/(?P<pid>\d+)", self._get_package):
                    return
            else:
                if self._dispatch("POST", r"/courses/(?P<code>[^/]+)/validate", self._validate):
                    return
                if self._dispatch("POST", r"/courses/(?P<code>[^/]+)/publish", self._publish):
                    return
                if self._dispatch(
                    "POST", r"/courses/(?P<code>[^/]+)/emergency-replace", self._replace
                ):
                    return
                if self._dispatch("POST", r"/courses/(?P<code>[^/]+)/suspensions", self._suspend):
                    return
                if self._dispatch("POST", r"/courses/(?P<code>[^/]+)/copy", self._copy):
                    return
                if self.path_parsed == "/courses":
                    self._send(201, {"saved": svc.save_draft(self._read_json())})
                    return
                if self._dispatch("POST", r"/reservations/(?P<rid>\d+)/cancel", self._cancel):
                    return
                if self.path_parsed == "/reservations":
                    self._send(201, svc.create_reservation(self._read_json()))
                    return
            self._send(404, {"code": "not_found", "message": f"没有此路由：{method} {self.path_parsed}"})
        except ServiceError as exc:
            self._send(exc.status, exc.to_dict())

    # ------------------------------------------------------------ 各端点

    def _get_draft(self, code: str) -> dict[str, Any]:
        return self.service.get_draft(code)

    def _list_packages(self, code: str) -> dict[str, Any]:
        return {"code": code, "packages": self.service.list_packages(code)}

    def _get_package(self, pid: str) -> dict[str, Any]:
        return self.service.get_package(int(pid))

    def _chain(self, pid: str) -> dict[str, Any]:
        return self.service.version_chain(int(pid))

    def _compare(self, pid: str) -> dict[str, Any]:
        other = self.query.get("with", [None])[0]
        if not other or not other.isdigit():
            raise ServiceError("invalid_request", "查询参数 with 必须是课程包 id", status=400)
        return self.service.compare_packages(int(pid), int(other))

    def _affected(self, pid: str) -> dict[str, Any]:
        return self.service.affected_future_reservations(int(pid))

    def _validate(self, code: str) -> dict[str, Any]:
        return self.service.validate_draft(code, as_of=_as_of(self.query))

    def _publish(self, code: str) -> dict[str, Any]:
        body = self._read_json()
        expected = self.query.get("expected_version", [None])[0]
        expected_version: int | None = None
        if expected is not None:
            if not expected.lstrip("-").isdigit():
                raise ServiceError("invalid_request", "expected_version 必须是整数", status=400)
            expected_version = int(expected)
        result = self.service.publish(
            code,
            operator=str(body.get("operator") or ""),
            expected_version=expected_version,
            as_of=_as_of(self.query),
        )
        return result

    def _replace(self, code: str) -> dict[str, Any]:
        body = self._read_json()
        node_id = body.get("node_id")
        replacement = body.get("replacement")
        reason = str(body.get("reason") or "")
        if not isinstance(node_id, str) or not isinstance(replacement, dict):
            raise ServiceError(
                "invalid_request", "需要 node_id（字符串）与 replacement（节点对象）", status=400
            )
        if not reason:
            raise ServiceError("invalid_request", "紧急替代必须填写 reason", status=400)
        return self.service.emergency_replace(
            code, node_id, replacement, reason=reason,
            operator=str(body.get("operator") or ""), as_of=_as_of(self.query),
        )

    def _suspend(self, code: str) -> dict[str, Any]:
        body = self._read_json()
        node_ids = body.get("node_ids")
        reason = str(body.get("reason") or "")
        if not isinstance(node_ids, list) or not all(isinstance(n, str) for n in node_ids):
            raise ServiceError("invalid_request", "node_ids 必须是字符串数组", status=400)
        if not reason:
            raise ServiceError("invalid_request", "局部停用必须填写 reason", status=400)
        return self.service.suspend_nodes(
            code, node_ids, reason=reason, operator=str(body.get("operator") or ""),
            as_of=_as_of(self.query),
        )

    def _copy(self, code: str) -> dict[str, Any]:
        body = self._read_json()
        new_code = body.get("new_code")
        if not isinstance(new_code, str) or not new_code.strip():
            raise ServiceError("invalid_request", "需要 new_code", status=400)
        return self.service.copy_course(
            code, new_code.strip(), title=body.get("title"),
            operator=str(body.get("operator") or ""),
        )

    def _cancel(self, rid: str) -> dict[str, Any]:
        return self.service.cancel_reservation(int(rid))


def make_server(host: str, port: int, store: Store) -> ThreadingHTTPServer:
    service = CurriculumService(store)

    class _Handler(Handler):
        pass

    _Handler.service = service
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.service = service  # type: ignore[attr-defined]
    httpd.store = store  # type: ignore[attr-defined]
    return httpd
