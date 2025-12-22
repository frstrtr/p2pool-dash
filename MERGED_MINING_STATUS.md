# P2Pool Merged Mining Status

## Date: December 22, 2025

## Current State: ✅ READY FOR PHASE 1 IMPLEMENTATION

### Infrastructure Status

#### ✅ Litecoin Core Testnet
- **Status**: Fully synced and operational
- **Blocks**: 4,476,250
- **RPC Port**: 19332
- **Version**: Standard Litecoin Core with Segwit/MWEB support
- **Location**: /home/user0/bin/litecoind

#### ✅ Dogecoin Core Testnet (Modified with Auxpow)
- **Status**: Running and operational
- **Blocks**: 21,482,578
- **RPC Port**: 44555
- **Version**: v1.14.99.0-436b09bb8 (MODIFIED with auxpow capability)
- **Location**: /home/user0/bin-auxpow/dogecoind
- **Startup Script**: ~/start-dogecoin-auxpow.sh
- **Library Path**: ~/lib (contains libboost_*.so.1.83.0)
- **Feature**: getblocktemplate with auxpow capability ✅ VERIFIED

#### ✅ P2Pool
- **Status**: Successfully starts with Litecoin testnet
- **Branch**: feature/scrypt-litecoin-dogecoin
- **Architecture**: 
  - `p2pool/bitcoin/` - Generic Bitcoin protocol with Segwit support
  - `p2pool/dash/` - Dash-specific code
  - `p2pool/litecoin/` - Scrypt-specific code with Segwit support
- **Network**: litecoin_testnet
- **Worker Port**: 9327
- **P2Pool Port**: 9338

### Addresses

#### Litecoin Testnet
- **Legacy**: mm3suEPoj1WnhYuRTdoM6dfEXQvZEyuu9h
- **P2SH-Segwit**: QcVudrUyKGwqjk4KWadnXfbHgnMVHB1Lif
- **Bech32 (Active)**: tltc1qpkcpgwl24flh35mknlsf374x8ypqv7de6esjh4

#### Dogecoin Testnet
- **Address**: nmkmeRtJu3wzg8THQYpnaUpTUtqKP15zRB

### Modified Dogecoin Features

#### Auxpow Capability Test Results ✅

**Command:**
```bash
~/start-dogecoin-auxpow.sh ~/bin-auxpow/dogecoin-cli -testnet getblocktemplate '{"capabilities":["auxpow"]}'
```

**Response (excerpt):**
```json
{
  "version": 6422788,
  "height": 21482578,
  "coinbasevalue": 1000000000000,
  "previousblockhash": "8cdfedd0d08acd6b...",
  "auxpow": {
    "chainid": 98,
    "target": "000000000000000000000000000000000000000000000000000000ffff0f0000"
  }
}
```

**Verification**: ✅ PASS
- Auxpow object present in response
- Chain ID correctly set to 98 (Dogecoin)
- Target provided in reversed byte order
- Full block template includes transactions, coinbasevalue, etc.

#### Key Differences from Standard Dogecoin

| Feature | Standard Dogecoin | Modified Dogecoin |
|---------|------------------|-------------------|
| RPC Method | `getauxblock()` | `getblocktemplate({"capabilities": ["auxpow"]})` |
| Coinbase Control | Dogecoin Core builds | P2Pool builds (multiaddress) |
| Payout Model | Single address | Multiple addresses per share |
| Transaction List | Not provided | Full list included |
| Submission | `getauxblock(hash, auxpow)` | `submitblock(block_hex)` |
| BIP 22/23 Compliant | ❌ No | ✅ Yes |

### Implementation Status

#### ✅ Completed
1. **Segwit/MWEB Support**: Added conditional rules to getblocktemplate
2. **Universal Coind Naming**: Renamed dashd→coind across entire codebase
3. **Transaction Parsing**: Fixed LateEnd errors with proper Segwit support
4. **Dynamic Helper Selection**: Automatically selects bitcoin/litecoin helper based on network
5. **P2Pool Startup**: Successfully starts and connects to Litecoin testnet
6. **Address Generation**: Created all necessary testnet addresses
7. **Modified Dogecoin**: Found, verified, and started auxpow-capable daemon
8. **Auxpow Testing**: Verified getblocktemplate with auxpow capability works

#### 🔄 In Progress
1. **Merged Mining Refactoring**: Transitioning from getauxblock to getblocktemplate with auxpow

#### ⏳ Pending
1. **Detection and Compatibility**: Add auxpow capability detection to P2Pool
2. **Coinbase Building**: Implement multiaddress coinbase construction
3. **Block Building**: Implement complete Dogecoin block construction
4. **Submission Logic**: Update to use submitblock instead of getauxblock
5. **Integration Testing**: Test complete merged mining flow
6. **Production Deployment**: Deploy to remote server and monitor

