"""Deterministic provider and TCP denial for the isolated test runner only."""
import hashlib
import math
import os
import re
import socket
from pathlib import Path

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _guard(original):
    def guarded(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("TCP disabled in isolated OpenHippo tests")
        return original(sock, address)
    return guarded


socket.socket.connect = _guard(_connect)
socket.socket.connect_ex = _guard(_connect_ex)


def pytest_configure(config):
    home = Path(os.environ["HOME"])
    assert (home / ".openhippo-offline-test").is_file(), "Use scripts/run_offline_tests.py"
    from openhippo.core import embedding

    class FakeProvider(embedding.EmbeddingProvider):
        model = "AUDIT_FAKE_HASH_EMBEDDING"

        @property
        def dimension(self):
            return 768

        def embed(self, text):
            if not text:
                return None
            vector = [0.0] * 768
            for token in re.findall(r"\w+", text.lower()) or [text]:
                digest = hashlib.sha256(token.encode()).digest()
                vector[int.from_bytes(digest[:2], "big") % 768] += 1
            norm = math.sqrt(sum(value * value for value in vector))
            return [value / norm for value in vector]

    embedding.set_provider(FakeProvider())
