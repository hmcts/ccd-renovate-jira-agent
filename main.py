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
from functools import lru_cache
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
JIRA_WITHDRAW_DUPLICATE_TICKETS = os.getenv("JIRA_WITHDRAW_DUPLICATE_TICKETS", "").lower() in {"1", "true", "yes", "on"}
JIRA_WITHDRAW_DUPLICATE_TICKETS_EVEN_IN_DRY_MODE = os.getenv("JIRA_WITHDRAW_DUPLICATE_TICKETS_EVEN_IN_DRY_MODE", "").lower() in {"1", "true", "yes", "on"}
FIX_TICKET_LABELS = os.getenv("FIX_TICKET_LABELS", "").lower() in {"1", "true", "yes", "on"}
FIX_TICKET_LABELS_EVEN_IN_DRY_MODE = os.getenv("FIX_TICKET_LABELS_EVEN_IN_DRY_MODE", "").lower() in {"1", "true", "yes", "on"}
FIX_TICKET_COMPONENTS = os.getenv("FIX_TICKET_COMPONENTS", "").lower() in {"1", "true", "yes", "on"}
FIX_TICKET_COMPONENTS_EVEN_IN_DRY_MODE = os.getenv("FIX_TICKET_COMPONENTS_EVEN_IN_DRY_MODE", "").lower() in {"1", "true", "yes", "on"}
FIX_TICKET_PR_LINKS = os.getenv("FIX_TICKET_PR_LINKS", "").lower() in {"1", "true", "yes", "on"}
VERBOSE_JIRA_DEDUPE = os.getenv("VERBOSE_JIRA_DEDUPE", "").lower() in {"1", "true", "yes", "on"}
CREATE_PR_LINKS = os.getenv("CREATE_PR_LINKS", "").lower() in {"1", "true", "yes", "on"}
UPDATE_PR_TITLE_WITH_JIRA = os.getenv("UPDATE_PR_TITLE_WITH_JIRA", "true").lower() in {"1", "true", "yes", "on"}
UPDATE_PR_TITLE_WITH_EXISTING_JIRA = os.getenv("UPDATE_PR_TITLE_WITH_EXISTING_JIRA", "false").lower() in {"1", "true", "yes", "on"}
UPDATE_PR_COMMENT_ON_EXISTING_JIRA_IF_MISSING = (
    os.getenv("UPDATE_PR_COMMENT_ON_EXISTING_JIRA_IF_MISSING", os.getenv("COMMENT_ON_EXISTING_JIRA_IF_MISSING", "false"))
    .lower() in {"1", "true", "yes", "on"}
)
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
PR_PROCESS_DELAY_SECONDS = float(os.getenv("PR_PROCESS_DELAY_SECONDS", "0") or "0")
LOG_TIMINGS = os.getenv("LOG_TIMINGS", "").lower() in {"1", "true", "yes", "on"}
PR_LIST_PROGRESS_EVERY = int(os.getenv("PR_LIST_PROGRESS_EVERY", "25") or "25")

def can_mutate_jira() -> bool:
    return MODE != "dry-run" or FIX_TICKET_LABELS_EVEN_IN_DRY_MODE or FIX_TICKET_COMPONENTS_EVEN_IN_DRY_MODE

def can_add_pr_links() -> bool:
    return MODE != "dry-run"

def can_transition_jira() -> bool:
    return MODE != "dry-run"

def _timed(label: str, start_time: float) -> None:
    if LOG_TIMINGS or VERBOSE:
        _log(f"[TIMING] {label}: {time.perf_counter() - start_time:.3f}s")

def _list_progress(repo_full_name: str, phase: str, scanned_count: int, kept_count: int, start_time: Optional[float] = None) -> None:
    suffix = ""
    if start_time is not None:
        suffix = f", elapsed={time.perf_counter() - start_time:.1f}s"
    print(f"[PRS] {repo_full_name} {phase}: scanned={scanned_count}, kept={kept_count}{suffix}")

MODE = os.getenv("MODE", "dry-run").lower()
VERBOSE = os.getenv("VERBOSE", "").lower() in {"1", "true", "yes", "on"}
LOCAL_CONFIG_PATH = os.getenv("LOCAL_CONFIG_PATH")

PAGE_SIZE = int(os.getenv("PAGE_SIZE", "50"))
COMPONENT_REPO_MAPPINGS_FILE = os.getenv("COMPONENT_REPO_MAPPINGS_FILE", "Component-Repo-Mappings.txt")

if not GITHUB_TOKEN:
    sys.exit("GITHUB_TOKEN required")
if not JIRA_BASE_URL:
    sys.exit("JIRA_BASE_URL required")
if not (JIRA_PAT or (JIRA_USER_EMAIL and JIRA_API_TOKEN)):
    sys.exit("Jira configuration missing: set JIRA_PAT or JIRA_USER_EMAIL/JIRA_API_TOKEN")

gh = Github(auth=Auth.Token(GITHUB_TOKEN), per_page=PAGE_SIZE)
_REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
_jira_session = requests.Session()

def load_component_repo_mappings(path: str) -> Dict[str, str]:
    mappings: Dict[str, str] = {}
    if not path or not os.path.exists(path):
        return mappings
    try:
        with open(path, "r") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                component, repo_slug = [part.strip() for part in line.split("=", 1)]
                if component and repo_slug:
                    mappings[repo_slug.lower()] = component
    except Exception as e:
        _elog(f"[WARN] Failed to load component mappings from {path}: {e}")
    return mappings

COMPONENT_REPO_MAPPINGS = load_component_repo_mappings(COMPONENT_REPO_MAPPINGS_FILE)

def jira_component_for_pr(pr_url: str, repo_full_name: str = "") -> Optional[str]:
    pr_url_lower = (pr_url or "").lower()
    repo_slug = (repo_full_name or "").split("/")[-1].strip().lower()

    if repo_slug and repo_slug in COMPONENT_REPO_MAPPINGS:
        return COMPONENT_REPO_MAPPINGS[repo_slug]

    for mapped_repo_slug, component in COMPONENT_REPO_MAPPINGS.items():
        if mapped_repo_slug in pr_url_lower:
            return component

    return None

def _cfg_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)

