#
# (C) Copyright 2005 Jacek Konieczny <jajcus@jajcus.net>
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

"""Caching proxy for Jabber/XMPP objects.

This package provides facilities to retrieve and transparently cache
cachable objects like Service Discovery responses or e.g. client version
informations."""

__revision__ = "$Id$"
__docformat__ = "restructuredtext en"

import threading
import logging
from datetime import datetime, timedelta

_state_values = {
        'new': 0,
        'fresh': 1,
        'old': 2,
        'stale': 3,
        'purged': 3
    };

# locking order (anti-deadlock):
# CacheSuite, Cache, CacheHandler, CacheItem

class CacheItem(object):
    """An item in a cache.

    :Ivariables:
        - `value`: item value (cached object).
        - `address`: item address.
        - `state`: current state.
        - `value`: numerical value of the current state (lower number means
          fresher item).
        - `timestamp`: time when the object was created.
        - `freshness_time`: time when the object stops being fresh.
        - `expire_time`: time when the object expires.
        - `purge_time`: time when the object should be purged. When 0 then
          item will never be automaticaly purged.
        - `_lock`: lock for thread safety.
    :Types:
        - `value`: `instance`
        - `address`: any hashable
        - `state`: `str`
        - `state_value`: `int`
        - `timestamp`: `datetime`
        - `freshness_time`: `datetime`
        - `expire_time`: `datetime`
        - `purge_time`: `datetime`
        - `_lock`: `threading.RLock`"""
    __slots__ = ['value', 'address', 'state', 'timestamp', 'freshness_time',
            'expire_time', 'purge_time', 'state_value', '_lock']
    def __init__(self, address, value, freshness_period, expiration_period,
            purge_period, state = "new"):
        """Initialize an CacheItem object.

        :Ivariables:
            - `address`: item address.
            - `value`: item value (cached object).
            - `freshness_period`: time interval after which the object stops being fresh.
            - `expiration_period`: time interval after which the object expires.
            - `purge_period`: time interval after which the object should be purged. When 0 then
              item will never be automaticaly purged.
            - `state`: initial state.
        :Types:
            - `address`: any hashable
            - `value`: `instance`
            - `freshness_time`: `timedelta`
            - `expire_time`: `timedelta`
            - `purge_time`: `timedelta`
            - `state`: `str`"""
        if freshness_period>expiration_period:
            return ValueError, "freshness_period greater then expiration_period"
        if expiration_period>purge_period:
            return ValueError, "expiration_period greater then purge_period"
        self.address = address
        self.value = value
        now = datetime.utcnow()
        self.timestamp = now
        self.freshness_time = now+freshness_period
        self.expire_time = now+expiration_period
        if purge_period:
            self.purge_time = now+purge_period
        else:
            self.purge_time = datetime.max
        self.state = state
        self.state_value = _state_values[state]
        self._lock = threading.RLock()

    def update_state(self):
        """Update current status of the item and compute time of the next
        state change.

        :return: the new state.
        :returntype: `datetime`"""
        self._lock.acquire()
        try:
            now = datetime.utcnow()
            if self.state == 'new':
                self.state = 'fresh'
            if self.state == 'fresh':
                if now > self.freshness_time:
                    self.state = 'old'
            if self.state == 'old':
                if now > self.expire_time:
                    self.state = 'stale'
            if self.state == 'stale':
                if now > self.purge_time:
                    self.state = 'purged'
            self.state_value = _state_values[self.state]
            return self.state
        finally:
            self._lock.release()

    def __cmp__(self,other):
        try:
            return cmp(
                    (-self.state_value, self.timestamp, id(self)),
                    (-other.state_value, other.timestamp, id(other))
                )
        except AttributeError:
            return cmp(id(self),id(other))

_hour = timedelta(hours = 1)

