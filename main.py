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
JIRA_RELEASE_APPROACH_FIELD = os.getenv("JIRA_RELEASE_APPROACH_FIELD", "")
JIRA_RELEASE_APPROACH_VALUE = os.getenv("JIRA_RELEASE_APPROACH_VALUE", "Tier 1: CI/CD")
FIX_TICKET_LABELS = os.getenv("FIX_TICKET_LABELS", "").lower() in {"1", "true", "yes", "on"}
FIX_TICKET_LABELS_EVEN_IN_DRY_MODE = os.getenv("FIX_TICKET_LABELS_EVEN_IN_DRY_MODE", "").lower() in {"1", "true", "yes", "on"}
FIX_TICKET_PR_LINKS = os.getenv("FIX_TICKET_PR_LINKS", "").lower() in {"1", "true", "yes", "on"}
VERBOSE_JIRA_DEDUPE = os.getenv("VERBOSE_JIRA_DEDUPE", "").lower() in {"1", "true", "yes", "on"}
CREATE_PR_LINKS = os.getenv("CREATE_PR_LINKS", "").lower() in {"1", "true", "yes", "on"}
UPDATE_PR_TITLE_WITH_JIRA = os.getenv("UPDATE_PR_TITLE_WITH_JIRA", "true").lower() in {"1", "true", "yes", "on"}
UPDATE_PR_TITLE_WITH_EXISTING_JIRA = os.getenv("UPDATE_PR_TITLE_WITH_EXISTING_JIRA", "false").lower() in {"1", "true", "yes", "on"}
COMMENT_ON_EXISTING_JIRA_IF_MISSING = os.getenv("COMMENT_ON_EXISTING_JIRA_IF_MISSING", "false").lower() in {"1", "true", "yes", "on"}
JIRA_TARGET_STATUS = os.getenv("JIRA_TARGET_STATUS", "")
JIRA_TARGET_STATUS_PATH = [s.strip() for s in os.getenv("JIRA_TARGET_STATUS_PATH", "").split(",") if s.strip()]
JIRA_SKIP_STATUSES = {s.strip().lower() for s in os.getenv("JIRA_SKIP_STATUSES", "Resume Development,Resume QA,Resume Release").split(",") if s.strip()}

def _parse_optional_pr_number(raw: str) -> int:
    value = (raw or "").strip()
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    match = re.search(r"/pull/(\d+)", value)
    if match:
        return int(match.group(1))
    raise ValueError(f"Invalid TEST_PR_NUMBER value: {value!r}. Use PR number (e.g. 600) or PR URL.")

TEST_PR_NUMBER = _parse_optional_pr_number(os.getenv("TEST_PR_NUMBER", "0"))
MAX_NEW_JIRA_TICKETS = int(os.getenv("MAX_NEW_JIRA_TICKETS", "0") or "0")

def can_mutate_jira() -> bool:
    return MODE != "dry-run" or FIX_TICKET_LABELS_EVEN_IN_DRY_MODE

def can_add_pr_links() -> bool:
    return MODE != "dry-run"

def can_transition_jira() -> bool:
    return MODE != "dry-run"

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
            "release_approach_field": JIRA_RELEASE_APPROACH_FIELD,
            "release_approach": JIRA_RELEASE_APPROACH_VALUE,
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

def _jira_single_select_value(raw_value: Any) -> Optional[Dict[str, Any]]:
    if raw_value is None:
        return None
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str):
        value = raw_value.strip()
        return {"value": value} if value else None
    raise ValueError(f"Unsupported Jira select field value type: {type(raw_value).__name__}")

def _jira_single_select_matches(current_value: Any, desired_value: Dict[str, Any]) -> bool:
    if not isinstance(current_value, dict):
        return False
    for key, value in desired_value.items():
        if current_value.get(key) != value:
            return False
    return True