def _cfg_status_path(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []

def load_repo_config(repo) -> Dict[str, Any]:
    # Defaults apply whenever a repo config omits a key; repo settings override these values.
    defaults = {
        "enabled": True,
        "pr_process_delay_seconds": PR_PROCESS_DELAY_SECONDS,
        "create_jira_for": {"security": True, "major": True, "critical-dep": False},
        "critical_dependencies": [],
        "labels": {},
        "jira": {
            "project": DEFAULT_JIRA_PROJECT,
            "priority": {"security": "High", "major": "Medium", "critical-dep": "High"},
            "labels": ["CCD-BAU", "RENOVATE-PR", "GENERATED-BY-Agent"],
            "release_approach_field": JIRA_RELEASE_APPROACH_FIELD,
            "release_approach": JIRA_RELEASE_APPROACH_VALUE,
            "create_pr_links": CREATE_PR_LINKS,
            "fix_ticket_labels": FIX_TICKET_LABELS,
            "fix_ticket_labels_even_in_dry_mode": FIX_TICKET_LABELS_EVEN_IN_DRY_MODE,
            "fix_components": FIX_TICKET_COMPONENTS,
            "fix_components_even_in_dry_mode": FIX_TICKET_COMPONENTS_EVEN_IN_DRY_MODE,
            "fix_ticket_pr_links": FIX_TICKET_PR_LINKS,
            "withdraw_duplicate_tickets": JIRA_WITHDRAW_DUPLICATE_TICKETS,
            "withdraw_duplicate_tickets_even_in_dry_mode": JIRA_WITHDRAW_DUPLICATE_TICKETS_EVEN_IN_DRY_MODE,
            "transition_merged_existing_via": "",
            "transition_merged_existing_path": [],
            "transition_closed_unmerged_existing_via": "",
            "target_status": JIRA_TARGET_STATUS,
            "target_status_path": JIRA_TARGET_STATUS_PATH,
            "skip_statuses": sorted(JIRA_SKIP_STATUSES),
        },
        "github": {
            "comment": True,
            "add_labels": True,
            "comment_on_existing_jira_if_missing": UPDATE_PR_COMMENT_ON_EXISTING_JIRA_IF_MISSING,
            "require_labels": ["Renovate Dependencies", "Renovate-dependencies"],
            "mark_jira_live_when_linked_pr_merged": False,
            "mark_jira_withdrawn_when_linked_pr_closed_unmerged": False,
            "list_prs_where_author": False,
            "pr_author": "renovate[bot]",
            "update_pr_title_with_new_jira": UPDATE_PR_TITLE_WITH_JIRA,
            "update_pr_title_with_existing_jira": UPDATE_PR_TITLE_WITH_EXISTING_JIRA,
        },
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
        print(f"[CONFIG] Fetching .github/renovate-jira.yml from {repo.full_name}")
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
        print(f"[STARTUP] Loading target repo {GITHUB_REPO}")
        return [gh_client.get_repo(GITHUB_REPO)]
    if REPO_LIST:
        repo_names = [r.strip() for r in REPO_LIST.split(",") if r.strip()]
        repos = []
        for repo_name in repo_names:
            print(f"[STARTUP] Loading target repo {repo_name}")
            repos.append(gh_client.get_repo(repo_name))
        return repos
    if REPO_LIST_FILE and os.path.exists(REPO_LIST_FILE):
        print(f"[STARTUP] Loading target repos from {REPO_LIST_FILE}")
        repos = []
        with open(REPO_LIST_FILE) as f:
            for line in f:
                repo_name = line.strip()
                if not repo_name or repo_name.startswith("#"):
                    continue
                print(f"[STARTUP] Loading target repo {repo_name}")
                repos.append(gh_client.get_repo(repo_name))
        return repos
    if ORG_NAME:
        print(f"[STARTUP] Discovering repos in org {ORG_NAME}")
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

def _jira_request(method: str, path: str, **kwargs):
    headers = kwargs.pop("headers", {})
    auth = kwargs.pop("auth", None if JIRA_PAT else HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN))
    timeout = kwargs.pop("timeout", _REQUEST_TIMEOUT)
    url = f"{JIRA_BASE_URL.rstrip('/')}{path}"
    return _jira_session.request(
        method,
        url,
        headers={"Accept": "application/json", **jira_auth(), **headers},
        auth=auth,
        timeout=timeout,
        **kwargs,
    )

def _clear_jira_issue_cache(issue_key: str) -> None:
    jira_get_issue.cache_clear()
    jira_get_status_name.cache_clear()
    jira_is_withdrawn.cache_clear()
    jira_issue_has_pr_link.cache_clear()

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
    component: Optional[str] = None,
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
    if component:
        payload["fields"]["components"] = [{"name": component}]
    release_approach = _jira_single_select_value(release_approach_value)
    if release_approach_field and release_approach:
        payload["fields"][release_approach_field] = release_approach
    if MODE == "dry-run":
        _log("[DRY-RUN] Would create Jira issue in project {}: {}".format(project, summary))
        return {"key": "DRY-RUN-1"}
    resp = _jira_request("POST", f"/rest/api/{JIRA_API_VERSION}/issue", json=payload)
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
    me = _jira_request("GET", f"/rest/api/{JIRA_API_VERSION}/myself")
    me.raise_for_status()
    proj = _jira_request("GET", f"/rest/api/{JIRA_API_VERSION}/project/{project}")
    proj.raise_for_status()
    _JIRA_PREFLIGHT_OK.add(project)

def _escape_jql(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')

def _jql_field_ref(field_name: str) -> str:
    match = re.fullmatch(r"customfield_(\d+)", field_name or "")
    if match:
        return f"cf[{match.group(1)}]"
    return f'"{_escape_jql(field_name)}"'

def _pr_slug(pr_url: str) -> str:
    m = re.search(r"github\\.com/([^/]+/[^/]+/pull/\\d+)", pr_url or "")
    return m.group(1) if m else ""

def _pr_repo_name(pr_url: str) -> str:
    m = re.search(r"github\\.com/[^/]+/([^/]+)/pull/\\d+", pr_url or "")
    return m.group(1) if m else ""

def _build_summary_token_jql(project: str, text: str) -> Optional[str]:
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", text or "")
    stop = {
        "update", "action", "actions", "bump", "dependency", "dependencies", "to", "from",
        "org", "com", "net", "io", "uk", "github", "hmcts", "version", "plugin"
    }
    keywords = [t for t in tokens if t.lower() not in stop]
    strong = []
    for token in keywords:
        lower = token.lower()
        if re.fullmatch(r"v?\d+(?:[._-]\d+)*", lower):
            continue
        if len(token) < 5:
            continue
        if "-" in token or "_" in token or "." in token:
            strong.append(token)
            continue
        if any(c.isdigit() for c in token) and any(c.isalpha() for c in token):
            strong.append(token)
    selected = []
    for t in strong:
        if t not in selected:
            selected.append(t)
    for t in keywords:
        lower = t.lower()
        if re.fullmatch(r"v?\d+(?:[._-]\d+)*", lower):
            continue
        if len(t) < 5:
            continue
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

def _jira_search_issues(project: str, jql: str, auth, headers, max_results: int = 5) -> List[Dict[str, Any]]:
    params = {"jql": jql, "maxResults": max_results, "fields": "key,summary,description,labels"}
    resp = _jira_request("GET", f"/rest/api/{JIRA_API_VERSION}/search", headers=headers, auth=auth, params=params)
    resp.raise_for_status()
    data = resp.json()
    return data.get("issues", [])

def _jira_issue_sort_key(issue_key: str) -> Any:
    match = re.search(r"-(\d+)$", issue_key or "")
    if match:
        return (0, int(match.group(1)))
    return (1, issue_key or "")

def _required_issue_labels(required_labels: List[str]) -> List[str]:
    return sorted({label.strip() for label in (required_labels or []) if label and label.strip()})

def _issue_has_required_labels(issue: Dict[str, Any], required_labels: List[str]) -> bool:
    wanted = set(_required_issue_labels(required_labels))
    if not wanted:
        return True
    fields = issue.get("fields", {}) or {}
    current = {label.strip() for label in (fields.get("labels") or []) if label and label.strip()}
    return wanted.issubset(current)

def _issue_key_has_required_labels(issue_key: str, required_labels: List[str]) -> bool:
    wanted = set(_required_issue_labels(required_labels))
    if not wanted:
        return True
    issue = jira_get_issue(issue_key, "labels")
    if not issue:
        return False
    return _issue_has_required_labels(issue, list(wanted))

def _jql_with_required_issue_labels(base_jql: str, required_labels: List[str]) -> str:
    base = base_jql or ""
    order_by = ""
    match = re.search(r"\s+ORDER\s+BY\s+.+$", base, re.IGNORECASE)
    if match:
        order_by = match.group(0)
        base = base[:match.start()].rstrip()
    jql = base
    for label in _required_issue_labels(required_labels):
        jql += f' AND labels = "{_escape_jql(label)}"'
    return f"{jql}{order_by}"

def _choose_existing_issue(
    issue_keys: List[str],
    context: str,
    withdraw_duplicates: bool = False,
    allow_withdraw_in_dry_run: bool = False,
) -> Optional[str]:
    keys = sorted({key for key in issue_keys if key}, key=_jira_issue_sort_key)
    if not keys:
        return None
    if len(keys) > 1:
        _log(f"[WARN] Multiple Jira tickets matched {context}: {keys}. Using {keys[0]}")
        if withdraw_duplicates:
            for duplicate_key in keys[1:]:
                _log(f"[INFO] Transitioning duplicate Jira {duplicate_key} to Withdrawn")
                jira_transition_issue(duplicate_key, "Withdrawn", allow_in_dry_run=allow_withdraw_in_dry_run)
    return keys[0]

def _jira_find_issue_keys_by_pr_reference(
    project: str,
    pr_url: str,
    auth,
    headers,
    required_labels: Optional[List[str]] = None,
) -> List[str]:
    if not pr_url:
        return []
    pr_slug = _pr_slug(pr_url)
    required_issue_labels = _required_issue_labels(required_labels or [])
    jqls = []
    if pr_slug:
        jqls.append(_jql_with_required_issue_labels(
            f'project = "{_escape_jql(project)}" '
            f'AND text ~ "{_escape_jql(pr_slug)}" '
            f'AND status != "Withdrawn"'
        , required_issue_labels))
    jqls.append(_jql_with_required_issue_labels(
        f'project = "{_escape_jql(project)}" '
        f'AND text ~ "{_escape_jql(pr_url)}" '
        f'AND status != "Withdrawn"'
    , required_issue_labels))

    matched_keys: List[str] = []
    for jql in jqls:
        try:
            issues = _jira_search_issues(project, jql, auth, headers)
            if VERBOSE and VERBOSE_JIRA_DEDUPE:
                keys = [i.get("key") for i in issues]
                _log(f"[INFO] Jira PR-reference search JQL={jql} -> {keys}")
            for issue in issues:
                if not _issue_has_required_labels(issue, required_issue_labels):
                    continue
                issue_key = issue.get("key")
                description = (issue.get("fields", {}) or {}).get("description") or ""
                if pr_url in description:
                    matched_keys.append(issue_key)
                    continue
                if issue_key and jira_issue_has_pr_link(issue_key, pr_url):
                    matched_keys.append(issue_key)
        except Exception as e:
            if VERBOSE and VERBOSE_JIRA_DEDUPE:
                _log(f"[WARN] Jira PR-reference search failed: {e}")
    if matched_keys:
        return sorted({key for key in matched_keys if key}, key=_jira_issue_sort_key)

    repo_name = _pr_repo_name(pr_url)
    fallback_terms = [term for term in [repo_name, pr_slug] if term]
    for fallback_term in fallback_terms:
        fallback_jql = _jql_with_required_issue_labels(
            f'project = "{_escape_jql(project)}" '
            f'AND text ~ "{_escape_jql(fallback_term)}" '
            f'AND status != "Withdrawn" ORDER BY created DESC',
            required_issue_labels,
        )
        try:
            issues = _jira_search_issues(project, fallback_jql, auth, headers, max_results=50)
            if VERBOSE and VERBOSE_JIRA_DEDUPE:
                keys = [i.get("key") for i in issues]
                _log(f"[INFO] Jira fallback PR-reference search JQL={fallback_jql} -> {keys}")
            for issue in issues:
                if not _issue_has_required_labels(issue, required_issue_labels):
                    continue
                issue_key = issue.get("key")
                description = (issue.get("fields", {}) or {}).get("description") or ""
                if pr_url and pr_url in description:
                    matched_keys.append(issue_key)
                    continue
                if issue_key and jira_issue_has_pr_link(issue_key, pr_url):
                    matched_keys.append(issue_key)
            if matched_keys:
                break
        except Exception as e:
            if VERBOSE and VERBOSE_JIRA_DEDUPE:
                _log(f"[WARN] Jira fallback PR-reference search failed for term '{fallback_term}': {e}")
    return sorted({key for key in matched_keys if key}, key=_jira_issue_sort_key)

def jira_find_existing_issue(
    summary: str,
    project: str,
    pr_url: str,
    required_labels: Optional[List[str]] = None,
    withdraw_duplicates: bool = False,
    allow_withdraw_in_dry_run: bool = False,
    fix_ticket_pr_links: bool = False,
) -> Optional[str]:
    title_candidate = summary.replace("Dependency update: ", "").strip()
    title_candidate = re.sub(r"^[A-Z]+-\d+\s*::\s*", "", title_candidate)
    jql_candidates = [
        summary,
        title_candidate,
    ]
    required_issue_labels = _required_issue_labels(required_labels or [])
    token_jql = _build_summary_token_jql(project, title_candidate)
    if VERBOSE and VERBOSE_JIRA_DEDUPE:
        _log(f"[INFO] Jira search candidates: {jql_candidates}")
    headers = {"Accept": "application/json", **jira_auth()}
    auth = None if JIRA_PAT else HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN)
    pr_reference_match = _choose_existing_issue(
        _jira_find_issue_keys_by_pr_reference(project, pr_url, auth, headers, required_issue_labels),
        f"PR reference {pr_url}",
        withdraw_duplicates=withdraw_duplicates,
        allow_withdraw_in_dry_run=allow_withdraw_in_dry_run,
    )
    if pr_reference_match:
        if VERBOSE and VERBOSE_JIRA_DEDUPE:
            _log(f"[INFO] Jira {pr_reference_match} matched via PR-reference search")
        return pr_reference_match
    for cand in [c for c in jql_candidates if c]:
        safe_candidate = re.sub(r"[^A-Za-z0-9 _./:-]", " ", cand or "")
        safe_candidate = re.sub(r"\s+", " ", safe_candidate).strip()
        if not safe_candidate:
            continue
        jql = _jql_with_required_issue_labels((
            f'project = "{_escape_jql(project)}" '
            f'AND summary ~ "{_escape_jql(safe_candidate)}" '
            f'AND status != "Withdrawn"'
        ), required_issue_labels)
        try:
            issues = _jira_search_issues(project, jql, auth, headers)
            if VERBOSE and VERBOSE_JIRA_DEDUPE:
                keys = [i.get("key") for i in issues]
                _log(f"[INFO] Jira search JQL={jql} -> {keys}")
            matched_keys = []
            for issue in issues:
                if not _issue_has_required_labels(issue, required_issue_labels):
                    continue
                issue_key = issue.get("key")
                description = (issue.get("fields", {}) or {}).get("description") or ""
                if pr_url and pr_url in description:
                    if VERBOSE and VERBOSE_JIRA_DEDUPE:
                        _log(f"[INFO] Jira {issue_key} matched PR URL in description")
                    matched_keys.append(issue_key)
                    continue
                if pr_url and issue_key and jira_issue_has_pr_link(issue_key, pr_url):
                    if VERBOSE and VERBOSE_JIRA_DEDUPE:
                        _log(f"[INFO] Jira {issue_key} matched PR URL in links")
                    matched_keys.append(issue_key)
                    continue
                if pr_url and issue_key and fix_ticket_pr_links and can_add_pr_links():
                    if jira_add_pr_remotelink(issue_key, pr_url):
                        if VERBOSE and VERBOSE_JIRA_DEDUPE:
                            _log(f"[INFO] Jira {issue_key} linked PR URL via remotelink")
                        matched_keys.append(issue_key)
            chosen = _choose_existing_issue(
                matched_keys,
                f"summary match for {pr_url or cand}",
                withdraw_duplicates=withdraw_duplicates,
                allow_withdraw_in_dry_run=allow_withdraw_in_dry_run,
            )
            if chosen:
                return chosen
        except Exception as e:
            if VERBOSE and VERBOSE_JIRA_DEDUPE:
                _log(f"[WARN] Jira search failed for summary match: {e}")
    if token_jql:
        try:
            issues = _jira_search_issues(project, _jql_with_required_issue_labels(token_jql, required_issue_labels), auth, headers)
            if VERBOSE and VERBOSE_JIRA_DEDUPE:
                keys = [i.get("key") for i in issues]
                _log(f"[INFO] Jira search JQL={_jql_with_required_issue_labels(token_jql, required_issue_labels)} -> {keys}")
            matched_keys = []
            for issue in issues:
                if not _issue_has_required_labels(issue, required_issue_labels):
                    continue
                issue_key = issue.get("key")
                description = (issue.get("fields", {}) or {}).get("description") or ""
                if pr_url and pr_url in description:
                    if VERBOSE and VERBOSE_JIRA_DEDUPE:
                        _log(f"[INFO] Jira {issue_key} matched PR URL in description")
                    if pr_url and issue_key and fix_ticket_pr_links:
                        if jira_add_pr_remotelink(issue_key, pr_url):
                            if VERBOSE and VERBOSE_JIRA_DEDUPE:
                                _log(f"[INFO] Jira {issue_key} linked PR URL via remotelink")
                    matched_keys.append(issue_key)
                    continue
                if pr_url and issue_key and jira_issue_has_pr_link(issue_key, pr_url):
                    if VERBOSE and VERBOSE_JIRA_DEDUPE:
                        _log(f"[INFO] Jira {issue_key} matched PR URL in links")
                    matched_keys.append(issue_key)
                    continue
                if pr_url and issue_key and fix_ticket_pr_links:
                    if jira_add_pr_remotelink(issue_key, pr_url):
                        if VERBOSE and VERBOSE_JIRA_DEDUPE:
                            _log(f"[INFO] Jira {issue_key} linked PR URL via remotelink")
                        matched_keys.append(issue_key)
            chosen = _choose_existing_issue(
                matched_keys,
                f"token match for {pr_url or title_candidate}",
                withdraw_duplicates=withdraw_duplicates,
                allow_withdraw_in_dry_run=allow_withdraw_in_dry_run,
            )
            if chosen:
                return chosen
        except Exception as e:
            if VERBOSE and VERBOSE_JIRA_DEDUPE:
                _log(f"[WARN] Jira token search failed: {e}")
    return None

@lru_cache(maxsize=2048)
def jira_issue_has_pr_link(issue_key: str, pr_url: str) -> bool:
    if issue_key.startswith("DRY-RUN") and not can_mutate_jira():
        return False
    pr_slug = _pr_slug(pr_url)
    try:
        resp = _jira_request("GET", f"/rest/api/{JIRA_API_VERSION}/issue/{issue_key}/remotelink")
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
    payload = {"object": {"url": pr_url, "title": f"PR: {pr_url}"}}
    try:
        resp = _jira_request("POST", f"/rest/api/{JIRA_API_VERSION}/issue/{issue_key}/remotelink", json=payload)
        resp.raise_for_status()
        jira_issue_has_pr_link.cache_clear()
        _vlog(f"[INFO] Added PR link to Jira {issue_key}: {pr_url}")
        return True
    except Exception as e:
        _vlog(f"[WARN] Jira remotelink add failed for {issue_key}: {e}")
    return False

@lru_cache(maxsize=1024)
def jira_get_issue(issue_key: str, extra_fields_key: str = "") -> Optional[Dict[str, Any]]:
    if issue_key.startswith("DRY-RUN") and not can_mutate_jira():
        return None
    fields_to_read = ["labels", "fixVersions", "status", "components", JIRA_EPIC_LINK_FIELD, "issuelinks"]
    if JIRA_RELEASE_APPROACH_FIELD:
        fields_to_read.append(JIRA_RELEASE_APPROACH_FIELD)
    extra_fields = [field_name for field_name in extra_fields_key.split(",") if field_name]
    for field_name in extra_fields:
        if field_name and field_name not in fields_to_read:
            fields_to_read.append(field_name)
    params = {"fields": ",".join(fields_to_read)}
    try:
        resp = _jira_request("GET", f"/rest/api/{JIRA_API_VERSION}/issue/{issue_key}", params=params)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        _vlog(f"[WARN] Jira get issue failed for {issue_key}: {e}")
    return None

@lru_cache(maxsize=1024)
def jira_is_withdrawn(issue_key: str) -> bool:
    if issue_key.startswith("DRY-RUN") and not can_mutate_jira():
        return False
    issue = jira_get_issue(issue_key)
    if not issue:
        return False
    status = (issue.get("fields", {}) or {}).get("status", {}) or {}
    return (status.get("name") or "").lower() == "withdrawn"

@lru_cache(maxsize=1024)
def jira_get_status_name(issue_key: str) -> str:
    if issue_key.startswith("DRY-RUN") and not can_mutate_jira():
        return ""
    issue = jira_get_issue(issue_key)
    if not issue:
        return ""
    status = (issue.get("fields", {}) or {}).get("status", {}) or {}
    return (status.get("name") or "").strip()

def jira_has_skip_status(issue_key: str, skip_statuses: Optional[set] = None) -> bool:
    status = jira_get_status_name(issue_key)
    effective_skip_statuses = skip_statuses if skip_statuses is not None else JIRA_SKIP_STATUSES
    return status.lower() in effective_skip_statuses if status else False

def jira_update_issue(issue_key: str, fields: Dict[str, Any], allow_in_dry_run: bool = False) -> None:
    if MODE == "dry-run" and not allow_in_dry_run:
        _log(f"[DRY-RUN] Would update Jira {issue_key} fields: {list(fields.keys())}")
        return
    payload = {"fields": fields}
    resp = _jira_request("PUT", f"/rest/api/{JIRA_API_VERSION}/issue/{issue_key}", json=payload)
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except ValueError:
            err = {"message": (resp.text or "").strip().replace("\n", " ")[:500]}
        raise RuntimeError(f"Jira update failed for {issue_key} (status {resp.status_code}): {err}") from None
    _clear_jira_issue_cache(issue_key)

def jira_transition_issue(issue_key: str, status_name: str, allow_in_dry_run: bool = False) -> bool:
    if not status_name:
        return False
    if issue_key.startswith("DRY-RUN"):
        return False
    current_status = jira_get_status_name(issue_key)
    if current_status and current_status.lower() == status_name.lower():
        _vlog(f"[INFO] Jira {issue_key} already in status {current_status}")
        return True
    if MODE == "dry-run" and not allow_in_dry_run:
        _log(f"[DRY-RUN] Would transition Jira {issue_key} to status {status_name}")
        return True
    if MODE == "dry-run" and allow_in_dry_run:
        _log(f"[INFO] Transitioning Jira {issue_key} to status {status_name} during dry-run override")
    elif not can_transition_jira():
        return False
    try:
        resp = _jira_request("GET", f"/rest/api/{JIRA_API_VERSION}/issue/{issue_key}/transitions")
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
            return False
        payload = {"transition": {"id": transition_id}}
        resp = _jira_request("POST", f"/rest/api/{JIRA_API_VERSION}/issue/{issue_key}/transitions", json=payload)
        resp.raise_for_status()
        _clear_jira_issue_cache(issue_key)
        _vlog(f"[INFO] Transitioned Jira {issue_key} to status {status_name}")
        return True
    except Exception as e:
        _vlog(f"[WARN] Jira transition failed for {issue_key}: {e}")
    return False

def jira_transition_issue_path(issue_key: str, statuses: List[str], allow_in_dry_run: bool = False) -> bool:
    if not statuses:
        return False
    if issue_key.startswith("DRY-RUN"):
        return False
    original_status = jira_get_status_name(issue_key)
    if original_status and original_status.lower() == statuses[-1].lower():
        _vlog(f"[INFO] Jira {issue_key} already in status {original_status}")
        return True
    if not can_transition_jira() and not (MODE == "dry-run" and allow_in_dry_run):
        return False
    for status in statuses:
        if not jira_transition_issue(issue_key, status, allow_in_dry_run=allow_in_dry_run):
            _log(f"[WARN] Jira {issue_key} transition path failed at status {status}; attempting rollback to {original_status or 'original status unknown'}")
            if original_status:
                current_status = jira_get_status_name(issue_key)
                if current_status and current_status.lower() != original_status.lower():
                    if jira_transition_issue(issue_key, original_status, allow_in_dry_run=allow_in_dry_run):
                        _log(f"[INFO] Rolled Jira {issue_key} back to {original_status}")
                    else:
                        _log(f"[WARN] Rollback failed for Jira {issue_key}; current status is {jira_get_status_name(issue_key) or 'unknown'}")
            return False
    return True

def jira_ensure_ticket_fields(
    issue_key: str,
    desired_labels: List[str],
    fix_version: str,
    component: Optional[str] = None,
    release_approach_field: str = "",
    release_approach_value: Any = None,
    sync_labels_bundle: bool = True,
    sync_component: bool = False,
    allow_in_dry_run: bool = False,
) -> None:
    issue = jira_get_issue(issue_key, release_approach_field if release_approach_field else "")
    if not issue:
        return
    _vlog(f"[INFO] Found Jira {issue_key}; checking requested Jira field sync")
    fields = issue.get("fields", {}) or {}
    updates: Dict[str, Any] = {}

    if sync_labels_bundle:
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

    if sync_component:
        current_components = [c.get("name") for c in (fields.get("components") or []) if c.get("name")]
        if component and component not in current_components:
            updates["components"] = [{"name": component}]

    if updates:
        _vlog(f"[INFO] Updating Jira {issue_key} fields: {sorted(updates.keys())}")
        jira_update_issue(issue_key, updates, allow_in_dry_run=allow_in_dry_run)
    else:
        _vlog(f"[INFO] Jira {issue_key} already has the requested field values")

def _extract_jira_key(text: str) -> Optional[str]:
    match = re.search(r"\b((?:CCD|HMC)-\d+)\b", text or "", re.IGNORECASE)
    return match.group(1).upper() if match else None

def _extract_prefixed_jira_key_from_title(title: str) -> Optional[str]:
    match = re.match(r"^\s*((?:CCD|HMC)-\d+)\s*::\s*", title or "", re.IGNORECASE)
    return match.group(1).upper() if match else None

def _pr_comment_bodies(pr) -> List[str]:
    cached = getattr(pr, "_cached_issue_comment_bodies", None)
    if cached is not None:
        return cached
    try:
        bodies = [c.body or "" for c in pr.get_issue_comments()]
    except Exception:
        bodies = []
    setattr(pr, "_cached_issue_comment_bodies", bodies)
    return bodies

def pr_find_referenced_ticket(pr, required_labels: Optional[List[str]] = None) -> Optional[str]:
    required_issue_labels = _required_issue_labels(required_labels or [])
    issue_key = _extract_prefixed_jira_key_from_title(pr.title or "")
    if issue_key and _issue_key_has_required_labels(issue_key, required_issue_labels):
        return issue_key
    for body in _pr_comment_bodies(pr):
        issue_key = _extract_jira_key(body)
        if issue_key and _issue_key_has_required_labels(issue_key, required_issue_labels):
            return issue_key
    return None

def pr_comment_has_ticket(pr, issue_key: str) -> bool:
    key = (issue_key or "").strip().upper()
    if not key:
        return False
    pattern = re.compile(rf"\b{re.escape(key)}\b", re.IGNORECASE)
    for body in _pr_comment_bodies(pr):
        if pattern.search(body):
            return True
    return False

def maybe_comment_existing_jira_if_missing(pr, issue_key: str, reason: str, repo_full_name: str, enabled: bool = False) -> None:
    if not enabled:
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
        setattr(pr, "_cached_issue_comment_bodies", None)
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

def maybe_update_pr_title_with_jira(pr, issue_key: str, repo_full_name: str, enabled: bool = True) -> None:
    if not enabled:
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

def _pr_is_merged(pr) -> bool:
    if getattr(pr, "merged_at", None):
        return True
    merged = getattr(pr, "merged", None)
    return bool(merged)

def _pr_is_closed_unmerged(pr) -> bool:
    return pr.state == "closed" and not _pr_is_merged(pr)

def _pr_matches_author(pr, author: str) -> bool:
    if not author:
        return True
    user = getattr(pr, "user", None)
    login = getattr(user, "login", "") or ""
    return login.lower() == author.lower()

def _extract_pr_urls(text: str) -> List[str]:
    return sorted({
        match.group(0)
        for match in re.finditer(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+", text or "")
    })

def _parse_pr_url(pr_url: str) -> Optional[tuple]:
    match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url or "")
    if not match:
        return None
    return match.group(1), match.group(2), int(match.group(3))

@lru_cache(maxsize=4096)
def github_get_pr_by_url(pr_url: str):
    parsed = _parse_pr_url(pr_url)
    if not parsed:
        return None
    owner, repo_name, pr_number = parsed
    try:
        return gh.get_repo(f"{owner}/{repo_name}").get_pull(pr_number)
    except Exception as e:
        _vlog(f"[WARN] Failed to load GitHub PR for {pr_url}: {e}")
        return None

def jira_search_issues_all(jql: str, fields: str, max_results: int = 50) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    start_at = 0
    while True:
        params = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": max_results,
            "fields": fields,
        }
        resp = _jira_request("GET", f"/rest/api/{JIRA_API_VERSION}/search", params=params)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("issues", []) or []
        issues.extend(batch)
        if start_at + len(batch) >= data.get("total", 0) or not batch:
            return issues
        start_at += len(batch)

