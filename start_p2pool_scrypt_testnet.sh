#!/bin/bash
# Start P2Pool for Litecoin + Dogecoin Merged Mining (Testnet)

cd "$(dirname "$0")"

# Set up PyPy and OpenSSL 1.1.1 environment
export PATH="$HOME/.local/pypy2.7-v7.3.20-linux64/bin:$PATH"
export LD_LIBRARY_PATH="$HOME/.local/openssl-1.1.1/lib:$LD_LIBRARY_PATH"

# Litecoin Testnet RPC credentials
LTC_RPC_USER="litecoinrpc"
LTC_RPC_PASS="LTC_testnet_pass_2024_secure"
LTC_RPC_HOST="192.168.80.182"
LTC_RPC_PORT="19332"

# Dogecoin Testnet RPC credentials
DOGE_RPC_USER="dogeuser"
DOGE_RPC_PASS="dogepass123"
DOGE_RPC_HOST="192.168.80.182"
DOGE_RPC_PORT="44555"

# P2Pool configuration
# Litecoin testnet addresses (choose one):
# Legacy: mm3suEPoj1WnhYuRTdoM6dfEXQvZEyuu9h
# P2SH-Segwit: QcVudrUyKGwqjk4KWadnXfbHgnMVHB1Lif  
# Bech32 (native segwit): tltc1qpkcpgwl24flh35mknlsf374x8ypqv7de6esjh4
P2POOL_ADDRESS="tltc1qpkcpgwl24flh35mknlsf374x8ypqv7de6esjh4"  # Using native segwit (best for Litecoin with MWEB)
P2POOL_FEE="0.5"                                                 # 0.5% pool fee
NET="litecoin_testnet"

# Dogecoin testnet address for merged mining: nmkmeRtJu3wzg8THQYpnaUpTUtqKP15zRB

echo "=== Starting P2Pool Scrypt (Litecoin + Dogecoin Testnet) ==="
echo "Network: $NET"
echo "Payout Address: $P2POOL_ADDRESS"
echo "Pool Fee: $P2POOL_FEE%"
echo ""

# Start P2Pool
pypy run_p2pool.py \
    --net $NET \
    --address $P2POOL_ADDRESS \
    --fee $P2POOL_FEE \
    --coind-address $LTC_RPC_HOST \
    --coind-rpc-port $LTC_RPC_PORT \
    --worker-port 9327 \
    --p2pool-port 9338 \
    --max-conns 40 \
    --outgoing-conns 8 \
    $LTC_RPC_USER $LTC_RPC_PASS \
    "$@"
