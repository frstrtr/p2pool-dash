#!/usr/bin/env pypy
"""
Directly read share store files and check if any share reached blockchain difficulty.

A share is a valid block when: pow_hash <= header['bits'].target
(i.e., the X11 hash of the block header meets the Dash network target)

Must be run from the p2pool-dash directory with pypy.
"""

import os
import sys
import time
import struct
import traceback

# Add p2pool to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from p2pool import data as p2pool_data
from p2pool.dash import data as dash_data
from p2pool.util import pack
from p2pool import networks

def target_to_difficulty(target):
    if target == 0:
        return float('inf')
    return 26959535291011309493156476344723991336010898738574164086137773096960.0 / target

def main():
    share_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'dash')
    
    # Find share files
    share_files = sorted([
        os.path.join(share_dir, f) 
        for f in os.listdir(share_dir) 
        if f.startswith('shares.') and not f.endswith('.bak')
    ])
    
    if not share_files:
        print "No share files found in %s" % share_dir
        return
    
    print "Found %d share files in %s" % (len(share_files), share_dir)
    for f in share_files:
        size_mb = os.path.getsize(f) / (1024.0 * 1024.0)
        print "  %s (%.1f MB)" % (os.path.basename(f), size_mb)
    print ""
    
    # Load the network config (mainnet dash)
    net = networks.nets['dash']
    
    total_shares = 0
    total_verified = 0
    parse_errors = 0
    blocks_found = []
    
    start_time = time.time()
    
    for filepath in share_files:
        filename = os.path.basename(filepath)
        file_shares = 0
        print "Reading %s..." % filename
        
        with open(filepath, 'rb') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    parts = line.split(' ', 1)
                    if len(parts) != 2:
                        continue
                    type_id_str, data_hex = parts
                    type_id = int(type_id_str)
                    
                    if type_id == 2:
                        # Verified hash entry
                        total_verified += 1
                        continue
                    elif type_id == 5:
                        # Share entry
                        raw_share = p2pool_data.share_type.unpack(data_hex.decode('hex'))
                        if raw_share['type'] < p2pool_data.Share.VERSION:
                            continue
                        
                        share = p2pool_data.load_share(raw_share, net, None)
                        total_shares += 1
                        file_shares += 1
                        
                        # THE KEY CHECK: did this share reach blockchain difficulty?
                        blockchain_target = share.header['bits'].target
                        share_pow = share.pow_hash
                        
                        if share_pow <= blockchain_target:
                            share_diff = target_to_difficulty(share.target)
                            net_diff = target_to_difficulty(blockchain_target)
                            pow_diff = target_to_difficulty(share_pow)
                            
                            # Extract payout address
                            try:
                                addr = dash_data.script2_to_address(share.new_script, net.PARENT)
                            except Exception:
                                addr = "unknown"
                            
                            # Extract node address (who found the block)
                            try:
                                node_addr = dash_data.pubkey_hash_to_address(share.share_data['pubkey_hash'], net.PARENT)
                            except Exception:
                                node_addr = "unknown"
                            
                            # Hash quality: how far PoW exceeded the target
                            hash_quality = pow_diff / net_diff if net_diff > 0 else 0
                            
                            block_info = {
                                'hash': '%064x' % share.header_hash,
                                'pow_hash': '%064x' % share_pow,
                                'share_diff': share_diff,
                                'network_diff': net_diff,
                                'pow_diff': pow_diff,
                                'hash_quality': hash_quality,
                                'timestamp': share.timestamp,
                                'absheight': share.absheight,
                                'payout_address': addr,
                                'node_address': node_addr,
                                'subsidy': share.share_data['subsidy'] / 1e8,
                                'file': filename,
                                'line': line_num,
                                'previous_block': '%064x' % share.header['previous_block'],
                            }
                            blocks_found.append(block_info)
                            
                            print "\n  *** BLOCK FOUND in %s line %d! ***" % (filename, line_num)
                            print "      Block hash:      %s" % block_info['hash']
                            print "      PoW hash:        %s" % block_info['pow_hash']
                            print "      Share diff:      %.2f" % share_diff
                            print "      Network diff:    %.2f" % net_diff
                            print "      Actual PoW diff: %.2f" % pow_diff
                            print "      Hash quality:    %.4fx (PoW diff / net diff)" % hash_quality
                            print "      Payout address:  %s" % addr
                            print "      Found by node:   %s" % node_addr
                            print "      Subsidy:         %.8f DASH" % block_info['subsidy']
                            print "      Absheight:       %d" % share.absheight
                            ts = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(share.timestamp))
                            print "      Timestamp:       %s" % ts
                            print "      Prev block:      %s" % block_info['previous_block']
                            print ""
                    elif type_id in (0, 1):
                        continue
                    else:
                        pass
                        
                except Exception as e:
                    parse_errors += 1
                    if parse_errors <= 5:
                        print "  Parse error at %s line %d: %s" % (filename, line_num, str(e))
        
        print "  -> %d shares loaded from %s" % (file_shares, filename)
    
    elapsed = time.time() - start_time
    
    print "\n" + "=" * 70
    print "RESULTS"
    print "=" * 70
    print "Total shares parsed:    %d" % total_shares
    print "Verified hash entries:  %d" % total_verified
    print "Parse errors:           %d" % parse_errors
    print "Time elapsed:           %.1f seconds" % elapsed
    print "Parse rate:             %.0f shares/sec" % (total_shares / elapsed if elapsed > 0 else 0)
    print ""
    print "Shares that reached blockchain difficulty: %d" % len(blocks_found)
    
    if blocks_found:
        print "\n*** BLOCKS FOUND ON THE P2POOL NETWORK (relayed to this node): ***"
        for i, b in enumerate(blocks_found, 1):
            ts = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(b['timestamp']))
            print "\n  Block #%d:" % i
            print "    Block hash:      %s" % b['hash']
            print "    PoW hash:        %s" % b['pow_hash']
            print "    Network diff:    {:,.2f}".format(b['network_diff'])
            print "    Actual PoW diff: {:,.2f}".format(b['pow_diff'])
            print "    Hash quality:    %.4fx (PoW diff / net diff)" % b['hash_quality']
            print "    Subsidy:         %.8f DASH" % b['subsidy']
            print "    Payout address:  %s" % b['payout_address']
            print "    Found by node:   %s" % b['node_address']
            print "    Timestamp:       %s" % ts
            print "    Prev block:      %s" % b['previous_block']
            print "    Source:          %s line %d" % (b['file'], b['line'])
    else:
        # Show some stats about how close shares got
        print "\nNo shares reached blockchain difficulty."
        print "Scanning for closest shares..."

if __name__ == '__main__':
    main()
