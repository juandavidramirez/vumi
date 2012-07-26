from twisted.trial import unittest
from twisted.internet.task import Clock
from twisted.internet.defer import inlineCallbacks, returnValue
from smpp.pdu_builder import DeliverSM, BindTransceiverResp
from smpp.pdu import unpack_pdu

from vumi.tests.utils import LogCatcher, PersistenceMixin
from vumi.transports.smpp.clientserver.client import (
    EsmeTransceiver, EsmeReceiver, EsmeTransmitter, EsmeCallbacks, ESME)
from vumi.transports.smpp.clientserver.config import ClientConfig


class FakeTransport(object):
    def __init__(self):
        self.connected = True

    def loseConnection(self):
        self.connected = False


class FakeEsmeTransceiver(EsmeTransceiver):

    def __init__(self, *args, **kwargs):
        EsmeTransceiver.__init__(self, *args, **kwargs)
        self.transport = FakeTransport()
        self.clock = Clock()
        self.callLater = self.clock.callLater

    def send_pdu(self, *args):
        pass


class FakeEsmeReceiver(EsmeReceiver):

    def __init__(self, *args, **kwargs):
        EsmeReceiver.__init__(self, *args, **kwargs)
        self.transport = FakeTransport()
        self.clock = Clock()
        self.callLater = self.clock.callLater

    def send_pdu(self, *args):
        pass


class FakeEsmeTransmitter(EsmeTransmitter):

    def __init__(self, *args, **kwargs):
        EsmeTransmitter.__init__(self, *args, **kwargs)
        self.transport = FakeTransport()
        self.clock = Clock()
        self.callLater = self.clock.callLater

    def send_pdu(self, *args):
        pass


class EsmeTestCaseBase(unittest.TestCase, PersistenceMixin):
    timeout = 5
    ESME_CLASS = None

    def setUp(self):
        self._persist_setUp()
        self._expected_callbacks = []

    def tearDown(self):
        self.assertEqual(self._expected_callbacks, [], "Uncalled callbacks.")
        return self._persist_tearDown()

    @inlineCallbacks
    def get_unbound_esme(self, **callbacks):
        config = ClientConfig(host="127.0.0.1", port="0",
                              system_id="1234", password="password")
        esme_callbacks = EsmeCallbacks(**callbacks)
        redis = yield self.get_redis_manager()
        returnValue(self.ESME_CLASS(config, redis, esme_callbacks))

    @inlineCallbacks
    def get_esme(self, **callbacks):
        esme = yield self.get_unbound_esme(**callbacks)
        yield esme.connectionMade()
        esme.state = esme.CONNECTED_STATE
        returnValue(esme)

    def get_sm(self, msg, data_coding=3):
        sm = DeliverSM(1, short_message=msg, data_coding=data_coding)
        return unpack_pdu(sm.get_bin())

    def assertion_cb(self, expected, *message_path):
        cb_id = len(self._expected_callbacks)
        self._expected_callbacks.append(cb_id)

        def cb(**value):
            self._expected_callbacks.remove(cb_id)
            for k in message_path:
                value = value[k]
            self.assertEqual(expected, value)

        return cb


class EsmeGenericMixin(object):
    """Generic tests."""

    @inlineCallbacks
    def test_bind_timeout(self):
        esme = yield self.get_unbound_esme()
        yield esme.connectionMade()

        self.assertEqual(True, esme.transport.connected)
        self.assertNotEqual(None, esme._lose_conn)

        esme.clock.advance(esme.smpp_bind_timeout)

        self.assertEqual(False, esme.transport.connected)
        self.assertEqual(None, esme._lose_conn)

    @inlineCallbacks
    def test_bind_no_timeout(self):
        esme = yield self.get_unbound_esme()
        yield esme.connectionMade()

        self.assertEqual(True, esme.transport.connected)
        self.assertNotEqual(None, esme._lose_conn)

        esme.handle_bind_transceiver_resp(unpack_pdu(
            BindTransceiverResp(1).get_bin()))

        self.assertEqual(True, esme.transport.connected)
        self.assertEqual(None, esme._lose_conn)
        esme.lc_enquire.stop()

    @inlineCallbacks
    def test_sequence_rollover(self):
        esme = yield self.get_unbound_esme()
        self.assertEqual(1, (yield esme.get_next_seq()))
        self.assertEqual(2, (yield esme.get_next_seq()))
        yield esme.redis.set('smpp_last_sequence_number', 0xFFFF0000)
        self.assertEqual(0xFFFF0001, (yield esme.get_next_seq()))
        self.assertEqual(1, (yield esme.get_next_seq()))


