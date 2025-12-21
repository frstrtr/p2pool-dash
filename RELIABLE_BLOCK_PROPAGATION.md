# Reliable Block Propagation - Design Document

## Current Implementation Analysis

### Existing Block Submission Flow (v1.3.0)
```
Found Block → submit_block_p2p() → Local dashd (single node)
           ↓
           → submit_block_rpc() → Local dashd RPC
                                ↓
                                Local dashd → Dash Network (sequential propagation)
```

**Current Limitations:**
1. **Single point of failure**: Only sends to local dashd node
2. **Sequential propagation**: Block goes: P2Pool → dashd → peer1 → peer2 → ...
3. **Latency**: Each hop adds 50-200ms delay
4. **Orphan risk**: Large pools with direct connections to many nodes propagate faster

**Files Involved:**
- `p2pool/dash/helper.py`: `submit_block()`, `submit_block_p2p()`, `submit_block_rpc()`
- `p2pool/dash/p2p.py`: `Protocol` class with `send_block()` method
- `p2pool/work.py`: Block found callback at line 453

---

## Proposed Enhancement: Multi-Peer Block Broadcaster

### Goal
Broadcast found blocks directly to **multiple Dash network nodes simultaneously** for faster propagation and redundancy.

### Critical Design Principle: Local dashd Protection

**The local dashd P2P connection is SACRED and must NEVER be dropped!**

Why this is critical:
1. **Primary block submission**: Local dashd receives blocks first (existing `submit_block_p2p()`)
2. **Transaction relay**: Needed for mempool synchronization and getblocktemplate
3. **Network sync**: Required for block headers and chain validation
4. **RPC operations**: Dashd RPC depends on healthy P2P connection
5. **Built-in warning**: P2Pool already has warnings when local dashd connection drops

**Protection mechanisms:**
- ✅ Local dashd gets `protected: true` flag in peer database
- ✅ Local dashd always has maximum score (999999) - never rotated out
- ✅ `_disconnect_peer()` refuses to disconnect protected peers
- ✅ `refresh_connections()` verifies local dashd is still connected
- ✅ If lost, logs CRITICAL WARNING and attempts reconnect
- ✅ Local dashd uses existing connection from `main.py connect_p2p()`, not recreated

**Connection architecture:**
```
main.py connect_p2p() → Local dashd P2P (PROTECTED, existing connection)
                      ↓
                      Used for ALL normal operations + block broadcast
                      
Broadcaster → Local dashd (PROTECTED, for parallel block broadcast)
           → Peer 1 (additional, quality-based rotation)
           → Peer 2 (additional, quality-based rotation)
           → Peer 3 (additional, quality-based rotation)
           → ... (up to 19 additional peers)
           
Total: 20 connections (1 protected + 19 rotatable)

Block broadcast: ALL 20 peers receive block in TRUE PARALLEL
- Local dashd via its P2P connection
- Peer 1-19 via their P2P connections  
- All sends happen simultaneously (defer.DeferredList)
- No priority ordering, pure parallel execution
```

### Architecture

```
                                    ┌─────────────────┐
                                    │  Found Block!   │
                                    └────────┬────────┘
                                             │
                          ┌──────────────────┼──────────────────┐
                          │   PARALLEL BROADCAST (all at once)  │
                          └──────────────────┼──────────────────┘
                                             │
         ┌────────────┬────────────┬─────────┴─────────┬────────────┬────────────┐
         ▼            ▼            ▼                   ▼            ▼            ▼
    ┌────────┐  ┌────────┐  ┌────────┐  ...     ┌────────┐  ┌────────┐  ┌────────┐
    │ Local  │  │ Peer 1 │  │ Peer 2 │           │Peer 18 │  │Peer 19 │  │RPC (V) │
    │ dashd  │  │        │  │        │           │        │  │        │  │Verify  │
    │*PROT*  │  │        │  │        │           │        │  │        │  │        │
    └───┬────┘  └───┬────┘  └───┬────┘           └───┬────┘  └───┬────┘  └───┬────┘
        │           │           │                     │           │           │
        └───────────┴───────────┴─────────────────────┴───────────┴───────────┘
                                        │
                                 Dash Network
                            (Maximum propagation speed)
```

**Key improvements:**
- TRUE parallel broadcast: All 20 peers receive block simultaneously
- No priority/ordering: Every peer gets it at the exact same moment
- Local dashd: Still protected, just sent in parallel with others
- RPC: Runs after P2P for verification (usually arrives after P2P)
- Speed: ~50ms to reach all 20 peers vs 50-200ms sequential