### Current Merged Mining Implementation

#### How getauxblock Works (Current)
```python
# Get work
auxblock = yield merged_proxy.rpc_getauxblock()
# Returns: {"hash": "...", "target": "...", "chainid": 98}

# Store work
self.merged_work.set({chainid: {
    'hash': int(auxblock['hash'], 16),
    'target': unpack(auxblock['target']),
    'merged_proxy': merged_proxy,
}})

# Submit solved block
merged_proxy.rpc_getauxblock(
    hash_hex,  # Block hash
    auxpow_hex # Auxpow proof
)
```

**Limitations:**
- Pool operator provides single Dogecoin address to daemon
- All Dogecoin rewards go to that one address
- Miners only get paid in Litecoin
- Pool operator must manually distribute Dogecoin

#### How getblocktemplate with auxpow Works (Target)
```python
# Get template with auxpow capability
template = yield merged_proxy.rpc_getblocktemplate({"capabilities": ["auxpow"]})
# Returns: Full block template including auxpow object

# Build coinbase with multiple outputs (one per shareholder)
coinbase_outputs = []
for miner_address, share_fraction in shareholders:
    amount = template['coinbasevalue'] * share_fraction
    coinbase_outputs.append({'address': miner_address, 'value': amount})

# Build complete Dogecoin block
dogecoin_block = build_block(
    template=template,
    coinbase=build_tx(outputs=coinbase_outputs),
    auxpow=build_auxpow_proof(litecoin_block),
)

# Submit complete block
merged_proxy.rpc_submitblock(dogecoin_block.encode('hex'))
```

**Benefits:**
- Each miner provides both Litecoin AND Dogecoin addresses
- Dogecoin rewards distributed proportionally based on shares
- Trustless: No pool operator custody
- Fair: Same payout model for both chains

### File Locations

#### Local Development
- **P2Pool**: /home/user0/Documents/GitHub/p2pool-dash
- **Reference Repo**: /home/user0/Documents/GitHub/jtoomim-p2pool
- **Modified Dogecoin Repo**: /home/user0/dogecoin-auxpow-gbt

#### Remote Server (192.168.80.182)
- **P2Pool**: /home/user0/p2pool-dash
- **Dogecoin Binaries**: /home/user0/bin-auxpow/
- **Dogecoin Libraries**: /home/user0/lib/
- **Startup Script**: /home/user0/start-dogecoin-auxpow.sh
- **Dogecoin Source**: /home/user0/dogecoin-auxpow-gbt (not built on server)
- **Litecoin Binaries**: /home/user0/bin/
- **Blockchain Data**:
  - Litecoin: /home/user0/.litecoin/testnet4/
  - Dogecoin: /home/user0/.dogecoin/testnet3/

### Key Code Files

#### Modified for Segwit Support
- `p2pool/dash/helper.py` - Added conditional segwit/mweb rules
- `p2pool/bitcoin/data.py` - Segwit-aware transaction parsing (from jtoomim)
- `p2pool/litecoin/helper.py` - Scrypt-specific helper with Segwit support
- `p2pool/node.py` - Dynamic helper selection based on SOFTFORKS_REQUIRED

#### Needs Modification for Auxpow
- `p2pool/work.py` - Lines 87-98: set_merged_work() function
- `p2pool/work.py` - Lines 475-495: Block submission logic
- `p2pool/merged_mining.py` - NEW: Helper functions for multiaddress coinbase

#### Configuration Files
- `start_p2pool_scrypt_testnet.sh` - Startup script with merged mining URL
- `p2pool/networks/dash_testnet.py` - Network configuration (if needed)
- `MERGED_MINING_REFACTOR_PLAN.md` - Detailed implementation plan

### Testing Checklist

#### ✅ Completed Tests
- [x] Litecoin Core RPC connectivity
- [x] Dogecoin Core RPC connectivity (auxpow version)
- [x] P2Pool startup with Litecoin testnet
- [x] Segwit transaction parsing
- [x] Address generation for both chains
- [x] getblocktemplate with auxpow capability
- [x] Modified Dogecoin daemon functionality

#### ⏳ Pending Tests
- [ ] Auxpow capability detection in P2Pool
- [ ] Multiaddress coinbase construction
- [ ] Complete Dogecoin block building
- [ ] Block submission via submitblock
- [ ] Share chain integration with multiaddress
- [ ] End-to-end merged mining flow
- [ ] Payout verification on both chains
- [ ] 24-hour stability test

### Next Steps (Prioritized)