def jira_list_epic_child_issues(project: str, required_labels: List[str]) -> List[Dict[str, Any]]:
    epic_key = _escape_jql(JIRA_EPIC_KEY)
    project_key = _escape_jql(project)
    jql = (
        f'project = "{project_key}" '
        f'AND {_jql_field_ref(JIRA_EPIC_LINK_FIELD)} = "{epic_key}" '
        f'AND status != "Withdrawn"'
    )
    jql = _jql_with_required_issue_labels(jql, required_labels)
    fields = "key,summary,description,labels,status,components"
    return jira_search_issues_all(jql, fields)

def jira_issue_pr_urls(issue_key: str, issue: Optional[Dict[str, Any]] = None) -> List[str]:
    urls = set(_extract_pr_urls((issue or {}).get("fields", {}).get("description") or ""))
    try:
        resp = _jira_request("GET", f"/rest/api/{JIRA_API_VERSION}/issue/{issue_key}/remotelink")
        resp.raise_for_status()
        for link in resp.json() or []:
            obj = link.get("object", {}) or {}
            link_url = obj.get("url") or ""
            if "/pull/" in link_url and "github.com/" in link_url:
                urls.update(_extract_pr_urls(link_url))
    except Exception as e:
        _vlog(f"[WARN] Jira remotelink fetch failed for {issue_key}: {e}")
    return sorted(urls)