class EsmeTransmitterMixin(EsmeGenericMixin):
    """Transmitter-side tests."""

    # TODO: Write some.


class EsmeReceiverMixin(EsmeGenericMixin):
    """Receiver-side tests."""

    @inlineCallbacks
    def test_deliver_sm_simple(self):
        """A simple message should be delivered."""
        esme = yield self.get_esme(
            deliver_sm=self.assertion_cb(u'hello', 'short_message'))
        yield esme.handle_deliver_sm(self.get_sm('hello'))

    @inlineCallbacks
    def test_deliver_sm_ucs2(self):
        """A UCS-2 message should be delivered."""
        esme = yield self.get_esme(
            deliver_sm=self.assertion_cb(u'hello', 'short_message'))
        yield esme.handle_deliver_sm(
            self.get_sm('\x00h\x00e\x00l\x00l\x00o', 8))

    @inlineCallbacks
    def test_bad_sm_ucs2(self):
        """An invalid UCS-2 message should be discarded."""
        bad_msg = '\n\x00h\x00e\x00l\x00l\x00o'

        esme = yield self.get_esme(
            deliver_sm=self.assertion_cb(bad_msg, 'short_message'))

        yield esme.handle_deliver_sm(self.get_sm(bad_msg, 8))
        self.flushLoggedErrors()

    @inlineCallbacks
    def test_deliver_sm_delivery_report(self):
        esme = yield self.get_esme(delivery_report=self.assertion_cb(
                u'DELIVRD', 'delivery_report', 'stat'))

        yield esme.handle_deliver_sm(self.get_sm(
                'id:1b1720be-5f48-41c4-b3f8-6e59dbf45366 sub:001 dlvrd:001 '
                'submit date:120726132548 done date:120726132548 stat:DELIVRD '
                'err:000 text:'))

    @inlineCallbacks
    def test_deliver_sm_multipart(self):
        esme = yield self.get_esme(
            deliver_sm=self.assertion_cb(u'hello world', 'short_message'))
        yield esme.handle_deliver_sm(self.get_sm(
                "\x05\x00\x03\xff\x02\x02 world"))
        yield esme.handle_deliver_sm(self.get_sm(
                "\x05\x00\x03\xff\x02\x01hello"))


class EsmeTransceiverTestCase(EsmeTestCaseBase, EsmeReceiverMixin,
                              EsmeTransmitterMixin):
    ESME_CLASS = FakeEsmeTransceiver


class EsmeTransmitterTestCase(EsmeTestCaseBase, EsmeTransmitterMixin):
    ESME_CLASS = FakeEsmeTransmitter

    @inlineCallbacks
    def test_deliver_sm_simple(self):
        """A message delivery should log an error since we're supposed
        to be a transmitter only."""
        def cb(**kw):
            self.assertEqual(u'hello', kw['short_message'])

        with LogCatcher() as log:
            esme = yield self.get_esme(deliver_sm=cb)
            esme.state = 'BOUND_TX'  # Assume we've bound correctly as a TX
            esme.handle_deliver_sm(self.get_sm('hello'))
            [error] = log.errors
            self.assertTrue('deliver_sm in wrong state' in error['message'][0])


class EsmeReceiverTestCase(EsmeTestCaseBase, EsmeReceiverMixin):
    ESME_CLASS = FakeEsmeReceiver

    @inlineCallbacks
    def test_submit_sm_simple(self):
        """A simple message log an error when trying to send over
        a receiver."""
        with LogCatcher() as log:
            esme = yield self.get_esme()
            esme.state = 'BOUND_RX'  # Fake RX bind
            esme.submit_sm(short_message='hello')
            [error] = log.errors
            self.assertTrue(('submit_sm in wrong state' in
                                            error['message'][0]))


class ESMETestCase(unittest.TestCase):

    def setUp(self):
        self.client_config = ClientConfig(
                host='localhost',
                port=2775,
                system_id='test_system',
                password='password',
                )
        self.kvs = None
        self.esme_callbacks = None
        self.esme = ESME(self.client_config, self.kvs,
                         self.esme_callbacks)

    def test_bind_as_transceiver(self):
        return self.esme.bindTransciever()
