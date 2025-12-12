#!/bin/bash
# P2Pool-Dash TESTNET start script with restart support

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG_FILE="$SCRIPT_DIR/p2pool_testnet.log"
PID_FILE="$SCRIPT_DIR/p2pool_testnet.pid"

# Testnet address (generate one with: dash-cli -testnet getnewaddress)
TESTNET_ADDRESS="yZkx49ksZKSmFK6caVA2dAK61JsQJqceD8"

# Function to kill existing testnet instances
kill_existing() {
    echo "Checking for existing P2Pool testnet instances..."
    if pgrep -f "pypy.*run_p2pool.*testnet" > /dev/null; then
        echo "Killing existing P2Pool testnet instance(s)..."
        pkill -9 -f "pypy.*run_p2pool.*testnet"
        sleep 2
    fi
    [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
}

# Parse arguments
case "$1" in
    restart)
        kill_existing
        echo "Starting P2Pool in TESTNET daemon mode..."
        echo "Log file: $LOG_FILE"
        
        nohup bash -c "cd '$SCRIPT_DIR' && pypy run_p2pool.py --net dash --testnet \
            --dashd-address 127.0.0.1 \
            --dashd-rpc-port 19998 \
            --dashd-p2p-port 19999 \
            -a $TESTNET_ADDRESS \
            --give-author 0 \
            --web-static web-static \
            >> '$LOG_FILE' 2>&1" > /dev/null 2>&1 &
        disown
        echo $! > "$PID_FILE"
        sleep 3
        
        if pgrep -f "pypy.*run_p2pool.*testnet" > /dev/null; then
            echo "P2Pool testnet restarted successfully (PID: $(pgrep -f 'pypy.*run_p2pool.*testnet' | head -1))"
            tail -20 "$LOG_FILE"
        else
            echo "ERROR: P2Pool failed to restart. Check $LOG_FILE"
            tail -50 "$LOG_FILE"
            exit 1
        fi
        ;;
    stop)
        kill_existing
        echo "P2Pool testnet stopped."
        ;;
    *)
        # Foreground mode with console output
        kill_existing
        echo "Starting P2Pool in TESTNET foreground mode..."
        echo "Log file: $LOG_FILE"
        
        pypy run_p2pool.py --net dash --testnet \
            --dashd-address 127.0.0.1 \
            --dashd-rpc-port 19998 \
            --dashd-p2p-port 19999 \
            -a "$TESTNET_ADDRESS" \
            --give-author 0 \
            --web-static web-static \
            2>&1 | tee -a "$LOG_FILE"
        ;;
esac
