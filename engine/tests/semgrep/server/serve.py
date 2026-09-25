"""Regression fixtures for outbound socket calls in the Engine server."""

import socket


def socket_alias() -> None:
    connection = socket.socket()
    alias = connection
    # ruleid: engine-server-no-outbound-socket
    alias.connect(("example.com", 443))


def socket_helper() -> None:
    # ruleid: engine-server-no-outbound-socket
    make_socket().connect(("example.com", 443))


def socket_context_manager() -> None:
    with socket.socket() as connection:
        # ruleid: engine-server-no-outbound-socket
        connection.sendto(b"content", ("example.com", 443))


def make_socket() -> socket.socket:
    return socket.socket()
