"""Run the complete unittest suite with isolated data and HTTP clients disabled."""

import ipaddress
import socket
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests import _environment


def deny_network(*args, **kwargs):
    raise AssertionError("Live HTTP is forbidden in offline regression tests")


def main() -> int:
    import aiohttp
    import requests
    from curl_cffi import requests as curl_requests

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def local_connect(connection, address):
        if hasattr(socket, "AF_UNIX") and connection.family == socket.AF_UNIX:
            return original_connect(connection, address)
        if isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback:
            return original_connect(connection, address)
        return deny_network()

    def local_connect_ex(connection, address):
        if isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback:
            return original_connect_ex(connection, address)
        return deny_network()

    with ExitStack() as stack:
        stack.enter_context(patch.object(curl_requests.Session, "request", side_effect=deny_network))
        stack.enter_context(patch.object(requests.Session, "request", side_effect=deny_network))
        stack.enter_context(patch.object(aiohttp.ClientSession, "_request", side_effect=deny_network))
        stack.enter_context(patch("urllib.request.urlopen", side_effect=deny_network))
        stack.enter_context(patch.object(socket.socket, "connect", local_connect))
        stack.enter_context(patch.object(socket.socket, "connect_ex", local_connect_ex))
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())