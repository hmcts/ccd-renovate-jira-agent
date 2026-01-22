# Renovate-Jira Agent v1:

## Project Overview

This is an **autonomous agent** that observes GitHub Renovate PRs, makes policy-based decisions, and creates Jira tracking tickets automatically. It operates on a goal-state-decision-action loop without human intervention.

**Core Flow**: GitHub PR event → Read PR metadata + `.github/renovate-jira.yml` config → Decision logic in `decision.py` → Create Jira issue + comment on PR

## Architecture

### Two-File Design

- **`decision.py`**: Pure decision logic (zero side effects). Exports `needs_jira(title, body, labels, critical_deps, create_jira_for) -> (category, reason)` that classifies PRs into `security`, `major`, or `critical-dep` categories
- **`main.py`**: Orchestration layer. Fetches PRs, loads per-repo config, calls decision logic, performs actions (Jira API, GitHub comments, labels)

**Why separated**: Decision logic is testable in isolation. `decision.py` has no I/O, making it easy to unit test with different PR scenarios.

### Per-Repo Configuration

Each repository can customize behavior via `.github/renovate-jira.yml`:

```yaml
enabled: true
create_jira_for:
  security: true        # CVEs, "security" label
  major: true           # Semver-major bumps, breaking changes
  critical-dep: false   # Default off unless explicitly listed
critical_dependencies:  # Repo-specific critical deps
  - "react"
  - "express"
labels:
  require: ["renovate"]  # Only process PRs with these labels
  add: ["needs-jira"]    # Labels to add when Jira created
jira:
  project: "DEV"
  priority:
    security: "High"
    major: "Medium"
    critical-dep: "High"
```

Config loading in `load_repo_config()` merges repo-specific YAML with sensible defaults (lines 50-65 in [main.py](main.py#L50-L65)).

## Decision Logic Patterns

### Security Detection (`decision.py:6-7`)

Uses CVE regex pattern `\bCVE-\d{4}-\d{4,}\b` and checks for "security" label. Returns immediately if matched and policy allows.

### Major Version Detection (`decision.py:9-15`)

Two methods:
1. Keyword scan: "major", "breaking", "migration" in title/body
2. Numeric bump: Extracts target version from title ("to X.Y.Z") and checks if major version > 1

### Critical Dependency Detection (`decision.py:17-23`)

Merges `DEFAULT_CRITICAL` set (openssl, spring-boot, log4j, etc.) with repo-specific `critical_dependencies` list. Simple substring match on lowercase text.

## Multi-Repo Scanning Modes

The agent supports 4 different targeting modes (lines 67-88 in [main.py](main.py#L67-L88)):

1. `GITHUB_REPO`: Single repo (e.g., "org/repo")
2. `REPO_LIST`: Comma-separated list
3. `REPO_LIST_FILE`: File with one repo per line (supports `#` comments)
4. `ORG_NAME`: Scan entire org with optional filters:
   - `REPO_TOPIC_FILTER`: Only repos with specific topic
   - `REPO_NAME_REGEX`: Regex pattern for repo names

## Idempotency & Deduplication

`pr_has_ticket_in_comments()` (lines 114-125 in [main.py](main.py#L114-L125)) scans PR comments for existing Jira ticket keys matching `\b([A-Z][A-Z0-9]+-\d+)\b`. If found, skips creating a duplicate ticket. This ensures agent can run repeatedly without spam.

## Environment Variables

**Required**:
- `GITHUB_TOKEN`: GitHub API access
- `JIRA_BASE_URL`, `JIRA_USER_EMAIL`, `JIRA_API_TOKEN`: Jira API credentials

**Optional**:
- `MODE=dry-run` (default) or `MODE=live`: Dry-run only logs intended actions
- `PAGE_SIZE=50`: GitHub API pagination
- `JIRA_PROJECT_KEY=DEV`: Default Jira project when not specified in repo config

## Testing Approach

When adding features:

1. **Update `decision.py` first**: Add new classification logic as pure function
2. **Test decision logic**: Call `needs_jira()` with sample PR data, verify (category, reason) tuple
3. **Update `main.py`**: Add orchestration for new category (Jira creation, labeling)
4. **Run in dry-run mode**: Set `MODE=dry-run` to verify behavior without creating real tickets

## Common Modifications

**Adding a new category**: 
1. Add detection function in `decision.py` (follow pattern of `mentions_cve()`, `is_major_bump()`)
2. Add to `needs_jira()` if-chain with corresponding `create_jira_for` config key
3. Update default config in `load_repo_config()` defaults dict
4. Document in README example YAML

**Changing Jira fields**:
Modify `jira_create_issue()` payload (lines 90-112 in [main.py](main.py#L90-L112)). Current schema: Task type, with summary/description/labels/priority.

**Rate limiting**:
`time.sleep(0.5)` between PRs (line 174 in [main.py](main.py#L174)). Adjust if hitting GitHub secondary rate limits.
