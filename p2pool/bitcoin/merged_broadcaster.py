"""
Merged Mining Network Broadcaster - Parallel block broadcasting for merged mining chains

This module handles broadcasting merged mining blocks (e.g., Dogecoin) to multiple
network nodes simultaneously. Unlike parent chain blocks, merged mining blocks 
require auxpow proof and are submitted via RPC, but we ALSO maintain P2P connections
for faster block propagation via inv/block messages.

Key features:
- Bootstrap peers from local merged mining node (dogecoind) via getpeerinfo
- Maintain P2P connections to child chain nodes for fast block propagation
- Discover additional peers via P2P 'addr' messages
- Parallel RPC submission + P2P broadcast when block found
- Persistent peer database
- Quality-based peer scoring
"""

from __future__ import division, print_function

import json
import os
import sys
import time

from twisted.internet import defer, reactor, protocol
from twisted.python import log, failure

from p2pool.bitcoin import data as bitcoin_data, p2p as bitcoin_p2p
from p2pool.util import deferral, variable, jsonrpc


def _with_timeout(df, timeout):
    """Wrap a deferred with a timeout"""
    result_df = defer.Deferred()
    timed_out = [False]
    
    def on_timeout():
        timed_out[0] = True
        if not result_df.called:
            result_df.errback(failure.Failure(defer.TimeoutError('Connection timeout')))
    
    timeout_call = reactor.callLater(timeout, on_timeout)
    
    def on_success(result):
        if not timed_out[0] and not result_df.called:
            timeout_call.cancel()
            result_df.callback(result)
    
    def on_failure(fail):
        if not timed_out[0] and not result_df.called:
            timeout_call.cancel()
            result_df.errback(fail)
    
    df.addCallbacks(on_success, on_failure)
    return result_df


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
            # Truncate long IPv6 addresses
            if len(str(host)) > 20:
                host = str(host)[:17] + '...'
            return '%s:%d' % (host, port)
        return str(addr)
    except Exception:
        return repr(addr)


