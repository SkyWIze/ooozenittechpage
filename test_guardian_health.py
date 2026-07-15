import sys
import os
import unittest
from unittest.mock import patch, MagicMock

# Импортируем тестируемый файл guardian
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import guardian

class TestGuardianHealth(unittest.TestCase):
    
    def setUp(self):
        # Сбрасываем глобальные переменные перед каждым тестом
        guardian._was_unhealthy = False
        guardian._unhealthy_until = 0.0
        guardian._fail_times = []
        
    @patch('guardian._open_upstream')
    def test_probe_upstream_calls_ping(self, mock_open):
        print("\n[1/3] Check: _probe_upstream requests /ping")
        # Настраиваем мок соединения
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"status": "ok"}'
        mock_conn.getresponse.return_value = mock_resp
        mock_open.return_value = mock_conn
        
        ok, detail = guardian._probe_upstream(timeout=1.0)
        
        # Проверяем, что запрос ушел именно на /ping
        mock_conn.request.assert_called_once_with(
            "GET", 
            "/ping", 
            headers={"Host": guardian._up.netloc if guardian._up else "", "User-Agent": "ZenitGuardian-health"}
        )
        self.assertTrue(ok)
        print("[OK] _probe_upstream requests the /ping endpoint successfully!")
        
    @patch('guardian._send_vk_notification')
    @patch('guardian._probe_upstream')
    def test_upstream_up_sends_notification_on_recovery(self, mock_probe, mock_send_vk):
        print("\n[2/3] Check: Reset unhealthy state and notify VK on recovery")
        
        # Имитируем, что система БЫЛА в аварийном состоянии
        guardian._was_unhealthy = True
        
        # Имитируем успешный пинг БД
        mock_probe.return_value = (True, "HTTP 200")
        
        # Вызываем проверку
        ok = guardian.upstream_up()
        
        self.assertTrue(ok)
        # Проверяем, что флаг сбросился
        self.assertFalse(guardian._was_unhealthy)
        # Проверяем, что уведомление о восстановлении было отправлено в ВК
        mock_send_vk.assert_called_once()
        args, kwargs = mock_send_vk.call_args
        self.assertIn("Связь с сервером успешно восстановлена", args[0])
        print("[OK] Recovered status transitions correctly and VK is notified!")

    @patch('guardian._send_vk_notification')
    @patch('guardian._probe_upstream')
    def test_upstream_up_does_not_send_if_already_healthy(self, mock_probe, mock_send_vk):
        print("\n[3/3] Check: No recovery notification spam when already healthy")
        
        # Система УЖЕ здорова (авария не была зафиксирована)
        guardian._was_unhealthy = False
        mock_probe.return_value = (True, "HTTP 200")
        
        ok = guardian.upstream_up()
        
        self.assertTrue(ok)
        # Уведомление отправляться не должно
        mock_send_vk.assert_not_called()
        print("[OK] No redundant recovery notifications if already healthy!")

if __name__ == "__main__":
    print("=== GUARDIAN MONITORING TESTS ===")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestGuardianHealth)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
