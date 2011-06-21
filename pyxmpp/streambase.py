#
# (C) Copyright 2003-2011 Jacek Konieczny <jajcus@jajcus.net>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License Version
# 2.1 as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, write to the Free Software
# Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.
#
# pylint: disable-msg=W0201

"""Core XMPP stream functionality.

Normative reference:
  - `RFC 6120 <http://xmpp.org/rfcs/rfc6120.html>`__
"""

from __future__ import absolute_import

__docformat__ = "restructuredtext en"

import inspect
import logging
import uuid
import re
from abc import ABCMeta

from .etree import ElementTree


from .xmppparser import XMLStreamHandler
from .error import StreamErrorElement
from .jid import JID
from .stanzaprocessor import StanzaProcessor, stanza_factory
from .exceptions import StreamError
from .exceptions import FatalStreamError, StreamParseError
from .constants import STREAM_QNP, XML_LANG_QNAME, STREAM_ROOT_TAG
from .settings import XMPPSettings
from .xmppserializer import serialize
from .streamevents import ConnectedEvent
from .streamevents import StreamConnectedEvent, GotFeaturesEvent
from .streamevents import AuthenticatedEvent, StreamRestartedEvent
from .mainloop.interfaces import EventHandler, event_handler

XMPPSettings.add_defaults(
        {
            u"language": "en",
            u"languages": ("en",),
            u"default_stanza_timeout": 300,
            u"extra_ns_prefixes": {},
        })

logger = logging.getLogger("pyxmpp.streambase")

LANG_SPLIT_RE = re.compile(r"(.*)(?:-[a-zA-Z0-9])?-[a-zA-Z0-9]+$")

ERROR_TAG = STREAM_QNP + u"error"
FEATURES_TAG = STREAM_QNP + u"features"

# just to distinguish those from a domain name
IP_RE = re.compile(r"^((\d+.){3}\d+)|([0-9a-f]*:[0-9a-f:]*:[0-9a-f]*)$")


class StreamFeatureHandled(object):
    """Object returned by a stream feature handler for recognized and handled
    features.
    """
    # pylint: disable-msg=R0903
    def __init__(self, feature_name, mandatory = False):
        self.feature_name = feature_name
        self.mandatory = mandatory
    def __repr__(self):
        if self.mandatory:
            return "StreamFeatureHandled({0!r}, mandatory = True)".format(
                                                            self.feature_name)
        else:
            return "StreamFeatureHandled({0!r})".format(self.feature_name)
    def __str__(self):
        return self.feature_name

class StreamFeatureNotHandled(object):
    """Object returned by a stream feature handler for recognized,
    but unhandled features.
    """
    # pylint: disable-msg=R0903
    def __init__(self, feature_name, mandatory = False):
        self.feature_name = feature_name
        self.mandatory = mandatory
    def __repr__(self):
        if self.mandatory:
            return "StreamFeatureNotHandled({0!r}, mandatory = True)".format(
                                                            self.feature_name)
        else:
            return "StreamFeatureNotHandled({0!r})".format(self.feature_name)
    def __str__(self):
        return self.feature_name

class StreamFeatureHandler:
    """Base class for stream feature handlers."""
    # pylint: disable-msg=W0232
    __metaclass__ = ABCMeta
    def handle_stream_features(self, stream, features):
        """Handle features announced by the stream peer.

        [initiator only]

        :Parameters:
            - `stream`: the stream
            - `features`: the features element just received
        :Types:
            - `stream`: `StreamBase`
            - `features`: `ElementTree.Element`

        :Return: 
            - `StreamFeatureHandled` instance if a feature was recognized and
              handled
            - `StreamFeatureNotHandled` instance if a feature was recognized
              but not handled
            - `None` if no feature was recognized
        """
        # pylint: disable-msg=W0613,R0201
        return False

    def make_stream_features(self, stream, features):
        """Update the features element announced by the stream.

        [receiver only]

        :Parameters:
            - `stream`: the stream
            - `features`: the features element about to be sent
        :Types:
            - `stream`: `StreamBase`
            - `features`: `ElementTree.Element`
        """
        # pylint: disable-msg=W0613,R0201
        return False