### Key Features

#### 1. **Multi-Connection Manager**
```python
class DashNetworkBroadcaster:
    """Manages independent P2P connections to Dash network nodes
    
    Key features:
    - Bootstrap from dashd's initial peer list
    - Discover new peers via P2P 'addr' messages
    - Maintain own peer database independent of dashd
    - Automatic quality-based peer selection and rotation
    - CRITICAL: Always preserve local dashd connection (never drop!)
    """
    
    def __init__(self, net, dashd, local_dashd_factory):
        self.net = net
        self.dashd = dashd
        self.local_dashd_factory = local_dashd_factory  # CRITICAL: Local dashd P2P connection
        self.local_dashd_addr = None  # Will be set during bootstrap
        self.connections = {}  # (host, port) -> Protocol instance
        self.peer_db = {}      # (host, port) -> peer info dict
        self.bootstrapped = False
        self.stats = {
            'blocks_sent': 0,
            'peer_stats': {},  # (host, port) -> {sent, failed, avg_latency, last_seen}
        }
        self.max_peers = 20  # Total connections including local dashd
        self.min_peers = 5
    
    @defer.inlineCallbacks
    def bootstrap_from_dashd(self):
        """One-time bootstrap: get initial peer list from dashd"""
        if self.bootstrapped:
            defer.returnValue(len(self.peer_db))
        
        print 'Broadcaster: Bootstrapping peer list from dashd...'
        
        # CRITICAL: Identify local dashd address (never drop this peer!)
        # Get dashd's listening address from getnetworkinfo
        try:
            net_info = yield self.dashd.rpc_getnetworkinfo()
            self.local_dashd_addr = (args.dashd_address, args.dashd_p2p_port)
            print 'Broadcaster: Local dashd at %s:%d marked as PROTECTED (never drop)' % self.local_dashd_addr
        except Exception as e:
            print >>sys.stderr, 'Broadcaster: WARNING - Could not identify local dashd address: %s' % e
            self.local_dashd_addr = (args.dashd_address, args.dashd_p2p_port)
        
        # Add local dashd to database with maximum priority
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
        
        # Get additional peers from dashd
        initial_peers = yield get_broadcast_peers(self.dashd, max_peers=50)
        
        # Add to our independent peer database (excluding local dashd - already added)
        for addr in initial_peers:
            if addr != self.local_dashd_addr:
                self.peer_db[addr] = {
                    'addr': addr,
                    'score': 100,  # Initial score
                    'first_seen': time.time(),
                    'last_seen': time.time(),
                    'source': 'dashd',
                    'protected': False,
                    'successful_broadcasts': 0,
                    'failed_broadcasts': 0,
                }
        
        self.bootstrapped = True
        print 'Broadcaster: Bootstrapped with %d peers from dashd (+ 1 protected local dashd)' % len(initial_peers)
        
        # Start connecting to top peers (respecting max_peers limit)
        yield self.refresh_connections()
        defer.returnValue(len(self.peer_db))
    
    def handle_addr_message(self, addrs):
        """Handle 'addr' message from connected peers - discover new peers!
        
        This is how we grow our peer database independently of dashd.
        Connected peers tell us about other peers they know.
        """
        new_count = 0
        for addr_info in addrs:
            addr = (addr_info['host'], addr_info['port'])
            
            # Add to database if new
            if addr not in self.peer_db:
                self.peer_db[addr] = {
                    'addr': addr,
                    'score': 50,  # Lower initial score than dashd peers
                    'first_seen': time.time(),
                    'last_seen': addr_info.get('timestamp', time.time()),
                    'source': 'p2p_discovery',
                    'successful_broadcasts': 0,
                    'failed_broadcasts': 0,
                }
                new_count += 1
            else:
                # Update last_seen
                self.peer_db[addr]['last_seen'] = addr_info.get('timestamp', time.time())
        
        if new_count > 0:
            print 'Broadcaster: Discovered %d new peers via P2P addr messages' % new_count
    
    @defer.inlineCallbacks
    def refresh_connections(self):
        """Maintain connections to the best peers from our database
        
        This runs periodically (every 60s) and:
        1. Scores all known peers
        2. Disconnects from low-quality peers (EXCEPT local dashd!)
        3. Connects to high-quality peers we're not connected to
        
        CRITICAL: Local dashd connection is NEVER dropped!
        """
        # Score all peers
        scored_peers = []
        current_time = time.time()
        
        for addr, info in self.peer_db.items():
            # CRITICAL: Local dashd always gets maximum score
            if info.get('protected', False):
                score = 999999  # Infinite priority
            else:
                score = self._calculate_peer_score(info, current_time)
            
            scored_peers.append((score, addr, info))
        
        # Sort by score (highest first)
        scored_peers.sort(reverse=True)
        
        # Select top N peers to connect to
        target_peers = scored_peers[:self.max_peers]
        target_addrs = set(addr for _, addr, _ in target_peers)
        current_addrs = set(self.connections.keys())
        
        # Disconnect from peers not in target list
        # CRITICAL: Never disconnect protected peers (local dashd)
        to_disconnect = current_addrs - target_addrs
        for addr in to_disconnect:
            conn = self.connections.get(addr)
            if conn and not conn.get('protected', False):
                self._disconnect_peer(addr)
            elif conn and conn.get('protected', False):
                print 'Broadcaster: Preserving protected connection to %s:%d (local dashd)' % addr
        
        # Connect to new peers
        to_connect = target_addrs - current_addrs
        for addr in to_connect:
            self._connect_peer(addr)
        
        # Verify local dashd is still connected
        if self.local_dashd_addr and self.local_dashd_addr not in self.connections:
            print >>sys.stderr, 'Broadcaster: CRITICAL WARNING - Local dashd connection lost! Attempting reconnect...'
            # Re-register the local dashd connection
            self.connections[self.local_dashd_addr] = {
                'factory': self.local_dashd_factory,
                'connector': None,
                'connected_at': time.time(),
                'protected': True
            }
        
        print 'Broadcaster: Maintaining %d connections (database has %d peers)' % (
            len(self.connections), len(self.peer_db))
        print 'Broadcaster: Local dashd connection status: %s' % (
            'PROTECTED ✓' if self.local_dashd_addr in self.connections else 'MISSING ✗')
        
        defer.returnValue(len(self.connections))
    
    def _calculate_peer_score(self, peer_info, current_time):
        """Calculate quality score for a peer
        
        Scoring factors:
        - Success rate (broadcasts that succeeded)
        - Recency (when did we last hear from this peer)
        - Source (dashd bootstrap = trusted)
        - Connection history
        """
        score = peer_info.get('score', 50)
        
        # Success rate bonus
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
        
        # Source bonus
        if peer_info['source'] == 'dashd':
            score += 30  # Trust dashd's peers more
        
        return max(0, score)
    
    def _connect_peer(self, addr):
        """Establish P2P connection to a Dash network peer
        
        Note: Local dashd is already connected via main.py's connect_p2p()
        This method is only called for additional broadcast peers
        """
        # Skip if this is local dashd (already connected)
        if addr == self.local_dashd_addr:
            print 'Broadcaster: Skipping connection to local dashd (already connected)'
            return
        
        host, port = addr
        factory = dash_p2p.ClientFactory(self.net.PARENT)
        
        # Hook into factory to receive 'addr' messages for peer discovery
        @defer.inlineCallbacks
        def on_connection(protocol):
            # Request peer addresses from this peer
            protocol.send_getaddr()
            
            # Hook addr message handler
            original_handle_addr = protocol.handle_addr
            def handle_addr_wrapper(addrs):
                self.handle_addr_message(addrs)
                return original_handle_addr(addrs)
            protocol.handle_addr = handle_addr_wrapper
        
        factory.gotConnection = on_connection
        connector = reactor.connectTCP(host, port, factory)
        
        self.connections[addr] = {
            'factory': factory,
            'connector': connector,
            'connected_at': time.time()
        }
        print 'Broadcaster: Connected to peer %s:%d' % (host, port)
    
    def _disconnect_peer(self, addr):
        """Close connection to a peer
        
        CRITICAL: Never disconnects local dashd (protected connection)
        """
        if addr not in self.connections:
            return
        
        conn = self.connections[addr]
        
        # CRITICAL: Refuse to disconnect protected peers
        if conn.get('protected', False):
            print >>sys.stderr, 'Broadcaster: BLOCKED attempt to disconnect protected peer %s:%d (local dashd)' % addr
            return
        
        # Safe to disconnect non-protected peer
        conn['factory'].stopTrying()
        if conn['connector']:
            conn['connector'].disconnect()
        del self.connections[addr]
        print 'Broadcaster: Disconnected from peer %s:%d' % addr
    
    @defer.inlineCallbacks
    def broadcast_block(self, block):
        """Send block to ALL connected peers in TRUE PARALLEL
        
        All peers receive block simultaneously (no priority order):
        - Local dashd: Via existing P2P connection
        - Additional peers: Via broadcaster P2P connections
        
        This maximizes propagation speed - every peer gets the block
        at the exact same time!
        """
        if not self.bootstrapped:
            yield self.bootstrap_from_dashd()
        
        if len(self.connections) < self.min_peers:
            yield self.refresh_connections()
        
        # Send to ALL peers in parallel (including local dashd)
        deferreds = []
        peer_addrs = []
        
        for addr, conn in self.connections.items():
            d = self._send_block_to_peer(addr, conn, block)
            deferreds.append(d)
            peer_addrs.append(addr)
        
        if not deferreds:
            print >>sys.stderr, 'Broadcaster: No peers available for broadcast!'
            defer.returnValue(0)
        
        print 'Broadcaster: Broadcasting block to %d peers in PARALLEL...' % len(deferreds)
        
        # Wait for all sends to complete
        results = yield defer.DeferredList(deferreds, consumeErrors=True)
        
        # Update peer database with results
        for (success, result), addr in zip(results, peer_addrs):
            if addr in self.peer_db:
                if success:
                    self.peer_db[addr]['successful_broadcasts'] += 1
                    self.peer_db[addr]['last_seen'] = time.time()
                else:
                    self.peer_db[addr]['failed_broadcasts'] += 1
        
        successes = sum(1 for success, _ in results if success)
        
        # Log results
        print 'Broadcaster: Block sent successfully to %d/%d peers' % (successes, len(deferreds))
        if addr == self.local_dashd_addr:
            local_success = results[peer_addrs.index(self.local_dashd_addr)][0]
            print 'Broadcaster: Local dashd result: %s' % ('SUCCESS ✓' if local_success else 'FAILED ✗')
        
        defer.returnValue(successes)
    
    def get_health_status(self):
        """Return connection health metrics"""
        return {
            'bootstrapped': self.bootstrapped,
            'peer_database_size': len(self.peer_db),
            'active_connections': len(self.connections),
            'blocks_broadcast': self.stats['blocks_sent'],
            'top_peers': sorted(
                [(addr, info['score'], info['successful_broadcasts']) 
                 for addr, info in self.peer_db.items()],
                key=lambda x: x[1],
                reverse=True
            )[:10]
        }
```

