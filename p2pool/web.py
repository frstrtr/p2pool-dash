from __future__ import division

import base64
import errno
import json
import os
import sys
import time
import traceback

from twisted.internet import defer, reactor
from twisted.python import log
from twisted.web import resource, static

import p2pool
from dash import data as bitcoin_data
from . import data as p2pool_data, p2p
from util import deferral, deferred_resource, graph, math, memory, pack, variable
from util import security_config


# ==============================================================================
# WEB RATE LIMITING
# ==============================================================================

class WebRateLimiter(object):
    """
    Rate limiter for web API endpoints to prevent DDoS impact on mining.
    Uses a sliding window approach per IP address.
    """
    def __init__(self, requests_per_minute=60, burst_limit=10):
        self.requests_per_minute = requests_per_minute
        self.burst_limit = burst_limit
        self.ip_requests = {}  # {ip: [(timestamp, ...), ...]}
        self.cleanup_interval = 60  # Cleanup old entries every 60 seconds
        self.last_cleanup = time.time()
    
    def check_rate_limit(self, ip):
        """
        Check if IP is within rate limits.
        Returns (allowed, retry_after) tuple.
        """
        now = time.time()
        
        # Periodic cleanup of old entries
        if now - self.last_cleanup > self.cleanup_interval:
            self._cleanup()
            self.last_cleanup = now
        
        if ip not in self.ip_requests:
            self.ip_requests[ip] = []
        
        # Remove requests older than 1 minute
        cutoff = now - 60
        self.ip_requests[ip] = [t for t in self.ip_requests[ip] if t > cutoff]
        
        # Check burst (requests in last second)
        burst_cutoff = now - 1
        recent_burst = len([t for t in self.ip_requests[ip] if t > burst_cutoff])
        if recent_burst >= self.burst_limit:
            return (False, 1)
        
        # Check minute rate
        if len(self.ip_requests[ip]) >= self.requests_per_minute:
            oldest = min(self.ip_requests[ip])
            retry_after = int(60 - (now - oldest)) + 1
            return (False, retry_after)
        
        # Allow request
        self.ip_requests[ip].append(now)
        return (True, 0)
    
    def _cleanup(self):
        """Remove stale entries"""
        now = time.time()
        cutoff = now - 120  # Keep 2 minutes of history
        for ip in list(self.ip_requests.keys()):
            self.ip_requests[ip] = [t for t in self.ip_requests[ip] if t > cutoff]
            if not self.ip_requests[ip]:
                del self.ip_requests[ip]
    
    def get_stats(self):
        """Get rate limiter statistics"""
        now = time.time()
        cutoff = now - 60
        active_ips = 0
        total_requests = 0
        for ip, times in self.ip_requests.items():
            recent = [t for t in times if t > cutoff]
            if recent:
                active_ips += 1
                total_requests += len(recent)
        return {
            'active_ips': active_ips,
            'requests_last_minute': total_requests,
            'limit_per_minute': self.requests_per_minute,
            'burst_limit': self.burst_limit,
        }

# Global rate limiter for web endpoints
web_rate_limiter = WebRateLimiter(requests_per_minute=120, burst_limit=20)


def _atomic_read(filename):
    try:
        with open(filename, 'rb') as f:
            return f.read()
    except IOError, e:
        if e.errno != errno.ENOENT:
            raise
    try:
        with open(filename + '.new', 'rb') as f:
            return f.read()
    except IOError, e:
        if e.errno != errno.ENOENT:
            raise
    return None

def _atomic_write(filename, data):
    with open(filename + '.new', 'wb') as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except:
            pass
    try:
        os.rename(filename + '.new', filename)
    except: # XXX windows can't overwrite
        os.remove(filename)
        os.rename(filename + '.new', filename)