def stream_element_handler(element_name, usage_restriction = None):
    """Method decorator generator for decorating stream element
    handler methods in `StreamFeatureHandler` subclasses.
    
    :Parameters:
        - `element_name`: stream element QName
        - `usage_restriction`: optional usage restriction: "initiator" or
          "receiver"
    :Types:
        - `element_name`: `unicode`
        - `usage_restriction`: `unicode`
    """
    def decorator(func):
        """The decorator"""
        func._pyxmpp_stream_element_handled = element_name
        func._pyxmpp_usage_restriction = usage_restriction
        return func
    return decorator

class StreamBase(StanzaProcessor, XMLStreamHandler, EventHandler):
    """Base class for a generic XMPP stream.

    Responsible for establishing connection, parsing the stream, dispatching
    received stanzas to apopriate handlers and sending application's stanzas.
    This doesn't provide any authentication or encryption (both required by
    the XMPP specification) and is not usable on its own.

    Whenever we say "stream" here we actually mean two streams
    (incoming and outgoing) of one connections, as defined by the XMPP
    specification.

    :Ivariables:
        - `stanza_namespace`: default namespace of the stream
        - `settings`: stream settings
        - `lock`: RLock object used to synchronize access to Stream object.
        - `features`: stream features as annouced by the receiver.
        - `me`: local stream endpoint JID.
        - `peer`: remote stream endpoint JID.
        - `process_all_stanzas`: when `True` then all stanzas received are
          considered local.
        - `initiator`: `True` if local stream endpoint is the initiating entity.
        - `version`: Negotiated version of the XMPP protocol. (0,9) for the
          legacy (pre-XMPP) Jabber protocol.
        - `_input_state`: `None`, "open" (<stream:stream> has been received)
          "restart" or "closed" (</stream:stream> or EOF has been received)
        - `_output_state`: `None`, "open" (<stream:stream> has been received)
          "restart" or "closed" (</stream:stream> or EOF has been received)
    :Types:
        - `settings`: XMPPSettings
        - `version`: (`int`, `int`) tuple
    """
    # pylint: disable-msg=R0902,R0904
    def __init__(self, stanza_namespace, handlers, settings = None):
        """Initialize StreamBase object

        :Parameters:
          - `stanza_namespace`: stream's default namespace URI ("jabber:client"
            for client, "jabber:server" for server, etc.)
          - `handlers`: objects to handle the stream events and elements
          - `settings`: extra settings
        :Types:
          - `stanza_namespace`: `unicode`
          - `settings`: XMPPSettings
          - `handlers`: `list` of objects
        """
        XMLStreamHandler.__init__(self)
        if settings is None:
            settings = XMPPSettings()
        self.settings = settings
        StanzaProcessor.__init__(self, settings[u"default_stanza_timeout"])
        self.stanza_namespace = stanza_namespace
        self._stanza_namespace_p = "{{{0}}}".format(stanza_namespace)
        self.process_all_stanzas = False
        self.port = None
        self.handlers = handlers
        self._stream_feature_handlers = []
        for handler in handlers:
            if isinstance(handler, StreamFeatureHandler):
                self._stream_feature_handlers.append(handler)
        self.addr = None
        self.me = None
        self.peer = None
        self.stream_id = None
        self.eof = False
        self.initiator = None
        self.features = None
        self.authenticated = False
        self.peer_authenticated = False
        self.tls_established = False
        self.auth_method_used = None
        self.version = None
        self.language = None
        self.peer_language = None
        self.transport = None
        self._input_state = None
        self._output_state = None

    def initiate(self, transport, to = None):
        """Initiate an XMPP connection over the `transport`.
        
        :Parameters:
            - `transport`: an XMPP transport instance
            - `to`: peer name
        """
        with self.lock:
            self.initiator = True
            self.transport = transport
            transport.set_target(self)
            if to:
                self.peer = JID(to)
            else:
                self.peer = None
            if transport.is_connected():
                self._initiate()

    def _initiate(self):
        """Initiate an XMPP connection over a connected `self.transport`.

        [ called with `self.lock` acquired ]
        """
        self.eof = False
        self._setup_stream_element_handlers()
        self.setup_stanza_handlers(self.handlers, "pre-auth")
        self._send_stream_start()

    def receive(self, transport, myname):
        """Receive an XMPP connection over the `transport`.

        :Parameters:
            - `transport`: an XMPP transport instance
            - `myname`: local stream endpoint name.
        """
        with self.lock:
            self.transport = transport
            transport.set_target(self)
            self.me = JID(myname)
            self.initiator = False
            self._setup_stream_element_handlers()
            self.setup_stanza_handlers(self.handlers, "pre-auth")

    def _setup_stream_element_handlers(self):
        """Set up stream element handlers.
        
        Scans the `self.handlers` list for `StreamFeatureHandler`
        instances and updates `self._element_handlers` mapping with their
        methods decorated with `@stream_element_handler`"""
        # pylint: disable-msg=W0212
        if self.initiator:
            mode = "initiator"
        else:
            mode = "receiver"
        self._element_handlers = {}
        for handler in self.handlers:
            if not isinstance(handler, StreamFeatureHandler):
                continue
            for _unused, meth in inspect.getmembers(handler, callable):
                if not hasattr(meth, "_pyxmpp_stream_element_handled"):
                    continue
                element_handled = meth._pyxmpp_stream_element_handled
                if element_handled in self._element_handlers:
                    # use only the first matching handler
                    continue
                if meth._pyxmpp_usage_restriction in (None, mode):
                    self._element_handlers[element_handled] = meth

    def disconnect(self):
        """Gracefully close the connection."""
        with self.lock:
            self.transport.disconnect()
            self._output_state = "closed"

    def event(self, event): # pylint: disable-msg=R0201
        """Handle a stream event.
        
        Called when connection state is changed.

        Should not be called with self.lock acquired!
        """
        event.stream = self
        logger.debug(u"Stream event: {0}".format(event))
        self.settings["event_queue"].put(event)
        return False

    def transport_connected(self):
        with self.lock:
            if self.initiator:
                if self._output_state is None:
                    self._initiate()

    def close(self):
        """Forcibly close the connection and clear the stream state."""
        self.transport.close()

    def stream_start(self, element):
        """Process <stream:stream> (stream start) tag received from peer.
        
        `self.lock` is acquired when this method is called.

        :Parameters:
            - `element`: root element (empty) created by the parser"""
        with self.lock:
            logger.debug("input document: " + ElementTree.tostring(element))
            if not element.tag.startswith(STREAM_QNP):
                self._send_stream_error("invalid-namespace")
                raise FatalStreamError("Bad stream namespace")
            if element.tag != STREAM_ROOT_TAG:
                self._send_stream_error("bad-format")
                raise FatalStreamError("Bad root element")

            self._input_state = "open"
            version = element.get("version")
            if version:
                try:
                    major, minor = version.split(".", 1)
                    major, minor = int(major), int(minor)
                except ValueError:
                    self._send_stream_error("unsupported-version")
                    raise FatalStreamError("Unsupported protocol version.")
                self.version = (major, minor)
            else:
                self.version = (0, 9)

            if self.version[0] != 1 and self.version != (0, 9):
                self._send_stream_error("unsupported-version")
                raise FatalStreamError("Unsupported protocol version.")

            peer_lang = element.get(XML_LANG_QNAME)
            self.peer_language = peer_lang
            if not self.initiator:
                lang = None
                languages = self.settings["languages"]
                while peer_lang:
                    if peer_lang in languages:
                        lang = peer_lang
                        break
                    match = LANG_SPLIT_RE.match(peer_lang)
                    if not match:
                        break
                    peer_lang = match.group(0)
                if lang:
                    self.language = lang

            if self.initiator:
                self.stream_id = element.get("id")
                peer = element.get("from")
                if peer:
                    peer = JID(peer)
                if self.peer:
                    if peer and peer != self.peer:
                        logger.debug("peer hostname mismatch: {0!r} != {1!r}"
                                                        .format(peer, self.peer))
                self.peer = peer
            else:
                to = element.get("to")
                if to:
                    to = self.check_to(to)
                    if not to:
                        self._send_stream_error("host-unknown")
                        raise FatalStreamError('Bad "to"')
                    self.me = JID(to)
                peer = element.get("from")
                if peer:
                    peer = JID(peer)
                self._send_stream_start(self.generate_id(), stream_to = peer)
                self._send_stream_features()

            if self._input_state == "restart":
                event = StreamRestartedEvent(self.peer)
            else:
                event = StreamConnectedEvent(self.peer)
        self.event(event)

    def stream_end(self):
        """Process </stream:stream> (stream end) tag received from peer.
        """
        logger.debug("Stream ended")
        with self.lock:
            self._input_state = "closed"
            self.transport.disconnect()
            self._output_state = "closed"

    def stream_eof(self):
        """Process stream EOF.
        """
        self.stream_end()

    def stream_element(self, element):
        """Process first level child element of the stream).

        :Parameters:
            - `element`: stanza's full XML
        """
        with self.lock:
            self._process_element(element)

    def stream_parse_error(self, descr):
        """Called when an error is encountered in the stream.

        :Parameters:
            - `descr`: description of the error
        :Types:
            - `descr`: `unicode`"""
        self.send_stream_error("not-well-formed")
        raise StreamParseError(descr)
 
    def _send_stream_start(self, stream_id = None, stream_to = None):
        """Send stream start tag."""
        if self._output_state in ("open", "closed"):
            raise StreamError("Stream start already sent")
        if not self.language:
            self.language = self.settings["language"]
        if stream_to:
            stream_to = unicode(stream_to)
        elif self.peer and self.initiator:
            stream_to = unicode(self.peer)
        stream_from = None
        if self.me and (self.tls_established or not self.initiator):
            stream_from = unicode(self.me)
        if stream_id:
            self.stream_id = stream_id
        else:
            self.stream_id = None
        self.transport.send_stream_head(self.stanza_namespace, 
                                        stream_from, stream_to,
                                    self.stream_id, language = self.language)
        self._output_state = "open"

    def send_stream_error(self, condition):
        """Send stream error element.

        :Parameters:
            - `condition`: stream error condition name, as defined in the
              XMPP specification.
        """
        with self.lock:
            self._send_stream_error(condition)

    def _send_stream_error(self, condition):
        """Same as `send_stream_error`, but expects `self.lock` acquired.
        """
        if self._output_state is "closed":
            return
        if self._output_state in (None, "restart"):
            self._send_stream_start()
        element = StreamErrorElement(condition).as_xml()
        self.transport.send_element(element)
        self.transport.disconnect()
        self._output_state = "closed"

    def _restart_stream(self):
        """Restart the stream as needed after SASL and StartTLS negotiation."""
        self._input_state = "restart"
        self._output_state = "restart"
        self.features = None
        if self.initiator:
            self._send_stream_start(self.stream_id)

    def _make_stream_features(self):
        """Create the <features/> element for the stream.

        [receving entity only]

        :returns: new <features/> element
        :returntype: `ElementTree.Element`"""
        features = ElementTree.Element(FEATURES_TAG)
        for handler in self._stream_feature_handlers:
            handler.make_stream_features(self, features)
        return features

    def _send_stream_features(self):
        """Send stream <features/>.

        [receiving entity only]"""
        self.features = self._make_stream_features()
        self._write_element(self.features)

    def write_element(self, element):
        """Write XML `element` to the stream.

        :Parameters:
            - `element`: Element node to send.
        :Types:
            - `element`: `ElementTree.Element`
        """
        with self.lock:
            self._write_element(element)

    def _write_element(self, element):
        """Same as `write_element` but with `self.lock` already acquired.
        """
        self.transport.send_element(element)

    def send(self, stanza):
        """Write stanza to the stream.

        :Parameters:
            - `stanza`: XMPP stanza to send.
        :Types:
            - `stanza`: `pyxmpp2.stanza.Stanza`
        """
        with self.lock:
            return self._send(stanza)

    def _send(self, stanza):
        """Same as `Stream.send` but assume `self.lock` is acquired."""
        self.fix_out_stanza(stanza)
        element = stanza.as_xml()
        self._write_element(element)

    def regular_tasks(self):
        """Do some housekeeping (cache expiration, timeout handling).

        This method should be called periodically from the application's
        main loop.
        
        :Return: suggested delay (in seconds) before the next call to this
                                                                    method.
        :Returntype: `int`
        """
        with self.lock:
            return self._regular_tasks()

    def _regular_tasks(self):
        """Same as `Stream.regular_tasks` but assume `self.lock` is acquired."""
        self._iq_response_handlers.expire()
        return 60

    def _process_element(self, element):
        """Process first level element of the stream.

        The element may be stream error or features, StartTLS
        request/response, SASL request/response or a stanza.

        :Parameters:
            - `element`: XML element
        :Types:
            - `element`: `ElementTree.Element`
        """
        tag = element.tag
        if tag in self._element_handlers:
            handler = self._element_handlers[tag]
            logger.debug("Passing element {0!r} to method {1!r}"
                                                .format(element, handler))
            handled = handler(self, element)
            if handled:
                return
        if tag.startswith(self._stanza_namespace_p):
            stanza = stanza_factory(element, self, self.language)
            self.process_stanza(stanza)
        elif tag == ERROR_TAG:
            error = StreamErrorElement(element)
            self.process_stream_error(error)
        elif tag == FEATURES_TAG:
            logger.debug("Got features element: {0}".format(serialize(element)))
            self._got_features(element)
        else:
            logger.debug("Unhandled element: {0}".format(serialize(element)))
            logger.debug(" known handlers: {0!r}".format(
                                                    self._element_handlers))

    def process_stream_error(self, error):
        """Process stream error element received.

        :Types:
            - `error`: `StreamErrorNode`

        :Parameters:
            - `error`: error received
        """
        # pylint: disable-msg=R0201
        logger.debug("Unhandled stream error: condition: {0} {1!r}"
                            .format(error.condition_name, error.serialize()))

    def check_to(self, to):
        """Check "to" attribute of received stream header.

        :return: `to` if it is equal to `self.me`, None otherwise.

        Should be overriden in derived classes which require other logic
        for handling that attribute."""
        if to != self.me:
            return None
        return to

    def generate_id(self):
        """Generate a random and unique stream ID.

        :return: the id string generated."""
        # pylint: disable-msg=R0201
        return unicode(uuid.uuid4())

    def _got_features(self, features):
        """Process incoming <stream:features/> element.

        [initiating entity only]

        The received features node is available in `self.features`."""
        self.features = features
        logger.debug("got features, passing to event handlers...")
        handled = self.event(GotFeaturesEvent(self.features))
        logger.debug("  handled: {0}".format(handled))
        if not handled:
            mandatory_handled = []
            mandatory_not_handled = []
            logger.debug("  passing to stream features handlers: {0}"
                                    .format(self._stream_feature_handlers))
            for handler in self._stream_feature_handlers:
                ret = handler.handle_stream_features(self, self.features)
                if ret is None:
                    continue
                elif isinstance(ret, StreamFeatureHandled):
                    if ret.mandatory:
                        mandatory_handled.append(unicode(ret))
                        break
                    break
                elif isinstance(ret, StreamFeatureNotHandled):
                    if ret.mandatory:
                        mandatory_not_handled.append(unicode(ret))
                        break
                else:
                    raise ValueError("Wrong value returned from a stream"
                            " feature handler: {0!r}".format(ret))
            if mandatory_not_handled and not mandatory_handled:
                self.send_stream_error("unsupported-feature")
                raise FatalStreamError(
                        u"Unsupported mandatory-to-implement features: "
                                        + u" ".join(mandatory_not_handled))

    def is_connected(self):
        """Check if stream is is_connected and stanzas may be sent.

        :return: True if stream connection is active."""
        return self.transport.is_connected() and self._output_state == "open"

    def set_peer_authenticated(self, peer, restart_stream = False):
        """Mark the other side of the stream authenticated as `peer`

        :Parameters:
            - `peer`: local JID just authenticated
            - `restart_stream`: `True` when stream should be restarted
                (needed after SASL authentication)
        :Types:
            - `peer`: `JID`
            - `restart_stream`: `bool`
        """
        with self.lock:
            self.peer_authenticated = True
            self.peer = peer
            if restart_stream:
                self._restart_stream()
        self.setup_stanza_handlers(self.handlers, "post-auth")
        self.event(AuthenticatedEvent(self.peer))

    def set_authenticated(self, me, restart_stream = False):
        """Mark stream authenticated as `me`

        :Parameters:
            - `me`: local JID just authenticated
            - `restart_stream`: `True` when stream should be restarted
                (needed after SASL authentication)
        :Types:
            - `me`: `JID`
            - `restart_stream`: `bool`
        """
        with self.lock:
            self.authenticated = True
            self.me = me
            if restart_stream:
                self._restart_stream()
        self.setup_stanza_handlers(self.handlers, "post-auth")
        self.event(AuthenticatedEvent(self.me))

# vi: sts=4 et sw=4
