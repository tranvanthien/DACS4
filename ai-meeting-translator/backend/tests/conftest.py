import pytest

def pytest_configure(config):
    config.addinivalue_line("markers", "mock: tests using mocks, no external API calls")
    config.addinivalue_line("markers", "real: tests calling real external APIs (requires internet)")
