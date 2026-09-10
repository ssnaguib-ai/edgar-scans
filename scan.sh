#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate

# SEC requires a real name and email in the User-Agent or it returns 403. It
# lives in scan.config, which is gitignored, so it is not published with the
# scan results.
if [ ! -f scan.config ]; then
    echo "Missing scan.config — copy scan.config.example to scan.config and set USER_AGENT." >&2
    exit 1
fi
source ./scan.config
if [ -z "$USER_AGENT" ]; then
    echo "scan.config does not set USER_AGENT." >&2
    exit 1
fi

START=$(date -v-8d +%Y-%m-%d)
END=$(date -v-1d +%Y-%m-%d)
OUT="live_$(date +%Y-%m-%d).csv"
echo "Scanning $START to $END -> $OUT"
python3 edgar_insider.py --start "$START" --end "$END" \
    --user-agent "$USER_AGENT" --out "$OUT"
STATUS=$?

# --- publish -------------------------------------------------------------
# Only publish a scan that actually succeeded. A non-zero exit means the CSV is
# missing or half-written, and committing it would overwrite a good result on
# the remote with a broken one.
if [ $STATUS -ne 0 ]; then
    echo "Scan failed (exit $STATUS) — not publishing." >&2
    exit $STATUS
fi
if [ ! -s "$OUT" ]; then
    echo "Scan produced no output at $OUT — not publishing." >&2
    exit 1
fi

REMOTE=$(git remote get-url origin 2>/dev/null)
if [ -z "$REMOTE" ]; then
    echo "Wrote $OUT. No git remote 'origin' — skipping publish." >&2
    exit 0
fi

# Accept either remote form: git@github.com:owner/repo.git or
# https://github.com/owner/repo.git
SLUG=$(echo "$REMOTE" | sed -E 's#^(git@github\.com:|https://github\.com/)##; s#\.git$##')
BRANCH=$(git branch --show-current)

git add "$OUT"
# An unchanged re-run on the same date stages nothing; that is not an error,
# the file is already published from the earlier run.
if git diff --cached --quiet; then
    echo "No change to $OUT since last run — already published."
else
    git commit -q -m "Scan $START to $END" || { echo "Commit failed." >&2; exit 1; }
fi

if ! git push -q origin "$BRANCH"; then
    echo "Push failed — $OUT is committed locally but not published." >&2
    exit 1
fi

# raw.githubusercontent.com caches a branch path for ~5 minutes, so a re-run
# on the same date may serve the previous content briefly.
echo
echo "Published: https://raw.githubusercontent.com/$SLUG/$BRANCH/$OUT"
