#!/bin/bash
# Weekly Report Runner
# Runs content decay watcher, site health monitor, and GSC weekly report

set -e

REPO_DIR="/Users/gho/Documents/affiliation-sites"
CREDENTIALS="$REPO_DIR/gsc-credentials.json"
SPREADSHEET_ID="1Pk29UDak7uEDrHiFpinz1uO-eKQJzijEi3fokc2goYQ"
REPORTS_DIR="$REPO_DIR/reports"
LOG_OUT="$REPORTS_DIR/gsc-monitor-out.log"
LOG_ERR="$REPORTS_DIR/gsc-monitor-err.log"
PYTHON="/opt/anaconda3/bin/python3"

cd "$REPO_DIR"

echo "=== $(date) — Starting weekly run ===" >> "$LOG_OUT"

# ─── 1. Content Decay Watcher ───
$PYTHON "$REPO_DIR/scripts/content-decay-watcher.py" \
    >> "$LOG_OUT" 2>> "$LOG_ERR"

# ─── 2. Site Health Monitor ───
$PYTHON "$REPO_DIR/scripts/site-health-monitor.py" \
    >> "$LOG_OUT" 2>> "$LOG_ERR"

# ─── 3. GSC Weekly Report (Markdown + macOS notification) ───
$PYTHON "$REPO_DIR/scripts/gsc_weekly_report.py" \
    --credentials "$CREDENTIALS" \
    --days 28 \
    >> "$LOG_OUT" 2>> "$LOG_ERR"

# ─── 4. Update Google Sheets Dashboard ───
SPREADSHEET_ID="$SPREADSHEET_ID" $PYTHON "$REPO_DIR/scripts/update_sheets_dashboard.py" \
    >> "$LOG_OUT" 2>> "$LOG_ERR"

echo "=== $(date) — Done ===" >> "$LOG_OUT"
