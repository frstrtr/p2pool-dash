#!/bin/bash
# P2Pool-Dash TESTNET start script for testing

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG_FILE="$SCRIPT_DIR/p2pool_testnet.log"

# Kill existing testnet instances
pkill -f "pypy.*run_p2pool.*dash_testnet" 2>/dev/null

# Testnet address (generate one with: dash-cli -testnet getnewaddress)
TESTNET_ADDRESS="yZkx49ksZKSmFK6caVA2dAK61JsQJqceD8"

echo "Starting P2Pool in TESTNET mode..."
echo "Log file: $LOG_FILE"

# Testnet ports:
# - dashd RPC: 19998 (default testnet)
# - dashd P2P: 19999 (default testnet)
# - p2pool P2P: 18999 (defined in networks/dash_testnet.py)
# - p2pool stratum: 17903 (defined in networks/dash_testnet.py)

pypy run_p2pool.py --net dash --testnet \
    --dashd-address 127.0.0.1 \
    --dashd-rpc-port 19998 \
    --dashd-p2p-port 19999 \
    -a "$TESTNET_ADDRESS" \
    --give-author 0 \
    --web-static web-static \
    --logfile "$LOG_FILE" \
    "$@" 2>&1 | tee -a "$LOG_FILE"
