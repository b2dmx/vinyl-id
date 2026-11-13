"""Home Assistant tests for the integration. Run with a Python that has
pytest-homeassistant-custom-component:  python -m pytest -c tests_ha/pytest.ini tests_ha"""
import sys

import pytest

if sys.platform == "win32":
    # Local runs only (CI is Linux): asyncio on Windows needs a socket pair for its own loop.
    import pytest_socket

    pytest_socket.disable_socket = lambda *a, **k: None
    pytest_socket.socket_allow_hosts = lambda *a, **k: None

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
