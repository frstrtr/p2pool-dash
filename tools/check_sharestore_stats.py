#!/usr/bin/env pypy
"""
Statistical analysis: is the block drought since Feb 4 just bad luck?

Computes expected block rate from pool hashrate vs network difficulty,
then calculates the Poisson probability of the observed dry spell.
"""

import os
import sys
import time
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from p2pool import data as p2pool_data
from p2pool.dash import data as dash_data
from p2pool.util import pack
from p2pool import networks

def target_to_difficulty(target):
    if target == 0:
        return float('inf')
    return 26959535291011309493156476344723991336010898738574164086137773096960.0 / target

def target_to_average_attempts(target):
    if target >= 2**256:
        return 0
    return 2**256 // (target + 1)

def main():
    share_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'dash')

    share_files = sorted([
        os.path.join(share_dir, f)
        for f in os.listdir(share_dir)
        if f.startswith('shares.') and not f.endswith('.bak')
    ])

    net = networks.nets['dash']
    BLOCK_PERIOD = net.PARENT.BLOCK_PERIOD  # Dash: 150 seconds (2.5 min)

    all_shares = []
    blocks_found = []

    print "Loading all shares from store..."
    t0 = time.time()

    for filepath in share_files:
        with open(filepath, 'rb') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parts = line.split(' ', 1)
                    if len(parts) != 2:
                        continue
                    type_id = int(parts[0])
                    if type_id != 5:
                        continue
                    raw_share = p2pool_data.share_type.unpack(parts[1].decode('hex'))
                    if raw_share['type'] < p2pool_data.Share.VERSION:
                        continue
                    share = p2pool_data.load_share(raw_share, net, None)

                    blockchain_target = share.header['bits'].target
                    is_block = share.pow_hash <= blockchain_target
                    net_diff = target_to_difficulty(blockchain_target)
                    share_work = target_to_average_attempts(share.target)

                    info = {
                        'timestamp': share.timestamp,
                        'share_target': share.target,
                        'blockchain_target': blockchain_target,
                        'net_diff': net_diff,
                        'share_work': share_work,
                        'is_block': is_block,
                        'pow_hash': share.pow_hash,
                        'absheight': share.absheight,
                    }
                    all_shares.append(info)
                    if is_block:
                        blocks_found.append(info)
                except Exception:
                    pass

    elapsed = time.time() - t0
    print "Loaded %d shares in %.1f seconds\n" % (len(all_shares), elapsed)

    # Sort by timestamp
    all_shares.sort(key=lambda s: s['timestamp'])
    blocks_found.sort(key=lambda s: s['timestamp'])

    if not all_shares:
        print "No shares found!"
        return

    first_ts = all_shares[0]['timestamp']
    last_ts = all_shares[-1]['timestamp']
    total_span_hours = (last_ts - first_ts) / 3600.0

    print "=" * 70
    print "SHARECHAIN OVERVIEW"
    print "=" * 70
    print "Total shares:     %d" % len(all_shares)
    print "Time span:        %s to %s" % (
        time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(first_ts)),
        time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(last_ts)))
    print "Duration:         %.1f hours (%.1f days)" % (total_span_hours, total_span_hours / 24)
    print "Blocks found:     %d" % len(blocks_found)

    if blocks_found:
        last_block_ts = blocks_found[-1]['timestamp']
        drought_hours = (last_ts - last_block_ts) / 3600.0
        print "\nLast block found: %s" % time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(last_block_ts))
        print "Latest share:     %s" % time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(last_ts))
        print "Drought duration: %.1f hours (%.1f days)" % (drought_hours, drought_hours / 24)

    # === METHOD 1: Sum of per-share block probabilities ===
    # For each share, P(block) = share_target / blockchain_target
    # (since share hash is uniform in [0, share_target], and block if hash <= blockchain_target)
    # Actually: P(block) = min(1, (blockchain_target+1) / (share_target+1))

    print "\n" + "=" * 70
    print "STATISTICAL ANALYSIS"
    print "=" * 70

    # Split into time periods for analysis
    if blocks_found:
        last_block_ts = blocks_found[-1]['timestamp']
    else:
        last_block_ts = first_ts

    # Period 1: shares during block-finding period
    # Period 2: shares after last block (the drought)
    period1_shares = [s for s in all_shares if s['timestamp'] <= last_block_ts]
    period2_shares = [s for s in all_shares if s['timestamp'] > last_block_ts]

    for period_name, shares, found_count in [
        ("Block-finding period (up to last block)", period1_shares, len(blocks_found)),
        ("Drought period (after last block)", period2_shares, 0),
        ("Overall", all_shares, len(blocks_found)),
    ]:
        if not shares:
            continue

        p1_ts = shares[0]['timestamp']
        p2_ts = shares[-1]['timestamp']
        span_h = (p2_ts - p1_ts) / 3600.0

        # Sum expected blocks from per-share probabilities
        expected_blocks = 0.0
        total_work = 0
        for s in shares:
            # Each share does work proportional to target_to_average_attempts(share_target)
            # Probability this specific share is a block:
            p_block = float(s['blockchain_target'] + 1) / float(s['share_target'] + 1)
            if p_block > 1.0:
                p_block = 1.0
            expected_blocks += p_block
            total_work += s['share_work']

        # Estimated pool hashrate from share work
        if span_h > 0:
            pool_hashrate = total_work / (span_h * 3600.0)
        else:
            pool_hashrate = 0

        # Average network difficulty
        avg_net_diff = sum(s['net_diff'] for s in shares) / len(shares)

        # Network hashrate estimate
        net_hashrate = avg_net_diff * 2**32 / BLOCK_PERIOD

        # Pool fraction
        pool_frac = pool_hashrate / net_hashrate if net_hashrate > 0 else 0

        # Expected blocks from hashrate method
        n_net_blocks = (span_h * 3600.0) / BLOCK_PERIOD
        expected_from_hashrate = n_net_blocks * pool_frac

        print "\n--- %s ---" % period_name
        print "  Shares:               %d" % len(shares)
        print "  Time span:            %s to %s (%.1f hours)" % (
            time.strftime('%m-%d %H:%M', time.gmtime(p1_ts)),
            time.strftime('%m-%d %H:%M', time.gmtime(p2_ts)),
            span_h)
        print "  Blocks found:         %d" % found_count
        print "  Est. pool hashrate:   %.2f TH/s" % (pool_hashrate / 1e12)
        print "  Avg network diff:     {:,.0f}".format(avg_net_diff)
        print "  Est. network hash:    %.2f PH/s" % (net_hashrate / 1e15)
        print "  Pool fraction:        %.4f%%" % (pool_frac * 100)
        print "  Network blocks in period: %.1f" % n_net_blocks
        print "  Expected blocks (sum of P per share): %.4f" % expected_blocks
        print "  Expected blocks (hashrate method):    %.4f" % expected_from_hashrate

        if found_count == 0 and expected_blocks > 0:
            # Poisson probability of finding 0 blocks
            p_zero = math.exp(-expected_blocks)
            print ""
            print "  >>> P(0 blocks | expected=%.4f) = e^(-%.4f) = %.4f = %.2f%%" % (
                expected_blocks, expected_blocks, p_zero, p_zero * 100)
            print "  >>> That's about 1 in %d chance" % int(round(1.0 / p_zero)) if p_zero > 0 else ""
            if p_zero > 0.01:
                print "  >>> VERDICT: This is within normal variance - just BAD LUCK"
            elif p_zero > 0.001:
                print "  >>> VERDICT: Unlucky, but not impossibly so (happens ~%.0f%% of the time)" % (p_zero * 100)
            else:
                print "  >>> VERDICT: Very unusual - worth investigating further"

        elif found_count > 0 and expected_blocks > 0:
            # How lucky was the block-finding period?
            # Poisson P(X >= found_count)
            p_at_least = 1.0
            cumulative = 0.0
            for k in range(found_count):
                p_k = math.exp(-expected_blocks) * (expected_blocks ** k) / math.factorial(k)
                cumulative += p_k
            p_at_least = 1.0 - cumulative
            print "  P(>= %d blocks | expected=%.4f) = %.4f = %.2f%%" % (
                found_count, expected_blocks, p_at_least, p_at_least * 100)

    # === Extended drought analysis from current time ===
    print "\n" + "=" * 70
    print "DROUGHT ANALYSIS (up to NOW)"
    print "=" * 70

    now = time.time()
    if blocks_found:
        drought_from_now = (now - blocks_found[-1]['timestamp']) / 3600.0
        print "Hours since last block: %.1f (%.1f days)" % (drought_from_now, drought_from_now / 24)

        # Use the most recent pool stats to estimate current expected rate
        recent_shares = [s for s in all_shares if s['timestamp'] > now - 6*3600]
        if len(recent_shares) < 10:
            recent_shares = all_shares[-200:]  # fallback to last 200 shares

        if recent_shares:
            r_span = (recent_shares[-1]['timestamp'] - recent_shares[0]['timestamp'])
            if r_span > 0:
                r_total_work = sum(s['share_work'] for s in recent_shares)
                r_pool_hr = r_total_work / float(r_span)
                r_avg_diff = sum(s['net_diff'] for s in recent_shares) / len(recent_shares)
                r_net_hr = r_avg_diff * 2**32 / BLOCK_PERIOD
                r_frac = r_pool_hr / r_net_hr if r_net_hr > 0 else 0
                blocks_per_hour = (3600.0 / BLOCK_PERIOD) * r_frac

                expected_in_drought = blocks_per_hour * drought_from_now
                p_zero_drought = math.exp(-expected_in_drought)

                print "Recent pool hashrate:   %.2f TH/s" % (r_pool_hr / 1e12)
                print "Recent network diff:    {:,.0f}".format(r_avg_diff)
                print "Expected blocks/hour:   %.4f" % blocks_per_hour
                print "Expected blocks/day:    %.2f" % (blocks_per_hour * 24)
                print "Expected blocks in drought (%.1f h): %.4f" % (drought_from_now, expected_in_drought)
                print ""
                print "P(0 blocks in %.1f hours) = %.4f = %.2f%%" % (
                    drought_from_now, p_zero_drought, p_zero_drought * 100)
                if p_zero_drought > 0:
                    print "That's about 1 in %d chance" % int(round(1.0 / p_zero_drought))

                # What drought duration would be at various significance levels
                print "\nExpected drought durations:"
                for pct in [50, 25, 10, 5, 1]:
                    if blocks_per_hour > 0:
                        t = -math.log(pct / 100.0) / blocks_per_hour
                        print "  %2d%% chance of drought >= %.1f hours (%.1f days)" % (pct, t, t/24)

    print ""

if __name__ == '__main__':
    main()