#### 2. **Independent Peer Discovery & Management**

P2Pool maintains its own peer database, independent of dashd:

**Bootstrap phase (one-time):**
```python
# On startup, get initial peer list from dashd
initial_peers = yield dashd.rpc_getpeerinfo()  # 20-50 peers
broadcaster.bootstrap_from_dashd(initial_peers)
```

**Discovery phase (continuous):**
```python
# Connected peers tell us about OTHER peers via 'addr' messages
# This is standard Bitcoin/Dash P2P protocol behavior

Peer A → P2Pool: "I know about peers B, C, D, E..."
Peer B → P2Pool: "I know about peers F, G, H, I..."
Peer C → P2Pool: "I know about peers J, K, L, M..."

# P2Pool builds a database of 100s of potential peers
```

**Selection phase (periodic):**
```python
# Every 60 seconds, score all known peers and connect to best 20
def calculate_peer_score(peer):
    score = 0
    
    # Historical success rate (most important)
    if peer.successful_broadcasts > 0:
        success_rate = peer.successful_broadcasts / peer.total_broadcasts
        score += success_rate * 100
    
    # Recency (is peer still active?)
    hours_since_seen = (now - peer.last_seen) / 3600
    if hours_since_seen < 1:    score += 50
    elif hours_since_seen < 6:  score += 30
    elif hours_since_seen < 24: score += 10
    else:                        score -= 20  # Stale peer
    
    # Trust level (dashd peers = more trusted)
    if peer.source == 'dashd':
        score += 30
    
    return score

# Connect to top 20, disconnect from bottom ones
top_peers = sorted(all_peers, key=calculate_peer_score)[:20]
```

