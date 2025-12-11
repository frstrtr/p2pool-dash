#!/bin/bash
# P2Pool-Dash start script with auto-kill of existing instances
# Supports: foreground mode, background mode (--daemon), and SSH launching

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG_FILE="$SCRIPT_DIR/p2pool.log"
PID_FILE="$SCRIPT_DIR/p2pool.pid"

# Function to kill existing instances
kill_existing() {
    echo "Checking for existing P2Pool instances..."
    if pgrep -f "pypy.*run_p2pool" > /dev/null; then
        echo "Killing existing P2Pool instance(s)..."
        pkill -9 -f "pypy.*run_p2pool"
        sleep 2
    fi
    # Clean up stale PID file
    [ -f "$PID_FILE" ] && rm -f "$PID_FILE"
}

# Function to rotate log if too large (>10MB)
rotate_log() {
    if [ -f "$LOG_FILE" ]; then
        LOG_SIZE=$(stat -c%s "$LOG_FILE" 2>/dev/null || stat -f%z "$LOG_FILE" 2>/dev/null || echo 0)
        if [ "$LOG_SIZE" -gt 10485760 ]; then
            mv "$LOG_FILE" "${LOG_FILE}.old"
            echo "Rotated old log file"
        fi
    fi
}

# Function to start p2pool
start_p2pool() {
    pypy run_p2pool.py --net dash \
        --dashd-address 127.0.0.1 \
        --dashd-rpc-port 9998 \
        -a XrTwUgw3ikobuXdLKvvSUjL9JpuPs9uqL7 \
        "$@"
}

# Parse arguments
DAEMON_MODE=false
for arg in "$@"; do
    case $arg in
        --daemon|-d)
            DAEMON_MODE=true
            shift
            ;;
        --kill|-k)
            kill_existing
            echo "P2Pool stopped."
            exit 0
            ;;
        --status|-s)
            if pgrep -f "pypy.*run_p2pool" > /dev/null; then
                echo "P2Pool is running (PID: $(pgrep -f 'pypy.*run_p2pool'))"
                exit 0
            else
                echo "P2Pool is not running"
                exit 1
            fi
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo "Options:"
            echo "  --daemon, -d    Run in background (daemon mode)"
            echo "  --kill, -k      Kill existing P2Pool instances"
            echo "  --status, -s    Check if P2Pool is running"
            echo "  --help, -h      Show this help"
            exit 0
            ;;
    esac
done

# Kill existing instances
kill_existing

# Rotate log if needed
rotate_log

if [ "$DAEMON_MODE" = true ]; then
    # Background/daemon mode - suitable for SSH launching
    echo "Starting P2Pool in daemon mode (logging to $LOG_FILE)..."
    nohup bash -c "cd '$SCRIPT_DIR' && pypy run_p2pool.py --net dash \
        --dashd-address 127.0.0.1 \
        --dashd-rpc-port 9998 \
        -a XrTwUgw3ikobuXdLKvvSUjL9JpuPs9uqL7 \
        >> '$LOG_FILE' 2>&1" > /dev/null 2>&1 &
    disown
    
    # Save PID
    echo $! > "$PID_FILE"
    sleep 2
    
    # Verify it started
    if pgrep -f "pypy.*run_p2pool" > /dev/null; then
        echo "P2Pool started successfully (PID: $(pgrep -f 'pypy.*run_p2pool' | head -1))"
        echo "Log file: $LOG_FILE"
        echo "Use 'tail -f $LOG_FILE' to follow logs"
        exit 0
    else
        echo "ERROR: P2Pool failed to start. Check $LOG_FILE for details."
        exit 1
    fi
else
    # Foreground mode - for interactive use or systemd
    echo "Starting P2Pool in foreground mode (logging to $LOG_FILE)..."
    start_p2pool 2>&1 | tee -a "$LOG_FILE"
fi
