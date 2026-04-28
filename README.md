# P2Pool-Dash

[![Release](https://img.shields.io/github/v/tag/frstrtr/p2pool-dash?label=release&sort=semver)](https://github.com/frstrtr/p2pool-dash/releases)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](COPYING)

> **Lineage:** [dashpay/p2pool-dash](https://github.com/dashpay/p2pool-dash) (stale since May 2023) --> **frstrtr/p2pool-dash** (active)

Decentralized pool mining software for Dash cryptocurrency (X11).

### What's different from dashpay/p2pool-dash

| Area | dashpay (stale) | frstrtr (this fork) |
|------|----------------|---------------------|
| **Dash Core** | v0.12-v18 era | v23+ (Protocol 70238+) |
| **Dashboard** | Basic stats | Luck badges, difficulty tracking, block history graphs |
| **Difficulty** | Basic | Variable difficulty + share-rate tuning |
| **Share archive** | Not available | Persistent share/block history |
| **Block propagation** | Basic relay | Enhanced reliable propagation |
| **Extranonce** | Not supported | Full extranonce rolling support |
| **Installation** | Requires Python 2.7 | PyPy on modern Ubuntu 24.04+ |
| **Maintenance** | Last commit May 2023 | Active development |

### 🔗 Sister project: P2Pool V36 — Merged Mining (LTC+DOGE)

> **[frstrtr/p2pool-merged-v36](https://github.com/frstrtr/p2pool-merged-v36)** — V36 share format with AuxPoW merged mining for Litecoin + Dogecoin. Running on mainnet. [First merged-mined DOGE block found on 2026-03-23](https://blockchair.com/dogecoin/block/f84500c25a4cce2a08887f29763726bd5ecec7b66fed65a88b181fb0b0ab2383). Latest release: [v0.14.2](https://github.com/frstrtr/p2pool-merged-v36/releases/tag/v0.14.2-hotfix).

> **🔧 c2pool — next generation p2pool in C++**
> [c2pool](https://github.com/frstrtr/c2pool) is a ground-up C++ reimplementation of p2pool for Litecoin with embedded DOGE merged mining. No coin daemon needed — built-in SPV syncs via P2P. Cross-platform (Linux/Windows/macOS). While p2pool-dash serves the Dash/X11 community, c2pool brings the same decentralized mining philosophy to Scrypt miners with a modern C++ codebase. [Download v0.1.1-alpha →](https://github.com/frstrtr/c2pool/releases/tag/v0.1.1-alpha)

## 📋 Documentation

**⚠️ IMPORTANT**: For complete installation instructions, troubleshooting, and configuration, please see:

### **[📖 INSTALL.md - Complete Installation Guide](INSTALL.md)**

The installation guide covers:
- ✅ System requirements and dependencies
- ✅ Dash Core installation and configuration
- ✅ Python 2.7 / PyPy setup (modern Ubuntu/Debian)
- ✅ dash_hash module compilation
- ✅ Standalone vs Multi-node configuration
- ✅ Common issues and solutions (OpenSSL, missing modules, etc.)
- ✅ Performance tuning and security

## Quick Start

### Requirements

* **Dash Core**: >=23.0.0 (Protocol 70238+)
* **Python**: 2.7 (via PyPy recommended)
* **Twisted**: >=19.10.0
* **pycryptodome**: >=3.9.0
* **dash_hash**: X11 hashing module (included as submodule)

### Features

* ✅ **Dash Core v23+**: Protocol 70238+ support
* ✅ **Enhanced Dashboard**: Luck badges with exponential decay, network difficulty graphs, block history
* ✅ **Variable Difficulty**: Configurable vardiff with --share-rate parameter, +difficulty and /difficulty modifiers
* ✅ **Share Archive**: Persistent share and block history across restarts
* ✅ **Peer Management**: Improved peer selection, connection dedup, capacity-aware discovery

### Modern Ubuntu/Debian (24.04+)

Python 2 is no longer available. Use PyPy:

```bash
# Install PyPy
sudo snap install pypy --classic

# Install dependencies
pypy -m pip install zope.interface==4.1.3 Twisted==15.4.0 pycrypto

# Clone and setup
git clone https://github.com/frstrtr/p2pool-dash.git
cd p2pool-dash
git submodule init
git submodule update

# Build dash_hash
cd dash_hash
pypy setup.py install --user
cd ..

# Run P2Pool
pypy run_p2pool.py --net dash -a YOUR_DASH_ADDRESS
```

**See [INSTALL.md](INSTALL.md) for detailed instructions.**

### Ubuntu 24.04 Automated Installer

For Ubuntu 24.04 systems, we provide an automated installer script that sets up PyPy2, builds a local OpenSSL 1.1, and configures p2pool with systemd integration:

```bash
./install_p2pool_ubuntu_2404.sh
```

### Older Systems (Ubuntu 20.04 and earlier)

If Python 2.7 is still available:

```bash
sudo apt-get install python2 python2-dev python2-twisted python2-pip gcc g++
git clone https://github.com/frstrtr/p2pool-dash.git
cd p2pool-dash
git submodule init && git submodule update
cd dash_hash && python2 setup.py install --user && cd ..
python2 run_p2pool.py --net dash -a YOUR_DASH_ADDRESS
```

## Mining to P2Pool

Point your miner to:
```
stratum+tcp://YOUR_IP:7903
```

Username: Your Dash address  
Password: anything

### Advanced Username Options

You can append modifiers to your Dash address. They are parsed by `work.py:get_user_details` and apply to both the stratum and getwork paths.

**Pseudoshare difficulty `+N`** — locks stratum vardiff at diff N (disables auto-tuning for this connection):
```
YOUR_ADDRESS+DIFFICULTY
Example: XdgF55wEHBRWwbuBniNYH4GvvaoYMgL84u+4096
```

**Share difficulty hint `/N`** — passed to share-generation as `desired_target`. The share class consensus-clamps it to the per-share band relative to chain history (`[pre_target3//30, pre_target3]` in [`p2pool/data.py`](p2pool/data.py)), so values outside the band are silently overridden — typically a no-op when sharechain difficulty is well above N. Does **not** lock stratum vardiff:
```
YOUR_ADDRESS/DIFFICULTY
Example: XdgF55wEHBRWwbuBniNYH4GvvaoYMgL84u/65536
```

**Share rate `+sN`** (stratum-only) — selects target seconds per pseudoshare for vardiff tuning, clamped to [1, 60]. Overrides the pool default (`--share-rate`):
```
YOUR_ADDRESS+sN
Example: XdgF55wEHBRWwbuBniNYH4GvvaoYMgL84u+s30
```

**Combined**: `YOUR_ADDRESS+4096+s15` locks vardiff at diff 4096 and asks for ~15 s per pseudoshare.

**Worker names** (for monitoring):
```
YOUR_ADDRESS.worker_name
Example: XdgF55wEHBRWwbuBniNYH4GvvaoYMgL84u.antminer1
```

### Stratum vardiff bounds

Pool-side vardiff is bounded to protect both the pool and the miner:

- **Floor**: `MIN_DIFFICULTY_FLOOR = 64` (in [`p2pool/dash/stratum.py`](p2pool/dash/stratum.py)) — hardest minimum the pool will offer. Prevents a misbehaving or low-end miner from being driven to a near-zero target and flooding the node with tens of thousands of submissions per second.
- **Ceiling**: `min(SANE_TARGET_RANGE[0], current_chain_target)`. The chain-target term lets vardiff track the sharechain when its difficulty exceeds the static parent-coin sane bound (diff 10K for Dash). For single Dash X11 ASICs (D3 / D5 / D7 / D9 / iBeLink BM-N3) at default share rate, this ceiling does not engage — vardiff settles below it. It only matters for stratum proxies that aggregate multiple miners into one connection (>~5 TH/s/conn), where it bounds the over-submission ratio.

Backup connections that have never submitted a share are exempt from timeout-based vardiff reduction, so they do not drift toward the floor while standing by.

## Configuration Modes

### Standalone Mode (Solo/Testing)
Edit `p2pool/networks/dash.py`:
```python
PERSIST = False  # No peers required
```

### Multi-Node Mode (Pool Mining)
Edit `p2pool/networks/dash.py`:
```python
PERSIST = True  # Connect to P2Pool network
```

**⚠️ IMPORTANT**: When upgrading to the latest version with Dash Platform support:
- **Delete old sharechain data**: `data/dash/shares.*` and `data/dash/graph_db`
- Old shares are incompatible due to `_script` field changes
- All nodes in the P2Pool network must update together
- **Protection**: Incompatible shares are validated and rejected BEFORE entering sharechain
- Outdated peers receive clear upgrade instructions in logs

**For detailed configuration, see [INSTALL.md](INSTALL.md).**

## Command Line Options

```bash
pypy run_p2pool.py --help
```

Common options:
- `--net dash` - Use Dash mainnet
- `--net dash_testnet` - Use Dash testnet
- `-a ADDRESS` - Your Dash payout address
- `--dashd-rpc-port 9998` - Dash RPC port (default: 9998)
- `--dashd-address 127.0.0.1` - Dash RPC address
- `--share-rate SECONDS` - Target seconds per pseudoshare (default: 10)

## Troubleshooting

### Common Issues

All issues and solutions are documented in **[INSTALL.md](INSTALL.md)**, including:

- ❌ `ImportError: No module named dash_hash` → Rebuild dash_hash module
- ❌ `AttributeError: ComposedWithContextualOptionalsType` → Update to latest version
- ❌ `ValueError: Block not found` → Update to commit e9b5f57+
- ❌ `ImportError: No module named bitcoin` → Update to latest version
- ❌ `ImportError: No module named OpenSSL` → Non-fatal, can ignore or see INSTALL.md
- ✅ `p2pool is not connected to any peers` → Fixed! No longer blocks work generation
- ❌ High CPU usage → Limit miner threads with `-t` flag

**See [INSTALL.md](INSTALL.md) for complete troubleshooting guide.**

## Recent Updates

### Stratum Vardiff & Username Parsing (April 2026)
- ✅ **Vardiff floor raised to diff 64** ([d1b67299](https://github.com/frstrtr/p2pool-dash/commit/d1b67299)) — protects pool from share-flood when a connection is driven near the absolute minimum
- ✅ **Idle-conn timeout vardiff gated on `shares_submitted > 0`** ([d1b67299](https://github.com/frstrtr/p2pool-dash/commit/d1b67299)) — backup/standby connections no longer drift to the floor while waiting; failover no longer triggers a flood
- ✅ **Init-order fix for `StratumRPCMiningProvider`** ([0dc22a8c](https://github.com/frstrtr/p2pool-dash/commit/0dc22a8c)) — early-return rejection paths (banned IP / connection flood) no longer raise `AttributeError: conn_id`
- ✅ **Username parser unified** ([69e1a972](https://github.com/frstrtr/p2pool-dash/commit/69e1a972)) — stratum delegates `+N` / `/N` parsing to `work.py:get_user_details`. Behavior change: `/N` no longer locks vardiff (use `+N` for that); `/N` is now a share-target hint that data.py consensus-clamps
- ✅ **Removed redundant session-linkage call** ([69e1a972](https://github.com/frstrtr/p2pool-dash/commit/69e1a972)) — vardiff now decays per-connection honestly when an active conn goes silent, with `MIN_DIFFICULTY_FLOOR=64` as the worst-case bound
- ✅ **Dynamic vardiff cap** ([69e1a972](https://github.com/frstrtr/p2pool-dash/commit/69e1a972)) — stratum cap = `min(SANE_TARGET_RANGE[0], current_chain_target)`, allowing vardiff on high-hashrate aggregators to track sharechain difficulty instead of plateauing at the static SANE cap and over-submitting

### v23.0+ Critical Fixes
- ✅ Missing type classes in pack.py (ComposedWithContextualOptionalsType, ContextualOptionalType, BoolType)
- ✅ Wrong module import (bitcoin → dash)
- ✅ Block hash formatting (zero-padding)
- ✅ Empty payee address handling
- ✅ Removed defunct bootstrap nodes
- ✅ Standalone mode support (PERSIST=False)

### Enhanced Features (December 2025)
- ✅ Enhanced difficulty control (+diff, /diff modifiers)
- ✅ X11 DUMB_SCRYPT_DIFF constant for accurate difficulty display
- ✅ Worker IP tracking infrastructure
- ✅ Configurable vardiff with --share-rate parameter (default: 10 seconds)
- ✅ Improved min_share_target bounds for better difficulty adjustment
- ✅ Fixed Dash-specific got_response() signature compatibility
- ✅ **Block luck calculation** with time-weighted average hashrate
- ✅ **Hashrate sampling** for precise luck statistics
- ✅ **Telegram notifications** for block announcements
- ✅ **Block status tracking** (confirmed/orphaned/pending)
- ✅ **Dash Platform support** (v20+): Handles OP_RETURN platform payments (22.5% block subsidy)
- ✅ **Packed object compatibility**: Fixed share verification for _script field handling
- ✅ **Mainnet ready**: Full support for masternode/platform/superblock payment structures
- ✅ **Solo mining support**: Removed peer connection requirement - works standalone with PERSIST=True
- ✅ **Incompatible share protection**: Pre-validation prevents outdated shares from entering sharechain
- ✅ **Smart peer connections**: Temporary bans for failing peers, counts total connections (incoming+outgoing)

## Port Forwarding

If behind NAT, forward these ports:
- **8999**: P2Pool P2P (for peer connections)
- **7903**: Stratum (for miners)

Do NOT forward port 9998 (Dash RPC - security risk)

## Web Interface & API

P2Pool provides a web interface at `http://YOUR_IP:7903/`:

### Web Pages
- `/static/index.html` - Classic status page
- `/static/dashboard.html` - Modern dashboard with graphs
- `/static/graphs.html` - Detailed statistics graphs

### API Endpoints
- `/local_stats` - Local node statistics
- `/global_stats` - Pool-wide statistics
- `/recent_blocks` - Recently found blocks with luck info
- `/current_payouts` - Current payout distribution
- `/hashrate_samples` - Hashrate sampling stats for luck calculation
- `/block_history` - Historical block data

### Luck Calculation

Block luck shows how "lucky" the pool was finding each block:
- **>100%** (green): Found faster than expected
- **75-100%** (yellow): Normal range
- **<75%** (red): Found slower than expected

Luck is calculated using: `(expected_time / actual_time) × 100%`

The pool uses three methods for hashrate estimation (in order of preference):
1. **Time-weighted average**: Uses actual hashrate samples between blocks (most precise)
2. **Simple average**: Average of hashrates at previous and current block
3. **Single hashrate**: Fallback to current pool hashrate

### Telegram Notifications

To enable Telegram block announcements:
1. Create a bot via [@BotFather](https://t.me/botfather)
2. Edit `data/dash/telegram_config.json`:
```json
{
  "enabled": true,
  "bot_token": "YOUR_BOT_TOKEN",
  "chat_id": "YOUR_CHAT_ID"
}
```

### Connection Threat Detection

The Stratum interface includes intelligent threat detection that monitors connection patterns per IP address. The system calculates a **connection-to-worker ratio** to distinguish between:

- **Normal**: Legitimate multi-rig miners (e.g., 7 connections running 7 unique workers = 1:1 ratio)
- **Elevated**: Suspicious patterns (e.g., 10 connections but only 2 workers = 5:1 ratio)
- **High**: Likely attack or misconfiguration (e.g., 20 connections with 3 workers = 6.7:1 ratio)

#### Configuration

Thresholds are configurable per network in `p2pool/networks/*.py`:

```python
# Default values (dash.py)
CONNECTION_WORKER_ELEVATED = 4.0   # Flag if >4 connections per worker
CONNECTION_WORKER_WARNING = 6.0     # Flag as high if >6 connections per worker
```

This ensures legitimate miners running multiple machines from the same IP are not incorrectly flagged as threats, while still detecting actual connection flooding attempts.

### Persistent Block History

P2Pool stores all found blocks in `data/dash/block_history.json` for permanent record-keeping. This allows the web interface to display complete historical data including:

- Block height, hash, and timestamp
- Network difficulty and block reward
- Pool hashrate at the time of discovery
- Block status (confirmed/orphaned/pending)
- Luck calculation with time-weighted averages

#### Populating Historical Blocks

If you want to add previously mined blocks to the persistent history (e.g., after a fresh install), use the `scripts/populate_block_history.py` utility:

```bash
# Create a file with block heights (one per line)
cat > historical_blocks.txt <<EOF
2389670
2389615
2389577
EOF

# Populate block history from blockchain
pypy scripts/populate_block_history.py \
    --datadir data/dash \
    --blocks-file historical_blocks.txt \
    --dashd-rpc-username YOUR_RPC_USER \
    --dashd-rpc-password YOUR_RPC_PASS
```

Or specify blocks directly:
```bash
pypy scripts/populate_block_history.py \
    --datadir data/dash \
    --blocks 2389670,2389615,2389577 \
    --dashd-rpc-username YOUR_RPC_USER \
    --dashd-rpc-password YOUR_RPC_PASS
```

The script will query dashd to fetch block rewards, timestamps, and difficulty, then merge this data into your block history. This ensures consistent graphs and statistics even for blocks found before the current p2pool installation.

Official wiki :
-------------------------
https://en.bitcoin.it/wiki/P2Pool

Alternate web front end :
-------------------------
* https://github.com/hardcpp/P2PoolExtendedFrontEnd
* https://github.com/johndoe75/p2pool-node-status
* https://github.com/justino/p2pool-ui-punchy

Sponsors:
-------------------------

Thanks to:
* The Bitcoin Foundation for its generous support of P2Pool
* The Litecoin Project for its generous donations to P2Pool
* The Vertcoin Community for its great contribution to P2Pool
* jakehaas, vertoe, chaeplin, dstorm, poiuty, elbereth  and mr.slaveg from the Darkcoin/Dash Community

