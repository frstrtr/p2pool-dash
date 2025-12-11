# P2Pool-Dash

Decentralized pool mining software for Dash cryptocurrency.

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

* ✅ **ASICBOOST Support**: Full BIP320 version-rolling implementation
* ✅ **Modern ASIC Compatible**: Works with Antminer D3/D5/D7
* ✅ **Enhanced Difficulty Control**: Support for +difficulty and /difficulty modifiers
* ✅ **Variable Difficulty**: Configurable vardiff with --share-rate parameter
* ✅ **Backward Compatible**: CPU/GPU miners still supported

### Modern Ubuntu/Debian (24.04+)

Python 2 is no longer available. Use PyPy:

```bash
# Install PyPy
sudo snap install pypy --classic

# Install dependencies
pypy -m pip install twisted==19.10.0 pycryptodome

# Clone and setup
git clone https://github.com/dashpay/p2pool-dash.git
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
git clone https://github.com/dashpay/p2pool-dash.git
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

You can append difficulty modifiers to your Dash address:

**Pseudoshare difficulty** (for vardiff tuning):
```
YOUR_ADDRESS+DIFFICULTY
Example: XdgF55wEHBRWwbuBniNYH4GvvaoYMgL84u+4096
```

**Actual share difficulty** (fixed minimum):
```
YOUR_ADDRESS/DIFFICULTY
Example: XdgF55wEHBRWwbuBniNYH4GvvaoYMgL84u/65536
```

**Worker names** (for monitoring):
```
YOUR_ADDRESS.worker_name
Example: XdgF55wEHBRWwbuBniNYH4GvvaoYMgL84u.antminer1
```

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
- ❌ `p2pool is not connected to any peers` → Set PERSIST=False or wait for peer discovery
- ❌ High CPU usage → Limit miner threads with `-t` flag

**See [INSTALL.md](INSTALL.md) for complete troubleshooting guide.**

## Recent Updates

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
