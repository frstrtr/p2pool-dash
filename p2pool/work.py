from __future__ import division
from collections import deque

import base64
import random
import re
import sys
import time

from twisted.internet import defer
from twisted.python import log

from bitcoin import getwork, data as bitcoin_data, helper, script, worker_interface
from util import forest, jsonrpc, variable, deferral, math, pack
import p2pool, p2pool.data as p2pool_data
from p2pool import merged_mining

print_throttle = 0.0

class WorkerBridge(worker_interface.WorkerBridge):
    COINBASE_NONCE_LENGTH = 8

    def __init__(self, node, my_pubkey_hash, donation_percentage, merged_urls, worker_fee, args, pubkeys, bitcoind, share_rate):
        worker_interface.WorkerBridge.__init__(self)
        self.recent_shares_ts_work = []

        self.node = node

        self.bitcoind = bitcoind
        self.pubkeys = pubkeys
        self.args = args
        self.my_pubkey_hash = my_pubkey_hash
		
        self.donation_percentage = args.donation_percentage
        self.worker_fee = args.worker_fee


        self.net = self.node.net.PARENT
        self.running = True
        self.pseudoshare_received = variable.Event()
        self.share_received = variable.Event()
        # Activity window must account for extreme variance in low-difficulty mining
        # At minimum difficulty (0.001), miners can have very long gaps between shares
        # due to Poisson distribution variance (95% CI = ~30x expected time)
        # Formula: 100 * STRATUM_SHARE_RATE gives safe margin for variance
        # For mainnet: 100 * 10 sec = 1000 seconds (~16.7 minutes)
        # This keeps count stable while still being responsive to real disconnects
        stratum_share_rate = getattr(self.node.net, 'STRATUM_SHARE_RATE', 10)  # Default 10 seconds if not defined
        activity_window = 100 * stratum_share_rate
        self.local_rate_monitor = math.RateMonitor(activity_window)
        self.local_addr_rate_monitor = math.RateMonitor(activity_window)

        self.removed_unstales_var = variable.Variable((0, 0, 0))
        self.removed_doa_unstales_var = variable.Variable(0)

        self.last_work_shares = variable.Variable( {} )

        self.my_share_hashes = set()
        self.my_doa_share_hashes = set()

        self.address_throttle = 0
        self.share_rate = args.share_rate  # Stratum vardiff target (seconds per pseudoshare)

        self.tracker_view = forest.TrackerView(self.node.tracker, forest.get_attributedelta_type(dict(forest.AttributeDelta.attrs,
            my_count=lambda share: 1 if share.hash in self.my_share_hashes else 0,
            my_doa_count=lambda share: 1 if share.hash in self.my_doa_share_hashes else 0,
            my_orphan_announce_count=lambda share: 1 if share.hash in self.my_share_hashes and share.share_data['stale_info'] == 'orphan' else 0,
            my_dead_announce_count=lambda share: 1 if share.hash in self.my_share_hashes and share.share_data['stale_info'] == 'doa' else 0,
        )))

        @self.node.tracker.verified.removed.watch
        def _(share):
            if share.hash in self.my_share_hashes and self.node.tracker.is_child_of(share.hash, self.node.best_share_var.value):
                assert share.share_data['stale_info'] in [None, 'orphan', 'doa'] # we made these shares in this instance
                self.removed_unstales_var.set((
                    self.removed_unstales_var.value[0] + 1,
                    self.removed_unstales_var.value[1] + (1 if share.share_data['stale_info'] == 'orphan' else 0),
                    self.removed_unstales_var.value[2] + (1 if share.share_data['stale_info'] == 'doa' else 0),
                ))
            if share.hash in self.my_doa_share_hashes and self.node.tracker.is_child_of(share.hash, self.node.best_share_var.value):
                self.removed_doa_unstales_var.set(self.removed_doa_unstales_var.value + 1)

        # MERGED WORK

        self.merged_work = variable.Variable({})

        @defer.inlineCallbacks
        def set_merged_work(merged_url, merged_userpass):
            merged_proxy = jsonrpc.HTTPProxy(merged_url, dict(Authorization='Basic ' + base64.b64encode(merged_userpass)))
            
            # Try to detect auxpow capability on first call
            auxpow_capable = None
            
            while self.running:
                try:
                    # First, try getblocktemplate with auxpow capability (multiaddress support)
                    if auxpow_capable is None or auxpow_capable:
                        template = yield deferral.retry('Error while calling merged getblocktemplate on %s:' % (merged_url,), 30)(
                            merged_proxy.rpc_getblocktemplate
                        )({"capabilities": ["auxpow"]})
                        
                        # Check if auxpow is supported (modified Dogecoin with multiaddress)
                        if 'auxpow' in template:
                            if auxpow_capable is None:
                                print 'Detected auxpow-capable merged mining daemon at %s (multiaddress support enabled)' % (merged_url,)
                            auxpow_capable = True
                            
                            chainid = template['auxpow']['chainid']
                            target_hex = template['auxpow']['target']
                            
                            # PHASE A: Build complete Dogecoin block to get its hash for merged mining commitment
                            # This is the key to resolving the "chicken-and-egg" problem:
                            # We build the Dogecoin block FIRST, before the Litecoin block
                            try:
                                from p2pool import merged_mining
                                
                                # Step 1-2: Build Dogecoin coinbase and transactions, calculate merkle root
                                # For now, use template transactions to calculate merkle root
                                # TODO: Build custom coinbase with PPLNS payouts
                                doge_tx_hashes = [int(tx['hash'], 16) for tx in template.get('transactions', [])]
                                
                                # Build a simple coinbase for Dogecoin (will be improved later with PPLNS)
                                # For now, use a dummy output - we just need a valid transaction structure
                                # The actual payout logic will be implemented in build_merged_coinbase later
                                doge_coinbase_tx = dict(
                                    version=1,
                                    tx_ins=[dict(
                                        previous_output=None,  # Coinbase input - None gets serialized as sentinel value
                                        script=script.create_push_script([template['height'], 'MERGED MINING']),
                                        sequence=None,  # Use None, not 0xffffffff which is the sentinel value
                                    )],
                                    tx_outs=[dict(
                                        value=template['coinbasevalue'],
                                        script='\x76\xa9\x14' + ('\x00' * 20) + '\x88\xac',  # OP_DUP OP_HASH160 <20 bytes> OP_EQUALVERIFY OP_CHECKSIG
                                    )],
                                    lock_time=0,
                                )
                                doge_coinbase_hash = bitcoin_data.hash256(bitcoin_data.tx_type.pack(doge_coinbase_tx))
                                all_doge_tx_hashes = [doge_coinbase_hash] + doge_tx_hashes
                                
                                # Step 2: Calculate Dogecoin merkle root
                                doge_merkle_root = bitcoin_data.merkle_hash(all_doge_tx_hashes)
                                print '[DEBUG] Calculated Dogecoin merkle root: %064x' % doge_merkle_root
                                
                                # Step 3-4: Build Dogecoin header with real merkle root and hash it
                                doge_header = dict(
                                    version=template['version'] | (1 << 8),  # Set auxpow bit
                                    previous_block=int(template['previousblockhash'], 16) if template.get('previousblockhash') else 0,
                                    merkle_root=doge_merkle_root,  # REAL merkle root from actual transactions
                                    timestamp=template['curtime'],
                                    bits=bitcoin_data.FloatingIntegerType().unpack(template['bits'].decode('hex')[::-1]),
                                    nonce=0,  # Will be set to parent nonce later
                                )
                                doge_header_packed = bitcoin_data.block_header_type.pack(doge_header)
                                doge_block_hash = bitcoin_data.hash256(doge_header_packed)
                                print '[DEBUG] Calculated Dogecoin block hash for LTC coinbase commitment: %064x' % doge_block_hash
                            except Exception as e:
                                print >>sys.stderr, '[ERROR] Failed to build Dogecoin block (v2-FIXED): %s' % e
                                import traceback
                                traceback.print_exc()
                                doge_block_hash = 0
                            
                            # PHASE B: Store for embedding in Litecoin coinbase
                            # This hash will be embedded in the Litecoin coinbase via mm_data
                            # NOW with actual block hash for merged mining commitment
                            parsed_target = pack.IntType(256).unpack(target_hex.decode('hex'))
                            print '[DEBUG] Dogecoin target from template: %064x' % parsed_target
                            self.merged_work.set(math.merge_dicts(self.merged_work.value, {chainid: dict(
                                template=template,
                                hash=doge_block_hash,  # CRITICAL: This hash gets embedded in Litecoin coinbase
                                target=parsed_target,
                                merged_proxy=merged_proxy,
                                multiaddress=True,
                                doge_header=doge_header,  # Save for later when building final block
                                doge_coinbase=doge_coinbase_tx,
                                doge_tx_hashes=all_doge_tx_hashes,
                            )}))
                        else:
                            # getblocktemplate succeeded but no auxpow - shouldn't happen
                            if auxpow_capable is None:
                                print 'Warning: getblocktemplate succeeded but no auxpow object at %s, falling back to getauxblock' % (merged_url,)
                            auxpow_capable = False
                            raise ValueError('No auxpow in template')
                            
                except Exception as e:
                    # Fall back to standard getauxblock (single address)
                    if auxpow_capable is None:
                        print 'Auxpow not supported at %s, using standard getauxblock (single address mode)' % (merged_url,)
                    auxpow_capable = False
                    
                    auxblock = yield deferral.retry('Error while calling merged getauxblock on %s:' % (merged_url,), 30)(
                        merged_proxy.rpc_getauxblock
                    )()
                    self.merged_work.set(math.merge_dicts(self.merged_work.value, {auxblock['chainid']: dict(
                        hash=int(auxblock['hash'], 16),
                        target='p2pool' if auxblock['target'] == 'p2pool' else pack.IntType(256).unpack(auxblock['target'].decode('hex')),
                        merged_proxy=merged_proxy,
                        multiaddress=False,
                    )}))
                
                yield deferral.sleep(1)
        
        for merged_url_tuple in merged_urls:
            # Handle both 2-tuple and 3-tuple formats
            if len(merged_url_tuple) == 3:
                merged_url, merged_userpass, merged_payout = merged_url_tuple
            else:
                merged_url, merged_userpass = merged_url_tuple
                merged_payout = None
            set_merged_work(merged_url, merged_userpass)

        @self.merged_work.changed.watch
        def _(new_merged_work):
            print 'Got new merged mining work!'

        # COMBINE WORK

        self.current_work = variable.Variable(None)
        def compute_work():
            t = self.node.bitcoind_work.value
            bb = self.node.best_block_header.value
            if bb is not None and bb['previous_block'] == t['previous_block'] and self.node.net.PARENT.POW_FUNC(bitcoin_data.block_header_type.pack(bb)) <= t['bits'].target:
                print 'Skipping from block %x to block %x! NewHeight=%s' % (bb['previous_block'],
                    self.node.net.PARENT.BLOCKHASH_FUNC(bitcoin_data.block_header_type.pack(bb)),t['height']+1,)
                '''
                # New block template from Dash daemon only
                t = dict(
                    version=bb['version'],
                    previous_block=self.node.net.PARENT.BLOCKHASH_FUNC(bitcoin_data.block_header_type.pack(bb)),
                    bits=bb['bits'], # not always true
                    coinbaseflags='',
                    height=t['height'] + 1,
                    time=bb['timestamp'] + 600, # better way?
                    transactions=[],
                    transaction_fees=[],
                    merkle_link=bitcoin_data.calculate_merkle_link([None], 0),
                    subsidy=self.node.bitcoind_work.value['subsidy'],
                    last_update=self.node.bitcoind_work.value['last_update'],
                    payment_amount=self.node.bitcoind_work.value['payment_amount'],
                    packed_payments=self.node.bitcoind_work.value['packed_payments'],
                )
                '''

            self.current_work.set(t)
        self.node.bitcoind_work.changed.watch(lambda _: compute_work())
        self.node.best_block_header.changed.watch(lambda _: compute_work())
        compute_work()

        self.new_work_event = variable.Event()
        @self.current_work.transitioned.watch
        def _(before, after):
            # trigger LP if version/previous_block/bits changed or transactions changed from nothing
            if any(before[x] != after[x] for x in ['version', 'previous_block', 'bits']) or (not before['transactions'] and after['transactions']):
                self.new_work_event.happened()
        self.merged_work.changed.watch(lambda _: self.new_work_event.happened())
        self.node.best_share_var.changed.watch(lambda _: self.new_work_event.happened())

    def stop(self):
        self.running = False

    def get_stale_counts(self):
        '''Returns (orphans, doas), total, (orphans_recorded_in_chain, doas_recorded_in_chain)'''
        my_shares = len(self.my_share_hashes)
        my_doa_shares = len(self.my_doa_share_hashes)
        delta = self.tracker_view.get_delta_to_last(self.node.best_share_var.value)
        my_shares_in_chain = delta.my_count + self.removed_unstales_var.value[0]
        my_doa_shares_in_chain = delta.my_doa_count + self.removed_doa_unstales_var.value
        orphans_recorded_in_chain = delta.my_orphan_announce_count + self.removed_unstales_var.value[1]
        doas_recorded_in_chain = delta.my_dead_announce_count + self.removed_unstales_var.value[2]

        my_shares_not_in_chain = my_shares - my_shares_in_chain
        my_doa_shares_not_in_chain = my_doa_shares - my_doa_shares_in_chain

        return (my_shares_not_in_chain - my_doa_shares_not_in_chain, my_doa_shares_not_in_chain), my_shares, (orphans_recorded_in_chain, doas_recorded_in_chain)

    @defer.inlineCallbacks
    def freshen_addresses(self, c):
        self.cur_address_throttle = time.time()
        if self.cur_address_throttle - self.address_throttle < 30:
            return
        self.address_throttle=time.time()
        print "ATTEMPTING TO FRESHEN ADDRESS."
        self.address = yield deferral.retry('Error getting a dynamic address from coind:', 5)(lambda: self.bitcoind.rpc_getnewaddress('p2pool'))()
        new_pubkey, _, _ = bitcoin_data.address_to_pubkey_hash(self.address, self.net)
        self.pubkeys.popleft()
        self.pubkeys.addkey(new_pubkey)
        print " Updated payout pool:"
        for i in xrange(len(self.pubkeys.keys)):
            print '    ...payout %d: %s(%f)' % (i, bitcoin_data.pubkey_hash_to_address(self.pubkeys.keys[i], self.net.ADDRESS_VERSION, -1, self.net),self.pubkeys.keyweights[i],)
        self.pubkeys.updatestamp(c)
        print " Next address rotation in : %fs" % (time.time()-c+self.args.timeaddresses)

    def get_user_details(self, username):
        print '[DEBUG] get_user_details called with username:', repr(username)
        contents = re.split('([+/])', username)
        assert len(contents) % 2 == 1

        user, contents2 = contents[0], contents[1:]
        
        # Parse merged mining addresses (format: ltc_addr,doge_addr or ltc_addr,doge_addr.worker)
        # Using , (comma) separator - URL-safe and not used in difficulty parsing
        merged_addresses = {}
        worker = ''
        
        if ',' in user:
            # Split merged addresses
            parts = user.split(',', 1)  # Only split on first comma
            user = parts[0]  # Primary address (Litecoin)
            if len(parts) > 1:
                merged_addr = parts[1]
                # Check if worker name is attached to merged address
                if '.' in merged_addr:
                    merged_addr, worker = merged_addr.split('.', 1)
                elif '_' in merged_addr:
                    merged_addr, worker = merged_addr.split('_', 1)
                merged_addresses['dogecoin'] = merged_addr
                print '[DEBUG] Using miner dogecoin address:', merged_addr
        
        # Parse worker name from primary address if not already set
        if not worker:
            if '_' in user:
                worker = user.split('_')[1]
                user = user.split('_')[0]
            elif '.' in user:
                worker = user.split('.')[1]
                user = user.split('.')[0]

        desired_pseudoshare_target = None
        desired_share_target = None
        for symbol, parameter in zip(contents2[::2], contents2[1::2]):
            if symbol == '+':
                try:
                    desired_pseudoshare_target = bitcoin_data.difficulty_to_target(float(parameter))
                except:
                    if p2pool.DEBUG:
                        log.err()
            elif symbol == '/':
                try:
                    desired_share_target = bitcoin_data.difficulty_to_target(float(parameter))
                except:
                    if p2pool.DEBUG:
                        log.err()        

        # Initialize pubkey_hash with default
        pubkey_hash = self.my_pubkey_hash
        
        if self.args.address == 'dynamic':
            i = self.pubkeys.weighted()
            pubkey_hash = self.pubkeys.keys[i]

            c = time.time()
            if (c - self.pubkeys.stamp) > self.args.timeaddresses:
                self.freshen_addresses(c)

        if random.uniform(0, 100) < self.worker_fee:
            pubkey_hash = self.my_pubkey_hash
        else:
            try:
                pubkey_hash, _, _ = bitcoin_data.address_to_pubkey_hash(user, self.node.net.PARENT)
            except: # XXX blah
                pubkey_hash = self.my_pubkey_hash
        
        # Append worker name to user for identification
        if worker:
            user = user + '.' + worker

        print '[DEBUG] get_user_details returning: user=%r, merged_addresses=%r' % (user, merged_addresses)
        return user, pubkey_hash, desired_share_target, desired_pseudoshare_target, merged_addresses

    def preprocess_request(self, user):
        print '[DEBUG] preprocess_request called with user:', repr(user)
        # Removed peer connection check - allow solo mining
        if time.time() > self.current_work.value['last_update'] + 60:
            raise jsonrpc.Error_for_code(-12345)(u'lost contact with coind')
        username, pubkey_hash, desired_share_target, desired_pseudoshare_target, merged_addresses = self.get_user_details(user)
        print '[DEBUG] preprocess_request returning 5 values: username=%r' % (username,)
        return username, pubkey_hash, desired_share_target, desired_pseudoshare_target, merged_addresses

    def _estimate_local_hash_rate(self):
        if len(self.recent_shares_ts_work) == 50:
            hash_rate = sum(work for ts, work in self.recent_shares_ts_work[1:])//(self.recent_shares_ts_work[-1][0] - self.recent_shares_ts_work[0][0])
            if hash_rate > 0:
                return hash_rate
        return None

    def get_local_rates(self):
        miner_hash_rates = {}
        miner_dead_hash_rates = {}
        datums, dt = self.local_rate_monitor.get_datums_in_last()
        for datum in datums:
            miner_hash_rates[datum['user']] = miner_hash_rates.get(datum['user'], 0) + datum['work']/dt
            if datum['dead']:
                miner_dead_hash_rates[datum['user']] = miner_dead_hash_rates.get(datum['user'], 0) + datum['work']/dt
        return miner_hash_rates, miner_dead_hash_rates

    def get_local_addr_rates(self):
        addr_hash_rates = {}
        datums, dt = self.local_addr_rate_monitor.get_datums_in_last()
        for datum in datums:
            addr_hash_rates[datum['pubkey_hash']] = addr_hash_rates.get(datum['pubkey_hash'], 0) + datum['work']/dt
        return addr_hash_rates

    def get_work(self, user, pubkey_hash, desired_share_target, desired_pseudoshare_target, merged_addresses=None):
        print '[DEBUG] get_work called with user=%r, merged_addresses=%r' % (user, merged_addresses)
        global print_throttle
        t0 = time.time()  # Benchmarking start
        
        # Store merged addresses for later use in block submission
        if merged_addresses is None:
            merged_addresses = {}
        self._current_merged_addresses = merged_addresses
        
        # Removed peer connection check - allow solo mining
        # P2Pool can work standalone even with PERSIST=True

        if self.merged_work.value:
            tree, size = bitcoin_data.make_auxpow_tree(self.merged_work.value)
            mm_hashes = [self.merged_work.value.get(tree.get(i), dict(hash=0))['hash'] for i in xrange(size)]
            mm_data = '\xfa\xbemm' + bitcoin_data.aux_pow_coinbase_type.pack(dict(
                merkle_root=bitcoin_data.merkle_hash(mm_hashes),
                size=size,
                nonce=0,
            ))
            mm_later = [(aux_work, mm_hashes.index(aux_work['hash']), mm_hashes) for chain_id, aux_work in self.merged_work.value.iteritems()]
            
            # Debug: Show which chains and hashes are being embedded
            print >>sys.stderr, '[DEBUG] Merged mining data being embedded in Litecoin coinbase:'
            for chain_id, aux_work in self.merged_work.value.iteritems():
                print >>sys.stderr, '[DEBUG]   Chain ID 0x%08x: hash=%064x' % (chain_id, aux_work['hash'])
            print >>sys.stderr, '[DEBUG]   Merkle root of mm_hashes: %064x' % bitcoin_data.merkle_hash(mm_hashes)
        else:
            mm_data = ''
            mm_later = []

        tx_hashes = [bitcoin_data.hash256(bitcoin_data.tx_type.pack(tx)) for tx in self.current_work.value['transactions']]
        tx_map = dict(zip(tx_hashes, self.current_work.value['transactions']))

        previous_share = self.node.tracker.items[self.node.best_share_var.value] if self.node.best_share_var.value is not None else None
        if previous_share is None:
            share_type = p2pool_data.Share
        else:
            previous_share_type = type(previous_share)

            if previous_share_type.SUCCESSOR is None or self.node.tracker.get_height(previous_share.hash) < self.node.net.CHAIN_LENGTH:
                share_type = previous_share_type
            else:
                successor_type = previous_share_type.SUCCESSOR

                counts = p2pool_data.get_desired_version_counts(self.node.tracker,
                    self.node.tracker.get_nth_parent_hash(previous_share.hash, self.node.net.CHAIN_LENGTH*9//10), self.node.net.CHAIN_LENGTH//10)
                upgraded = counts.get(successor_type.VERSION, 0)/sum(counts.itervalues())
                if upgraded > .65:
                    print 'Switchover imminent. Upgraded: %.3f%% Threshold: %.3f%%' % (upgraded*100, 95)
                # Share -> NewShare only valid if 95% of hashes in [net.CHAIN_LENGTH*9//10, net.CHAIN_LENGTH] for new version
                if counts.get(successor_type.VERSION, 0) > sum(counts.itervalues())*95//100:
                    share_type = successor_type
                else:
                    share_type = previous_share_type
        local_addr_rates = self.get_local_addr_rates()

        if desired_share_target is None:
            desired_share_target = 2**256-1
            local_hash_rate = local_addr_rates.get(pubkey_hash, 0)
            if local_hash_rate > 0.0:
                desired_share_target = min(desired_share_target,
                    bitcoin_data.average_attempts_to_target(local_hash_rate * self.node.net.SHARE_PERIOD / 0.0167)) # limit to 1.67% of pool shares by modulating share difficulty



            lookbehind = 3600//self.node.net.SHARE_PERIOD
            block_subsidy = self.node.bitcoind_work.value['subsidy']
            if previous_share is not None and self.node.tracker.get_height(previous_share.hash) > lookbehind:
                expected_payout_per_block = local_addr_rates.get(pubkey_hash, 0)/p2pool_data.get_pool_attempts_per_second(self.node.tracker, self.node.best_share_var.value, lookbehind) \
                    * block_subsidy*(1-self.donation_percentage/100) # XXX doesn't use global stale rate to compute pool hash
                if expected_payout_per_block < self.node.net.PARENT.DUST_THRESHOLD:
                    desired_share_target = min(desired_share_target,
                        bitcoin_data.average_attempts_to_target((bitcoin_data.target_to_average_attempts(self.node.bitcoind_work.value['bits'].target)*self.node.net.SPREAD)*self.node.net.PARENT.DUST_THRESHOLD/block_subsidy)
                    )

        if True:
            share_info, gentx, other_transaction_hashes, get_share = share_type.generate_transaction(
                tracker=self.node.tracker,
                share_data=dict(
                    previous_share_hash=self.node.best_share_var.value,
                    coinbase=(script.create_push_script([
                        self.current_work.value['height'],
                        ] + ([mm_data] if mm_data else []) + [
                    ]) + self.current_work.value['coinbaseflags'] + getattr(self.node.net, 'COINBASEEXT', b''))[:100],
                    coinbase_payload=self.current_work.value.get('coinbase_payload', b''),
                    nonce=random.randrange(2**32),
                    pubkey_hash=pubkey_hash,
                    subsidy=self.current_work.value['subsidy'],
                    donation=math.perfect_round(65535*self.donation_percentage/100),
                    stale_info=(lambda (orphans, doas), total, (orphans_recorded_in_chain, doas_recorded_in_chain):
                        'orphan' if orphans > orphans_recorded_in_chain else
                        'doa' if doas > doas_recorded_in_chain else
                        None
                    )(*self.get_stale_counts()),
                    desired_version=(share_type.SUCCESSOR if share_type.SUCCESSOR is not None else share_type).VOTING_VERSION,
                    payment_amount=self.current_work.value.get('payment_amount', 0),
                    packed_payments=self.current_work.value.get('packed_payments', b''),
                ),
                block_target=self.current_work.value['bits'].target,
                desired_timestamp=int(time.time() + 0.5),
                desired_target=desired_share_target,
                ref_merkle_link=dict(branch=[], index=0),
                desired_other_transaction_hashes_and_fees=zip(tx_hashes, self.current_work.value['transaction_fees']),
                net=self.node.net,
                known_txs=tx_map,
                base_subsidy=self.current_work.value['subsidy'],
            )

        packed_gentx = bitcoin_data.tx_type.pack(gentx)
        other_transactions = [tx_map[tx_hash] for tx_hash in other_transaction_hashes]

        mm_later = [(dict(aux_work, target=aux_work['target'] if aux_work['target'] != 'p2pool' else share_info['bits'].target), index, hashes) for aux_work, index, hashes in mm_later]

        if desired_pseudoshare_target is None:
            target = 2**256-1
            local_hash_rate = self._estimate_local_hash_rate()
            if local_hash_rate is not None:
                target = min(target,
                    bitcoin_data.average_attempts_to_target(local_hash_rate * 1)) # limit to 1 share response every second by modulating pseudoshare difficulty
        else:
            target = desired_pseudoshare_target
        for aux_work, index, hashes in mm_later:
            target = max(target, aux_work['target'])
        
        # Clip to SANE_TARGET_RANGE: [min_target (hardest), max_target (easiest)]
        # SANE_TARGET_RANGE[0] = lowest target = highest difficulty (e.g., 10000)
        # SANE_TARGET_RANGE[1] = highest target = lowest difficulty (e.g., 1)
        # We do NOT enforce P2Pool share floor here - that would prevent vardiff from
        # setting difficulty higher than the (potentially easy) P2Pool share chain.
        # Stratum separately checks if shares meet P2Pool criteria before crediting them.
        target = math.clip(target, self.node.net.PARENT.SANE_TARGET_RANGE)

        getwork_time = time.time()
        lp_count = self.new_work_event.times
        
        # CRITICAL: merkle_link must use other_transaction_hashes (the transactions actually in the share)
        # NOT tx_hashes (all available transactions). The share's coinbase + other_transaction_hashes
        # defines the block that will be submitted. The merkle_link must match this.
        # This is true for BOTH regular P2Pool AND merged mining.
        merkle_link = bitcoin_data.calculate_merkle_link([None] + other_transaction_hashes, 0)
        if mm_later:
            print >>sys.stderr, '[DEBUG] Merged mining: merkle_link uses other_transaction_hashes (%d txs)' % len(other_transaction_hashes)


        if print_throttle is 0.0:
            print_throttle = time.time()
        else:
            current_time = time.time()
            if (current_time - print_throttle) > 5.0:
                print 'New work for worker %s! Difficulty: %.06f Share difficulty: %.06f (speed %.06f) Total block value: %.6f %s including %i transactions' % (
                    bitcoin_data.pubkey_hash_to_address(pubkey_hash, self.node.net.PARENT.ADDRESS_VERSION, -1, self.node.net.PARENT),
                    bitcoin_data.target_to_difficulty(target),
                    bitcoin_data.target_to_difficulty(share_info['bits'].target),
                    local_addr_rates.get(pubkey_hash, 0),
                    self.current_work.value['subsidy']*1e-8, self.node.net.PARENT.SYMBOL,
                    len(self.current_work.value['transactions']),
                )
                print_throttle = time.time()

        #need this for stats
        self.last_work_shares.value[bitcoin_data.pubkey_hash_to_address(pubkey_hash, self.node.net.PARENT.ADDRESS_VERSION, -1, self.node.net.PARENT)]=share_info['bits']

        coinbase_payload_data_size = 0
        if gentx['version'] == 3 and gentx['type'] == 5:
            coinbase_payload_data_size = len(pack.VarStrType().pack(gentx['extra_payload']))

        # For stratum coinb1/coinb2, use tx_id_type (stripped format without SegWit marker)
        # This is necessary because:
        # 1. Miner computes merkle_root by hashing coinb1+nonce+coinb2
        # 2. We use get_txid() (which uses tx_id_type) for merkle calculations
        # 3. Both must produce the same hash for shares to be valid
        packed_gentx_stripped = bitcoin_data.tx_id_type.pack(gentx)

        # Fixed based on jtoomim's p2pool implementation
        # share_target = vardiff pseudoshare difficulty (already floored at p2pool_share_floor above)
        # min_share_target = P2Pool share chain difficulty floor (respects SANE_TARGET_RANGE)
        ba = dict(
            version=self.current_work.value['version'],
            previous_block=self.current_work.value['previous_block'],
            merkle_link=merkle_link,
            coinb1=packed_gentx_stripped[:-coinbase_payload_data_size-self.COINBASE_NONCE_LENGTH-4],
            coinb2=packed_gentx_stripped[-coinbase_payload_data_size-4:],
            timestamp=self.current_work.value['time'],
            bits=self.current_work.value['bits'],
            min_share_target=min(share_info['bits'].target, self.node.net.PARENT.SANE_TARGET_RANGE[1]),  # P2Pool share difficulty floor
            share_target=target,  # Vardiff pseudoshare target (already floored)
        )

        received_header_hashes = set()

        def got_response(header, user, coinbase_nonce, submitted_target=None):
            # submitted_target: optional override for the target the miner was actually working at
            # This is needed for vardiff - stratum adjusts target after get_work() returns
            effective_target = submitted_target if submitted_target is not None else target
            
            assert len(coinbase_nonce) == self.COINBASE_NONCE_LENGTH
            # IMPORTANT: CachingWorkerBridge modifies x['coinb1'] by appending caching nonce bytes,
            # but ba['coinb1'] is the original. The lambda in CachingWorkerBridge prepends the
            # caching nonce to coinbase_nonce before calling us. So coinbase_nonce is FULL length.
            # We must reconstruct from packed_gentx_stripped using the FULL coinbase_nonce.
            coinbase_payload_data_size_local = 0
            if gentx['version'] == 3 and gentx['type'] == 5:
                coinbase_payload_data_size_local = len(pack.VarStrType().pack(gentx['extra_payload']))
            # Reconstruct using stripped format (tx_id_type) to match miner's merkle calculation
            # ALWAYS reconstruct - even if nonce is all zeros, because CachingWorkerBridge
            # modifies coinb1 and stratum uses the modified version.
            new_packed_gentx = packed_gentx_stripped[:-coinbase_payload_data_size_local-self.COINBASE_NONCE_LENGTH-4] + coinbase_nonce + packed_gentx_stripped[-coinbase_payload_data_size_local-4:]
            new_gentx = bitcoin_data.tx_id_type.unpack(new_packed_gentx)

            # Debug: Print work.py's calculation for comparison with stratum
            # Show what we're actually using to construct new_packed_gentx
            coinb1_actual = packed_gentx_stripped[:-coinbase_payload_data_size_local-self.COINBASE_NONCE_LENGTH-4]
            coinb2_actual = packed_gentx_stripped[-coinbase_payload_data_size_local-4:]
            print >>sys.stderr, '[WORK DEBUG] packed_gentx_stripped length: %d' % len(packed_gentx_stripped)
            print >>sys.stderr, '[WORK DEBUG] coinbase_payload_data_size_local: %d' % coinbase_payload_data_size_local
            print >>sys.stderr, '[WORK DEBUG] COINBASE_NONCE_LENGTH: %d' % self.COINBASE_NONCE_LENGTH
            print >>sys.stderr, '[WORK DEBUG] coinb1_actual length: %d' % len(coinb1_actual)
            print >>sys.stderr, '[WORK DEBUG] coinb2_actual length: %d' % len(coinb2_actual)
            print >>sys.stderr, '[WORK DEBUG] coinbase_nonce hex: %s (len=%d)' % (coinbase_nonce.encode('hex'), len(coinbase_nonce))
            # Use txid (stripped hash without SegWit witness data) for merkle root
            # This ensures consistency with auxpow serialization which uses tx_id_type
            work_coinbase_txid = bitcoin_data.get_txid(new_gentx)
            work_coinbase_hash = work_coinbase_txid  # For backward compatibility in debug output
            work_merkle_root = bitcoin_data.check_merkle_link(work_coinbase_txid, ba['merkle_link'])
            print >>sys.stderr, '[WORK DEBUG] new_packed_gentx length: %d' % len(new_packed_gentx)
            print >>sys.stderr, '[WORK DEBUG] coinbase txid (stripped): %064x' % work_coinbase_txid
            print >>sys.stderr, '[WORK DEBUG] coinbase hash: %064x' % work_coinbase_hash
            print >>sys.stderr, '[WORK DEBUG] header[merkle_root]: %064x' % header['merkle_root']
            print >>sys.stderr, '[WORK DEBUG] calculated merkle_root: %064x' % work_merkle_root
            print >>sys.stderr, '[WORK DEBUG] MATCH: %s' % (work_merkle_root == header['merkle_root'])

            header_hash = self.node.net.PARENT.BLOCKHASH_FUNC(bitcoin_data.block_header_type.pack(header))
            pow_hash = self.node.net.PARENT.POW_FUNC(bitcoin_data.block_header_type.pack(header))
            
            # Debug: Save the header we hashed for later comparison
            if not hasattr(self, '_last_hashed_header'):
                self._last_hashed_header = {}
            self._last_hashed_header_packed = bitcoin_data.block_header_type.pack(header)
            self._last_hashed_header_hex = self._last_hashed_header_packed.encode('hex')
            
            # Debug: Log every 1000th attempt to monitor progress
            if not hasattr(self, '_attempt_counter'):
                self._attempt_counter = 0
                self._best_pow_hash = 2**256-1
            self._attempt_counter += 1
            if pow_hash < self._best_pow_hash:
                self._best_pow_hash = pow_hash
                ratio = float(pow_hash) / float(header['bits'].target)
                print >>sys.stderr, 'New best hash! pow=%064x target=%064x (%.2f%% of target)' % (pow_hash, header['bits'].target, ratio * 100)
            if self._attempt_counter % 1000 == 0:
                print >>sys.stderr, 'Block mining: %d attempts, best=%.2f%% of target' % (self._attempt_counter, float(self._best_pow_hash) / float(header['bits'].target) * 100)
            
            try:
                if pow_hash <= header['bits'].target or p2pool.DEBUG:
                    if pow_hash <= header['bits'].target:
                        print
                        print '#' * 70
                        print '### DASH BLOCK FOUND! ###'
                        print '#' * 70
                        print 'Time:        %s' % time.strftime('%Y-%m-%d %H:%M:%S')
                        print 'Miner:       %s' % user
                        print 'Block hash:  %064x' % header_hash
                        print 'POW hash:    %064x' % pow_hash
                        print 'Target:      %064x' % header['bits'].target
                        if 'height' in share_info:
                            print 'Height:      %d' % share_info['height']
                        print 'Txs:         %d' % (1 + len(other_transactions))
                        print 'Explorer:    %s%064x' % (self.node.net.PARENT.BLOCK_EXPLORER_URL_PREFIX, header_hash)
                        print '#' * 70
                        print
                    # Submit block and add error callback to catch any failures
                    block_submission = helper.submit_block(dict(header=header, txs=[new_gentx] + other_transactions), False, self.node.factory, self.node.bitcoind, self.node.bitcoind_work, self.node.net)
                    @block_submission.addErrback
                    def block_submit_error(err):
                        print >>sys.stderr, '*** CRITICAL: Block submission failed! ***'
                        log.err(err, 'Block submission error:')
                    if pow_hash <= header['bits'].target:
                        # New block found
                        self.node.factory.new_block.happened(header_hash)
            except:
                log.err(None, 'Error while processing potential block:')

            user, _, _, _, _ = self.get_user_details(user)
            assert header['previous_block'] == ba['previous_block']
            # Note: header['merkle_root'] is calculated in stratum.py with the correct coinbase_nonce
            # Don't recalculate it here because worker_interface.py prepends additional nonce data
            assert header['bits'] == ba['bits']

            # Allow shares that are within 3 work events of current (grace period for network latency)
            # Work events fire on new blocks, new best shares, etc. - can be rapid
            work_event_diff = self.new_work_event.times - lp_count
            on_time = work_event_diff <= 3  # Allow up to 3 work events behind

            # DEBUG: Check what's in mm_later
            print >>sys.stderr, '[DEBUG] mm_later has %d items, pow_hash=%064x' % (len(mm_later), pow_hash)
            for aux_work, index, hashes in mm_later:
                print >>sys.stderr, '[DEBUG] Checking aux_work target=%064x, meets=%s' % (aux_work['target'], pow_hash <= aux_work['target'])

            for aux_work, index, hashes in mm_later:
                try:
                    # Merged mining: Check if hash meets Auxiliary chain (Dogecoin) difficulty
                    # Three scenarios:
                    # 1. pow_hash < aux_work['target'] only: Valid DOGE block (partial win)
                    # 2. pow_hash < both targets: Valid LTC + DOGE blocks (full win)
                    # 3. pow_hash < header['bits'].target only: Valid LTC block only
                    
                    # Debug: log the comparison
                    if pow_hash <= aux_work['target']:
                        print >>sys.stderr, 'Dogecoin block candidate: pow_hash=%064x target=%064x ratio=%.2f%%' % (
                            pow_hash, aux_work['target'], float(pow_hash) / float(aux_work['target']) * 100)
                    
                    if pow_hash <= aux_work['target']:
                        # Hash meets Dogecoin difficulty - submit auxpow block
                        # Check if this is multiaddress merged mining (getblocktemplate with auxpow)
                        if aux_work.get('multiaddress'):
                            # Build complete Dogecoin block with auxpow proof
                            # The Litecoin block (parent) has already been mined with correct POW
                            # We just need to construct the Dogecoin block that references it
                            template = aux_work['template']
                            
                            # Get miner's merged addresses if provided  
                            merged_addrs = getattr(self, '_current_merged_addresses', {})
                            dogecoin_address = merged_addrs.get('dogecoin')
                            
                            print >>sys.stderr, '[DEBUG] Building Dogecoin auxpow block'
                            print >>sys.stderr, '[DEBUG] Litecoin pow_hash: %064x' % pow_hash
                            print >>sys.stderr, '[DEBUG] Dogecoin target: %064x' % aux_work['target']
                            print >>sys.stderr, '[DEBUG] Meets target: %s (%.2f%%)' % (
                                pow_hash <= aux_work['target'],
                                float(pow_hash) / float(aux_work['target']) * 100
                            )
                            
                            # PHASE C: Build Dogecoin block for submission using pre-calculated data
                            # Use the header and transactions we built BEFORE mining (Phase A)
                            try:
                                # Retrieve pre-built Dogecoin header from Phase A
                                if 'doge_header' not in aux_work:
                                    raise ValueError('Missing pre-built Dogecoin header from Phase A')
                                
                                doge_header = aux_work['doge_header'].copy()
                                doge_coinbase = aux_work['doge_coinbase']
                                doge_tx_hashes = aux_work['doge_tx_hashes']
                                
                                print >>sys.stderr, '[DEBUG] Using pre-built Dogecoin header from Phase A'
                                print >>sys.stderr, '[DEBUG] Dogecoin merkle root: %064x' % doge_header['merkle_root']
                                print >>sys.stderr, '[DEBUG] Dogecoin nonce (should be 0): %d' % doge_header['nonce']
                                print >>sys.stderr, '[DEBUG] Dogecoin block hash from aux_work: %064x' % aux_work['hash']
                                
                                # Verify: Calculate what the Dogecoin block hash should be
                                doge_header_packed_check = bitcoin_data.block_header_type.pack(doge_header)
                                doge_block_hash_check = bitcoin_data.hash256(doge_header_packed_check)
                                print >>sys.stderr, '[DEBUG] Dogecoin block hash (recalculated): %064x' % doge_block_hash_check
                                print >>sys.stderr, '[DEBUG] Do they match? %s' % (doge_block_hash_check == aux_work['hash'])
                                
                                # DON'T update nonce - in AuxPoW, child block nonce stays 0
                                # The actual mining work is done on the parent (Litecoin) block
                                # doge_header['nonce'] should already be 0 from Phase A
                                
                                # Debug: Compare coinbase transactions
                                print >>sys.stderr, '[DEBUG] new_gentx type: %s' % type(new_gentx)
                                print >>sys.stderr, '[DEBUG] gentx type: %s' % type(gentx)
                                # Use the DIRECT concatenation for hash (new_packed_gentx from line 595)
                                # NOT pack(new_gentx) which may differ due to unpack/repack!
                                print >>sys.stderr, '[DEBUG] Using direct concatenation (new_packed_gentx) for hash'
                                print >>sys.stderr, '[DEBUG] new_packed_gentx length: %d bytes' % len(new_packed_gentx)
                                print >>sys.stderr, '[DEBUG] packed_gentx length: %d bytes' % len(packed_gentx)
                                print >>sys.stderr, '[DEBUG] new_packed_gentx == packed_gentx: %s' % (new_packed_gentx == packed_gentx)
                                
                                # Calculate Litecoin coinbase hash using txid (stripped, no witness)
                                # This matches auxpow serialization which uses tx_id_type
                                ltc_coinbase_hash = bitcoin_data.get_txid(new_gentx)
                                ltc_coinbase_hash_from_original = bitcoin_data.get_txid(gentx)
                                
                                # CRITICAL: Verify that tx_id_type.pack(new_gentx) produces correct hash
                                packed_coinbase_for_auxpow = bitcoin_data.tx_id_type.pack(new_gentx)
                                print >>sys.stderr, '[DEBUG] new_packed_gentx length: %d' % len(new_packed_gentx)
                                print >>sys.stderr, '[DEBUG] packed_coinbase_for_auxpow length: %d' % len(packed_coinbase_for_auxpow)
                                print >>sys.stderr, '[DEBUG] ltc_coinbase_hash (txid): %064x' % ltc_coinbase_hash
                                
                                print >>sys.stderr, '[DEBUG] Litecoin coinbase txid (from new_gentx): %064x' % ltc_coinbase_hash
                                print >>sys.stderr, '[DEBUG] Litecoin coinbase txid (from gentx): %064x' % ltc_coinbase_hash_from_original
                                
                                # Build the ACTUAL Litecoin block's transaction list
                                # This is what gets submitted to the Litecoin network (see line 627)
                                # The merkle root should be calculated from THIS list, not from P2Pool share data
                                ltc_tx_list = [new_gentx] + other_transactions
                                # Use txid (stripped hash) for all transactions in merkle tree
                                ltc_tx_hashes = [ltc_coinbase_hash] + [bitcoin_data.get_txid(tx) for tx in other_transactions]
                                
                                # Calculate the REAL Litecoin block's merkle root
                                ltc_block_merkle_root = bitcoin_data.merkle_hash(ltc_tx_hashes)
                                
                                print >>sys.stderr, '[DEBUG] Litecoin block has %d transactions' % len(ltc_tx_list)
                                print >>sys.stderr, '[DEBUG] Litecoin block merkle root (calculated): %064x' % ltc_block_merkle_root
                                print >>sys.stderr, '[DEBUG] P2Pool share merkle root (from header): %064x' % header['merkle_root']
                                print >>sys.stderr, '[DEBUG] Are they the same? %s' % (ltc_block_merkle_root == header['merkle_root'])
                                
                                # CRITICAL: Use ba['merkle_link'] which was saved when this job was created!
                                # The miner's header merkle_root was computed using ba['merkle_link']
                                # NOT the current merkle_link (which may have changed)
                                ltc_coinbase_merkle_branch = ba['merkle_link']
                                
                                print >>sys.stderr, '[DEBUG] Using ba[merkle_link] from job (branch length: %d)' % len(ltc_coinbase_merkle_branch['branch'])
                                
                                # Verify: coinbase_hash + ba['merkle_link'] should equal header['merkle_root']
                                calculated_root = bitcoin_data.check_merkle_link(ltc_coinbase_hash, ltc_coinbase_merkle_branch)
                                print >>sys.stderr, '[DEBUG] Calculated merkle root from ba[merkle_link]: %064x' % calculated_root
                                print >>sys.stderr, '[DEBUG] Header merkle root: %064x' % header['merkle_root']
                                print >>sys.stderr, '[DEBUG] Do they MATCH? %s' % (calculated_root == header['merkle_root'])
                                
                                # Use the header as-is - merkle_root should match ba['merkle_link']
                                litecoin_header_for_auxpow = header.copy()
                                
                                print >>sys.stderr, '[DEBUG] Litecoin auxpow header merkle_root: %064x' % litecoin_header_for_auxpow['merkle_root']
                                print >>sys.stderr, '[DEBUG] Merkle roots match coinbase branch? %s' % (header['merkle_root'] == calculated_root)
                                
                                # Calculate the auxiliary chain merkle link
                                # This is the merkle branch from the Dogecoin block hash to the aux merkle root
                                # The aux merkle root is embedded in the Litecoin coinbase
                                aux_merkle_link = bitcoin_data.calculate_merkle_link(hashes, index)
                                
                                print >>sys.stderr, '[DEBUG] Aux merkle link: index=%d, branch_length=%d' % (index, len(aux_merkle_link['branch']))
                                print >>sys.stderr, '[DEBUG] Aux merkle hashes tree size: %d' % len(hashes)
                                for i, h in enumerate(hashes):
                                    print >>sys.stderr, '[DEBUG]   hashes[%d] = %064x' % (i, h)
                                
                                # Verify the aux merkle link leads to the correct root
                                aux_merkle_root_check = bitcoin_data.check_merkle_link(aux_work['hash'], aux_merkle_link)
                                print >>sys.stderr, '[DEBUG] Aux merkle root (from link): %064x' % aux_merkle_root_check
                                print >>sys.stderr, '[DEBUG] Aux merkle root (expected): %064x' % bitcoin_data.merkle_hash(hashes)
                                
                                # Reconstruct Dogecoin block using pre-built header and transactions
                                # The doge_coinbase and transactions were locked in during Phase A
                                doge_tx_list = [doge_coinbase] + [template['transactions'][i] for i in range(len(template.get('transactions', [])))]
                                
                                merged_block = dict(
                                    header=doge_header,
                                    txs=doge_tx_list,
                                    auxpow=dict(
                                        merkle_tx=dict(
                                            tx=new_gentx,  # Litecoin coinbase transaction
                                            block_hash=header_hash,  # Litecoin block hash (NOT used by Dogecoin validation)
                                            merkle_link=ltc_coinbase_merkle_branch,  # Merkle branch from LTC coinbase to LTC merkle root
                                        ),
                                        merkle_link=aux_merkle_link,  # FIXED: Merkle branch from DOGE block hash to aux merkle root
                                        parent_block_header=litecoin_header_for_auxpow,  # Litecoin block header
                                    )
                                )
                                
                                auxpow = merged_block['auxpow']
                                
                                # Pack complete auxpow block for submission
                                # Dogecoin auxpow block format: header + auxpow + transactions
                                # The auxpow must be serialized immediately after the header (before txs)
                                # because CBlock serialization includes auxpow in the header section
                                header_packed = bitcoin_data.block_header_type.pack(merged_block['header'])
                                
                                # Debug: Check Dogecoin block header bits vs our comparison
                                doge_header_target = merged_block['header']['bits'].target
                                doge_version = merged_block['header']['version']
                                print >>sys.stderr, 'Dogecoin block version=0x%x (auxpow bit set: %s)' % (
                                    doge_version, 
                                    'YES' if (doge_version & 0x100) else 'NO'
                                )
                                print >>sys.stderr, 'Dogecoin block header bits.target=%064x' % doge_header_target
                                print >>sys.stderr, 'aux_work target=%064x' % aux_work['target']
                                print >>sys.stderr, 'Litecoin POW hash=%064x' % pow_hash
                                # Use integer division to avoid float overflow
                                ratio_pct = (pow_hash * 100) // doge_header_target if doge_header_target > 0 else 0
                                print >>sys.stderr, 'Does LTC hash meet DOGE header target? %s (ratio=%d%%)' % (
                                    pow_hash <= doge_header_target,
                                    ratio_pct
                                )
                                
                                # Debug: Show Litecoin header details
                                ltc_header_packed = bitcoin_data.block_header_type.pack(litecoin_header_for_auxpow)
                                ltc_header_hash_from_packed = self.node.net.PARENT.POW_FUNC(ltc_header_packed)
                                print >>sys.stderr, '[DEBUG] Litecoin header in auxpow:'
                                print >>sys.stderr, '[DEBUG]   version: 0x%08x' % litecoin_header_for_auxpow['version']
                                print >>sys.stderr, '[DEBUG]   previous_block: %064x' % litecoin_header_for_auxpow['previous_block']
                                print >>sys.stderr, '[DEBUG]   merkle_root: %064x' % litecoin_header_for_auxpow['merkle_root']
                                print >>sys.stderr, '[DEBUG]   timestamp: %d' % litecoin_header_for_auxpow['timestamp']
                                print >>sys.stderr, '[DEBUG]   bits: 0x%08x' % litecoin_header_for_auxpow['bits'].bits
                                print >>sys.stderr, '[DEBUG]   nonce: 0x%08x' % litecoin_header_for_auxpow['nonce']
                                print >>sys.stderr, '[DEBUG]   POW hash (recalculated): %064x' % ltc_header_hash_from_packed
                                print >>sys.stderr, '[DEBUG]   POW hash (original): %064x' % pow_hash
                                print >>sys.stderr, '[DEBUG]   Do they match? %s' % (ltc_header_hash_from_packed == pow_hash)
                                
                                auxpow_packed = bitcoin_data.aux_pow_type.pack(auxpow)
                                
                                # DEBUG: What coinbase hash does the packed auxpow produce?
                                # Extract the coinbase tx from the packed auxpow
                                packed_coinbase_in_auxpow = bitcoin_data.tx_id_type.pack(new_gentx)
                                coinbase_hash_from_auxpow = bitcoin_data.hash256(packed_coinbase_in_auxpow)
                                print >>sys.stderr, '[DEBUG] Coinbase hash (from new_packed_gentx): %064x' % ltc_coinbase_hash
                                print >>sys.stderr, '[DEBUG] Coinbase hash (from tx_id_type.pack): %064x' % coinbase_hash_from_auxpow
                                print >>sys.stderr, '[DEBUG] Do auxpow coinbase hashes match? %s' % (ltc_coinbase_hash == coinbase_hash_from_auxpow)
                                print >>sys.stderr, '[DEBUG] packed_coinbase_in_auxpow length: %d' % len(packed_coinbase_in_auxpow)
                                print >>sys.stderr, '[DEBUG] new_packed_gentx length: %d' % len(new_packed_gentx)
                                if packed_coinbase_in_auxpow != new_packed_gentx:
                                    print >>sys.stderr, '[ERROR] Coinbase bytes MISMATCH in auxpow!'
                                    print >>sys.stderr, '[DEBUG] tx_id_type.pack(new_gentx)[:100]: %s' % packed_coinbase_in_auxpow[:100].encode('hex')
                                    print >>sys.stderr, '[DEBUG] new_packed_gentx[:100]: %s' % new_packed_gentx[:100].encode('hex')
                                
                                # Pack transactions
                                import StringIO
                                txs_stream = StringIO.StringIO()
                                pack.ListType(bitcoin_data.tx_type).write(txs_stream, merged_block['txs'])
                                txs_packed = txs_stream.getvalue()
                                
                                # Correct Dogecoin auxpow block format: header + auxpow + transactions
                                complete_block = header_packed + auxpow_packed + txs_packed
                                complete_block_hex = complete_block.encode('hex')
                                
                                # Debug: Show what we're submitting
                                print >>sys.stderr, 'Submitting Dogecoin auxpow block:'
                                print >>sys.stderr, '  Block hex length: %d bytes' % len(complete_block)
                                print >>sys.stderr, '  Header (first 80 bytes): %s' % complete_block[:80].encode('hex')
                                print >>sys.stderr, '  Bits field in header (bytes 72-76): %s' % complete_block[72:76].encode('hex')
                                print >>sys.stderr, '  Parent header nonce in auxpow: 0x%08x' % header['nonce']
                                
                                # Decode bits to verify
                                import struct
                                bits_packed = struct.unpack('<I', complete_block[72:76])[0]
                                print >>sys.stderr, '  Bits as uint32 (little-endian): 0x%08x' % bits_packed
                                
                                # Dump auxpow structure for debugging
                                print >>sys.stderr, '  [DEBUG] Full block hex (for manual decode):'
                                print >>sys.stderr, '  %s' % complete_block_hex
                                print >>sys.stderr, '  [DEBUG] Auxpow packed length: %d bytes' % len(auxpow_packed)
                                print >>sys.stderr, '  [DEBUG] Auxpow hex: %s' % auxpow_packed.encode('hex')
                                
                                # Compare parent header in auxpow with what we hashed
                                parent_header_in_auxpow = bitcoin_data.block_header_type.pack(header).encode('hex')
                                if hasattr(self, '_last_hashed_header_hex'):
                                    if parent_header_in_auxpow == self._last_hashed_header_hex:
                                        print >>sys.stderr, '  [OK] Parent header matches hashed header'
                                    else:
                                        print >>sys.stderr, '  [ERROR] Parent header MISMATCH!'
                                        print >>sys.stderr, '    Hashed:  %s' % self._last_hashed_header_hex
                                        print >>sys.stderr, '    In aux:  %s' % parent_header_in_auxpow
                                
                                # Submit via submitblock (modified Dogecoin with getblocktemplate auxpow support)
                                print 'Submitting multiaddress merged block via submitblock...'
                                print 'Block size: %d bytes (header + auxpow + %d txs)' % (len(complete_block), len(merged_block['txs']))
                                print '[DEBUG] About to call rpc_submitblock with %d byte hex string' % (len(complete_block_hex),)
                                print '[DEBUG] Block hex (first 200 chars): %s...' % (complete_block_hex[:200],)
                                df = deferral.retry('Error submitting multiaddress merged block: (will retry)', 10, 10)(
                                    aux_work['merged_proxy'].rpc_submitblock
                                )(complete_block_hex)
                                
                                @df.addCallback
                                def _(result, aux_work=aux_work):
                                    print '[DEBUG] rpc_submitblock returned: %r (type: %s)' % (result, type(result))
                                    if result is None or result == True:
                                        print 'Multiaddress merged block accepted!'
                                    else:
                                        print >>sys.stderr, 'Multiaddress merged block rejected: %s' % (result,)
                                
                                @df.addErrback
                                def _(err):
                                    print >>sys.stderr, '[DEBUG] rpc_submitblock raised error: %s' % (err,)
                                    log.err(err, 'Error submitting multiaddress merged block:')
                                    
                            except Exception as e:
                                print >>sys.stderr, 'Error building multiaddress merged block: %s' % (e,)
                                log.err(None, 'Error building multiaddress merged block:')
                        else:
                            # Standard getauxblock submission (backward compatible)
                            df = deferral.retry('Error submitting merged block: (will retry)', 10, 10)(aux_work['merged_proxy'].rpc_getauxblock)(
                                pack.IntType(256, 'big').pack(aux_work['hash']).encode('hex'),
                                bitcoin_data.aux_pow_type.pack(dict(
                                    merkle_tx=dict(
                                        tx=new_gentx,
                                        block_hash=header_hash,
                                        merkle_link=merkle_link,
                                    ),
                                    merkle_link=bitcoin_data.calculate_merkle_link(hashes, index),
                                    parent_block_header=header,
                                )).encode('hex'),
                            )
                            @df.addCallback
                            def _(result, aux_work=aux_work):
                                if result != (pow_hash <= aux_work['target']):
                                    print >>sys.stderr, 'Merged block submittal result: %s Expected: %s' % (result, pow_hash <= aux_work['target'])
                                else:
                                    print 'Merged block submittal result: %s' % (result,)
                            @df.addErrback
                            def _(err):
                                log.err(err, 'Error submitting merged block:')
                except:
                    log.err(None, 'Error while processing merged mining POW:')

            # TODO: P2Pool share creation doesn't work with Stratum yet because Share.__init__
            # reconstructs gentx from the original packed_gentx, but Stratum miners change
            # the coinbase_nonce which modifies gentx. This causes merkle_root mismatch.
            # For now, treat all submissions as pseudoshares.
            if False and pow_hash <= share_info['bits'].target and header_hash not in received_header_hashes:
                print >>sys.stderr, '[DEBUG] Attempting to create P2Pool share:'
                print >>sys.stderr, '  pow_hash: %064x' % pow_hash
                print >>sys.stderr, '  target:   %064x' % share_info['bits'].target
                print >>sys.stderr, '  passes:   %s' % (pow_hash <= share_info['bits'].target)
                print >>sys.stderr, '  header: %s' % header
                last_txout_nonce = pack.IntType(8*self.COINBASE_NONCE_LENGTH).unpack(coinbase_nonce)
                try:
                    share = get_share(header, last_txout_nonce)
                except Exception as e:
                    print >>sys.stderr, '[DEBUG] get_share failed: %s' % e
                    print >>sys.stderr, '[DEBUG] Recalculating pow_hash with header:'
                    recalc_pow = self.node.net.PARENT.POW_FUNC(bitcoin_data.block_header_type.pack(header))
                    print >>sys.stderr, '  Recalc pow_hash: %064x' % recalc_pow
                    print >>sys.stderr, '  Original pow_hash: %064x' % pow_hash
                    print >>sys.stderr, '  Match: %s' % (recalc_pow == pow_hash)
                    raise

                print 'GOT SHARE! %s %s prev %s age %.2fs%s' % (
                    user,
                    p2pool_data.format_hash(share.hash),
                    p2pool_data.format_hash(share.previous_hash),
                    time.time() - getwork_time,
                    ' DEAD ON ARRIVAL' if not on_time else '',
                )
                self.my_share_hashes.add(share.hash)
                if not on_time:
                    self.my_doa_share_hashes.add(share.hash)

                self.node.tracker.add(share)
                self.node.set_best_share()

                try:
                    if (pow_hash <= header['bits'].target or p2pool.DEBUG) and self.node.p2p_node is not None:
                        self.node.p2p_node.broadcast_share(share.hash)
                except:
                    log.err(None, 'Error forwarding block solution:')

                self.share_received.happened(bitcoin_data.target_to_average_attempts(share.target), not on_time, share.hash)
                
                # Update local rate monitor for shares (they are also pseudoshares)
                # Use effective_target (vardiff target) for work calculation
                self.local_rate_monitor.add_datum(dict(work=bitcoin_data.target_to_average_attempts(effective_target), dead=not on_time, user=user, share_target=share_info['bits'].target))
                self.local_addr_rate_monitor.add_datum(dict(work=bitcoin_data.target_to_average_attempts(effective_target), pubkey_hash=pubkey_hash))
                received_header_hashes.add(header_hash)
            elif pow_hash > effective_target:
                print 'Worker %s submitted share with hash > target:' % (user,)
                print '    Hash:   %56x' % (pow_hash,)
                print '    Target: %56x' % (effective_target,)
            elif header_hash in received_header_hashes:
                print >>sys.stderr, 'Worker %s submitted share more than once!' % (user,)
            else:
                received_header_hashes.add(header_hash)

                work_value = bitcoin_data.target_to_average_attempts(effective_target)
                self.pseudoshare_received.happened(work_value, not on_time, user)
                self.recent_shares_ts_work.append((time.time(), work_value))
                while len(self.recent_shares_ts_work) > 50:
                    self.recent_shares_ts_work.pop(0)
                self.local_rate_monitor.add_datum(dict(work=work_value, dead=not on_time, user=user, share_target=share_info['bits'].target))
                self.local_addr_rate_monitor.add_datum(dict(work=work_value, pubkey_hash=pubkey_hash))

            return on_time

        t1 = time.time()
        if p2pool.BENCH:
            print "%8.3f ms for work.py:get_work(%s)" % ((t1-t0)*1000., bitcoin_data.pubkey_hash_to_address(pubkey_hash, self.node.net.PARENT.ADDRESS_VERSION, -1, self.node.net.PARENT))
        
        return ba, got_response
