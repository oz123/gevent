# Copyright (c) 2011-2015 Denis Bilenko. See LICENSE for details.
"""
c-ares based hostname resolver.
"""
from __future__ import absolute_import, print_function, division
import os

from _socket import getaddrinfo as native_getaddrinfo
from _socket import gethostbyname_ex as native_gethostbyname_ex
from _socket import gaierror
from _socket import herror
from _socket import error
from _socket import EAI_NONAME

from gevent._compat import text_type
from gevent._compat import integer_types
from gevent._compat import PY3
from gevent._compat import MAC

from gevent.hub import Waiter
from gevent.hub import get_hub

from gevent.socket import AF_UNSPEC
from gevent.socket import AF_INET
from gevent.socket import AF_INET6
from gevent.socket import SOCK_DGRAM
from gevent.socket import SOCK_STREAM
from gevent.socket import SOL_TCP
from gevent.socket import SOL_UDP
from gevent.socket import AI_NUMERICHOST

from gevent._config import config
from gevent._config import AresSettingMixin

from .cares import channel, InvalidIP # pylint:disable=import-error,no-name-in-module
from . import _lookup_port as lookup_port
from . import _resolve_special
from . import AbstractResolver

__all__ = ['Resolver']


class Resolver(AbstractResolver):
    """
    Implementation of the resolver API using the `c-ares`_ library.

    This implementation uses the c-ares library to handle name
    resolution. c-ares is natively asynchronous at the socket level
    and so integrates well into gevent's event loop.

    In comparison to :class:`gevent.resolver_thread.Resolver` (which
    delegates to the native system resolver), the implementation is
    much more complex. In addition, there have been reports of it not
    properly honoring certain system configurations (for example, the
    order in which IPv4 and IPv6 results are returned may not match
    the threaded resolver). However, because it does not use threads,
    it may scale better for applications that make many lookups.

    There are some known differences from the system resolver.

    - ``gethostbyname_ex`` and ``gethostbyaddr`` may return
      different for the ``aliaslist`` tuple member. (Sometimes the
      same, sometimes in a different order, sometimes a different
      alias altogether.)

    - ``gethostbyname_ex`` may return the ``ipaddrlist`` in a
      different order.

    - ``getaddrinfo`` does not return ``SOCK_RAW`` results.

    - ``getaddrinfo`` may return results in a different order.

    - Handling of ``.local`` (mDNS) names may be different, even
      if they are listed in the hosts file.

    - c-ares will not resolve ``broadcasthost``, even if listed in
      the hosts file.

    - This implementation may raise ``gaierror(4)`` where the
      system implementation would raise ``herror(1)`` or vice versa,
      with different error numbers. However, after 2020-04, this should be
      much reduced.

    - The results for ``localhost`` may be different. In
      particular, some system resolvers will return more results
      from ``getaddrinfo`` than c-ares does, such as SOCK_DGRAM
      results, and c-ares may report more ips on a multi-homed
      host.

    - The system implementation may return some names fully qualified, where
      this implementation returns only the host name. This appears to be
      the case only with entries found in ``/etc/hosts``.

    - c-ares supports a limited set of flags for ``getnameinfo`` and
      ``getaddrinfo``; unknown flags are ignored. System-specific flags
      such as ``AI_V4MAPPED_CFG`` are not supported.

    - ``getaddrinfo`` may return canonical names even without the ``AI_CANONNAME``
      being set.

    .. caution::

        This module is considered extremely experimental on PyPy, and
        due to its implementation in cython, it may be slower. It may also lead to
        interpreter crashes.

    .. versionchanged:: 1.5.0
       This version of gevent typically embeds c-ares 1.15.0 or newer. In
       that version of c-ares, domains ending in ``.onion`` `are never
       resolved <https://github.com/c-ares/c-ares/issues/196>`_ or even
       sent to the DNS server.

    .. versionchanged:: NEXT
       ``getaddrinfo`` is now implemented using the native c-ares function
       from c-ares 1.16 or newer.

    .. versionchanged:: NEXT
        Now ``herror`` and ``gaierror`` are raised more consistently with
        the standard library resolver, and have more consistent errno values.

    .. _c-ares: http://c-ares.haxx.se
    """

    cares_class = channel

    def __init__(self, hub=None, use_environ=True, **kwargs):
        if hub is None:
            hub = get_hub()
        self.hub = hub
        if use_environ:
            for setting in config.settings.values():
                if isinstance(setting, AresSettingMixin):
                    value = setting.get()
                    if value is not None:
                        kwargs.setdefault(setting.kwarg_name, value)
        self.cares = self.cares_class(hub.loop, **kwargs)
        self.pid = os.getpid()
        self.params = kwargs
        self.fork_watcher = hub.loop.fork(ref=False)
        self.fork_watcher.start(self._on_fork)

    def __repr__(self):
        return '<gevent.resolver_ares.Resolver at 0x%x ares=%r>' % (id(self), self.cares)

    def _on_fork(self):
        # NOTE: See comment in gevent.hub.reinit.
        pid = os.getpid()
        if pid != self.pid:
            self.hub.loop.run_callback(self.cares.destroy)
            self.cares = self.cares_class(self.hub.loop, **self.params)
            self.pid = pid

    def close(self):
        if self.cares is not None:
            self.hub.loop.run_callback(self.cares.destroy)
            self.cares = None
        self.fork_watcher.stop()

    def gethostbyname(self, hostname, family=AF_INET):
        hostname = _resolve_special(hostname, family)
        return self.gethostbyname_ex(hostname, family)[-1][0]

    HOSTNAME_ENCODING = 'idna' if PY3 else 'ascii'
    _LOCAL_HOSTNAMES = (
        b'localhost',
        b'ip6-localhost',
    )

    _LOCAL_AND_BROADCAST_HOSTNAMES = _LOCAL_HOSTNAMES + (
        b'255.255.255.255',
        b'<broadcast>',
    )

    EAI_NONAME_MSG = (
        'nodename nor servname provided, or not known'
        if MAC else
        'Name or service not known'
    )

    def _hostname_to_bytes(self, hostname):
        if isinstance(hostname, text_type):
            hostname = hostname.encode(self.HOSTNAME_ENCODING)
        elif not isinstance(hostname, (bytes, bytearray)):
            raise TypeError('Expected str, bytes or bytearray, not %s' % type(hostname).__name__)

        return bytes(hostname)

    def gethostbyname_ex(self, hostname, family=AF_INET):
        hostname = self._hostname_to_bytes(hostname)

        if hostname in self._LOCAL_AND_BROADCAST_HOSTNAMES:
            return native_gethostbyname_ex(hostname)

        while True:
            ares = self.cares
            try:
                waiter = Waiter(self.hub)
                ares.gethostbyname(waiter, hostname, family)
                result = waiter.get()
                if not result[-1]:
                    raise herror(EAI_NONAME, self.EAI_NONAME_MSG)
                return result
            except herror as ex:
                if ares is self.cares:
                    if ex.args[0] == 1:
                        # Somewhere along the line, the internal
                        # implementation of gethostbyname_ex changed to invoke
                        # getaddrinfo() as a first pass, much like we do for ``getnameinfo()``;
                        # this means it raises a different error for not-found hosts.
                        raise gaierror(EAI_NONAME, self.EAI_NONAME_MSG)
                    raise
                # "self.cares is not ares" means channel was destroyed (because we were forked)

    def _lookup_port(self, port, socktype):
        return lookup_port(port, socktype)

    def _getaddrinfo(
            self, host, port,
            family=0, socktype=0, proto=0, flags=0,
            fill_in_type_proto=True
    ):
        """
        Returns a list ``(family, socktype, proto, canonname, sockaddr)``

        :raises gaierror: If no results are found.
        """
        # pylint:disable=too-many-locals,too-many-branches
        if isinstance(host, text_type):
            host = host.encode('idna')

        if not isinstance(host, bytes) or (flags & AI_NUMERICHOST) or host in self._LOCAL_HOSTNAMES:
            # XXX: Now that we're using ares_getaddrinfo, how much of this is still
            # necessary?
            # this handles cases which do not require network access
            # 1) host is None
            # 2) host is of an invalid type
            # 3) AI_NUMERICHOST flag is set
            # 4) It's a well-known alias. TODO: This is special casing that we don't
            #    really want to do. It's here because it resolves a discrepancy with the system
            #    resolvers caught by test cases. In gevent 20.4.0, this only worked correctly on
            #    Python 3 and not Python 2, by accident.
            return native_getaddrinfo(host, port, family, socktype, proto, flags)


        if isinstance(port, text_type):
            port = port.encode('ascii')
        elif isinstance(port, integer_types):
            if port == 0:
                port = None
            else:
                port = str(port).encode('ascii')

        waiter = Waiter(self.hub)
        self.cares.getaddrinfo(
            waiter,
            host,
            port,
            family,
            socktype,
            proto,
            flags,
        )
        # Result is a list of:
        # (family, socktype, proto, canonname, sockaddr)
        # Where sockaddr depends on family; for INET it is
        # (address, port)
        # and INET6 is
        # (address, port, flow info, scope id)
        result = waiter.get()

        if not result:
            raise gaierror(EAI_NONAME, self.EAI_NONAME_MSG)

        if fill_in_type_proto:
            # c-ares 1.16 DOES NOT fill in socktype or proto in the results,
            # ever. It's at least supposed to do that if they were given as
            # hints, but it doesn't (https://github.com/c-ares/c-ares/issues/317)
            # Sigh.
            # The SOL_* constants are another (older?) name for IPPROTO_*
            if socktype:
                hard_type_proto = [
                    (socktype, SOL_TCP if socktype == SOCK_STREAM else SOL_UDP),
                ]
            elif proto:
                hard_type_proto = [
                    (SOCK_STREAM if proto == SOL_TCP else SOCK_DGRAM, proto),
                ]
            else:
                hard_type_proto = [
                    (SOCK_STREAM, SOL_TCP),
                    (SOCK_DGRAM, SOL_UDP),
                ]

            result = [
                (rfamily,
                 hard_type if not rtype else rtype,
                 hard_proto if not rproto else rproto,
                 rcanon,
                 raddr)
                for rfamily, rtype, rproto, rcanon, raddr
                in result
                for hard_type, hard_proto
                in hard_type_proto
            ]
        return result

    def getaddrinfo(self, host, port, family=0, socktype=0, proto=0, flags=0):
        while True:
            ares = self.cares
            try:
                return self._getaddrinfo(host, port, family, socktype, proto, flags)
            except gaierror:
                if ares is self.cares:
                    raise

    def _gethostbyaddr(self, ip_address):
        ip_address = self._hostname_to_bytes(ip_address)

        waiter = Waiter(self.hub)
        try:
            self.cares.gethostbyaddr(waiter, ip_address)
            return waiter.get()
        except InvalidIP:
            result = self._getaddrinfo(ip_address, None, family=AF_UNSPEC, socktype=SOCK_DGRAM)
            if not result:
                raise
            _ip_address = result[0][-1][0]
            if isinstance(_ip_address, text_type):
                _ip_address = _ip_address.encode('ascii')
            if _ip_address == ip_address:
                raise
            waiter.clear()
            self.cares.gethostbyaddr(waiter, _ip_address)
            return waiter.get()

    def gethostbyaddr(self, ip_address):
        ip_address = _resolve_special(ip_address, AF_UNSPEC)
        while True:
            ares = self.cares
            try:
                return self._gethostbyaddr(ip_address)
            except herror:
                if ares is self.cares:
                    raise

    def _getnameinfo(self, hostname, port, sockaddr, flags):

        result = self._getaddrinfo(hostname, port,
                                   family=AF_UNSPEC, socktype=SOCK_DGRAM, fill_in_type_proto=False)
        if len(result) != 1:
            raise error('sockaddr resolved to multiple addresses')

        family, _socktype, _proto, _name, address = result[0]

        if family == AF_INET:
            if len(sockaddr) != 2:
                raise error("IPv4 sockaddr must be 2 tuple")
        elif family == AF_INET6:
            address = address[:2] + sockaddr[2:]

        waiter = Waiter(self.hub)
        self.cares.getnameinfo(waiter, address, flags)
        node, service = waiter.get()

        if service is None and PY3:
            # ares docs: "If the query did not complete
            # successfully, or one of the values was not
            # requested, node or service will be NULL ". Python 2
            # allows that for the service, but Python 3 raises
            # an error. This is tested by test_socket in py 3.4
            err = gaierror(EAI_NONAME, self.EAI_NONAME_MSG)
            err.errno = EAI_NONAME
            raise err

        return node, service or '0'

    def getnameinfo(self, sockaddr, flags):
        if not isinstance(flags, integer_types):
            raise TypeError('an integer is required')
        if not isinstance(sockaddr, tuple):
            raise TypeError('getnameinfo() argument 1 must be a tuple')

        address = sockaddr[0]
        address = self._hostname_to_bytes(sockaddr[0])

        port = sockaddr[1]
        if not isinstance(port, integer_types):
            raise TypeError('port must be an integer, not %s' % type(port))

        if port >= 65536:
            # System resolvers do different things with an
            # out-of-bound port; macOS CPython 3.8 raises ``gaierror: [Errno 8]
            # nodename nor servname provided, or not known``, while
            # manylinux CPython 2.7 appears to ignore it and raises ``error:
            # sockaddr resolved to multiple addresses``. TravisCI, at least ot
            # one point, successfully resolved www.gevent.org to ``(readthedocs.org, '0')``.
            # But c-ares 1.16 would raise ``gaierror(25, 'ARES_ESERVICE: unknown')``.
            # Doing this appears to get the expected results.
            port = 0

        if len(sockaddr) > 2:
            # Must be IPv6: (host, port, [flowinfo, [scopeid]])
            flowinfo = sockaddr[2]
            if flowinfo > 0xfffff:
                raise OverflowError("getnameinfo(): flowinfo must be 0-1048575.")

        while True:
            ares = self.cares
            try:
                return self._getnameinfo(address, port, sockaddr, flags)
            except gaierror:
                if ares is self.cares:
                    raise
