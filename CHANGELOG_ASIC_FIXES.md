# ASIC Support Fixes - December 2025

This document summarizes the fixes made to support high-hashrate ASIC miners (particularly Antminer D9 with ~1.7 TH/s each, ~10 TH/s total for 6 miners).

**Release v1.0.0** - First stable release with full ASIC support (December 11, 2025)

## Summary of Changes

### 1. Fixed SANE_TARGET_RANGE for Correct Difficulty Calculation
**Files:** `p2pool/dash/networks/dash.py`

- **Problem:** The original `SANE_TARGET_RANGE` used incorrect target values that resulted in wrong difficulty calculations for vardiff.
- **Solution:** Changed to use standard bdiff difficulty 1 target (`0xFFFF * 2**208`) as the maximum target (minimum difficulty).
- **Max Difficulty:** Reduced from 100,000 to 10,000 to ensure 2 TH/s ASICs can submit pseudoshares frequently enough (every ~20 seconds at max difficulty vs ~200 seconds before).

```python
# Before:
SANE_TARGET_RANGE = (2**256//2**32//1000000 - 1, 2**256//2**32 - 1)

# After:
_DIFF1_TARGET = 0xFFFF * 2**208  # Standard bdiff difficulty 1 target
SANE_TARGET_RANGE = (_DIFF1_TARGET // 10000, _DIFF1_TARGET)  # Max diff 10000
```

### 2. Fixed MAX_TARGET for P2Pool Share Difficulty
**Files:** `p2pool/networks/dash.py`

- **Problem:** `MAX_TARGET = 2**256//2**20 - 1` gave an extremely easy difficulty (~0.000244) that caused share spam.
- **Solution:** Changed to standard bdiff difficulty 1 target for reasonable minimum share difficulty.

```python
# Before:
MAX_TARGET = 2**256//2**20 - 1  # ~0.000244 difficulty

# After:
MAX_TARGET = 0xFFFF * 2**208  # Standard bdiff difficulty 1
```

### 3. Improved Stratum Vardiff Algorithm
**Files:** `p2pool/dash/stratum.py`

Major improvements to handle high-hashrate ASIC miners:

- **Initial Difficulty 100:** New workers start at difficulty 100 instead of 1, preventing share flood from high-hashrate ASICs while still allowing slower miners to submit shares quickly.
- **Job-Specific Target Tracking:** Each job stores its own target when created. When shares are submitted, they're validated against the job's original target, not the current target. This prevents race conditions during vardiff adjustments.
- **Bidirectional Vardiff:** Difficulty adjusts both UP (for fast miners) and DOWN (for slow miners) to maintain ~10 second share intervals.
- **Faster Adjustment:** Triggers vardiff recalculation after 3 shares instead of 12.
- **Larger Adjustment Range:** Allows 0.25x to 4x adjustment per iteration (was 0.5x to 2x).
- **Rate Limiting:** Drops submissions if more than 100/sec to prevent server overload.
- **Short Job IDs:** Changed from 128-bit to 32-bit (8 hex chars) for ASIC compatibility.
- **Better Error Handling:** Don't disconnect on temporary dashd errors.

### 4. Fixed "hash > target" Race Condition
**Files:** `p2pool/dash/stratum.py`, `p2pool/work.py`

- **Problem:** When vardiff adjusted difficulty, shares submitted for old jobs would be validated against the new (harder) target, causing "hash > target" rejections even though the share was valid for its original job.
- **Solution:** Store `job_target` with each job in `handler_map`. When a share is submitted, retrieve and use that job's original target for validation.
- **Result:** Miners no longer see share rejections during vardiff transitions.

```python
# Job creation: capture target at dispatch time
job_target = self.target
self.handler_map[jobid] = x, got_response, job_target

# Share submission: use the job's original target
x, got_response, job_target = self.handler_map[job_id]
result = got_response(header, worker_name, coinb_nonce, job_target)
```

### 5. Fixed Weakref Callback Errors
**Files:** `p2pool/util/forest.py`, `p2pool/util/variable.py`

- **Problem:** Weakref callbacks could crash with "TypeError: 'NoneType' object is not callable" during garbage collection.
- **Solution:** Added safe wrappers that check for None before calling the referenced object.

### 6. Improved Work Generation
**Files:** `p2pool/work.py`

- **Fixed Share Difficulty Floor:** Properly enforce minimum difficulty from `SANE_TARGET_RANGE` during bootstrap and normal operation.
- **Submitted Target Support:** `got_response()` now accepts an optional `submitted_target` parameter for vardiff compatibility.
- The P2Pool share floor now respects both `share_info['bits'].target` and network's `SANE_TARGET_RANGE[1]`.