class CacheFetcher:
    """Base class for cache object fetchers -- classes responsible for
    retrieving objects from network.

    An instance of a fetcher class is created for each object requested and 
    not found in the cache, then `fetch` method is called to initialize
    the asynchronous retrieval process. Fetcher object's `got_it` method
    should be called on a successfull retrieval and `error` otherwise.
    `timeout` will be called when the request timeouts.

    :Ivariables:
        - `cache`: cache object which created this fetcher.
        - `address`: requested item address.
        - `timeout_time`: timeout time.
        - `active`: `True` as long as the fetcher is active and requestor
          expects one of the handlers to be called.
    :Types:
        - `cache`: `Cache`
        - `address`: any hashable
        - `timeout_time`: `datetime`
        - `active`: `bool`
    """
    def __init__(self, cache, address,
            item_freshness_period, item_expiration_period, item_purge_period,
            object_handler, error_handler, timeout_handler, timeout_period,
            backup_state = None):
        """Initialize an `CacheFetcher` object.

        :Parameters:
            - `cache`: cache object which created this fetcher.
            - `address`: requested item address.
            - `item_freshness_period`: freshness period for the requested item.
            - `item_expiration_period`: expiration period for the requested item.
            - `item_purge_period`: purge period for the requested item.
            - `object_handler`: function to be called after the item is fetched.
            - `error_handler`: function to be called on error.
            - `timeout_handler`: function to be called on timeout
            - `timeout_period`: timeout interval.
            - `backup_state`: when not `None` and the fetch fails than an
              object from cache of at least this state will be passed to the
              `object_handler`. If such object is not available, then
              `error_handler` is called.
        :Types:
            - `cache`: `Cache`
            - `address`: any hashable
            - `item_freshness_period`: `timedelta`
            - `item_expiration_period`: `timedelta`
            - `item_purge_period`: `timedelta`
            - `object_handler`: callable(address, value, state)
            - `error_handler`: callable(address, error_data)
            - `timeout_handler`: callable(address)
            - `timeout_period`: `timedelta`
            - `backup_state`: `bool`"""
        self.cache = cache
        self.address = address
        self._item_freshness_period = item_freshness_period
        self._item_expiration_period = item_expiration_period
        self._item_purge_period = item_purge_period
        self._object_handler = object_handler
        self._error_handler = error_handler
        self._timeout_handler = timeout_handler
        if timeout_period:
            self.timeout_time = datetime.utcnow()+timeout_period
        else:
            self.timeout_time = datetime.max
        self._backup_state = backup_state
        self.active = True

    def _deactivate(self):
        """Remove the fetcher from cache and mark it not active."""
        self.cache.remove_fetcher(self)
        if self.active:
            self._deactivated()
    
    def _deactivated(self):
        """Mark the fetcher inactive after it is removed from the cache."""
        self.active = False
        
    def fetch(self):
        """Start the retrieval process.

        This method must be implemented in any fetcher class."""
        raise RuntimeError, "Pure virtual method called"

    def got_it(self, value, state = "new"):
        """Handle a successfull retrieval and call apriopriate handler.

        Should be called when retrieval succeeds.

        Do nothing when the fetcher is not active any more (after
        one of handlers was already called).

        :Parameters:
            - `value`: fetched object.
            - `state`: initial state of the object.
        :Types:
            - `value`: any
            - `state`: `str`"""
        if not self.active:
            return
        item = CacheItem(self.address, value, self._item_freshness_period,
                self._item_expiration_period, self._item_purge_period, state)
        self._object_handler(item.address, item.value, item.state)
        self.cache.add_item(item)
        self._deactivate()

    def error(self, error_data):
        """Handle a retrieval error and call apriopriate handler.

        Should be called when retrieval fails.

        Do nothing when the fetcher is not active any more (after
        one of handlers was already called).

        :Parameters:
            - `error_data`: additional information about the error (e.g. `StanzaError` instance).
        :Types:
            - `error_data`: fetcher dependant
        """
        if not self.active:
            return
        if not self._try_backup_item():
            self._error_handler(self.address, error_data)
        self.cache.invalidate_object(self.address)
        self._deactivate()

    def timeout(self):
        """Handle fetcher timeout and call apriopriate handler.

        Is called by the cache object and should _not_ be called by fetcher or
        application.

        Do nothing when the fetcher is not active any more (after
        one of handlers was already called)."""
        if not self.active:
            return
        if not self._try_backup_item():
            if self._timeout_handler:
                self._timeout_handler(self.address)
            else:
                self._error_handler(self.address, None)
        self.cache.invalidate_object(self.address)
        self._deactivate()

    def _try_backup_item(self):
        """Check if a backup item is available in cache and call
        the item handler if it is.
        
        :return: `True` if backup item was found.
        :returntype: `bool`"""
        if not self._backup_state:
            return False
        item = self.cache.get_item(self.address, self._backup_state)
        if item:
            self._object_handler(item.address, item.value, item.state)
            return True
        else:
            False

