import os
import unittest
from unittest.mock import Mock, patch

with patch.dict(os.environ, {
    "GITHUB_TOKEN": "test", "JIRA_BASE_URL": "https://example.atlassian.net",
    "JIRA_AUTH_MODE": "token", "JIRA_USER_EMAIL": "test@example.com", "JIRA_API_TOKEN": "test",
}, clear=True):
    import main


class JiraReleaseApproachTests(unittest.TestCase):
    def setUp(self):
        main.jira_resolve_release_approach_field.cache_clear()
        self.addCleanup(main.jira_resolve_release_approach_field.cache_clear)
        self.metadata = Mock()
        self.metadata.json.return_value = [
            {"id": "customfield_10123", "name": "Release approach"},
            {"id": "customfield_10124", "name": "Other field"},
        ]

    def test_create_sends_discovered_cloud_field_and_caches_lookup(self):
        created = Mock(status_code=201)
        created.json.return_value = {"key": "CCD-123"}
        with patch.object(main, "MODE", "run"), patch.object(
            main, "_jira_request", side_effect=[self.metadata, created, created]
        ) as request:
            for _ in range(2):
                main.jira_create_issue(
                    "Update dependency", "Description", [], "CCD", "3-Medium",
                    release_approach_field="auto", release_approach_value="Tier 1: CI/CD",
                )
                fields = request.call_args.kwargs["json"]["fields"]
                self.assertEqual(fields["customfield_10123"], {"value": "Tier 1: CI/CD"})
                self.assertNotIn("auto", fields)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args_list[0].args, ("GET", f"/rest/api/{main.JIRA_API_VERSION}/field"))

    def test_explicit_id_and_disabled_field_skip_discovery(self):
        with patch.object(main, "_jira_request") as request:
            self.assertEqual(main.jira_resolve_release_approach_field("customfield_123"), "customfield_123")
            self.assertEqual(main.jira_resolve_release_approach_field(""), "")
            request.assert_not_called()

    def test_missing_or_ambiguous_field_prevents_creation(self):
        for fields in [[], self.metadata.json.return_value[:1] * 2]:
            with self.subTest(fields=fields):
                main.jira_resolve_release_approach_field.cache_clear()
                self.metadata.json.return_value = fields
                with patch.object(main, "MODE", "run"), patch.object(
                    main, "_jira_request", return_value=self.metadata
                ) as request:
                    with self.assertRaisesRegex(RuntimeError, "Set jira.release_approach_field"):
                        main.jira_create_issue(
                            "Update", "Description", [], "CCD", "3-Medium",
                            release_approach_field="auto", release_approach_value="Tier 1: CI/CD",
                        )
                    self.assertEqual(request.call_count, 1)

    def test_sync_reads_and_updates_discovered_field_only_when_needed(self):
        for current in [None, {"value": "Tier 1: CI/CD", "id": "123"}]:
            with self.subTest(current=current), patch.object(
                main, "_jira_request", return_value=self.metadata
            ), patch.object(main, "JIRA_EPIC_KEY", ""), patch.object(
                main, "jira_get_issue", return_value={"fields": {"customfield_10123": current}}
            ) as get_issue, patch.object(main, "jira_update_issue") as update:
                main.jira_ensure_ticket_fields(
                    "CCD-123", [], "", release_approach_field="auto",
                    release_approach_value="Tier 1: CI/CD",
                )
                get_issue.assert_called_once_with("CCD-123", "customfield_10123")
                if current is None:
                    update.assert_called_once_with(
                        "CCD-123", {"customfield_10123": {"value": "Tier 1: CI/CD"}},
                        allow_in_dry_run=False,
                    )
                else:
                    update.assert_not_called()

    def test_dry_run_discovers_without_creating(self):
        with patch.object(main, "MODE", "dry-run"), patch.object(
            main, "_jira_request", return_value=self.metadata
        ) as request:
            main.jira_create_issue(
                "Update", "Description", [], "CCD", "3-Medium",
                release_approach_field="auto", release_approach_value="Tier 1: CI/CD",
            )
            request.assert_called_once_with("GET", f"/rest/api/{main.JIRA_API_VERSION}/field")


if __name__ == "__main__":
    unittest.main()
