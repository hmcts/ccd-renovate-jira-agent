import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import requests

BASE_ENV = {
    "GITHUB_TOKEN": "test-github-token",
    "JIRA_BASE_URL": "https://example.atlassian.net",
    "JIRA_USER_EMAIL": "test@example.com",
    "JIRA_API_TOKEN": "test-api-token",
}
with patch.dict(os.environ, BASE_ENV, clear=True):
    import main


class JiraAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.config = patch.multiple(
            main,
            JIRA_AUTH_MODE="cookie",
            JIRA_SESSION_COOKIE="cloud.session.token=test-session; other=test",
            JIRA_PAT="stale-pat",
            JIRA_API_BASE_URL=BASE_ENV["JIRA_BASE_URL"],
        )
        self.config.start()
        self.addCleanup(self.config.stop)

    def request(self, status=200, content_type="application/json", body="", **kwargs):
        response = requests.Response()
        response.status_code = status
        response._content = body.encode()
        response.headers["Content-Type"] = content_type
        # Inspect the prepared request, including requests' automatic auth behavior.
        with patch("requests.sessions.get_netrc_auth") as netrc, patch.object(
            main._jira_session, "send", return_value=response
        ) as send:
            result = main._jira_request("GET", "/rest/api/2/myself", **kwargs)
            netrc.assert_not_called()
        return result, send.call_args.args[0], send.call_args.kwargs

    def test_cookie_ignores_stale_tokens_and_disables_redirects(self):
        _, request, options = self.request(allow_redirects=True)
        self.assertEqual(request.headers["Cookie"], main.JIRA_SESSION_COOKIE)
        self.assertNotIn("Authorization", request.headers)
        self.assertFalse(options["allow_redirects"])

    def test_cookie_search_explicit_auth_uses_cookie(self):
        _, request, _ = self.request(auth=main.jira_request_auth(), headers=main.jira_auth())
        self.assertIn("Cookie", request.headers)
        self.assertNotIn("Authorization", request.headers)

    def test_expired_denied_redirect_and_login_html(self):
        for status, content_type in [(401, "application/json"), (403, "application/json"),
                                     (302, "text/plain"), (200, "text/html; charset=utf-8")]:
            with self.subTest(status=status, content_type=content_type):
                with self.assertRaisesRegex(requests.HTTPError, "Refresh JIRA_SESSION_COOKIE") as error:
                    self.request(status, content_type)
                self.assertNotIn("test-session", str(error.exception))

    def test_cookie_errors_distinguish_denied_session_and_xsrf(self):
        for status, body, reason in [
            (401, "", "rejected the session cookie"),
            (403, "", "denied this operation"),
            (403, "XSRF check failed: test-session", "CSRF/XSRF check"),
        ]:
            with self.subTest(status=status, body=body):
                with self.assertRaises(requests.HTTPError) as error:
                    self.request(status, body=body)
                message = str(error.exception)
                self.assertIn(f"HTTP {status} GET /rest/api/2/myself", message)
                self.assertIn(reason, message)
                self.assertNotIn("test-session", message)

    def test_no_content_success_is_not_a_login_page(self):
        for status in (204, 205):
            with self.subTest(status=status):
                response, _, _ = self.request(status, "text/html; charset=utf-8")
                self.assertEqual(response.status_code, status)

    def test_token_uses_gateway_without_cookie_or_stale_pat(self):
        with patch.multiple(main, JIRA_AUTH_MODE="token",
                            JIRA_API_BASE_URL="https://api.atlassian.com/ex/jira/cloud-id"):
            _, request, _ = self.request()
        self.assertEqual(request.url, "https://api.atlassian.com/ex/jira/cloud-id/rest/api/2/myself")
        self.assertTrue(request.headers["Authorization"].startswith("Basic "))
        self.assertNotIn("Cookie", request.headers)

    def test_pat_uses_bearer_without_cookie(self):
        with patch.object(main, "JIRA_AUTH_MODE", "pat"):
            _, request, _ = self.request()
        self.assertEqual(request.headers["Authorization"], "Bearer stale-pat")
        self.assertNotIn("Cookie", request.headers)

    def test_startup_validation_and_legacy_defaults(self):
        cases = [
            ({}, 0, "token"),
            ({"JIRA_PAT": "test-pat"}, 0, "pat"),
            ({"JIRA_AUTH_MODE": "token", "JIRA_PAT": "test-pat"}, 0, "token"),
            ({"JIRA_AUTH_MODE": "cookie", "JIRA_API_TOKEN": "", "JIRA_USER_EMAIL": "",
              "JIRA_SESSION_COOKIE": "cloud.session.token=test"}, 0, "cookie"),
            ({"JIRA_AUTH_MODE": "cookie"}, 1, "requires JIRA_SESSION_COOKIE"),
            ({"JIRA_AUTH_MODE": "cookie", "JIRA_SESSION_COOKIE": "a=b\nc=d"}, 1, "single-line"),
            ({"JIRA_AUTH_MODE": "cookie", "JIRA_SESSION_COOKIE": "a=b",
              "JIRA_API_BASE_URL": "https://api.atlassian.com/ex/jira/id"}, 1, "HTTPS Jira site origin"),
            ({"JIRA_AUTH_MODE": "cookie", "JIRA_SESSION_COOKIE": "a=b",
              "JIRA_BASE_URL": "http://example.atlassian.net"}, 1, "HTTPS Jira site origin"),
            ({"JIRA_AUTH_MODE": "token", "JIRA_API_TOKEN": "", "JIRA_PAT": "test-pat"}, 1, "requires"),
            ({"JIRA_AUTH_MODE": "pat"}, 1, "requires JIRA_PAT"),
            ({"JIRA_AUTH_MODE": "unknown"}, 1, "must be"),
            ({"JIRA_SESSION_COOKIE": "a=b", "JIRA_API_TOKEN": ""}, 1, "requires"),
        ]
        for overrides, code, message in cases:
            with self.subTest(overrides=overrides):
                result = subprocess.run(
                    [sys.executable, "-c", "import main; print(main.JIRA_AUTH_MODE)"],
                    env={**os.environ, **BASE_ENV, **overrides},
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn(message, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
