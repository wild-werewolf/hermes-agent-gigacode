"""Allowlist HTTP(S) egress proxy for the Hermes container — a stand-in for squid where no proxy
image can be pulled. Standard library only; runs on the Hermes image's own Python.

    python3 egress_proxy.py --allowlist allowlist.txt [--listen 0.0.0.0:3128]
                            [--ports 443,80] [--upstream proxy.corp.example:8080]

Allowlist file: one entry per line, ``#`` starts a comment. ``.corp.example`` allows the domain and
every subdomain, ``api.corp.example`` only that host. The file is re-read whenever it changes, so
an edit applies to the next connection without a restart.

HTTPS goes through ``CONNECT host:port`` (tunnelled, never decrypted); plain HTTP through an
absolute-form request, one request per connection. Anything else — a host or port not on the
lists, a malformed request — gets ``403``/``400`` and is logged as ``DENY``. ``--upstream`` chains
every allowed connection through a corporate proxy instead of dialling it directly.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from urllib.parse import urlsplit

log = logging.getLogger("egress")
HEAD_LIMIT = 64 * 1024
HEAD_TIMEOUT = 30
CHUNK = 64 * 1024


class Allowlist:
    """Host allowlist backed by a file that is re-read when its mtime changes."""

    def __init__(self, path: str):
        self.path = path
        self._mtime: int | None = None
        self._exact: frozenset[str] = frozenset()
        self._suffixes: tuple[str, ...] = ()
        self._reload()

    def _reload(self) -> None:
        mtime = os.stat(self.path).st_mtime_ns
        if mtime == self._mtime:
            return
        exact, suffixes = set(), []
        with open(self.path, encoding="utf-8") as fh:
            for raw in fh:
                entry = raw.split("#", 1)[0].strip().lower().rstrip(".")
                if entry.startswith("."):
                    suffixes.append(entry)
                    exact.add(entry[1:])
                elif entry:
                    exact.add(entry)
        self._exact, self._suffixes, self._mtime = frozenset(exact), tuple(suffixes), mtime
        log.info("allowlist loaded: %d hosts, %d domains", len(exact) - len(suffixes), len(suffixes))

    def allows(self, host: str) -> bool:
        try:
            self._reload()
        except OSError as exc:  # keep the last good list rather than opening or closing everything
            log.error("allowlist %s unreadable, keeping the previous one: %s", self.path, exc)
        host = host.lower().rstrip(".")
        return host in self._exact or host.endswith(self._suffixes)


def split_host_port(authority: str, default_port: int) -> tuple[str, int]:
    """``host[:port]`` / ``[v6]:port`` → ``(host, port)``; ValueError when malformed."""
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port_text = rest[1:] if rest.startswith(":") else ""
    else:
        host, sep, port_text = authority.rpartition(":")
        if not sep:
            host, port_text = authority, ""
    port = int(port_text) if port_text else default_port
    if not host or not 0 < port < 65536:
        raise ValueError(authority)
    return host, port


def parse_request(head: bytes) -> tuple[str, str, int, bytes]:
    """Request head → ``(method, host, port, bytes to send upstream)``; ValueError when unusable.

    CONNECT sends nothing (the tunnel starts after our 200). Plain HTTP is rewritten to origin
    form, loses its ``Proxy-*`` headers and is forced to ``Connection: close``."""
    request_line, *headers = head.decode("latin-1").split("\r\n")
    method, target, version = request_line.split(" ")
    if method == "CONNECT":
        host, port = split_host_port(target, 443)
        return method, host, port, b""
    url = urlsplit(target)
    if url.scheme != "http" or not url.hostname:
        raise ValueError(target)
    path = (url.path or "/") + (f"?{url.query}" if url.query else "")
    kept = [h for h in headers if h and not h.lower().startswith(("proxy-", "connection:", "keep-alive:"))]
    forwarded = "\r\n".join([f"{method} {path} {version}", *kept, "Connection: close", "", ""])
    return method, url.hostname, url.port or 80, forwarded.encode("latin-1")


async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while data := await src.read(CHUNK):
            dst.write(data)
            await dst.drain()
    except (ConnectionError, OSError):
        return  # the peer went away; closing below ends the other direction too
    finally:
        dst.close()


async def open_upstream(upstream: tuple[str, int] | None, host: str, port: int):
    """Connection to ``host:port``, directly or through a CONNECT tunnel on ``upstream``."""
    if upstream is None:
        return await asyncio.open_connection(host, port, limit=HEAD_LIMIT)
    reader, writer = await asyncio.open_connection(*upstream, limit=HEAD_LIMIT)
    writer.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode("latin-1"))
    await writer.drain()
    reply = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
    if reply.split(b" ", 2)[1:2] != [b"200"]:
        writer.close()
        raise ConnectionError(f"upstream refused CONNECT {host}:{port}: {reply.splitlines()[0]!r}")
    return reader, writer


class EgressProxy:
    def __init__(self, allowlist: Allowlist, ports: frozenset[int], upstream: tuple[str, int] | None):
        self.allowlist, self.ports, self.upstream = allowlist, ports, upstream

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
            method, host, port, forwarded = parse_request(head)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ValueError, UnicodeError):
            await self.refuse(writer, b"400 Bad Request", f"DENY malformed request from {peer}")
            return
        if port not in self.ports or not self.allowlist.allows(host):
            await self.refuse(writer, b"403 Forbidden", f"DENY {method} {host}:{port}")
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(open_upstream(self.upstream, host, port), HEAD_TIMEOUT)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            await self.refuse(writer, b"502 Bad Gateway", f"FAIL {method} {host}:{port}: {exc}")
            return
        log.info("ALLOW %s %s:%d", method, host, port)
        if method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            up_writer.write(forwarded)
        await asyncio.gather(pump(reader, up_writer), pump(up_reader, writer))

    @staticmethod
    async def refuse(writer: asyncio.StreamWriter, status: bytes, message: str) -> None:
        log.info("%s", message)
        writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass  # the client already left; nothing to tell it
        writer.close()


async def serve(args: argparse.Namespace) -> None:
    host, port = split_host_port(args.listen, 3128)
    upstream = split_host_port(args.upstream, 3128) if args.upstream else None
    ports = frozenset(int(p) for p in args.ports.split(",") if p.strip())
    proxy = EgressProxy(Allowlist(args.allowlist), ports, upstream)
    server = await asyncio.start_server(proxy.handle, host, port, limit=HEAD_LIMIT)
    log.info("listening on %s:%d, ports %s, upstream %s", host, port, sorted(ports), args.upstream or "direct")
    async with server:
        await server.serve_forever()


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--allowlist", required=True)
    parser.add_argument("--listen", default="0.0.0.0:3128")
    parser.add_argument("--ports", default="443,80", help="comma-separated destination ports")
    parser.add_argument("--upstream", default="", help="corporate proxy host:port to chain through")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(message)s")
    asyncio.run(serve(args))


if __name__ == "__main__":
    main(sys.argv[1:])
