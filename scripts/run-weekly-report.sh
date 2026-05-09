#!/bin/bash
# Hermes Event-Driven Weekly Report Runner
# Replaces the original run-weekly-report.sh with event emission
#
# Schedule: 0 9 * * 1 (Mondays 9am)
# Emits Hermes events after each step for agent processing.

set -e

REPO_DIR="/Users/gho/Documents/affiliation-sites"
CREDENTIALS="$REPO_DIR/gsc-credentials.json"
SPREADSHEET_ID="1Pk29UDak7uEDrHiFpinz1uO-eKQJzijEi3fokc2goYQ"
REPORTS_DIR="$REPO_DIR/reports"
LOG_OUT="$REPORTS_DIR/gsc-monitor-out.log"
LOG_ERR="$REPORTS_DIR/gsc-monitor-err.log"
PYTHON="/opt/anaconda3/bin/python3"

# Hermes event bus directory
export HERMES_EVENTS_DIR="${HERMES_EVENTS_DIR:-$HOME/hermes-events/inbox}"

# Ensure event bus directory exists
mkdir -p "$HERMES_EVENTS_DIR"

cd "$REPO_DIR"

echo "=== $(date) — Starting Hermes weekly run ===" >> "$LOG_OUT"

# ─── 1. Content Decay Watcher (original + agent) ───
echo "$(date) — [1/4] Content Decay Watcher" >> "$LOG_OUT"
$PYTHON "$REPO_DIR/scripts/content-decay-watcher.py" \
    >> "$LOG_OUT" 2>> "$LOG_ERR"

# Emit Hermes events for decay findings
$PYTHON "$REPO_DIR/scripts/content_decay_agent.py" \
    --emit-events \
    --inbox "$HERMES_EVENTS_DIR" \
    >> "$LOG_OUT" 2>> "$LOG_ERR"

# ─── 2. Site Health Monitor (original + agent) ───
echo "$(date) — [2/4] Site Health Monitor" >> "$LOG_OUT"
$PYTHON "$REPO_DIR/scripts/site-health-monitor.py" \
    >> "$LOG_OUT" 2>> "$LOG_ERR"

# Emit Hermes events for health findings
$PYTHON "$REPO_DIR/scripts/site_health_agent.py" \
    --emit-events \
    --inbox "$HERMES_EVENTS_DIR" \
    >> "$LOG_OUT" 2>> "$LOG_ERR"

# ─── 3. GSC Weekly Report (original + agent) ───
echo "$(date) — [3/4] GSC Weekly Report" >> "$LOG_OUT"
$PYTHON "$REPO_DIR/scripts/gsc_weekly_report.py" \
    --credentials "$CREDENTIALS" \
    --days 28 \
    >> "$LOG_OUT" 2>> "$LOG_ERR"

# Emit Hermes events for GSC analytics
$PYTHON "$REPO_DIR/scripts/gsc_weekly_agent.py" \
    --emit-events \
    --inbox "$HERMES_EVENTS_DIR" \
    >> "$LOG_OUT" 2>> "$LOG_ERR"

# ─── 4. Update Google Sheets Dashboard (original + agent) ───
echo "$(date) — [4/4] Dashboard Sync" >> "$LOG_OUT"
SPREADSHEET_ID="$SPREADSHEET_ID" $PYTHON "$REPO_DIR/scripts/update_sheets_dashboard.py" \
    >> "$LOG_OUT" 2>> "$LOG_ERR"

# Emit Hermes events for dashboard sync
$PYTHON "$REPO_DIR/scripts/dashboard_sync_agent.py" \
    --emit-events \
    --inbox "$HERMES_EVENTS_DIR" \
    >> "$LOG_OUT" 2>> "$LOG_ERR"

# ─── 5. Route events to interested agents ───
echo "$(date) — [5/5] Routing events to agents" >> "$LOG_OUT"
$PYTHON "$HOME/.hermes/skills/hermes-agents/hermes-event-bus/scripts/event_router.py" \
    --inbox "$HERMES_EVENTS_DIR" \
    --route-all \
    >> "$LOG_OUT" 2>> "$LOG_ERR" || true

echo "=== $(date) — Hermes weekly run complete ===" >> "$LOG_OUT"
