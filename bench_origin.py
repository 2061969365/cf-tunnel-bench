#!/usr/bin/env python3
"""cf-tunnel-bench origin server.

Serves an endless, incompressible, non-cacheable byte stream so an external
client can measure sustained download throughput through a tunnel.

Stdlib only. Binds to loopback by default; expose it through cloudflared,
never directly.

    python bench_origin.py --port 18080 --token deadbeefdeadbeef

Routes (token is mandatory):
    /<token>/healthz   -> JSON
    /<token>/stream    -> chunked octet-stream, capped at --max-gb
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

TOOL = "cf-tunnel-bench"
VERSION = "1.0.0"
BLOCK_SIZE = 1 << 20
MAX_BYTES_DEFAULT_GB = 4
CHUNK_HEADER = b"%x\r\n" % BLOCK_SIZE
CHUNK_TAIL = b"\r\n"


class Config:
    token = ""
    block = b""
    max_bytes = MAX_BYTES_DEFAULT_GB << 30
    quiet = False


def _random_block() -> bytes:
    try:
        return os.urandom(BLOCK_SIZE)
    except NotImplementedError:
        return bytes(range(256)) * (BLOCK_SIZE // 256)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{TOOL}/{VERSION}"

    def log_message(self, fmt, *args):
        if not Config.quiet:
            sys.stderr.write("[origin] %s\n" % (fmt % args))

    def _parts(self):
        return [p for p in urlsplit(self.path).path.split("/") if p]

    def _write_head(self, status: int, ctype: str, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        parts = self._parts()
        if not parts or parts[0] != Config.token:
            self._write_head(404, "text/plain; charset=utf-8", {"Content-Length": "10"})
            self.wfile.write(b"not found\n")
            return
        tail = parts[1] if len(parts) > 1 else "stream"
        if tail == "healthz":
            body = json.dumps(
                {
                    "ok": True,
                    "block": BLOCK_SIZE,
                    "maxGb": Config.max_bytes >> 30,
                    "version": VERSION,
                }
            ).encode()
            self._write_head(200, "application/json", {"Content-Length": str(len(body))})
            self.wfile.write(body)
            return
        if tail != "stream":
            self._write_head(404, "text/plain; charset=utf-8", {"Content-Length": "10"})
            self.wfile.write(b"not found\n")
            return
        self._stream()

    def _stream(self):
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
        except OSError:
            pass
        self._write_head(
            200,
            "application/octet-stream",
            {"Transfer-Encoding": "chunked", "X-Content-Type-Options": "nosniff"},
        )
        wfile = self.wfile
        sent = 0
        blocks = 0
        try:
            while sent < Config.max_bytes:
                wfile.write(CHUNK_HEADER)
                wfile.write(Config.block)
                wfile.write(CHUNK_TAIL)
                sent += BLOCK_SIZE
                blocks += 1
                if blocks % 8 == 0:
                    wfile.flush()
            wfile.write(b"0\r\n\r\n")
            wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.close_connection = True
            if not Config.quiet:
                sys.stderr.write("[origin] stream closed after %d MiB\n" % (sent >> 20))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="cf-tunnel-bench origin server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--token", default="", help="path token; random 16 hex chars if omitted")
    ap.add_argument("--max-gb", type=int, default=MAX_BYTES_DEFAULT_GB,
                    help="per-response cap in GiB (safety valve)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    Config.token = args.token or os.urandom(8).hex()
    Config.block = _random_block()
    Config.max_bytes = max(1, args.max_gb) << 30
    Config.quiet = args.quiet

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    srv.request_queue_size = 128
    base = "http://%s:%d/%s" % (args.host, args.port, Config.token)
    print(json.dumps({"tool": TOOL, "version": VERSION, "listen": "%s:%d" % (args.host, args.port),
                      "token": Config.token, "healthz": base + "/healthz",
                      "stream": base + "/stream"}), flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
