#!/bin/bash
# Start P2Pool for Litecoin + Dogecoin Merged Mining (Testnet)

cd "$(dirname "$0")"

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
P2POOL_ADDRESS="mwjUmhAW68zCtgZpW5b1xD5g7MZew6xPV4"  # Replace with your Litecoin testnet address
P2POOL_FEE="0.5"                                     # 0.5% pool fee
NET="litecoin_testnet"

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
    --bitcoind-rpc-username $LTC_RPC_USER \
    --bitcoind-rpc-password $LTC_RPC_PASS \
    --bitcoind-address $LTC_RPC_HOST \
    --bitcoind-rpc-port $LTC_RPC_PORT \
    --worker-port 9327 \
    --p2pool-port 9338 \
    --max-conns 40 \
    --outgoing-conns 8 \
    "$@"
