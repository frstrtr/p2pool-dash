#!/usr/bin/env python3
"""
P2Pool Merged Mining Monitor
Displays real-time dashboard of Dogecoin testnet mining activity
"""

import subprocess
import json
import time
import sys
from datetime import datetime
from collections import deque
import hashlib

# Configuration
DOGE_RPC_USER = "dogeuser"
DOGE_RPC_PASS = "dogepass123"
DOGE_RPC_URL = "http://127.0.0.1:44555/"
P2POOL_LOG = "/tmp/p2pool_new.log"
MINING_ADDRESS = "mm3suEPoj1WnhYuRTdoM6dfEXQvZEyuu9h"
EXPLORER_URL = "https://blockexplorer.one/dogecoin/testnet"
SSH_HOST = "user0@192.168.80.182"

# P2Pool donation script (from merged_mining.py)
DONATION_SCRIPT = "4104ffd03de44a6e11b9917f3a29f9443283d9871c9d743ef30d5eddcd37094b64d1b3d8090496b53256786bf5c82932ec23c3b74d9f05a6f95a8b5529352656664bac"
# Donation address (derived from pubkey): 1BHCtLJRhWftUQT9RZmhEBYx6QXJZbXRKL (mainnet) or testnet equivalent

# Track recent candidates
recent_candidates = deque(maxlen=10)
last_candidate_count = 0
candidate_times = deque(maxlen=100)  # Track timestamps for hashrate calculation

def run_ssh_command(cmd):
    """Execute command on remote host via SSH"""
    result = subprocess.run(
        ["ssh", SSH_HOST, cmd],
        capture_output=True,
        text=True,
        timeout=10
    )
    return result.stdout.strip()

def rpc_call(method, params=[]):
    """Make RPC call to Dogecoin daemon"""
    cmd = f'curl -s --user {DOGE_RPC_USER}:{DOGE_RPC_PASS} --data-binary \'{{"jsonrpc":"1.0","id":"monitor","method":"{method}","params":{json.dumps(params)}}}\' -H "content-type: text/plain;" {DOGE_RPC_URL}'
    result = run_ssh_command(cmd)
    try:
        return json.loads(result)["result"]
    except:
        return None

def get_network_info():
    """Get current network mining info"""
    return rpc_call("getmininginfo")

def get_block_count():
    """Get current block height"""
    return rpc_call("getblockcount")

def get_balance():
    """Get wallet balance"""
    return rpc_call("getbalance")

def check_recent_blocks(num_blocks=10):
    """Check recent blocks for our mined blocks (optimized for speed)"""
    current_height = get_block_count()
    if not current_height:
        return []
    
    found_blocks = []
    # Only scan last 10 blocks for speed over SSH
    for i in range(min(num_blocks, 10)):
        height = current_height - i
        block_hash = rpc_call("getblockhash", [height])
        if not block_hash:
            continue
            
        block = rpc_call("getblock", [block_hash, 2])
        if not block:
            continue
        
        coinbase_tx = block["tx"][0]
        vouts = coinbase_tx.get("vout", [])
        
        # Check if our address is in outputs
        for vout in vouts:
            addresses = vout.get("scriptPubKey", {}).get("addresses", [])
            if MINING_ADDRESS in addresses:
                found_blocks.append({
                    "height": height,
                    "hash": block_hash[:16] + "...",
                    "confirmations": current_height - height + 1,
                    "outputs": len(vouts),
                    "time": datetime.fromtimestamp(block["time"]).strftime("%H:%M:%S"),
                    "explorer": f"{EXPLORER_URL}/block/{block_hash}"
                })
                break
    
    return found_blocks

