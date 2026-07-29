"""Port-wait helper: bound port detected, closed port times out."""
from __future__ import annotations

import socket
import threading

from pipeline import devserve


def test_await_port_succeeds_on_bound_port():
    server = socket.create_server(("localhost", 0))
    port = server.getsockname()[1]
    accept_thread = threading.Thread(target=lambda: server.accept(), daemon=True)
    accept_thread.start()
    try:
        assert devserve.await_port(port, timeout=5.0) is True
    finally:
        server.close()


def test_await_port_times_out_on_closed_port():
    sock = socket.create_server(("localhost", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert devserve.await_port(port, timeout=1.0) is False
