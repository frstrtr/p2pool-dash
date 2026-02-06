# Dash Protocol Update for Dash Core v23.x Compatibility

## Date: February 6, 2026

## Summary
Updated P2Pool-Dash protocol version and minimum dashd version to match Dash Core v23.x.
This is a routine version bump to stay current with the Dash network protocol.

## Changes Made

### 1. Protocol Version Bump
Updated P2P protocol version from `70238` to `70240`:

**File: `p2pool/dash/p2p.py`**
- `version=70238` → `version=70240` in `send_version()` 
- `send_getheaders(version=70238)` → `send_getheaders(version=70240)`

**File: `p2pool/dash/height_tracker.py`**
- `send_getheaders(version=70238)` → `send_getheaders(version=70240)`

### 2. Minimum Dash Core Version
Updated `VERSION_CHECK` from `v >= 200000` to `v >= 230000` in:
- `p2pool/dash.py`
- `p2pool/networks/dash.py`
- `p2pool/networks/dash_testnet.py`
- `p2pool/networks/dash_regtest.py`
- `p2pool/networks/dash_custom_example.py`

### 3. Error Message Updates
**File: `p2pool/dash/helper.py`**
- Updated error messages from "Upgrade to v20.0.0 or newer!" to "Upgrade to v23.0.0 or newer!"

## Protocol Version History (Dash Core v23.0)

- **70240 (PLATFORMBAN_V2_SHORT_ID_VERSION)**: Latest version
  - Added PLATFORMBAN message to v2 P2P short ID mapping
- **70239 (QFCOMMIT_STALE_REPROP_BAN_VERSION)**: Ban re-propagation of old QFCOMMIT
- **70238 (PLATFORM_BAN_VERSION)**: Introduced platformban p2p message
- **70237 (ISDLOCK_CYCLEHASH_UPDATE_VERSION)**: Changed cycleHash field in isdlock message
- **70221 (MIN_PEER_PROTO_VERSION)**: Minimum peer protocol version

## Compatibility
- **Required Dash Core Version**: v23.0.0 or newer (230000+)
- **Protocol Version**: 70240
- **Backwards Compatibility**: Older Dash Core versions (< v23.0) are no longer supported

## References
- Dash Core v23.0.0 Release: https://github.com/dashpay/dash/releases/tag/v23.0.0
- Dash Core v23.0.2 Release: https://github.com/dashpay/dash/releases/tag/v23.0.2
