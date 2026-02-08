#!/usr/bin/env python3
"""
Check all shares on the p2pool sharechain to find any that reached blockchain difficulty.

A share "reaches blockchain difficulty" when the share hash is below the network target
(i.e., the share's proof-of-work is strong enough to be a valid block on the Dash blockchain).
"""

import json
import sys
import time
import urllib.request

NODE_URL = "http://192.168.86.24:7903"

def fetch_json(url, retries=5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt
                if attempt < retries - 1:
                    time.sleep(wait)
                    continue
            print(f"Error fetching {url}: {e}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"Error fetching {url}: {e}", file=sys.stderr)
            return None
    return None

def target_to_difficulty(target):
    if target == 0:
        return float('inf')
    # Standard Bitcoin difficulty calculation
    return 26959535291011309493156476344723991336010898738574164086137773096960.0 / target

def main():
    # Get global stats for reference
    stats = fetch_json(f"{NODE_URL}/global_stats")
    if stats:
        net_diff = stats['network_block_difficulty']
        min_diff = stats['min_difficulty']
        print(f"Network block difficulty: {net_diff:,.2f}")
        print(f"P2Pool min share difficulty: {min_diff:,.2f}")
        print(f"Ratio (net/share): {net_diff/min_diff:,.0f}x")
        print()

    # Get the best share hash (tip of sharechain)
    best = fetch_json(f"{NODE_URL}/web/best_share_hash")
    if not best:
        print("Could not get best share hash!")
        return

    print(f"Best share hash: {best}")
    print(f"Walking the sharechain...\n")

    current_hash = best
    total_shares = 0
    shares_at_blockchain_diff = []
    start_time = time.time()
    batch_count = 0

    while current_hash and current_hash != '0' * 64:
        share = fetch_json(f"{NODE_URL}/web/share/{current_hash}")
        if not share:
            print(f"Could not fetch share {current_hash}, stopping.")
            break

        total_shares += 1

        # The share hash itself
        share_hash = share['block']['hash']

        # The share hash as an integer (lower = more work)
        share_hash_int = int(share_hash, 16)

        # Network target from the block header (what the blockchain requires)
        network_target = share['block']['header']['target']

        # Share target (what p2pool requires for a share)
        share_target = share['share_data']['target']
        max_target = share['share_data']['max_target']

        # Share difficulty vs network difficulty
        share_diff = target_to_difficulty(share_target)
        network_diff = target_to_difficulty(network_target)

        # A share reaches blockchain difficulty if its hash (as integer) 
        # is LESS THAN OR EQUAL to the network target
        reached_blockchain = share_hash_int <= network_target

        if reached_blockchain:
            shares_at_blockchain_diff.append({
                'hash': share_hash,
                'share_diff': share_diff,
                'network_diff': network_diff,
                'timestamp': share['share_data']['timestamp'],
                'payout_address': share['share_data']['payout_address'],
                'absheight': share['share_data']['absheight'],
                'subsidy': share['block']['gentx']['value'],
            })
            print(f"  *** BLOCK FOUND! Share #{total_shares} (absheight {share['share_data']['absheight']})")
            print(f"      Hash: {share_hash}")
            print(f"      Share diff: {share_diff:,.2f}")
            print(f"      Network diff: {network_diff:,.2f}")
            print(f"      Payout: {share['share_data']['payout_address']}")
            ts = share['share_data']['timestamp']
            print(f"      Time: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts))} UTC")
            print()

        # Progress reporting
        if total_shares % 100 == 0:
            elapsed = time.time() - start_time
            rate = total_shares / elapsed if elapsed > 0 else 0
            print(f"  ... checked {total_shares} shares ({rate:.1f} shares/sec), "
                  f"found {len(shares_at_blockchain_diff)} blocks so far, "
                  f"current absheight: {share['share_data']['absheight']}")

        # Move to parent
        parent = share.get('parent')
        if not parent or parent == '0' * 64:
            break
        current_hash = parent
        
        # Small delay to avoid rate limiting
        time.sleep(0.05)

    elapsed = time.time() - start_time
    print(f"\n{'='*70}")
    print(f"RESULTS")
    print(f"{'='*70}")
    print(f"Total shares checked: {total_shares}")
    print(f"Time elapsed: {elapsed:.1f} seconds")
    print(f"Shares that reached blockchain difficulty: {len(shares_at_blockchain_diff)}")

    if shares_at_blockchain_diff:
        print(f"\nShares that were valid blocks:")
        for i, s in enumerate(shares_at_blockchain_diff, 1):
            ts = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(s['timestamp']))
            print(f"\n  Block #{i}:")
            print(f"    Share hash:    {s['hash']}")
            print(f"    Absheight:     {s['absheight']}")
            print(f"    Share diff:    {s['share_diff']:,.2f}")
            print(f"    Network diff:  {s['network_diff']:,.2f}")
            print(f"    Block reward:  {s['subsidy']} DASH")
            print(f"    Payout addr:   {s['payout_address']}")
            print(f"    Timestamp:     {ts} UTC")
    else:
        print("\nNo shares in the current sharechain reached blockchain difficulty.")
        if stats:
            ratio = stats['network_block_difficulty'] / stats['min_difficulty']
            print(f"(Network difficulty is {ratio:,.0f}x higher than share difficulty, "
                  f"so roughly 1 in {ratio:,.0f} shares would be expected to find a block)")

if __name__ == '__main__':
    main()