def jira_create_issue(
    summary: str,
    description: str,
    labels: List[str],
    project: str,
    priority: str,
    release_approach_field: str = "",
    release_approach_value: Any = None,
) -> Dict[str, Any]:
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
    release_approach = _jira_single_select_value(release_approach_value)
    if release_approach_field and release_approach:
        payload["fields"][release_approach_field] = release_approach
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
            if pr_url and issue_key and FIX_TICKET_PR_LINKS and can_add_pr_links():
                if jira_add_pr_remotelink(issue_key, pr_url):
                    if VERBOSE and VERBOSE_JIRA_DEDUPE:
                        _log(f"[INFO] Jira {issue_key} linked PR URL via remotelink")
                    return issue_key
                if pr_url and issue_key and jira_issue_has_pr_link(issue_key, pr_url):
                    if VERBOSE and VERBOSE_JIRA_DEDUPE:
                        _log(f"[INFO] Jira {issue_key} matched PR URL in links")
                    return issue_key
                if pr_url and issue_key and FIX_TICKET_PR_LINKS and can_add_pr_links():
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
                    if pr_url and issue_key and FIX_TICKET_PR_LINKS:
                        if jira_add_pr_remotelink(issue_key, pr_url):
                            if VERBOSE and VERBOSE_JIRA_DEDUPE:
                                _log(f"[INFO] Jira {issue_key} linked PR URL via remotelink")
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
    if issue_key.startswith("DRY-RUN") and not can_mutate_jira():
        return False
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
    if not can_add_pr_links():
        return False
    if jira_issue_has_pr_link(issue_key, pr_url):
        return False
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

def jira_get_issue(issue_key: str, extra_fields: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    if issue_key.startswith("DRY-RUN") and not can_mutate_jira():
        return None
    url = f"{JIRA_BASE_URL.rstrip('/')}/rest/api/{JIRA_API_VERSION}/issue/{issue_key}"
    headers = {"Accept": "application/json", **jira_auth()}
    auth = None if JIRA_PAT else HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN)
    fields_to_read = ["labels", "fixVersions", "status", JIRA_EPIC_LINK_FIELD, "issuelinks"]
    if JIRA_RELEASE_APPROACH_FIELD:
        fields_to_read.append(JIRA_RELEASE_APPROACH_FIELD)
    for field_name in extra_fields or []:
        if field_name and field_name not in fields_to_read:
            fields_to_read.append(field_name)
    params = {"fields": ",".join(fields_to_read)}
    try:
        resp = requests.get(url, headers=headers, params=params, auth=auth)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        _vlog(f"[WARN] Jira get issue failed for {issue_key}: {e}")
    return None

def jira_is_withdrawn(issue_key: str) -> bool:
    if issue_key.startswith("DRY-RUN") and not can_mutate_jira():
        return False
    issue = jira_get_issue(issue_key)
    if not issue:
        return False
    status = (issue.get("fields", {}) or {}).get("status", {}) or {}
    return (status.get("name") or "").lower() == "withdrawn"

def jira_get_status_name(issue_key: str) -> str:
    if issue_key.startswith("DRY-RUN") and not can_mutate_jira():
        return ""
    issue = jira_get_issue(issue_key)
    if not issue:
        return ""
    status = (issue.get("fields", {}) or {}).get("status", {}) or {}
    return (status.get("name") or "").strip()

def jira_has_skip_status(issue_key: str) -> bool:
    status = jira_get_status_name(issue_key)
    return status.lower() in JIRA_SKIP_STATUSES if status else False

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

def jira_transition_issue(issue_key: str, status_name: str) -> None:
    if not status_name:
        return
    if issue_key.startswith("DRY-RUN"):
        return
    current_status = jira_get_status_name(issue_key)
    if current_status and current_status.lower() == status_name.lower():
        _vlog(f"[INFO] Jira {issue_key} already in status {current_status}")
        return
    if not can_transition_jira():
        return
    base = JIRA_BASE_URL.rstrip("/")
    headers = {"Accept": "application/json", **jira_auth()}
    auth = None if JIRA_PAT else HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN)
    list_url = f"{base}/rest/api/{JIRA_API_VERSION}/issue/{issue_key}/transitions"
    try:
        resp = requests.get(list_url, headers=headers, auth=auth)
        resp.raise_for_status()
        data = resp.json()
        transitions = data.get("transitions", [])
        if VERBOSE:
            names = [t.get("name") for t in transitions if t.get("name")]
            _log(f"[INFO] Jira {issue_key} available transitions: {names}")
        transition_id = None
        for t in transitions:
            if (t.get("name") or "").lower() == status_name.lower():
                transition_id = t.get("id")
                break
        if not transition_id:
            if VERBOSE:
                _log(f"[INFO] Jira {issue_key} has no transition to status {status_name}")
            return
        payload = {"transition": {"id": transition_id}}
        resp = requests.post(list_url, json=payload, headers=headers, auth=auth)
        resp.raise_for_status()
        _vlog(f"[INFO] Transitioned Jira {issue_key} to status {status_name}")
    except Exception as e:
        _vlog(f"[WARN] Jira transition failed for {issue_key}: {e}")

