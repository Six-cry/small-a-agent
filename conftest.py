"""Use non-secret configuration and prevent external I/O in the offline suite."""

import os
import socket

import pytest


# This file is loaded only by pytest, before collection imports config.py.
os.environ["ANTHROPIC_API_KEY"] = "offline-test-placeholder"
os.environ["ANTHROPIC_BASE_URL"] = "https://example.invalid"
os.environ["MODEL_ID"] = "offline-test-model"
os.environ["MCP_ENABLED"] = "false"
os.environ["DASHSCOPE_API_KEY"] = ""
os.environ["EMBEDDING_BASE_URL"] = "https://example.invalid/v1"
os.environ["TAVILY_API_KEY"] = ""


@pytest.fixture(autouse=True)
def prohibit_external_connections(monkeypatch):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def check_address(address):
        if isinstance(address, tuple) and address[0] not in {
            "127.0.0.1", "::1", "localhost"
        }:
            raise AssertionError("Offline tests cannot connect to external services")

    def guarded_connect(sock, address):
        check_address(address)
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        check_address(address)
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
