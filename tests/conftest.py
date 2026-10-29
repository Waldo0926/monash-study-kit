"""测试一律用临时数据目录：paths 在导入时就定下位置，所以要在导入包之前设好。"""
import os
import tempfile
import socket

import pytest

for key in tuple(os.environ):
    if key.startswith("MONASH_KIT_"):
        os.environ.pop(key)

os.environ["MONASH_KIT_HOME"] = tempfile.mkdtemp(prefix="monash-kit-test-")
os.environ.pop("ED_TOKEN", None)
os.environ["MONASH_KIT_NO_UPDATE_CHECK"] = "1"   # 测试不联网查新版本；update_check 的测试自己打开


@pytest.fixture(autouse=True)
def offline_sockets(monkeypatch):
    connect = socket.socket.connect
    def local_only(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "localhost", "::1"}:
            raise AssertionError("Tests must not connect to real Moodle, Ed or other external services")
        return connect(sock, address)
    monkeypatch.setattr(socket.socket, "connect", local_only)
