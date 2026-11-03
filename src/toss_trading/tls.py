"""Verified HTTPS using the operating system's certificate trust store."""

import ssl

import truststore


def ssl_context() -> ssl.SSLContext:
    # Per-client context: do not monkeypatch ssl globally or disable verification.
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