def get_candidate_info():
    """Get block candidate information from P2Pool log"""
    global last_candidate_count, recent_candidates, candidate_times
    
    # Count total candidates
    count_cmd = f'grep -c "Dogecoin block candidate" {P2POOL_LOG} 2>/dev/null || echo 0'
    total_candidates = int(run_ssh_command(count_cmd))
    
    # Get recent candidates if count increased
    if total_candidates > last_candidate_count:
        candidates_cmd = f'grep "Dogecoin block candidate" {P2POOL_LOG} | tail -20'
        candidates_output = run_ssh_command(candidates_cmd)
        
        for line in candidates_output.split('\n'):
            if 'pow_hash=' in line and 'ratio=' in line:
                try:
                    # Parse timestamp
                    timestamp_str = line.split('>')[0].strip()
                    timestamp = timestamp_str.split()[1]
                    
                    # Parse full datetime for hashrate calculation
                    dt = datetime.strptime(timestamp_str.split('>')[0].strip(), "%Y-%m-%d %H:%M:%S.%f")
                    candidate_times.append(dt)
                    
                    # Parse hash and ratio
                    pow_hash = line.split('pow_hash=')[1].split()[0][:16] + "..."
                    ratio = line.split('ratio=')[1].split('%')[0]
                    
                    candidate_info = {
                        "time": timestamp,
                        "hash": pow_hash,
                        "ratio": float(ratio)
                    }
                    
                    # Add if not already in recent list
                    if candidate_info not in recent_candidates:
                        recent_candidates.append(candidate_info)
                except:
                    continue
        
        last_candidate_count = total_candidates
    
    return total_candidates, list(recent_candidates)

def calculate_local_hashrate(network_diff):
    """Calculate approximate local hashrate based on candidate finding rate"""
    if len(candidate_times) < 2:
        return 0
    
    # Calculate time span
    time_span = (candidate_times[-1] - candidate_times[0]).total_seconds()
    if time_span < 1:
        return 0
    
    # Candidates per second
    candidates_per_sec = len(candidate_times) / time_span
    
    # Estimate hashrate: candidates/sec * (2^32 * difficulty)
    # This is approximate based on probability of finding blocks
    if network_diff > 0:
        hashrate = candidates_per_sec * (2**32 * network_diff) / 1000  # Convert to KH/s
        return max(0, hashrate)
    
    return 0

def get_donation_balance(mined_blocks):
    """Get donation outputs from blocks we actually mined"""
    try:
        if not mined_blocks:
            return {
                "outputs": 0,
                "total": 0,
                "scanned_blocks": 0
            }
        
        donation_outputs = 0
        donation_total = 0
        
        # Only check blocks we mined (much faster)
        for block_info in mined_blocks:
            height = block_info['height']
            block_hash = rpc_call("getblockhash", [height])
            if not block_hash:
                continue
            
            block = rpc_call("getblock", [block_hash, 2])
            if not block:
                continue
            
            # Check coinbase transaction for donation output
            coinbase_tx = block["tx"][0]
            vouts = coinbase_tx.get("vout", [])
            
            # Look for outputs with our donation script
            for vout in vouts:
                script_hex = vout.get("scriptPubKey", {}).get("hex", "")
                if script_hex == DONATION_SCRIPT:
                    donation_outputs += 1
                    donation_total += vout.get("value", 0)
        
        return {
            "outputs": donation_outputs,
            "total": donation_total,
            "scanned_blocks": len(mined_blocks)
        }
    except:
        return {
            "outputs": 0,
            "total": 0,
            "scanned_blocks": 0
        }

def clear_screen():
    """Clear terminal screen and reset cursor"""
    import sys
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()

def print_header():
    """Print dashboard header"""
    print("=" * 80)
    print(" P2POOL DOGECOIN TESTNET MERGED MINING MONITOR".center(80))
    print("=" * 80)
    print()

def print_network_stats(info):
    """Print network statistics"""
    if not info:
        print("⚠️  Unable to fetch network info")
        return
    
    print("📊 NETWORK STATS")
    print("-" * 80)
    print(f"  Block Height:     {info['blocks']:,}")
    print(f"  Difficulty:       {info['difficulty']:.10f}")
    print(f"  Network Hashrate: {info['networkhashps']/1000:.1f} KH/s")
    print(f"  Chain:            {info.get('chain', 'unknown')}")
    print()

