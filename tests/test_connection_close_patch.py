# -*- coding: utf-8 -*-
"""Verify patch_connection_close() settles every pending CDP callback.

Reproduces the exact state the live export hit: one callback already resolved
(by Connection._async_send's ConnectionClosed handler) sitting alongside a
pending one. With stock pyppeteer that aborts _on_close mid-loop and the pending
future never completes — the process then hangs forever. With the patch applied,
every callback ends up settled so the awaiting coroutine gets an exception and
the exporter can retry.
"""
import asyncio
import sys

from pyppeteer import connection as pyppeteer_connection
from pyppeteer.errors import NetworkError

sys.path.insert(0, ".")
from weread_exporter.__main__ import patch_connection_close  # noqa: E402


def make_connection(loop):
    """A Connection carrying the attributes _on_close touches, without a socket."""
    conn = pyppeteer_connection.Connection.__new__(pyppeteer_connection.Connection)
    conn._callbacks = {}
    conn._sessions = {}
    conn._closeCallback = None
    conn._recv_fut = loop.create_future()
    conn._loop = loop
    return conn


def add_callback(conn, loop, method, resolved=False):
    fut = loop.create_future()
    fut.error = NetworkError()
    fut.method = method
    conn._callbacks[id(fut)] = fut
    if resolved:
        fut.set_result(None)          # what _async_send does on a closed socket
    return fut


async def main():
    loop = asyncio.get_running_loop()

    print("--- stock pyppeteer _on_close ---")
    conn = make_connection(loop)
    done_cb = add_callback(conn, loop, "Page.navigate", resolved=True)
    pending_cb = add_callback(conn, loop, "Runtime.evaluate")
    try:
        await pyppeteer_connection.Connection._on_close(conn)
    except BaseException as exc:
        print("   raised %s: %s  -> loop aborted, pending callback left unsettled"
              % (type(exc).__name__, exc))
    print("   pending callback settled? %s" % pending_cb.done())
    if not pending_cb.done():
        pending_cb.cancel()

    print("--- patched _on_close ---")
    patch_connection_close()
    conn = make_connection(loop)
    done_cb = add_callback(conn, loop, "Page.navigate", resolved=True)
    pending_cb = add_callback(conn, loop, "Runtime.evaluate")
    cancelled = add_callback(conn, loop, "Runtime.callFunctionOn")
    cancelled.cancel()
    await pyppeteer_connection.Connection._on_close(conn)

    print("   already-resolved callback kept its result: %s" % (done_cb.done() and not done_cb.cancelled()))
    print("   pending callback settled?    %s" % pending_cb.done())
    print("   cancelled callback untouched: %s" % cancelled.cancelled())
    print("   callbacks dict cleared?      %s" % (len(conn._callbacks) == 0))

    ok = (
        pending_cb.done()
        and not pending_cb.cancelled()
        and isinstance(pending_cb.exception(), Exception)
        and done_cb.done()
        and cancelled.cancelled()
        and len(conn._callbacks) == 0
    )
    print("RESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