def get_target_pr_groups(repo, cfg):
    open_prs = []
    if TEST_PR_NUMBER:
        print(f"[PRS] Loading TEST_PR_NUMBER={TEST_PR_NUMBER} in {repo.full_name}")
        try:
            pr = repo.get_pull(TEST_PR_NUMBER)
            if pr.state == "open":
                open_prs.append(pr)
        except Exception as e:
            _vlog(f"[WARN] Failed to load TEST_PR_NUMBER={TEST_PR_NUMBER} in {repo.full_name}: {e}")
        return open_prs

    list_prs_where_author = _cfg_bool(cfg.get("github", {}).get("list_prs_where_author"), False)
    author_filter = (cfg.get("github", {}).get("pr_author") or "renovate[bot]").strip() if list_prs_where_author else ""

    seen = set()
    open_list_started = time.perf_counter()
    print(f"[PRS] Listing open PRs for {repo.full_name} (total unknown until scan completes)")
    scanned_open = 0
    for pr in repo.get_pulls(state="open", sort="updated"):
        scanned_open += 1
        if author_filter and not _pr_matches_author(pr, author_filter):
            if PR_LIST_PROGRESS_EVERY > 0 and scanned_open % PR_LIST_PROGRESS_EVERY == 0:
                _list_progress(repo.full_name, "open PR listing", scanned_open, len(open_prs), open_list_started)
            continue
        seen.add(pr.number)
        open_prs.append(pr)
        if PR_LIST_PROGRESS_EVERY > 0 and scanned_open % PR_LIST_PROGRESS_EVERY == 0:
            _list_progress(repo.full_name, "open PR listing", scanned_open, len(open_prs), open_list_started)
    _list_progress(repo.full_name, "open PR listing complete", scanned_open, len(open_prs), open_list_started)

    return open_prs

