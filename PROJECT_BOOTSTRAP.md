# Renovate -> Jira Agent: Build Blueprint (From Scratch)

## Purpose
Use this document as a single source of truth to build the Renovate -> Jira automation agent from zero. It defines the problem, required outcomes, architecture, implementation tasks, configuration contract, and acceptance criteria.

## Problem Statement
Teams receive many Renovate pull requests. Manual triage and Jira ticket creation is repetitive and inconsistent. High-risk dependency updates (security fixes, major upgrades, critical libraries) need guaranteed Jira traceability for planning, governance, and release management.

## Target Outcome
When an eligible Renovate PR is open, the system should automatically ensure there is exactly one valid Jira issue tracking it, with correct metadata and optional PR/Jira cross-linking.

## In Scope
- Scan open PRs from one or many repositories.
- Apply policy-based decision rules to determine if a PR needs Jira.
- Create Jira issue when no valid existing ticket is found.
- Prevent duplicates using PR comment parsing plus Jira search/link checks.
- Optionally update existing Jira ticket fields (labels, fixVersion, epic, release approach).
- Optionally transition Jira status.
- Operate in `dry-run` and `run` modes.
- Run via GitHub Actions (scheduled/manual).

## Out of Scope
- Closing PRs, merging PRs, or modifying Renovate configuration itself.
- Advanced NLP classification.
- Jira workflow creation/administration.

## High-Level Design
- `decision.py`: pure decision logic (no API calls).
- `main.py`: orchestration and side effects (GitHub + Jira APIs).
- `.github/workflows/renovate-jira-agent.yml`: automation runner.
- `.github/renovate-jira.yml` (per repo): policy overrides.
- `repo-list.txt`: optional target list.

## Functional Requirements
1. Target repo selection supports:
- `GITHUB_REPO`
- `REPO_LIST`
- `REPO_LIST_FILE`
- `ORG_NAME` (+ optional `REPO_TOPIC_FILTER`, `REPO_NAME_REGEX`)

2. PR eligibility checks:
- PR must be open.
- PR must contain at least one required label from repo config (`github.require_labels`) if configured.

3. Decision categories:
- `security`: CVE pattern in title/body or `security` label.
- `major`: semantic-major/breaking indicators.
- `critical-dep`: mentions configured critical dependency.

4. Deduplication order:
- Existing Jira key in PR comments.
- Jira search by summary/tokenized query.
- Jira PR remotelink verification.

5. Jira creation behavior:
- Issue type `Task`.
- Set summary, description, labels, priority, fixVersion, epic link.
- Optional release approach custom field.

6. Existing Jira ticket behavior:
- Skip if ticket status in configured skip statuses.
- Optionally repair labels/fixVersion/epic/release approach.
- Optionally add PR remotelink.

7. GitHub feedback behavior:
- Optional PR comment after Jira creation.
- Optional PR label updates.

8. Safety controls:
- `MODE=dry-run` must avoid mutation unless explicitly allowed flags are set.
- Optional `TEST_PR_NUMBER` and `MAX_NEW_JIRA_TICKETS` for controlled rollout.

## Non-Functional Requirements
- Idempotent across repeated runs.
- Clear logs for skip reasons and actions taken.
- Config-driven behavior with safe defaults.
- Minimal dependencies (`PyGithub`, `requests`, `PyYAML`).

## Configuration Contract
### Global environment variables
Required:
- `GITHUB_TOKEN`
- `JIRA_BASE_URL`
- Jira auth: either `JIRA_PAT` or (`JIRA_USER_EMAIL` + `JIRA_API_TOKEN`)

Common optional:
- `MODE` (`dry-run` or `run`)
- `VERBOSE`
- `TEST_PR_NUMBER`
- `MAX_NEW_JIRA_TICKETS`
- `FIX_TICKET_LABELS`
- `FIX_TICKET_LABELS_EVEN_IN_DRY_MODE`
- `FIX_TICKET_PR_LINKS`
- `CREATE_PR_LINKS`
- `JIRA_TARGET_STATUS`
- `JIRA_TARGET_STATUS_PATH`
- `JIRA_SKIP_STATUSES`
- `JIRA_PROJECT_KEY`, `JIRA_FIX_VERSION`, `JIRA_EPIC_LINK_FIELD`, `JIRA_EPIC_KEY`
- `JIRA_RELEASE_APPROACH_FIELD`, `JIRA_RELEASE_APPROACH_VALUE`

