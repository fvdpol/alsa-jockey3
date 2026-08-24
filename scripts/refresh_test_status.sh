#!/usr/bin/env bash
#
# simple bash script that will refresh the docs/test_results.md in this repo
# and commit to git if no staged changes are found in git.
#
# execute from the root of the git repository
# intended to be executed periodically from cron on the alsa-dev server
#
set -euo pipefail


# Configuration
DOC_PATH="docs/test_status.md"
COMMIT_MSG="docs: update test status [auto]"


# Guard: Ensure execution from repository root
if [[ ! -f "scripts/refresh_test_status.sh" || ! -d ".git" ]]; then
  echo "Error: This script must be executed from the root of the Git repository." >&2
  echo "Current directory: $(pwd)" >&2
  exit 1
fi


# 1. Assemble the Markdown file
{
  cat << 'EOF'
# Automated Test Results

Snapshot of hardware-in-the-loop coverage, published by hand or scheduled
job — see [test_strategy.md §11](test_strategy.md#11-results-retention-and-reporting) 
for how it is produced.  Reflects committed code only.  Full detail (driver revision,
commit count, age) is in `ledger.py`'s coverage table, not reproduced here.

EOF

  # Run python script to append table
  python3 tests/hw/ledger.py --matrix

  echo -e "\n---\n*Report generated automatically by local CI runner on `hostname --long`*"
} > "$DOC_PATH"


# Ensure the current branch is main
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
if [ "$CURRENT_BRANCH" != "main" ]; then
  echo "Error: You are currently on branch '$CURRENT_BRANCH'." >&2
  echo "Skipping auto-commit to prevent committing to wrong branch." >&2
  exit 1
fi


# 2. Check if there are ALREADY staged changes in the repository
if ! git diff --cached --quiet; then
  echo "Error: Uncommitted staged changes detected. Skipping auto-commit to prevent committing WIP." >&2
  exit 1
fi

# 3. Check if the generated document has actual unstaged modifications
if git diff --quiet -- "$DOC_PATH"; then
  echo "No changes detected in $DOC_PATH. Nothing to commit."
  exit 0
fi

# 4. Stage and commit only the updated markdown document
echo "Changes detected in $DOC_PATH. Committing..."
git add "$DOC_PATH"
git commit -m "$COMMIT_MSG"
git push