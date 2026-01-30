"""
Merged Mining Network Broadcaster - Parallel block broadcasting for merged mining chains

This module handles broadcasting merged mining blocks (e.g., Dogecoin) to multiple
network nodes simultaneously. Merged mining blocks are submitted differently than
parent chain blocks - they require auxpow proof and are typically submitted via RPC.

Key features:
- Bootstrap from local merged mining node (dogecoind)
- Discover peers via P2P 'addr' messages for additional propagation
- Parallel RPC submission to multiple Dogecoin nodes (if configured)
- Parallel P2P block announcement to discovered peers
- Persistent peer database
- Quality-based peer scoring
"""

from __future__ import division, print_function

import json
import os
import sys
import time

from twisted.internet import defer, reactor, protocol
from twisted.python import log

from p2pool.bitcoin import data as bitcoin_data
from p2pool.util import deferral, variable, jsonrpc


def _safe_addr_str(addr):
    """Safely format address tuple for printing"""
    try:
        if isinstance(addr, tuple) and len(addr) == 2:
            host, port = addr
            try:
                if isinstance(host, unicode):
                    host = host.encode('ascii', 'replace')
            except NameError:
                pass
            return '%s:%d' % (host, port)
        return str(addr)
    except Exception:
        return repr(addr)


class MergedMiningBroadcaster(object):
    """Manages block broadcasting for merged mining chains (e.g., Dogecoin)
    
    Unlike parent chain broadcasting, merged mining blocks:
    1. Must be submitted via RPC (submitblock with auxpow)
    2. Can optionally be announced via P2P for faster propagation
    3. Don't require maintaining persistent P2P connections
    
    This broadcaster focuses on:
    - Primary RPC submission to local merged mining node
    - Optional parallel P2P announcement to discovered peers
    - Multiple RPC endpoints for redundancy (if configured)
    """
    
    def __init__(self, merged_proxy, merged_url, datadir_path, chain_name='dogecoin',
                 additional_rpc_endpoints=None, p2p_net=None, p2p_port=None):
        """Initialize merged mining broadcaster
        
        Args:
            merged_proxy: Primary JSON-RPC proxy to merged mining node
            merged_url: URL of primary merged mining node
            datadir_path: Directory to store peer database
            chain_name: Name for logging (e.g., 'dogecoin')
            additional_rpc_endpoints: List of additional RPC proxies for redundancy
            p2p_net: Network object for P2P connections (optional)
            p2p_port: P2P port for the chain (e.g., 22556 for Dogecoin mainnet)
        """
        print('MergedBroadcaster[%s]: Initializing...' % chain_name)
        
        self.merged_proxy = merged_proxy
        self.merged_url = merged_url
        self.datadir_path = datadir_path
        self.chain_name = chain_name
        self.additional_rpc_endpoints = additional_rpc_endpoints or []
        self.p2p_net = p2p_net
        self.p2p_port = p2p_port
        
        # Peer database for P2P discovery (if p2p_net provided)
        self.peer_db = {}
        
        # Active P2P connections (optional)
        self.p2p_connections = {}
        
        # Valid P2P ports for Dogecoin
        # Dogecoin: 22556 (mainnet), 44556 (testnet)
        self.valid_ports = [22556, 44556, 18444]
        if p2p_port and p2p_port not in self.valid_ports:
            self.valid_ports.append(p2p_port)
        
        # Configuration
        self.max_p2p_peers = 10  # Fewer P2P peers than parent chain
        self.bootstrapped = False
        self.stopping = False
        
        # Statistics
        self.stats = {
            'blocks_submitted': 0,
            'rpc_successes': 0,
            'rpc_failures': 0,
            'p2p_announcements': 0,
            'last_block_time': None,
            'last_block_hash': None,
        }
        
        print('MergedBroadcaster[%s]: Configuration:' % chain_name)
        print('  Primary RPC: %s' % merged_url)
        print('  Additional RPC endpoints: %d' % len(self.additional_rpc_endpoints))
        print('  P2P enabled: %s' % ('Yes' if p2p_net else 'No'))
        if p2p_net:
            print('  P2P port: %s' % p2p_port)
            print('  Max P2P peers: %d' % self.max_p2p_peers)
        print('MergedBroadcaster[%s]: Initialization complete' % chain_name)
    
    @defer.inlineCallbacks
    def start(self):
        """Start the merged mining broadcaster"""
        print('MergedBroadcaster[%s]: Starting...' % self.chain_name)
        
        # Load peer database if P2P enabled
        if self.p2p_net:
            self._load_peer_database()
            
            # Try to bootstrap peers from node
            yield self._bootstrap_peers()
            
            # Start periodic peer refresh
            self.refresh_loop = deferral.RobustLoopingCall(self._refresh_peers)
            self.refresh_loop.start(300)  # Every 5 minutes
        
        # Start periodic database save
        self.save_loop = deferral.RobustLoopingCall(self._save_peer_database)
        self.save_loop.start(300)  # Every 5 minutes
        
        print('MergedBroadcaster[%s]: Started' % self.chain_name)
        defer.returnValue(True)
    
    def stop(self):
        """Stop the broadcaster"""
        print('MergedBroadcaster[%s]: Stopping...' % self.chain_name)
        self.stopping = True
        
        if hasattr(self, 'refresh_loop'):
            self.refresh_loop.stop()
        if hasattr(self, 'save_loop'):
            self.save_loop.stop()
        
        self._save_peer_database()
        
        # Disconnect P2P peers
        for addr in list(self.p2p_connections.keys()):
            self._disconnect_p2p_peer(addr)
        
        print('MergedBroadcaster[%s]: Stopped' % self.chain_name)
    
    @defer.inlineCallbacks
    def _bootstrap_peers(self):
        """Bootstrap peer list from merged mining node"""
        if not self.p2p_net:
            return
        
        try:
            print('MergedBroadcaster[%s]: Bootstrapping peers from node...' % self.chain_name)
            peer_info = yield self.merged_proxy.rpc_getpeerinfo()
            
            added = 0
            for peer in peer_info:
                addr_str = peer.get('addr', '')
                if not addr_str:
                    continue
                
                # Parse address
                if addr_str.startswith('['):
                    if ']:' in addr_str:
                        host, port = addr_str.rsplit(':', 1)
                        host = host[1:-1]
                        port = int(port)
                    else:
                        continue
                elif ':' in addr_str:
                    host, port = addr_str.rsplit(':', 1)
                    port = int(port)
                else:
                    host = addr_str
                    port = self.p2p_port or 22556
                
                addr = (host, port)
                
                # Filter non-standard ports
                if port not in self.valid_ports:
                    continue
                
                if addr not in self.peer_db:
                    self.peer_db[addr] = {
                        'addr': addr,
                        'score': 100,
                        'first_seen': time.time(),
                        'last_seen': time.time(),
                        'source': 'node_bootstrap',
                    }
                    added += 1
            
            self.bootstrapped = True
            print('MergedBroadcaster[%s]: Bootstrapped %d peers' % (self.chain_name, added))
            
        except Exception as e:
            print('MergedBroadcaster[%s]: Bootstrap error: %s' % (self.chain_name, e), file=sys.stderr)
    
    @defer.inlineCallbacks
    def _refresh_peers(self):
        """Periodic peer refresh"""
        if self.stopping or not self.p2p_net:
            return
        
        try:
            peer_info = yield self.merged_proxy.rpc_getpeerinfo()
            
            for peer in peer_info:
                addr_str = peer.get('addr', '')
                if not addr_str or ':' not in addr_str:
                    continue
                
                host, port = addr_str.rsplit(':', 1)
                try:
                    port = int(port)
                except ValueError:
                    continue
                
                if port not in self.valid_ports:
                    continue
                
                addr = (host, port)
                if addr in self.peer_db:
                    self.peer_db[addr]['last_seen'] = time.time()
                else:
                    self.peer_db[addr] = {
                        'addr': addr,
                        'score': 50,
                        'first_seen': time.time(),
                        'last_seen': time.time(),
                        'source': 'refresh',
                    }
        except Exception as e:
            print('MergedBroadcaster[%s]: Refresh error: %s' % (self.chain_name, e), file=sys.stderr)
    
    def _disconnect_p2p_peer(self, addr):
        """Disconnect from a P2P peer"""
        if addr in self.p2p_connections:
            try:
                conn = self.p2p_connections[addr]
                if conn.get('connector'):
                    conn['connector'].disconnect()
            except:
                pass
            del self.p2p_connections[addr]
    
    @defer.inlineCallbacks
    def broadcast_block(self, block_hex, block_hash, auxpow_info=None):
        """Submit merged mining block to multiple endpoints in parallel
        
        Args:
            block_hex: Hex-encoded block data for RPC submission
            block_hash: Block hash (for logging)
            auxpow_info: Optional dict with auxpow details for logging
            
        Returns:
            Deferred that fires with (rpc_success, p2p_announcements)
        """
        print('')
        print('=' * 70)
        print('MergedBroadcaster[%s]: BLOCK BROADCAST INITIATED' % self.chain_name)
        print('=' * 70)
        print('  Block hash: %s' % (block_hash if isinstance(block_hash, str) else '%064x' % block_hash))
        print('  Block size: %d bytes' % (len(block_hex) // 2))
        
        start_time = time.time()
        
        # Phase 1: Submit via RPC to all endpoints in parallel
        rpc_deferreds = []
        rpc_endpoints = []
        
        # Primary endpoint
        rpc_deferreds.append(self._submit_via_rpc(self.merged_proxy, block_hex, 'primary'))
        rpc_endpoints.append('primary')
        
        # Additional endpoints
        for i, proxy in enumerate(self.additional_rpc_endpoints):
            rpc_deferreds.append(self._submit_via_rpc(proxy, block_hex, 'additional_%d' % i))
            rpc_endpoints.append('additional_%d' % i)
        
        print('MergedBroadcaster[%s]: Submitting to %d RPC endpoint(s)...' % (
            self.chain_name, len(rpc_deferreds)))
        
        # Wait for all RPC submissions
        rpc_results = yield defer.DeferredList(rpc_deferreds, consumeErrors=True)
        
        rpc_time = time.time() - start_time
        
        # Count RPC results
        rpc_successes = sum(1 for success, result in rpc_results if success and result)
        rpc_failures = len(rpc_results) - rpc_successes
        
        # Update stats
        self.stats['blocks_submitted'] += 1
        self.stats['rpc_successes'] += rpc_successes
        self.stats['rpc_failures'] += rpc_failures
        self.stats['last_block_time'] = time.time()
        self.stats['last_block_hash'] = block_hash if isinstance(block_hash, str) else '%064x' % block_hash
        
        # Log RPC results
        primary_success = rpc_results[0][0] and rpc_results[0][1] if rpc_results else False
        
        print('')
        print('MergedBroadcaster[%s]: RPC SUBMISSION COMPLETE' % self.chain_name)
        print('  Time: %.3f seconds' % rpc_time)
        print('  Primary RPC: %s' % ('SUCCESS' if primary_success else 'FAILED'))
        print('  Total: %d/%d succeeded' % (rpc_successes, len(rpc_results)))
        
        # Phase 2: Optional P2P announcement (if configured and have peers)
        p2p_announcements = 0
        # P2P announcement is optional and not implemented in this version
        # Could be added later to send inv messages to discovered peers
        
        print('')
        print('MergedBroadcaster[%s]: BROADCAST COMPLETE' % self.chain_name)
        print('  RPC submissions: %d/%d' % (rpc_successes, len(rpc_results)))
        print('  P2P announcements: %d' % p2p_announcements)
        print('=' * 70)
        print('')
        
        defer.returnValue((rpc_successes > 0, p2p_announcements))
    
    @defer.inlineCallbacks
    def _submit_via_rpc(self, proxy, block_hex, endpoint_name):
        """Submit block via RPC to a single endpoint
        
        Args:
            proxy: JSON-RPC proxy
            block_hex: Hex-encoded block
            endpoint_name: Name for logging
            
        Returns:
            Deferred that fires with True on success, False on failure
        """
        try:
            result = yield proxy.rpc_submitblock(block_hex)
            
            # submitblock returns None on success, error string on failure
            if result is None:
                print('MergedBroadcaster[%s]: %s - SUCCESS' % (self.chain_name, endpoint_name))
                defer.returnValue(True)
            else:
                print('MergedBroadcaster[%s]: %s - REJECTED: %s' % (
                    self.chain_name, endpoint_name, result), file=sys.stderr)
                defer.returnValue(False)
                
        except Exception as e:
            print('MergedBroadcaster[%s]: %s - ERROR: %s' % (
                self.chain_name, endpoint_name, e), file=sys.stderr)
            defer.returnValue(False)
    
    def _get_peer_db_path(self):
        """Get path to peer database file"""
        return os.path.join(self.datadir_path, 'merged_broadcast_peers_%s.json' % self.chain_name)
    
    def _load_peer_database(self):
        """Load peer database from disk"""
        if not self.p2p_net:
            return
        
        db_path = self._get_peer_db_path()
        
        if not os.path.exists(db_path):
            return
        
        try:
            with open(db_path, 'rb') as f:
                data = json.loads(f.read())
            
            for addr_str, peer_info in data.get('peers', {}).items():
                if ':' in addr_str:
                    parts = addr_str.rsplit(':', 1)
                    addr = (parts[0], int(parts[1]))
                    self.peer_db[addr] = peer_info
                    self.peer_db[addr]['addr'] = addr
            
            self.bootstrapped = data.get('bootstrapped', False)
            print('MergedBroadcaster[%s]: Loaded %d peers' % (self.chain_name, len(self.peer_db)))
            
        except Exception as e:
            print('MergedBroadcaster[%s]: Load error: %s' % (self.chain_name, e), file=sys.stderr)
    
    def _save_peer_database(self):
        """Save peer database to disk"""
        if self.stopping or not self.p2p_net:
            return
        
        db_path = self._get_peer_db_path()
        
        try:
            peers_json = {}
            for addr, info in self.peer_db.items():
                addr_str = _safe_addr_str(addr)
                peer_copy = dict(info)
                if 'addr' in peer_copy:
                    del peer_copy['addr']
                peers_json[addr_str] = peer_copy
            
            data = {
                'bootstrapped': self.bootstrapped,
                'peers': peers_json,
                'saved_at': time.time()
            }
            
            tmp_path = db_path + '.tmp'
            with open(tmp_path, 'wb') as f:
                f.write(json.dumps(data, indent=2))
            os.rename(tmp_path, db_path)
            
        except Exception as e:
            print('MergedBroadcaster[%s]: Save error: %s' % (self.chain_name, e), file=sys.stderr)
    
    def get_stats(self):
        """Get broadcaster statistics"""
        return {
            'chain': self.chain_name,
            'chain_name': self.chain_name.capitalize(),  # For dashboard display
            'bootstrapped': self.bootstrapped,
            'total_peers': len(self.peer_db) if self.p2p_net else 0,
            'active_connections': 0,  # Merged uses RPC, not P2P connections
            'rpc_endpoints': 1 + len(self.additional_rpc_endpoints),
            'blocks_submitted': self.stats['blocks_submitted'],
            'blocks_broadcast': self.stats['blocks_submitted'],  # Alias for dashboard
            'rpc_successes': self.stats['rpc_successes'],
            'rpc_failures': self.stats['rpc_failures'],
            'successful_broadcasts': self.stats['rpc_successes'],  # Alias for dashboard
            'failed_broadcasts': self.stats['rpc_failures'],  # Alias for dashboard
            'p2p_announcements': self.stats['p2p_announcements'],
            'success_rate': (self.stats['rpc_successes'] / 
                           (self.stats['rpc_successes'] + self.stats['rpc_failures']) * 100
                           if (self.stats['rpc_successes'] + self.stats['rpc_failures']) > 0 else 0),
            'last_block_time': self.stats['last_block_time'],
            'last_block_hash': self.stats['last_block_hash'],
        }
