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
from github import Github, Auth
from decision import needs_jira

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("renovate-jira-agent")

LOG_PREFIX = ""

def _log(message: str) -> None:
    print(f"{LOG_PREFIX}{message}")

def _elog(message: str) -> None:
    print(f"{LOG_PREFIX}{message}", file=sys.stderr)

def _vlog(message: str) -> None:
    if VERBOSE:
        _log(message)

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
DEFAULT_JIRA_PROJECT = os.getenv("JIRA_PROJECT_KEY", "CCD")
JIRA_FIX_VERSION = os.getenv("JIRA_FIX_VERSION", "CCD CI/CD Release")
JIRA_EPIC_LINK_FIELD = os.getenv("JIRA_EPIC_LINK_FIELD", "customfield_10008")
JIRA_EPIC_KEY = os.getenv("JIRA_EPIC_KEY", "CCD-7071")
FIX_TICKET_LABELS = os.getenv("FIX_TICKET_LABELS", "").lower() in {"1", "true", "yes", "on"}
FIX_TICKET_LABELS_EVEN_IN_DRY_MODE = os.getenv("FIX_TICKET_LABELS_EVEN_IN_DRY_MODE", "").lower() in {"1", "true", "yes", "on"}
FIX_TICKET_PR_LINKS = os.getenv("FIX_TICKET_PR_LINKS", "").lower() in {"1", "true", "yes", "on"}
VERBOSE_JIRA_DEDUPE = os.getenv("VERBOSE_JIRA_DEDUPE", "").lower() in {"1", "true", "yes", "on"}

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

gh = Github(auth=Auth.Token(GITHUB_TOKEN), per_page=PAGE_SIZE)

