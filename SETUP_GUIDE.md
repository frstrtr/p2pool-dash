# Litecoin + Dogecoin Merge Mining Setup Guide

This guide explains the complete setup for testing Litecoin + Dogecoin merge mining with p2pool.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                   Merge Mining Setup                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Miners (CPU/ASIC)                                          │
│         ↓                                                   │
│  P2Pool Stratum (port 19327)                                │
│         ↓                                                   │
│  ┌──────────────────┐         ┌──────────────────┐         │
│  │ Litecoin Node    │         │ Dogecoin Node    │         │
│  │ (PARENT CHAIN)   │◄───────►│ (CHILD CHAIN)    │         │
│  │ testnet:19332    │         │ testnet:44555    │         │
│  │ Scrypt PoW       │         │ Scrypt PoW       │         │
│  │ 2.5 min blocks   │         │ 1 min blocks     │         │
│  └──────────────────┘         └──────────────────┘         │
│         ↑                              ↑                    │
│         │                              │                    │
│         └─────────── P2Pool ───────────┘                    │
│              (merge mining logic)                           │
└─────────────────────────────────────────────────────────────┘
```

## Do You Need Both Nodes?

**YES, you need BOTH Litecoin testnet and Dogecoin testnet nodes:**

1. **Litecoin Node (Parent Chain)**
   - This is the PRIMARY chain miners solve
   - Provides work templates via RPC
   - Higher difficulty target
   - Block rewards go to Litecoin addresses

2. **Dogecoin Node (Child Chain)**  
   - Merge-mined alongside Litecoin
   - Provides auxpow templates via RPC
   - Lower difficulty (shares Litecoin's PoW)
   - Coinbase includes Dogecoin auxpow data

3. **P2Pool Server**
   - Connects to BOTH nodes via RPC
   - Constructs combined work
   - Manages share chain
   - Distributes rewards

## Why Both Nodes Are Required

### Merge Mining Process:
1. Miner requests work from P2Pool
2. P2Pool calls `getblocktemplate` on **Litecoin** (parent)
3. P2Pool calls `getblocktemplate` with `auxpow` on **Dogecoin** (child)
4. P2Pool combines both into single work unit:
   - Litecoin block header (80 bytes to hash)
   - Dogecoin auxpow commitment in Litecoin coinbase
   - Merkle proof connecting the two chains
5. Miner solves Litecoin difficulty
6. Solution is valid for:
   - ✓ Litecoin block (if meets LTC difficulty)
   - ✓ Dogecoin block (if meets DOGE difficulty, via auxpow)
   - ✓ P2Pool share (if meets share difficulty)

## Node Setup Instructions

### 1. Litecoin Testnet Node

**Installation:**
```bash
# Download Litecoin Core
wget https://download.litecoin.org/litecoin-0.21.2.2/linux/litecoin-0.21.2.2-x86_64-linux-gnu.tar.gz
tar xzf litecoin-0.21.2.2-x86_64-linux-gnu.tar.gz
sudo install -m 0755 -o root -g root -t /usr/local/bin litecoin-0.21.2.2/bin/*
```

**Configuration** (`~/.litecoin/litecoin.conf`):
```ini
# Network
testnet=1
listen=1

# RPC
server=1
rpcuser=litecoinrpc
rpcpassword=YourSecurePassword123
rpcport=19332
rpcbind=0.0.0.0
rpcallowip=192.168.80.0/24

# P2P
port=19335

# Performance
dbcache=2000
maxmempool=300

# Logging
debug=0
```

**Start:**
```bash
litecoind -testnet -daemon
litecoin-cli -testnet getblockchaininfo
```

**Sync Status:**
```bash
# Wait for sync (can take hours/days)
litecoin-cli -testnet getblockcount
litecoin-cli -testnet getblockchaininfo | grep -E "chain|blocks|headers"
```

### 2. Dogecoin Testnet Node (Already Running!)

**You already have this running at 192.168.80.182 (oplex32)**

**Current Configuration:**
- RPC: localhost:44555 (needs network binding)
- Chain: testnet
- Synced: ✓ 21.3M blocks
- Auxpow: ✓ Enabled (chainid 98)

**Update Configuration** (on 192.168.80.182):
```bash
ssh oplex32
nano ~/.dogecoin/dogecoin.conf
```

Add network RPC binding:
```ini
# Allow RPC from p2pool server
rpcbind=0.0.0.0
rpcallowip=192.168.80.0/24
```

Restart:
```bash
dogecoin-cli -testnet stop
dogecoind -testnet -daemon
```

### 3. P2Pool Configuration

**Edit run_p2pool.py** or create new script:
```python
#!/usr/bin/env pypy
import sys
from p2pool import main

# Litecoin testnet with Dogecoin merge mining
sys.argv = [
    'run_p2pool.py',
    '--net', 'litecoin_testnet',
    
    # Litecoin RPC (parent chain)
    '--bitcoind-address', '192.168.80.YOUR_LTC_NODE:19332',
    '--bitcoind-rpc-username', 'litecoinrpc',
    '--bitcoind-rpc-password', 'YourSecurePassword123',
    
    # Dogecoin RPC (child chain for merge mining)
    '--merged-mining', 'http://dogeuser:dogepass123@192.168.80.182:44555/',
    
    # P2Pool ports
    '--p2pool-port', '19338',      # Share chain P2P
    '--worker-port', '19327',      # Stratum for miners
    
    # Mining address (Litecoin testnet address)
    '--address', 'YOUR_tLTC_ADDRESS_HERE',
    
    # Performance
    '--max-conns', '40',
]

main.main()
```

## Network Ports Summary

| Service | Host | Port | Protocol | Purpose |
|---------|------|------|----------|---------|
| Litecoin RPC | LTC node | 19332 | HTTP | getblocktemplate |
| Litecoin P2P | LTC node | 19335 | Bitcoin P2P | Block propagation |
| Dogecoin RPC | 192.168.80.182 | 44555 | HTTP | auxpow template |
| Dogecoin P2P | 192.168.80.182 | 44556 | Bitcoin P2P | Block propagation |
| P2Pool P2P | P2Pool server | 19338 | P2Pool | Share chain |
| P2Pool Stratum | P2Pool server | 19327 | Stratum | Miners connect |

## Testing Setup

### 1. Verify ltc_scrypt Module
```bash
cd litecoin_scrypt/
./build.sh
python3 test_scrypt.py  # or: pypy test_scrypt.py
```

### 2. Test Litecoin RPC
```bash
curl --user litecoinrpc:YourSecurePassword123 \
  --data-binary '{"jsonrpc":"1.0","id":"test","method":"getblockchaininfo","params":[]}' \
  -H 'content-type: text/plain;' \
  http://localhost:19332/
```

### 3. Test Dogecoin Auxpow
```bash
ssh oplex32
curl --user dogeuser:dogepass123 \
  --data-binary '{"jsonrpc":"1.0","id":"test","method":"getblocktemplate","params":[{"capabilities":["auxpow"]}]}' \
  -H 'content-type: text/plain;' \
  http://localhost:44555/
```

Expected response includes:
```json
{
  "auxpow": {
    "chainid": 98,
    "target": "0000000000..."
  },
  ...
}
```

### 4. Start P2Pool
```bash
# Install Python dependencies first
pip install --user twisted pyOpenSSL

# Run p2pool
pypy run_p2pool_ltc_testnet.py
```

### 5. Test Mining
```bash
# Install cpuminer-multi (Scrypt support)
sudo apt install build-essential automake libcurl4-openssl-dev
git clone https://github.com/tpruvot/cpuminer-multi
cd cpuminer-multi
./autogen.sh
./configure CFLAGS="-O3 -march=native"
make

# Mine to P2Pool
./minerd -a scrypt -o stratum+tcp://localhost:19327 -u tLTC_ADDRESS -p x
```

## Troubleshooting

### Litecoin Node Won't Sync
- Check disk space: `df -h`
- Check connections: `litecoin-cli -testnet getpeerinfo | grep addr`
- Bootstrap: Add `addnode=testnet-seed.litecointools.com` to config

### Dogecoin RPC Connection Refused
```bash
# On oplex32, verify RPC is listening on network
ss -tlnp | grep 44555

# Should show: 0.0.0.0:44555 (not 127.0.0.1:44555)
```

### P2Pool Can't Connect to Nodes
- Test connectivity: `telnet 192.168.80.182 44555`
- Check firewall: `sudo ufw status`
- Verify credentials in p2pool config

### No Shares Found
- Normal on testnet (low difficulty)
- Check miner connection: P2Pool should show "Connected worker"
- Verify Scrypt algorithm: `--algo scrypt` in miner

## Expected Mining Results

**Litecoin Testnet:**
- Network hashrate: ~500 MH/s
- Block time: 2.5 minutes
- Your CPU (~1 KH/s): Very unlikely to find blocks alone

**Dogecoin Testnet:**
- Network hashrate: ~338 KH/s  
- Block time: 1 minute
- Your CPU (~1 KH/s): ~4-5 blocks/day possible!

**P2Pool Shares:**
- Share difficulty: Adjusted by pool
- Target: 15 second shares
- Even at 1 KH/s you should find shares

## Next Steps

1. ✓ ltc_scrypt module built and tested
2. ⏳ Set up Litecoin testnet node
3. ⏳ Configure Dogecoin RPC for network access
4. ⏳ Configure P2Pool for merge mining
5. ⏳ Test with CPU miner
6. ⏳ Verify coinbase construction with auxpow

## References

- [Litecoin Core](https://litecoin.org/)
- [Dogecoin Core](https://dogecoin.com/)
- [jtoomim/p2pool](https://github.com/jtoomim/p2pool)
- [Merged Mining Specification](https://en.bitcoin.it/wiki/Merged_mining_specification)