def jira_transition_issue_path(issue_key: str, statuses: List[str]) -> None:
    if not statuses:
        return
    if issue_key.startswith("DRY-RUN"):
        return
    current_status = jira_get_status_name(issue_key)
    if current_status and current_status.lower() == statuses[-1].lower():
        _vlog(f"[INFO] Jira {issue_key} already in status {current_status}")
        return
    if not can_transition_jira():
        return
    for status in statuses:
        jira_transition_issue(issue_key, status)

def jira_ensure_ticket_fields(
    issue_key: str,
    desired_labels: List[str],
    fix_version: str,
    release_approach_field: str = "",
    release_approach_value: Any = None,
) -> None:
    issue = jira_get_issue(issue_key, extra_fields=[release_approach_field] if release_approach_field else None)
    if not issue:
        return
    _vlog(f"[INFO] Found Jira {issue_key}; checking labels/epic/fixVersion/release approach")
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

    desired_release_approach = _jira_single_select_value(release_approach_value)
    if release_approach_field and desired_release_approach:
        current_release_approach = fields.get(release_approach_field)
        if not _jira_single_select_matches(current_release_approach, desired_release_approach):
            updates[release_approach_field] = desired_release_approach

    if updates:
        _vlog(f"[INFO] Updating Jira {issue_key} fields: {sorted(updates.keys())}")
        jira_update_issue(issue_key, updates)
    else:
        _vlog(f"[INFO] Jira {issue_key} already has desired labels/epic/fixVersion/release approach")

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

def pr_comment_has_ticket(pr, issue_key: str) -> bool:
    key = (issue_key or "").strip().upper()
    if not key:
        return False
    pattern = re.compile(rf"\b{re.escape(key)}\b", re.IGNORECASE)
    try:
        comments = pr.get_issue_comments()
        for c in comments:
            if pattern.search(c.body or ""):
                return True
    except Exception:
        return False
    return False

def maybe_comment_existing_jira_if_missing(pr, issue_key: str, reason: str, repo_full_name: str) -> None:
    if not COMMENT_ON_EXISTING_JIRA_IF_MISSING:
        return
    if not issue_key or issue_key == "UNKNOWN":
        return
    if pr_comment_has_ticket(pr, issue_key):
        _vlog(f"[INFO] PR #{pr.number} in {repo_full_name} already has Jira comment for {issue_key}")
        return
    comment = f"Existing Jira issue {issue_key} already tracks this Renovate PR. Reason: {reason}"
    if MODE == "dry-run":
        _log(f"[DRY-RUN] Would comment on PR #{pr.number} in {repo_full_name}: {comment}")
        return
    try:
        pr.create_issue_comment(comment)
        _log(f"[INFO] Added PR comment linking existing Jira {issue_key} for PR #{pr.number} in {repo_full_name}")
    except Exception as e:
        _elog(f"Warning: failed to comment existing Jira on PR #{pr.number} in {repo_full_name}: {e}")

def _prefixed_pr_title(current_title: str, issue_key: str) -> str:
    title = (current_title or "").strip()
    key = (issue_key or "").strip().upper()
    if not title or not key:
        return title

    match = re.match(r"^\s*([A-Z][A-Z0-9]+-\d+)\s*::\s*(.*)$", title, re.IGNORECASE)
    if match:
        existing_key = (match.group(1) or "").upper()
        remainder = (match.group(2) or "").strip()
        if existing_key == key:
            return title
        if remainder:
            return f"{key} :: {remainder}"
        return key
    return f"{key} :: {title}"

