"""asyncio on top of the GLib main loop.

Ward runs one thread for networking and the interface: slixmpp (asyncio) and GTK (GLib) share the GLib main
context.  PyGObject 3.50 has the same idea built in (gi.events), but distributions still ship 3.42 to 3.48, so we
carry our own small implementation and use it everywhere: one code path, tested on every version.

How it works: GLibEventLoop is a normal asyncio.SelectorEventLoop whose selector never blocks.  Instead, a GLib
source owns every file descriptor asyncio wants to watch and tells GLib how long asyncio may sleep (the time to the
next timer, or 0 when callbacks are ready).  After GLib has polled, the source checks which descriptors fired and
dispatches asyncio's own _run_once(), whose select() call simply returns those events.

Use `with loop.running(): app.run(argv)` to run a Gtk.Application with asyncio available inside it, or the usual
loop.run_until_complete() / run_forever() for headless code and tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import selectors
import sys
import threading
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from gi.repository import GLib

log = logging.getLogger("ward.aio")
T = TypeVar("T")

_IN = GLib.IOCondition.IN
_OUT = GLib.IOCondition.OUT
_MAX_TIMEOUT_MS = 2**31 - 1


def _fd(fileobj: Any) -> int:
    if isinstance(fileobj, int):
        return fileobj
    try:
        return int(fileobj.fileno())
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Invalid file object: %r" % (fileobj,)) from None


class _Key(selectors.SelectorKey):
    """A SelectorKey that remembers its GLib unix-fd tag."""

    tag: Any = None


class _Source(GLib.Source):
    def __init__(self, loop: GLibEventLoop):
        super().__init__()
        self.loop = loop
        self.ready: list[tuple[selectors.SelectorKey, int]] = []
        self.set_name("ward asyncio")

    def prepare(self):
        # Never claim readiness here: file descriptors are only known after the poll, in check().
        return False, self.loop._timeout_ms()

    def check(self):
        ready = []
        for key in self.loop._selector_keys():
            cond = self.query_unix_fd(key.tag)
            events = 0
            # Mirror selectors.EpollSelector: ERR/HUP/NVAL wake readers and writers alike.
            if cond & ~_OUT:
                events |= selectors.EVENT_READ
            if cond & ~_IN:
                events |= selectors.EVENT_WRITE
            events &= key.events
            if events:
                ready.append((key, events))
        self.ready = ready
        return bool(ready) or self.loop._timeout_ms() == 0

    def dispatch(self, callback, args):
        self.loop._dispatch()
        return GLib.SOURCE_CONTINUE


class _Selector(selectors.BaseSelector):
    """Registers asyncio's descriptors with the GLib source; select() returns what the source found."""

    def __init__(self, loop: GLibEventLoop):
        self._loop = loop
        self._keys: dict[int, _Key] = {}
        self.source: _Source | None = None

    # -- source lifetime: a destroyed GSource cannot be attached again, so detach() makes a fresh one
    def attach(self, context: GLib.MainContext) -> None:
        self.source = _Source(self._loop)
        for key in self._keys.values():
            key.tag = self.source.add_unix_fd(key.fd, self._condition(key.events))
        self.source.attach(context)

    def detach(self) -> None:
        if self.source is not None:
            self.source.destroy()
            self.source = None
        for key in self._keys.values():
            key.tag = None

    @staticmethod
    def _condition(events: int) -> GLib.IOCondition:
        cond = GLib.IOCondition(0)
        if events & selectors.EVENT_READ:
            cond |= _IN
        if events & selectors.EVENT_WRITE:
            cond |= _OUT
        return cond

    def register(self, fileobj, events, data=None):
        if not events or events & ~(selectors.EVENT_READ | selectors.EVENT_WRITE):
            raise ValueError("Invalid events: %r" % events)
        fd = _fd(fileobj)
        if fd in self._keys:
            raise KeyError("%r (FD %d) is already registered" % (fileobj, fd))
        key = _Key(fileobj, fd, events, data)
        if self.source is not None:
            key.tag = self.source.add_unix_fd(fd, self._condition(events))
        self._keys[fd] = key
        return key

    def unregister(self, fileobj):
        fd = _fd(fileobj)
        key = self._keys.pop(fd)
        if self.source is not None and key.tag is not None:
            self.source.remove_unix_fd(key.tag)
        return key

    def modify(self, fileobj, events, data=None):
        fd = _fd(fileobj)
        old = self._keys.get(fd)
        if old is None:
            raise KeyError("%r is not registered" % (fileobj,))
        if events == old.events:
            key = _Key(old.fileobj, fd, events, data)
            key.tag = old.tag
            self._keys[fd] = key
            return key
        self.unregister(fileobj)
        return self.register(fileobj, events, data)

    def select(self, timeout=None):
        if self.source is None:
            return []
        ready, self.source.ready = self.source.ready, []
        # Other GLib sources run between check() and our dispatch, and their callbacks may have changed or removed
        # asyncio's registrations meanwhile: always report against the current key.
        out = []
        for key, events in ready:
            cur = self._keys.get(key.fd)
            if cur is not None and events & cur.events:
                out.append((cur, events & cur.events))
        return out

    def get_key(self, fileobj):
        return self._keys[_fd(fileobj)]

    def get_map(self):
        return {k.fileobj: k for k in self._keys.values()}

    def keys(self):
        return list(self._keys.values())

    def close(self):
        self.detach()
        self._keys.clear()


