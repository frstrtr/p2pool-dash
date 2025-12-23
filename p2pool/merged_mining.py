"""
Helper functions for merged mining with multiaddress coinbase support

This module provides utilities for building merged mining blocks with
multiaddress coinbase transactions, allowing proportional payouts to
multiple miners on merged chains (e.g., Dogecoin).
"""

import sys
from p2pool.bitcoin import data as bitcoin_data
from p2pool.util import pack
from p2pool import data as p2pool_data

# P2Pool author donation script (1% of merged mining blocks)
DONATION_SCRIPT = '4104ffd03de44a6e11b9917f3a29f9443283d9871c9d743ef30d5eddcd37094b64d1b3d8090496b53256786bf5c82932ec23c3b74d9f05a6f95a8b5529352656664bac'.decode('hex')


def build_coinbase_input_script(height, extradata=''):
    """
    Build coinbase input script with block height (BIP 34)
    
    Args:
        height: Block height (integer)
        extradata: Optional extra data to include
    
    Returns:
        Packed script bytes
    """
    # Encode height as compact size (BIP 34 requirement)
    height_bytes = pack.IntType(32).pack(height)
    # Remove leading zero bytes
    while len(height_bytes) > 1 and height_bytes[0] == '\x00':
        height_bytes = height_bytes[1:]
    
    script_bytes = chr(len(height_bytes)) + height_bytes
    if extradata:
        script_bytes += extradata
    
    return script_bytes


def build_merged_coinbase(template, shareholders, net):
    """
    Build coinbase transaction with multiple outputs for merged mining
    
    Includes P2Pool author donation (1% of block reward + fees)
    
    Args:
        template: Block template from getblocktemplate (with auxpow)
        shareholders: Dict of {address: fraction} OR {address: (fraction, net_obj)}
        net: Network object (default if shareholders don't specify network per address)
    
    Returns:
        Dict representing the coinbase transaction
    """
    total_reward = template['coinbasevalue']
    height = template['height']
    
    # Reserve 1% for P2Pool author donation
    donation_amount = total_reward // 100  # 1% of block reward
    miners_reward = total_reward - donation_amount
    
    print >>sys.stderr, '[DONATION] Total reward: %d, Donation (1%%): %d, Miners: %d' % (
        total_reward, donation_amount, miners_reward)
    
    # Build outputs for each shareholder (from 99% of reward)
    tx_outs = []
    total_distributed = 0
    for address, value in shareholders.iteritems():
        # Handle both old format (address: fraction) and new format (address: (fraction, net))
        if isinstance(value, tuple):
            fraction, addr_net = value
        else:
            fraction = value
            addr_net = net.PARENT if hasattr(net, 'PARENT') else net
        
        amount = int(miners_reward * fraction)
        total_distributed += amount
        if amount > 0:  # Skip dust outputs
            try:
                # Use existing bitcoin_data.address_to_script2 function with proper network
                script2 = bitcoin_data.address_to_script2(address, addr_net)
                tx_outs.append({
                    'value': amount,
                    'script': script2,
                })
            except ValueError as e:
                print >>sys.stderr, 'Warning: Failed to decode address %s: %s' % (address, e)
    
    # Add P2Pool author donation output (1% of block reward)
    # This marks blocks as P2Pool-mined in the blockchain
    tx_outs.append({
        'value': donation_amount,
        'script': DONATION_SCRIPT,
    })
    
    print >>sys.stderr, '[DONATION] Added P2Pool author donation output: %d satoshis' % donation_amount
    print >>sys.stderr, '[DONATION] Total outputs: %d (shareholders) + 1 (donation) = %d' % (
        len(tx_outs) - 1, len(tx_outs))
    
    # If no valid outputs, create a single output to a default address
    # (This should never happen in normal operation)
    if not tx_outs:
        print >>sys.stderr, 'ERROR: No valid shareholder outputs, merged mining will fail!'
        # Create dummy output to prevent transaction from being invalid
        tx_outs.append({
            'value': total_reward,
            'script': '\x76\xa9\x14' + '\x00' * 20 + '\x88\xac',  # OP_DUP OP_HASH160 <zeros> OP_EQUALVERIFY OP_CHECKSIG
        })
    
    # Build coinbase transaction
    # Note: For coinbase, use None for previous_output and sequence
    # PossiblyNoneType will encode them properly during serialization
    coinbase_tx = {
        'version': 1,
        'tx_ins': [{
            'previous_output': None,  # Will be encoded as dict(hash=0, index=2**32-1)
            'sequence': None,  # Will be encoded as 0xffffffff
            'script': build_coinbase_input_script(height, '/P2Pool-Scrypt/'),
        }],
        'tx_outs': tx_outs,
        'lock_time': 0,
    }
    
    return coinbase_tx


