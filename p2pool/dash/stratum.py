import random
import sys
import time
import weakref

from twisted.internet import protocol, reactor
from twisted.python import log

from p2pool.dash import data as dash_data, getwork
from p2pool.util import expiring_dict, jsonrpc, pack


def clip(num, bot, top):
    return min(top, max(bot, num))


# ==============================================================================
# GLOBAL POOL STATISTICS AND CONNECTION MANAGEMENT
# ==============================================================================

class PoolStatistics(object):
    """
    Global pool statistics tracker for performance monitoring and load management.
    
    This class tracks:
    - Total connected workers
    - Per-worker hash rates and share counts
    - Global share submission rate (for load protection)
    - Connection history for session resumption
    
    PERFORMANCE SAFEGUARDS:
    - Enforces minimum difficulty floor to prevent server overload
    - Tracks global submission rate to detect/prevent DoS
    - Limits total concurrent connections
    """
    
    # Class-level singleton instance
    _instance = None
    
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = PoolStatistics()
        return cls._instance
    
    def __init__(self):
        # Connection tracking (weak refs to auto-cleanup disconnected workers)
        self.connections = weakref.WeakValueDictionary()
        self.connection_count = 0
        
        # Per-worker statistics {worker_name: {shares, hash_rate, last_seen, ...}}
        self.worker_stats = {}
        
        # Global submission rate tracking (for DoS protection)
        self.global_submissions = []  # List of (timestamp, difficulty) tuples
        self.global_submission_window = 60  # Track last 60 seconds
        
        # Session storage for resumption {session_id: session_data}
        self.sessions = expiring_dict.ExpiringDict(3600)  # 1 hour session timeout
        
        # Pool-wide configuration limits
        self.MAX_CONNECTIONS = 10000  # Maximum concurrent connections
        self.MIN_DIFFICULTY_FLOOR = 0.001  # Absolute minimum difficulty (pool protection)
        self.MAX_SUBMISSIONS_PER_SECOND = 1000  # Global rate limit
        
        # Performance metrics
        self.total_shares_accepted = 0
        self.total_shares_rejected = 0
        self.startup_time = time.time()
    
    def register_connection(self, conn_id, connection):
        """Register a new stratum connection"""
        self.connections[conn_id] = connection
        self.connection_count = len(self.connections)
        return self.connection_count < self.MAX_CONNECTIONS
    
    def unregister_connection(self, conn_id):
        """Unregister a stratum connection"""
        if conn_id in self.connections:
            del self.connections[conn_id]
        self.connection_count = len(self.connections)
    
    def record_share(self, worker_name, difficulty, accepted=True):
        """Record a share submission for statistics"""
        now = time.time()
        
        # Update global submission tracking
        self.global_submissions.append((now, difficulty))
        # Prune old entries
        cutoff = now - self.global_submission_window
        self.global_submissions = [(t, d) for t, d in self.global_submissions if t > cutoff]
        
        # Update worker stats
        if worker_name not in self.worker_stats:
            self.worker_stats[worker_name] = {
                'shares': 0,
                'accepted': 0,
                'rejected': 0,
                'hash_rate': 0.0,
                'last_seen': now,
                'first_seen': now,
                'difficulties': [],
            }
        
        stats = self.worker_stats[worker_name]
        stats['shares'] += 1
        stats['last_seen'] = now
        
        if accepted:
            stats['accepted'] += 1
            self.total_shares_accepted += 1
        else:
            stats['rejected'] += 1
            self.total_shares_rejected += 1
        
        # Track recent difficulties for hash rate estimation
        stats['difficulties'].append((now, difficulty))
        # Keep last 100 entries
        if len(stats['difficulties']) > 100:
            stats['difficulties'] = stats['difficulties'][-100:]
        
        # Estimate hash rate from recent shares
        if len(stats['difficulties']) >= 2:
            oldest_t, oldest_d = stats['difficulties'][0]
            time_span = now - oldest_t
            if time_span > 0:
                total_work = sum(d for t, d in stats['difficulties'])
                # Hash rate = total_difficulty * 2^32 / time_span
                stats['hash_rate'] = (total_work * (2**32)) / time_span
    
    def get_global_submission_rate(self):
        """Get current global submission rate (shares/second)"""
        now = time.time()
        cutoff = now - self.global_submission_window
        recent = [(t, d) for t, d in self.global_submissions if t > cutoff]
        if len(recent) < 2:
            return 0.0
        return len(recent) / self.global_submission_window
    
    def is_submission_rate_safe(self):
        """Check if global submission rate is within safe limits"""
        return self.get_global_submission_rate() < self.MAX_SUBMISSIONS_PER_SECOND
    
    def get_safe_minimum_difficulty(self, requested_difficulty):
        """
        Calculate safe minimum difficulty considering pool load.
        
        PERFORMANCE SAFEGUARD: This prevents miners from setting too-low difficulty
        that would overwhelm the pool with share submissions.
        
        Args:
            requested_difficulty: The difficulty requested by the miner
            
        Returns:
            Safe difficulty (may be higher than requested if pool is under load)
        """
        # Absolute floor - never go below this
        safe_diff = max(requested_difficulty, self.MIN_DIFFICULTY_FLOOR)
        
        # If submission rate is high, enforce higher minimum
        submission_rate = self.get_global_submission_rate()
        if submission_rate > self.MAX_SUBMISSIONS_PER_SECOND * 0.5:
            # Pool is under load - increase minimum difficulty
            load_factor = submission_rate / (self.MAX_SUBMISSIONS_PER_SECOND * 0.5)
            safe_diff = max(safe_diff, self.MIN_DIFFICULTY_FLOOR * load_factor * 10)
            print 'POOL LOAD: High submission rate (%.1f/s), enforcing min diff %.4f' % (
                submission_rate, safe_diff)
        
        return safe_diff
    
    def store_session(self, session_id, session_data):
        """Store session data for potential resumption"""
        self.sessions[session_id] = session_data
    
    def get_session(self, session_id):
        """Retrieve stored session data"""
        return self.sessions.get(session_id)
    
    def get_worker_stats(self, worker_name=None):
        """Get statistics for a specific worker or all workers"""
        if worker_name:
            return self.worker_stats.get(worker_name)
        return self.worker_stats
    
    def get_pool_stats(self):
        """Get overall pool statistics"""
        now = time.time()
        return {
            'connections': self.connection_count,
            'workers': len(self.worker_stats),
            'total_accepted': self.total_shares_accepted,
            'total_rejected': self.total_shares_rejected,
            'submission_rate': self.get_global_submission_rate(),
            'uptime': now - self.startup_time,
        }


