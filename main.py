#!/usr/bin/env python3
"""
Renovate -> Jira automation agent (v4)
Adds per-repo optional config at .github/renovate-jira.yml
"""

import os
import sys
import time
import logging
import re
import yaml
from typing import Iterable, Dict, Any, List, Optional
import requests
from requests.auth import HTTPBasicAuth
from github import Github
from decision import needs_jira

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("renovate-jira-agent")

# Env/config
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
REPO_LIST = os.getenv("REPO_LIST")
REPO_LIST_FILE = os.getenv("REPO_LIST_FILE")
ORG_NAME = os.getenv("ORG_NAME")
REPO_TOPIC_FILTER = os.getenv("REPO_TOPIC_FILTER")
REPO_NAME_REGEX = os.getenv("REPO_NAME_REGEX")

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL")
JIRA_USER_EMAIL = os.getenv("JIRA_USER_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PAT = os.getenv("JIRA_PAT")
JIRA_API_VERSION = os.getenv("JIRA_API_VERSION", "2")
DEFAULT_JIRA_PROJECT = os.getenv("JIRA_PROJECT_KEY", "DEV")
JIRA_FIX_VERSION = os.getenv("JIRA_FIX_VERSION", "CCD CI/CD Release")
JIRA_EPIC_LINK_FIELD = os.getenv("JIRA_EPIC_LINK_FIELD", "customfield_10008")
JIRA_EPIC_KEY = os.getenv("JIRA_EPIC_KEY", "CCD-7071")

MODE = os.getenv("MODE", "dry-run").lower()
VERBOSE = os.getenv("VERBOSE", "").lower() in {"1", "true", "yes", "on"}
LOCAL_CONFIG_PATH = os.getenv("LOCAL_CONFIG_PATH")

PAGE_SIZE = int(os.getenv("PAGE_SIZE", "50"))

if not GITHUB_TOKEN:
    sys.exit("GITHUB_TOKEN required")
if not JIRA_BASE_URL:
    sys.exit("JIRA_BASE_URL required")
if not (JIRA_PAT or (JIRA_USER_EMAIL and JIRA_API_TOKEN)):
    sys.exit("Jira configuration missing: set JIRA_PAT or JIRA_USER_EMAIL/JIRA_API_TOKEN")

gh = Github(GITHUB_TOKEN, per_page=PAGE_SIZE)

def load_repo_config(repo) -> Dict[str, Any]:
    defaults = {
        "enabled": True,
        "create_jira_for": {"security": True, "major": True, "critical-dep": False},
        "critical_dependencies": [],
        "labels": {"require": ["renovate"], "add": ["CCD-BAU", "RENOVATE-PR", "GENERATED-BY-Agent"]},
        "jira": {"project": DEFAULT_JIRA_PROJECT, "priority": {"security": "High", "major": "Medium", "critical-dep": "High"}},
        "github": {"comment": True, "add_labels": True},
    }
    def merge_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
        merged = defaults.copy()
        merged.update(cfg)
        merged["create_jira_for"] = {**defaults["create_jira_for"], **cfg.get("create_jira_for", {})}
        merged["labels"] = {**defaults["labels"], **cfg.get("labels", {})}
        merged["jira"] = {**defaults["jira"], **cfg.get("jira", {})}
        merged["github"] = {**defaults["github"], **cfg.get("github", {})}
        merged["critical_dependencies"] = cfg.get("critical_dependencies", defaults["critical_dependencies"])
        return merged
    if LOCAL_CONFIG_PATH:
        if os.path.exists(LOCAL_CONFIG_PATH):
            try:
                with open(LOCAL_CONFIG_PATH, "r") as f:
                    cfg = yaml.safe_load(f) or {}
                if VERBOSE:
                    print(f"[CONFIG] Loaded local config from {LOCAL_CONFIG_PATH}")
                return merge_config(cfg)
            except Exception as e:
                if VERBOSE:
                    print(f"[CONFIG] Failed to load local config {LOCAL_CONFIG_PATH}: {e}")
        else:
            if VERBOSE:
                print(f"[CONFIG] Local config path not found: {LOCAL_CONFIG_PATH}")
    try:
        contents = repo.get_contents(".github/renovate-jira.yml")
        cfg = yaml.safe_load(contents.decoded_content) or {}
        merged = merge_config(cfg)
        if VERBOSE:
            print(f"[CONFIG] Loaded .github/renovate-jira.yml from {repo.full_name}")
        return merged
    except Exception as e:
        if VERBOSE:
            print(f"[CONFIG] Using defaults for {repo.full_name}: {e}")
        return defaults

def get_target_repos(gh_client) -> Iterable:
    if GITHUB_REPO:
        return [gh_client.get_repo(GITHUB_REPO)]
    if REPO_LIST:
        return [gh_client.get_repo(r.strip()) for r in REPO_LIST.split(",") if r.strip()]
    if REPO_LIST_FILE and os.path.exists(REPO_LIST_FILE):
        with open(REPO_LIST_FILE) as f:
            return [gh_client.get_repo(line.strip()) for line in f if line.strip() and not line.startswith("#")]
    if ORG_NAME:
        org = gh_client.get_organization(ORG_NAME)
        pattern = re.compile(REPO_NAME_REGEX) if REPO_NAME_REGEX else None
        repos = []
        for repo in org.get_repos():
            if REPO_TOPIC_FILTER:
                try:
                    if REPO_TOPIC_FILTER not in repo.get_topics():
                        continue
                except Exception:
                    continue
            if pattern and not pattern.search(repo.name):
                continue
            repos.append(repo)
        return repos
    raise RuntimeError("No target repos specified")

def jira_auth() -> Dict[str, str]:
    if JIRA_PAT:
        return {"Authorization": f"Bearer {JIRA_PAT}"}
    return {}

def jira_create_issue(summary: str, description: str, labels: List[str], project: str, priority: str) -> Dict[str, Any]:
    payload = {
        "fields": {
            "project": {"key": project},
            "summary": summary,
            "description": description,
            "issuetype": {"name": "Task"},
            "labels": labels,
            "priority": {"name": priority},
            "fixVersions": [{"name": JIRA_FIX_VERSION}],
            JIRA_EPIC_LINK_FIELD: JIRA_EPIC_KEY,
        }
    }
    if MODE == "dry-run":
        print("[DRY-RUN] Would create Jira issue in project {}: {}".format(project, summary))
        return {"key": "DRY-RUN-1"}
    url = f"{JIRA_BASE_URL.rstrip('/')}/rest/api/{JIRA_API_VERSION}/issue"
    headers = {"Accept": "application/json", **jira_auth()}
    auth = None if JIRA_PAT else HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN)
    resp = requests.post(url, json=payload, headers=headers, auth=auth)
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except ValueError:
            err = {"message": (resp.text or "").strip().replace("\n", " ")[:500]}
        raise RuntimeError(f"Jira create failed (status {resp.status_code}): {err}") from None
    try:
        return resp.json()
    except ValueError:
        snippet = (resp.text or "").strip().replace("\n", " ")[:500]
        raise RuntimeError(f"Jira response not JSON (status {resp.status_code}): {snippet}") from None

