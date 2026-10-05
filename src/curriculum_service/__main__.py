"""命令行入口：python -m curriculum_service [--db PATH] [--host H] [--port P]"""
from __future__ import annotations

import argparse

from .server import make_server
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="主题课程依赖发布后端")
    parser.add_argument("--db", default="data/curriculum.db", help="SQLite 路径（默认 data/curriculum.db）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    store = Store(args.db)
    httpd = make_server(args.host, args.port, store)
    print(f"课程依赖发布服务已启动：http://{args.host}:{args.port} （数据库 {args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()


if __name__ == "__main__":
    main()
