# Copyright 2024 by UltrafunkAmsterdam (https://github.com/UltrafunkAmsterdam)
# All rights reserved.
# This file is part of the nodriver package.
# and is released under the "GNU AFFERO GENERAL PUBLIC LICENSE".
# Please see the LICENSE.txt file that should have been included as part of this package.

from __future__ import annotations

import asyncio
import collections
import inspect
import itertools
import json
import logging
import threading
import types
from asyncio import iscoroutine, iscoroutinefunction
from typing import Any, Awaitable, Callable, Generator, List, TypeVar, Union

import websockets.asyncio.client

from .. import cdp
from . import browser as _browser
from . import util

T = TypeVar("T")

GLOBAL_DELAY = 0.005
MAX_SIZE: int = 2**28
PING_TIMEOUT: int = 900  # 15 minutes

TargetType = Union[cdp.target.TargetInfo, cdp.target.TargetID]

logger = logging.getLogger(__name__)


class ProtocolException(Exception):
    def __init__(self, *args, **kwargs):  # real signature unknown

        self.message = None
        self.code = None
        self.args = args
        if isinstance(args[0], dict):

            self.message = args[0].get("message", None)  # noqa
            self.code = args[0].get("code", None)

        elif hasattr(args[0], "to_json"):

            def serialize(obj, _d=0):
                res = "\n"
                for k, v in obj.items():
                    space = "\t" * _d
                    if isinstance(v, dict):
                        res += f"{space}{k}: {serialize(v, _d + 1)}\n"
                    else:
                        res += f"{space}{k}: {v}\n"

                return res

            self.message = serialize(args[0].to_json())

        else:
            self.message = "| ".join(str(x) for x in args)

    def __str__(self):
        return f"{self.message} [code: {self.code}]" if self.code else f"{self.message}"


class SettingClassVarNotAllowedException(PermissionError):
    pass


class Transaction(asyncio.Future):
    __cdp_obj__: Generator = None

    method: str = None
    params: dict = None

    id: int = None

    def __init__(self, cdp_obj: Generator):
        """
        :param cdp_obj:
        """
        super().__init__()
        self.__cdp_obj__ = cdp_obj
        self.connection = None

        cmd = next(self.__cdp_obj__)
        self.method = cmd["method"]
        self.params = cmd.get("params", {})

    @property
    def message(self):
        return json.dumps({"method": self.method, "params": self.params, "id": self.id})

    @property
    def has_exception(self):
        try:
            if self.exception():
                return True
        except asyncio.InvalidStateError as e:  # noqa
            if "not set" in e.args:
                return False
        except:
            return True
        return False

    def __call__(self, **response: dict):
        """
        parsed the response message and marks the future
        complete

        :param response:
        :return:
        """
        # check if already completed to avoid InvalidStateError
        if self.done():
            return

        if "error" in response:
            # set exception and bail out
            try:
                self.set_exception(ProtocolException(response["error"]))
            except asyncio.InvalidStateError:
                # race: someone beat us to completing the future
                pass
            return
        try:
            # try to parse the result according to the py cdp docs.
            self.__cdp_obj__.send(response["result"])
        except KeyError as e:
            raise KeyError(f"key '{e.args}' not found in message: {response['result']}")
        except StopIteration as e:
            # exception value holds the parsed response
            try:
                self.set_result(e.value)
            except asyncio.InvalidStateError:
                # race: someone beat us to completing the future
                pass

    def __repr__(self):
        success = False if (self.done() and self.has_exception) else True
        if self.done():
            status = "finished"
        else:
            status = "pending"
        fmt = (
            f"<{self.__class__.__name__}\n\t"
            f"method: {self.method}\n\t"
            f"status: {status}\n\t"
            f"success: {success}>"
        )
        return fmt