**Benefits:**
- ✅ **Independent of dashd**: P2Pool network survives even if local dashd restarts
- ✅ **Self-healing**: Automatically discovers new peers, prunes dead ones
- ✅ **Quality-driven**: Continuously improves peer quality based on actual performance
- ✅ **Scalable**: Database can grow to 100s of peers, but only connects to best 20
- ✅ **Resilient**: If dashd dies, P2Pool still has its own peer connections

#### 3. **Enhanced Block Submission**
```python
@defer.inlineCallbacks
def submit_block_multi_peer(block, broadcaster, dashd, dashd_work, net):
    """Submit block via parallel broadcast to ALL peers simultaneously
    
    New strategy (MAXIMUM SPEED):
    - All peers receive block at the same time via P2P
    - No sequential ordering, pure parallel broadcast
    - Local dashd + 19 network peers = 20 simultaneous sends
    - Also submit via RPC for verification/fallback
    """
    
    block_hash = dash_data.hash256(dash_data.block_header_type.pack(block['header']))
    
    print ''
    print '=' * 70
    print 'PARALLEL BLOCK BROADCAST STARTED'
    print '  Block hash: %064x' % block_hash
    print '  Target peers: %d (including local dashd)' % broadcaster.get_connected_count()
    print '=' * 70
    
    results = {}
    
    # 1. PARALLEL P2P BROADCAST to ALL peers (local dashd + network peers)
    #    This is the FASTEST path - all peers get block simultaneously
    try:
        start_time = time.time()
        peers_reached = yield broadcaster.broadcast_block(block)
        broadcast_time = time.time() - start_time
        
        results['parallel_p2p'] = {
            'success': peers_reached > 0,
            'time': broadcast_time,
            'peers_reached': peers_reached
        }
        print 'Parallel P2P broadcast: %d peers in %.3fs (%.1f peers/sec)' % (
            peers_reached, broadcast_time, peers_reached / broadcast_time if broadcast_time > 0 else 0)
    except Exception as e:
        results['parallel_p2p'] = {'success': False, 'error': str(e)}
        print >>sys.stderr, 'Parallel P2P broadcast failed: %s' % e
    
    # 2. RPC FALLBACK/VERIFICATION (for compatibility and verification)
    #    Runs after P2P, but P2P usually wins the race
    try:
        yield submit_block_rpc(block, False, dashd, dashd_work, net)
        results['rpc'] = {'success': True}
    except Exception as e:
        results['rpc'] = {'success': False, 'error': str(e)}
    
    # Log overall results
    print ''
    print 'BLOCK SUBMISSION SUMMARY:'
    print '  Parallel P2P: %s (%d peers)' % (
        '✓' if results['parallel_p2p']['success'] else '✗',
        results['parallel_p2p'].get('peers_reached', 0))
    print '  RPC verify:   %s' % ('✓' if results['rpc']['success'] else '✗')
    print '=' * 70
    
    # Success if P2P succeeded (RPC is just verification)
    defer.returnValue(results['parallel_p2p']['success'])
```