def load_repo_config(repo) -> Dict[str, Any]:
    # Defaults apply whenever a repo config omits a key; repo settings override these values.
    defaults = {
        "enabled": True,
        "create_jira_for": {"security": True, "major": True, "critical-dep": False},
        "critical_dependencies": [],
        "labels": {},
        "jira": {
            "project": DEFAULT_JIRA_PROJECT,
            "priority": {"security": "High", "major": "Medium", "critical-dep": "High"},
            "labels": ["CCD-BAU", "RENOVATE-PR", "GENERATED-BY-Agent"],
        },
        "github": {"comment": True, "add_labels": True, "require_labels": ["Renovate Dependencies"]},
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
        _log("[DRY-RUN] Would create Jira issue in project {}: {}".format(project, summary))
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

def _escape_jql(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')

def _pr_slug(pr_url: str) -> str:
    m = re.search(r"github\\.com/([^/]+/[^/]+/pull/\\d+)", pr_url or "")
    return m.group(1) if m else ""

def _build_summary_token_jql(project: str, text: str) -> Optional[str]:
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", text or "")
    stop = {"update", "action", "actions", "bump", "dependency", "dependencies", "to", "from"}
    keywords = [t for t in tokens if t.lower() not in stop]
    strong = [t for t in keywords if "-" in t or any(c.isdigit() for c in t)]
    selected = []
    for t in strong:
        if t not in selected:
            selected.append(t)
    for t in keywords:
        if len(selected) >= 2:
            break
        if t not in selected:
            selected.append(t)
    if not selected:
        return None
    clauses = " AND ".join([f'summary ~ "{_escape_jql(t)}"' for t in selected[:2]])
    return (
        f'project = "{_escape_jql(project)}" '
        f'AND {clauses} '
        f'AND status != "Withdrawn"'
    )

def jira_find_existing_issue(summary: str, project: str, pr_url: str) -> Optional[str]:
    title_candidate = summary.replace("Dependency update: ", "").strip()
    title_candidate = re.sub(r"^[A-Z]+-\d+\s*::\s*", "", title_candidate)
    jql_candidates = [
        summary,
        title_candidate,
    ]
    token_jql = _build_summary_token_jql(project, title_candidate)
    if VERBOSE and VERBOSE_JIRA_DEDUPE:
        _log(f"[INFO] Jira search candidates: {jql_candidates}")
    url = f"{JIRA_BASE_URL.rstrip('/')}/rest/api/{JIRA_API_VERSION}/search"
    headers = {"Accept": "application/json", **jira_auth()}
    auth = None if JIRA_PAT else HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN)
    for cand in [c for c in jql_candidates if c]:
        jql = (
            f'project = "{_escape_jql(project)}" '
            f'AND summary ~ "{_escape_jql(cand)}" '
            f'AND status != "Withdrawn"'
        )
        params = {"jql": jql, "maxResults": 5, "fields": "key,summary,description"}
        try:
            resp = requests.get(url, headers=headers, params=params, auth=auth)
            resp.raise_for_status()
            data = resp.json()
            issues = data.get("issues", [])
            if VERBOSE and VERBOSE_JIRA_DEDUPE:
                keys = [i.get("key") for i in issues]
                _log(f"[INFO] Jira search JQL={jql} -> {keys}")
            for issue in issues:
                issue_key = issue.get("key")
                description = (issue.get("fields", {}) or {}).get("description") or ""
                if pr_url and pr_url in description:
                    if VERBOSE and VERBOSE_JIRA_DEDUPE:
                        _log(f"[INFO] Jira {issue_key} matched PR URL in description")
                    return issue_key
                if pr_url and issue_key and jira_issue_has_pr_link(issue_key, pr_url):
                    if VERBOSE and VERBOSE_JIRA_DEDUPE:
                        _log(f"[INFO] Jira {issue_key} matched PR URL in links")
                    return issue_key
                if pr_url and issue_key and FIX_TICKET_PR_LINKS:
                    if jira_add_pr_remotelink(issue_key, pr_url):
                        if VERBOSE and VERBOSE_JIRA_DEDUPE:
                            _log(f"[INFO] Jira {issue_key} linked PR URL via remotelink")
                        return issue_key
        except Exception as e:
            if VERBOSE and VERBOSE_JIRA_DEDUPE:
                _log(f"[WARN] Jira search failed for summary match: {e}")
    if token_jql:
        try:
            resp = requests.get(url, headers=headers, params={"jql": token_jql, "maxResults": 5, "fields": "key,summary,description"}, auth=auth)
            resp.raise_for_status()
            data = resp.json()
            issues = data.get("issues", [])
            if VERBOSE and VERBOSE_JIRA_DEDUPE:
                keys = [i.get("key") for i in issues]
                _log(f"[INFO] Jira search JQL={token_jql} -> {keys}")
            for issue in issues:
                issue_key = issue.get("key")
                description = (issue.get("fields", {}) or {}).get("description") or ""
                if pr_url and pr_url in description:
                    if VERBOSE and VERBOSE_JIRA_DEDUPE:
                        _log(f"[INFO] Jira {issue_key} matched PR URL in description")
                    return issue_key
                if pr_url and issue_key and jira_issue_has_pr_link(issue_key, pr_url):
                    if VERBOSE and VERBOSE_JIRA_DEDUPE:
                        _log(f"[INFO] Jira {issue_key} matched PR URL in links")
                    return issue_key
                if pr_url and issue_key and FIX_TICKET_PR_LINKS:
                    if jira_add_pr_remotelink(issue_key, pr_url):
                        if VERBOSE and VERBOSE_JIRA_DEDUPE:
                            _log(f"[INFO] Jira {issue_key} linked PR URL via remotelink")
                        return issue_key
        except Exception as e:
            if VERBOSE and VERBOSE_JIRA_DEDUPE:
                _log(f"[WARN] Jira token search failed: {e}")
    return None

def jira_issue_has_pr_link(issue_key: str, pr_url: str) -> bool:
    pr_slug = _pr_slug(pr_url)
    url = f"{JIRA_BASE_URL.rstrip('/')}/rest/api/{JIRA_API_VERSION}/issue/{issue_key}/remotelink"
    headers = {"Accept": "application/json", **jira_auth()}
    auth = None if JIRA_PAT else HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN)
    try:
        resp = requests.get(url, headers=headers, auth=auth)
        resp.raise_for_status()
        links = resp.json() or []
        for link in links:
            obj = link.get("object", {}) or {}
            link_url = obj.get("url") or ""
            link_title = obj.get("title") or ""
            if pr_url and pr_url in link_url:
                return True
            if pr_slug and pr_slug in link_url:
                return True
            if pr_slug and pr_slug in link_title:
                return True
    except Exception as e:
        _vlog(f"[WARN] Jira remotelink check failed for {issue_key}: {e}")

    issue = jira_get_issue(issue_key)
    if not issue:
        return False
    fields = issue.get("fields", {}) or {}
    for link in fields.get("issuelinks") or []:
        for side in ("inwardIssue", "outwardIssue"):
            issue_obj = link.get(side) or {}
            issue_fields = issue_obj.get("fields", {}) or {}
            summary = issue_fields.get("summary") or ""
            if pr_url and pr_url in summary:
                return True
            if pr_slug and pr_slug in summary:
                return True
    return False

