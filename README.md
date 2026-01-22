Renovate -> Jira Agent v4: supports per-repo .github/renovate-jira.yml

Install Requirements:
====================
pip install -r requirements.txt
python -m pip install -r requirements.txt

Env Variables Required:
=======================
export MODE=run, or dry-run
export REPO_LIST_FILE=./repo-list.txt
export GITHUB_TOKEN=<YOUR-GITHUB-FINE-GRAINED-TOKEN>
export JIRA_BASE_URL=https://tools.hmcts.net/jira
export JIRA_USER_EMAIL=<YOUR_JIRA_LOGIN_EMAIL_ADDRESS>
export JIRA_PAT=<YOUR_JIRA_PAT>
export JIRA_API_VERSION=2


Testing:

curl -H "Authorization: Bearer $JIRA_PAT" \
  -H "Accept: application/json" \
  https://tools.hmcts.net/jira/rest/api/2/myself

LOCAL_CONFIG_PATH=.github/renovate-jira.yml VERBOSE=1 python main.py

========================


Renovate → Jira automation has all the core agent properties:

✅ It has a goal

“For Renovate PRs that meet policy, ensure Jira tracking exists.”

✅ It observes state

Reads GitHub PRs

Reads PR labels, titles, bodies

Reads per-repo policy (renovate-jira.yml)

Reads existing PR comments (dedupe)

✅ It makes decisions

Classifies PRs (security / major / critical)

Applies repo-specific policy

Decides whether to act

✅ It takes actions

Creates Jira issues

Comments on PRs

Adds labels

Skips safely when rules say “no”

✅ It runs autonomously

Triggered by GitHub Actions on schedule or manually

No human clicking buttons

✅ It’s auditable

Logs every action

PR comments link Jira tickets

Config is versioned in Git

