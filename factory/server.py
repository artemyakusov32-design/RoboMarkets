"""HTTP-адаптер арены и интерфейс наблюдения. Запуск: python3 -m factory.server."""
import argparse
import hmac
import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .runs import ApiError, RunStore

WEB = Path(__file__).resolve().parent.parent / "web"
logger = logging.getLogger(__name__)


def make_server(host="127.0.0.1", port=4001, token=None, store=None):
    store = store or RunStore()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logger.info(fmt, *args)

        def send(self, status, body, content_type="application/json; charset=utf-8"):
            raw = json.dumps(body, ensure_ascii=False).encode() if content_type.startswith("application/json") else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Arena-Run, X-Arena-Spec, X-Arena-Mode, X-Arena-Seq")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_OPTIONS(self):
            self.send(204, b"", "text/plain")

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def dispatch(self):
            try:
                path = self.path.split("?", 1)[0]
                if self.command == "GET" and path in ("/", "/app.js", "/style.css"):
                    name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[path]
                    mime = {"/": "text/html", "/app.js": "text/javascript", "/style.css": "text/css"}[path]
                    return self.send(200, (WEB / name).read_bytes(), mime + "; charset=utf-8")
                if token and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    raise ApiError("unauthorized", "Нужен Bearer-токен", 401)
                spec = self.headers.get("X-Arena-Spec")
                if spec and spec != "v1":
                    raise ApiError("unsupported_spec", "Поддерживается только v1", 400)
                if self.command == "GET":
                    if path == "/v1/health":
                        return self.send(200, {"status": "ok"})
                    if path == "/v1/info":
                        return self.send(200, dict(name="ОПД · Завод роботов", version="0.1.0", project="factory", specVersion="v1"))
                    if path == "/api/runs":
                        return self.send(200, {"runs": store.summaries()})
                    match = re.fullmatch(r"/api/runs/([A-Za-z0-9_-]+)", path)
                    if match:
                        return self.send(200, store.snapshot(match[1]))
                match = re.fullmatch(r"/v1/runs/([A-Za-z0-9_-]+)/(start|event|finish)", path)
                if self.command != "POST" or not match:
                    raise ApiError("not_found", "Маршрут не найден", 404)
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 2 * 1024 * 1024:
                        raise ApiError("bad_size", "Допустимо JSON-тело до 2 МБ", 413)
                    self.connection.settimeout(5)
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict):
                        raise ValueError()
                except (ValueError, UnicodeDecodeError):
                    raise ApiError("bad_json", "Ожидается JSON-объект")
                seq = self.headers.get("X-Arena-Seq")
                if match[2] == "event" and seq is not None and str(body.get("seq")) != seq:
                    raise ApiError("bad_seq", "seq тела и заголовка различаются")
                result = getattr(store, match[2])(match[1], body)
                self.send(200, result)
            except ApiError as exc:
                self.send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                logger.exception("Ошибка обработки запроса")
                self.send(500, {"error": {"code": "internal_error", "message": "Ошибка сервиса"}})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4001)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = make_server(args.host, args.port, os.environ.get("ARENA_TOKEN"))
    print("Завод запущен: http://{}:{}".format(args.host, server.server_port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
