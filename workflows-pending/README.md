# Pending workflows

These two GitHub Actions workflows could not be pushed to `.github/workflows/`
because the automation token lacks the `workflows` permission. Activate them
once by either:

1. Running `bash scripts/install_workflows.sh` from a local clone using your
   own GitHub credentials, **or**
2. In the GitHub web UI: Add file → Create new file →
   `.github/workflows/weekly-pull.yml` → paste the contents of
   `workflows-pending/weekly-pull.yml` → commit. Repeat for
   `push-approved.yml`.