def jira_add_pr_remotelink(issue_key: str, pr_url: str) -> bool:
    if not pr_url:
        return False
    if MODE == "dry-run" and not FIX_TICKET_LABELS_EVEN_IN_DRY_MODE:
        _log(f"[DRY-RUN] Would add PR link to Jira {issue_key}: {pr_url}")
        return True
    url = f"{JIRA_BASE_URL.rstrip('/')}/rest/api/{JIRA_API_VERSION}/issue/{issue_key}/remotelink"
    headers = {"Accept": "application/json", **jira_auth()}
    auth = None if JIRA_PAT else HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN)
    payload = {"object": {"url": pr_url, "title": f"PR: {pr_url}"}}
    try:
        resp = requests.post(url, json=payload, headers=headers, auth=auth)
        resp.raise_for_status()
        _vlog(f"[INFO] Added PR link to Jira {issue_key}: {pr_url}")
        return True
    except Exception as e:
        _vlog(f"[WARN] Jira remotelink add failed for {issue_key}: {e}")
    return False

def jira_get_issue(issue_key: str) -> Optional[Dict[str, Any]]:
    url = f"{JIRA_BASE_URL.rstrip('/')}/rest/api/{JIRA_API_VERSION}/issue/{issue_key}"
    headers = {"Accept": "application/json", **jira_auth()}
    auth = None if JIRA_PAT else HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN)
    params = {"fields": f"labels,fixVersions,status,{JIRA_EPIC_LINK_FIELD},issuelinks"}
    try:
        resp = requests.get(url, headers=headers, params=params, auth=auth)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        _vlog(f"[WARN] Jira get issue failed for {issue_key}: {e}")
    return None

def jira_is_withdrawn(issue_key: str) -> bool:
    issue = jira_get_issue(issue_key)
    if not issue:
        return False
    status = (issue.get("fields", {}) or {}).get("status", {}) or {}
    return (status.get("name") or "").lower() == "withdrawn"

def jira_update_issue(issue_key: str, fields: Dict[str, Any]) -> None:
    if MODE == "dry-run" and not FIX_TICKET_LABELS_EVEN_IN_DRY_MODE:
        _log(f"[DRY-RUN] Would update Jira {issue_key} fields: {list(fields.keys())}")
        return
    url = f"{JIRA_BASE_URL.rstrip('/')}/rest/api/{JIRA_API_VERSION}/issue/{issue_key}"
    headers = {"Accept": "application/json", **jira_auth()}
    auth = None if JIRA_PAT else HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN)
    payload = {"fields": fields}
    resp = requests.put(url, json=payload, headers=headers, auth=auth)
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except ValueError:
            err = {"message": (resp.text or "").strip().replace("\n", " ")[:500]}
        raise RuntimeError(f"Jira update failed for {issue_key} (status {resp.status_code}): {err}") from None

def jira_ensure_ticket_fields(issue_key: str, desired_labels: List[str], fix_version: str) -> None:
    issue = jira_get_issue(issue_key)
    if not issue:
        return
    _vlog(f"[INFO] Found Jira {issue_key}; checking labels/epic/fixVersion")
    fields = issue.get("fields", {}) or {}
    updates: Dict[str, Any] = {}

    current_labels = set(fields.get("labels") or [])
    desired_labels_set = set(desired_labels or [])
    if desired_labels_set and not desired_labels_set.issubset(current_labels):
        updates["labels"] = sorted(current_labels | desired_labels_set)

    current_fix_versions = [v.get("name") for v in (fields.get("fixVersions") or []) if v.get("name")]
    if fix_version and fix_version not in current_fix_versions:
        updates["fixVersions"] = [{"name": name} for name in (current_fix_versions + [fix_version])]

    current_epic = fields.get(JIRA_EPIC_LINK_FIELD)
    if JIRA_EPIC_KEY and current_epic != JIRA_EPIC_KEY:
        updates[JIRA_EPIC_LINK_FIELD] = JIRA_EPIC_KEY

    if updates:
        _vlog(f"[INFO] Updating Jira {issue_key} fields: {sorted(updates.keys())}")
        jira_update_issue(issue_key, updates)
    else:
        _vlog(f"[INFO] Jira {issue_key} already has desired labels/epic/fixVersion")

