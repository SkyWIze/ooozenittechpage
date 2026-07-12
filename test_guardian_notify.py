import os
import json
import ssl
import urllib.request
import urllib.parse

def load_dotenv(dotenv_path):
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
            print(f"Не удалось загрузить .env: {e}")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

def test_vk_notification():
    token = os.getenv("BOT_TOKEN", "").strip()
    peer_id_str = os.getenv("TECH_CHAT_PEER", "2000000001").strip()
    
    print("=== ТЕСТ ОПОВЕЩЕНИЙ GUARDIAN ===")
    print(f"Используемый токен: {token[:10]}...{token[-10:] if len(token) > 20 else ''}")
    print(f"Используемый ID чата: {peer_id_str}")
    
    if not token:
        print("❌ Ошибка: BOT_TOKEN пустой в файле .env!")
        return
    if not peer_id_str:
        print("❌ Ошибка: TECH_CHAT_PEER пустой в файле .env!")
        return
        
    try:
        peer_id = int(peer_id_str)
    except ValueError:
        print(f"❌ Ошибка: Некорректный формат TECH_CHAT_PEER: {peer_id_str}")
        return

    message = "🔔 [Guardian Test] Проверка связи! Оповещения от прокси-сервера настроены верно."

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
        
        print("Отправка запроса к VK API...")
        with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
            resp_data = json.loads(response.read().decode("utf-8"))
            if "error" in resp_data:
                print(f"❌ Ошибка VK API: {resp_data['error']}")
            else:
                print("✅ УСПЕХ! Сообщение успешно отправлено. Проверьте ваш чат в ВК!")
    except Exception as e:
        print(f"❌ Не удалось отправить запрос: {type(e).__name__}: {e}")

if __name__ == "__main__":
    test_vk_notification()
