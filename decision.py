import re
from typing import Tuple, List, Optional, Dict, Any

DEFAULT_CRITICAL = {"openssl", "spring-boot", "spring-security", "log4j", "jsonwebtoken", "mysql-connector", "postgresql", "hibernate"}

def mentions_cve(text: str) -> bool:
    return bool(re.search(r"\bCVE-\d{4}-\d{4,}\b", text, re.IGNORECASE))

def is_major_bump(title: str, body: str) -> bool:
    text = (title + " " + body).lower()
    if re.search(r"\bmajor\b|\bbreaking\b|\bmigration\b", text):
        return True
    if re.search(r"\bupdate\s*=\s*major\b", text):
        return True
    # Match "to 3." or "to v3" or "to 3" in title.
    m = re.search(r"\bto\s+v?([0-9]+)(?:\.\d+)?\b", title.lower())
    if m and int(m.group(1)) > 1:
        return True
    # Match Renovate summary style "2.9.3 -> 3.2.3" in title/body.
    m = re.search(r"\b([0-9]+)(?:\.[0-9]+){0,2}\s*->\s*([0-9]+)(?:\.[0-9]+){0,2}\b", text)
    if m and int(m.group(1)) != int(m.group(2)):
        return True
    return False

def touches_critical_dependency(text: str, critical_deps: Optional[List[str]]) -> bool:
    txt = text.lower()
    deps = set(d.lower() for d in (critical_deps or []))
    deps = deps.union(DEFAULT_CRITICAL)
    for d in deps:
        if d in txt:
            return True
    return False

def needs_jira(title: str, body: str, labels: List[str], critical_deps: Optional[List[str]] = None, create_jira_for: Optional[Dict[str,bool]] = None) -> Tuple[Optional[str], Optional[str]]:
    text = (title + " " + body).lower()
    labels_set = {l.lower() for l in labels}
    create_cfg = create_jira_for or {"security": True, "major": True, "critical-dep": False}

    if (mentions_cve(title) or mentions_cve(body) or "security" in labels_set) and create_cfg.get("security", True):
        return ("security", "Security / CVE detected")

    if is_major_bump(title, body) and create_cfg.get("major", True):
        return ("major", "Semver-major or breaking change detected")

    if touches_critical_dependency(text, critical_deps) and create_cfg.get("critical-dep", False):
        return ("critical-dep", "Updates a critical dependency")

    return (None, None)
