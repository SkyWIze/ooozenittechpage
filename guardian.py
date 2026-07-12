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
import time
import http.server
import urllib.request
import urllib.parse
from urllib.parse import urlsplit, parse_qs

def load_dotenv(dotenv_path):
    """Собственный парсер .env на чистом Python для сохранения нулевых зависимостей."""
    if os.path.exists(dotenv_path):
        try:
            with open(dotenv_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        os.environ[k] = v
        except Exception as e:
            print(f"[guardian] не удалось загрузить .env: {e}", flush=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# --- Конфигурация -------------------------------------------------------------
PORT = int(os.getenv("PORT", "3000"))
UPSTREAM_URL = os.getenv("UPSTREAM_URL", "").strip().rstrip("/")
UPSTREAM_TIMEOUT = float(os.getenv("GUARDIAN_TIMEOUT", "90"))
HEALTH_TIMEOUT = 4.0
# Проверять SSL-сертификат бота? Выключи (0/false), если UPSTREAM_URL — это https
# по «голому» IP: сертификат выписан на домен и не совпадёт с IP. Guardian ходит
# к своему же бэкенду, так что отключение проверки тут безопасно.
VERIFY_SSL = os.getenv("GUARDIAN_VERIFY_SSL", "1").strip().lower() not in ("0", "false", "no")

MAINT_FILE = os.path.join(BASE_DIR, "guardian_maintenance.html")
HOSTERR_FILE = os.path.join(BASE_DIR, "guardian_hosterror.html")
UPDATING_FILE = os.path.join(BASE_DIR, "guardian_updating.html")
LOGO_FILE = os.path.join(BASE_DIR, "logo", "zenit-logo.png")
FAVICON_FILE = os.path.join(BASE_DIR, "logo", "zenit-favicon.png")

# Режим «идёт обновление»: бот при старте пингует /__guardian/deploying → включается
# страница обновления; когда бот реально поднялся, он пингует /__guardian/ready →
# страница уходит РОВНО тогда. Окно (DEPLOY_WINDOW) — лишь страховка-максимум, чтобы
# страница не залипла навсегда, если «ready» так и не пришёл (деплой провалился).
GUARDIAN_TOKEN = os.getenv("GUARDIAN_TOKEN", "").strip()
DEPLOY_WINDOW_SEC = float(os.getenv("GUARDIAN_DEPLOY_WINDOW", "120"))
_deploying_until = 0.0

# Кэш статических файлов в памяти
# Структура: path -> {"status": int, "headers": list, "data": bytes}
_static_cache = {}

CACHEABLE_EXTENSIONS = (
    ".js", ".css", ".png", ".jpg", ".jpeg", ".svg", 
    ".ico", ".woff", ".woff2", ".ttf", ".txt", ".xml"
)

# «Предохранитель» против ошибок хоста (напр. disk I/O error → бот жив, но сайт
# отдаёт 500). После FAIL_THRESHOLD ошибок апстрима (5xx/обрыв) за FAIL_WINDOW секунд
# guardian уходит в режим техработ на UNHEALTHY_COOLDOWN секунд, чтобы юзеры не тыкали
# в сломанный сайт. По истечении окна следующий запрос снова пробует апстрим —
# если хост ожил, сайт возвращается сам.
FAIL_THRESHOLD = int(os.getenv("GUARDIAN_FAIL_THRESHOLD", "2"))
FAIL_WINDOW_SEC = float(os.getenv("GUARDIAN_FAIL_WINDOW", "30"))
UNHEALTHY_COOLDOWN_SEC = float(os.getenv("GUARDIAN_UNHEALTHY_COOLDOWN", "120"))
_fail_times = []
_unhealthy_until = 0.0
_was_unhealthy = False


def _unhealthy_active():
    return time.time() < _unhealthy_until


def _send_vk_notification(message):
    """Отправляет служебное уведомление в тех-чат ВК."""
    token = os.getenv("BOT_TOKEN", "").strip()
    peer_id_str = os.getenv("TECH_CHAT_PEER", "2000000001").strip()
    if not token or not peer_id_str:
        log("VK-оповещение пропущено: BOT_TOKEN или TECH_CHAT_PEER не заданы")
        return
    try:
        peer_id = int(peer_id_str)
    except ValueError:
        log(f"Некорректный TECH_CHAT_PEER: {peer_id_str}")
        return

    try:
        url = "https://api.vk.com/method/messages.send"
        params = {
            "peer_id": peer_id,
            "message": message,
            "random_id": 0,
            "access_token": token,
            "v": "5.131"
        }
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
            resp_data = json.loads(response.read().decode("utf-8"))
            if "error" in resp_data:
                log(f"Ошибка VK API при отправке уведомления: {resp_data['error']}")
            else:
                log("VK-оповещение успешно отправлено в тех-чат")
    except Exception as e:
        log(f"Не удалось отправить VK-оповещение: {type(e).__name__}: {e}")


def _record_failure(reason=""):
    """Фиксирует ошибку апстрима; при превышении порога — включает режим техработ."""
    global _unhealthy_until, _was_unhealthy
    now = time.time()
    _fail_times.append(now)
    cutoff = now - FAIL_WINDOW_SEC
    while _fail_times and _fail_times[0] < cutoff:
        _fail_times.pop(0)
    if len(_fail_times) >= FAIL_THRESHOLD and not _unhealthy_active():
        _unhealthy_until = now + UNHEALTHY_COOLDOWN_SEC
        _fail_times.clear()
        _was_unhealthy = True
        log(f"апстрим нездоров ({reason}) — режим техработ на {int(UNHEALTHY_COOLDOWN_SEC)}с")
        
        # Фоновое оповещение в ВК
        import threading
        msg = (
            "⚠️ Внимание: Обнаружены технические неполадки на сервере хостинга (ошибка базы данных или диска).\n\n"
            "🛡️ Прокси-сервер Guardian временно закрыл доступ к сайту и боту "
            "Скоро все придет в норму (надеюсь)."
        )
        threading.Thread(target=_send_vk_notification, args=(msg,), daemon=True, name="guardian-vk-notify").start()

# Заголовки, которые нельзя пробрасывать как есть (hop-by-hop).
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

_up = urlsplit(UPSTREAM_URL) if UPSTREAM_URL else None
# Bothost выдаёт самоподписанные сертификаты на *.bothost.tech, а апстрим — это наш
# же бот. Поэтому проверку сертификата апстрима отключаем безусловно (не зависим от
# переменной окружения, чтобы не было сюрпризов).
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def log(msg):
    print(f"[guardian] {msg}", flush=True)


def _deploying_active():
    return time.time() < _deploying_until


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


def _looks_down(status, body):
    """Когда слот бота лежит, центральный прокси Bothost отдаёт своё
    «404 page not found» (короткий текст). Это значит бот недоступен —
    отличаем от настоящих 404 самого приложения (там JSON/HTML)."""
    return status == 404 and b"page not found" in (body or b"")[:120].lower()


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
        body = resp.read()
        conn.close()
        if _looks_down(resp.status, body):
            return False, "Bothost 404 (слот бота лежит)"
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
        if _deploying_active():
            return self._serve_updating()
        if _unhealthy_active():
            # Предохранитель тикает только от 500 (хост-ошибка) → показываем её страницу.
            return self._serve_host_error()
        self._proxy()

    def _guardian_route(self, path):
        if path == "/__guardian/health":
            # В окне обновления тоже считаем «не готов», чтобы страница не уехала раньше.
            up = (not _deploying_active()) and upstream_up()
            return self._send_json(200 if up else 503, {"up": up})
        if path == "/__guardian/deploying":
            return self._deploying_control()
        if path == "/__guardian/ready":
            return self._ready_control()
        if path == "/__guardian/logo.png":
            return self._send_static(LOGO_FILE, "image/png")
        if path == "/__guardian/favicon.png":
            return self._send_static(FAVICON_FILE, "image/png")
        return self._send_json(404, {"error": "not found"})

    def _deploying_control(self):
        global _deploying_until
        if not GUARDIAN_TOKEN:
            return self._send_json(403, {"error": "GUARDIAN_TOKEN не задан"})
        qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        if (qs.get("token") or [""])[0] != GUARDIAN_TOKEN:
            return self._send_json(403, {"error": "неверный токен"})
        secs = DEPLOY_WINDOW_SEC
        if qs.get("seconds"):
            try:
                secs = float(qs["seconds"][0])
            except ValueError:
                pass
        _deploying_until = time.time() + max(0.0, secs)
        log(f"режим обновления включён (страховка {int(secs)}с)")
        return self._send_json(200, {"ok": True, "deploying_for_sec": int(secs)})

    def _ready_control(self):
        global _deploying_until, _static_cache
        if not GUARDIAN_TOKEN:
            return self._send_json(403, {"error": "GUARDIAN_TOKEN не задан"})
        qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        if (qs.get("token") or [""])[0] != GUARDIAN_TOKEN:
            return self._send_json(403, {"error": "неверный токен"})
        was = _deploying_active()
        _deploying_until = 0.0
        _static_cache.clear()
        log("бот сообщил о готовности — режим обновления снят, кэш статики ОЧИЩЕН")
        return self._send_json(200, {"ok": True, "deploying": False, "cache_cleared": True})

    def _proxy(self):
        path = self.path.split("?", 1)[0]
        is_get = (self.command == "GET")
        is_cacheable = is_get and path.lower().endswith(CACHEABLE_EXTENSIONS)

        # 1. Проверяем кэш статики в памяти
        if is_cacheable and path in _static_cache:
            cached = _static_cache[path]
            # log(f"CACHE HIT: {path}")
            self.send_response(cached["status"])
            for k, v in cached["headers"]:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(cached["data"])))
            self.send_header("X-Cache", "HIT-Guardian")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(cached["data"])
            self.close_connection = True
            return

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
            # Обрыв/таймаут — обычно редеплой или стоп слота: нейтральная заглушка.
            reason = f"{type(e).__name__}: {e}"
            log(f"UPSTREAM FAIL {self.command} {self.path} -> {UPSTREAM_URL} (Host={_up.netloc}): {reason}")
            _record_failure(f"Сбой подключения: {reason}")
            return self._serve_maintenance()

        # 500 — бот ЖИВ, но ошибка приложения (часто disk I/O error на хосте):
        # отдельная страница «проблемы на стороне хостинга» + предохранитель.
        if resp.status == 500:
            log(f"UPSTREAM 500 {self.command} {self.path} -> {UPSTREAM_URL}")
            _record_failure("HTTP 500 (ошибка диска/БД)")
            return self._serve_host_error()

        # 502/503/504 — слот недоступен (редеплой/стоп): нейтральная заглушка + предохранитель.
        if resp.status in (502, 503, 504):
            log(f"UPSTREAM {resp.status} {self.command} {self.path} -> {UPSTREAM_URL}")
            _record_failure(f"HTTP {resp.status} (сервер недоступен)")
            return self._serve_maintenance()

        if _looks_down(resp.status, data):
            log(f"UPSTREAM DOWN (Bothost 404) {self.command} {self.path} -> {UPSTREAM_URL}")
            _record_failure("Bothost 404 (контейнер выключен)")
            return self._serve_maintenance()

        # Успешный ответ (связь восстановлена)
        global _was_unhealthy
        if _was_unhealthy:
            _was_unhealthy = False
            log("✅ Связь с апстримом восстановлена — сайт снова работает!")
            import threading
            msg = (
                "🤖 Ассистент Ева | ООО «ЗЕНИТ»\n\n"
                "✅ Связь с сервером успешно восстановлена! Сайт ooo-zenitprov.ru снова работает в штатном режиме.\n\n"
                "🛡️ Все системы функционируют стабильно. Заглушка техработ автоматически отключена."
            )
            threading.Thread(target=_send_vk_notification, args=(msg,), daemon=True, name="guardian-vk-notify").start()

        # 2. Сохраняем успешный ответ статики в кэш
        if is_cacheable and resp.status == 200:
            cached_headers = [
                (k, v) for k, v in resp.getheaders()
                if k.lower() not in HOP_BY_HOP and k.lower() not in ("content-length", "connection")
            ]
            _static_cache[path] = {
                "status": resp.status,
                "headers": cached_headers,
                "data": data
            }
            log(f"CACHE MISS & SAVE: {path} ({len(data)} bytes)")

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

    def _serve_updating(self):
        try:
            data = _read_file(UPDATING_FILE)
        except Exception:
            data = b"<h1>Update in progress</h1>"
        self.send_response(503)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Retry-After", "5")
        self.send_header("Cache-Control", "no-store")
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

    def _serve_host_error(self):
        try:
            data = _read_file(HOSTERR_FILE)
        except Exception:
            data = b"<h1>Site temporarily unavailable (host issue)</h1>"
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
