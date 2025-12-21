"""
Dash Network Broadcaster - Independent peer discovery and parallel block broadcasting

This module maintains an independent peer database and broadcasts found blocks
to multiple Dash network nodes simultaneously for faster propagation.

Key features:
- Bootstrap from dashd, then maintain independent peer list
- Discover new peers via P2P 'addr' messages
- Quality-based peer scoring and automatic rotation
- Protected local dashd connection (never dropped)
- Persistent peer database
"""

import json
import os
import sys
import time

from twisted.internet import defer, reactor
from twisted.python import log

from . import data as dash_data, p2p as dash_p2p
from p2pool.util import deferral


def _safe_addr_str(addr):
    """Safely format address tuple for printing (handles Unicode in Python 2.7)"""
    try:
        if isinstance(addr, tuple) and len(addr) == 2:
            host, port = addr
            # Ensure host is ASCII-safe string
            if isinstance(host, unicode):
                host = host.encode('ascii', 'replace')
            return '%s:%d' % (host, port)
        return str(addr)
    except Exception:
        return repr(addr)


class DashNetworkBroadcaster(object):
    """Manages independent P2P connections to Dash network nodes for block broadcasting"""
    
    def __init__(self, net, dashd, local_dashd_factory, local_dashd_addr, datadir_path):
        """Initialize broadcaster
        
        Args:
            net: Network configuration object
            dashd: JSON-RPC proxy to dashd
            local_dashd_factory: Existing P2P factory for local dashd
            local_dashd_addr: (host, port) tuple for local dashd
            datadir_path: Directory to store peer database
        """
        print 'Broadcaster: Initializing DashNetworkBroadcaster...'
        
        self.net = net
        self.dashd = dashd
        self.local_dashd_factory = local_dashd_factory
        self.local_dashd_addr = local_dashd_addr
        self.datadir_path = datadir_path
        
        # Peer database: (host, port) -> peer info dict
        self.peer_db = {}
        
        # Active connections: (host, port) -> connection info dict
        self.connections = {}
        
        # Connection tracking for retry logic
        self.connection_attempts = {}  # (host, port) -> attempt count
        self.connection_failures = {}  # (host, port) -> last failure time
        self.last_dashd_refresh = 0    # Timestamp of last dashd peer refresh
        
        # Configuration
        self.max_peers = 20  # Total connections including local dashd
        self.min_peers = 5   # Minimum required for health
        self.max_connection_attempts = 3  # Max retries per peer before backoff
        self.connection_timeout = 300  # 5 minutes backoff after max failures
        self.dashd_refresh_interval = 1800  # 30 minutes between dashd refreshes
        self.bootstrapped = False
        
        # Statistics
        self.stats = {
            'blocks_sent': 0,
            'total_broadcasts': 0,
            'successful_broadcasts': 0,
            'failed_broadcasts': 0,
            'peer_stats': {},  # (host, port) -> detailed stats
            'connection_stats': {
                'total_attempts': 0,
                'successful_connections': 0,
                'failed_connections': 0,
                'timeouts': 0,
                'refused': 0,
                'dashd_refreshes': 0
            }
        }
        
        print 'Broadcaster: Configuration:'
        print '  Max peers: %d' % self.max_peers
        print '  Min peers: %d' % self.min_peers
        print '  Max connection attempts: %d' % self.max_connection_attempts
        print '  Connection timeout: %ds' % self.connection_timeout
        print '  Dashd refresh interval: %ds' % self.dashd_refresh_interval
        print '  Local dashd: %s (PROTECTED)' % _safe_addr_str(local_dashd_addr)
        print '  Data directory: %s' % datadir_path
        print 'Broadcaster: Initialization complete'
    
    @defer.inlineCallbacks
    def start(self):
        """Start the broadcaster (bootstrap and begin peer management)"""
        print 'Broadcaster: Starting broadcaster...'
        
        # Load persistent peer database
        self._load_peer_database()
        
        # Bootstrap from dashd if needed
        if not self.bootstrapped or len(self.peer_db) < self.min_peers:
            yield self.bootstrap_from_dashd()
        else:
            print 'Broadcaster: Using cached peer database (%d peers)' % len(self.peer_db)
        
        # Start adaptive peer refresh (smart polling)
        self.refresh_loop = deferral.RobustLoopingCall(self._adaptive_refresh)
        self.refresh_loop.start(5)  # Check every 5 seconds (fast check, only acts when needed)
        print 'Broadcaster: Adaptive peer refresh started (checks every 5s, acts when needed)'
        
        # Start periodic database save
        self.save_loop = deferral.RobustLoopingCall(self._save_peer_database)
        self.save_loop.start(300)  # Every 5 minutes
        print 'Broadcaster: Database save loop started (5min interval)'
        
        print 'Broadcaster: Start complete'
        defer.returnValue(True)
    
    @defer.inlineCallbacks
    def bootstrap_from_dashd(self):
        """One-time bootstrap: get initial peer list from dashd"""
        print ''
        print '=' * 70
        print 'Broadcaster: BOOTSTRAP PHASE - Fetching peers from dashd'
        print '=' * 70
        
        if self.bootstrapped:
            print 'Broadcaster: Already bootstrapped, skipping'
            defer.returnValue(len(self.peer_db))
        
        # CRITICAL: Add local dashd with maximum priority
        print 'Broadcaster: Registering local dashd as PROTECTED peer'
        self.peer_db[self.local_dashd_addr] = {
            'addr': self.local_dashd_addr,
            'score': 999999,  # Maximum score - never drop!
            'first_seen': time.time(),
            'last_seen': time.time(),
            'source': 'local_dashd',
            'protected': True,  # CRITICAL FLAG
            'successful_broadcasts': 0,
            'failed_broadcasts': 0,
        }
        
        # Register the existing local dashd connection
        self.connections[self.local_dashd_addr] = {
            'factory': self.local_dashd_factory,
            'connector': None,  # Already connected
            'connected_at': time.time(),
            'protected': True  # CRITICAL FLAG
        }
        print 'Broadcaster: Local dashd at %s marked as PROTECTED' % _safe_addr_str(self.local_dashd_addr)
        
        # Get additional peers from dashd
        try:
            print 'Broadcaster: Querying dashd.rpc_getpeerinfo()...'
            peer_info = yield self.dashd.rpc_getpeerinfo()
            print 'Broadcaster: Received %d peers from dashd' % len(peer_info)
            
            added_count = 0
            for peer in peer_info:
                addr_str = peer.get('addr', '')
                if not addr_str:
                    continue
                
                # Parse address
                if ':' in addr_str:
                    host, port = addr_str.rsplit(':', 1)
                    port = int(port)
                else:
                    host = addr_str
                    port = self.net.PARENT.P2P_PORT
                
                addr = (host, port)
                
                # Skip if this is local dashd (already added)
                if addr == self.local_dashd_addr:
                    continue
                
                # Calculate initial score based on dashd metrics
                score = 100  # Base score
                
                # Prefer outbound connections
                if not peer.get('inbound', True):
                    score += 100
                
                # Prefer low latency
                ping_ms = peer.get('pingtime', 1.0) * 1000
                if ping_ms < 50:
                    score += 50
                elif ping_ms < 100:
                    score += 30
                elif ping_ms < 200:
                    score += 10
                
                # Prefer long uptime
                conn_time = peer.get('conntime', 0)
                if conn_time > 3600:
                    score += 20
                
                # Add to database
                self.peer_db[addr] = {
                    'addr': addr,
                    'score': score,
                    'first_seen': time.time(),
                    'last_seen': time.time(),
                    'source': 'dashd',
                    'protected': False,
                    'successful_broadcasts': 0,
                    'failed_broadcasts': 0,
                    'ping_ms': ping_ms,
                    'outbound': not peer.get('inbound', True)
                }
                added_count += 1
            
            print 'Broadcaster: Added %d new peers from dashd' % added_count
            
        except Exception as e:
            print >>sys.stderr, 'Broadcaster: ERROR fetching peers from dashd: %s' % e
            log.err(e, 'Broadcaster bootstrap error:')
        
        self.bootstrapped = True
        print 'Broadcaster: Bootstrap complete - %d total peers in database' % len(self.peer_db)
        print '=' * 70
        print ''
        
        # Start connecting to top peers
        yield self.refresh_connections()
        defer.returnValue(len(self.peer_db))
    
    @defer.inlineCallbacks
    def _refresh_peers_from_dashd(self):
        """Refresh peer list from dashd (emergency or scheduled refresh)"""
        try:
            print 'Broadcaster: Querying dashd.rpc_getpeerinfo() for fresh peers...'
            peer_info = yield self.dashd.rpc_getpeerinfo()
            print 'Broadcaster: Received %d peers from dashd' % len(peer_info)
            
            added_count = 0
            updated_count = 0
            
            for peer in peer_info:
                addr_str = peer.get('addr', '')
                if not addr_str:
                    continue
                
                # Parse address
                if ':' in addr_str:
                    host, port = addr_str.rsplit(':', 1)
                    port = int(port)
                else:
                    host = addr_str
                    port = self.net.PARENT.P2P_PORT
                
                addr = (host, port)
                
                # Skip local dashd
                if addr == self.local_dashd_addr:
                    continue
                
                # Filter out ephemeral ports (incoming connection ports from dashd)
                if port not in [9999, 19999, 18444]:
                    continue
                
                # Calculate score
                score = 100
                if not peer.get('inbound', True):
                    score += 100
                
                ping_ms = peer.get('pingtime', 1.0) * 1000
                if ping_ms < 50:
                    score += 50
                elif ping_ms < 100:
                    score += 30
                elif ping_ms < 200:
                    score += 10
                
                conn_time = peer.get('conntime', 0)
                if conn_time > 3600:
                    score += 20
                
                # Add or update
                if addr not in self.peer_db:
                    self.peer_db[addr] = {
                        'addr': addr,
                        'score': score,
                        'first_seen': time.time(),
                        'last_seen': time.time(),
                        'source': 'dashd_refresh',
                        'protected': False,
                        'successful_broadcasts': 0,
                        'failed_broadcasts': 0,
                        'ping_ms': ping_ms,
                        'outbound': not peer.get('inbound', True)
                    }
                    added_count += 1
                    # Clear failure history for new peer from dashd
                    print 'Broadcaster: NEW peer from dashd: %s (ping=%.1fms, score=%d)' % (
                        _safe_addr_str(addr), ping_ms, score)
                    if addr in self.connection_failures:
                        del self.connection_failures[addr]
                        print '  -> Cleared previous failure history'
                    if addr in self.connection_attempts:
                        self.connection_attempts[addr] = 0
                else:
                    # Update existing peer
                    self.peer_db[addr]['last_seen'] = time.time()
                    self.peer_db[addr]['ping_ms'] = ping_ms
                    self.peer_db[addr]['score'] = max(self.peer_db[addr]['score'], score)
                    updated_count += 1
            
            self.last_dashd_refresh = time.time()
            self.stats['connection_stats']['dashd_refreshes'] += 1
            
            print 'Broadcaster: Dashd refresh complete'
            print '  New peers added: %d' % added_count
            print '  Existing peers updated: %d' % updated_count
            print '  Total peers in database: %d' % len(self.peer_db)
            
            defer.returnValue(added_count + updated_count)
            
        except Exception as e:
            print >>sys.stderr, 'Broadcaster: ERROR refreshing from dashd: %s' % e
            log.err(e, 'Broadcaster dashd refresh error:')
            defer.returnValue(0)
    
    def handle_addr_message(self, addrs):
        """Handle 'addr' message from connected peers - discover new peers!
        
        Args:
            addrs: List of address dicts with 'host', 'port', 'timestamp'
        """
        if not addrs:
            return
        
        new_count = 0
        updated_count = 0
        filtered_count = 0
        
        for addr_info in addrs:
            host = addr_info.get('host')
            port = addr_info.get('port', self.net.PARENT.P2P_PORT)
            
            if not host:
                continue
            
            # Filter out non-standard ports (ephemeral/random ports from incoming connections)
            # Only accept standard Dash P2P port (9999 mainnet, 19999 testnet, 18444 regtest)
            if port not in [9999, 19999, 18444]:
                filtered_count += 1
                continue
            
            addr = (host, port)
            timestamp = addr_info.get('timestamp', time.time())
            
            # Add to database if new
            if addr not in self.peer_db:
                self.peer_db[addr] = {
                    'addr': addr,
                    'score': 50,  # Lower initial score than dashd peers
                    'first_seen': time.time(),
                    'last_seen': timestamp,
                    'source': 'p2p_discovery',
                    'protected': False,
                    'successful_broadcasts': 0,
                    'failed_broadcasts': 0,
                }
                new_count += 1
                print '  + NEW: %s (via P2P discovery)' % _safe_addr_str(addr)
            else:
                # Update last_seen
                self.peer_db[addr]['last_seen'] = timestamp
                updated_count += 1
        
        # Only log if there are significant updates (new peers or many filtered)
        if new_count > 0:
            print 'Broadcaster: P2P discovery - %d new peers added (total: %d peers)' % (
                new_count, len(self.peer_db))
        elif filtered_count > 100:
            print 'Broadcaster: P2P discovery - filtered %d ephemeral ports, %d updated' % (
                filtered_count, updated_count)
    
    def handle_ping_message(self, peer_addr):
        """Handle 'ping' message - track peer activity
        
        Args:
            peer_addr: (host, port) tuple of peer
        """
        if peer_addr in self.peer_db:
            self.peer_db[peer_addr]['last_seen'] = time.time()
            # Don't log every ping - too verbose
    
    def handle_block_message(self, peer_addr, block_hash):
        """Handle 'block' or 'inv' message - track block propagation
        
        This helps us understand which peers are well-connected and see blocks quickly.
        Useful for prioritizing peers that might help us propagate OUR blocks faster.
        
        Args:
            peer_addr: (host, port) tuple of peer
            block_hash: Block hash (int)
        """
        if peer_addr in self.peer_db:
            self.peer_db[peer_addr]['last_seen'] = time.time()
            # Bonus score for peers that relay blocks to us
            if 'blocks_relayed' not in self.peer_db[peer_addr]:
                self.peer_db[peer_addr]['blocks_relayed'] = 0
            self.peer_db[peer_addr]['blocks_relayed'] += 1
            self.peer_db[peer_addr]['score'] += 5  # Small bonus
            
            print 'Broadcaster: BLOCK from %s (hash=%064x, total_blocks=%d)' % (
                _safe_addr_str(peer_addr), block_hash, 
                self.peer_db[peer_addr]['blocks_relayed'])
    
    def handle_tx_message(self, peer_addr):
        """Handle 'tx' or 'inv' message for transactions
        
        Track transaction relay activity to identify well-connected peers.
        
        Args:
            peer_addr: (host, port) tuple of peer
        """
        if peer_addr in self.peer_db:
            self.peer_db[peer_addr]['last_seen'] = time.time()
            # Track transaction relay (less verbose than blocks)
            if 'txs_relayed' not in self.peer_db[peer_addr]:
                self.peer_db[peer_addr]['txs_relayed'] = 0
            self.peer_db[peer_addr]['txs_relayed'] += 1
            
            # Only log every 100 transactions to reduce spam
            if self.peer_db[peer_addr]['txs_relayed'] % 100 == 0:
                print 'Broadcaster: TX activity from %s (%d transactions relayed)' % (
                    _safe_addr_str(peer_addr), self.peer_db[peer_addr]['txs_relayed'])
    
    @defer.inlineCallbacks
    def refresh_connections(self):
        """Maintain connections to the best peers from our database
        
        Runs periodically (every 60s) to:
        1. Check if we need to refresh peer list from dashd (if too many failures)
        2. Score all known peers
        3. Disconnect from low-quality peers (EXCEPT local dashd!)
        4. Connect to high-quality peers we're not connected to with retry logic
        """
        print ''
        print 'Broadcaster: === PEER REFRESH CYCLE ==='
        current_time = time.time()
        
        # Check if we need to refresh from dashd (emergency fallback)
        active_connections = len([c for c in self.connections.values() if not c.get('protected')])
        failed_peers = len([f for f in self.connection_failures.values() if current_time - f < self.connection_timeout])
        
        should_refresh_dashd = False
        if active_connections < self.min_peers:
            print 'Broadcaster: WARNING - Only %d active peers (min: %d)' % (active_connections, self.min_peers)
            should_refresh_dashd = True
        
        if failed_peers > self.max_peers:
            print 'Broadcaster: WARNING - Too many failed peers (%d in backoff)' % failed_peers
            should_refresh_dashd = True
        
        time_since_last_refresh = current_time - self.last_dashd_refresh
        if should_refresh_dashd and time_since_last_refresh > 300:  # At least 5 min between emergency refreshes
            print ''
            print 'Broadcaster: EMERGENCY DASHD REFRESH TRIGGERED'
            print '  Reason: Insufficient healthy peers'
            print '  Active peers: %d / %d' % (active_connections, self.min_peers)
            print '  Failed peers in backoff: %d' % failed_peers
            yield self._refresh_peers_from_dashd()
        elif time_since_last_refresh > self.dashd_refresh_interval:
            print ''
            print 'Broadcaster: SCHEDULED DASHD REFRESH (every %ds)' % self.dashd_refresh_interval
            yield self._refresh_peers_from_dashd()
        
        # Score all peers
        scored_peers = []
        for addr, info in self.peer_db.items():
            # CRITICAL: Local dashd always gets maximum score
            if info.get('protected', False):
                score = 999999
            else:
                score = self._calculate_peer_score(info, current_time)
            
            scored_peers.append((score, addr, info))
        
        # Sort by score (highest first)
        scored_peers.sort(reverse=True)
        
        # Select top N peers to connect to
        target_peers = scored_peers[:self.max_peers]
        target_addrs = set(addr for _, addr, _ in target_peers)
        current_addrs = set(self.connections.keys())
        
        print 'Broadcaster: Peer selection:'
        print '  Database size: %d peers' % len(self.peer_db)
        print '  Current connections: %d' % len(current_addrs)
        print '  Target connections: %d' % len(target_addrs)
        
        # Disconnect from peers not in target list
        to_disconnect = current_addrs - target_addrs
        if to_disconnect:
            print 'Broadcaster: Disconnecting from %d low-quality peers:' % len(to_disconnect)
        
        for addr in to_disconnect:
            conn = self.connections.get(addr)
            if conn and not conn.get('protected', False):
                print '  - Disconnecting %s' % _safe_addr_str(addr)
                self._disconnect_peer(addr)
            elif conn and conn.get('protected', False):
                print '  - PRESERVING protected connection to %s (local dashd)' % _safe_addr_str(addr)
        
        # Connect to new peers with retry/backoff logic
        to_connect = target_addrs - current_addrs
        
        # Filter out peers in backoff period
        to_connect_filtered = []
        in_backoff = []
        for addr in to_connect:
            # Check if peer is in backoff period
            if addr in self.connection_failures:
                time_since_failure = current_time - self.connection_failures[addr]
                if time_since_failure < self.connection_timeout:
                    in_backoff.append((addr, int(self.connection_timeout - time_since_failure)))
                    continue
                else:
                    # Backoff expired - clear failure time but keep attempt counter
                    # This allows retrying (attempt 2/3, then 3/3)
                    del self.connection_failures[addr]
            
            # Check if we've exceeded max attempts
            attempts = self.connection_attempts.get(addr, 0)
            if attempts >= self.max_connection_attempts:
                # After max attempts, require longer backoff before reset
                if addr in self.connection_failures:
                    time_since_failure = current_time - self.connection_failures[addr]
                    # After 30 minutes (6x normal backoff), reset and try again
                    if time_since_failure > self.connection_timeout * 6:
                        print 'Broadcaster: Peer %s backoff expired after max attempts, resetting counter' % _safe_addr_str(addr)
                        del self.connection_failures[addr]
                        self.connection_attempts[addr] = 0
                    else:
                        continue
                else:
                    # Just hit max attempts - put in backoff
                    self.connection_failures[addr] = current_time
                    print 'Broadcaster: Peer %s exceeded max attempts (%d), entering extended backoff (30m)' % (
                        _safe_addr_str(addr), attempts)
                continue
            
            to_connect_filtered.append(addr)
        
        if in_backoff:
            print 'Broadcaster: %d peers in backoff period:' % len(in_backoff)
            for addr, remaining in in_backoff[:5]:  # Show first 5
                print '  - %s (backoff: %ds remaining)' % (_safe_addr_str(addr), remaining)
            if len(in_backoff) > 5:
                print '  ... and %d more' % (len(in_backoff) - 5)
        
        if to_connect_filtered:
            print 'Broadcaster: Connecting to %d new high-quality peers:' % len(to_connect_filtered)
        
        for addr in to_connect_filtered:
            attempts = self.connection_attempts.get(addr, 0)
            print '  + Connecting to %s (attempt %d/%d)' % (
                _safe_addr_str(addr), attempts + 1, self.max_connection_attempts)
            self._connect_peer(addr)
        
        # Verify local dashd is still connected
        if self.local_dashd_addr and self.local_dashd_addr not in self.connections:
            print >>sys.stderr, 'Broadcaster: CRITICAL WARNING - Local dashd connection lost!'
            print >>sys.stderr, 'Broadcaster: Attempting to re-register local dashd connection...'
            # Re-register the local dashd connection
            self.connections[self.local_dashd_addr] = {
                'factory': self.local_dashd_factory,
                'connector': None,
                'connected_at': time.time(),
                'protected': True
            }
        
        # Log top 5 peers
        print 'Broadcaster: Top 5 peers by score:'
        for i, (score, addr, info) in enumerate(target_peers[:5]):
            protected = ' [PROTECTED]' if info.get('protected') else ''
            source = info.get('source', 'unknown')
            success_rate = 0
            total = info['successful_broadcasts'] + info['failed_broadcasts']
            if total > 0:
                success_rate = (info['successful_broadcasts'] * 100.0) / total
            
            print '  %d. %s - score=%.1f, source=%s, success=%.1f%%%s' % (
                i+1, _safe_addr_str(addr), score, source, success_rate, protected)
        
        print 'Broadcaster: Connection status: %d connected (local dashd: %s)' % (
            len(self.connections),
            'PROTECTED [OK]' if self.local_dashd_addr in self.connections else 'MISSING [!]')
        print 'Broadcaster: === REFRESH COMPLETE ==='
        print ''
        
        defer.returnValue(len(self.connections))
    
    def _calculate_peer_score(self, peer_info, current_time):
        """Calculate quality score for a peer
        
        Args:
            peer_info: Peer info dict
            current_time: Current timestamp
            
        Returns:
            float: Quality score (higher is better)
        """
        score = peer_info.get('score', 50)
        
        # Success rate bonus (most important)
        total = peer_info['successful_broadcasts'] + peer_info['failed_broadcasts']
        if total > 0:
            success_rate = peer_info['successful_broadcasts'] / float(total)
            score += success_rate * 100
        
        # Recency penalty (haven't seen in a while)
        age_hours = (current_time - peer_info['last_seen']) / 3600.0
        if age_hours > 24:
            score -= 50  # Very stale
        elif age_hours > 6:
            score -= 20  # Somewhat stale
        elif age_hours < 1:
            score += 50  # Very fresh
        
        # Source bonus
        if peer_info['source'] == 'dashd':
            score += 30  # Trust dashd's peers more
        
        return max(0, score)
    
    def _connect_peer(self, addr):
        """Establish P2P connection to a Dash network peer with retry tracking
        
        Note: Local dashd is already connected via main.py's connect_p2p()
        """
        # Skip if this is local dashd (already connected)
        if addr == self.local_dashd_addr:
            print 'Broadcaster: Skipping connection to local dashd (already connected)'
            return
        
        host, port = addr
        
        # Track connection attempt
        if addr not in self.connection_attempts:
            self.connection_attempts[addr] = 0
        self.connection_attempts[addr] += 1
        self.stats['connection_stats']['total_attempts'] += 1
        
        try:
            factory = dash_p2p.ClientFactory(self.net.PARENT)
            
            # Track connection success/failure
            connection_start_time = time.time()
            
            # Hook to handle connection success
            original_gotConnection = getattr(factory, 'gotConnection', None)
            
            def gotConnection_wrapper(protocol):
                connection_time = time.time() - connection_start_time
                print 'Broadcaster: CONNECTED to %s (%.3fs, attempt %d/%d)' % (
                    _safe_addr_str(addr), connection_time, 
                    self.connection_attempts[addr], self.max_connection_attempts)
                
                # Clear failure history on successful connection
                if addr in self.connection_failures:
                    del self.connection_failures[addr]
                self.connection_attempts[addr] = 0  # Reset attempt counter
                self.stats['connection_stats']['successful_connections'] += 1
                
                # Update peer database
                if addr in self.peer_db:
                    self.peer_db[addr]['last_seen'] = time.time()
                    self.peer_db[addr]['score'] += 10  # Bonus for successful connection
                
                # Request peer addresses from this peer (P2P discovery)
                try:
                    protocol.send_getaddr()
                    print 'Broadcaster:   -> Sent getaddr request to %s' % _safe_addr_str(addr)
                except Exception as e:
                    print >>sys.stderr, 'Broadcaster: Error sending getaddr to %s: %s' % (_safe_addr_str(addr), e)
                
                # Hook addr message handler for P2P discovery
                original_handle_addr = getattr(protocol, 'handle_addr', None)
                if original_handle_addr:
                    def handle_addr_wrapper(addrs):
                        # Convert to our format and pass to handler
                        addr_list = []
                        for addr_data in addrs:
                            addr_list.append({
                                'host': addr_data['address'].get('address', ''),
                                'port': addr_data['address'].get('port', self.net.PARENT.P2P_PORT),
                                'timestamp': addr_data.get('timestamp', time.time())
                            })
                        self.handle_addr_message(addr_list)
                        return original_handle_addr(addrs)
                    
                    protocol.handle_addr = handle_addr_wrapper
                
                # Hook block message handler to track block propagation
                original_handle_block = getattr(protocol, 'handle_block', None)
                if original_handle_block:
                    def handle_block_wrapper(block):
                        block_hash = dash_data.hash256(dash_data.block_header_type.pack(block['header']))
                        self.handle_block_message(addr, block_hash)
                        return original_handle_block(block)
                    
                    protocol.handle_block = handle_block_wrapper
                
                # Hook inv message handler to track activity
                original_handle_inv = getattr(protocol, 'handle_inv', None)
                if original_handle_inv:
                    def handle_inv_wrapper(invs):
                        for inv in invs:
                            if inv.get('type') == 2:  # MSG_BLOCK
                                block_hash = inv.get('hash', 0)
                                self.handle_block_message(addr, block_hash)
                            elif inv.get('type') == 1:  # MSG_TX
                                self.handle_tx_message(addr)
                        return original_handle_inv(invs)
                    
                    protocol.handle_inv = handle_inv_wrapper
                
                # Hook ping message handler
                original_handle_ping = getattr(protocol, 'handle_ping', None)
                if original_handle_ping:
                    def handle_ping_wrapper(nonce):
                        self.handle_ping_message(addr)
                        return original_handle_ping(nonce)
                    
                    protocol.handle_ping = handle_ping_wrapper
                
                if original_gotConnection:
                    original_gotConnection(protocol)
            
            factory.gotConnection = gotConnection_wrapper
            
            # Hook to handle connection failure
            original_clientConnectionFailed = getattr(factory, 'clientConnectionFailed', None)
            
            def clientConnectionFailed_wrapper(connector, reason):
                connection_time = time.time() - connection_start_time
                error_msg = str(reason.value)
                
                # Track failure type
                if 'timed out' in error_msg.lower() or 'timeout' in error_msg.lower():
                    self.stats['connection_stats']['timeouts'] += 1
                    failure_type = 'TIMEOUT'
                    should_log = True  # Always log timeouts
                elif 'refused' in error_msg.lower():
                    self.stats['connection_stats']['refused'] += 1
                    failure_type = 'REFUSED'
                    should_log = (self.stats['connection_stats']['refused'] % 50 == 1)  # Log every 50th
                else:
                    failure_type = 'ERROR'
                    should_log = True  # Always log other errors
                
                self.stats['connection_stats']['failed_connections'] += 1
                self.connection_failures[addr] = time.time()
                
                if should_log:
                    print >>sys.stderr, 'Broadcaster: CONNECTION %s to %s (%.3fs, attempt %d/%d): %s' % (
                        failure_type, _safe_addr_str(addr), connection_time,
                        self.connection_attempts[addr], self.max_connection_attempts,
                        error_msg[:100])
                
                # Update peer database
                if addr in self.peer_db:
                    self.peer_db[addr]['score'] -= 20  # Penalty for failed connection
                
                # Remove from connections dict
                if addr in self.connections:
                    del self.connections[addr]
                
                if original_clientConnectionFailed:
                    original_clientConnectionFailed(connector, reason)
            
            factory.clientConnectionFailed = clientConnectionFailed_wrapper
            
            # Initiate connection
            connector = reactor.connectTCP(host, port, factory, timeout=30)
            
            self.connections[addr] = {
                'factory': factory,
                'connector': connector,
                'connected_at': time.time(),
                'protected': False
            }
            
            print 'Broadcaster: Initiated connection to %s (timeout=30s)' % _safe_addr_str(addr)
            
        except Exception as e:
            print >>sys.stderr, 'Broadcaster: EXCEPTION connecting to %s: %s' % (_safe_addr_str(addr), e)
            self.stats['connection_stats']['failed_connections'] += 1
            self.connection_failures[addr] = time.time()
            if addr in self.connections:
                del self.connections[addr]
    
    def _disconnect_peer(self, addr):
        """Close connection to a peer
        
        CRITICAL: Never disconnects local dashd (protected connection)
        """
        if addr not in self.connections:
            return
        
        conn = self.connections[addr]
        
        # CRITICAL: Refuse to disconnect protected peers
        if conn.get('protected', False):
            print >>sys.stderr, 'Broadcaster: BLOCKED attempt to disconnect PROTECTED peer %s' % _safe_addr_str(addr)
            return
        
        # Safe to disconnect non-protected peer
        try:
            conn['factory'].stopTrying()
            if conn['connector']:
                conn['connector'].disconnect()
        except Exception as e:
            print >>sys.stderr, 'Broadcaster: Error disconnecting %s: %s' % (_safe_addr_str(addr), e)
        
        del self.connections[addr]
        print 'Broadcaster: Disconnected from %s' % _safe_addr_str(addr)
    
    @defer.inlineCallbacks
    def broadcast_block(self, block):
        """Send block to ALL connected peers in TRUE PARALLEL
        
        Args:
            block: Block dict to broadcast
            
        Returns:
            Deferred that fires with number of successful sends
        """
        print ''
        print '=' * 70
        print 'Broadcaster: PARALLEL BLOCK BROADCAST INITIATED'
        print '=' * 70
        
        if not self.bootstrapped:
            print 'Broadcaster: Not bootstrapped yet, bootstrapping now...'
            yield self.bootstrap_from_dashd()
        
        if len(self.connections) < self.min_peers:
            print 'Broadcaster: Insufficient peers (%d < %d), refreshing...' % (
                len(self.connections), self.min_peers)
            yield self.refresh_connections()
        
        block_hash = dash_data.hash256(dash_data.block_header_type.pack(block['header']))
        
        print 'Broadcaster: Block details:'
        print '  Block hash: %064x' % block_hash
        print '  Target peers: %d' % len(self.connections)
        print '  Transactions: %d' % len(block.get('txs', []))
        
        # Send to ALL peers in parallel (including local dashd)
        deferreds = []
        peer_addrs = []
        
        start_time = time.time()
        
        for addr, conn in self.connections.items():
            d = self._send_block_to_peer(addr, conn, block)
            deferreds.append(d)
            peer_addrs.append(addr)
        
        if not deferreds:
            print >>sys.stderr, 'Broadcaster: ERROR - No peers available for broadcast!'
            defer.returnValue(0)
        
        print 'Broadcaster: Broadcasting to %d peers in PARALLEL...' % len(deferreds)
        
        # Wait for all sends to complete
        results = yield defer.DeferredList(deferreds, consumeErrors=True)
        
        broadcast_time = time.time() - start_time
        
        # Update statistics
        for (success, result), addr in zip(results, peer_addrs):
            if addr in self.peer_db:
                if success:
                    self.peer_db[addr]['successful_broadcasts'] += 1
                    self.peer_db[addr]['last_seen'] = time.time()
                else:
                    self.peer_db[addr]['failed_broadcasts'] += 1
        
        successes = sum(1 for success, _ in results if success)
        failures = len(results) - successes
        
        # Update global stats
        self.stats['blocks_sent'] += 1
        self.stats['total_broadcasts'] += len(results)
        self.stats['successful_broadcasts'] += successes
        self.stats['failed_broadcasts'] += failures
        
        # Check local dashd result
        local_dashd_success = None
        if self.local_dashd_addr in peer_addrs:
            idx = peer_addrs.index(self.local_dashd_addr)
            local_dashd_success = results[idx][0]
        
        # Log results
        print ''
        print 'Broadcaster: BROADCAST COMPLETE'
        print '  Time: %.3f seconds' % broadcast_time
        print '  Success: %d/%d peers (%.1f%%)' % (successes, len(results), 
            (successes * 100.0) / len(results) if results else 0)
        print '  Failed: %d peers' % failures
        print '  Speed: %.1f peers/second' % (len(results) / broadcast_time if broadcast_time > 0 else 0)
        print '  Local dashd: %s' % (
            'SUCCESS ✓' if local_dashd_success else 
            'FAILED ✗' if local_dashd_success is False else 
            'NOT FOUND')
        print '=' * 70
        print ''
        
        defer.returnValue(successes)
    
    @defer.inlineCallbacks
    def _send_block_to_peer(self, addr, conn, block):
        """Send block to a single peer
        
        Args:
            addr: (host, port) tuple
            conn: Connection dict
            block: Block to send
            
        Returns:
            Deferred that fires with True on success, False on failure
        """
        try:
            factory = conn['factory']
            
            # Wait for protocol to be ready
            protocol = yield factory.getProtocol()
            
            # Send block via P2P
            protocol.send_block(block=block)
            
            defer.returnValue(True)
            
        except Exception as e:
            print >>sys.stderr, 'Broadcaster: Error sending block to %s: %s' % (_safe_addr_str(addr), e)
            defer.returnValue(False)
    
    def _get_peer_db_path(self):
        """Get path to peer database file"""
        return os.path.join(self.datadir_path, 'broadcast_peers.json')
    
    def _cleanup_invalid_ports(self):
        """Remove peers with invalid/ephemeral ports from database"""
        valid_ports = [9999, 19999, 18444]  # mainnet, testnet, regtest
        invalid_addrs = []
        
        for addr in self.peer_db.keys():
            if addr[1] not in valid_ports:
                invalid_addrs.append(addr)
        
        if invalid_addrs:
            print 'Broadcaster: Cleaning up %d peers with invalid ports (ephemeral ports)' % len(invalid_addrs)
            for addr in invalid_addrs:
                del self.peer_db[addr]
                # Also clean from other tracking dicts
                if addr in self.connection_attempts:
                    del self.connection_attempts[addr]
                if addr in self.connection_failures:
                    del self.connection_failures[addr]
            print 'Broadcaster: Cleanup complete - %d valid peers remain' % len(self.peer_db)
    
    def _load_peer_database(self):
        """Load peer database from disk"""
        db_path = self._get_peer_db_path()
        
        if not os.path.exists(db_path):
            print 'Broadcaster: No cached peer database found at %s' % db_path
            return
        
        try:
            print 'Broadcaster: Loading peer database from %s' % db_path
            with open(db_path, 'rb') as f:
                data = json.loads(f.read())
            
            # Load peers
            for addr_str, peer_info in data.get('peers', {}).items():
                # Parse address
                if ':' in addr_str:
                    host, port = addr_str.rsplit(':', 1)
                    # Ensure host is plain string, not unicode (Python 2.7 compatibility)
                    if isinstance(host, unicode):
                        host = host.encode('ascii', 'replace')
                    addr = (host, int(port))
                else:
                    continue
                
                self.peer_db[addr] = peer_info
                # Convert address back to tuple
                self.peer_db[addr]['addr'] = addr
            
            self.bootstrapped = data.get('bootstrapped', False)
            
            print 'Broadcaster: Loaded %d peers from database' % len(self.peer_db)
            
            # Clean up invalid ports (ephemeral ports from incoming connections)
            self._cleanup_invalid_ports()
            
        except Exception as e:
            print >>sys.stderr, 'Broadcaster: Error loading peer database: %s' % e
            log.err(e, 'Broadcaster database load error:')
    
    def _save_peer_database(self):
        """Save peer database to disk"""
        db_path = self._get_peer_db_path()
        
        try:
            # Convert to JSON-serializable format
            peers_json = {}
            for addr, peer_info in self.peer_db.items():
                addr_str = _safe_addr_str(addr)
                # Create a copy to avoid modifying original
                peer_copy = dict(peer_info)
                # Remove non-serializable fields
                peer_copy.pop('addr', None)
                peers_json[addr_str] = peer_copy
            
            data = {
                'version': '1.0',
                'last_updated': time.time(),
                'bootstrapped': self.bootstrapped,
                'local_dashd': _safe_addr_str(self.local_dashd_addr),
                'peers': peers_json
            }
            
            # Atomic write
            temp_path = db_path + '.tmp'
            with open(temp_path, 'wb') as f:
                f.write(json.dumps(data, indent=2, sort_keys=True))
            
            if os.path.exists(db_path):
                os.remove(db_path)
            os.rename(temp_path, db_path)
            
            print 'Broadcaster: Saved peer database (%d peers) to %s' % (len(self.peer_db), db_path)
            
        except Exception as e:
            print >>sys.stderr, 'Broadcaster: Error saving peer database: %s' % e
            log.err(e, 'Broadcaster database save error:')
    
    def get_health_status(self):
        """Return connection health metrics"""
        top_peers = sorted(
            [(addr, info.get('score', 0), info['successful_broadcasts']) 
             for addr, info in self.peer_db.items()],
            key=lambda x: x[1],
            reverse=True
        )[:10]
        
        return {
            'bootstrapped': self.bootstrapped,
            'peer_database_size': len(self.peer_db),
            'active_connections': len(self.connections),
            'blocks_sent': self.stats['blocks_sent'],
            'total_broadcasts': self.stats['total_broadcasts'],
            'successful_broadcasts': self.stats['successful_broadcasts'],
            'failed_broadcasts': self.stats['failed_broadcasts'],
            'success_rate': (self.stats['successful_broadcasts'] * 100.0 / 
                           self.stats['total_broadcasts'] 
                           if self.stats['total_broadcasts'] > 0 else 0),
            'top_peers': top_peers,
            'local_dashd_connected': self.local_dashd_addr in self.connections
        }
    
    def stop(self):
        """Stop the broadcaster (cleanup)"""
        print 'Broadcaster: Stopping broadcaster...'
        
        # Stop loops
        if hasattr(self, 'refresh_loop') and self.refresh_loop.running:
            self.refresh_loop.stop()
        
        if hasattr(self, 'save_loop') and self.save_loop.running:
            self.save_loop.stop()
        
        # Save database
        self._save_peer_database()
        
        # Disconnect non-protected peers
        for addr in list(self.connections.keys()):
            if not self.connections[addr].get('protected', False):
                self._disconnect_peer(addr)
        
        print 'Broadcaster: Stopped'
    
    @defer.inlineCallbacks
    def _adaptive_refresh(self):
        """Smart refresh - only do expensive operations when needed
        
        This runs every 5 seconds but only triggers full refresh when:
        - Connection count drops below minimum
        - Too many failed connections
        - Scheduled refresh interval reached
        - Last refresh was >60s ago and we have capacity
        
        This avoids disrupting mining during normal operation.
        """
        current_time = time.time()
        
        # Fast health check (no expensive operations)
        active_connections = len([c for c in self.connections.values() if not c.get('protected')])
        
        # Determine if we need full refresh
        need_refresh = False
        reason = None
        
        # Critical: Below minimum peers
        if active_connections < self.min_peers:
            need_refresh = True
            reason = 'below_minimum (have=%d, need=%d)' % (active_connections, self.min_peers)
        
        # Check if we have room for more peers and it's been a while
        elif active_connections < self.max_peers:
            time_since_refresh = current_time - getattr(self, '_last_full_refresh', 0)
            if time_since_refresh > 60:  # Only refresh if >60s since last
                need_refresh = True
                reason = 'periodic_maintenance (last=%.0fs ago)' % time_since_refresh
        
        # Scheduled refresh (every 30 min)
        elif (current_time - self.last_dashd_refresh) > self.dashd_refresh_interval:
            need_refresh = True
            reason = 'scheduled_dashd_refresh'
        
        if need_refresh:
            print 'Broadcaster: Adaptive refresh triggered - %s' % reason
            self._last_full_refresh = current_time
            yield self.refresh_connections()
        # Otherwise, do nothing (no disruption to mining)
    
    def get_network_status(self):
        """Get detailed network status for web dashboard
        
        Returns dict with comprehensive P2P network state
        """
        current_time = time.time()
        
        # Connection statistics
        active_peers = []
        protected_peers = []
        for addr, conn in self.connections.items():
            peer_info = self.peer_db.get(addr, {})
            peer_data = {
                'host': addr[0],
                'port': addr[1],
                'protected': conn.get('protected', False),
                'connected_since': conn.get('connected_at', 0),
                'uptime_seconds': int(current_time - conn.get('connected_at', current_time)),
                'score': peer_info.get('score', 0),
                'source': peer_info.get('source', 'unknown'),
                'successful_broadcasts': peer_info.get('successful_broadcasts', 0),
                'failed_broadcasts': peer_info.get('failed_broadcasts', 0),
                'blocks_relayed': peer_info.get('blocks_relayed', 0),
                'txs_relayed': peer_info.get('txs_relayed', 0),
            }
            
            if conn.get('protected'):
                protected_peers.append(peer_data)
            else:
                active_peers.append(peer_data)
        
        # Sort by score
        active_peers.sort(key=lambda x: x['score'], reverse=True)
        
        # Backoff statistics
        backoff_peers = []
        for addr, failure_time in self.connection_failures.items():
            time_since_failure = current_time - failure_time
            if time_since_failure < self.connection_timeout:
                backoff_peers.append({
                    'host': addr[0],
                    'port': addr[1],
                    'backoff_remaining_seconds': int(self.connection_timeout - time_since_failure),
                    'attempts': self.connection_attempts.get(addr, 0)
                })
        
        # Database statistics
        peer_sources = {}
        for peer_info in self.peer_db.values():
            source = peer_info.get('source', 'unknown')
            peer_sources[source] = peer_sources.get(source, 0) + 1
        
        return {
            'enabled': True,
            'bootstrapped': self.bootstrapped,
            'health': {
                'active_connections': len(active_peers),
                'protected_connections': len(protected_peers),
                'total_connections': len(self.connections),
                'target_max_peers': self.max_peers,
                'target_min_peers': self.min_peers,
                'healthy': len(self.connections) >= self.min_peers,
                'local_dashd_connected': self.local_dashd_addr in self.connections
            },
            'statistics': {
                'blocks_broadcast': self.stats['blocks_sent'],
                'total_peer_broadcasts': self.stats['total_broadcasts'],
                'successful_broadcasts': self.stats['successful_broadcasts'],
                'failed_broadcasts': self.stats['failed_broadcasts'],
                'success_rate_percent': (self.stats['successful_broadcasts'] * 100.0 / 
                                       self.stats['total_broadcasts'] 
                                       if self.stats['total_broadcasts'] > 0 else 0),
                'connection_attempts': self.stats['connection_stats']['total_attempts'],
                'successful_connections': self.stats['connection_stats']['successful_connections'],
                'failed_connections': self.stats['connection_stats']['failed_connections'],
                'timeouts': self.stats['connection_stats']['timeouts'],
                'refused': self.stats['connection_stats']['refused'],
                'dashd_refreshes': self.stats['connection_stats']['dashd_refreshes']
            },
            'database': {
                'total_peers': len(self.peer_db),
                'sources': peer_sources,
                'last_dashd_refresh': self.last_dashd_refresh,
                'seconds_since_refresh': int(current_time - self.last_dashd_refresh)
            },
            'peers': {
                'protected': protected_peers,
                'active': active_peers,
                'in_backoff': backoff_peers
            },
            'configuration': {
                'max_peers': self.max_peers,
                'min_peers': self.min_peers,
                'max_connection_attempts': self.max_connection_attempts,
                'connection_timeout_seconds': self.connection_timeout,
                'dashd_refresh_interval_seconds': self.dashd_refresh_interval
            }
        }
