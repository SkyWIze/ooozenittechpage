import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import guardian


class GuardianHealthTests(unittest.TestCase):
    def setUp(self):
        guardian._up = urlsplit("https://bot.example.test")
        guardian._was_unhealthy = False
        guardian._fail_times = []
        guardian._recovery_pending = False
        guardian._recovery_successes = 0
        guardian._recovery_last_probe = 0.0
        guardian._failure_mode = "unavailable"
        guardian._failure_generation = 0

    @patch("guardian._open_upstream")
    def test_probe_requires_http_200(self, open_upstream):
        connection = open_upstream.return_value
        response = connection.getresponse.return_value
        response.read.return_value = b"ok"
        response.status = 200
        self.assertEqual(guardian._probe_upstream(1), (True, "HTTP 200"))
        connection.request.assert_called_once_with(
            "GET", "/ping", headers={"Host": "bot.example.test", "User-Agent": "ZenitGuardian-health"}
        )
        response.status = 404
        self.assertEqual(guardian._probe_upstream(1, "/"), (False, "HTTP 404"))

    @patch("guardian.threading.Thread")
    def test_unavailable_outage_uses_neutral_page_and_no_notification(self, thread):
        guardian._failure_mode = "host"
        for _ in range(guardian.FAIL_THRESHOLD):
            guardian._record_failure("Bothost 502", send_notification=False)
        self.assertTrue(guardian._unhealthy_active())
        self.assertEqual(guardian._failure_mode, "unavailable")
        thread.assert_not_called()
        handler = guardian.Handler.__new__(guardian.Handler)
        handler.path = "/"
        handler._serve_maintenance = MagicMock()
        handler._serve_host_error = MagicMock()
        with patch("guardian._deploying_active", return_value=False), patch("guardian.upstream_up", return_value=False):
            handler._dispatch()
        handler._serve_maintenance.assert_called_once()
        handler._serve_host_error.assert_not_called()

    @patch("guardian.threading.Thread")
    @patch("guardian._probe_upstream", return_value=(True, "HTTP 200"))
    def test_recovery_requires_three_spaced_ping_and_page_checks(self, probe, thread):
        guardian._recovery_pending = True
        guardian._was_unhealthy = True
        clock = [10.0]
        with patch("guardian.time.time", side_effect=lambda: clock[0]):
            self.assertFalse(guardian.upstream_up())
            clock[0] = 15.0
            self.assertFalse(guardian.upstream_up())
            clock[0] = 20.0
            self.assertTrue(guardian.upstream_up())
        self.assertEqual(probe.call_count, 6)
        self.assertFalse(guardian._unhealthy_active())
        self.assertFalse(guardian._was_unhealthy)
        thread.assert_called_once()
        self.assertIn("снова работает", thread.call_args.kwargs["args"][0])

    @patch("guardian._probe_upstream", return_value=(True, "HTTP 200"))
    def test_one_failure_does_not_reload_maintenance_page_immediately(self, probe):
        guardian._record_failure("HTTP 502", send_notification=False)
        self.assertFalse(guardian._unhealthy_active())
        clock = [10.0]
        with patch("guardian.time.time", side_effect=lambda: clock[0]):
            self.assertFalse(guardian.upstream_up())
            clock[0] = 15.0
            self.assertFalse(guardian.upstream_up())
            clock[0] = 20.0
            self.assertTrue(guardian.upstream_up())
        self.assertEqual(probe.call_count, 6)
        self.assertFalse(guardian._fail_times)

    @patch("guardian.threading.Thread")
    @patch("guardian._probe_upstream")
    def test_failed_page_resets_recovery_and_suppresses_notice(self, probe, thread):
        guardian._recovery_pending = True
        guardian._was_unhealthy = True
        probe.side_effect = [
            (True, "HTTP 200"), (True, "HTTP 200"),
            (True, "HTTP 200"), (False, "HTTP 502"),
            (True, "HTTP 200"), (True, "HTTP 200"),
        ]
        clock = [10.0]
        with patch("guardian.time.time", side_effect=lambda: clock[0]):
            self.assertFalse(guardian.upstream_up())
            clock[0] = 15.0
            self.assertFalse(guardian.upstream_up())
            clock[0] = 20.0
            self.assertFalse(guardian.upstream_up())
        self.assertTrue(guardian._unhealthy_active())
        self.assertEqual(guardian._recovery_successes, 1)
        thread.assert_not_called()

    @patch("guardian._probe_upstream")
    def test_parallel_recovery_check_waits_for_first_probe(self, probe):
        guardian._recovery_pending = True
        guardian._recovery_probe_lock.acquire()
        try:
            self.assertFalse(guardian.upstream_up())
        finally:
            guardian._recovery_probe_lock.release()
        probe.assert_not_called()
        self.assertEqual(guardian._recovery_successes, 0)

    @patch("guardian._probe_upstream")
    def test_ready_does_not_clear_deployment_when_page_fails(self, probe):
        probe.side_effect = [(True, "HTTP 200"), (False, "HTTP 502")]
        handler = guardian.Handler.__new__(guardian.Handler)
        handler.path = "/__guardian/ready?token=secret"
        handler._send_json = MagicMock()
        with patch.object(guardian, "GUARDIAN_TOKEN", "secret"), patch.object(guardian, "_deploying_until", 500):
            handler._ready_control()
            self.assertEqual(guardian._deploying_until, 500)
        handler._send_json.assert_called_once_with(503, {"error": "бот или главная страница ещё не готовы"})


if __name__ == "__main__":
    unittest.main()
