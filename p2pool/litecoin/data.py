"""
Litecoin data structures and hashing functions
Based on p2pool dash/data.py but adapted for Scrypt PoW
"""

from __future__ import division

import hashlib
import random
import warnings
from Crypto.Hash import RIPEMD

import p2pool
from p2pool.util import math, pack

def hash256(data):
    return pack.IntType(256).unpack(hashlib.sha256(hashlib.sha256(data).digest()).digest())

def hash160(data):
    return pack.IntType(160).unpack(RIPEMD.new(hashlib.sha256(data).digest()).digest())

# Scrypt PoW function for Litecoin/Dogecoin
def scrypt_hash(data):
    """
    Scrypt hash function (N=1024, r=1, p=1) for Litecoin/Dogecoin
    
    Note: This requires ltc_scrypt or py-scrypt module
    """
    try:
        import ltc_scrypt
        return pack.IntType(256).unpack(ltc_scrypt.getPoWHash(data))
    except ImportError:
        try:
            import scrypt
            # Litecoin parameters: N=1024, r=1, p=1, output=32 bytes
            return pack.IntType(256).unpack(scrypt.hash(data, data, 1024, 1, 1, 32))
        except ImportError:
            raise ImportError("Scrypt library not found. Install ltc_scrypt or py-scrypt: pip install ltc-scrypt")

# Import common types from dash.data since they're compatible
from p2pool.dash.data import (
    ChecksummedType,
    FloatingInteger,
    FloatingIntegerType,
    address_type,
    target_to_average_attempts,
    target_to_difficulty,
    difficulty_to_target,
    merkle_hash,
    # Transaction types
    tx_type,
    # Block types  
    block_header_type,
    block_type,
    # Merkle types for auxpow
    merkle_tx_type,
    merkle_link_type,
    aux_pow_type,
    aux_pow_coinbase_type,
    make_auxpow_tree,
    merkle_record_type,
)

# Re-export for compatibility
__all__ = [
    'hash256', 'hash160', 'scrypt_hash',
    'ChecksummedType', 'FloatingInteger', 'FloatingIntegerType',
    'address_type',
    'target_to_average_attempts', 'target_to_difficulty', 'difficulty_to_target',
    'merkle_hash',
    'tx_type',
    'block_header_type', 'block_type',
    'merkle_tx_type', 'merkle_link_type',
    'aux_pow_type', 'aux_pow_coinbase_type', 'make_auxpow_tree',
    'merkle_record_type',
]