def build_merged_block(template, coinbase_tx, auxpow_proof, parent_block_header, merkle_link_to_parent):
    """
    Build complete merged mining block with auxpow
    
    Args:
        template: Block template from getblocktemplate
        coinbase_tx: Coinbase transaction dict
        auxpow_proof: Dict with merkle_tx, merkle_link, parent_block_header for auxpow
        parent_block_header: Parent chain (Litecoin) block header
        merkle_link_to_parent: Merkle link from coinbase to parent block
    
    Returns:
        Complete block dict ready for packing and submitblock
    """
    # Collect all transaction hashes for merkle root calculation
    # Start with coinbase transaction (packed)
    coinbase_packed = bitcoin_data.tx_type.pack(coinbase_tx)
    tx_hashes = [bitcoin_data.hash256(coinbase_packed)]
    
    # Collect both hashes and unpacked txs (needed for block packing later)
    tx_list = [coinbase_tx]
    
    # Add transactions from template
    for tx in template.get('transactions', []):
        # Transactions in template are hex-encoded raw format
        tx_data = tx['data'].decode('hex')
        # Calculate hash directly from raw data
        tx_hashes.append(bitcoin_data.hash256(tx_data))
        # Unpack for including in block
        tx_unpacked = bitcoin_data.tx_type.unpack(tx_data)
        tx_list.append(tx_unpacked)
    
    # Calculate merkle root from transaction hashes
    merkle_root = bitcoin_data.merkle_hash(tx_hashes)
    
    # Debug bits unpacking
    bits_hex = template['bits']
    bits_bytes = bits_hex.decode('hex')
    bits_reversed = bits_bytes[::-1]
    print >>sys.stderr, '[DEBUG merged_mining] Dogecoin template bits=%s' % bits_hex
    print >>sys.stderr, '[DEBUG merged_mining] Bits bytes=%s, reversed=%s' % (bits_bytes.encode('hex'), bits_reversed.encode('hex'))
    
    # Build block header
    # NOTE: Bits field needs byte reversal! Dogecoin getblocktemplate returns bits in 
    # big-endian hex format but the header expects little-endian uint32
    header = {
        'version': template['version'],
        'previous_block': int(template['previousblockhash'], 16),
        'merkle_root': merkle_root,
        'timestamp': template['curtime'],
        'bits': bitcoin_data.FloatingIntegerType().unpack(bits_reversed),
        'nonce': parent_block_header['nonce'],  # Use nonce from parent chain
    }
    
    print >>sys.stderr, '[DEBUG merged_mining] Created FloatingInteger: %r' % header['bits']
    print >>sys.stderr, '[DEBUG merged_mining] FloatingInteger.bits (raw 32-bit): 0x%08x' % header['bits'].bits
    
    # Build complete block with auxpow
    # Note: For multiaddress merged mining, we need to submit via submitauxblock or submitblock
    # The block structure includes the auxpow proof
    block = {
        'header': header,
        'txs': tx_list,
    }
    
    return block, auxpow_proof


def calculate_shareholder_fractions(tracker, min_payout=1000000):
    """
    Calculate payout fractions from share chain tracker
    
    Args:
        tracker: ShareTracker object with recent shares
        min_payout: Minimum payout amount in satoshis (to avoid dust)
    
    Returns:
        Dict of {address: fraction} where fractions sum to 1.0
    """
    # This is a placeholder - actual implementation will integrate with
    # P2Pool's share chain tracking
    
    # For now, return empty dict (will use getauxblock fallback)
    return {}