def get_web_root(wb, datadir_path, bitcoind_getinfo_var, stop_event=variable.Event(), static_dir=None):
    node = wb.node
    start_time = time.time()
    
    web_root = resource.Resource()
    
    def get_users():
        height, last = node.tracker.get_height_and_last(node.best_share_var.value)
        weights, total_weight, donation_weight = node.tracker.get_cumulative_weights(node.best_share_var.value, min(height, 720), 65535*2**256)
        res = {}
        for script in sorted(weights, key=lambda s: weights[s]):
            address = bitcoin_data.script2_to_address(script, node.net.PARENT)
            if address is not None:
                res[address] = weights[script]/total_weight
        return res
    
    def get_current_scaled_txouts(scale, trunc=0):
        txouts = node.get_current_txouts()
        total = sum(txouts.itervalues())
        results = dict((addr, value*scale//total) for addr, value in txouts.iteritems())
        if trunc > 0:
            total_random = 0
            random_set = set()
            for s in sorted(results, key=results.__getitem__):
                if results[s] >= trunc:
                    break
                total_random += results[s]
                random_set.add(s)
            if total_random:
                winner = math.weighted_choice((addr, results[script]) for addr in random_set)
                for addr in random_set:
                    del results[addr]
                results[winner] = total_random
        if sum(results.itervalues()) < int(scale):
            results[math.weighted_choice(results.iteritems())] += int(scale) - sum(results.itervalues())
        return results
    
    def get_patron_sendmany(total=None, trunc='0.01'):
        if total is None:
            return 'need total argument. go to patron_sendmany/<TOTAL>'
        total = int(float(total)*1e8)
        trunc = int(float(trunc)*1e8)
        return json.dumps(dict(
            (bitcoin_data.script2_to_address(script, node.net.PARENT), value/1e8)
            for script, value in get_current_scaled_txouts(total, trunc).iteritems()
            if bitcoin_data.script2_to_address(script, node.net.PARENT) is not None
        ))
    
    def get_global_stats():
        # averaged over last hour
        if node.tracker.get_height(node.best_share_var.value) < 10:
            return None
        lookbehind = min(node.tracker.get_height(node.best_share_var.value), 3600//node.net.SHARE_PERIOD)
        
        nonstale_hash_rate = p2pool_data.get_pool_attempts_per_second(node.tracker, node.best_share_var.value, lookbehind)
        stale_prop = p2pool_data.get_average_stale_prop(node.tracker, node.best_share_var.value, lookbehind)
        diff = bitcoin_data.target_to_difficulty(wb.current_work.value['bits'].target)

        return dict(
            pool_nonstale_hash_rate=nonstale_hash_rate,
            pool_hash_rate=nonstale_hash_rate/(1 - stale_prop),
            pool_stale_prop=stale_prop,
            min_difficulty=bitcoin_data.target_to_difficulty(node.tracker.items[node.best_share_var.value].max_target),
            network_block_difficulty=diff,
            network_hashrate=(diff * 2**32 // node.net.PARENT.BLOCK_PERIOD),
        )
    
    def get_local_stats():
        if node.tracker.get_height(node.best_share_var.value) < 10:
            return None
        lookbehind = min(node.tracker.get_height(node.best_share_var.value), 3600//node.net.SHARE_PERIOD)
        
        global_stale_prop = p2pool_data.get_average_stale_prop(node.tracker, node.best_share_var.value, lookbehind)
        
        my_unstale_count = sum(1 for share in node.tracker.get_chain(node.best_share_var.value, lookbehind) if share.hash in wb.my_share_hashes)
        my_orphan_count = sum(1 for share in node.tracker.get_chain(node.best_share_var.value, lookbehind) if share.hash in wb.my_share_hashes and share.share_data['stale_info'] == 'orphan')
        my_doa_count = sum(1 for share in node.tracker.get_chain(node.best_share_var.value, lookbehind) if share.hash in wb.my_share_hashes and share.share_data['stale_info'] == 'doa')
        my_share_count = my_unstale_count + my_orphan_count + my_doa_count
        my_stale_count = my_orphan_count + my_doa_count
        
        my_stale_prop = my_stale_count/my_share_count if my_share_count != 0 else None
        
        my_work = sum(bitcoin_data.target_to_average_attempts(share.target)
            for share in node.tracker.get_chain(node.best_share_var.value, lookbehind - 1)
            if share.hash in wb.my_share_hashes)
        actual_time = (node.tracker.items[node.best_share_var.value].timestamp -
            node.tracker.items[node.tracker.get_nth_parent_hash(node.best_share_var.value, lookbehind - 1)].timestamp)
        share_att_s = my_work / actual_time
        
        miner_hash_rates, miner_dead_hash_rates = wb.get_local_rates()
        (stale_orphan_shares, stale_doa_shares), shares, _ = wb.get_stale_counts()

        miner_last_difficulties = {}
        for addr in wb.last_work_shares.value:
            miner_last_difficulties[addr] = bitcoin_data.target_to_difficulty(wb.last_work_shares.value[addr].target)
        
        return dict(
            my_hash_rates_in_last_hour=dict(
                note="DEPRECATED",
                nonstale=share_att_s,
                rewarded=share_att_s/(1 - global_stale_prop),
                actual=share_att_s/(1 - my_stale_prop) if my_stale_prop is not None else 0, # 0 because we don't have any shares anyway
            ),
            my_share_counts_in_last_hour=dict(
                shares=my_share_count,
                unstale_shares=my_unstale_count,
                stale_shares=my_stale_count,
                orphan_stale_shares=my_orphan_count,
                doa_stale_shares=my_doa_count,
            ),
            my_stale_proportions_in_last_hour=dict(
                stale=my_stale_prop,
                orphan_stale=my_orphan_count/my_share_count if my_share_count != 0 else None,
                dead_stale=my_doa_count/my_share_count if my_share_count != 0 else None,
            ),
            miner_hash_rates=miner_hash_rates,
            miner_dead_hash_rates=miner_dead_hash_rates,
            miner_last_difficulties=miner_last_difficulties,
            efficiency_if_miner_perfect=(1 - stale_orphan_shares/shares)/(1 - global_stale_prop) if shares else None, # ignores dead shares because those are miner's fault and indicated by pseudoshare rejection
            efficiency=(1 - (stale_orphan_shares+stale_doa_shares)/shares)/(1 - global_stale_prop) if shares else None,
            peers=dict(
                incoming=sum(1 for peer in node.p2p_node.peers.itervalues() if peer.incoming),
                outgoing=sum(1 for peer in node.p2p_node.peers.itervalues() if not peer.incoming),
            ),
            shares=dict(
                total=shares,
                orphan=stale_orphan_shares,
                dead=stale_doa_shares,
            ),
            uptime=time.time() - start_time,
            attempts_to_share=bitcoin_data.target_to_average_attempts(node.tracker.items[node.best_share_var.value].max_target),
            attempts_to_block=bitcoin_data.target_to_average_attempts(node.dashd_work.value['bits'].target),
            block_value=node.dashd_work.value['subsidy']*1e-8,
            block_value_payments=node.dashd_work.value.get('payment_amount', 0)*1e-8,
            block_value_miner=max(0, node.dashd_work.value['subsidy'] - node.dashd_work.value.get('payment_amount', 0))*1e-8,
            warnings=p2pool_data.get_warnings(node.tracker, node.best_share_var.value, node.net, bitcoind_getinfo_var.value, node.dashd_work.value),
            donation_proportion=wb.donation_percentage/100,
            version=p2pool.__version__,
            protocol_version=p2p.Protocol.VERSION,
            fee=wb.worker_fee,
        )
    
    # Initialize security config with datadir
    sec_config = security_config.security_config
    sec_config.set_datadir(datadir_path)
    
    class WebInterface(deferred_resource.DeferredResource):
        def __init__(self, func, mime_type='application/json', args=(), rate_limit=True, require_auth=False):
            deferred_resource.DeferredResource.__init__(self)
            self.func, self.mime_type, self.args = func, mime_type, args
            self.rate_limit = rate_limit
            self.require_auth = require_auth
        
        def getChild(self, child, request):
            return WebInterface(self.func, self.mime_type, self.args + (child,), self.rate_limit, self.require_auth)
        
        def _check_auth(self, request):
            """Check HTTP Basic Authentication"""
            if not sec_config.get('web_auth_enabled', False):
                return True
            
            auth_header = request.getHeader('Authorization')
            username, password = sec_config.parse_basic_auth(auth_header)
            
            if username is None:
                return False
            
            return sec_config.check_web_auth(username, password)
        
        def _send_auth_required(self, request):
            """Send 401 Unauthorized response"""
            request.setResponseCode(401)
            request.setHeader('WWW-Authenticate', 'Basic realm="P2Pool Web Interface"')
            request.setHeader('Content-Type', 'application/json')
            return json.dumps({'error': 'Authentication required'})
        
        @defer.inlineCallbacks
        def render_GET(self, request):
            # Check authentication if required or if globally enabled
            if self.require_auth or sec_config.get('web_auth_enabled', False):
                if not self._check_auth(request):
                    defer.returnValue(self._send_auth_required(request))
                    return
            
            # Apply rate limiting
            if self.rate_limit:
                client_ip = request.getClientIP()
                allowed, retry_after = web_rate_limiter.check_rate_limit(client_ip)
                if not allowed:
                    request.setResponseCode(429)
                    request.setHeader('Content-Type', 'application/json')
                    request.setHeader('Retry-After', str(retry_after))
                    defer.returnValue(json.dumps({
                        'error': 'Rate limit exceeded',
                        'retry_after': retry_after,
                    }))
                    return
            
            request.setHeader('Content-Type', self.mime_type)
            request.setHeader('Access-Control-Allow-Origin', '*')
            res = yield self.func(*self.args)
            defer.returnValue(json.dumps(res) if self.mime_type == 'application/json' else res)
    
    # Protected static files wrapper for authentication
    class ProtectedFile(resource.Resource):
        """Wrapper for static.File that adds authentication"""
        def __init__(self, path):
            resource.Resource.__init__(self)
            self.static_resource = static.File(path)
        
        def _check_auth(self, request):
            """Check HTTP Basic Authentication"""
            if not sec_config.get('web_auth_enabled', False):
                return True
            
            auth_header = request.getHeader('Authorization')
            username, password = sec_config.parse_basic_auth(auth_header)
            
            if username is None:
                return False
            
            return sec_config.check_web_auth(username, password)
        
        def getChild(self, path, request):
            # Check auth for all static file requests
            if not self._check_auth(request):
                return AuthRequiredResource()
            return self.static_resource.getChild(path, request)
        
        def render_GET(self, request):
            if not self._check_auth(request):
                request.setResponseCode(401)
                request.setHeader('WWW-Authenticate', 'Basic realm="P2Pool Web Interface"')
                request.setHeader('Content-Type', 'text/html')
                return '<html><body><h1>401 Unauthorized</h1><p>Authentication required.</p></body></html>'
            return self.static_resource.render_GET(request)
    
    class AuthRequiredResource(resource.Resource):
        """Resource that always returns 401"""
        def render_GET(self, request):
            request.setResponseCode(401)
            request.setHeader('WWW-Authenticate', 'Basic realm="P2Pool Web Interface"')
            request.setHeader('Content-Type', 'text/html')
            return '<html><body><h1>401 Unauthorized</h1><p>Authentication required.</p></body></html>'
    
    def decent_height():
        return min(node.tracker.get_height(node.best_share_var.value), 720)
    web_root.putChild('rate', WebInterface(lambda: p2pool_data.get_pool_attempts_per_second(node.tracker, node.best_share_var.value, decent_height())/(1-p2pool_data.get_average_stale_prop(node.tracker, node.best_share_var.value, decent_height()))))
    web_root.putChild('difficulty', WebInterface(lambda: bitcoin_data.target_to_difficulty(node.tracker.items[node.best_share_var.value].max_target)))
    web_root.putChild('users', WebInterface(get_users))
    web_root.putChild('user_stales', WebInterface(lambda:
        p2pool_data.get_user_stale_props(node.tracker, node.best_share_var.value,
            node.tracker.get_height(node.best_share_var.value), node.net.PARENT)))
    web_root.putChild('fee', WebInterface(lambda: wb.worker_fee))
    web_root.putChild('current_payouts', WebInterface(lambda: dict(
        (bitcoin_data.script2_to_address(script, node.net.PARENT), value/1e8)
        for script, value in node.get_current_txouts().iteritems()
        if bitcoin_data.script2_to_address(script, node.net.PARENT) is not None)))
    web_root.putChild('patron_sendmany', WebInterface(get_patron_sendmany, 'text/plain'))
    web_root.putChild('global_stats', WebInterface(get_global_stats))
    web_root.putChild('local_stats', WebInterface(get_local_stats))
    
    # ==== NEW: Stratum statistics endpoint ====
    def get_stratum_stats():
        """Get stratum pool statistics including per-worker data"""
        try:
            from p2pool.dash.stratum import pool_stats
            stats = pool_stats.get_pool_stats()
            worker_stats = pool_stats.get_worker_stats()
            
            # Format worker stats for JSON
            formatted_workers = {}
            for worker_name, wstats in worker_stats.items():
                # Get aggregate connection stats (Option A: Aggregate Worker Stats)
                conn_aggregate = pool_stats.get_worker_aggregate_stats(worker_name)
                
                formatted_workers[worker_name] = {
                    'shares': wstats.get('shares', 0),
                    'accepted': wstats.get('accepted', 0),
                    'rejected': wstats.get('rejected', 0),
                    'hash_rate': wstats.get('hash_rate', 0),
                    'last_seen': wstats.get('last_seen', 0),
                    'first_seen': wstats.get('first_seen', 0),
                    # Connection aggregate stats
                    'connections': conn_aggregate.get('connection_count', 0) if conn_aggregate else 0,
                    'active_connections': conn_aggregate.get('active_connections', 0) if conn_aggregate else 0,
                    'backup_connections': conn_aggregate.get('backup_connections', 0) if conn_aggregate else 0,
                    'connection_difficulties': conn_aggregate.get('difficulties', []) if conn_aggregate else [],
                }
            
            return {
                'pool': stats,
                'workers': formatted_workers,
            }
        except Exception as e:
            return {'error': str(e)}
    
    web_root.putChild('stratum_stats', WebInterface(get_stratum_stats))
    
    # ==== Security/DDoS monitoring endpoint ====
    def get_stratum_security():
        """Get stratum security and DDoS detection metrics"""
        try:
            from p2pool.dash.stratum import pool_stats
            return pool_stats.get_security_stats()
        except Exception as e:
            return {'error': str(e)}
    
    web_root.putChild('stratum_security', WebInterface(get_stratum_security))
    
    # ==== Ban stats endpoint ====
    def get_ban_stats():
        """Get current ban statistics"""
        try:
            from p2pool.dash.stratum import pool_stats
            return pool_stats.get_ban_stats()
        except Exception as e:
            return {'error': str(e)}
    
    web_root.putChild('ban_stats', WebInterface(get_ban_stats))
    
    # ==== Web rate limiter stats endpoint ====
    def get_web_rate_stats():
        """Get web rate limiter statistics"""
        return web_rate_limiter.get_stats()
    
    web_root.putChild('web_rate_stats', WebInterface(get_web_rate_stats, rate_limit=False))
    
    web_root.putChild('peer_addresses', WebInterface(lambda: ' '.join('%s%s' % (peer.transport.getPeer().host, ':'+str(peer.transport.getPeer().port) if peer.transport.getPeer().port != node.net.P2P_PORT else '') for peer in node.p2p_node.peers.itervalues())))
    web_root.putChild('peer_txpool_sizes', WebInterface(lambda: dict(('%s:%i' % (peer.transport.getPeer().host, peer.transport.getPeer().port), peer.remembered_txs_size) for peer in node.p2p_node.peers.itervalues())))
    web_root.putChild('pings', WebInterface(defer.inlineCallbacks(lambda: defer.returnValue(
        dict([(a, (yield b)) for a, b in
            [(
                '%s:%i' % (peer.transport.getPeer().host, peer.transport.getPeer().port),
                defer.inlineCallbacks(lambda peer=peer: defer.returnValue(
                    min([(yield peer.do_ping().addCallback(lambda x: x/0.001).addErrback(lambda fail: None)) for i in xrange(3)])
                ))()
            ) for peer in list(node.p2p_node.peers.itervalues())]
        ])
    ))))
    web_root.putChild('peer_versions', WebInterface(lambda: dict(('%s:%i' % peer.addr, peer.other_sub_version) for peer in node.p2p_node.peers.itervalues())))
    web_root.putChild('payout_addr', WebInterface(lambda: getattr(wb, 'address', None)))
    web_root.putChild('payout_addrs', WebInterface(
        lambda: [bitcoin_data.pubkey_hash_to_address(pubkey_hash, node.net.PARENT) for pubkey_hash in wb.pubkeys.keys]))
    
    # ==========================================================================
    # BLOCK HISTORY STORAGE - for accurate luck calculation
    # ==========================================================================
    # Stores pool hashrate at the time each block is found
    # Format: {'block_hash': {'ts': timestamp, 'pool_hashrate': H/s, 'network_diff': diff, ...}}
    
    block_history_file = os.path.join(datadir_path, 'block_history.json')
    block_history = {}
    
    # Load existing block history
    if os.path.exists(block_history_file):
        try:
            with open(block_history_file, 'rb') as f:
                block_history = json.loads(f.read())
            print 'Loaded %d blocks from block history' % len(block_history)
        except Exception as e:
            log.err(e, 'Error loading block history:')
            block_history = {}
    
    def save_block_history():
        """Save block history to disk."""
        try:
            with open(block_history_file + '.new', 'wb') as f:
                f.write(json.dumps(block_history, indent=2))
            try:
                os.rename(block_history_file + '.new', block_history_file)
            except:
                os.remove(block_history_file)
                os.rename(block_history_file + '.new', block_history_file)
        except Exception as e:
            log.err(e, 'Error saving block history:')
    
    def record_block_found(block_hash, block_height, share_hash, miner_address, timestamp=None):
        """Record a found block with current pool hashrate for accurate luck calculation."""
        if block_hash in block_history:
            return  # Already recorded
        
        ts = timestamp if timestamp else time.time()
        
        # Calculate current pool hashrate
        pool_hashrate = None
        stale_prop = None
        network_diff = None
        expected_time = None
        
        try:
            height = node.tracker.get_height(node.best_share_var.value)
            if height >= 2:
                lookbehind = min(height, 3600 // node.net.SHARE_PERIOD)
                if lookbehind >= 2:
                    raw_hashrate = p2pool_data.get_pool_attempts_per_second(
                        node.tracker, node.best_share_var.value, lookbehind)
                    stale_prop = p2pool_data.get_average_stale_prop(
                        node.tracker, node.best_share_var.value, lookbehind)
                    pool_hashrate = raw_hashrate / (1 - stale_prop) if stale_prop < 1 else raw_hashrate
                    
                    # Get network difficulty
                    if wb.current_work.value and 'bits' in wb.current_work.value:
                        network_diff = bitcoin_data.target_to_difficulty(
                            wb.current_work.value['bits'].target)
                        if pool_hashrate > 0:
                            expected_time = (network_diff * 2**32) / pool_hashrate
        except Exception as e:
            log.err(e, 'Error calculating hashrate for block record:')
        
        block_history[block_hash] = {
            'ts': ts,
            'block_height': block_height,
            'share_hash': share_hash,
            'miner': miner_address,
            'pool_hashrate': pool_hashrate,
            'stale_prop': stale_prop,
            'network_diff': network_diff,
            'expected_time': expected_time,
        }
        
        print 'Recorded block %s at height %d with pool hashrate %.2f TH/s' % (
            block_hash[:16], block_height, (pool_hashrate / 1e12) if pool_hashrate else 0)
        
        # Save to disk
        save_block_history()
    
    def get_block_history_data(block_hash):
        """Get historical data for a block if available."""
        return block_history.get(block_hash)
    
    # Expose block history via API
    web_root.putChild('block_history', WebInterface(lambda: block_history))
    
    # Cache for block status to avoid repeated RPC calls
    block_status_cache = {}  # {block_hash: {'status': 'confirmed'|'orphaned'|'pending', 'checked': timestamp}}
    BLOCK_CACHE_TTL = 300  # 5 minutes cache for block status
    
    @defer.inlineCallbacks
    def get_block_status(block_hash):
        """Check if a block is confirmed, orphaned, or pending."""
        now = time.time()
        
        # Check cache first
        if block_hash in block_status_cache:
            cached = block_status_cache[block_hash]
            # If confirmed/orphaned, cache forever; if pending, cache for TTL
            if cached['status'] in ('confirmed', 'orphaned'):
                defer.returnValue(cached['status'])
            elif now - cached['checked'] < BLOCK_CACHE_TTL:
                defer.returnValue(cached['status'])
        
        try:
            block_info = yield wb.dashd.rpc_getblock(block_hash)
            confirmations = block_info.get('confirmations', 0)
            chainlock = block_info.get('chainlock', False)
            
            if chainlock or confirmations >= 6:
                status = 'confirmed'
            elif confirmations >= 0:
                status = 'pending'  # Not yet confirmed, but not orphaned
            else:
                status = 'orphaned'  # Negative confirmations = orphaned
            
            block_status_cache[block_hash] = {'status': status, 'checked': now}
            defer.returnValue(status)
        except Exception:
            # Block not found in node = orphaned
            block_status_cache[block_hash] = {'status': 'orphaned', 'checked': now}
            defer.returnValue('orphaned')
    
    @defer.inlineCallbacks
    def get_recent_blocks():
        if node.best_share_var.value is None:
            defer.returnValue([])
        
        blocks = []
        try:
            height = node.tracker.get_height(node.best_share_var.value)
            if height < 1:
                defer.returnValue([])
                
            for s in node.tracker.get_chain(node.best_share_var.value, min(height, node.net.CHAIN_LENGTH)):
                if s.pow_hash <= s.header['bits'].target:
                    block_hash = '%064x' % s.header_hash
                    try:
                        block_number = p2pool_data.parse_bip0034(s.share_data['coinbase'])[0]
                    except Exception:
                        block_number = 0
                    
                    # Check block status
                    status = yield get_block_status(block_hash)
                    
                    blocks.append(dict(
                        ts=s.timestamp,
                        hash=block_hash,
                        number=block_number,
                        share='%064x' % s.hash,
                        explorer_url=node.net.PARENT.BLOCK_EXPLORER_URL_PREFIX + block_hash,
                        status=status,
                    ))
        except Exception as e:
            log.err(e, 'Error getting recent blocks:')
            defer.returnValue([])
        
        # Calculate luck for each block using AVERAGE hashrate between blocks
        # Luck = expected_time / actual_time * 100%
        # For accurate luck: use average of (prev_block_hashrate + current_block_hashrate) / 2
        if len(blocks) >= 1:
            # Get current pool hashrate as fallback for blocks without historical data
            current_pool_hashrate = None
            current_expected_time = None
            current_network_diff = None
            try:
                lookbehind = min(height, 3600 // node.net.SHARE_PERIOD)
                if lookbehind >= 2:
                    pool_hashrate = p2pool_data.get_pool_attempts_per_second(
                        node.tracker, node.best_share_var.value, lookbehind)
                    stale_prop = p2pool_data.get_average_stale_prop(
                        node.tracker, node.best_share_var.value, lookbehind)
                    current_pool_hashrate = pool_hashrate / (1 - stale_prop) if stale_prop < 1 else pool_hashrate
                    
                    # Get current network difficulty for expected time calculation
                    if wb.current_work.value and 'bits' in wb.current_work.value:
                        current_network_diff = bitcoin_data.target_to_difficulty(
                            wb.current_work.value['bits'].target)
                        if current_pool_hashrate > 0:
                            current_expected_time = (current_network_diff * 2**32) / current_pool_hashrate
            except Exception as e:
                log.err(e, 'Error calculating pool hashrate for luck:')
            
            # Sort by timestamp (oldest first for calculation)
            blocks_sorted = sorted(blocks, key=lambda x: x['ts'])
            
            for i, block in enumerate(blocks_sorted):
                # Get historical data for this block and previous block
                hist_data = get_block_history_data(block['hash'])
                prev_hist_data = get_block_history_data(blocks_sorted[i-1]['hash']) if i > 0 else None
                
                # Determine hashrate to use for this block's luck calculation
                block_hashrate = None
                block_network_diff = None
                
                if hist_data and hist_data.get('pool_hashrate'):
                    block_hashrate = hist_data['pool_hashrate']
                    block_network_diff = hist_data.get('network_diff', current_network_diff)
                    block['luck_approximate'] = False
                else:
                    block_hashrate = current_pool_hashrate
                    block_network_diff = current_network_diff
                    block['luck_approximate'] = True
                
                block['pool_hashrate_at_find'] = block_hashrate
                
                # Initialize luck fields
                block['luck'] = None
                block['time_to_find'] = None
                block['expected_time'] = None
                block['avg_hashrate_used'] = None
                
                if i == 0:
                    # First block: can't calculate time_to_find without previous reference
                    continue
                
                # Time between this block and previous block
                prev_block = blocks_sorted[i - 1]
                actual_time = block['ts'] - prev_block['ts']
                
                if actual_time <= 0:
                    continue  # Invalid time difference
                    
                block['time_to_find'] = actual_time
                
                # Calculate AVERAGE hashrate between prev block and this block
                prev_hashrate = None
                if prev_hist_data and prev_hist_data.get('pool_hashrate'):
                    prev_hashrate = prev_hist_data['pool_hashrate']
                elif block['luck_approximate']:
                    prev_hashrate = current_pool_hashrate  # Fallback
                else:
                    prev_hashrate = block_hashrate  # Use current block's hashrate if we don't have prev
                
                if prev_hashrate and block_hashrate:
                    avg_hashrate = (prev_hashrate + block_hashrate) / 2
                    block['avg_hashrate_used'] = avg_hashrate
                    
                    # Calculate expected time using average hashrate
                    if block_network_diff and avg_hashrate > 0:
                        expected_time = (block_network_diff * 2**32) / avg_hashrate
                        block['expected_time'] = expected_time
                        block['luck'] = (expected_time / actual_time) * 100
                elif block_hashrate and block_network_diff:
                    # Fallback to single hashrate
                    expected_time = (block_network_diff * 2**32) / block_hashrate
                    block['expected_time'] = expected_time
                    block['luck'] = (expected_time / actual_time) * 100
            
            # Restore original order (newest first)
            blocks = sorted(blocks_sorted, key=lambda x: x['ts'], reverse=True)
            
            # Calculate overall luck stats
            valid_luck = [b['luck'] for b in blocks if b['luck'] is not None]
            approximate_count = sum(1 for b in blocks if b.get('luck_approximate', True) and b.get('luck') is not None)
            accurate_count = len(valid_luck) - approximate_count
            
            if valid_luck:
                avg_luck = sum(valid_luck) / len(valid_luck)
                # Add summary to first item
                if blocks:
                    blocks[0]['pool_avg_luck'] = avg_luck
                    blocks[0]['blocks_for_luck'] = len(valid_luck)
                    blocks[0]['accurate_luck_count'] = accurate_count
                    blocks[0]['approximate_luck_count'] = approximate_count
                    if accurate_count == len(valid_luck):
                        blocks[0]['luck_note'] = "All luck values are accurate (using avg hashrate between blocks)"
                    elif accurate_count > 0:
                        blocks[0]['luck_note'] = "%d accurate, %d approximate (using current hashrate)" % (accurate_count, approximate_count)
                    else:
                        blocks[0]['luck_note'] = "Luck is approximate (uses current pool hashrate)"
        
        defer.returnValue(blocks)
    
    @defer.inlineCallbacks
    def get_luck_stats():
        """Get pool luck statistics based on blocks found."""
        blocks = yield get_recent_blocks()
        
        if not blocks:
            defer.returnValue(dict(
                blocks_found=0,
                luck_available=False,
                message="No blocks found yet"
            ))
        
        # Calculate luck statistics
        valid_blocks = [b for b in blocks if b.get('luck') is not None]
        
        if len(valid_blocks) < 1:
            # Only one block or no luck calculated, can't show stats
            defer.returnValue(dict(
                blocks_found=len(blocks),
                luck_available=False,
                message="Need at least 2 blocks to calculate luck"
            ))
        
        luck_values = [b['luck'] for b in valid_blocks]
        times_to_find = [b['time_to_find'] for b in valid_blocks if b.get('time_to_find') and b.get('time_to_find') > 0]
        
        # Current expected time to block
        current_expected_time = None
        try:
            height = node.tracker.get_height(node.best_share_var.value)
            if height and height >= 2:
                lookbehind = min(height, 3600 // node.net.SHARE_PERIOD)
                if lookbehind >= 2:
                    pool_hashrate = p2pool_data.get_pool_attempts_per_second(
                        node.tracker, node.best_share_var.value, lookbehind)
                    stale_prop = p2pool_data.get_average_stale_prop(
                        node.tracker, node.best_share_var.value, lookbehind)
                    effective_hashrate = pool_hashrate / (1 - stale_prop) if stale_prop < 1 else pool_hashrate
                    
                    if wb.current_work.value and 'bits' in wb.current_work.value and effective_hashrate > 0:
                        block_difficulty = bitcoin_data.target_to_difficulty(
                            wb.current_work.value['bits'].target)
                        current_expected_time = (block_difficulty * 2**32) / effective_hashrate
        except Exception as e:
            log.err(e, 'Error calculating expected time for luck_stats:')
        
        # Time since last block
        time_since_last = None
        if blocks:
            try:
                newest_block = max(blocks, key=lambda x: x['ts'])
                time_since_last = time.time() - newest_block['ts']
                if time_since_last < 0:
                    time_since_last = 0  # Clock skew protection
            except Exception:
                pass
        
        # Calculate current luck trend safely
        current_luck_trend = None
        if current_expected_time and time_since_last and time_since_last > 0:
            current_luck_trend = (current_expected_time / time_since_last) * 100
        
        defer.returnValue(dict(
            blocks_found=len(blocks),
            blocks_with_luck=len(valid_blocks),
            luck_available=True,
            luck_approximate=True,  # Note: luck uses current hashrate, not historical
            
            # Luck statistics
            average_luck=sum(luck_values) / len(luck_values) if luck_values else None,
            min_luck=min(luck_values) if luck_values else None,
            max_luck=max(luck_values) if luck_values else None,
            
            # Timing statistics  
            avg_time_between_blocks=sum(times_to_find) / len(times_to_find) if times_to_find else None,
            min_time_between_blocks=min(times_to_find) if times_to_find else None,
            max_time_between_blocks=max(times_to_find) if times_to_find else None,
            
            # Current state
            current_expected_time=current_expected_time,
            time_since_last_block=time_since_last,
            current_luck_trend=current_luck_trend,
            
            # Note about approximation
            note="Luck values are approximate (calculated using current pool hashrate)",
            
            # Individual block luck (newest first)
            blocks=[dict(
                number=b.get('number'),
                ts=b['ts'],
                luck=b.get('luck'),
                time_to_find=b.get('time_to_find'),
                status=b.get('status'),
            ) for b in sorted(blocks, key=lambda x: x['ts'], reverse=True)]
        ))
    
    web_root.putChild('recent_blocks', WebInterface(get_recent_blocks))
    web_root.putChild('luck_stats', WebInterface(get_luck_stats))
    web_root.putChild('uptime', WebInterface(lambda: time.time() - start_time))
    web_root.putChild('stale_rates', WebInterface(lambda: p2pool_data.get_stale_counts(node.tracker, node.best_share_var.value, decent_height(), rates=True)))
    
    new_root = resource.Resource()
    web_root.putChild('web', new_root)
    
    stat_log = []
    if os.path.exists(os.path.join(datadir_path, 'stats')):
        try:
            with open(os.path.join(datadir_path, 'stats'), 'rb') as f:
                stat_log = json.loads(f.read())
        except:
            log.err(None, 'Error loading stats:')
    def update_stat_log():
        while stat_log and stat_log[0]['time'] < time.time() - 24*60*60:
            stat_log.pop(0)
        
        lookbehind = 3600//node.net.SHARE_PERIOD
        if node.tracker.get_height(node.best_share_var.value) < lookbehind:
            return None
        
        global_stale_prop = p2pool_data.get_average_stale_prop(node.tracker, node.best_share_var.value, lookbehind)
        (stale_orphan_shares, stale_doa_shares), shares, _ = wb.get_stale_counts()
        miner_hash_rates, miner_dead_hash_rates = wb.get_local_rates()
        
        my_current_payout=0.0
        for pubkey_hash in wb.pubkeys.keys:
            my_current_payout += node.get_current_txouts().get(
                    pubkey_hash, 0)*1e-8
        stat_log.append(dict(
            time=time.time(),
            pool_hash_rate=p2pool_data.get_pool_attempts_per_second(node.tracker, node.best_share_var.value, lookbehind)/(1-global_stale_prop),
            pool_stale_prop=global_stale_prop,
            local_hash_rates=miner_hash_rates,
            local_dead_hash_rates=miner_dead_hash_rates,
            shares=shares,
            stale_shares=stale_orphan_shares + stale_doa_shares,
            stale_shares_breakdown=dict(orphan=stale_orphan_shares, doa=stale_doa_shares),
            current_payout=my_current_payout,
            peers=dict(
                incoming=sum(1 for peer in node.p2p_node.peers.itervalues() if peer.incoming),
                outgoing=sum(1 for peer in node.p2p_node.peers.itervalues() if not peer.incoming),
            ),
            attempts_to_share=bitcoin_data.target_to_average_attempts(node.tracker.items[node.best_share_var.value].max_target),
            attempts_to_block=bitcoin_data.target_to_average_attempts(node.dashd_work.value['bits'].target),
            block_value=node.dashd_work.value['subsidy']*1e-8,
        ))
        
        with open(os.path.join(datadir_path, 'stats'), 'wb') as f:
            f.write(json.dumps(stat_log))
    x = deferral.RobustLoopingCall(update_stat_log)
    x.start(5*60)
    stop_event.watch(x.stop)
    new_root.putChild('log', WebInterface(lambda: stat_log))
    
    def get_share(share_hash_str):
        if int(share_hash_str, 16) not in node.tracker.items:
            return None
        share = node.tracker.items[int(share_hash_str, 16)]
        
        return dict(
            parent='%064x' % share.previous_hash if share.previous_hash else "None",
            far_parent='%064x' % share.share_info['far_share_hash'] if share.share_info['far_share_hash'] else "None",
            children=['%064x' % x for x in sorted(node.tracker.reverse.get(share.hash, set()), key=lambda sh: -len(node.tracker.reverse.get(sh, set())))], # sorted from most children to least children
            type_name=type(share).__name__,
            local=dict(
                verified=share.hash in node.tracker.verified.items,
                time_first_seen=start_time if share.time_seen == 0 else share.time_seen,
                peer_first_received_from=share.peer_addr,
            ),
            share_data=dict(
                timestamp=share.timestamp,
                target=share.target,
                max_target=share.max_target,
                payout_address=bitcoin_data.script2_to_address(
                                    share.new_script,
                                    node.net.PARENT),
                donation=share.share_data['donation']/65535,
                stale_info=share.share_data['stale_info'],
                nonce=share.share_data['nonce'],
                desired_version=share.share_data['desired_version'],
                absheight=share.absheight,
                abswork=share.abswork,
            ),
            block=dict(
                hash='%064x' % share.header_hash,
                header=dict(
                    version=share.header['version'],
                    previous_block='%064x' % share.header['previous_block'],
                    merkle_root='%064x' % share.header['merkle_root'],
                    timestamp=share.header['timestamp'],
                    target=share.header['bits'].target,
                    nonce=share.header['nonce'],
                ),
                gentx=dict(
                    hash='%064x' % share.gentx_hash,
                    raw=bitcoin_data.tx_id_type.pack(share.gentx).encode('hex') if hasattr(share, 'gentx') else "unknown",
                    coinbase=share.share_data['coinbase'].ljust(2, '\x00').encode('hex'),
                    value=share.share_data['subsidy']*1e-8,
                    last_txout_nonce='%016x' % share.contents['last_txout_nonce'],
                ),
                other_transaction_hashes=['%064x' % x for x in share.get_other_tx_hashes(node.tracker)],
            ),
        )

    def get_share_address(share_hash_str):
        if int(share_hash_str, 16) not in node.tracker.items:
            return None
        share = node.tracker.items[int(share_hash_str, 16)]
        try:
            return share.address
        except AttributeError:
            return bitcoin_data.script2_to_address(share.new_script,
                                                   node.net.ADDRESS_VERSION, -1,
                                                   node.net.PARENT)

    new_root.putChild('payout_address', WebInterface(lambda share_hash_str: get_share_address(share_hash_str)))
    new_root.putChild('share', WebInterface(lambda share_hash_str: get_share(share_hash_str)))
    new_root.putChild('heads', WebInterface(lambda: ['%064x' % x for x in node.tracker.heads]))
    new_root.putChild('verified_heads', WebInterface(lambda: ['%064x' % x for x in node.tracker.verified.heads]))
    new_root.putChild('tails', WebInterface(lambda: ['%064x' % x for t in node.tracker.tails for x in node.tracker.reverse.get(t, set())]))
    new_root.putChild('verified_tails', WebInterface(lambda: ['%064x' % x for t in node.tracker.verified.tails for x in node.tracker.verified.reverse.get(t, set())]))
    new_root.putChild('best_share_hash', WebInterface(lambda: '%064x' % node.best_share_var.value if node.best_share_var.value is not None else '0'*64))
    new_root.putChild('my_share_hashes', WebInterface(lambda: ['%064x' % my_share_hash for my_share_hash in wb.my_share_hashes]))
    new_root.putChild('my_share_hashes50', WebInterface(lambda: ['%064x' % my_share_hash for my_share_hash in list(wb.my_share_hashes)[:50]]))
    def get_share_data(share_hash_str):
        if int(share_hash_str, 16) not in node.tracker.items:
            return ''
        share = node.tracker.items[int(share_hash_str, 16)]
        return p2pool_data.share_type.pack(share.as_share())
    new_root.putChild('share_data', WebInterface(lambda share_hash_str: get_share_data(share_hash_str), 'application/octet-stream'))
    new_root.putChild('currency_info', WebInterface(lambda: dict(
        symbol=node.net.PARENT.SYMBOL,
        block_explorer_url_prefix=node.net.PARENT.BLOCK_EXPLORER_URL_PREFIX,
        address_explorer_url_prefix=node.net.PARENT.ADDRESS_EXPLORER_URL_PREFIX,
        tx_explorer_url_prefix=node.net.PARENT.TX_EXPLORER_URL_PREFIX,
    )))
    new_root.putChild('version', WebInterface(lambda: p2pool.__version__))
    
    hd_path = os.path.join(datadir_path, 'graph_db')
    hd_data = _atomic_read(hd_path)
    hd_obj = {}
    if hd_data is not None:
        try:
            hd_obj = json.loads(hd_data)
        except Exception:
            log.err(None, 'Error reading graph database:')
    dataview_descriptions = {
        'last_hour': graph.DataViewDescription(150, 60*60),
        'last_day': graph.DataViewDescription(300, 60*60*24),
        'last_week': graph.DataViewDescription(300, 60*60*24*7),
        'last_month': graph.DataViewDescription(300, 60*60*24*30),
        'last_year': graph.DataViewDescription(300, 60*60*24*365.25),
    }
    hd = graph.HistoryDatabase.from_obj({
        'local_hash_rate': graph.DataStreamDescription(dataview_descriptions, is_gauge=False),
        'local_dead_hash_rate': graph.DataStreamDescription(dataview_descriptions, is_gauge=False),
        'local_share_hash_rates': graph.DataStreamDescription(dataview_descriptions, is_gauge=False,
            multivalues=True, multivalue_undefined_means_0=True,
            default_func=graph.make_multivalue_migrator(dict(good='local_share_hash_rate', dead='local_dead_share_hash_rate', orphan='local_orphan_share_hash_rate'),
                post_func=lambda bins: [dict((k, (v[0] - (sum(bin.get(rem_k, (0, 0))[0] for rem_k in ['dead', 'orphan']) if k == 'good' else 0), v[1])) for k, v in bin.iteritems()) for bin in bins])),
        'pool_rates': graph.DataStreamDescription(dataview_descriptions, multivalues=True,
            multivalue_undefined_means_0=True),
        'current_payout': graph.DataStreamDescription(dataview_descriptions),
        'current_payouts': graph.DataStreamDescription(dataview_descriptions, multivalues=True),
        'peers': graph.DataStreamDescription(dataview_descriptions, multivalues=True, default_func=graph.make_multivalue_migrator(dict(incoming='incoming_peers', outgoing='outgoing_peers'))),
        'miner_hash_rates': graph.DataStreamDescription(dataview_descriptions, is_gauge=False, multivalues=True, multivalues_keep=10000),
        'miner_dead_hash_rates': graph.DataStreamDescription(dataview_descriptions, is_gauge=False, multivalues=True, multivalues_keep=10000),
        'desired_version_rates': graph.DataStreamDescription(dataview_descriptions, multivalues=True,
            multivalue_undefined_means_0=True),
        'traffic_rate': graph.DataStreamDescription(dataview_descriptions, is_gauge=False, multivalues=True),
        'getwork_latency': graph.DataStreamDescription(dataview_descriptions),
        'memory_usage': graph.DataStreamDescription(dataview_descriptions),
    }, hd_obj)
    x = deferral.RobustLoopingCall(lambda: _atomic_write(hd_path, json.dumps(hd.to_obj())))
    x.start(100)
    stop_event.watch(x.stop)
    @wb.pseudoshare_received.watch
    def _(work, dead, user):
        t = time.time()
        hd.datastreams['local_hash_rate'].add_datum(t, work)
        if dead:
            hd.datastreams['local_dead_hash_rate'].add_datum(t, work)
        if user is not None:
            hd.datastreams['miner_hash_rates'].add_datum(t, {user: work})
            if dead:
                hd.datastreams['miner_dead_hash_rates'].add_datum(t, {user: work})
    @wb.share_received.watch
    def _(work, dead, share_hash):
        t = time.time()
        if not dead:
            hd.datastreams['local_share_hash_rates'].add_datum(t, dict(good=work))
        else:
            hd.datastreams['local_share_hash_rates'].add_datum(t, dict(dead=work))
        def later():
            res = node.tracker.is_child_of(share_hash, node.best_share_var.value)
            if res is None: res = False # share isn't connected to sharechain? assume orphaned
            if res and dead: # share was DOA, but is now in sharechain
                # move from dead to good
                hd.datastreams['local_share_hash_rates'].add_datum(t, dict(dead=-work, good=work))
            elif not res and not dead: # share wasn't DOA, and isn't in sharechain
                # move from good to orphan
                hd.datastreams['local_share_hash_rates'].add_datum(t, dict(good=-work, orphan=work))
        reactor.callLater(200, later)
    @node.p2p_node.traffic_happened.watch
    def _(name, bytes):
        hd.datastreams['traffic_rate'].add_datum(time.time(), {name: bytes})
    def add_point():
        if node.tracker.get_height(node.best_share_var.value) < 10:
            return None
        lookbehind = min(node.net.CHAIN_LENGTH, 60*60//node.net.SHARE_PERIOD, node.tracker.get_height(node.best_share_var.value))
        t = time.time()
        
        pool_rates = p2pool_data.get_stale_counts(node.tracker, node.best_share_var.value, lookbehind, rates=True)
        pool_total = sum(pool_rates.itervalues())
        hd.datastreams['pool_rates'].add_datum(t, pool_rates)
        
        current_txouts = node.get_current_txouts()
        my_current_payouts = 0.0
        for pubkey_hash in wb.pubkeys.keys:
            my_current_payouts += current_txouts.get(
                    pubkey_hash, 0) * 1e-8
        hd.datastreams['current_payout'].add_datum(t, my_current_payouts)
        miner_hash_rates, miner_dead_hash_rates = wb.get_local_rates()
        # Convert script bytes to address strings for matching with miner_hash_rates
        current_txouts_by_address = dict(
            (bitcoin_data.script2_to_address(script, node.net.PARENT), value)
            for script, value in current_txouts.iteritems()
            if bitcoin_data.script2_to_address(script, node.net.PARENT) is not None
        )
        hd.datastreams['current_payouts'].add_datum(t, dict((user, current_txouts_by_address[user]*1e-8) for user in miner_hash_rates if user in current_txouts_by_address))
        
        hd.datastreams['peers'].add_datum(t, dict(
            incoming=sum(1 for peer in node.p2p_node.peers.itervalues() if peer.incoming),
            outgoing=sum(1 for peer in node.p2p_node.peers.itervalues() if not peer.incoming),
        ))
        
        vs = p2pool_data.get_desired_version_counts(node.tracker, node.best_share_var.value, lookbehind)
        vs_total = sum(vs.itervalues())
        hd.datastreams['desired_version_rates'].add_datum(t, dict((str(k), v/vs_total*pool_total) for k, v in vs.iteritems()))
        try:
            hd.datastreams['memory_usage'].add_datum(t, memory.resident())
        except:
            if p2pool.DEBUG:
                traceback.print_exc()
    x = deferral.RobustLoopingCall(add_point)
    x.start(5)
    stop_event.watch(x.stop)
    @node.dashd_work.changed.watch
    def _(new_work):
        hd.datastreams['getwork_latency'].add_datum(time.time(), new_work['latency'])
    new_root.putChild('graph_data', WebInterface(lambda source, view: hd.datastreams[source].dataviews[view].get_data(time.time())))
    
    if static_dir is None:
        static_dir = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), 'web-static')
    
    # Use ProtectedFile wrapper if authentication is enabled, otherwise use regular static.File
    web_root.putChild('static', ProtectedFile(static_dir))
    
    # Add security config endpoint
    web_root.putChild('security_config', WebInterface(lambda: sec_config.get_config_summary(), require_auth=True))
    
    # Return web_root and record_block_found function for block tracking
    return web_root, record_block_found
