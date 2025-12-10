# ASIC Support Fixes - December 2025

This document summarizes the fixes made to support high-hashrate ASIC miners (particularly Antminer D9 with ~2 TH/s).

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

- **Faster Adjustment:** Triggers vardiff recalculation after 3 shares instead of 12
- **Larger Adjustment Range:** Allows 0.25x to 4x adjustment per iteration (was 0.5x to 2x)
- **Rate Limiting:** Drops submissions if more than 100/sec to prevent server overload
- **Proper Difficulty Floor:** Uses `min()` instead of `max()` to enforce difficulty floor (lower target = harder difficulty)
- **Short Job IDs:** Changed from 128-bit to 32-bit (8 hex chars) for ASIC compatibility
- **Better Error Handling:** Don't disconnect on temporary dashd errors

### 4. Fixed Weakref Callback Errors
**Files:** `p2pool/util/forest.py`, `p2pool/util/variable.py`

- **Problem:** Weakref callbacks could crash with "TypeError: 'NoneType' object is not callable" during garbage collection.
- **Solution:** Added safe wrappers that check for None before calling the referenced object.

### 5. Improved Work Generation
**Files:** `p2pool/work.py`

- **Fixed Share Difficulty Floor:** Properly enforce minimum difficulty from `SANE_TARGET_RANGE` during bootstrap and normal operation.
- The P2Pool share floor now respects both `share_info['bits'].target` and network's `SANE_TARGET_RANGE[1]`.

### 6. Better Dashd Error Handling
**Files:** `p2pool/dash/helper.py`

- Added specific handling for `TimeoutError` and `ConnectionRefusedError` from dashd RPC calls.
- Retry silently on temporary errors instead of crashing.

### 7. Web Interface Fix
**Files:** `p2pool/web.py`

- Fixed `best_share_hash` endpoint to handle `None` value during bootstrap (returns 64 zeros instead of crashing).

## Testing Configuration

For testing without peers, `CHAIN_LENGTH` and `REAL_CHAIN_LENGTH` are temporarily reduced to 10 in `p2pool/networks/dash.py`. **Revert to 4320 for production use.**

## Deployment Notes

After deploying these changes:
1. Restart P2Pool to apply new configuration
2. Workers will reconnect and difficulty will ramp up appropriately
3. Local hashrate display should show correct values (~2 TH/s per D9 ASIC)
4. Vardiff should stabilize around 3000-10000 for D9 miners with 10s target rate