def process_pr(repo, pr, cfg) -> bool:
    global LOG_PREFIX
    print(f"Processing PR #{pr.number} ({pr.title or ''})")
    process_started = time.perf_counter()
    previous_prefix = LOG_PREFIX
    LOG_PREFIX = "\t"
    try:
        if not cfg.get("enabled", True):
            _vlog(f"[SKIP] Repo disabled for PR #{getattr(pr,'number','?')} in {repo.full_name}")
            return False
        jira_component = jira_component_for_pr(pr.html_url, repo.full_name)
        jira_cfg = cfg.get("jira", {})
        github_cfg = cfg.get("github", {})
        create_pr_links = _cfg_bool(jira_cfg.get("create_pr_links"), CREATE_PR_LINKS)
        fix_ticket_labels = _cfg_bool(jira_cfg.get("fix_ticket_labels"), FIX_TICKET_LABELS)
        fix_ticket_labels_even_in_dry_mode = _cfg_bool(
            jira_cfg.get("fix_ticket_labels_even_in_dry_mode"),
            FIX_TICKET_LABELS_EVEN_IN_DRY_MODE,
        )
        fix_ticket_components = _cfg_bool(jira_cfg.get("fix_components"), FIX_TICKET_COMPONENTS)
        fix_ticket_components_even_in_dry_mode = _cfg_bool(
            jira_cfg.get("fix_components_even_in_dry_mode"),
            FIX_TICKET_COMPONENTS_EVEN_IN_DRY_MODE,
        )
        fix_ticket_pr_links = _cfg_bool(jira_cfg.get("fix_ticket_pr_links"), FIX_TICKET_PR_LINKS)
        withdraw_duplicate_tickets = _cfg_bool(
            jira_cfg.get("withdraw_duplicate_tickets"),
            JIRA_WITHDRAW_DUPLICATE_TICKETS,
        )
        withdraw_duplicate_tickets_even_in_dry_mode = _cfg_bool(
            jira_cfg.get("withdraw_duplicate_tickets_even_in_dry_mode"),
            JIRA_WITHDRAW_DUPLICATE_TICKETS_EVEN_IN_DRY_MODE,
        )
        transition_merged_existing_via = (jira_cfg.get("transition_merged_existing_via") or "").strip()
        transition_merged_existing_path = _cfg_status_path(
            jira_cfg.get("transition_merged_existing_path")
        )
        transition_closed_unmerged_existing_via = (
            jira_cfg.get("transition_closed_unmerged_existing_via") or ""
        ).strip()
        target_status = (jira_cfg.get("target_status") or "").strip()
        target_status_path = _cfg_status_path(jira_cfg.get("target_status_path"))
        skip_statuses = {s.strip().lower() for s in (jira_cfg.get("skip_statuses") or []) if str(s).strip()}
        mark_jira_live_when_linked_pr_merged = _cfg_bool(
            github_cfg.get("mark_jira_live_when_linked_pr_merged"),
            False,
        )
        mark_jira_withdrawn_when_linked_pr_closed_unmerged = _cfg_bool(
            github_cfg.get("mark_jira_withdrawn_when_linked_pr_closed_unmerged"),
            False,
        )
        update_pr_title_with_jira = _cfg_bool(
            github_cfg.get("update_pr_title_with_new_jira"),
            UPDATE_PR_TITLE_WITH_JIRA,
        )
        update_pr_title_with_existing_jira = _cfg_bool(
            github_cfg.get("update_pr_title_with_existing_jira"),
            UPDATE_PR_TITLE_WITH_EXISTING_JIRA,
        )
        comment_on_existing_jira_if_missing = _cfg_bool(
            github_cfg.get("comment_on_existing_jira_if_missing"),
            UPDATE_PR_COMMENT_ON_EXISTING_JIRA_IF_MISSING,
        )
        allow_ticket_updates_in_dry_run = (
            fix_ticket_labels_even_in_dry_mode
            or fix_ticket_components_even_in_dry_mode
            or withdraw_duplicate_tickets_even_in_dry_mode
        )
        can_fix_existing_ticket = fix_ticket_labels or fix_ticket_components or withdraw_duplicate_tickets
        is_open_pr = pr.state == "open"
        is_merged_pr = pr.state == "closed" and _pr_is_merged(pr)
        is_closed_unmerged_pr = _pr_is_closed_unmerged(pr)
        if not is_open_pr:
            if not (
                can_fix_existing_ticket
                and (
                    (mark_jira_live_when_linked_pr_merged and is_merged_pr)
                    or (mark_jira_withdrawn_when_linked_pr_closed_unmerged and is_closed_unmerged_pr)
                )
            ):
                _vlog(f"[SKIP] PR #{pr.number} in {repo.full_name} is not eligible in state={pr.state}")
                return False
        require_labels = set(l.lower() for l in github_cfg.get("require_labels", []))
        if not require_labels:
            # Backward compatibility for older configs.
            require_labels = set(l.lower() for l in cfg.get("labels", {}).get("require", []))
        pr_labels = set(l.name.lower() for l in pr.get_labels())
        if require_labels and not (pr_labels & require_labels):
            if is_open_pr:
                _vlog(f"[SKIP] PR #{pr.number} in {repo.full_name} missing required labels: {sorted(require_labels)}")
                return False
            _vlog(f"[INFO] PR #{pr.number} in {repo.full_name} missing required labels but continuing existing-ticket fix path for state={pr.state}")
        category = None
        reason = "Jira key already referenced on PR"
        required_existing_labels = cfg.get("jira", {}).get("labels", [])
        existing = pr_find_referenced_ticket(pr, required_existing_labels)
        project = cfg.get("jira", {}).get("project", DEFAULT_JIRA_PROJECT)
        if existing and withdraw_duplicate_tickets:
            headers = {"Accept": "application/json", **jira_auth()}
            auth = None if JIRA_PAT else HTTPBasicAuth(JIRA_USER_EMAIL, JIRA_API_TOKEN)
            pr_reference_keys = _jira_find_issue_keys_by_pr_reference(
                project,
                pr.html_url,
                auth,
                headers,
                required_existing_labels,
            )
            existing = _choose_existing_issue(
                [existing, *pr_reference_keys],
                f"referenced Jira for {pr.html_url or pr.number}",
                withdraw_duplicates=True,
                allow_withdraw_in_dry_run=withdraw_duplicate_tickets_even_in_dry_mode,
            ) or existing
        if existing and jira_is_withdrawn(existing):
            _vlog(f"[INFO] Jira {existing} is Withdrawn; creating a new ticket")
            existing = None
        if existing:
            if jira_has_skip_status(existing, skip_statuses):
                _log(f"[SKIP] PR #{pr.number} in {repo.full_name} has Jira ticket {existing} in skip status")
                return False
            if fix_ticket_labels or fix_ticket_components:
                labels_to_add = cfg.get("jira", {}).get("labels", [])
                jira_ensure_ticket_fields(
                    existing,
                    labels_to_add,
                    JIRA_FIX_VERSION,
                    jira_component,
                    jira_cfg.get("release_approach_field", JIRA_RELEASE_APPROACH_FIELD),
                    jira_cfg.get("release_approach"),
                    sync_labels_bundle=fix_ticket_labels,
                    sync_component=fix_ticket_components,
                    allow_in_dry_run=allow_ticket_updates_in_dry_run,
                )
            if is_merged_pr and transition_merged_existing_path:
                jira_transition_issue_path(
                    existing,
                    transition_merged_existing_path,
                    allow_in_dry_run=allow_ticket_updates_in_dry_run,
                )
            elif is_merged_pr and transition_merged_existing_via:
                jira_transition_issue(
                    existing,
                    transition_merged_existing_via,
                    allow_in_dry_run=allow_ticket_updates_in_dry_run,
                )
            elif is_closed_unmerged_pr and transition_closed_unmerged_existing_via:
                jira_transition_issue(
                    existing,
                    transition_closed_unmerged_existing_via,
                    allow_in_dry_run=allow_ticket_updates_in_dry_run,
                )
            if update_pr_title_with_existing_jira:
                maybe_update_pr_title_with_jira(pr, existing, repo.full_name, enabled=update_pr_title_with_existing_jira)
            if github_cfg.get("comment", True):
                maybe_comment_existing_jira_if_missing(pr, existing, reason, repo.full_name, enabled=comment_on_existing_jira_if_missing)
            _log(f"[SKIP] PR #{pr.number} in {repo.full_name} already has Jira ticket {existing}")
            return False
        summary = f"Dependency update: {pr.title}"
        existing = jira_find_existing_issue(
            summary,
            project,
            pr.html_url,
            required_existing_labels,
            withdraw_duplicates=withdraw_duplicate_tickets,
            allow_withdraw_in_dry_run=withdraw_duplicate_tickets_even_in_dry_mode,
            fix_ticket_pr_links=fix_ticket_pr_links,
        )
        if existing:
            if jira_has_skip_status(existing, skip_statuses):
                _log(f"[SKIP] PR #{pr.number} in {repo.full_name} has Jira ticket {existing} in skip status")
                return False
            if fix_ticket_labels or fix_ticket_components:
                labels_to_add = cfg.get("jira", {}).get("labels", [])
                jira_ensure_ticket_fields(
                    existing,
                    labels_to_add,
                    JIRA_FIX_VERSION,
                    jira_component,
                    jira_cfg.get("release_approach_field", JIRA_RELEASE_APPROACH_FIELD),
                    jira_cfg.get("release_approach"),
                    sync_labels_bundle=fix_ticket_labels,
                    sync_component=fix_ticket_components,
                    allow_in_dry_run=allow_ticket_updates_in_dry_run,
                )
            if is_merged_pr and transition_merged_existing_path:
                jira_transition_issue_path(
                    existing,
                    transition_merged_existing_path,
                    allow_in_dry_run=allow_ticket_updates_in_dry_run,
                )
            elif is_merged_pr and transition_merged_existing_via:
                jira_transition_issue(
                    existing,
                    transition_merged_existing_via,
                    allow_in_dry_run=allow_ticket_updates_in_dry_run,
                )
            elif is_closed_unmerged_pr and transition_closed_unmerged_existing_via:
                jira_transition_issue(
                    existing,
                    transition_closed_unmerged_existing_via,
                    allow_in_dry_run=allow_ticket_updates_in_dry_run,
                )
            if update_pr_title_with_existing_jira:
                maybe_update_pr_title_with_jira(pr, existing, repo.full_name, enabled=update_pr_title_with_existing_jira)
            if github_cfg.get("comment", True):
                maybe_comment_existing_jira_if_missing(pr, existing, reason, repo.full_name, enabled=comment_on_existing_jira_if_missing)
            _log(f"[SKIP] PR #{pr.number} in {repo.full_name} already has Jira ticket {existing} (summary+PR link)")
            return False
        if not is_open_pr:
            _vlog(f"[SKIP] PR #{pr.number} in {repo.full_name} is merged/closed and no existing Jira ticket was matched")
            return False
        category, reason = needs_jira(pr.title or "", pr.body or "", [l.name for l in pr.get_labels()],
                                     critical_deps=cfg.get("critical_dependencies", []),
                                     create_jira_for=cfg.get("create_jira_for", {}))
        if not category:
            _vlog(f"[SKIP] PR #{pr.number} in {repo.full_name} did not match any rule")
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
            jira_component,
            jira_cfg.get("release_approach_field", JIRA_RELEASE_APPROACH_FIELD),
            jira_cfg.get("release_approach"),
        )
        issue_key = jira_resp.get("key", "UNKNOWN")
        if pr.html_url and create_pr_links and can_add_pr_links():
            jira_add_pr_remotelink(issue_key, pr.html_url)
        maybe_update_pr_title_with_jira(pr, issue_key, repo.full_name, enabled=update_pr_title_with_jira)
        if target_status_path:
            jira_transition_issue_path(issue_key, target_status_path)
        elif target_status:
            jira_transition_issue(issue_key, target_status)
        comment = f"Created Jira issue {issue_key} to track this Renovate PR. Reason: {reason}"
        if MODE != "dry-run":
            if github_cfg.get("comment", True):
                try:
                    pr.create_issue_comment(comment)
                    setattr(pr, "_cached_issue_comment_bodies", None)
                except Exception as e:
                    _elog(f"Warning: failed to comment on PR #{pr.number} in {repo.full_name}: {e}")
            if github_cfg.get("add_labels", True):
                try:
                    pr.add_to_labels(*labels_to_add)
                except Exception as e:
                    _elog(f"Warning: failed to add labels on PR #{pr.number} in {repo.full_name}: {e}")
        _log(f"Created Jira {issue_key} for PR #{pr.number} in {repo.full_name}")
        return True
    finally:
        _timed(f"PR #{pr.number} in {repo.full_name}", process_started)
        LOG_PREFIX = previous_prefix

