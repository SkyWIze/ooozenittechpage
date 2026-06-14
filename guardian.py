"""
Guardian — «привратник» сайта ООО Зенит.

Зачем: Bothost отдаёт «Bad Gateway», когда контейнер основного бота не отвечает
(редеплой / падение / OOM / бот выключен). Этот ОТДЕЛЬНЫЙ маленький контейнер стоит
перед ботом, держит публичный порт ВСЕГДА и переживает любые редеплои бота.

    домен -> [GUARDIAN :3000]  -> проксирует -> UPSTREAM_URL (контейнер бота)
                   |
                   └─ бот недоступен → красивая страница «Сайт временно недоступен»
                      (с авто-перезагрузкой: оживёт бот — посетителя вернёт на сайт)

Зависимостей нет — только стандартная библиотека Python.

Переменные окружения:
    PORT            публичный порт guardian (по умолчанию 3000)
    UPSTREAM_URL    адрес контейнера бота, напр. https://zenit-bot.bothost.ru
                    (ОБЯЗАТЕЛЬНО; без него guardian всегда показывает заглушку)
    GUARDIAN_TIMEOUT таймаут запроса к боту в секундах (по умолчанию 30)
"""

import http.client
import json
import os
import socketserver
import ssl
import http.server
from urllib.parse import urlsplit

# --- Конфигурация -------------------------------------------------------------
PORT = int(os.getenv("PORT", "3000"))
UPSTREAM_URL = os.getenv("UPSTREAM_URL", "").strip().rstrip("/")
UPSTREAM_TIMEOUT = float(os.getenv("GUARDIAN_TIMEOUT", "30"))
HEALTH_TIMEOUT = 4.0
# Проверять SSL-сертификат бота? Выключи (0/false), если UPSTREAM_URL — это https
# по «голому» IP: сертификат выписан на домен и не совпадёт с IP. Guardian ходит
# к своему же бэкенду, так что отключение проверки тут безопасно.
VERIFY_SSL = os.getenv("GUARDIAN_VERIFY_SSL", "1").strip().lower() not in ("0", "false", "no")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAINT_FILE = os.path.join(BASE_DIR, "guardian_maintenance.html")
LOGO_FILE = os.path.join(BASE_DIR, "logo", "zenit-logo.png")
FAVICON_FILE = os.path.join(BASE_DIR, "logo", "zenit-favicon.png")

# Заголовки, которые нельзя пробрасывать как есть (hop-by-hop).
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

_up = urlsplit(UPSTREAM_URL) if UPSTREAM_URL else None
_ssl_ctx = ssl.create_default_context()
if not VERIFY_SSL:
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE


def log(msg):
    print(f"[guardian] {msg}", flush=True)


def _open_upstream(timeout):
    """Создаёт соединение к боту по UPSTREAM_URL (http или https)."""
    if not _up:
        raise RuntimeError("UPSTREAM_URL не задан")
    host = _up.hostname
    if _up.scheme == "https":
        port = _up.port or 443
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=_ssl_ctx)
    port = _up.port or 80
    return http.client.HTTPConnection(host, port, timeout=timeout)


def upstream_up():
    """Быстрая проверка доступности бота (для авто-перезагрузки страницы)."""
    ok, _ = _probe_upstream(HEALTH_TIMEOUT)
    return ok


def _probe_upstream(timeout):
    """Возвращает (доступен, детали) — для логов диагностики."""
    if not _up:
        return False, "UPSTREAM_URL не задан"
    try:
        conn = _open_upstream(timeout)
        conn.request("GET", "/", headers={"Host": _up.netloc, "User-Agent": "ZenitGuardian-health"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return (resp.status < 500), f"HTTP {resp.status}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _read_file(path):
    with open(path, "rb") as f:
        return f.read()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ZenitGuardian"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self): self._dispatch()
    def do_POST(self): self._dispatch()
    def do_PUT(self): self._dispatch()
    def do_DELETE(self): self._dispatch()
    def do_PATCH(self): self._dispatch()
    def do_HEAD(self): self._dispatch()
    def do_OPTIONS(self): self._dispatch()

    def _dispatch(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/__guardian/"):
            return self._guardian_route(path)
        self._proxy()

    def _guardian_route(self, path):
        if path == "/__guardian/health":
            up = upstream_up()
            return self._send_json(200 if up else 503, {"up": up})
        if path == "/__guardian/logo.png":
            return self._send_static(LOGO_FILE, "image/png")
        if path == "/__guardian/favicon.png":
            return self._send_static(FAVICON_FILE, "image/png")
        return self._send_json(404, {"error": "not found"})

    def _proxy(self):
        # читаем тело запроса (если есть)
        body = None
        length = self.headers.get("Content-Length")
        if length is not None:
            try:
                body = self.rfile.read(int(length))
            except Exception:
                return self._serve_maintenance()

        try:
            conn = _open_upstream(UPSTREAM_TIMEOUT)
            fwd_headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in HOP_BY_HOP}
            # Host должен указывать на бота, иначе прокси Bothost не сроутит
            fwd_headers["Host"] = _up.netloc
            conn.request(self.command, self.path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
        except Exception as e:
            log(f"UPSTREAM FAIL {self.command} {self.path} -> {UPSTREAM_URL} (Host={_up.netloc}): {type(e).__name__}: {e}")
            return self._serve_maintenance()

        if resp.status in (502, 503, 504):
            log(f"UPSTREAM {resp.status} {self.command} {self.path} -> {UPSTREAM_URL}")
            return self._serve_maintenance()

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.close_connection = True

    def _serve_maintenance(self):
        try:
            data = _read_file(MAINT_FILE)
        except Exception:
            data = b"<h1>Site temporarily unavailable</h1>"
        self.send_response(503)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Retry-After", "10")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.close_connection = True

    def _send_static(self, path, content_type):
        try:
            data = _read_file(path)
        except Exception:
            return self._send_json(404, {"error": "file not found"})
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.close_connection = True

    def _send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.close_connection = True


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if not UPSTREAM_URL:
        log("ВНИМАНИЕ: UPSTREAM_URL не задан — guardian будет всегда показывать заглушку.")
    else:
        log(f"апстрим (бот): {UPSTREAM_URL}")
    log(f"слушаю публичный порт {PORT}")
    ok, detail = _probe_upstream(8.0)
    log(f"проверка апстрима при старте: {'ДОСТУПЕН' if ok else 'НЕ ДОСТУПЕН'} ({detail})")
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log("остановлен")


if __name__ == "__main__":
    main()