_JIRA_PREFLIGHT_OK = set()

def jira_preflight(project: str) -> None:
    if MODE == "dry-run":
        return
    if project in _JIRA_PREFLIGHT_OK:
        return
    headers = {"Accept": "application/json", **jira_auth()}
    auth = None if JIRA_PAT else HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN)
    base = JIRA_BASE_URL.rstrip("/")
    me = requests.get(f"{base}/rest/api/{JIRA_API_VERSION}/myself", headers=headers, auth=auth)
    me.raise_for_status()
    proj = requests.get(f"{base}/rest/api/{JIRA_API_VERSION}/project/{project}", headers=headers, auth=auth)
    proj.raise_for_status()
    _JIRA_PREFLIGHT_OK.add(project)

def pr_has_ticket_in_comments(pr) -> Optional[str]:
    import re
    try:
        comments = pr.get_issue_comments()
        pattern = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
        for c in comments:
            m = pattern.search(c.body or "")
            if m:
                return m.group(1)
    except Exception:
        pass
    return None

def process_pr(repo, pr, cfg):
    if not cfg.get("enabled", True):
        if VERBOSE:
            print(f"[SKIP] Repo disabled for PR #{getattr(pr,'number','?')} in {repo.full_name}")
        return
    if pr.state != "open":
        if VERBOSE:
            print(f"[SKIP] PR #{pr.number} in {repo.full_name} is not open")
        return
    require_labels = set(l.lower() for l in cfg.get("labels", {}).get("require", []))
    pr_labels = set(l.name.lower() for l in pr.get_labels())
    if require_labels and not (pr_labels & require_labels):
        if VERBOSE:
            print(f"[SKIP] PR #{pr.number} in {repo.full_name} missing required labels: {sorted(require_labels)}")
        return
    category, reason = needs_jira(pr.title or "", pr.body or "", [l.name for l in pr.get_labels()],
                                 critical_deps=cfg.get("critical_dependencies", []),
                                 create_jira_for=cfg.get("create_jira_for", {}))
    if not category:
        if VERBOSE:
            print(f"[SKIP] PR #{pr.number} in {repo.full_name} did not match any rule")
        return
    existing = pr_has_ticket_in_comments(pr)
    if existing:
        if VERBOSE:
            print(f"[SKIP] PR #{pr.number} in {repo.full_name} already has Jira ticket {existing}")
        return
    project = cfg.get("jira", {}).get("project", DEFAULT_JIRA_PROJECT)
    jira_preflight(project)
    priority_map = cfg.get("jira", {}).get("priority", {})
    priority = priority_map.get(category, "Medium")
    summary = f"Dependency update: {pr.title}"
    description = f"Renovate PR: {pr.html_url}\n\nReason detected: {reason}\n\nPR excerpt:\n{(pr.body or '')[:1000]}"
    labels_to_add = cfg.get("labels", {}).get("add", ["needs-jira"])
    jira_resp = jira_create_issue(summary, description, labels_to_add, project, priority)
    issue_key = jira_resp.get("key", "UNKNOWN")
    comment = f"Created Jira issue {issue_key} to track this Renovate PR. Reason: {reason}"
    if MODE != "dry-run":
        if cfg.get("github", {}).get("comment", True):
            try:
                pr.create_issue_comment(comment)
            except Exception as e:
                print(f"Warning: failed to comment on PR #{pr.number} in {repo.full_name}: {e}", file=sys.stderr)
        if cfg.get("github", {}).get("add_labels", True):
            try:
                pr.add_to_labels(*labels_to_add)
            except Exception as e:
                print(f"Warning: failed to add labels on PR #{pr.number} in {repo.full_name}: {e}", file=sys.stderr)
    print(f"Created Jira {issue_key} for PR #{pr.number} in {repo.full_name}")

def main():
    repos = get_target_repos(gh)
    print(f"Scanning {len(repos)} repos")
    for repo in repos:
        cfg = load_repo_config(repo)
        print(f"Repo {repo.full_name} config enabled={cfg.get('enabled', True)}")
        for pr in repo.get_pulls(state="open", sort="updated"):
            try:
                process_pr(repo, pr, cfg)
                time.sleep(0.5)
            except Exception as e:
                print(f"Error processing PR #{getattr(pr,'number','?')} in {repo.full_name}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