def print_mining_stats(candidates, balance, hashrate, donation_info):
    """Print our mining statistics"""
    print("⛏️  MINING STATS")
    print("-" * 80)
    print(f"  Total Candidates: {candidates}")
    print(f"  Local Hashrate:   {hashrate:.2f} KH/s (estimated)")
    print(f"  Wallet Balance:   {balance:,.1f} DOGE")
    
    if donation_info:
        print(f"\n  💰 P2Pool Donation Fund (last {donation_info['scanned_blocks']} blocks):")
        print(f"     Outputs Found:  {donation_info['outputs']}")
        print(f"     Total Donated:  {donation_info['total']:,.1f} DOGE")
    else:
        print(f"\n  💰 P2Pool Donation Fund: Scanning...")
    print()

def print_recent_candidates(candidates_list):
    """Print recent block candidates"""
    print("🎯 RECENT CANDIDATES (Last 10)")
    print("-" * 80)
    if not candidates_list:
        print("  No candidates yet...")
    else:
        print(f"  {'Time':<12} {'Hash':<22} {'% to Target':>12}")
        print("  " + "-" * 78)
        for c in reversed(candidates_list):  # Most recent first
            ratio_str = f"{c['ratio']:.2f}%"
            print(f"  {c['time']:<12} {c['hash']:<22} {ratio_str:>12}")
    print()

def print_mined_blocks(blocks):
    """Print our mined blocks"""
    print("✅ MINED BLOCKS (Last 10 blocks scanned)")
    print("-" * 80)
    if not blocks:
        print("  No blocks found in last 10 blocks")
        print("  (Testnet competition: ~658 KH/s, 0.26s block time)")
    else:
        print(f"  {'Height':<10} {'Time':<10} {'Confs':<7} {'Outs':<5} {'Block Hash':<20}")
        print("  " + "-" * 78)
        for b in blocks[:5]:  # Show top 5
            print(f"  {b['height']:<10} {b['time']:<10} {b['confirmations']:<7} {b['outputs']:<5} {b['hash']:<20}")
            print(f"     🔗 {b['explorer']}")
    print()

def print_footer(update_time):
    """Print dashboard footer"""
    print("-" * 80)
    print(f"Last update: {update_time.strftime('%Y-%m-%d %H:%M:%S')} | Press Ctrl+C to exit")
    print("=" * 80)

def main():
    """Main monitoring loop"""
    # Initial startup message (only shown once)
    clear_screen()
    print("Starting P2Pool mining monitor...")
    print("Connecting to remote server...")
    print("Please wait...")
    time.sleep(2)
    
    try:
        iteration = 0
        while True:
            try:
                # Fetch data silently
                network_info = get_network_info()
                balance = get_balance()
                total_candidates, recent = get_candidate_info()
                
                # Calculate local hashrate
                network_diff = network_info.get('difficulty', 0) if network_info else 0
                local_hashrate = calculate_local_hashrate(network_diff)
                
                # Check for mined blocks (fast scan)
                mined_blocks = check_recent_blocks(10)
                
                # Check donation balance in our mined blocks
                donation_info = get_donation_balance(mined_blocks)
                
                # Now clear and display everything at once
                clear_screen()
                print_header()
                print_network_stats(network_info)
                print_mining_stats(total_candidates, balance, local_hashrate, donation_info)
                print_recent_candidates(recent)
                print_mined_blocks(mined_blocks)
                print_footer(datetime.now())
                
                iteration += 1
                
                # Wait before next refresh
                time.sleep(5)
                
            except KeyboardInterrupt:
                raise
            except Exception as e:
                clear_screen()
                print_header()
                print(f"\n⚠️  Error: {e}")
                print("Retrying in 5 seconds...")
                time.sleep(5)
                
    except KeyboardInterrupt:
        print("\n\nMonitoring stopped by user")
        sys.exit(0)

if __name__ == "__main__":
    main()