class Cache:
    """Caching proxy for object retrieval and caching.

    Object factories ("fetchers") are registered in the `Cache` object and used
    to e.g. retrieve requested objects from network.  They are called only when
    the requested object is not in the cache or is not fresh enough.

    A state (freshness level) name may be provided when requesting an object.
    When the cached item state is "less fresh" then requested, then new object
    will be retrieved.

    Following states are defined:

      - 'new': always a new object should be retrieved.
      - 'fresh': a fresh object (not older than freshness time)
      - 'old': object not fresh, but most probably still valid.
      - 'stale': object known to be expired.

    :Ivariables:
        - `default_freshness_period`: default freshness period (in seconds).
        - `default_expiration_period`: default expiration period (in seconds).
        - `default_purge_period`: default purge period (in seconds). When
          0 then items are never purged because of their age.
        - `max_items`: maximum number of items to store.
        - `_items`: dictionary of stored items.
        - `_items_list`: list of stored items with the most suitable for 
          purging first.
        - `_fetchers`: dictionary of registered object fetchers.
        - `_active_fetchers`: list of active fetchers sorted by the time of
          its expiration time.
        - `_lock`: lock for thread safety.
    :Types:
        - `default_freshness_period`: timedelta
        - `default_expiration_period`: timedelta
        - `default_purge_period`: timedelta
        - `max_items`: `int`
        - `_items`: `dict` of (`classobj`, addr) -> `CacheItem`
        - `_items_list`: `list` of (`int`, `timestamp`, `CacheItem`)
        - `_fetchers`: `dict` of `classobj` -> `CacheFetcher` based class
        - `_active_fetchers`: `list` of (`int`, `CacheFetcher`)
        - `_lock`: `threading.RLock`
    """
    def __init__(self, max_items, default_freshness_period = _hour,
            default_expiration_period = 12*_hour, default_purge_period = 24*_hour):
        """Initialize a `Cache` object.
        
            :Parameters:
                - `default_freshness_period`: default freshness period (in seconds).
                - `default_expiration_period`: default expiration period (in seconds).
                - `default_purge_period`: default purge period (in seconds). When
                  0 then items are never purged because of their age.
                - `max_items`: maximum number of items to store.
            :Types:
                - `default_freshness_period`: number
                - `default_expiration_period`: number
                - `default_purge_period`: number
                - `max_items`: number
        """
        self.default_freshness_period = default_freshness_period
        self.default_expiration_period = default_expiration_period
        self.default_purge_period = default_purge_period
        self.max_items = max_items
        self._items = {}
        self._items_list = []
        self._fetcher_class = None
        self._active_fetchers = []
        self._purged = 0
        self._lock = threading.RLock()

    def request_object(self, address, state, object_handler, 
            error_handler = None, timeout_handler = None,
            backup_state = None, timeout = timedelta(minutes=60),
            freshness_period = None, expiration_period = None, purge_period = None):

        self._lock.acquire()
        try:
            item = self.get_item(address, state)
            if item:
                object_handler(item.address, item.value, item.state)
            if not self._fetcher:
                raise TypeError, "No cache fetcher defined"
            if not error_handler:
                def error_handler(address, data):
                    return object_handler(address, None, 'error')
            if freshness_period is None:
                freshness_period = self.default_freshness_period
            if expiration_period is None:
                expiration_period = self.default_expiration_period
            if purge_period is None:
                purge_period = self.default_purge_period
            
            fetcher = self._fetcher(self, address, freshness_period,
                    expiration_period, purge_period, object_handler, error_handler,
                    timeout_handler, timeout, backup_state)
            fetcher.fetch()
            self._active_fetchers.append((fetcher.timeout_time,fetcher))
            self._active_fetchers.sort()
        finally:
            self._lock.release()

    def invalidate_object(self, address, state = 'stale'):
        self._lock.acquire()
        try:
            item = self.get_item(address)
            if item and item.state_value<_state_values[state]:
                item.state=state
                item.update_state()
                self._items_list.sort()
        finally:
            self._lock.release()

    def add_item(self, item):
        """Add an item to the cache.

        Item state is updated before adding it (it will not be 'new' any more).

        :Parameters:
            - `item`: the item to add.
        :Types:
            - `item`: `CacheItem`

        :return: state of the item after addition.
        :returntype: `str`
        """
        self._lock.acquire()
        try:
            state = item.update_state()
            if state != 'purged':
                if len(self._items_list) >= self.max_items:
                    self.purge_items()
                self._items[item.address] = item
                self._items_list.append(item)
                self._items_list.sort()
            return item.state
        finally:
            self._lock.release()

    def get_item(self, address, state = 'fresh'):
        """Get an item from the cache.

        :Parameters:
            - `object_class`: class of the requested item.
            - `address`: its address.
            - `state`: the worst state that is acceptable.
        :Types:
            - `object_class`: `classobj`
            - `address`: any hashable
            - `state`: `str`
           
        :return: the item or `None` if it was not found.
        :returntype: `CacheItem`"""
        self._lock.acquire()
        try:
            item = self._items.get(address)
            if not item:
                return None
            new_state = self.update_item(item)
            if _state_values[state] >= item.state_value:
                return item
            return None
        finally:
            self._lock.release()

    def update_item(self, item):
        """Update state of an item in the cache.

        Update item's state and remove the item from the cache
        if its new state is 'purged'
        
        :Parameters:
            - `item`: item to update.
        :Types:
            - `item`: `CacheItem`
            
        :return: new state of the item.
        :returntype: `str`"""

        self._lock.acquire()
        try:
            state = item.update_state()
            self._items_list.sort()
            if item.state == 'purged':
                self._purged += 1
                if self._purged > 0.25*self.max_items:
                    self.purge_items()
            return state
        finally:
            self._lock.release()

    def num_items(self):
        return len(self._items_list)

    def purge_items(self):
        """Remove purged and overlimit items from the cache.

        TODO: optimize somehow.
        
        Leave no more than 75% of `self.max_items` items in the cache."""
        self._lock.acquire()
        try:
            il=self._items_list
            num_items = len(il)
            need_remove = num_items - int(0.75 * self.max_items)

            for i in range(need_remove):
                item=il.pop(0)
                try:
                    del self._items[item.address]
                except KeyError:
                    pass

            while il and il[0].update_state()=="purged":
                item=il.pop(0)
                try:
                    del self._items[item.address]
                except KeyError:
                    pass
        finally:
            self._lock.release()

    def tick(self):
        self._lock.acquire()
        try:
            now = datetime.utcnow()
            for t,f in list(self._active_fetchers):
                if t > now:
                    break
                f.timeout()
            self.purge_items()
        finally:
            self._lock.release()

    def remove_fetcher(self, fetcher):
        """Remove a running fetcher from the list of active fetchers.

        :Parameters:
            - `fetcher`: fetcher instance.
        :Types:
            - `fetcher`: `CacheFetcher`"""
        self._lock.acquire()
        try:
            for t, f in list(self._active_fetchers):
                if f is fetcher:
                    self._active_fetchers.remove((t, f))
                    f._deactivated()
                    return
        finally:
            self._lock.release()

    def set_fetcher(self, fetcher_class):
        """Set the fetcher class.

        :Parameters:
            - `fetcher_class`: the fetcher class.
        :Types:
            - `fetcher_class`: `CacheFetcher` based class
        """
        self._lock.acquire()
        try:
            self._fetcher = fetcher_class
        finally:
            self._lock.release()

