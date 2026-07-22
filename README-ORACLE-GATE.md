# Oracle-Gate Instrumented p2pool-dash Node

A controlled, instrumented p2pool-dash node that joins the **live public
p2pool-dash mainnet sharechain** and acts as three things at once:

1. **Byte-parity oracle** — logs the raw serialized bytes of every share it
   sees, plus the exact expected-vs-received byte diff whenever a share fails
   verification. These are your KAT (known-answer-test) vectors.
2. **Peer / miner discovery scanner** — logs every peer handshake and the
   payout script carried by every share, building a live peer + miner map.
3. **Permissive gate** — for an allowlist of peer IPs (or globally), it will
   **not ban or disconnect** a peer that sends a bad/unrecognized share or
   trips a protocol quirk. The bad share is still kept out of the sharechain,
   but the connection stays alive so a peer can keep iterating against it.

All instrumentation is **OFF by default** and only activates when the
`P2POOL_ORACLE_GATE=1` environment variable is set. With the flag unset, the
node behaves byte-identically to vanilla p2pool-dash, so you can run vanilla
and instrumented builds from the same checkout.

---

## What changed (all gated)

| Feature | File | Behavior when `P2POOL_ORACLE_GATE=1` |
| --- | --- | --- |
| Verbose peer handshake log | `p2pool/p2p.py` (`handle_version`) | Emits `PEER_HANDSHAKE` per connect |
| Verbose share-received log | `p2pool/p2p.py` (`handle_shares`) | Emits `SHARE_RAW` + `SHARE_RECV` per share |
| Verify pass/fail log | `p2pool/data.py` (`attempt_verify`) | Emits `VERIFY_PASS` / `VERIFY_FAIL` |
| Expected-vs-received byte diff | `p2pool/data.py` (`Share.check`) | Emits `VERIFY_DIFF` on mismatch |
| Log-instead-of-ban | `p2pool/p2p.py` (`badPeerHappened`, `handle_shares`) | Emits `NOBAN`, keeps connection alive |
| Startup banner | `p2pool/main.py` | Prints config + emits `STARTUP` |

The instrumentation itself lives in one new module, `p2pool/oracle_gate.py`.

---

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `P2POOL_ORACLE_GATE` | *(unset)* | `1` turns everything on. Unset = vanilla. |
| `P2POOL_ORACLE_LOG` | `~/oracle-gate.log` | Log destination file. |
| `P2POOL_GATE_NOBAN_IPS` | *(empty)* | Comma-separated allowlist of peer IPs that must never be banned/dropped for a bad share. When set, **only** these IPs are exempt. |
| `P2POOL_GATE_NOBAN_ALL` | *(see note)* | `1` = never ban/drop **any** peer for a bad share. |

**No-ban default:** when the gate is on and no `P2POOL_GATE_NOBAN_IPS` list is
provided, the node is globally permissive (equivalent to
`P2POOL_GATE_NOBAN_ALL=1`) — that is the intended posture for a controlled
diagnostic node. Provide `P2POOL_GATE_NOBAN_IPS` to narrow the exemption to
just your own c2pool node IPs while banning everyone else normally.

---

## Prerequisites

- **Python 2.7** with Twisted (this is legacy python2 p2pool).
- A fully-synced **dashd** (mainnet) reachable over both **RPC** and **P2P**,
  with RPC credentials. Minimal `~/.dashcore/dash.conf`:

  ```ini
  server=1
  rpcuser=YOUR_RPC_USER
  rpcpassword=YOUR_RPC_PASSWORD
  rpcport=9998        # mainnet RPC (PARENT.RPC_PORT)
  # dashd mainnet P2P is 9999 (PARENT.P2P_PORT)
  txindex=1
  ```

---

## Running on mainnet

The p2pool-dash **sharechain** P2P port is **8999** (`--p2pool-port`), and the
network auto-bootstraps against the live seed `rov.p2p-spb.xyz`
(`BOOTSTRAP_ADDRS` in `p2pool/networks/dash.py`). You can additionally pin the
public net with `-n`.

### Fully permissive controlled node (global no-ban)

```bash
export P2POOL_ORACLE_GATE=1
export P2POOL_ORACLE_LOG=$HOME/oracle-gate.log
# no allowlist -> globally permissive

python2 run_p2pool.py \
    --net dash \
    --dashd-address 127.0.0.1 \
    --dashd-rpc-port 9998 \
    --dashd-p2p-port 9999 \
    --p2pool-port 8999 \
    -a XyourDashPayoutAddressHere \
    -n rov.p2p-spb.xyz:8999 \
    YOUR_RPC_USER YOUR_RPC_PASSWORD
```

