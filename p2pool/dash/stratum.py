import random
import sys
import time

from twisted.internet import protocol, reactor
from twisted.python import log

from p2pool.dash import data as dash_data, getwork
from p2pool.util import expiring_dict, jsonrpc, pack


def clip(num, bot, top):
    return min(top, max(bot, num))


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
    
    def rpc_subscribe(self, miner_version=None, session_id=None, *args):
        reactor.callLater(0, self._send_work)
        
        return [
            ["mining.notify", "ae6812eb4cd7735a302a8a9dd95cf71f"], # subscription details
            "", # extranonce1
            self.wb.COINBASE_NONCE_LENGTH, # extranonce2_size
        ]
    
    def rpc_authorize(self, username, password):
        if not hasattr(self, 'authorized'):  # authorize can be called many times in one connection
            print '>>>Authorize: %s from %s' % (username, self.worker_ip)
            self.authorized = username
        self.username = username.strip()
        
        # Dash doesn't have get_user_details, so we just parse the username directly
        # Username can be: address, address+difficulty, address/difficulty
        self.user = self.username
        self.address = self.username.split('+')[0].split('/')[0]
        self.desired_share_target = None
        self.desired_pseudoshare_target = None
        
        reactor.callLater(0, self._send_work)
        return True
    
    def rpc_configure(self, extensions, extensionParameters):
        # extensions is a list of extension codes defined in BIP310
        # extensionParameters is a dict of parameters for each extension code
        if 'version-rolling' in extensions:
            # mask from miner is mandatory but we dont use it
            miner_mask = extensionParameters['version-rolling.mask']
            # min-bit-count from miner is mandatory but we dont use it
            try:
                minbitcount = extensionParameters['version-rolling.min-bit-count']
            except:
                log.err("A miner tried to connect with a malformed version-rolling.min-bit-count parameter. This is probably a bug in your mining software. Braiins OS is known to have this bug. You should complain to them.")
                minbitcount = 2  # probably not needed
            # according to the spec, pool should return largest mask possible (to support mining proxies)
            return {"version-rolling": True, "version-rolling.mask": '{:08x}'.format(self.pool_version_mask & (int(miner_mask, 16)))}
            # pool can send mining.set_version_mask at any time if the pool mask changes
        
        if 'minimum-difficulty' in extensions:
            print 'Extension method minimum-difficulty not implemented'
        if 'subscribe-extranonce' in extensions:
            # Enable extranonce subscription for this connection (required for ASICs)
            self.extranonce_subscribe = True
            print '>>>ExtranOnce subscribed from %s' % (self.worker_ip)
            # Return value indicates support
            return {"subscribe-extranonce": True}
    
    def _send_work(self):
        try:
            x, got_response = self.wb.get_work(*self.wb.preprocess_request('' if self.username is None else self.username))
        except:
            log.err()
            self.transport.loseConnection()
            return
        
        if self.desired_pseudoshare_target:
            self.fixed_target = True
            self.target = self.desired_pseudoshare_target
            self.target = max(self.target, int(x['bits'].target))
        else:
            self.fixed_target = False
            # Use min_share_target as lower bound for difficulty adjustment
            self.target = x['share_target'] if self.target == None else max(x['min_share_target'], self.target)
        
        # For ASIC compatibility: periodically send extranonce updates
        # Even with empty extranonce, this helps ASICs reset their state
        if self.extranonce_subscribe:
            current_time = time.time()
            # Send extranonce update every 30 seconds or on first work
            if current_time - self.last_extranonce_update > 30:
                self._notify_extranonce_change()
                self.last_extranonce_update = current_time
        
        jobid = str(random.randrange(2**128))
        self.other.svc_mining.rpc_set_difficulty(dash_data.target_to_difficulty(self.target)).addErrback(lambda err: None)
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
        self.handler_map[jobid] = x, got_response
    
    def rpc_submit(self, worker_name, job_id, extranonce2, ntime, nonce, version_bits=None, *args):
        # ASICBOOST: version_bits is the version mask that the miner used
        worker_name = worker_name.strip()
        if job_id not in self.handler_map:
            print >>sys.stderr, '''Couldn't link returned work's job id with its handler. This should only happen if this process was recently restarted!'''
            return False
        
        x, got_response = self.handler_map[job_id]
        coinb_nonce = extranonce2.decode('hex')
        assert len(coinb_nonce) == self.wb.COINBASE_NONCE_LENGTH
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
        # Dash's got_response takes 3 args: (header, user, coinbase_nonce)
        # Bitcoin's takes 4: (header, username, coinbase_nonce, pseudoshare_target)
        result = got_response(header, worker_name, coinb_nonce)
        
        # Adjust difficulty on this stratum to target ~10sec/pseudoshare
        if not self.fixed_target:
            self.recent_shares.append(time.time())
            if len(self.recent_shares) > 12 or (time.time() - self.recent_shares[0]) > 10 * len(self.recent_shares) * self.share_rate:
                old_time = self.recent_shares[0]
                del self.recent_shares[0]
                olddiff = dash_data.target_to_difficulty(self.target)
                self.target = int(self.target * clip((time.time() - old_time) / (len(self.recent_shares) * self.share_rate), 0.5, 2.) + 0.5)
                newtarget = clip(self.target, self.wb.net.SANE_TARGET_RANGE[0], self.wb.net.SANE_TARGET_RANGE[1])
                if newtarget != self.target:
                    print "Clipping target from %064x to %064x" % (self.target, newtarget)
                self.target = newtarget
                # Ensure target doesn't go below minimum share target
                self.target = max(x['min_share_target'], self.target)
                self.recent_shares = [time.time()]
                self._send_work()
        
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
        self.wb.new_work_event.unwatch(self.watch_id)

class StratumProtocol(jsonrpc.LineBasedPeer):
    def connectionMade(self):
        self.svc_mining = StratumRPCMiningProvider(self.factory.wb, self.other, self.transport)
    
    def connectionLost(self, reason):
        if hasattr(self, 'svc_mining'):
            self.svc_mining.close()

class StratumServerFactory(protocol.ServerFactory):
    protocol = StratumProtocol
    
    def __init__(self, wb):
        self.wb = wb
