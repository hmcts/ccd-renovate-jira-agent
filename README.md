# Renovate -> Jira Agent v1

Supports per-repo configuration via `.github/renovate-jira.yml`.

## Install Requirements

```bash
pip install -r requirements.txt
python -m pip install -r requirements.txt
```

## Environment Variables

```bash
export MODE=dry-run # or run
export REPO_LIST_FILE=./repo-list.txt
export TEST_PR_NUMBER=1234 # Optional: process only this PR number
export MAX_NEW_JIRA_TICKETS=1 # Optional: stop after creating this many new Jira tickets
export PR_PROCESS_DELAY_SECONDS=0 # Optional: delay between PRs; increase only if you hit rate limits
export GITHUB_TOKEN=<YOUR-GITHUB-FINE-GRAINED-TOKEN>
export JIRA_BASE_URL=https://tools.hmcts.net/jira

#export JIRA_USER_EMAIL=<YOUR_JIRA_LOGIN_EMAIL_ADDRESS>

#If using PAT authentication, use JIRA_PAT instead of the above two variables
export JIRA_PAT=<YOUR_JIRA_PAT>

export JIRA_API_VERSION=2
export VERBOSE_JIRA_DEDUPE=false # Optional: extra diagnostics for Jira dedupe
export LOG_TIMINGS=true # Optional: log per-PR, per-repo, and total runtime timings
export JIRA_RELEASE_APPROACH_FIELD=customfield_12345 # Optional: Jira custom field id for "Release approach"
export JIRA_RELEASE_APPROACH_VALUE="Tier 1: CI/CD" # Optional: select value for Release approach
```

When `MODE=dry-run`, no new Jira tickets are created unless a specific `*_even_in_dry_mode` behavior is enabled. Most behavior flags now live in repo config, for example `jira.fix_ticket_labels`, `jira.fix_components_even_in_dry_mode`, `jira.sync_mend_confidence`, `jira.sync_mend_confidence_even_in_dry_mode`, `jira.mend_confidence_label_prefix`, `jira.withdraw_duplicate_tickets`, `jira.target_status_path`, and `github.update_pr_title_with_new_jira`.
Set `github.mark_jira_live_when_linked_pr_merged: true` to let Jira maintenance transition linked tickets when the linked PR is merged.
Set `github.mark_jira_withdrawn_when_linked_pr_closed_unmerged: true` to let Jira maintenance transition linked tickets when the linked PR is closed without merging.
Set `github.list_prs_where_author: true` to prefilter the initial PR list by author before the normal per-PR label checks run. Configure the author with `github.pr_author`.
Set top-level `pr_process_delay_seconds` in `.github/renovate-jira.yml` to override the global `PR_PROCESS_DELAY_SECONDS` env var for a specific repo.

## Quick validation of JIRA PAT

```bash
curl -H "Authorization: Bearer $JIRA_PAT" \
  -H "Accept: application/json" \
  https://tools.hmcts.net/jira/rest/api/2/myself
```

## run locally
```bash
LOCAL_CONFIG_PATH=.github/renovate-jira.yml VERBOSE=1 python main.py
```

Single-ticket test run example:
```bash
MODE=run GITHUB_REPO=hmcts/your-repo TEST_PR_NUMBER=1234 MAX_NEW_JIRA_TICKETS=1 VERBOSE=1 python main.py
```

Existing-ticket component-only test example:
```bash
MODE=run GITHUB_REPO=hmcts/ccd-case-document-am-api TEST_PR_NUMBER=1234 MAX_NEW_JIRA_TICKETS=0 VERBOSE=1 python main.py
```

## Per-Repo Configuration

Example `.github/renovate-jira.yml`:

```yaml
# Optional per-repo configuration for the Renovate->Jira agent
enabled: true
pr_process_delay_seconds: 0

create_jira_for:
  security: true
  major: true
  critical-dep: false

critical_dependencies:
  - spring-boot
  - log4j
  - openssl

github:
  comment: false
  comment_on_existing_jira_if_missing: false
  add_labels: false
  require_labels: ["Renovate Dependencies", "Renovate-dependencies"]
  mark_jira_live_when_linked_pr_merged: true
  mark_jira_withdrawn_when_linked_pr_closed_unmerged: true
  list_prs_where_author: true
  pr_author: "renovate[bot]"
  update_pr_title_with_new_jira: false
  update_pr_title_with_existing_jira: false

jira:
  project: "CCD"
  labels: ["CCD-BAU", "RENOVATE-PR", "GENERATED-BY-Agent"]
  create_pr_links: true
  fix_ticket_labels: true
  fix_ticket_labels_even_in_dry_mode: true
  fix_components: true
  fix_components_even_in_dry_mode: true
  fix_ticket_pr_links: false
  sync_mend_confidence: true
  sync_mend_confidence_even_in_dry_mode: true
  mend_confidence_label_prefix: "mend-confidence"
  withdraw_duplicate_tickets: true
  withdraw_duplicate_tickets_even_in_dry_mode: true
  transition_merged_existing_via: "Released to production"
  transition_merged_existing_path: ["Blocked", "Resume Release", "Released to production"]
  transition_closed_unmerged_existing_via: "Withdrawn"
  target_status: "Resume Development"
  target_status_path: ["Blocked", "Resume Development"]
  skip_statuses: ["Resume Development", "Resume QA", "Resume Release"]
  release_approach_field: "customfield_12345"
  release_approach: "Tier 1: CI/CD"
  priority:
    security: "2-High"
    major: "3-Medium"
    critical-dep: "2-High"
```

## What It Does

Renovate -> Jira automation has all the core agent properties:

- It has a goal: For Renovate PRs that meet policy, ensure Jira tracking exists.
- It observes state: Reads GitHub PRs, labels, titles, bodies, per-repo policy, and existing PR comments (dedupe).
- It makes decisions: Classifies PRs (security / major / critical), applies repo-specific policy, and decides whether to act.
- It takes actions: Creates Jira issues, comments on PRs, adds labels, and skips safely when rules say "no".
- It runs autonomously: Triggered by GitHub Actions on schedule or manually; no human clicking buttons.
- It's auditable: Logs every action, PR comments link Jira tickets, config is versioned in Git.

## Why a PR Is Skipped

A PR is skipped with `did not match any rule` when it does not meet any of the decision rules in `decision.py`:

- Security: CVE in title/body or a `security` label, and `create_jira_for.security` is true.
- Major: keywords like `major`, `breaking`, or `migration`, or a major version bump in title/body, and `create_jira_for.major` is true.
- Critical dependency: PR mentions a dependency in `critical_dependencies`, and `create_jira_for.critical-dep` is true.
