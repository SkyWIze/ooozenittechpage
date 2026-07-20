# PROJECT_MEMORY.md

## Архитектура
- `guardian.py` — автономный прокси-сервер ("привратник") на стандартной библиотеке Python (`http.server`, `socketserver`), стоящий перед бэкендом.
- `guardian_maintenance.html`, `guardian_hosterror.html`, `guardian_updating.html` — HTML-страницы заглушек для режима техработ, хост-ошибок и авто-обновления бота.
- `test_guardian_health.py`, `test_guardian_notify.py` — юнит-тесты доступности бэкенда и ВК-уведомлений.

## Зависимости
- Только стандартная библиотека Python (без внешних pip-пакетов).

## Принятые решения и грабли
- **Обработка обрыва соединения клиентом (`BrokenPipeError` / `ConnectionResetError`)**:
  - *Проблема*: При разрыве соединения со стороны клиента (закрытие вкладки браузера, отмена загрузки статики `.js`/`.css`) стандартный `socketserver` в Python выводил длинный стектрейс `BrokenPipeError: [Errno 32] Broken pipe`.
  - *Решение*: В `Handler.handle()` и `ThreadingHTTPServer.handle_error()` добавлена безысключительная обработка обрыва сокета (`BrokenPipeError`, `ConnectionResetError`, `ConnectionAbortedError`, сетевые `OSError` с `errno` 32/104/103/10054/10053), чтобы не засорять логи.

## Текущий контекст
- Обработка раннего отключения клиентов исправлена в [guardian.py](file:///c:/Users/gfnma/OneDrive/Desktop/zenit-guardian/guardian.py).
