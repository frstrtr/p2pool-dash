#!/bin/bash
# Monitor Litecoin and Dogecoin testnet sync progress

echo "=== Node Sync Status Monitor ==="
echo "Host: 192.168.80.182"
echo "Time: $(date)"
echo ""

# Litecoin Testnet
echo "━━━ LITECOIN TESTNET ━━━"
LTC_DATA=$(curl -s --user litecoinrpc:LTC_testnet_pass_2024_secure \
  --data-binary '{"jsonrpc":"1.0","method":"getblockchaininfo","id":"1"}' \
  http://192.168.80.182:19332/)

LTC_BLOCKS=$(echo $LTC_DATA | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['blocks'])" 2>/dev/null)
LTC_HEADERS=$(echo $LTC_DATA | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['headers'])" 2>/dev/null)
LTC_PROGRESS=$(echo $LTC_DATA | python3 -c "import sys,json; print('{:.2%}'.format(json.load(sys.stdin)['result']['verificationprogress']))" 2>/dev/null)

echo "  Blocks:   $LTC_BLOCKS"
echo "  Headers:  $LTC_HEADERS"
echo "  Progress: $LTC_PROGRESS"
echo "  Status:   Syncing..."
echo ""

# Dogecoin Testnet
echo "━━━ DOGECOIN TESTNET ━━━"
DOGE_DATA=$(curl -s --user dogeuser:dogepass123 \
  --data-binary '{"jsonrpc":"1.0","method":"getblockchaininfo","id":"1"}' \
  http://192.168.80.182:44555/)

if echo "$DOGE_DATA" | grep -q '"error":null'; then
  DOGE_BLOCKS=$(echo $DOGE_DATA | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['blocks'])" 2>/dev/null)
  DOGE_HEADERS=$(echo $DOGE_DATA | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['headers'])" 2>/dev/null)
  DOGE_PROGRESS=$(echo $DOGE_DATA | python3 -c "import sys,json; print('{:.2%}'.format(json.load(sys.stdin)['result']['verificationprogress']))" 2>/dev/null)
  
  echo "  Blocks:   $DOGE_BLOCKS"
  echo "  Headers:  $DOGE_HEADERS"
  echo "  Progress: $DOGE_PROGRESS"
  
  if [ "$DOGE_BLOCKS" = "$DOGE_HEADERS" ]; then
    echo "  Status:   ✓ Fully synced!"
  else
    echo "  Status:   Syncing..."
  fi
else
  DOGE_ERROR=$(echo $DOGE_DATA | python3 -c "import sys,json; print(json.load(sys.stdin)['error']['message'])" 2>/dev/null)
  echo "  Status:   $DOGE_ERROR"
fi
echo ""

# Network Ports
echo "━━━ NETWORK PORTS ━━━"
ssh 192.168.80.182 "ss -tln | grep -E '19332|44555'" | while read line; do
  echo "  $line"
done
echo ""

# Disk Usage
echo "━━━ DISK USAGE ━━━"
ssh 192.168.80.182 "du -sh ~/.litecoin ~/.dogecoin 2>/dev/null" | while read line; do
  echo "  $line"
done
echo ""

# Estimated sync times
if [ ! -z "$LTC_BLOCKS" ] && [ "$LTC_BLOCKS" != "null" ]; then
  if [ "$LTC_BLOCKS" -lt "$LTC_HEADERS" ]; then
    REMAINING=$((LTC_HEADERS - LTC_BLOCKS))
    echo "━━━ LITECOIN SYNC ESTIMATE ━━━"
    echo "  Remaining blocks: $REMAINING"
    echo "  At ~100 blocks/sec: ~$((REMAINING / 100 / 60)) minutes"
    echo "  At ~10 blocks/sec:  ~$((REMAINING / 10 / 60)) minutes"
    echo ""
  fi
fi

echo "Run this script again to monitor progress."
echo "For continuous monitoring: watch -n 30 $0"