def maintain_repo_jira_tickets(repo, cfg) -> None:
    jira_cfg = cfg.get("jira", {})
    include_merged = _cfg_bool(cfg.get("github", {}).get("mark_jira_live_when_linked_pr_merged"), False)
    include_closed_unmerged = _cfg_bool(cfg.get("github", {}).get("mark_jira_withdrawn_when_linked_pr_closed_unmerged"), False)
    if not include_merged and not include_closed_unmerged:
        return

    required_existing_labels = jira_cfg.get("labels", [])
    fix_ticket_labels = _cfg_bool(jira_cfg.get("fix_ticket_labels"), FIX_TICKET_LABELS)
    fix_ticket_labels_even_in_dry_mode = _cfg_bool(
        jira_cfg.get("fix_ticket_labels_even_in_dry_mode"),
        FIX_TICKET_LABELS_EVEN_IN_DRY_MODE,
    )
    withdraw_duplicate_tickets = _cfg_bool(
        jira_cfg.get("withdraw_duplicate_tickets"),
        JIRA_WITHDRAW_DUPLICATE_TICKETS,
    )
    withdraw_duplicate_tickets_even_in_dry_mode = _cfg_bool(
        jira_cfg.get("withdraw_duplicate_tickets_even_in_dry_mode"),
        JIRA_WITHDRAW_DUPLICATE_TICKETS_EVEN_IN_DRY_MODE,
    )
    fix_ticket_components = _cfg_bool(jira_cfg.get("fix_components"), FIX_TICKET_COMPONENTS)
    fix_ticket_components_even_in_dry_mode = _cfg_bool(
        jira_cfg.get("fix_components_even_in_dry_mode"),
        FIX_TICKET_COMPONENTS_EVEN_IN_DRY_MODE,
    )
    skip_statuses = {s.strip().lower() for s in (jira_cfg.get("skip_statuses") or []) if str(s).strip()}
    allow_ticket_updates_in_dry_run = (
        fix_ticket_labels_even_in_dry_mode
        or fix_ticket_components_even_in_dry_mode
        or withdraw_duplicate_tickets_even_in_dry_mode
    )
    project = jira_cfg.get("project", DEFAULT_JIRA_PROJECT)
    transition_merged_existing_via = (jira_cfg.get("transition_merged_existing_via") or "").strip()
    transition_merged_existing_path = _cfg_status_path(
        jira_cfg.get("transition_merged_existing_path")
    )
    transition_closed_unmerged_existing_via = (
        jira_cfg.get("transition_closed_unmerged_existing_via") or ""
    ).strip()

    started = time.perf_counter()
    print(f"[JIRA] Listing epic child tickets for {repo.full_name}")
    try:
        issues = jira_list_epic_child_issues(project, required_existing_labels)
    except Exception as e:
        _elog(f"[WARN] Failed to list Jira epic child tickets for {repo.full_name}: {e}")
        return
    _list_progress(repo.full_name, "jira epic child listing complete", len(issues), len(issues), started)

    pr_to_issue_keys: Dict[str, List[str]] = {}
    for issue in issues:
        issue_key = issue.get("key")
        if not issue_key:
            continue
        for pr_url in jira_issue_pr_urls(issue_key, issue):
            parsed = _parse_pr_url(pr_url)
            if not parsed:
                continue
            owner, repo_name, _ = parsed
            if f"{owner}/{repo_name}".lower() != repo.full_name.lower():
                continue
            pr_to_issue_keys.setdefault(pr_url, []).append(issue_key)

    if not pr_to_issue_keys:
        _vlog(f"[INFO] No Jira epic child tickets linked to PRs in {repo.full_name}")
        return

    print(f"[JIRA] Maintaining {len(pr_to_issue_keys)} Jira-linked PR ticket group(s) for {repo.full_name}")
    jira_component = jira_component_for_pr("", repo.full_name)
    for pr_url, issue_keys in sorted(pr_to_issue_keys.items()):
        canonical_issue = _choose_existing_issue(
            issue_keys,
            f"epic child tickets for {pr_url}",
            withdraw_duplicates=withdraw_duplicate_tickets,
            allow_withdraw_in_dry_run=withdraw_duplicate_tickets_even_in_dry_mode,
        )
        if not canonical_issue or jira_is_withdrawn(canonical_issue):
            continue
        if jira_has_skip_status(canonical_issue, skip_statuses):
            _log(f"[SKIP] Jira {canonical_issue} for {pr_url} is in skip status")
            continue

        pr = github_get_pr_by_url(pr_url)
        if not pr:
            continue

        if fix_ticket_labels or fix_ticket_components:
            jira_ensure_ticket_fields(
                canonical_issue,
                jira_cfg.get("labels", []),
                JIRA_FIX_VERSION,
                jira_component,
                jira_cfg.get("release_approach_field", JIRA_RELEASE_APPROACH_FIELD),
                jira_cfg.get("release_approach"),
                sync_labels_bundle=fix_ticket_labels,
                sync_component=fix_ticket_components,
                allow_in_dry_run=allow_ticket_updates_in_dry_run,
            )

        if _pr_is_merged(pr):
            if include_merged and transition_merged_existing_path:
                jira_transition_issue_path(
                    canonical_issue,
                    transition_merged_existing_path,
                    allow_in_dry_run=allow_ticket_updates_in_dry_run,
                )
            elif include_merged and transition_merged_existing_via:
                jira_transition_issue(
                    canonical_issue,
                    transition_merged_existing_via,
                    allow_in_dry_run=allow_ticket_updates_in_dry_run,
                )
        elif _pr_is_closed_unmerged(pr):
            if include_closed_unmerged and transition_closed_unmerged_existing_via:
                jira_transition_issue(
                    canonical_issue,
                    transition_closed_unmerged_existing_via,
                    allow_in_dry_run=allow_ticket_updates_in_dry_run,
                )

