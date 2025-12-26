# Miner Payout Calculation Fix

## Issue
The miner stats web interface was showing "Pending..." and "Calculating..." for block rewards and estimated payouts, even after 100+ blocks confirmation.

## Root Cause
The `get_miner_payouts()` endpoint in `p2pool/web.py` was using a **hardcoded placeholder estimate** of 10% of the block reward:

```python
estimated_payout = block_reward * 0.1  # Rough estimate, actual varies
```

This was a temporary stub implementation that:
- ✅ Correctly tracked found blocks via `block_history` persistent storage
- ✅ Correctly fetched actual block rewards from the blockchain
- ❌ **Did NOT calculate actual PPLNS payouts** - just guessed 10%

## Solution
Replaced the hardcoded estimate with the **actual PPLNS (Pay Per Last N Shares) calculation** using the existing `p2pool_data.get_expected_payouts()` function.

### How It Works

The fix calculates each miner's actual share of the block reward by:

1. **Retrieving the share hash** when the block was found (stored in `block_history`)
2. **Getting the share data** from the share tracker at that specific time
3. **Calling `get_expected_payouts()`** with:
   - The share tracker
   - The specific share hash from when the block was found
   - The network difficulty at that time
   - The actual block reward in satoshis
   - The network parameters

4. **Extracting the payout** for the miner's address from the PPLNS distribution

### Fallback Strategy
If the historical share is no longer in memory (older blocks), the function falls back to calculating based on the current best share, which is less accurate but still better than the hardcoded 10%.

### PPLNS Distribution Model
The payout is calculated per P2Pool's PPLNS model:
- **98% of reward** distributed proportionally based on share weight during the payout window (typically 4-5 days)
- **2% of reward** distributed equally to all miners as block finder bonus

## Files Modified
- `p2pool/web.py` - Fixed `get_miner_payouts()` function (lines 545-580)

## Data Sources Used

### Block History Storage
Persistent JSON file at `<datadir>/block_history.json` containing:
- Block hash and height
- Timestamp when found
- Pool hashrate at block time
- Network difficulty
- Miner address
- Block reward (fetched from blockchain)
- Block status (pending/confirmed)

### Share Tracker
In-memory P2Pool share chain containing:
- All shares submitted by miners
- Share difficulty and weight
- Miner addresses
- Timestamps

### Blockchain Data
RPC calls to dashd for:
- Block confirmations
- Actual transaction outputs (rewards)
- Block status (confirmed after 100 maturity blocks)

## Testing
The fix should now correctly display:
- ✅ Block rewards fetched from blockchain
- ✅ Estimated payouts calculated using actual PPLNS formula
- ✅ Correct status (pending/confirmed) based on block maturity
- ✅ Updated in real-time as blocks mature and confirmations increase

## Notes
- Block confirmations are tracked until 100+ (Dash coinbase maturity)
- Confirmed blocks show final payout values
- Pending blocks show estimated values that may change as more shares are submitted
- If PPLNS calculation fails, payout shows as 0 (safe fallback) rather than guessing