class GLibEventLoop(asyncio.SelectorEventLoop):
    """An asyncio event loop that runs inside a GLib main context (the default one unless told otherwise)."""

    def __init__(self, context: GLib.MainContext | None = None):
        self._context = context or GLib.MainContext.default()
        self._gsel = _Selector(self)
        self._mainloop: GLib.MainLoop | None = None
        self._depth = 0
        super().__init__(selector=self._gsel)
        self._clock_resolution = 1e-3

    # ------------------------------------------------------------- GLib side
    def time(self) -> float:
        return GLib.get_monotonic_time() / 1e6

    def _selector_keys(self) -> list[_Key]:
        return self._gsel.keys()

    def _timeout_ms(self) -> int:
        if self._ready or self._stopping:
            return 0
        if self._scheduled:
            delta = self._scheduled[0]._when - self.time()
            if delta <= self._clock_resolution:
                return 0
            return min(math.ceil(delta * 1000), _MAX_TIMEOUT_MS)
        return -1

    def _dispatch(self) -> None:
        self._run_once()
        if self._stopping:
            if self._mainloop is not None:
                self._mainloop.quit()
            else:
                # Running inside someone else's main loop (Gtk.Application.run): stop() has nothing to stop.
                self._stopping = False

    @contextlib.contextmanager
    def running(self, quit_func=None) -> Iterator[GLibEventLoop]:
        """Mark the loop as running while GLib code (Gtk.Application.run, GLib.MainLoop.run) iterates the
        context.  Nested use is allowed and only the outermost level sets things up.  PyGObject 3.50 and newer
        call this itself, with the quit function of the GLib loop it is about to run, from their run()
        overrides: that loop is ours or nested in ours, so the argument needs no handling here."""
        if self._depth:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return
        self._check_closed()
        if asyncio._get_running_loop() is not None:
            raise RuntimeError("another asyncio event loop is already running in this thread")
        old_hooks = sys.get_asyncgen_hooks()
        self._thread_id = threading.get_ident()
        sys.set_asyncgen_hooks(firstiter=self._asyncgen_firstiter_hook, finalizer=self._asyncgen_finalizer_hook)
        self._set_coroutine_origin_tracking(self._debug)
        asyncio._set_running_loop(self)
        self._depth = 1
        self._gsel.attach(self._context)
        try:
            yield self
        finally:
            self._depth = 0
            self._gsel.detach()
            asyncio._set_running_loop(None)
            self._thread_id = None
            self._stopping = False
            try:
                self._set_coroutine_origin_tracking(False)
            except AttributeError:
                pass
            sys.set_asyncgen_hooks(*old_hooks)

    def run_forever(self) -> None:
        if self.is_running():
            raise RuntimeError("This event loop is already running")
        with self.running():
            self._mainloop = GLib.MainLoop.new(self._context, False)
            try:
                self._mainloop.run()
            finally:
                self._mainloop = None

    def is_running(self) -> bool:
        return self._depth > 0

    def stop(self) -> None:
        self._stopping = True
        self._context.wakeup()

    def close(self) -> None:
        if self.is_running():
            raise RuntimeError("Cannot close a running event loop")
        super().close()


# ---------------------------------------------------------------------------------------------------------------
_loop: GLibEventLoop | None = None
_tasks: set[asyncio.Task] = set()
_workers: ThreadPoolExecutor | None = None


def install() -> GLibEventLoop:
    """Create (once) the process-wide GLib event loop and make it the current asyncio loop."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = GLibEventLoop()
        _loop.set_default_executor(workers())
        asyncio.set_event_loop(_loop)
    return _loop


def get_loop() -> GLibEventLoop:
    return _loop if _loop is not None and not _loop.is_closed() else install()


def workers() -> ThreadPoolExecutor:
    """The small worker pool for CPU work and blocking I/O (image decoding, file crypto, gpg, HTTP)."""
    global _workers
    if _workers is None:
        _workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ward-worker")
    return _workers


def _log_result(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("background task %s failed", task.get_name(), exc_info=exc)


def spawn(coro: Coroutine[Any, Any, T], name: str | None = None) -> asyncio.Task[T]:
    """Run a coroutine in the background, keep a reference until it finishes and log its exceptions.  This is
    how the interface calls into the core: spawn(core.chats.send(...))."""
    task = get_loop().create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_log_result)
    return task


async def run_in_thread(func: Callable[..., T], *args: Any) -> T:
    return await asyncio.get_running_loop().run_in_executor(workers(), func, *args)


def gio_call(start: Callable[..., Any], finish: Callable[[Any], T], *args: Any) -> Awaitable[T]:
    """Await a Gio-style asynchronous call: `await gio_call(obj.foo_async, obj.foo_finish, a, b, None)`.
    The callback argument is appended automatically; pass the GCancellable (or None) yourself."""
    fut: asyncio.Future[T] = asyncio.get_running_loop().create_future()

    def done(_obj, result, *_):
        if fut.cancelled():
            return
        try:
            fut.set_result(finish(result))
        except Exception as e:  # GLib.Error and friends
            fut.set_exception(e)

    start(*args, done)
    return fut


def cancel_all() -> None:
    for t in list(_tasks):
        t.cancel()
