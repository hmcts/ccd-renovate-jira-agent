import os
import unittest
import requests
from unittest.mock import Mock, patch

with patch.dict(os.environ, {
    "GITHUB_TOKEN": "test", "JIRA_BASE_URL": "https://example.atlassian.net",
    "JIRA_AUTH_MODE": "token", "JIRA_USER_EMAIL": "test@example.com", "JIRA_API_TOKEN": "test",
}, clear=True):
    import main


class JiraTransitionTests(unittest.TestCase):
    def test_cookie_transition_204_html_header_succeeds_and_clears_cache(self):
        transitions = requests.Response()
        transitions.status_code = 200
        transitions.headers["Content-Type"] = "application/json"
        transitions._content = b'{"transitions": [{"id": "331", "name": "Resume Development", "to": {"name": "To Do"}}]}'
        success = requests.Response()
        success.status_code = 204
        success.headers["Content-Type"] = "text/html; charset=utf-8"
        success._content = b""
        with patch.multiple(main, MODE="run", JIRA_AUTH_MODE="cookie", JIRA_SESSION_COOKIE="a=b"), patch.object(
            main, "jira_get_status_name", return_value="Blocked"
        ), patch.object(main, "_clear_jira_issue_cache") as clear_cache, patch.object(
            main._jira_session, "request", side_effect=[transitions, success]
        ) as request:
            self.assertTrue(main.jira_transition_issue("CCD-8446", "To Do"))
            clear_cache.assert_called_once_with("CCD-8446")
            self.assertEqual(request.call_args.args[0], "POST")
            self.assertEqual(request.call_args.kwargs["json"], {"transition": {"id": "331"}})

    def test_already_to_do_needs_no_transition(self):
        with patch.object(main, "MODE", "run"), patch.object(
            main, "jira_get_status_name", return_value="To Do"
        ), patch.object(main, "_jira_request") as request:
            self.assertTrue(main.jira_transition_issue("CCD-8445", "To Do"))
            request.assert_not_called()

    def test_selects_action_by_destination_status(self):
        self.check_transitions([
            {"id": "331", "name": "Resume Development", "to": {"name": "To Do"}},
        ], "331")

    def test_path_already_to_do_does_not_reenter_blocked(self):
        with patch.object(main, "MODE", "run"), patch.object(
            main, "jira_get_status_name", return_value="To Do"
        ), patch.object(main, "_jira_request") as request:
            self.assertTrue(main.jira_transition_issue_path("CCD-8445", ["Blocked", "To Do"]))
            request.assert_not_called()

    def test_exact_action_name_keeps_precedence(self):
        self.check_transitions([
            {"id": "1", "name": "Add to Backlog", "to": {"name": "To Do"}},
            {"id": "2", "name": "To Do", "to": {"name": "To Do"}},
        ], "2")

    def test_ambiguous_or_unavailable_destination_does_not_post(self):
        for transitions in [[], [
            {"id": "1", "name": "First action", "to": {"name": "To Do"}},
            {"id": "2", "name": "Second action", "to": {"name": "To Do"}},
        ]]:
            with self.subTest(transitions=transitions):
                self.check_transitions(transitions, None)

    def check_transitions(self, transitions, expected_id):
        response = Mock()
        response.json.return_value = {"transitions": transitions}
        with patch.object(main, "MODE", "run"), patch.object(
            main, "jira_get_status_name", return_value="Blocked"
        ), patch.object(main, "_clear_jira_issue_cache"), patch.object(
            main, "_jira_request", return_value=response
        ) as request:
            result = main.jira_transition_issue("CCD-8445", "To Do")
            self.assertEqual(result, expected_id is not None)
            if expected_id:
                self.assertEqual(request.call_count, 2)
                self.assertEqual(request.call_args.args[0], "POST")
                self.assertEqual(request.call_args.kwargs["json"], {"transition": {"id": expected_id}})
            else:
                self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