### 7. Better Dashd Error Handling
**Files:** `p2pool/dash/helper.py`

- Added specific handling for `TimeoutError` and `ConnectionRefusedError` from dashd RPC calls.
- Retry silently on temporary errors instead of crashing.

### 8. Web Interface Improvements
**Files:** `p2pool/web.py`, `p2pool/data.py`

- Fixed `best_share_hash` endpoint to handle `None` value during bootstrap (returns 64 zeros instead of crashing).
- **Added `parse_bip0034()` function** (from jtoomim's fork) to extract block height from coinbase transaction (BIP 34 standard).
- **Enhanced `recent_blocks` endpoint** to include:
  - Block number (height) extracted from coinbase
  - Direct block explorer URL for each found block
  - Graceful error handling when no blocks found yet
- **Added `currency_info` endpoint** providing SYMBOL, BLOCK_EXPLORER_URL_PREFIX, ADDRESS_EXPLORER_URL_PREFIX, and TX_EXPLORER_URL_PREFIX for web interface.

The web interface now properly displays found blocks with their height and provides clickable links to the Dash blockchain explorer (chainz.cryptoid.info).

## Testing Results

With 6× Antminer D9 miners (~1.7 TH/s each):
- **Total Hashrate:** ~9-10 TH/s (correctly measured)
- **Share Rejections:** 0% "hash > target" errors after fix
- **Dead on Arrival:** ~0%
- **Vardiff Range:** 3000-10000 depending on individual miner speed
- **Miners Stay Connected:** No disconnections due to vardiff changes

## Testing Configuration

For testing without peers, `CHAIN_LENGTH` and `REAL_CHAIN_LENGTH` are temporarily reduced to 10 in `p2pool/networks/dash.py`. **Revert to 4320 for production use.**

## Deployment Notes

After deploying these changes:
1. Restart P2Pool to apply new configuration
2. Workers will reconnect and start at difficulty 100
3. Vardiff will ramp up to appropriate difficulty within seconds
4. Local hashrate display should show correct values (~1.7 TH/s per D9 ASIC)
5. Vardiff should stabilize around 3000-10000 for D9 miners with 10s target rate

---

## v1.1.0 Improvements (December 11, 2025)

Based on comparison with jtoomim's p2pool fork, the following additional improvements were added:

### 9. Performance Benchmarking (`--bench` flag)
**Files:** `p2pool/__init__.py`, `p2pool/main.py`, `p2pool/work.py`, `p2pool/dash/stratum.py`

- Added `BENCH` global flag (similar to jtoomim's implementation)
- New `--bench` command-line argument to enable performance timing
- `get_work()` and `rpc_submit()` now print timing info when BENCH enabled
- Useful for identifying performance bottlenecks

```bash
# Enable benchmarking
./run_p2pool.py --bench ...
```

### 10. Improved Stratum Error Handling
**Files:** `p2pool/dash/stratum.py`

- Changed stale job handling from raising exceptions to returning `False`
- Better compatibility with various miner implementations
- Added timing benchmarks for submit processing
- Improved error logging with worker name context

### 11. Hash Rate Estimation Integration
**Files:** `p2pool/work.py`

- Hash rate estimation already present via `_estimate_local_hash_rate()` and `get_local_addr_rates()`
- Used for:
  - Automatic share difficulty adjustment
  - Dust threshold protection (prevents unpayable shares from low-hashrate miners)
  - Local rate monitoring per address

### 12. Dust Threshold Protection
**Files:** `p2pool/work.py`, `p2pool/dash/networks/dash.py`

Already implemented:
- `DUST_THRESHOLD = 0.001e8` (0.001 DASH) in network config
- Workers with expected payout < DUST_THRESHOLD get increased share difficulty
- Prevents wasted work from miners who can't achieve payable share amounts

### Features from jtoomim's fork already present in p2pool-dash:
- ✅ Variable difficulty (vardiff) in stratum
- ✅ Hash rate estimation
- ✅ Dust threshold protection  
- ✅ Version rolling / ASICBoost (BIP320)
- ✅ Share rate parameter (`--share-rate`)
- ✅ `parse_bip0034()` for block height extraction
- ✅ Enhanced web interface with block explorer links
- ✅ `currency_info` endpoint

### Command Line Arguments

```bash
# New in v1.1.0:
--bench              # Enable benchmarking mode (print performance timing)

# Existing:
--debug              # Enable debug mode
--share-rate SECS    # Target seconds per pseudoshare (default: 10)
```

---

## v1.2.0 - Enhanced Stratum Protocol (December 11, 2025)

Major Stratum protocol enhancements with careful attention to pool performance protection.

### 13. `mining.suggest_difficulty` Support (NEW)
**Files:** `p2pool/dash/stratum.py`

Miners can now suggest their preferred starting difficulty using the standard `mining.suggest_difficulty` method.

**PERFORMANCE SAFEGUARDS:**
- Pool-wide minimum difficulty floor (0.001 by default)
- Dynamic adjustment based on pool load - when submission rate is high, minimum difficulty is automatically raised
- Vardiff still operates after initial difficulty is set
- Suggested difficulty is validated against pool safety limits

```python
# Miner request:
{"method": "mining.suggest_difficulty", "params": [1024]}

# Pool accepts or adjusts to safe minimum
```

### 14. `minimum-difficulty` BIP310 Extension (NEW)
**Files:** `p2pool/dash/stratum.py`

Full support for BIP310 minimum-difficulty extension, allowing miners to set a difficulty floor for their connection.

**PERFORMANCE SAFEGUARDS:**
- Pool-wide minimum difficulty enforcement
- Dynamic minimum based on current pool load
- Vardiff cannot go below the negotiated minimum

```python
# Miner negotiates via mining.configure:
{"method": "mining.configure", "params": [["minimum-difficulty"], {"minimum-difficulty.value": 100}]}

# Pool response includes actual minimum (may be higher than requested if pool is under load)
```

### 15. Global Pool Statistics Tracking (NEW)
**Files:** `p2pool/dash/stratum.py`

New `PoolStatistics` singleton class providing:
- Total connected workers count
- Per-worker hash rates and share counts
- Global share submission rate monitoring
- Connection history for session resumption
- Pool performance metrics

**Available via web API:**
```bash
curl http://localhost:7903/stratum_stats
```

Returns:
```json
{
  "pool": {
    "connections": 6,
    "workers": 3,
    "total_accepted": 15234,
    "total_rejected": 12,
    "submission_rate": 45.2,
    "uptime": 86400
  },
  "workers": {
    "miner1": {"shares": 5000, "accepted": 4998, "rejected": 2, "hash_rate": 1700000000000},
    ...
  }
}
```

### 16. Per-Worker Statistics (NEW)
**Files:** `p2pool/dash/stratum.py`

Track per-worker metrics:
- Share count (submitted/accepted/rejected)
- Estimated hash rate
- First/last seen timestamps
- Current difficulty
- Connection duration

### 17. Dynamic Share Rate Configuration (NEW)
**Files:** `p2pool/dash/stratum.py`

Workers can specify custom share rate via username suffix:
```
address+s5    # 5 seconds per share
address+100+s3  # difficulty 100, 3 seconds per share
```

Clamped to reasonable range (1-60 seconds).

### 18. Session Resumption (NEW)
**Files:** `p2pool/dash/stratum.py`

When miners reconnect with their session ID, the pool can restore:
- Previous difficulty setting
- Suggested difficulty
- Minimum difficulty floor
- Custom share rate

Reduces initial difficulty negotiation time after reconnects.

### 19. `client.reconnect` Support (NEW)
**Files:** `p2pool/dash/stratum.py`

Pool can request miners to reconnect (for load balancing):
```python
# Pool to miner:
{"method": "client.reconnect", "params": ["hostname", 3333, 0]}
```

Session state is preserved for seamless resumption.

### 20. Structured Connection Logging (NEW)
**Files:** `p2pool/dash/stratum.py`

Enhanced logging with:
- Connection/disconnection events with session IDs
- Per-session statistics (duration, shares)
- Worker identification
- Difficulty change tracking

### Performance Protection Summary

The new difficulty-related features include multiple safeguards to prevent pool overload:

| Safeguard | Description |
|-----------|-------------|
| MIN_DIFFICULTY_FLOOR | Absolute minimum difficulty (0.001 by default) |
| MAX_SUBMISSIONS_PER_SECOND | Global rate limit (1000/sec default) |
| Dynamic minimum adjustment | When submission rate > 50% of max, minimum difficulty increases |
| Per-connection rate limiting | Drop shares if > 100/sec from single connection |
| Vardiff continues operating | Even with suggest_difficulty, vardiff adjusts based on actual performance |

### Web API Endpoints

| Endpoint | Description |
|----------|-------------|
| `/stratum_stats` | Pool and worker statistics |
| `/global_stats` | General pool stats (existing) |
| `/local_stats` | Local node stats (existing) |

### Stratum Protocol Extensions Supported

| Extension | BIP | Status |
|-----------|-----|--------|
| `version-rolling` | BIP320 | ✅ Full support |
| `minimum-difficulty` | BIP310 | ✅ NEW |
| `subscribe-extranonce` | NiceHash | ✅ Full support |
| `mining.suggest_difficulty` | Standard | ✅ NEW |