---

## Implementation Plan

### Phase 1: Core Infrastructure (Week 1)
**Files to create:**
- `p2pool/dash/broadcaster.py` - DashNetworkBroadcaster class with independent peer management
- `p2pool/dash/peer_database.py` - Peer storage and scoring system

**Tasks:**
1. Implement one-time bootstrap from dashd.rpc_getpeerinfo()
2. Implement P2P 'addr' message handling for peer discovery
3. Build independent peer database with quality scoring
4. Automatic peer refresh and rotation (every 60s)
5. Connection health monitoring
6. Peer database persistence (save to disk)

### Phase 2: Integration (Week 2)
**Files to modify:**
- `p2pool/dash/helper.py` - Add submit_block_multi_peer()
- `p2pool/main.py` - Initialize broadcaster at startup
- `p2pool/work.py` - Update block found callback

**Tasks:**
1. Integrate broadcaster into main initialization
2. Update submit_block() to use multi-peer
3. Add command-line arguments
4. Backward compatibility (fallback to single peer)

### Phase 3: Monitoring & Stats (Week 3)
**Files to create:**
- `p2pool/web_handlers/broadcast_stats.py` - Web dashboard

**Files to modify:**
- `web-static/dashboard.html` - Add broadcast statistics panel

**Tasks:**
1. Per-peer performance tracking
2. Web dashboard with live stats
3. Prometheus-style metrics export
4. Alerting for peer failures

### Phase 4: Testing & Optimization (Week 4)
**Files to create:**
- `test_broadcaster.py` - Unit tests
- `test_block_propagation.py` - Integration tests