def pr_has_ticket_in_comments(pr) -> Optional[str]:
    import re
    try:
        comments = pr.get_issue_comments()
        pattern = re.compile(r"\b((?:CCD|HMC)-\d+)\b", re.IGNORECASE)
        for c in comments:
            m = pattern.search(c.body or "")
            if m:
                return m.group(1).upper()
    except Exception:
        pass
    return None

def process_pr(repo, pr, cfg):
    global LOG_PREFIX
    print(f"Processing PR #{pr.number} ({pr.title or ''})")
    previous_prefix = LOG_PREFIX
    LOG_PREFIX = "\t"
    try:
        if not cfg.get("enabled", True):
            _vlog(f"[SKIP] Repo disabled for PR #{getattr(pr,'number','?')} in {repo.full_name}")
            return
        if pr.state != "open":
            _vlog(f"[SKIP] PR #{pr.number} in {repo.full_name} is not open")
            return
        require_labels = set(l.lower() for l in cfg.get("github", {}).get("require_labels", []))
        if not require_labels:
            # Backward compatibility for older configs.
            require_labels = set(l.lower() for l in cfg.get("labels", {}).get("require", []))
        pr_labels = set(l.name.lower() for l in pr.get_labels())
        if require_labels and not (pr_labels & require_labels):
            _vlog(f"[SKIP] PR #{pr.number} in {repo.full_name} missing required labels: {sorted(require_labels)}")
            return
        category, reason = needs_jira(pr.title or "", pr.body or "", [l.name for l in pr.get_labels()],
                                     critical_deps=cfg.get("critical_dependencies", []),
                                     create_jira_for=cfg.get("create_jira_for", {}))
        if not category:
            _vlog(f"[SKIP] PR #{pr.number} in {repo.full_name} did not match any rule")
            return
        existing = pr_has_ticket_in_comments(pr)
        if existing and jira_is_withdrawn(existing):
            _vlog(f"[INFO] Jira {existing} is Withdrawn; creating a new ticket")
            existing = None
        if existing:
            if FIX_TICKET_LABELS:
                labels_to_add = cfg.get("jira", {}).get("labels", [])
                jira_ensure_ticket_fields(existing, labels_to_add, JIRA_FIX_VERSION)
            _log(f"[SKIP] PR #{pr.number} in {repo.full_name} already has Jira ticket {existing}")
            return
        summary = f"Dependency update: {pr.title}"
        project = cfg.get("jira", {}).get("project", DEFAULT_JIRA_PROJECT)
        existing = jira_find_existing_issue(summary, project, pr.html_url)
        if existing:
            if FIX_TICKET_LABELS:
                labels_to_add = cfg.get("jira", {}).get("labels", [])
                jira_ensure_ticket_fields(existing, labels_to_add, JIRA_FIX_VERSION)
            _log(f"[SKIP] PR #{pr.number} in {repo.full_name} already has Jira ticket {existing} (summary+PR link)")
            return
        jira_preflight(project)
        priority_map = cfg.get("jira", {}).get("priority", {})
        priority = priority_map.get(category, "Medium")
        description = f"Renovate PR: {pr.html_url}\n\nReason detected: {reason}\n\nPR excerpt:\n{(pr.body or '')[:1000]}"
        labels_to_add = cfg.get("jira", {}).get("labels", [])
        jira_resp = jira_create_issue(summary, description, labels_to_add, project, priority)
        issue_key = jira_resp.get("key", "UNKNOWN")
        comment = f"Created Jira issue {issue_key} to track this Renovate PR. Reason: {reason}"
        if MODE != "dry-run":
            if cfg.get("github", {}).get("comment", True):
                try:
                    pr.create_issue_comment(comment)
                except Exception as e:
                    _elog(f"Warning: failed to comment on PR #{pr.number} in {repo.full_name}: {e}")
            if cfg.get("github", {}).get("add_labels", True):
                try:
                    pr.add_to_labels(*labels_to_add)
                except Exception as e:
                    _elog(f"Warning: failed to add labels on PR #{pr.number} in {repo.full_name}: {e}")
        _log(f"Created Jira {issue_key} for PR #{pr.number} in {repo.full_name}")
    finally:
        LOG_PREFIX = previous_prefix

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
