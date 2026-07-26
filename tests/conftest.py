import pytest
from fastapi.testclient import TestClient

from inspeg.api.app import create_app
from inspeg.store import Store

# The API only answers to loopback Host headers (DNS-rebinding defence), so
# the test client must look like a real local browser rather than TestClient's
# default "testserver". tests/test_security.py overrides these deliberately.
BASE_URL = "http://127.0.0.1:8137"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data")
    yield s
    s.close()


@pytest.fixture
def client(store):
    return TestClient(create_app(store), base_url=BASE_URL)
