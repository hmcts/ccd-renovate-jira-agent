# Project Summary: Renovate -> Jira Agent

This agent automatically turns important Renovate dependency PRs into Jira work items so dependency risk is tracked and visible without manual triage.

## Why we created it
- Manual dependency triage was repetitive, inconsistent, and easy to miss.
- Security/major upgrades needed reliable Jira traceability for governance and release planning.
- Teams needed a standard, policy-driven way to decide which Renovate PRs require formal tracking.

## What it does
- Scans open Renovate PRs across one repo, a repo list, or an org.
- Applies rules to detect PRs that should be tracked:
  - Security/CVE updates
  - Major/breaking version upgrades
  - Critical dependency updates (configurable)
- Uses per-repo policy (`.github/renovate-jira.yml`) to control behavior.
- Creates Jira tickets with the right project, priority, labels, fixVersion, epic, and optional release-approach field.
- Prevents duplicates by checking PR comments and Jira search/link matches.
- Optionally comments back on PRs, adds labels, adds Jira remote links, and transitions Jira status.
- Supports safe operation modes:
  - `dry-run` for preview/testing
  - `run` for live ticket creation


## Benefits
- Improves coverage: fewer high-risk dependency PRs slip through untracked.
- Saves engineering time by removing repetitive ticket creation and linking.
- Standardizes process across repos while still allowing per-repo customization.
- Improves auditability: clear GitHub-to-Jira trace for compliance/reporting.
- Safer rollout: dry-run mode, skip-status rules, dedupe, and ticket caps reduce risk.

## Why Python was used
- Fast to build and iterate: the automation was delivered quickly with minimal boilerplate.
- Strong API ecosystem: mature libraries for GitHub and Jira integrations reduced custom plumbing.
- Readable and maintainable: clear scripting style makes policy/rule updates easy for engineers.
- Great fit for CI/CD automation: simple runtime setup in GitHub Actions and reliable execution for scheduled/manual runs.
- Easy configurability: environment-variable and YAML-driven behavior are straightforward to implement in Python.

## Why GitHub Workflow was used
- Native to where Renovate PRs live: no external event wiring was needed.
- Simpler operations: no separate server, container platform, or scheduler to provision and maintain.
- Secure secret management: GitHub repository/org secrets integrate directly with the workflow.
- Built-in controls: easy scheduled runs, manual `workflow_dispatch`, logging, and run history in one place.
- Lower cost and faster adoption: teams can onboard using existing GitHub permissions and governance.
- Better developer visibility: engineers can see automation behavior alongside the PRs it processes.

## One-line value proposition
Automated, policy-based dependency governance: if a Renovate PR matters, a Jira trail exists automatically.