def main():
    run_started = time.perf_counter()
    repos = get_target_repos(gh)
    print(f"Scanning {len(repos)} repos")
    created_count = 0
    for repo in repos:
        repo_started = time.perf_counter()
        print(f"[REPO] Starting {repo.full_name}")
        cfg = load_repo_config(repo)
        repo_pr_delay_seconds = float(cfg.get("pr_process_delay_seconds", PR_PROCESS_DELAY_SECONDS) or 0)
        print(f"Repo {repo.full_name} config enabled={cfg.get('enabled', True)}")
        open_prs = get_target_pr_groups(repo, cfg)

        def _process_repo_prs(prs, phase_label: str) -> bool:
            nonlocal created_count
            if prs:
                _vlog(f"[INFO] {repo.full_name} {phase_label}: {len(prs)} PR(s)")
            for pr in prs:
                try:
                    created = process_pr(repo, pr, cfg)
                    if created:
                        created_count += 1
                        if MAX_NEW_JIRA_TICKETS > 0 and created_count >= MAX_NEW_JIRA_TICKETS:
                            _log(f"[STOP] Reached MAX_NEW_JIRA_TICKETS={MAX_NEW_JIRA_TICKETS}; ending run")
                            _timed("total run", run_started)
                            return True
                    if repo_pr_delay_seconds > 0:
                        time.sleep(repo_pr_delay_seconds)
                except Exception as e:
                    print(f"Error processing PR #{getattr(pr,'number','?')} in {repo.full_name}: {e}", file=sys.stderr)
            return False

        if _process_repo_prs(open_prs, "open PR phase"):
            return
        maintain_repo_jira_tickets(repo, cfg)
        _timed(f"repo {repo.full_name}", repo_started)
    _timed("total run", run_started)

if __name__ == "__main__":
    main()
