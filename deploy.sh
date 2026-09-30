#!/usr/bin/env bash
# ===========================================================================
# tvbox-source-monitor - one-click GitHub deployment (spec §39-10)
#
#   ./deploy.sh                          # private repo named after this folder
#   ./deploy.sh --repo tvbox-source-monitor --public
#   ./deploy.sh --dry-run                # check prerequisites, change nothing
#
# What it does:
#   1. verifies git / gh / gh auth, and the `workflow` scope
#   2. creates .env from .env.example (never committed)
#   3. git init -b main + first commit (the project gets its OWN repository)
#   4. gh repo create --source=. --push
#   5. sets the GH_TOKEN and ALERT_GITHUB_REPO repository secrets
#   6. prints the two manual steps GitHub does not allow a token to do for you
# ===========================================================================
set -euo pipefail

REPO_NAME=""
VISIBILITY="--private"
DRY_RUN=0
SKIP_SECRETS=0
BRANCH="main"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)       REPO_NAME="${2:-}"; shift 2 ;;
    --public)     VISIBILITY="--public"; shift ;;
    --private)    VISIBILITY="--private"; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --no-secrets) SKIP_SECRETS=1; shift ;;
    -h|--help)    sed -n '2,18p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '    \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

# --- 0. project sanity -----------------------------------------------------
[ -f config/app.yaml ] || die "run this from the tvbox-source-monitor project root"
[ "$(basename "$PROJECT_DIR")" = "tvbox-source-monitor" ] || warn "folder name is $(basename "$PROJECT_DIR"), expected tvbox-source-monitor"
[ -n "$REPO_NAME" ] || REPO_NAME="$(basename "$PROJECT_DIR")"
REPO_NAME="$(printf '%s' "$REPO_NAME" | tr '[:upper:] ' '[:lower:]-')"

# --- 1. prerequisites ------------------------------------------------------
say "1/6  checking prerequisites"
command -v git >/dev/null 2>&1 || die "git is not installed"
ok "git $(git --version | awk '{print $3}')"
command -v gh >/dev/null 2>&1 || die "gh (GitHub CLI) is not installed - https://cli.github.com"
ok "gh $(gh --version | awk 'NR==1{print $3}')"
gh auth status >/dev/null 2>&1 || die "gh is not logged in - run: gh auth login"
GH_USER="$(gh api user --jq .login)"
ok "logged in as $GH_USER"

# `workflow` scope is required: pushing .github/workflows/* is rejected without it
if gh auth status 2>&1 | grep -q "'workflow'"; then
  ok "token has the 'workflow' scope"
else
  warn "token is missing the 'workflow' scope - .github/workflows/* cannot be pushed"
  if [ "$DRY_RUN" = "1" ]; then
    warn "dry-run: would run  gh auth refresh -h github.com -s workflow"
  else
    echo "    running: gh auth refresh -h github.com -s workflow"
    echo "    -> open the URL it prints, enter the code, then come back here"
    gh auth refresh -h github.com -s workflow
  fi
fi

# --- 2. .env --------------------------------------------------------------
say "2/6  preparing .env"
if [ -f .env ]; then
  ok ".env already exists - left untouched"
elif [ "$DRY_RUN" = "1" ]; then
  warn "dry-run: would create .env from .env.example"
else
  cp .env.example .env
  ok "created .env from .env.example (it is gitignored)"
  warn "optional: put a PAT in .env as GH_TOKEN to enable the GitHub search adapter"
fi

# --- 3. local git repo ----------------------------------------------------
say "3/6  initialising the local repository"
if [ -d .git ]; then
  ok "already a git repository"
elif [ "$DRY_RUN" = "1" ]; then
  warn "dry-run: would run  git init -b $BRANCH"
else
  git init -q -b "$BRANCH"
  ok "git init -b $BRANCH"
fi

# a fresh deployment must not ship a database that still holds local test rows
if [ -f data/monitor.db ] && [ "$DRY_RUN" = "0" ]; then
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY'
import sqlite3, pathlib
p = pathlib.Path("data/monitor.db")
try:
    c = sqlite3.connect(p)
    rows = c.execute("select count(*) from sources").fetchone()[0]
    c.close()
except Exception:
    rows = 0
