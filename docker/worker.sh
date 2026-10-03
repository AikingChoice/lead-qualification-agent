#!/bin/sh
set -eu

interval="${POLL_INTERVAL_SECONDS:-300}"
max_entries="${MAX_ENTRIES_PER_POLL:-20}"
daily_limit="${DAILY_MODEL_CALL_LIMIT:-200}"

while :; do
    echo "[$(date -u +%FT%TZ)] Starting RSS poll"
    if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
        if rss-surveillance run --db "$RSS_AGENT_DB" --max-entries "$max_entries" --daily-model-limit "$daily_limit" --deliver-notifications; then
            result=0
        else
            result=$?
        fi
    else
        if rss-surveillance run --db "$RSS_AGENT_DB" --max-entries "$max_entries" --daily-model-limit "$daily_limit"; then
            result=0
        else
            result=$?
        fi
    fi
    if [ "$result" -ne 0 ]; then
        echo "[$(date -u +%FT%TZ)] Poll failed with exit code $result" >&2
    fi
    sleep "$interval"
done