# Global pool statistics instance
pool_stats = PoolStatistics.get_instance()


class StratumRPCMiningProvider(object):
    def __init__(self, wb, other, transport):
        self.pool_version_mask = 0x1fffe000  # BIP320 standard mask for ASICBOOST
        self.wb = wb
        self.other = other
        self.transport = transport
        
        self.username = None
        self.worker_ip = transport.getPeer().host if transport else None  # Track worker IP
        self.handler_map = expiring_dict.ExpiringDict(300)
        
        # Extranonce support for ASICs
        self.extranonce_subscribe = False
        self.extranonce1 = ""
        self.last_extranonce_update = 0
        
        self.watch_id = self.wb.new_work_event.watch(self._send_work)
        
        self.recent_shares = []
        self.target = None
        self.share_rate = wb.share_rate  # From command-line or default (10 sec)
        self.fixed_target = False
        self.desired_pseudoshare_target = None
        
        # ==== NEW: Enhanced difficulty management ====
        # Suggested difficulty from miner (mining.suggest_difficulty)
        self.suggested_difficulty = None
        # Minimum difficulty floor from BIP310 minimum-difficulty extension
        self.minimum_difficulty = None
        # Dynamic share rate (can be overridden per-worker)
        self.worker_share_rate = None
        
        # ==== NEW: Session management ====
        self.session_id = '%016x' % random.randrange(2**64)
        self.connection_time = time.time()
        
        # ==== NEW: Connection tracking ====
        self.conn_id = id(self)
        pool_stats.register_connection(self.conn_id, self)
        
        # ==== NEW: Per-connection statistics ====
        self.shares_submitted = 0
        self.shares_accepted = 0
        self.shares_rejected = 0
        self.last_share_time = None
    
    def rpc_subscribe(self, miner_version=None, session_id=None, *args):
        """
        Handle mining.subscribe - the first message from a miner.
        
        Supports session resumption: if session_id is provided and valid,
        restore the previous session state.
        
        Args:
            miner_version: Miner software identification string
            session_id: Optional session ID for resumption
            *args: Additional arguments (ignored)
        
        Returns:
            [subscription_details, extranonce1, extranonce2_size]
        """
        print 'STRATUM: mining.subscribe from %s - miner: %s, session: %s' % (
            self.worker_ip, miner_version, session_id[:16] if session_id else 'new')
        
        # ==== NEW: Session resumption support ====
        if session_id:
            stored_session = pool_stats.get_session(session_id)
            if stored_session:
                # Restore session state
                self.session_id = session_id
                self.target = stored_session.get('target')
                self.suggested_difficulty = stored_session.get('suggested_difficulty')
                self.minimum_difficulty = stored_session.get('minimum_difficulty')
                self.worker_share_rate = stored_session.get('share_rate')
                print 'STRATUM: Resumed session %s for %s' % (session_id[:16], self.worker_ip)
        
        reactor.callLater(0, self._send_work)
        
        return [
            [["mining.set_difficulty", "ae6812eb4cd7735a302a8a9dd95cf71f1"], ["mining.notify", "ae6812eb4cd7735a302a8a9dd95cf71f2"]], # subscription details
            self.extranonce1 if self.extranonce1 else "",  # extranonce1 (use stored if resuming)
            self.wb.COINBASE_NONCE_LENGTH,  # extranonce2_size
            self.session_id,  # Return session ID for potential future resumption
        ]
    
    def rpc_authorize(self, username, password):
        if not hasattr(self, 'authorized'):  # authorize can be called many times in one connection
            self.authorized = username
        self.username = username.strip()
        print 'STRATUM: mining.authorize from %s - worker: %s' % (self.worker_ip, self.username)
        
        # ==== ENHANCED: Parse username with extended format support ====
        # Username formats supported:
        #   address                      - Basic address
        #   address+difficulty           - Address with pseudoshare difficulty
        #   address/share_difficulty     - Address with share difficulty
        #   address+diff/sharediff       - Both difficulties
        #   address+diff+sN              - Difficulty + share rate N seconds
        #   address.worker               - Address with worker name
        #   address_worker               - Address with worker name (alt format)
        
        self.user = self.username
        self.address = self.username.split('+')[0].split('/')[0].split('.')[0].split('_')[0]
        self.desired_share_target = None
        self.desired_pseudoshare_target = None
        
        # Parse extended options from username
        parts = self.username.replace('/', '+').split('+')
        for part in parts[1:]:
            if part.startswith('s') and part[1:].isdigit():
                # Dynamic share rate: +s5 means 5 seconds per share
                requested_rate = float(part[1:])
                # Clamp to reasonable range (1-60 seconds)
                self.worker_share_rate = clip(requested_rate, 1.0, 60.0)
                print 'STRATUM: Worker %s requested share rate: %.1fs' % (self.username, self.worker_share_rate)
            elif part.replace('.', '').isdigit():
                # Difficulty specification
                try:
                    diff = float(part)
                    self.desired_pseudoshare_target = dash_data.difficulty_to_target(diff)
                except:
                    pass
        
        reactor.callLater(0, self._send_work)
        return True
    
    # ==== NEW: mining.suggest_difficulty implementation ====
    def rpc_suggest_difficulty(self, difficulty):
        """
        Handle mining.suggest_difficulty - allows miner to suggest initial difficulty.
        
        PERFORMANCE SAFEGUARD: The suggested difficulty is subject to:
        1. Pool-wide minimum difficulty floor (prevents server overload)
        2. Dynamic adjustment based on pool load
        3. The normal vardiff algorithm will still adjust from this starting point
        
        Args:
            difficulty: Suggested starting difficulty (float)
        
        Returns:
            True on acceptance, the actual difficulty may differ from suggested
        """
        try:
            requested_diff = float(difficulty)
        except (ValueError, TypeError):
            print 'STRATUM: Invalid suggest_difficulty from %s: %s' % (self.worker_ip, difficulty)
            return False
        
        # PERFORMANCE SAFEGUARD: Apply pool-wide safety limits
        safe_diff = pool_stats.get_safe_minimum_difficulty(requested_diff)
        
        # Also respect any minimum-difficulty set by BIP310 extension
        if self.minimum_difficulty is not None:
            safe_diff = max(safe_diff, self.minimum_difficulty)
        
        self.suggested_difficulty = safe_diff
        self.target = dash_data.difficulty_to_target(safe_diff)
        
        # Log if we had to adjust the difficulty
        if safe_diff != requested_diff:
            print 'STRATUM: suggest_difficulty from %s: requested %.4f, using %.4f (pool safeguard)' % (
                self.worker_ip, requested_diff, safe_diff)
        else:
            print 'STRATUM: suggest_difficulty from %s: accepted %.4f' % (self.worker_ip, safe_diff)
        
        # Send immediate difficulty update to miner
        self.other.svc_mining.rpc_set_difficulty(safe_diff).addErrback(lambda err: None)
        
        return True
    
    def rpc_configure(self, extensions, extensionParameters):
        """
        Handle mining.configure (BIP310) - negotiate protocol extensions.
        
        Supported extensions:
        - version-rolling (BIP320 ASICBoost)
        - minimum-difficulty (sets difficulty floor for this connection)
        - subscribe-extranonce (NiceHash-style extranonce updates)
        
        Args:
            extensions: List of extension codes to negotiate
            extensionParameters: Dict of parameters for each extension
        
        Returns:
            Dict of negotiated extension results
        """
        print 'STRATUM: mining.configure from %s - extensions: %s' % (self.worker_ip, extensions)
        
        result = {}
        
        if 'version-rolling' in extensions:
            # ASICBoost support (BIP320)
            miner_mask = extensionParameters.get('version-rolling.mask', '0')
            try:
                minbitcount = extensionParameters.get('version-rolling.min-bit-count', 2)
            except:
                log.err("A miner tried to connect with a malformed version-rolling.min-bit-count parameter. This is probably a bug in your mining software. Braiins OS is known to have this bug. You should complain to them.")
                minbitcount = 2
            # Return the negotiated mask (pool mask AND miner mask)
            negotiated_mask = self.pool_version_mask & int(miner_mask, 16)
            result["version-rolling"] = True
            result["version-rolling.mask"] = '{:08x}'.format(negotiated_mask)
            print 'STRATUM: ASICBoost enabled for %s - mask: %08x' % (self.worker_ip, negotiated_mask)
        
        # ==== NEW: minimum-difficulty BIP310 extension ====
        if 'minimum-difficulty' in extensions:
            requested_min = extensionParameters.get('minimum-difficulty.value', 1)
            try:
                requested_min = float(requested_min)
            except (ValueError, TypeError):
                requested_min = 1.0
            
            # PERFORMANCE SAFEGUARD: Apply pool-wide safety limits
            safe_min = pool_stats.get_safe_minimum_difficulty(requested_min)
            
            self.minimum_difficulty = safe_min
            result["minimum-difficulty"] = True
            
            # If this sets a floor higher than current target, update target
            if self.target is not None:
                current_diff = dash_data.target_to_difficulty(self.target)
                if current_diff < safe_min:
                    self.target = dash_data.difficulty_to_target(safe_min)
                    print 'STRATUM: minimum-difficulty raised current diff from %.4f to %.4f for %s' % (
                        current_diff, safe_min, self.worker_ip)
            
            if safe_min != requested_min:
                print 'STRATUM: minimum-difficulty from %s: requested %.4f, enforcing %.4f (pool safeguard)' % (
                    self.worker_ip, requested_min, safe_min)
            else:
                print 'STRATUM: minimum-difficulty enabled for %s: %.4f' % (self.worker_ip, safe_min)
            
        if 'subscribe-extranonce' in extensions:
            # Enable extranonce subscription for this connection (required for ASICs)
            self.extranonce_subscribe = True
            result["subscribe-extranonce"] = True
            print 'STRATUM: Extranonce subscription enabled for %s' % self.worker_ip
        
        return result if result else None
    
    # ==== NEW: client.reconnect for load balancing ====
    def rpc_reconnect(self, hostname=None, port=None, wait_time=0):
        """
        Request client to reconnect (for load balancing).
        
        This is typically called BY THE POOL to ask a miner to reconnect,
        possibly to a different server.
        
        Args:
            hostname: New hostname to connect to (optional)
            port: New port to connect to (optional)
            wait_time: Seconds to wait before reconnecting
        
        Returns:
            True
        """
        # Store session before disconnect for potential resumption
        pool_stats.store_session(self.session_id, {
            'target': self.target,
            'suggested_difficulty': self.suggested_difficulty,
            'minimum_difficulty': self.minimum_difficulty,
            'share_rate': self.worker_share_rate,
            'username': self.username,
            'worker_ip': self.worker_ip,
        })
        print 'STRATUM: Stored session %s for reconnect' % self.session_id[:16]
        return True
    
    def _send_work(self):
        try:
            x, got_response = self.wb.get_work(*self.wb.preprocess_request('' if self.username is None else self.username))
        except Exception as e:
            # Don't disconnect for temporary errors like "lost contact with dashd"
            # Just log and skip this work update - miner will keep working on old job
            error_msg = str(e)
            if 'lost contact' in error_msg or 'not connected' in error_msg:
                # Temporary error - don't spam logs, just skip
                pass
            else:
                log.err(None, 'Error getting work for stratum:')
            return
        
        if self.desired_pseudoshare_target:
            # Fixed target from username parsing (e.g., "wallet+1000")
            # Note: we do NOT cap this at P2Pool share floor - vardiff should be
            # allowed to go harder than the (potentially easy) bootstrap P2Pool chain
            self.fixed_target = True
            self.target = self.desired_pseudoshare_target
        else:
            self.fixed_target = False
            # Start at a reasonable default difficulty that will quickly adjust via vardiff
            # Don't cap at min_share_target - that would prevent vardiff from going harder
            if self.target is None:
                # ==== ENHANCED: Use suggested_difficulty if provided ====
                if self.suggested_difficulty is not None:
                    initial_diff = self.suggested_difficulty
                    print 'STRATUM: Worker starting at suggested difficulty %.4f' % initial_diff
                else:
                    # Default: start at difficulty 100
                    # This is a balance between:
                    # - Not too low (diff 1 causes share flood from ASICs)
                    # - Not too high (diff 10000 makes slow miners wait too long)
                    # At 1.7 TH/s ASIC: ~0.25 sec/share (will ramp up quickly)
                    # At 100 MH/s GPU: ~4 sec/share (reasonable)
                    initial_diff = 100
                    print 'STRATUM: New worker starting at default difficulty %d' % initial_diff
                
                # PERFORMANCE SAFEGUARD: Apply pool-wide minimum
                initial_diff = pool_stats.get_safe_minimum_difficulty(initial_diff)
                
                diff1_target = 0xFFFF * 2**208  # Standard bdiff difficulty 1 target
                self.target = diff1_target // int(initial_diff)
        
        # ==== ENHANCED: Enforce minimum difficulty floor ====
        if self.minimum_difficulty is not None and self.target is not None:
            current_diff = dash_data.target_to_difficulty(self.target)
            if current_diff < self.minimum_difficulty:
                self.target = dash_data.difficulty_to_target(self.minimum_difficulty)
        
        # For ASIC compatibility: periodically send extranonce updates
        # Even with empty extranonce, this helps ASICs reset their state
        if self.extranonce_subscribe:
            current_time = time.time()
            # Send extranonce update every 30 seconds or on first work
            if current_time - self.last_extranonce_update > 30:
                self._notify_extranonce_change()
                self.last_extranonce_update = current_time
        
        # Use short job IDs for ASIC compatibility (8 hex chars max)
        # ASICs like Antminer truncate long job IDs causing "job_id does not change" errors
        jobid = '%08x' % random.randrange(2**32)
        job_target = self.target  # Capture the target that will be sent with this job
        self.other.svc_mining.rpc_set_difficulty(dash_data.target_to_difficulty(job_target)).addErrback(lambda err: None)
        self.other.svc_mining.rpc_notify(
            jobid, # jobid
            getwork._swap4(pack.IntType(256).pack(x['previous_block'])).encode('hex'), # prevhash
            x['coinb1'].encode('hex'), # coinb1
            x['coinb2'].encode('hex'), # coinb2
            [pack.IntType(256).pack(s).encode('hex') for s in x['merkle_link']['branch']], # merkle_branch
            getwork._swap4(pack.IntType(32).pack(x['version'])).encode('hex'), # version
            getwork._swap4(pack.IntType(32).pack(x['bits'].bits)).encode('hex'), # nbits
            getwork._swap4(pack.IntType(32).pack(x['timestamp'])).encode('hex'), # ntime
            True, # clean_jobs
        ).addErrback(lambda err: None)
        self.handler_map[jobid] = x, got_response, job_target  # Store job_target with the job
    
    def rpc_submit(self, worker_name, job_id, extranonce2, ntime, nonce, version_bits=None, *args):
        # ASICBOOST: version_bits is the version mask that the miner used
        t0 = time.time()  # Benchmarking start
        worker_name = worker_name.strip()
        
        # Rate limiting: drop submissions if coming too fast (more than 100/sec)
        # This prevents server overload when difficulty is too low
        now = time.time()
        if not hasattr(self, '_last_submit_time'):
            self._last_submit_time = 0
            self._rapid_submit_count = 0
        
        time_since_last = now - self._last_submit_time
        if time_since_last < 0.01:  # More than 100 submissions/sec
            self._rapid_submit_count += 1
            if self._rapid_submit_count > 10:
                # Drop submission silently to reduce load, but still return True
                # to avoid miner reconnecting
                return True
        else:
            self._rapid_submit_count = 0
        self._last_submit_time = now
        
        if job_id not in self.handler_map:
            print >>sys.stderr, 'Stale job submission from %s: job_id=%s not found' % (worker_name, job_id[:16])
            # Return False for stale jobs instead of raising exception
            # This is more compatible with various miner implementations
            return False
        
        x, got_response, job_target = self.handler_map[job_id]  # Retrieve job_target
        
        try:
            coinb_nonce = extranonce2.decode('hex')
        except Exception as e:
            print >>sys.stderr, 'Invalid extranonce2 from %s: %s' % (worker_name, str(e))
            return False
        
        if len(coinb_nonce) != self.wb.COINBASE_NONCE_LENGTH:
            print >>sys.stderr, 'Invalid extranonce2 length from %s: got %d, expected %d' % (worker_name, len(coinb_nonce), self.wb.COINBASE_NONCE_LENGTH)
            return False
        
        new_packed_gentx = x['coinb1'] + coinb_nonce + x['coinb2']
        
        job_version = x['version']
        nversion = job_version
        
        # Check if miner changed bits that they were not supposed to change
        if version_bits:
            if ((~self.pool_version_mask) & int(version_bits, 16)) != 0:
                # Protocol does not say error needs to be returned but ckpool returns
                # {"error": "Invalid version mask", "id": "id", "result":""}
                raise ValueError("Invalid version mask {0}".format(version_bits))
            nversion = (job_version & ~self.pool_version_mask) | (int(version_bits, 16) & self.pool_version_mask)
        
        header = dict(
            version=nversion,
            previous_block=x['previous_block'],
            merkle_root=dash_data.check_merkle_link(dash_data.hash256(new_packed_gentx), x['merkle_link']),
            timestamp=pack.IntType(32).unpack(getwork._swap4(ntime.decode('hex'))),
            bits=x['bits'],
            nonce=pack.IntType(32).unpack(getwork._swap4(nonce.decode('hex'))),
        )
        # Dash's got_response takes 4 args: (header, user, coinbase_nonce, submitted_target)
        # Use job_target (the target sent with THIS job) for proper validation and hashrate
        result = got_response(header, worker_name, coinb_nonce, job_target)
        
        # ==== ENHANCED: Update statistics ====
        self.shares_submitted += 1
        self.last_share_time = now
        current_diff = dash_data.target_to_difficulty(job_target)
        
        if result:
            self.shares_accepted += 1
            pool_stats.record_share(worker_name, current_diff, accepted=True)
        else:
            self.shares_rejected += 1
            pool_stats.record_share(worker_name, current_diff, accepted=False)
        
        # Aggressive vardiff: adjust difficulty to target ~share_rate seconds per pseudoshare
        # For high-hashrate ASICs, we need to ramp up difficulty quickly to avoid flooding
        if not self.fixed_target:
            self.recent_shares.append(now)
            
            # ==== ENHANCED: Use worker-specific or default share rate ====
            effective_share_rate = self.worker_share_rate if self.worker_share_rate else self.share_rate
            
            # Aggressive adjustment: trigger on fewer shares and allow larger jumps
            # Trigger if we have 3+ shares OR time exceeded (faster trigger than standard 12)
            num_shares = len(self.recent_shares)
            time_elapsed = now - self.recent_shares[0] if num_shares > 0 else 0
            target_time = num_shares * effective_share_rate
            
            # Adjust if: 3+ shares collected, OR time significantly exceeds/undershoots target
            should_adjust = (num_shares >= 3 or 
                            (num_shares >= 1 and time_elapsed > 2 * target_time) or
                            (num_shares >= 1 and time_elapsed < target_time / 4))
            
            if should_adjust and num_shares > 0:
                # Calculate actual share rate vs target
                actual_rate = time_elapsed / num_shares if num_shares > 0 else effective_share_rate
                adjustment = actual_rate / effective_share_rate
                
                # Allow larger adjustments (0.25x to 4x) for faster response
                adjustment = clip(adjustment, 0.25, 4.0)
                
                old_diff = dash_data.target_to_difficulty(self.target)
                self.target = int(self.target * adjustment + 0.5)
                
                # Clip to SANE_TARGET_RANGE only - don't cap at min_share_target
                # SANE_TARGET_RANGE[0] = hardest (lowest target, e.g. diff 10000)
                # SANE_TARGET_RANGE[1] = easiest (highest target, e.g. diff 1)
                newtarget = clip(self.target, self.wb.net.SANE_TARGET_RANGE[0], self.wb.net.SANE_TARGET_RANGE[1])
                if newtarget != self.target:
                    self.target = newtarget
                
                # ==== ENHANCED: Enforce minimum difficulty floor ====
                if self.minimum_difficulty is not None:
                    new_diff = dash_data.target_to_difficulty(self.target)
                    if new_diff < self.minimum_difficulty:
                        self.target = dash_data.difficulty_to_target(self.minimum_difficulty)
                
                # ==== ENHANCED: Enforce pool-wide minimum ====
                new_diff = dash_data.target_to_difficulty(self.target)
                safe_min = pool_stats.get_safe_minimum_difficulty(new_diff)
                if new_diff < safe_min:
                    self.target = dash_data.difficulty_to_target(safe_min)
                    new_diff = safe_min
                
                if abs(new_diff - old_diff) / max(old_diff, 0.001) > 0.1:  # Only log significant changes
                    print 'Vardiff %s: %.2f -> %.2f (%.1f shares in %.1fs, target %.1fs)' % (
                        worker_name, old_diff, new_diff, num_shares, time_elapsed, target_time)
                
                # Reset and send new work
                self.recent_shares = [now]
                self._send_work()
        
        # Benchmarking: print timing if BENCH enabled
        t1 = time.time()
        try:
            import p2pool
            if p2pool.BENCH and (t1-t0) > 0.01:  # Only log if > 10ms
                print "%8.3f ms for stratum:rpc_submit(%s)" % ((t1-t0)*1000., worker_name)
        except:
            pass
        
        return result
    
    def rpc_set_extranonce(self, extranonce1, extranonce2_size):
        """
        Handle mining.set_extranonce from pool/proxy
        
        This is sent BY THE POOL to miners when extranonce changes.
        Miners that subscribed to 'subscribe-extranonce' expect this.
        
        Args:
            extranonce1: New extranonce1 value (hex string)
            extranonce2_size: Size of extranonce2 in bytes (integer)
        
        Returns:
            True on success
        """
        if not self.extranonce_subscribe:
            # Miner didn't subscribe to extranonce updates
            return False
        
        # Update the extranonce for this connection
        if extranonce1:
            self.extranonce1 = extranonce1
        else:
            self.extranonce1 = ""
        
        if extranonce2_size != self.wb.COINBASE_NONCE_LENGTH:
            print >>sys.stderr, 'WARNING: extranonce2_size mismatch: expected %d, got %d' % (
                self.wb.COINBASE_NONCE_LENGTH, extranonce2_size)
        
        print '>>>Set extranonce: %s (size=%d) for %s' % (
            extranonce1 if extranonce1 else "(empty)", 
            extranonce2_size, 
            self.worker_ip
        )
        
        return True
    
    def _notify_extranonce_change(self, new_extranonce1=None):
        """
        Notify miners that subscribed to extranonce updates
        Called when extranonce needs to change (e.g., reconnection, long mining session)
        """
        if not self.extranonce_subscribe:
            return
        
        # Use current or new extranonce1
        extranonce1 = new_extranonce1 if new_extranonce1 is not None else self.extranonce1
        extranonce2_size = self.wb.COINBASE_NONCE_LENGTH
        
        # Send mining.set_extranonce notification to miner
        self.other.svc_mining.rpc_set_extranonce(
            extranonce1,
            extranonce2_size
        ).addErrback(lambda err: None)
        
        print '>>>Notified extranonce change to %s: %s (size=%d)' % (
            self.worker_ip,
            extranonce1 if extranonce1 else "(empty)",
            extranonce2_size
        )
    
    def close(self):
        """
        Clean up when connection closes.
        
        - Unregister from work events
        - Store session for potential resumption
        - Update pool statistics
        """
        self.wb.new_work_event.unwatch(self.watch_id)
        
        # Store session for potential resumption
        pool_stats.store_session(self.session_id, {
            'target': self.target,
            'suggested_difficulty': self.suggested_difficulty,
            'minimum_difficulty': self.minimum_difficulty,
            'share_rate': self.worker_share_rate,
            'username': self.username,
            'worker_ip': self.worker_ip,
        })
        
        # Unregister connection
        pool_stats.unregister_connection(self.conn_id)
        
        # Log disconnect with statistics
        session_duration = time.time() - self.connection_time
        print 'STRATUM: Disconnect %s (%s) - session: %s, duration: %.0fs, shares: %d/%d' % (
            self.worker_ip,
            self.username if self.username else 'unknown',
            self.session_id[:16],
            session_duration,
            self.shares_accepted,
            self.shares_submitted,
        )
    
    # ==== NEW: Get worker statistics ====
    def get_stats(self):
        """Get statistics for this connection"""
        return {
            'worker_ip': self.worker_ip,
            'username': self.username,
            'session_id': self.session_id,
            'connection_time': self.connection_time,
            'shares_submitted': self.shares_submitted,
            'shares_accepted': self.shares_accepted,
            'shares_rejected': self.shares_rejected,
            'current_difficulty': dash_data.target_to_difficulty(self.target) if self.target else None,
            'minimum_difficulty': self.minimum_difficulty,
            'suggested_difficulty': self.suggested_difficulty,
            'share_rate': self.worker_share_rate if self.worker_share_rate else self.share_rate,
        }