class MergedMiningBroadcaster(object):
    """Manages block broadcasting for merged mining chains (e.g., Dogecoin)
    
    P2P connections for faster block propagation:
    - Maintain P2P connections to merged chain nodes BEFORE blocks are found
    - Bootstrap peers from local merged mining node (dogecoind) via getpeerinfo
    - Discover additional peers via P2P 'addr' messages
    - When block found: RPC submit + P2P broadcast simultaneously
    
    This broadcaster focuses on:
    - Primary RPC submission to local merged mining node
    - P2P announcement to connected peers for faster propagation
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
            p2p_net: Network object for P2P connections (required for P2P)
            p2p_port: P2P port for the chain (e.g., 22556 for Dogecoin mainnet)
        """
        print('MergedBroadcaster[%s]: Initializing...' % chain_name)
        
        self.merged_proxy = merged_proxy
        self.merged_url = merged_url
        self.datadir_path = datadir_path
        self.chain_name = chain_name
        self.additional_rpc_endpoints = additional_rpc_endpoints or []
        self.p2p_net = p2p_net
        self.p2p_port = p2p_port or 22556  # Default to Dogecoin mainnet
        
        # Peer database for P2P discovery
        self.peer_db = {}
        
        # Active P2P connections
        self.connections = {}
        
        # Connection tracking
        self.connection_attempts = {}
        self.connection_failures = {}
        self.max_connection_attempts = 3
        self.connection_timeout = 60  # Backoff time in seconds
        
        # Valid P2P ports for Dogecoin
        # Dogecoin: 22556 (mainnet), 44556 (testnet), 44557 (testnet4alpha)
        self.valid_ports = [22556, 44556, 44557, 18444]
        if self.p2p_port not in self.valid_ports:
            self.valid_ports.append(self.p2p_port)
        
        # Configuration
        self.max_peers = 10  # Target number of P2P connections
        self.min_peers = 4   # Minimum before we actively try to connect more
        self.bootstrapped = False
        self.stopping = False
        
        # Statistics
        self.stats = {
            'blocks_submitted': 0,
            'rpc_successes': 0,
            'rpc_failures': 0,
            'p2p_broadcasts': 0,
            'p2p_successes': 0,
            'p2p_failures': 0,
            'peers_discovered': 0,
            'last_block_time': None,
            'last_block_hash': None,
        }
        
        print('MergedBroadcaster[%s]: Configuration:' % chain_name)
        print('  Primary RPC: %s' % merged_url)
        print('  Additional RPC endpoints: %d' % len(self.additional_rpc_endpoints))
        print('  P2P enabled: %s' % ('Yes' if p2p_net else 'No'))
        if p2p_net:
            print('  P2P port: %s' % self.p2p_port)
            print('  Max P2P peers: %d' % self.max_peers)
        print('MergedBroadcaster[%s]: Initialization complete' % chain_name)
    
    @defer.inlineCallbacks
    def start(self):
        """Start the merged mining broadcaster"""
        print('MergedBroadcaster[%s]: Starting...' % self.chain_name)
        
        # Load peer database
        self._load_peer_database()
        
        if self.p2p_net:
            # Bootstrap peers from node
            yield self._bootstrap_peers()
            
            # Establish initial P2P connections
            yield self._connect_to_peers()
            
            # Start connection maintenance loop
            self.connection_loop = deferral.RobustLoopingCall(self._maintain_connections)
            self.connection_loop.start(30)  # Every 30 seconds
            
            # Start periodic peer refresh from node
            self.refresh_loop = deferral.RobustLoopingCall(self._refresh_peers)
            self.refresh_loop.start(300)  # Every 5 minutes
        
        # Start periodic database save
        self.save_loop = deferral.RobustLoopingCall(self._save_peer_database)
        self.save_loop.start(300)  # Every 5 minutes
        
        print('MergedBroadcaster[%s]: Started' % self.chain_name)
        if self.p2p_net:
            print('MergedBroadcaster[%s]: Active P2P connections: %d' % (
                self.chain_name, len(self.connections)))
        defer.returnValue(True)
    
    def stop(self):
        """Stop the broadcaster"""
        print('MergedBroadcaster[%s]: Stopping...' % self.chain_name)
        self.stopping = True
        
        if hasattr(self, 'connection_loop'):
            self.connection_loop.stop()
        if hasattr(self, 'refresh_loop'):
            self.refresh_loop.stop()
        if hasattr(self, 'save_loop'):
            self.save_loop.stop()
        
        self._save_peer_database()
        
        # Disconnect all P2P peers
        for addr in list(self.connections.keys()):
            self._disconnect_peer(addr)
        
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
                
                # Parse address (handle IPv6)
                if addr_str.startswith('['):
                    if ']:' in addr_str:
                        host, port = addr_str.rsplit(':', 1)
                        host = host[1:-1]  # Remove brackets
                        port = int(port)
                    else:
                        continue
                elif ':' in addr_str:
                    # Could be IPv4:port or IPv6 without port
                    parts = addr_str.rsplit(':', 1)
                    if len(parts) == 2:
                        host = parts[0]
                        try:
                            port = int(parts[1])
                        except ValueError:
                            continue
                    else:
                        continue
                else:
                    host = addr_str
                    port = self.p2p_port
                
                addr = (host, port)
                
                # Filter non-standard ports
                if port not in self.valid_ports:
                    continue
                
                if addr not in self.peer_db:
                    self.peer_db[addr] = {
                        'addr': addr,
                        'score': 100,  # High score for bootstrap peers
                        'first_seen': time.time(),
                        'last_seen': time.time(),
                        'source': 'coind',  # From daemon's peers
                    }
                    added += 1
                else:
                    # Update existing peer
                    self.peer_db[addr]['last_seen'] = time.time()
            
            self.bootstrapped = True
            print('MergedBroadcaster[%s]: Bootstrapped %d new peers (total: %d)' % (
                self.chain_name, added, len(self.peer_db)))
            
        except Exception as e:
            print('MergedBroadcaster[%s]: Bootstrap error: %s' % (self.chain_name, e), file=sys.stderr)
    
    @defer.inlineCallbacks
    def _connect_to_peers(self):
        """Connect to peers from the database"""
        if not self.p2p_net or self.stopping:
            return
        
        # Get peers sorted by score (highest first)
        peers_by_score = sorted(
            self.peer_db.items(),
            key=lambda x: x[1].get('score', 0),
            reverse=True
        )
        
        connected = 0
        current_time = time.time()
        
        for addr, peer_info in peers_by_score:
            if len(self.connections) >= self.max_peers:
                break
            
            if addr in self.connections:
                continue
            
            # Check backoff
            if addr in self.connection_failures:
                if current_time - self.connection_failures[addr] < self.connection_timeout:
                    continue
            
            # Check attempt count
            attempts = self.connection_attempts.get(addr, 0)
            if attempts >= self.max_connection_attempts:
                continue
            
            try:
                yield self._connect_to_peer(addr)
                connected += 1
            except Exception as e:
                self.connection_attempts[addr] = attempts + 1
                if attempts + 1 >= self.max_connection_attempts:
                    self.connection_failures[addr] = current_time
        
        if connected > 0:
            print('MergedBroadcaster[%s]: Connected to %d new peers (total: %d)' % (
                self.chain_name, connected, len(self.connections)))
    
    @defer.inlineCallbacks
    def _connect_to_peer(self, addr):
        """Connect to a single peer"""
        host, port = addr
        
        print('MergedBroadcaster[%s]: Connecting to %s...' % (
            self.chain_name, _safe_addr_str(addr)))
        
        factory = bitcoin_p2p.ClientFactory(self.p2p_net)
        connector = reactor.connectTCP(host, port, factory, timeout=10)
        
        try:
            # Wait for handshake with timeout
            protocol = yield _with_timeout(factory.getProtocol(), 15)
            
            self.connections[addr] = {
                'factory': factory,
                'connector': connector,
                'protocol': protocol,
                'connected_at': time.time(),
            }
            
            # Reset failure tracking on success
            if addr in self.connection_attempts:
                del self.connection_attempts[addr]
            if addr in self.connection_failures:
                del self.connection_failures[addr]
            
            # Update peer database score
            if addr in self.peer_db:
                self.peer_db[addr]['last_seen'] = time.time()
                self.peer_db[addr]['score'] = min(200, self.peer_db[addr].get('score', 50) + 10)
            
            print('MergedBroadcaster[%s]: Connected to %s' % (
                self.chain_name, _safe_addr_str(addr)))
            
            # Hook P2P messages for discovery
            self._hook_protocol_messages(addr, protocol)
            
            # Request peer addresses
            try:
                if hasattr(protocol, 'send_getaddr') and callable(protocol.send_getaddr):
                    protocol.send_getaddr()
            except Exception as e:
                print('MergedBroadcaster[%s]: Error sending getaddr: %s' % (
                    self.chain_name, e), file=sys.stderr)
            
            defer.returnValue(True)
            
        except Exception as e:
            print('MergedBroadcaster[%s]: Failed to connect to %s: %s' % (
                self.chain_name, _safe_addr_str(addr), e), file=sys.stderr)
            
            try:
                connector.disconnect()
            except:
                pass
            
            raise
    
    def _hook_protocol_messages(self, addr, protocol):
        """Hook P2P message handlers for peer discovery"""
        # Hook addr message handler for P2P discovery
        original_handle_addr = getattr(protocol, 'handle_addr', None)
        if original_handle_addr:
            broadcaster = self  # Capture reference for closure
            
            def handle_addr_wrapper(addrs):
                # Process addresses for peer discovery
                for addr_data in addrs:
                    try:
                        host = addr_data['address'].get('address', '')
                        port = addr_data['address'].get('port', broadcaster.p2p_port)
                        
                        if not host or port not in broadcaster.valid_ports:
                            continue
                        
                        peer_addr = (host, port)
                        if peer_addr not in broadcaster.peer_db:
                            broadcaster.peer_db[peer_addr] = {
                                'addr': peer_addr,
                                'score': 50,
                                'first_seen': time.time(),
                                'last_seen': time.time(),
                                'source': 'p2p',
                            }
                            broadcaster.stats['peers_discovered'] += 1
                    except Exception:
                        pass
                
                return original_handle_addr(addrs)
            
            protocol.handle_addr = handle_addr_wrapper
    
    def _disconnect_peer(self, addr):
        """Disconnect from a peer"""
        if addr not in self.connections:
            return
        
        try:
            conn = self.connections[addr]
            if conn.get('connector'):
                conn['connector'].disconnect()
        except Exception as e:
            print('MergedBroadcaster[%s]: Error disconnecting %s: %s' % (
                self.chain_name, _safe_addr_str(addr), e), file=sys.stderr)
        
        if addr in self.connections:
            del self.connections[addr]
    
    @defer.inlineCallbacks
    def _maintain_connections(self):
        """Maintain minimum number of P2P connections"""
        if self.stopping or not self.p2p_net:
            return
        
        # Clean up dead connections
        for addr in list(self.connections.keys()):
            conn = self.connections[addr]
            protocol = conn.get('protocol')
            if protocol and hasattr(protocol, 'transport'):
                if not protocol.transport or not protocol.transport.connected:
                    print('MergedBroadcaster[%s]: Lost connection to %s' % (
                        self.chain_name, _safe_addr_str(addr)))
                    del self.connections[addr]
        
        # Connect more if needed
        if len(self.connections) < self.min_peers:
            yield self._connect_to_peers()
    
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
    
    @defer.inlineCallbacks
    def broadcast_block(self, block_hex, block_hash, auxpow_info=None):
        """Submit merged mining block via RPC + P2P broadcast in parallel
        
        Args:
            block_hex: Hex-encoded block data for RPC submission
            block_hash: Block hash (for logging and P2P inv)
            auxpow_info: Optional dict with auxpow details for logging
            
        Returns:
            Deferred that fires with (rpc_success, p2p_announcements)
        """
        print('')
        print('=' * 70)
        print('MergedBroadcaster[%s]: BLOCK BROADCAST INITIATED' % self.chain_name)
        print('=' * 70)
        
        # Convert hash to int if string
        if isinstance(block_hash, str):
            hash_int = int(block_hash, 16)
            hash_str = block_hash
        else:
            hash_int = block_hash
            hash_str = '%064x' % block_hash
        
        print('  Block hash: %s' % hash_str)
        print('  Block size: %d bytes' % (len(block_hex) // 2))
        print('  Active P2P connections: %d' % len(self.connections))
        
        start_time = time.time()
        
        # Phase 1: Submit via RPC to all endpoints in parallel
        rpc_deferreds = []
        
        # Primary endpoint
        rpc_deferreds.append(self._submit_via_rpc(self.merged_proxy, block_hex, 'primary'))
        
        # Additional endpoints
        for i, proxy in enumerate(self.additional_rpc_endpoints):
            rpc_deferreds.append(self._submit_via_rpc(proxy, block_hex, 'additional_%d' % i))
        
        print('MergedBroadcaster[%s]: Phase 1 - Submitting to %d RPC endpoint(s)...' % (
            self.chain_name, len(rpc_deferreds)))
        
        # Phase 2: Broadcast via P2P simultaneously (non-blocking)
        p2p_successes = 0
        p2p_failures = 0
        
        if self.p2p_net and self.connections:
            print('MergedBroadcaster[%s]: Phase 2 - Broadcasting to %d P2P peers...' % (
                self.chain_name, len(self.connections)))
            
            for addr, conn in list(self.connections.items()):
                try:
                    protocol = conn.get('protocol')
                    if protocol and hasattr(protocol, 'send_inv'):
                        # Send inv message with block hash
                        protocol.send_inv(invs=[dict(type='block', hash=hash_int)])
                        p2p_successes += 1
                        self.stats['p2p_successes'] += 1
                except Exception as e:
                    p2p_failures += 1
                    self.stats['p2p_failures'] += 1
                    print('MergedBroadcaster[%s]: P2P broadcast to %s failed: %s' % (
                        self.chain_name, _safe_addr_str(addr), e), file=sys.stderr)
            
            self.stats['p2p_broadcasts'] += 1
        
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
        self.stats['last_block_hash'] = hash_str
        
        # Log results
        primary_success = rpc_results[0][0] and rpc_results[0][1] if rpc_results else False
        
        print('')
        print('MergedBroadcaster[%s]: BROADCAST COMPLETE' % self.chain_name)
        print('  Time: %.3f seconds' % rpc_time)
        print('  RPC: %d/%d succeeded (%s primary)' % (
            rpc_successes, len(rpc_results), 'OK' if primary_success else 'FAILED'))
        print('  P2P: %d/%d peers reached' % (p2p_successes, p2p_successes + p2p_failures))
        print('=' * 70)
        print('')
        
        defer.returnValue((rpc_successes > 0, p2p_successes))
    
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
            'total_peers': len(self.peer_db),
            'active_connections': len(self.connections),  # Now tracking actual P2P connections
            'rpc_endpoints': 1 + len(self.additional_rpc_endpoints),
            'blocks_submitted': self.stats['blocks_submitted'],
            'blocks_broadcast': self.stats['blocks_submitted'],  # Alias for dashboard
            'rpc_successes': self.stats['rpc_successes'],
            'rpc_failures': self.stats['rpc_failures'],
            'successful_broadcasts': self.stats['rpc_successes'],  # Alias for dashboard
            'failed_broadcasts': self.stats['rpc_failures'],  # Alias for dashboard
            'p2p_broadcasts': self.stats['p2p_broadcasts'],
            'p2p_successes': self.stats['p2p_successes'],
            'p2p_failures': self.stats['p2p_failures'],
            'peers_discovered': self.stats['peers_discovered'],
            'success_rate': (self.stats['rpc_successes'] / 
                           (self.stats['rpc_successes'] + self.stats['rpc_failures']) * 100
                           if (self.stats['rpc_successes'] + self.stats['rpc_failures']) > 0 else 0),
            'last_block_time': self.stats['last_block_time'],
            'last_block_hash': self.stats['last_block_hash'],
        }
    
    def get_peer_details(self):
        """Get detailed peer information for dashboard"""
        peers = []
        for addr, conn in self.connections.items():
            connected_at = conn.get('connected_at', 0)
            uptime = time.time() - connected_at if connected_at else 0
            
            peer_info = self.peer_db.get(addr, {})
            peers.append({
                'address': _safe_addr_str(addr),
                'source': peer_info.get('source', 'unknown'),
                'score': peer_info.get('score', 0),
                'connected': True,
                'uptime': int(uptime),
            })
        
        return peers