def maybe_update_pr_title_with_jira(pr, issue_key: str, repo_full_name: str) -> None:
    if not UPDATE_PR_TITLE_WITH_JIRA:
        _vlog(f"[INFO] PR title update disabled for PR #{pr.number} in {repo_full_name}")
        return
    if not issue_key or issue_key == "UNKNOWN":
        _vlog(f"[INFO] Skipping PR title update for PR #{pr.number} in {repo_full_name}: invalid Jira key")
        return

    current = pr.title or ""
    desired = _prefixed_pr_title(current, issue_key)
    if desired == current:
        _vlog(f"[INFO] PR #{pr.number} in {repo_full_name} title already contains Jira key {issue_key}")
        return
    if MODE == "dry-run":
        _log(f"[DRY-RUN] Would update PR #{pr.number} title to: {desired}")
        return
    _vlog(f"[INFO] Updating PR #{pr.number} title from '{current}' to '{desired}'")

    primary_error = None
    try:
        pr.edit(title=desired)
        _log(f"[INFO] Updated PR #{pr.number} title with Jira key {issue_key}")
        return
    except Exception as e:
        primary_error = e
        _vlog(f"[WARN] PR edit API failed for PR #{pr.number} in {repo_full_name}: {e}")

    # Fallback via issue API (PRs are also issues).
    try:
        pr.as_issue().edit(title=desired)
        _log(f"[INFO] Updated PR #{pr.number} title with Jira key {issue_key} (issue API fallback)")
    except Exception as e:
        _log(f"[WARN] Failed to update title on PR #{pr.number} in {repo_full_name}: {e}")
        _elog(f"Warning: failed to update title on PR #{pr.number} in {repo_full_name}: primary={primary_error}; fallback={e}")

