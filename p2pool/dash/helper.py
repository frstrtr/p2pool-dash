import sys
import time

from twisted.internet import defer, error as twisted_error

import p2pool
from p2pool.dash import data as dash_data
from p2pool.util import deferral, jsonrpc

@deferral.retry('Error while checking dash connection:', 1)
@defer.inlineCallbacks
def check(dashd, net):
    if not (yield net.PARENT.RPC_CHECK(dashd)):
        print >>sys.stderr, "    Check failed! Make sure that you're connected to the right dashd with --dashd-rpc-port!"
        raise deferral.RetrySilentlyException()
    if not net.VERSION_CHECK((yield dashd.rpc_getnetworkinfo())['version']):
        print >>sys.stderr, '    dash version too old! Upgrade to v20.0.0 or newer!'
        raise deferral.RetrySilentlyException()

@deferral.retry('Error getting work from dashd:', 3)
@defer.inlineCallbacks
def getwork(dashd, net, use_getblocktemplate=True):
    def go():
        if use_getblocktemplate:
            return dashd.rpc_getblocktemplate(dict(mode='template'))
        else:
            return dashd.rpc_getmemorypool()
    try:
        start = time.time()
        work = yield go()
        end = time.time()
    except twisted_error.TimeoutError:
        print >>sys.stderr, '    dashd RPC timeout - dashd may be busy or overloaded, retrying...'
        raise deferral.RetrySilentlyException()
    except twisted_error.ConnectionRefusedError:
        print >>sys.stderr, '    dashd connection refused - is dashd running?'
        raise deferral.RetrySilentlyException()
    except jsonrpc.Error_for_code(-32601): # Method not found
        use_getblocktemplate = not use_getblocktemplate
        try:
            start = time.time()
            work = yield go()
            end = time.time()
        except jsonrpc.Error_for_code(-32601): # Method not found
            print >>sys.stderr, 'Error: dash version too old! Upgrade to v20.0.0 or newer!'
            raise deferral.RetrySilentlyException()

    # Include ALL transactions from getblocktemplate
    # Per BIP 22, transactions are already ordered with dependencies before dependents
    packed_transactions = []
    for x in work.get('transactions', []):
        if isinstance(x, dict):
            packed_transactions.append(x['data'].decode('hex'))
        else:
            packed_transactions.append(x.decode('hex'))

    if 'height' not in work:
        work['height'] = (yield dashd.rpc_getblock(work['previousblockhash']))['height'] + 1
    elif p2pool.DEBUG:
        assert work['height'] == (yield dashd.rpc_getblock(work['previousblockhash']))['height'] + 1

    # Dash Payments
    packed_payments = []
    payment_amount = 0

    payment_objects = []
    if 'masternode' in work:
        if isinstance(work['masternode'], list):
            payment_objects += work['masternode']
        else:
            payment_objects += [work['masternode']]
    if 'superblock' in work:
        payment_objects += work['superblock']

    for obj in payment_objects:
        g={}
        # Always count the amount towards payment_amount, even if payee is empty
        if 'amount' in obj and obj['amount'] > 0:
            payment_amount += obj['amount']
            # Only add to packed_payments if there's a valid payee address
            if 'payee' in obj and obj['payee']:
                g['payee'] = str(obj['payee'])
                g['amount'] = obj['amount']
                packed_payments.append(g)

    coinbase_payload = None
    if 'coinbase_payload' in work and len(work['coinbase_payload']) != 0:
        coinbase_payload = work['coinbase_payload'].decode('hex')

    defer.returnValue(dict(
        version=work['version'],
        previous_block=int(work['previousblockhash'], 16),
        transactions=map(dash_data.tx_type.unpack, packed_transactions),
        transaction_hashes=map(dash_data.hash256, packed_transactions),
        transaction_fees=[x.get('fee', None) if isinstance(x, dict) else None for x in work['transactions']],
        subsidy=work['coinbasevalue'],
        time=work['time'] if 'time' in work else work['curtime'],
        bits=dash_data.FloatingIntegerType().unpack(work['bits'].decode('hex')[::-1]) if isinstance(work['bits'], (str, unicode)) else dash_data.FloatingInteger(work['bits']),
        coinbaseflags=work['coinbaseflags'].decode('hex') if 'coinbaseflags' in work else ''.join(x.decode('hex') for x in work['coinbaseaux'].itervalues()) if 'coinbaseaux' in work else '',
        height=work['height'],
        last_update=time.time(),
        use_getblocktemplate=use_getblocktemplate,
        latency=end - start,
        payment_amount = payment_amount,
        packed_payments = packed_payments,
        coinbase_payload = coinbase_payload,
    ))

@deferral.retry('Error submitting primary block: (will retry)', 10, 10)
def submit_block_p2p(block, factory, net):
    """Submit found block via Dash P2P network for fast propagation."""
    block_hash = dash_data.hash256(dash_data.block_header_type.pack(block['header']))
    
    if factory.conn.value is None:
        print >>sys.stderr, 'No dashd P2P connection when block submittal attempted! %s%064x' % (net.PARENT.BLOCK_EXPLORER_URL_PREFIX, block_hash)
        raise deferral.RetrySilentlyException()
    
    print 'P2P: Sending block %064x to dashd via P2P protocol...' % block_hash
    factory.conn.value.send_block(block=block)
    print 'P2P: Block sent successfully to dashd for network propagation'

