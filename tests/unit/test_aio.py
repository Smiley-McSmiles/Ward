import asyncio
import os
import ssl
import subprocess
import threading
import time

import pytest
from gi.repository import Gio, GLib

from ward import aio


def test_timers_and_callbacks(run):
    order = []

    async def main():
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, order.append, "late")
        loop.call_soon(order.append, "soon")
        t0 = loop.time()
        await asyncio.sleep(0.1)
        return loop.time() - t0

    elapsed = run(main())
    assert order == ["soon", "late"]
    assert 0.09 <= elapsed < 1.0


def test_tcp_echo(run):
    async def handle(reader, writer):
        data = await reader.read(100)
        writer.write(data.upper())
        await writer.drain()
        writer.close()

    async def main():
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"hello ward")
        await writer.drain()
        got = await reader.read(100)
        writer.close()
        server.close()
        await server.wait_closed()
        return got

    assert run(main()) == b"HELLO WARD"


def test_large_transfer_with_backpressure(run):
    payload = os.urandom(4 * 1024 * 1024)

    async def handle(reader, writer):
        got = await reader.readexactly(len(payload))
        writer.write(str(len(got)).encode())
        await writer.drain()
        writer.close()

    async def main():
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(payload)
        await writer.drain()
        got = await reader.read(100)
        writer.close()
        server.close()
        return got

    assert run(main()) == str(len(payload)).encode()


def _self_signed(tmp_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out",
                    str(crt), "-days", "2", "-subj", "/CN=localhost"], check=True, capture_output=True)
    return str(key), str(crt)


@pytest.mark.skipif(not os.path.exists("/usr/bin/openssl"), reason="openssl missing")
def test_tls(run, tmp_path):
    key, crt = _self_signed(tmp_path)
    sctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    sctx.load_cert_chain(crt, key)
    cctx = ssl.create_default_context(cafile=crt)
    cctx.check_hostname = False

    async def handle(reader, writer):
        writer.write(await reader.readline())
        await writer.drain()
        writer.close()

    async def main():
        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=sctx)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=cctx)
        writer.write(b"secure\n")
        line = await reader.readline()
        writer.close()
        server.close()
        return line

    assert run(main()) == b"secure\n"


def test_threads_wake_the_loop(run):
    def slow():
        time.sleep(0.05)
        return threading.current_thread().name

    async def main():
        name = await aio.run_in_thread(slow)
        fut = asyncio.get_running_loop().create_future()
        loop = asyncio.get_running_loop()
        threading.Thread(target=lambda: loop.call_soon_threadsafe(fut.set_result, 42)).start()
        return name, await fut

    name, val = run(main())
    assert name.startswith("ward-worker")
    assert val == 42


def test_gio_async_call(run, tmp_path):
    p = tmp_path / "f.txt"
    p.write_bytes(b"gio says hi")

    async def main():
        f = Gio.File.new_for_path(str(p))
        _ok, data, _etag = await aio.gio_call(f.load_contents_async, f.load_contents_finish, None)
        return data

    assert run(main()) == b"gio says hi"


def test_glib_timeouts_interleave(run):
    hits = []

    async def main():
        GLib.timeout_add(20, lambda: hits.append("glib") and False)
        await asyncio.sleep(0.08)

    run(main())
    assert hits == ["glib"]


def test_running_inside_glib_main_loop(loop):
    """What Gtk.Application.run() does: GLib owns the loop, asyncio tasks still run."""
    result = {}
    ml = GLib.MainLoop()

    async def work():
        await asyncio.sleep(0.02)
        result["inner"] = asyncio.get_running_loop() is loop
        ml.quit()

    def start():
        aio.spawn(work())
        return False

    with loop.running():
        GLib.idle_add(start)
        GLib.timeout_add(5000, ml.quit)
        ml.run()
    assert result == {"inner": True}
    assert not loop.is_running()


def test_cancellation_and_exceptions(run):
    async def forever():
        await asyncio.sleep(3600)

    async def main():
        t = asyncio.ensure_future(forever())
        await asyncio.sleep(0)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

        async def boom():
            await asyncio.sleep(0.01)
            raise ZeroDivisionError

        await asyncio.wait_for(boom(), 1)

    with pytest.raises(ZeroDivisionError):
        run(main())


def test_subprocess(run):
    async def main():
        p = await asyncio.create_subprocess_exec("echo", "sub", stdout=asyncio.subprocess.PIPE)
        out, _ = await p.communicate()
        return out

    assert run(main()) == b"sub\n"


def test_stop_and_restart(loop):
    loop.call_soon(loop.stop)
    loop.run_forever()
    assert loop.run_until_complete(asyncio.sleep(0, result="again")) == "again"
