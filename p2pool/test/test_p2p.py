import random

from twisted.internet import defer, endpoints, protocol, reactor
from twisted.trial import unittest

from p2pool import networks, p2p
from p2pool.dash import data as dash_data
from p2pool.util import deferral


class Test(unittest.TestCase):
    @defer.inlineCallbacks
    def test_sharereq(self):
        class MyNode(p2p.Node):
            def __init__(self, df):
                p2p.Node.__init__(self, lambda: None, 28999, networks.nets['dash'], {}, set([('127.0.0.1', 8999)]), 0, 0, 0, 0)
                
                self.df = df
            
            def handle_share_hashes(self, hashes, peer):
                peer.get_shares(
                    hashes=[hashes[0]],
                    parents=5,
                    stops=[],
                ).chainDeferred(self.df)
        
        df = defer.Deferred()
        n = MyNode(df)
        n.start()
        try:
            yield df
        finally:
            yield n.stop()
    
    @defer.inlineCallbacks
    def test_tx_limit(self):
        class MyNode(p2p.Node):
            def __init__(self, df):
                p2p.Node.__init__(self, lambda: None, 28999, networks.nets['dash'], {}, set([('127.0.0.1', 8999)]), 0, 0, 0, 0)
                
                self.df = df
                self.sent_time = 0
            
            @defer.inlineCallbacks
            def got_conn(self, conn):
                p2p.Node.got_conn(self, conn)
                
                yield deferral.sleep(.5)
                
                new_mining_txs = dict(self.mining_txs_var.value)
                for i in xrange(3):
                    huge_tx = dict(
                        version=0,
                        type=0,
                        tx_ins=[],
                        tx_outs=[dict(
                            value=0,
                            script='x'*900000,
                        )],
                        lock_time=i,
                        extra_payload=None,
                    )
                    new_mining_txs[dash_data.hash256(dash_data.tx_type.pack(huge_tx))] = huge_tx
                self.mining_txs_var.set(new_mining_txs)
                
                self.sent_time = reactor.seconds()
            
            def lost_conn(self, conn, reason):
                self.df.callback(None)
        try:
            p2p.Protocol.max_remembered_txs_size *= 10
            
            df = defer.Deferred()
            n = MyNode(df)
            n.start()
            yield df
            if not (n.sent_time <= reactor.seconds() <= n.sent_time + 1):
                raise ValueError('node did not disconnect within 1 seconds of receiving too much tx data')
            yield n.stop()
        finally:
            p2p.Protocol.max_remembered_txs_size //= 10


class LoopbackBanTest(unittest.TestCase):
    # Regression tests for issue #757: p2pool-dash silently RSTs loopback
    # reconnects after connection churn because a ban entry for 127.0.0.1 gets
    # written by the self-nonce / connect-failure paths and then poisons
    # ServerFactory.buildProtocol. Co-located / loopback peers must never be
    # banned (matching the "never ban localhost" policy in badPeerHappened).
    def test_is_loopback(self):
        self.assertTrue(p2p.is_loopback('127.0.0.1'))
        self.assertTrue(p2p.is_loopback('127.1.2.3'))
        self.assertTrue(p2p.is_loopback('::1'))
        self.assertFalse(p2p.is_loopback('8.8.8.8'))
        self.assertFalse(p2p.is_loopback(None))

    def test_client_failure_never_bans_loopback(self):
        class FakeConnector(object):
            def __init__(self, host):
                self._host = host
            def getDestination(self):
                class D(object):
                    pass
                d = D()
                d.host, d.port = self._host, 8999
                return d
        class FakeReason(object):
            def getErrorMessage(self):
                return 'connection refused'

        # loopback outbound failure during churn must NOT create a ban
        node = type('N', (object,), {})()
        node.bans = {}
        cf = p2p.ClientFactory(node, 0, 0)
        cf.attempts.add(('127', '0'))
        cf.clientConnectionFailed(FakeConnector('127.0.0.1'), FakeReason())
        self.assertNotIn('127.0.0.1', node.bans)

        # sanity: a real remote failure IS still banned (behavior preserved)
        node2 = type('N', (object,), {})()
        node2.bans = {}
        cf2 = p2p.ClientFactory(node2, 0, 0)
        cf2.attempts.add(('8', '8'))
        cf2.clientConnectionFailed(FakeConnector('8.8.8.8'), FakeReason())
        self.assertIn('8.8.8.8', node2.bans)

    def test_buildprotocol_admits_banned_loopback(self):
        import time as _time
        class FakeProto(object):
            pass
        node = type('N', (object,), {})()
        node.bans = {'127.0.0.1': _time.time() + 3600, '8.8.8.8': _time.time() + 3600}
        sf = p2p.ServerFactory(node, 100)
        orig = p2p.Protocol
        p2p.Protocol = lambda *a, **k: FakeProto()
        try:
            class Addr(object):
                pass
            a = Addr(); a.host = '127.0.0.1'
            # even with 127.0.0.1 in bans, the loopback re-accept must be admitted
            self.assertIsInstance(sf.buildProtocol(a), FakeProto)
            b = Addr(); b.host = '8.8.8.8'
            # a genuinely-banned remote host is still refused (behavior preserved)
            self.assertIsNone(sf.buildProtocol(b))
        finally:
            p2p.Protocol = orig