**Tasks:**
1. Regtest environment testing
2. Testnet validation
3. Performance benchmarking
4. Mainnet deployment

---

## Peer Database Persistence

**Storage format (JSON):**
```json
{
  "version": "1.0",
  "last_updated": 1703174400,
  "local_dashd": "127.0.0.1:9999",
  "peers": {
    "127.0.0.1:9999": {
      "first_seen": 1703000000,
      "last_seen": 1703174400,
      "source": "local_dashd",
      "protected": true,
      "successful_broadcasts": 154,
      "failed_broadcasts": 0,
      "score": 999999
    },
    "123.45.67.89:9999": {
      "first_seen": 1703000000,
      "last_seen": 1703174300,
      "source": "dashd",
      "protected": false,
      "successful_broadcasts": 47,
      "failed_broadcasts": 2,
      "score": 195.5
    },
    "98.76.54.32:9999": {
      "first_seen": 1703010000,
      "last_seen": 1703174200,
      "source": "p2p_discovery",
      "protected": false,
      "successful_broadcasts": 32,
      "failed_broadcasts": 1,
      "score": 178.2
    }
  }
}
```

**Note:** Local dashd is stored with `protected: true` flag and infinite score to ensure it's never removed.

**Save location:**
- `data/dash/broadcast_peers.json` (mainnet)
- `data/dash_testnet/broadcast_peers.json` (testnet)

**Save frequency:**
- Every 5 minutes (incremental updates)
- On shutdown (full save)
- After successful block broadcast (update scores)

**Benefits:**
- ✅ Fast startup (no need to re-bootstrap from dashd)
- ✅ Preserves peer quality history
- ✅ Survives P2Pool restarts
- ✅ Can share peer database between instances

---

## Configuration

### Command-Line Arguments
```bash
# Enable multi-peer broadcasting (default: enabled)
--broadcast-enabled

# Maximum number of active broadcast peers (default: 20)
--broadcast-max-peers NUM

# Minimum required peers for health (default: 5)
--broadcast-min-peers NUM

# Peer refresh/rotation interval in seconds (default: 60)
--broadcast-refresh-interval SEC

# Skip dashd bootstrap, use saved peer database
--broadcast-skip-bootstrap

# Peer database file location
--broadcast-peer-db FILE

# Disable broadcast fallback to local-only
--broadcast-no-fallback
```

### Automatic Peer Management

**Phase 1: Bootstrap (one-time on startup)**
```
P2Pool → dashd.rpc_getpeerinfo() → Get 20-50 initial peers
       ↓
Add to peer database with "dashd" source (trusted)
       ↓
Connect to top 20 peers immediately
```

**Phase 2: Discovery (continuous)**
```
Connected Peers → Send 'getaddr' request
              ↓
Peers respond with 'addr' messages (lists of other peers)
              ↓
P2Pool adds new peers to database
              ↓
Database grows to 100s of potential peers
```

**Phase 3: Quality Management (every 60 seconds)**
```
For each peer in database:
  - Calculate quality score
    * +100 points: successful broadcast history
    * +50 points: seen recently (< 1 hour)
    * +30 points: from dashd (trusted source)
    * -20 points: stale (not seen in 24h)
  
Sort by score → Connect to top 20 → Disconnect from rest
```

**Example peer database evolution:**
```
Startup:     20 peers (from dashd)
1 hour:      150 peers (discovered via P2P)
24 hours:    500 peers (network-wide discovery)
1 week:      800 peers (pruned stale ones)

Always connected to best 20 at any moment
```

---

## Performance Expectations

### Current System (Single dashd)
```
Block found → dashd → network propagation
Latency: 50-200ms to first peer
Full network: 2-5 seconds
```

### Enhanced System (Parallel Multi-peer)
```
Block found → 20 peers simultaneously (TRUE PARALLEL)
Latency: 50-100ms to ALL peers (parallel, not sequential)
Full network: 0.5-2 seconds (60-75% improvement)
```

**Performance breakdown:**
- **Old sequential**: dashd (50ms) → peer1 (100ms) → peer2 (150ms) → ... = cumulative delays
- **New parallel**: All 20 peers in ~50-100ms simultaneously = NO cumulative delay
- **Network saturation**: With 20 well-connected peers, block reaches 80%+ of network in <1s