def process_pr(repo, pr, cfg) -> bool:
    global LOG_PREFIX
    print(f"Processing PR #{pr.number} ({pr.title or ''})")
    previous_prefix = LOG_PREFIX
    LOG_PREFIX = "\t"
    try:
        if not cfg.get("enabled", True):
            _vlog(f"[SKIP] Repo disabled for PR #{getattr(pr,'number','?')} in {repo.full_name}")
            return False
        if pr.state != "open":
            _vlog(f"[SKIP] PR #{pr.number} in {repo.full_name} is not open")
            return False
        require_labels = set(l.lower() for l in cfg.get("github", {}).get("require_labels", []))
        if not require_labels:
            # Backward compatibility for older configs.
            require_labels = set(l.lower() for l in cfg.get("labels", {}).get("require", []))
        pr_labels = set(l.name.lower() for l in pr.get_labels())
        if require_labels and not (pr_labels & require_labels):
            _vlog(f"[SKIP] PR #{pr.number} in {repo.full_name} missing required labels: {sorted(require_labels)}")
            return False
        category, reason = needs_jira(pr.title or "", pr.body or "", [l.name for l in pr.get_labels()],
                                     critical_deps=cfg.get("critical_dependencies", []),
                                     create_jira_for=cfg.get("create_jira_for", {}))
        if not category:
            _vlog(f"[SKIP] PR #{pr.number} in {repo.full_name} did not match any rule")
            return False
        existing = pr_has_ticket_in_comments(pr)
        if existing and jira_is_withdrawn(existing):
            _vlog(f"[INFO] Jira {existing} is Withdrawn; creating a new ticket")
            existing = None
        if existing:
            if jira_has_skip_status(existing):
                _log(f"[SKIP] PR #{pr.number} in {repo.full_name} has Jira ticket {existing} in skip status")
                return False
            if FIX_TICKET_LABELS:
                jira_cfg = cfg.get("jira", {})
                labels_to_add = cfg.get("jira", {}).get("labels", [])
                jira_ensure_ticket_fields(
                    existing,
                    labels_to_add,
                    JIRA_FIX_VERSION,
                    jira_cfg.get("release_approach_field", JIRA_RELEASE_APPROACH_FIELD),
                    jira_cfg.get("release_approach"),
                )
            if UPDATE_PR_TITLE_WITH_EXISTING_JIRA:
                maybe_update_pr_title_with_jira(pr, existing, repo.full_name)
            if cfg.get("github", {}).get("comment", True):
                maybe_comment_existing_jira_if_missing(pr, existing, reason, repo.full_name)
            _log(f"[SKIP] PR #{pr.number} in {repo.full_name} already has Jira ticket {existing}")
            return False
        summary = f"Dependency update: {pr.title}"
        project = cfg.get("jira", {}).get("project", DEFAULT_JIRA_PROJECT)
        existing = jira_find_existing_issue(summary, project, pr.html_url)
        if existing:
            if jira_has_skip_status(existing):
                _log(f"[SKIP] PR #{pr.number} in {repo.full_name} has Jira ticket {existing} in skip status")
                return False
            if FIX_TICKET_LABELS:
                jira_cfg = cfg.get("jira", {})
                labels_to_add = cfg.get("jira", {}).get("labels", [])
                jira_ensure_ticket_fields(
                    existing,
                    labels_to_add,
                    JIRA_FIX_VERSION,
                    jira_cfg.get("release_approach_field", JIRA_RELEASE_APPROACH_FIELD),
                    jira_cfg.get("release_approach"),
                )
            if UPDATE_PR_TITLE_WITH_EXISTING_JIRA:
                maybe_update_pr_title_with_jira(pr, existing, repo.full_name)
            if cfg.get("github", {}).get("comment", True):
                maybe_comment_existing_jira_if_missing(pr, existing, reason, repo.full_name)
            _log(f"[SKIP] PR #{pr.number} in {repo.full_name} already has Jira ticket {existing} (summary+PR link)")
            return False
        jira_preflight(project)
        priority_map = cfg.get("jira", {}).get("priority", {})
        priority = priority_map.get(category, "Medium")
        description = f"Renovate PR: {pr.html_url}\n\nReason detected: {reason}\n\nPR excerpt:\n{(pr.body or '')[:1000]}"
        labels_to_add = cfg.get("jira", {}).get("labels", [])
        jira_cfg = cfg.get("jira", {})
        jira_resp = jira_create_issue(
            summary,
            description,
            labels_to_add,
            project,
            priority,
            jira_cfg.get("release_approach_field", JIRA_RELEASE_APPROACH_FIELD),
            jira_cfg.get("release_approach"),
        )
        issue_key = jira_resp.get("key", "UNKNOWN")
        if pr.html_url and CREATE_PR_LINKS and can_add_pr_links():
            jira_add_pr_remotelink(issue_key, pr.html_url)
        maybe_update_pr_title_with_jira(pr, issue_key, repo.full_name)
        if JIRA_TARGET_STATUS_PATH:
            jira_transition_issue_path(issue_key, JIRA_TARGET_STATUS_PATH)
        elif JIRA_TARGET_STATUS:
            jira_transition_issue(issue_key, JIRA_TARGET_STATUS)
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
        return True
    finally:
        LOG_PREFIX = previous_prefix

def main():
    repos = get_target_repos(gh)
    print(f"Scanning {len(repos)} repos")
    created_count = 0
    for repo in repos:
        cfg = load_repo_config(repo)
        print(f"Repo {repo.full_name} config enabled={cfg.get('enabled', True)}")
        for pr in repo.get_pulls(state="open", sort="updated"):
            if TEST_PR_NUMBER and pr.number != TEST_PR_NUMBER:
                continue
            try:
                created = process_pr(repo, pr, cfg)
                if created:
                    created_count += 1
                    if MAX_NEW_JIRA_TICKETS > 0 and created_count >= MAX_NEW_JIRA_TICKETS:
                        _log(f"[STOP] Reached MAX_NEW_JIRA_TICKETS={MAX_NEW_JIRA_TICKETS}; ending run")
                        return
                time.sleep(0.5)
            except Exception as e:
                print(f"Error processing PR #{getattr(pr,'number','?')} in {repo.full_name}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