### Gate only your c2pool node IPs (ban everyone else normally)

```bash
export P2POOL_ORACLE_GATE=1
export P2POOL_GATE_NOBAN_IPS=203.0.113.10,203.0.113.11   # your c2pool nodes
python2 run_p2pool.py --net dash --p2pool-port 8999 \
    --dashd-rpc-port 9998 --dashd-p2p-port 9999 \
    -a XyourDashPayoutAddressHere \
    -n rov.p2p-spb.xyz:8999 \
    YOUR_RPC_USER YOUR_RPC_PASSWORD
```

Point your c2pool node's sharechain peer at this node's `8999`. Because the
oracle-gate keeps the connection alive on rejected shares, c2pool can keep
resubmitting corrected shares and you can watch the `VERIFY_DIFF` lines close
the gap.

On startup you will see:

```
*** ORACLE-GATE INSTRUMENTATION ACTIVE ***
    log file: /home/you/oracle-gate.log
    no-ban:   ALL peers
```

---

## Reading the log

The log is **tab-separated and greppable**. Each line:

```
<iso-timestamp>\t<epoch>\t<CATEGORY>\t<key=value>\t<key=value> ...
```

### Categories

- **`STARTUP`** — gate config at boot.
- **`PEER_HANDSHAKE`** — `addr`, `incoming`, `protocol_version`, `sub_version`
  (user-agent), `services`, `best_share_hash` (peer's advertised tip). The
  live-peer map. A peer's payout/miner addresses are not in the handshake —
  they are derived from the `payout_script` field of the shares it sends.
- **`SHARE_RAW`** — `addr`, `share_version`, `raw` (full hex of the serialized
  share, **before** decode; survives decode/PoW failures). Your KAT corpus.
- **`SHARE_RECV`** — decoded share: `share_hash`, `share_version`,
  `previous_hash`, `gentx_txid`, `payout_script` (hex — the miner this share
  pays), `payout_pubkey_hash`, `donation`, `new_tx_count`.
- **`VERIFY_PASS` / `VERIFY_FAIL`** — `share_hash`, `peer`, and (on fail)
  `reason` (which check failed).
- **`VERIFY_DIFF`** — the whole point. Emitted when a share fails a specific
  verification check, with **both** sides:
  - `check=share_info` → `expected` vs `received` (hex of packed share_info).
  - `check=gentx` → `expected_gentx_txid` vs `received_gentx_txid`, plus
    `expected_gentx_bytes` (hex of the coinbase/gentx our node computed).
  - `check=merkle_link` → `expected` vs `received` merkle link.
  `expected` = what **our** node (this vanilla oracle) computed. `received` =
  what the peer (e.g. c2pool) sent. The delta is exactly what c2pool must fix.
- **`NOBAN`** — a ban/disconnect that was suppressed: `addr`, `reason`,
  `action`. Confirms the gate kept a connection alive.

### Useful greps

```bash
# Everything a specific c2pool node sent, in order
grep -P '\t(SHARE_RECV|VERIFY_FAIL|VERIFY_DIFF|NOBAN)\t' ~/oracle-gate.log | grep 203.0.113.10

# All byte diffs (the fix list)
grep -P '\tVERIFY_DIFF\t' ~/oracle-gate.log

# Extract the expected gentx bytes for a failing share
grep -P '\tVERIFY_DIFF\t' ~/oracle-gate.log | grep 'check=gentx'

# Live miner map (payout scripts seen)
grep -P '\tSHARE_RECV\t' ~/oracle-gate.log | grep -oP 'payout_script=\S+' | sort -u

# Confirm nobody got banned
grep -P '\tNOBAN\t' ~/oracle-gate.log
```

### Reconstructing a KAT vector

For a share that fails `check=gentx`, take the `SHARE_RAW` `raw=` hex for that
`share_hash` (the exact bytes the peer sent) together with the `VERIFY_DIFF`
`expected_gentx_bytes=` (the coinbase our node expected). Feeding the raw share
back through `p2pool.data.Share` reproduces `received_gentx_txid`; the target
c2pool must produce `expected_gentx_txid`. That pair is a byte-parity KAT.

---

## Safety notes

- This node **never accepts an invalid share** into the sharechain. The gate
  only changes whether the *connection* is dropped, never whether a bad share
  is trusted.
- With the flag unset the node is byte-unchanged from vanilla, so you can run a
  vanilla node from the same checkout for comparison.
- Being globally permissive (`NOBAN_ALL`) means this node will not defend
  itself against a spamming peer by banning. Run it as a controlled diagnostic
  node, and prefer `P2POOL_GATE_NOBAN_IPS` in the field.