### Orphan Rate Impact
- **Current**: ~0.6% orphan rate (network average)
- **Expected**: ~0.3-0.4% orphan rate (50% reduction)
- **Competitive advantage**: Matches large pool propagation speed

---

## Security Considerations

### DoS Protection
- Limit connections to trusted/verified peers
- Rate limiting per peer
- Connection timeout handling
- Bandwidth monitoring

### Privacy
- No sensitive data exposed in peer list
- Optional Tor support for privacy
- Randomized connection ordering

### Reliability
- Graceful degradation to single-peer mode
- Automatic peer health checking
- Failover to backup peers
- Circuit breaker pattern for failing peers

---

## Monitoring Metrics

### Key Performance Indicators
1. **Broadcast Success Rate**: % of blocks successfully sent to peers
2. **Average Broadcast Time**: Mean time to send to all peers
3. **Peer Availability**: % of configured peers online
4. **Orphan Rate**: % of found blocks that become orphaned
5. **Network Propagation Time**: Time until block appears on explorers

### Dashboard Panels
```
┌─────────────────────────────────────────────────────┐
│  Broadcast Statistics                               │
├─────────────────────────────────────────────────────┤
│  Connected Peers: 15/20                             │
│  Blocks Broadcast: 47                               │
│  Success Rate: 98.7%                                │
│  Avg Broadcast Time: 87ms                           │
│  Last Block: 2 minutes ago                          │
├─────────────────────────────────────────────────────┤
│  Peer Health:                                       │
│    ✓ dashd (local)           - 100% (47/47)        │
│    ✓ insight.dash.org        - 100% (47/47)        │
│    ✓ seed.dashninja.pl       - 97.8% (46/47)       │
│    ✗ peer.example.com        - 0% (reconnecting)    │
└─────────────────────────────────────────────────────┘
```

---

## Backward Compatibility

The enhancement is **fully backward compatible**:

1. **Default behavior**: Multi-peer enabled with fallback
2. **Disable flag**: `--broadcast-peers=false` reverts to v1.3.0 behavior
3. **No peer config**: Falls back to local dashd only
4. **Connection failure**: Automatically degrades to single-peer mode

---

## Success Criteria

### Must Have (Launch Blockers)
- ✅ Parallel block broadcast to 5+ peers
- ✅ Automatic peer reconnection
- ✅ Graceful fallback to single peer
- ✅ Zero impact on existing functionality
- ✅ Successful regtest/testnet validation

### Should Have (Post-Launch)
- ✅ Web dashboard with stats
- ✅ Prometheus metrics export
- ✅ Peer reputation scoring
- ✅ Geographic diversity in peer selection

### Nice to Have (Future)
- ✅ Machine learning for optimal peer selection
- ✅ Automatic peer discovery from network
- ✅ ChainLock propagation optimization
- ✅ InstantSend transaction broadcasting

---

## Timeline

| Phase | Duration | Deliverable |
|-------|----------|-------------|
| Phase 1 | Week 1 | Core broadcaster infrastructure |
| Phase 2 | Week 2 | Integration with existing code |
| Phase 3 | Week 3 | Monitoring & web dashboard |
| Phase 4 | Week 4 | Testing & optimization |
| **Total** | **4 weeks** | **Production-ready v1.4.0** |

---

## Testing Strategy

### Unit Tests
- DashNetworkBroadcaster connection management
- Peer configuration parsing
- Error handling and retry logic

### Integration Tests
- Regtest: 3-node network block propagation
- Testnet: Real network validation
- Performance: Benchmark vs single-peer

### Stress Tests
- 100+ blocks in rapid succession
- Peer disconnection scenarios
- Network partition recovery

---

## Risk Mitigation

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| Peer connection failures | Medium | High | Automatic reconnection, fallback |
| Increased bandwidth usage | Low | Medium | Rate limiting, configurable peers |
| Block propagation errors | Critical | Low | Multi-path redundancy, RPC verify |
| Configuration complexity | Low | Medium | Sane defaults, auto-discovery |

---

## References

- **FUTURE.md**: Original feature proposal (lines 34-85)
- **Current implementation**: `p2pool/dash/helper.py` submit_block()
- **Dash P2P protocol**: `p2pool/dash/p2p.py`
- **Bitcoin Relay Network**: Similar concept for Bitcoin
- **Falcon Network**: Research on fast block propagation

---

**Status**: Design Phase - Ready for Implementation
**Target Release**: v1.4.0
**Priority**: Critical (competitive advantage)
