#!/usr/bin/env bash
# One-time workflow installer.
#
# WHY THIS EXISTS: the GitHub App token used to create this repo does not have
# the `workflows` permission, so .github/workflows/*.yml could not be pushed
# directly. The workflow files live in workflows-pending/ instead.
#
# Run this once from a clone made with YOUR own credentials (they have full
# permissions), or simply move the two files via the GitHub web UI
# (Add file → Create new file → paste contents).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .github/workflows
cp workflows-pending/*.yml .github/workflows/
git add .github/workflows workflows-pending
git rm -r --cached workflows-pending >/dev/null 2>&1 || true
rm -rf workflows-pending
git add -A
git commit -m "ci: activate GitHub Actions workflows"
git push origin main
echo "✅ Workflows installed. Check the Actions tab."
