#!/bin/bash
# Monitor dashd reindex progress
# Usage: ./reindex_monitor.sh [interval_seconds]

INTERVAL=${1:-10}

while true; do
    INFO=$(dash-cli getblockchaininfo 2>/dev/null)
    if [ $? -ne 0 ]; then
        LAST=$(tail -1 ~/.dashcore/debug.log 2>/dev/null | grep -oP 'progress=\K[0-9.]+')
        printf "\r\033[K[%s] RPC unavailable — log progress: %s" "$(date +%H:%M:%S)" "${LAST:-waiting...}"
    else
        BLOCKS=$(echo "$INFO" | grep -oP '"blocks"\s*:\s*\K[0-9]+')
        HEADERS=$(echo "$INFO" | grep -oP '"headers"\s*:\s*\K[0-9]+')
        PROGRESS=$(echo "$INFO" | grep -oP '"verificationprogress"\s*:\s*\K[0-9.]+')
        PCT=$(awk "BEGIN {printf \"%.4f\", $PROGRESS * 100}")
        printf "\r\033[K[%s] %s / %s blocks  —  %s%%" "$(date +%H:%M:%S)" "$BLOCKS" "$HEADERS" "$PCT"
        if [ "$BLOCKS" = "$HEADERS" ] && [ "$HEADERS" != "0" ]; then
            echo ""
            echo "Reindex complete!"
            break
        fi
    fi
    sleep "$INTERVAL"
done