class ExtranonceService(object):
    """Service for NiceHash-style mining.extranonce.subscribe"""
    def __init__(self, parent):
        self.parent = parent
    
    def rpc_subscribe(self):
        """
        NiceHash-style extranonce subscription
        Called via mining.extranonce.subscribe method
        
        Many ASICs use this NiceHash protocol.
        See: https://github.com/nicehash/Specifications/blob/master/NiceHash_extranonce_subscribe_extension.txt
        
        Returns:
            True on success
        """
        self.parent.extranonce_subscribe = True
        print '>>>ExtranOnce subscribed (NiceHash method) from %s' % (self.parent.worker_ip)
        return True


# ==============================================================================
# STRATUM PROTOCOL AND FACTORY
# ==============================================================================

class StratumProtocol(jsonrpc.LineBasedPeer):
    def connectionMade(self):
        self.svc_mining = StratumRPCMiningProvider(self.factory.wb, self.other, self.transport)
        # Add extranonce service for NiceHash compatibility
        self.svc_mining.svc_extranonce = ExtranonceService(self.svc_mining)
    
    def connectionLost(self, reason):
        if hasattr(self, 'svc_mining'):
            self.svc_mining.close()


class StratumServerFactory(protocol.ServerFactory):
    protocol = StratumProtocol
    
    def __init__(self, wb):
        self.wb = wb
    
    def get_pool_stats(self):
        """Get global pool statistics"""
        return pool_stats.get_pool_stats()
    
    def get_worker_stats(self, worker_name=None):
        """Get per-worker statistics"""
        return pool_stats.get_worker_stats(worker_name)
