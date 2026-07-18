#!/bin/bash
# velocity.sh
# Runs the Velocity tracker for each configured ETF, then commits and pushes
# the report branch to GitHub. GitHub Pages serves the report branch at
# yourusername.github.io/velocity — the hub index.html links every ETF.
#
# Layout:
#   vecocity/main/velocity.sh   ← this file (+ velocity.py, data/, .env)
#   velocity/report/           ← report git checkout, on the `report` branch

set -euo pipefail

SCRIPT_DIR="$HOME/velocity/main"
REPORT="$HOME/velocity/report"          # single repo, checked out on report branch

# Keep this in sync with ETF_CONFIG at the top of velocity.py.
ETFS=(MTUM QUAL DYNF)

LOG="[velocity $(date '+%Y-%m-%d %H:%M')]"
echo "$LOG Starting"

# Load API key from .env file if present
# Format: GROQ_API_KEY=gsk_...
[ -f "$SCRIPT_DIR/.env" ] && { set -a; source "$SCRIPT_DIR/.env"; set +a; }

# Run tracker for each ETF — DB defaults to src/data/{ETF}.db next to this
# script, report output goes to $REPORT/{ETF}/ with a shared hub index.html
# at $REPORT/index.html
for etf in "${ETFS[@]}"; do
    echo "$LOG Running $etf"
    python3 "$SCRIPT_DIR/velocity.py" \
        --etf "$etf" \
        --report-dir "$REPORT"
done

echo "$LOG Tracker done"

# Commit and push report branch
cd "$REPORT"
git add .
git diff --staged --quiet && { echo "$LOG No changes — skipping push"; exit 0; }
git commit -m "Velocity $(date '+%Y-%m-%d')"
git push origin report

echo "$LOG Pushed - GitHub Pages updating"
