"""Jade reference cloud server.

A minimal, honest backend for the local-first Jade assistant:
accounts, OAuth brokerage for connectors, server-assisted device
pairing (signaling only), and an OpenAI-compatible chat endpoint.

Reference implementation — NOT production-hardened. Read
server/PRODUCTION.md before exposing this to real users.

Everything here runs on the standard library plus the optional
``cryptography`` package (server-only; never a CLI dependency).
"""

__version__ = "0.1.0"
