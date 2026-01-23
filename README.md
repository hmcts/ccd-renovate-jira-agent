# Renovate -> Jira Agent v4

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
export GITHUB_TOKEN=<YOUR-GITHUB-FINE-GRAINED-TOKEN>
export JIRA_BASE_URL=https://tools.hmcts.net/jira
export JIRA_USER_EMAIL=<YOUR_JIRA_LOGIN_EMAIL_ADDRESS>
export JIRA_PAT=<YOUR_JIRA_PAT>
export JIRA_API_VERSION=2
```

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

## Per-Repo Configuration

Example `.github/renovate-jira.yml`:

```yaml
enabled: true

create_jira_for:
  security: true
  major: true
  critical-dep: false

critical_dependencies:
  - spring-boot
  - log4j
  - openssl

jira:
  project: "CCD"
  labels: ["CCD-BAU", "RENOVATE-PR", "GENERATED-BY-Agent"]
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
