import http.client
import json
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import guardian


class GuardianManualTests(unittest.TestCase):
    def setUp(self):
        guardian._manual_locked = False
        guardian._manual_code_digest = ""
        guardian._manual_generation = ""
        guardian._code_attempts.clear()
        guardian._deploying_until = 0.0
        guardian._recovery_pending = False
        guardian._fail_times.clear()
        self.token = patch.object(guardian, "GUARDIAN_TOKEN", "test-control-secret")
        self.probe = patch.object(guardian, "_probe_upstream", return_value=(True, "HTTP 200"))
        self.proxy = patch.object(
            guardian.Handler, "_proxy",
            lambda handler: handler._send_json(200, {"proxied": True}),
        )
        self.token.start()
        self.probe.start()
        self.proxy.start()
        self.server = guardian.ThreadingHTTPServer(("127.0.0.1", 0), guardian.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.proxy.stop()
        self.probe.stop()
        self.token.stop()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = (response.status, response.read(), dict(response.getheaders()))
        connection.close()
        return result

    def control(self, action, code="", token="test-control-secret"):
        payload = json.dumps({"action": action, "code": code}).encode()
        return self.request(
            "POST", "/__guardian/manual", payload,
            {"Content-Type": "application/json", "X-Guardian-Token": token},
        )

    def test_manual_lock_code_cookie_and_open(self):
        code = "private-code-long-enough"
        self.assertEqual(self.control("close", code)[0], 200)
        status, body, _ = self.request(
            "GET", "/__guardian/manual", headers={"X-Guardian-Token": "test-control-secret"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["manual_locked"])
        status, page, _ = self.request("GET", "/")
        self.assertEqual(status, 503)
        self.assertIn("КОД ДОСТУПА".encode(), page)
        self.assertNotIn(code.encode(), page)
        self.assertEqual(self.request("GET", "/__guardian/health")[0], 503)

        wrong = urlencode({"code": "wrong"}).encode()
        self.assertEqual(self.request("POST", "/__guardian/access", wrong)[0], 503)
        valid = urlencode({"code": code}).encode()
        status, _, headers = self.request("POST", "/__guardian/access", valid)
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"]
        self.assertIn("Secure; HttpOnly; SameSite=Lax", cookie)
        self.assertNotIn(code, cookie)
        self.assertEqual(self.request("GET", "/", headers={"Cookie": cookie.split(";", 1)[0]})[0], 200)
        self.assertEqual(self.request("GET", "/")[0], 503)

        self.assertEqual(self.request("GET", "/__guardian/ready?token=test-control-secret")[0], 200)
        self.assertEqual(self.request("GET", "/")[0], 503)
        self.assertEqual(self.control("open")[0], 200)
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.control("close", "another-private-code")[0], 200)
        self.assertEqual(self.request("GET", "/", headers={"Cookie": cookie.split(";", 1)[0]})[0], 503)

    def test_control_requires_secret_and_limits_code_attempts(self):
        self.assertEqual(self.control("close", "private-code-long-enough", token="wrong")[0], 403)
        self.assertEqual(self.request("GET", "/__guardian/manual")[0], 403)
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.control("close", "private-code-long-enough")[0], 200)
        wrong = urlencode({"code": "wrong"}).encode()
        for _ in range(8):
            self.assertEqual(self.request("POST", "/__guardian/access", wrong)[0], 503)
        status, page, _ = self.request("POST", "/__guardian/access", wrong)
        self.assertEqual(status, 503)
        self.assertIn("Слишком много попыток".encode(), page)

    def test_recovery_notice_waits_while_site_is_manually_closed(self):
        self.assertEqual(self.control("close", "private-code-long-enough")[0], 200)
        guardian._recovery_pending = True
        guardian._was_unhealthy = True
        clock = [10.0]
        with patch("guardian.time.time", side_effect=lambda: clock[0]), patch("guardian.threading.Thread") as notification_thread:
            self.assertFalse(guardian.upstream_up())
            clock[0] = 15.0
            self.assertFalse(guardian.upstream_up())
            clock[0] = 20.0
            self.assertTrue(guardian.upstream_up())
            notification_thread.assert_not_called()
        self.assertTrue(guardian._manual_locked)


if __name__ == "__main__":
    unittest.main()