class CacheSuite:
    """Caching proxy for object retrieval and caching.

    Object factories for other classes are registered in the
    `Cache` object and used to e.g. retrieve requested objects from network.
    They are called only when the requested object is not in the cache
    or is not fresh enough.

    Objects are addressed using their class and a class dependant address.
    Eg. `DiscoInfo` objects are addressed using (`DiscoInfo`,(jid, node)) tuple.

    Additionaly a state (freshness level) name may be provided when requesting
    an object. When the cached item state is "less fresh" then requested, then
    new object will be retrieved.

    Following states are defined:

      - 'new': always a new object should be retrieved.
      - 'fresh': a fresh object (not older than freshness time)
      - 'old': object not fresh, but most probably still valid.
      - 'stale': object known to be expired.

    :Ivariables:
        - `default_freshness_period`: default freshness period (in seconds).
        - `default_expiration_period`: default expiration period (in seconds).
        - `default_purge_period`: default purge period (in seconds). When
          0 then items are never purged because of their age.
        - `max_items`: maximum number of obejects of one class to store.
        - `_caches`: dictionary of per-class caches.
        - `_lock`: lock for thread safety.
    :Types:
        - `default_freshness_period`: timedelta
        - `default_expiration_period`: timedelta
        - `default_purge_period`: timedelta
        - `max_items`: `int`
        - `_caches`: `dict` of (`classobj`, addr) -> `Cache`
        - `_lock`: `threading.RLock`
    """
    def __init__(self, max_items, default_freshness_period = _hour,
            default_expiration_period = 12*_hour, default_purge_period = 24*_hour):
        """Initialize a `Cache` object.
        
            :Parameters:
                - `default_freshness_period`: default freshness period (in seconds).
                - `default_expiration_period`: default expiration period (in seconds).
                - `default_purge_period`: default purge period (in seconds). When
                  0 then items are never purged because of their age.
                - `max_items`: maximum number of items to store.
            :Types:
                - `default_freshness_period`: number
                - `default_expiration_period`: number
                - `default_purge_period`: number
                - `max_items`: number
        """
        self.default_freshness_period = default_freshness_period
        self.default_expiration_period = default_expiration_period
        self.default_purge_period = default_purge_period
        self.max_items = max_items
        self._caches = {}
        self._lock = threading.RLock()

    def request_object(self, object_class, address, state, object_handler, 
            error_handler = None, timeout_handler = None,
            backup_state = None, timeout = None,
            freshness_period = None, expiration_period = None, purge_period = None):

        self._lock.acquire()
        try:
            if object_class not in self._caches:
                raise TypeError, "No cache for %r" % (object_class,)
      
            self._caches[object_class].request_object(address, state, object_handler,
                    error_handler, timeout_handler, backup_state, timeout,
                    freshness_period, expiration_period, purge_period)
        finally:
            self._lock.release()

    def tick(self):
        self._lock.acquire()
        try:
            for cache in self._caches.values():
                cache.tick()
        finally:
            self._lock.release()

    def register_fetcher(self, object_class, fetcher_class):
        """Register a fetcher class for an object class.

        :Parameters:
            - `object_class`: class to be retrieved by the fetcher.
            - `fetcher_class`: the fetcher class.
        :Types:
            - `object_class`: `classobj`
            - `fetcher_class`: `CacheFetcher` based class
        """
        self._lock.acquire()
        try:
            cache = self._caches.get(object_class)
            if not cache:
                cache = Cache(self.max_items, self.default_freshness_period,
                        self.default_expiration_period, self.default_purge_period)
                self._caches[object_class] = cache
            cache.set_fetcher(fetcher_class)
        finally:
            self._lock.release()

    def unregister_fetcher(self, object_class):
        """Unregister a fetcher class for an object class.

        :Parameters:
            - `object_class`: class retrieved by the fetcher.
        :Types:
            - `object_class`: `classobj`
        """
        self._lock.acquire()
        try:
            cache = self._caches.get(object_class)
            if not cache:
                return
            cache.set_fetcher(None)
        finally:
            self._lock.release()
               
# vi: sts=4 et sw=4