#### Immediate (Today)
1. **Implement Auxpow Detection** (1 hour)
   - Modify `set_merged_work()` to try getblocktemplate first
   - Detect auxpow capability in response
   - Store flag for multiaddress support
   - Fallback to getauxblock if not supported

2. **Create Helper Module** (1 hour)
   - Create `p2pool/merged_mining.py`
   - Implement address decoding for Dogecoin
   - Implement coinbase script building
   - Add unit tests

#### Short Term (This Week)
3. **Build Coinbase Constructor** (2 hours)
   - Implement `build_merged_coinbase()` function
   - Calculate output amounts based on share fractions
   - Handle multiple outputs correctly
   - Test with mock shareholder data

4. **Build Block Constructor** (2 hours)
   - Implement `build_merged_block()` function
   - Integrate template transactions
   - Calculate merkle root
   - Attach auxpow proof

5. **Update Submission Logic** (1 hour)
   - Modify block submission in work.py
   - Add submitblock call for multiaddress
   - Keep getauxblock for backward compatibility
   - Add error handling

#### Medium Term (Next Week)
6. **Integration Testing** (3 hours)
   - Test with regtest first
   - Test with testnet
   - Connect real miner
   - Verify payouts

7. **Production Deployment** (2 hours)
   - Deploy to remote server
   - Update startup scripts
   - Monitor logs
   - Document procedures

### Known Issues

1. **None currently** - All previous issues resolved ✅

### Risk Factors

1. **Block Validation**: Dogecoin may reject blocks with incorrect format
   - **Mitigation**: Extensive testing on regtest before testnet

2. **Merkle Root Calculation**: Complex with multiple transactions
   - **Mitigation**: Use existing P2Pool merkle functions

3. **Share Chain Sync**: Need current shareholder state for coinbase
   - **Mitigation**: Cache recent state, use snapshot approach

4. **Performance**: Building complete blocks may be slower
   - **Mitigation**: Profile and optimize critical paths

### Resources

#### Documentation
- **Modified Dogecoin Impl**: /home/user0/dogecoin-auxpow-gbt/IMPLEMENTATION_SUMMARY.md
- **Refactor Plan**: /home/user0/Documents/GitHub/p2pool-dash/MERGED_MINING_REFACTOR_PLAN.md
- **Address Guide**: /home/user0/Documents/GitHub/p2pool-dash/TESTNET_ADDRESSES.md
- **Test Report**: /home/user0/dogecoin-auxpow-gbt/TEST_REPORT.md

#### Reference Code
- **jtoomim P2Pool**: /home/user0/Documents/GitHub/jtoomim-p2pool
- **Modified Dogecoin**: /home/user0/dogecoin-auxpow-gbt/src/rpc/mining.cpp (lines 370-781)
- **P2Pool Work Module**: /home/user0/Documents/GitHub/p2pool-dash/p2pool/work.py

#### Key Git Commits
- **Segwit/MWEB Support**: Local changes in feature/scrypt-litecoin-dogecoin
- **Coind Rename**: 197 occurrences across 9 files
- **Auxpow Feature**: dogecoin-auxpow-gbt@436b09bb8

### Success Metrics

When implementation is complete, we should see:

1. **P2Pool Startup**: Successfully connects to both Litecoin and Dogecoin (auxpow)
2. **Template Retrieval**: Gets auxpow-capable templates from Dogecoin
3. **Coinbase Construction**: Builds multiaddress coinbase with shareholder outputs
4. **Block Mining**: Miners work on Litecoin blocks as usual
5. **Block Submission**: Successfully submits blocks to both chains when targets met
6. **Payout Distribution**: Each miner receives Litecoin AND Dogecoin proportionally
7. **Stability**: Runs continuously without errors for 24+ hours

### Estimated Time to Completion

- **Phase 1** (Detection): 1 hour
- **Phase 2** (Coinbase): 2-3 hours
- **Phase 3** (Block Building): 2-3 hours
- **Phase 4** (Submission): 1-2 hours
- **Phase 5** (Integration): 2-3 hours
- **Phase 6** (Production): 2-3 hours

**Total**: ~10-15 hours of development + testing

### Contact Information

- **User**: user0@192.168.80.182
- **Modified Dogecoin Repo**: frstrtr/dogecoin-auxpow-gbt
- **P2Pool Repo**: Local fork with feature/scrypt-litecoin-dogecoin branch

---

## Summary

✅ **Infrastructure Ready**: Both Litecoin and modified Dogecoin (with auxpow) are running
✅ **Feature Verified**: getblocktemplate with auxpow capability confirmed working
✅ **P2Pool Functional**: Successfully starts and mines Litecoin
⏳ **Implementation Pending**: Need to refactor merged mining code for multiaddress support

**Next Action**: Begin Phase 1 implementation (auxpow detection and compatibility)