class EventTransaction(Transaction):
    event = None
    value = None

    def __init__(self, event_object):
        try:
            super().__init__(None)
        except:
            pass
        self.set_result(event_object)
        self.event = self.value = self.result()

    def __repr__(self):
        status = "finished"
        success = False if self.exception() else True
        event_object = self.result()
        fmt = (
            f"{self.__class__.__name__}\n\t"
            f"event: {event_object.__class__.__module__}.{event_object.__class__.__name__}\n\t"
            f"status: {status}\n\t"
            f"success: {success}>"
        )
        return fmt


class CantTouchThis(type):
    def __setattr__(cls, attr, value):
        """
        :meta private:
        """
        if attr == "__annotations__":
            # fix autodoc
            return super().__setattr__(attr, value)
        raise SettingClassVarNotAllowedException(
            "\n".join(
                (
                    "don't set '%s' on the %s class directly, as those are shared with other objects.",
                    "use `my_object.%s = %s`  instead",
                )
            )
            % (attr, cls.__name__, attr, value)
        )


class Connection(metaclass=CantTouchThis):
    attached: bool = None

    @property
    def browser(self) -> _browser.Browser:
        return self._browser

    @property
    def websocket(self) -> websockets.asyncio.client.ClientConnection:
        return self._websocket

    @property
    def target(self) -> cdp.target.TargetInfo:
        return self._target

    def __init__(
        self,
        websocket_url: str,
        target: cdp.target.TargetInfo = None,
        browser: _browser.Browser = None,
        **kwargs,
    ):
        super().__init__()
        self.websocket_url: str = websocket_url
        self.mapper = {}
        self.handlers = collections.defaultdict(list)
        self.enabled_domains = []
        self._target = target
        self._browser = browser
        self._websocket = None
        self._listener_task = None
        self._event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()
        # threading.Lock used for handlers because add_handler/remove_handler are sync methods
        # (API compatibility). Critical sections are <1μs so event loop blocking is negligible.
        self._handlers_lock = threading.Lock()
        self.__count__ = itertools.count(0)
        self.__dict__.update(**kwargs)

    @property
    def closed(self):
        if not self.websocket:
            return True
        return bool(self.websocket.close_code)

    def add_handler(
        self,
        event_type_or_domain: Union[type, types.ModuleType, List[type]],
        handler: Union[Callable, Awaitable],
    ):
        """
        add a handler for given event

        if event_type_or_domain is a module instead of a type, it will find all available events and add
        the handler.

        if you want to receive event updates (network traffic are also 'events') you can add handlers for those events.
        handlers can be regular callback functions or async coroutine functions (and also just lamba's).
        for example, you want to check the network traffic:

        .. code-block::

            page.add_handler(cdp.network.RequestWillBeSent, lambda event: print('network event => %s' % event.request))

        the next time you make network traffic you will see your console print like crazy.

        :param event_type_or_domain:
        :type event_type_or_domain:
        :param handler:
        :type handler:

        :return:
        :rtype:
        """

        if not isinstance(event_type_or_domain, list):
            event_type_or_domain = [event_type_or_domain]

        for evt_dom in event_type_or_domain:
            if isinstance(evt_dom, types.ModuleType):
                event_types = []
                for name, obj in inspect.getmembers_static(evt_dom):
                    if name.isupper():
                        continue
                    if not name[0].isupper():
                        continue
                    if type(obj) != type:
                        continue
                    if inspect.isbuiltin(obj):
                        continue
                    event_types.append(obj)
                with self._handlers_lock:
                    for obj in event_types:
                        self.handlers[obj].append(handler)
                return
            else:
                with self._handlers_lock:
                    self.handlers[evt_dom].append(handler)

    def remove_handler(
        self,
        event_type_or_domain: Union[type, types.ModuleType, List[type]],
        handler: Union[Callable, Awaitable] = None,
    ):
        """
        remove a handler for given event
        :param event_type_or_domain:
        :type event_type_or_domain:
        :param handler:
        :type handler:
        """
        if handler:
            with self._handlers_lock:
                for event, callbacks in list(self.handlers.items()):
                    if handler in callbacks:
                        self.handlers[event].remove(handler)

        if not isinstance(event_type_or_domain, list):
            event_type_or_domain = [event_type_or_domain]

        for evt_dom in event_type_or_domain:
            if isinstance(evt_dom, types.ModuleType):
                event_types = []
                for name, obj in inspect.getmembers_static(evt_dom):
                    if name.isupper():
                        continue
                    if not name[0].isupper():
                        continue
                    if type(obj) != type:
                        continue
                    if inspect.isbuiltin(obj):
                        continue
                    event_types.append(obj)
                with self._handlers_lock:
                    for obj in event_types:
                        if obj in self.handlers:
                            del self.handlers[obj]
                return
            else:
                with self._handlers_lock:
                    if evt_dom in self.handlers:
                        del self.handlers[evt_dom]

    async def _connect_unlocked(self):
        """Internal connection logic without lock acquisition"""
        if not self.websocket or bool(self.websocket.close_code):
            try:
                self._websocket = await websockets.asyncio.client.connect(
                    self.websocket_url,
                    ping_timeout=PING_TIMEOUT,
                    max_size=MAX_SIZE,
                )
                self._listener_task = asyncio.ensure_future(self._listener())

            except (Exception,) as e:
                logger.debug("exception during opening of websocket : %s", e)
                raise

            await self._register_handlers()

    async def connect(self, **kw):
        """
        opens the websocket connection. should not be called manually by users
        :param kw:
        :return:
        """
        async with self._connection_lock:
            await self._connect_unlocked()

    async def disconnect(self):
        """
        closes the websocket connection. should not be called manually by users.
        """
        async with self._connection_lock:
            if self._listener_task:
                self._listener_task.cancel()
                try:
                    await self._listener_task
                except asyncio.CancelledError:
                    pass
            if self.websocket:
                self.enabled_domains.clear()
                await self.websocket.close()
                logger.debug("\n❌ closed websocket connection to %s", self.websocket_url)

    def __getattr__(self, item):
        """:meta private:"""
        try:
            return getattr(self.target, item)
        except AttributeError:
            raise

    async def __aenter__(self):
        """:meta private:"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """:meta private:"""
        await self.close()

    def _handle_handler_task_result(self, task: asyncio.Task):
        """
        Callback to handle exceptions from async handler tasks.
        """
        try:
            task.result()
        except Exception as e:
            logger.warning(
                "exception in async handler task => %s",
                e,
                exc_info=True,
            )

    async def _register_handlers(self):
        """
        ensure that for current (event) handlers, the corresponding
        domain is enabled in the protocol.

        """
        seen = []
        # save a copy of current enabled domains in a variable
        # domains will be removed from this variable
        # if it is still needed according to the set handlers
        # so at the end this variable will hold the domains that
        # are not represented by handlers, and can be removed
        enabled_domains = self.enabled_domains.copy()
        with self._handlers_lock:
            handlers_snapshot = self.handlers.copy()
            for event_type in list(handlers_snapshot.keys()):
                if len(self.handlers.get(event_type, [])) == 0:
                    self.handlers.pop(event_type, None)
        for event_type in handlers_snapshot:
            domain_mod = None
            if event_type not in self.handlers:
                continue
            if isinstance(event_type, type):
                domain_mod = util.cdp_get_module(event_type.__module__)
            if domain_mod in self.enabled_domains:
                # at this point, the domain is being used by a handler
                # so remove that domain from temp variable 'enabled_domains' if present
                if domain_mod in enabled_domains:
                    enabled_domains.remove(domain_mod)
                continue
            elif domain_mod not in self.enabled_domains:
                if domain_mod in (cdp.target, cdp.storage):
                    # by default enabled
                    continue
                should_enable = False
                with self._handlers_lock:
                    if domain_mod not in self.enabled_domains:
                        logger.debug("registered %s", domain_mod)
                        self.enabled_domains.append(domain_mod)
                        should_enable = True
                if should_enable:
                    try:
                        await self.send(domain_mod.enable(), _is_update=True)
                    except:  # noqa - as broad as possible, we don't want an error before the "actual" request is sent
                        logger.debug("", exc_info=True)
                        with self._handlers_lock:
                            try:
                                self.enabled_domains.remove(domain_mod)
                            except ValueError:
                                # benign race condition; domain already removed by concurrent call
                                pass
                    finally:
                        continue
        with self._handlers_lock:
            for ed in enabled_domains:
                # we started with a copy of self.enabled_domains and removed a domain from this
                # temp variable when we registered it or saw handlers for it.
                # items still present at this point are unused and need removal
                try:
                    self.enabled_domains.remove(ed)
                except ValueError:
                    # benign race condition; domain already removed by concurrent call
                    continue

    async def _listener(self):
        seen_one = False
        while True:
            try:
                # No timeout needed - recv() is cancellable and returns immediately on messages/close.
                # No lock needed - only one _listener task exists, and it's the sole caller of recv().
                raw = await self.websocket.recv()
            except ProtocolException:
                break
            except websockets.exceptions.ConnectionClosedOK:
                await self.disconnect()
                break
            except websockets.exceptions.ConnectionClosed:
                await self.disconnect()
                break
            except (Exception,) as e:
                logger.info(
                    "error when receiving websocket response: %s" % e, exc_info=True
                )
                raise
            else:
                message = json.loads(raw)
                seen_one = True
                if "id" in message:
                    async with self._lock:
                        try:
                            tx: Transaction = self.mapper.pop(message["id"])
                        except KeyError:
                            logger.warning("Received message with unknown id: %s", message.get("id"))
                            continue
                    tx(**message)
                    logger.debug("got answer for (message_id:%d) => %s", tx.id, message)
                else:
                    # probably an event
                    try:
                        event = cdp.util.parse_json_event(message)
                    except Exception as e:
                        logger.info(
                            "%s: %s  during parsing of json from event : %s"
                            % (type(e).__name__, e.args, message),
                            exc_info=True,
                        )
                        continue
                    except KeyError as e:
                        logger.info("some lousy KeyError %s" % e, exc_info=True)
                        continue
                    try:
                        with self._handlers_lock:
                            if type(event) in self.handlers:
                                callbacks = list(self.handlers[type(event)])
                            else:
                                callbacks = []
                        if not callbacks:
                            continue
                        for callback in callbacks:
                            try:
                                if iscoroutinefunction(callback) or iscoroutine(
                                    callback
                                ):
                                    try:
                                        task = asyncio.create_task(callback(event, self))
                                        task.add_done_callback(self._handle_handler_task_result)
                                    except TypeError as e:
                                        task = asyncio.create_task(callback(event))
                                        task.add_done_callback(self._handle_handler_task_result)
                                else:
                                    try:
                                        callback(event, self)
                                    except TypeError:
                                        callback(event)
                            except Exception as e:
                                logger.warning(
                                    "exception in callback %s for event %s => %s",
                                    callback,
                                    event.__class__.__name__,
                                    e,
                                    exc_info=True,
                                )
                                # since it's handlers, don't raise and screw our program

                    except (Exception,) as e:
                        raise

    async def send(
        self, cdp_obj: Generator[dict[str, Any], dict[str, Any], Any], _is_update=False
    ) -> Any:
        """
        send a protocol command. the commands are made using any of the cdp.<domain>.<method>()'s
        and is used to send custom cdp commands as well.

        :param cdp_obj: the generator object created by a cdp method

        :param _is_update: internal flag
            prevents infinite loop by skipping the registeration of handlers
            when multiple calls to connection.send() are made
        :return:
        """
        # Check websocket state inside lock to avoid race with connect()/disconnect()
        async with self._connection_lock:
            await self._connect_unlocked()
        if not _is_update:
            await self._register_handlers()
        tx = Transaction(cdp_obj)
        async with self._lock:
            the_id = next(self.__count__)
            tx.id = the_id
            self.mapper[the_id] = tx
        await self.websocket.send(tx.message)
        return await tx

    async def _send_oneshot(self, cdp_obj):
        """fire and forget , eg: send command without waiting for any response"""
        return await self.send(cdp_obj, _is_update=True)