if rows == 0:
    for suffix in ("", "-wal", "-shm"):
        f = pathlib.Path(str(p) + suffix)
        if f.exists():
            f.unlink()
    print("    ok   removed the empty local monitor.db so the first run starts clean")
else:
    print(f"    warn local monitor.db holds {rows} source(s) - it will be committed as is")
PY
  fi
fi

if [ "$DRY_RUN" = "1" ]; then
  warn "dry-run: would set the git identity, add dist/.gitkeep, and commit"
else
  git rev-parse --verify HEAD >/dev/null 2>&1 && HAVE_COMMIT=1 || HAVE_COMMIT=0
  if [ "$HAVE_COMMIT" = "0" ]; then
    git config user.name  >/dev/null 2>&1 || git config user.name  "$GH_USER"
    git config user.email >/dev/null 2>&1 || git config user.email "$GH_USER@users.noreply.github.com"
    ok "git identity: $(git config user.name) <$(git config user.email)>"
  fi

  [ -d dist ] || mkdir -p dist
  [ -f dist/.gitkeep ] || : > dist/.gitkeep

  git add -A
  if git diff --cached --quiet && [ "$HAVE_COMMIT" = "1" ]; then
    ok "nothing new to commit"
  elif [ "$HAVE_COMMIT" = "1" ]; then
    git commit -q -m "chore: update tvbox-source-monitor"
    ok "committed the working tree"
  else
    git commit -q -m "feat: tvbox-source-monitor V1.0"
    ok "created the first commit"
  fi
fi

# --- 4. remote repository -------------------------------------------------
say "4/6  creating the GitHub repository"
if git remote get-url origin >/dev/null 2>&1; then
  ok "origin already set to $(git remote get-url origin)"
else
  if [ "$DRY_RUN" = "1" ]; then
    warn "dry-run: would run  gh repo create $GH_USER/$REPO_NAME $VISIBILITY --source=. --remote=origin --push"
  else
    gh repo create "$GH_USER/$REPO_NAME" $VISIBILITY --source=. --remote=origin --push
    ok "created and pushed $GH_USER/$REPO_NAME"
  fi
fi

if [ "$DRY_RUN" = "0" ]; then
  git push -u origin "$BRANCH" 2>/dev/null || git push origin "$BRANCH"
fi

# --- 5. repository secrets ------------------------------------------------
say "5/6  setting repository secrets"
if [ "$SKIP_SECRETS" = "1" ]; then
  warn "--no-secrets: skipped"
elif [ "$DRY_RUN" = "1" ]; then
  warn "dry-run: would set GH_TOKEN and ALERT_GITHUB_REPO"
else
  if gh secret set GH_TOKEN --repo "$GH_USER/$REPO_NAME" --body "$(gh auth token)" >/dev/null 2>&1; then
    ok "GH_TOKEN set (enables the GitHub source-discovery adapter)"
  else
    warn "could not set GH_TOKEN - the adapter will be skipped, everything else still works"
  fi
  if gh secret set ALERT_GITHUB_REPO --repo "$GH_USER/$REPO_NAME" --body "$GH_USER/$REPO_NAME" >/dev/null 2>&1; then
    ok "ALERT_GITHUB_REPO set to $GH_USER/$REPO_NAME (alerts become Issues)"
  fi
fi

# --- 6. what a token cannot do -------------------------------------------
say "6/6  two manual steps left (GitHub forbids a token from doing these)"
cat <<TXT
    (a) Enable Pages
        Settings -> Pages -> Build and deployment -> Source = "GitHub Actions"
        Do NOT pick "Deploy from a branch": dist/ is not at the branch root.

    (b) Turn on the schedules
        Actions tab -> allow workflows to run (a fresh private repo asks once).

    Then run the first build:
        Actions -> nightly-build -> Run workflow
        (or locally:  make pipeline && git push)

    Your permanent 影视仓 config URL:
        https://$GH_USER.github.io/$REPO_NAME/tvbox.json

    Nothing is published until the safety valve sees at least
    output.min_sources (=3) sources, so an early 404 or {} is expected.
    Add sources to data/candidates.json, or rely on the discovery adapter.
TXT

say "done"
printf '    repository : https://github.com/%s/%s\n' "$GH_USER" "$REPO_NAME"
printf '    pages      : https://%s.github.io/%s/dashboard/\n' "$GH_USER" "$REPO_NAME"
printf '    next       : edit data/candidates.json, commit, push\n\n'