### Per-repo file: `.github/renovate-jira.yml`
```yaml
enabled: true
create_jira_for:
  security: true
  major: true
  critical-dep: false

critical_dependencies:
  - spring-boot
  - log4j

github:
  require_labels: ["Renovate Dependencies"]
  comment: true
  add_labels: true

jira:
  project: "CCD"
  labels: ["CCD-BAU", "RENOVATE-PR", "GENERATED-BY-Agent"]
  priority:
    security: "High"
    major: "Medium"
    critical-dep: "High"
  release_approach_field: "customfield_12345"
  release_approach: "Tier 1: CI/CD"
```

## Build Plan (Implementation Sequence)
1. Create `decision.py` with deterministic helpers:
- `mentions_cve`
- `is_major_bump`
- `touches_critical_dependency`
- `needs_jira`

2. Create `main.py` to:
- Read env config.
- Resolve target repos.
- Load/merge per-repo YAML config.
- Iterate open PRs.
- Run decision logic.
- Deduplicate.
- Create/update Jira.
- Add PR comment/labels.

3. Add workflow `.github/workflows/renovate-jira-agent.yml`:
- `workflow_dispatch` + scheduled cron.
- Python setup + dependency install.
- Secure secret/env wiring.

4. Add docs:
- `README.md` with setup, env vars, local run.
- Example `.github/renovate-jira.yml`.

5. Add smoke-test procedure:
- `MODE=dry-run`
- single `TEST_PR_NUMBER`
- `MAX_NEW_JIRA_TICKETS=1`
- verify logs and dedupe behavior.

## Acceptance Criteria
- PRs matching configured categories produce one Jira issue in `run` mode.
- No duplicate Jira issue on subsequent runs for same PR.
- Non-matching PRs log explicit skip reason.
- Existing Jira in skip status is not modified.
- Dry-run performs no Jira/GitHub mutations unless explicitly enabled.
- Workflow can run manually and complete successfully with valid credentials.

## Suggested Prompt To Generate This Project With AI
```text
Build a Python project called renovate-jira-agent.

Requirements:
1) Scan open PRs from GitHub repos (single repo, repo list, repo list file, or org scan with filters).
2) Use a pure decision module to classify PRs into categories: security, major, critical-dep.
3) Load optional per-repo config from .github/renovate-jira.yml and merge with defaults.
4) If PR requires Jira, dedupe using:
   - Jira key in PR comments
   - Jira summary/token search
   - Jira remotelink PR match
5) Create Jira Task with summary, description, labels, priority, fixVersion, epic, optional release-approach field.
6) Optional behaviors via env flags:
   - fix existing ticket labels/fields
   - add PR links
   - transition status/path
   - skip specific ticket statuses
7) Support MODE=dry-run and MODE=run.
8) Add GitHub Actions workflow for manual + scheduled runs.
9) Add README with env vars, setup, and examples.

Use files:
- main.py
- decision.py
- requirements.txt
- .github/workflows/renovate-jira-agent.yml
- README.md
- repo-list.txt

Dependencies: PyGithub, requests, PyYAML.
```

## Known Risks And Mitigations
- Jira dedupe false positives/negatives: keep URL-based remotelink matching and verbose diagnostics toggle.
- Workflow transition mismatches: use optional transition path (`JIRA_TARGET_STATUS_PATH`).
- Over-processing: use required labels + category toggles + max ticket cap during rollout.

## Rollout Checklist
- Validate Jira PAT/auth with `/myself` API.
- Run local dry-run against known PR.
- Run workflow dispatch in dry-run.
- Enable `MODE=run` with `MAX_NEW_JIRA_TICKETS=1`.
- Remove cap after verification.