@deferral.retry('Error submitting block: (will retry)', 10, 10)
@defer.inlineCallbacks
def submit_block_rpc(block, ignore_failure, dashd, dashd_work, net):
    block_hash = dash_data.hash256(dash_data.block_header_type.pack(block['header']))
    pow_hash = net.PARENT.POW_FUNC(dash_data.block_header_type.pack(block['header']))
    block_height = block['header'].get('height', 'unknown')
    
    print ''
    print '=' * 70
    print 'BLOCK SUBMISSION STARTED at %s' % time.strftime('%Y-%m-%d %H:%M:%S')
    print '  Block hash:   %064x' % block_hash
    print '  POW hash:     %064x' % pow_hash
    print '  Target:       %064x' % block['header']['bits'].target
    print '  Transactions: %d' % len(block.get('txs', []))
    print '=' * 70
    
    if dashd_work.value['use_getblocktemplate']:
        try:
            result = yield dashd.rpc_submitblock(dash_data.block_type.pack(block).encode('hex'))
        except jsonrpc.Error_for_code(-32601): # Method not found, for older litecoin versions
            result = yield dashd.rpc_getblocktemplate(dict(mode='submit', data=dash_data.block_type.pack(block).encode('hex')))
        # submitblock returns None on success, "duplicate" if block already known (P2P won the race!)
        success = result is None or result == 'duplicate'
        p2p_won_race = result == 'duplicate'
    else:
        result = yield dashd.rpc_getmemorypool(dash_data.block_type.pack(block).encode('hex'))
        success = result
        p2p_won_race = False
    success_expected = pow_hash <= block['header']['bits'].target
    
    print 'BLOCK SUBMISSION RESULT (RPC):'
    if p2p_won_race:
        print '  *** P2P WON THE RACE! Block already propagated via P2P network ***'
        print '  RPC result: %r (this is expected when P2P submits first)' % result
        print '  SUCCESS: Block was accepted!'
    else:
        print '  RPC accepted: %s (result: %r)' % (result is None, result)
    print '  Expected success: %s' % success_expected
    
    if (not success and success_expected and not ignore_failure) or (success and not success_expected):
        print >>sys.stderr, 'Block submittal result: %s (%r) Expected: %s' % (success, result, success_expected)
    
    # Check chainlock status after submission
    if success:
        yield check_block_chainlock(dashd, block_hash, net)

@defer.inlineCallbacks
def submit_block(block, ignore_failure, factory, dashd, dashd_work, net):
    """Submit block via both P2P and RPC for redundant propagation."""
    # Submit via P2P first for fastest network propagation (synchronous)
    submit_block_p2p(block, factory, net)
    # Also submit via RPC (submitblock call) and wait for result
    yield submit_block_rpc(block, ignore_failure, dashd, dashd_work, net)

@defer.inlineCallbacks
def check_block_header(bitcoind, block_hash):
    try:
        yield bitcoind.rpc_getblockheader(block_hash)
    except jsonrpc.Error_for_code(-5):
        defer.returnValue(False)
    else:
        defer.returnValue(True)

@defer.inlineCallbacks
def check_block_chainlock(dashd, block_hash, net):
    """Check and log the chainlock status of a submitted block."""
    from twisted.internet import reactor
    
    # Wait a few seconds for block to propagate
    for delay in [2, 5, 10, 30, 60]:
        yield deferral.sleep(delay)
        try:
            block_info = yield dashd.rpc_getblock('%064x' % block_hash)
            chainlock = block_info.get('chainlock', False)
            confirmations = block_info.get('confirmations', 0)
            height = block_info.get('height', 'unknown')
            
            print ''
            print 'CHAINLOCK STATUS CHECK (%ds after submission):' % delay
            print '  Block hash:    %064x' % block_hash
            print '  Height:        %s' % height
            print '  Confirmations: %s' % confirmations
            print '  ChainLock:     %s' % ('YES - LOCKED!' if chainlock else 'NO - not yet locked')
            
            if chainlock:
                print ''
                print '*** BLOCK CHAINLOCKED SUCCESSFULLY! ***'
                print '  Explorer: %s%064x' % (net.PARENT.BLOCK_EXPLORER_URL_PREFIX, block_hash)
                print ''
                defer.returnValue(True)
            
            if confirmations < 0:
                print ''
                print '*** WARNING: BLOCK MAY BE ORPHANED (confirmations=%d) ***' % confirmations
                print '  Another block may have been chainlocked at this height.'
                print ''
                # Check what block is at this height now
                try:
                    current_hash = yield dashd.rpc_getblockhash(height)
                    if current_hash != '%064x' % block_hash:
                        print '  Current block at height %s: %s' % (height, current_hash)
                        current_block = yield dashd.rpc_getblock(current_hash)
                        print '  Current block chainlock: %s' % current_block.get('chainlock', False)
                except Exception as e:
                    print '  Error checking current block: %s' % e
                defer.returnValue(False)
                
        except jsonrpc.Error_for_code(-5):
            print 'CHAINLOCK CHECK: Block %064x not found in local node (may be orphaned)' % block_hash
            defer.returnValue(False)
        except Exception as e:
            print 'CHAINLOCK CHECK: Error checking block: %s' % e
    
    print ''
    print '*** WARNING: Block not chainlocked after 60 seconds ***'
    print '  Block may be at risk of being orphaned if another chainlock wins.'
    print ''
    defer.returnValue(False)
