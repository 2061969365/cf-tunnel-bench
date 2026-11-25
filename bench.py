#!/usr/bin/env python3
"""cf-tunnel-bench client.

Measures sustained HTTP download throughput to a target URL using N parallel
HTTP/1.1 connections for a fixed duration, discarding the ramp-up window.

Targets:
    --url https://<tunnel-host>/<token>/stream   main metric (tunnel)
    --edge-down                                  control: this host -> Cloudflare edge
    --edge-up                                    control: this host -> Cloudflare edge (upload)
    --url http://127.0.0.1:PORT/<token>/stream   control: origin ceiling (loopback)

Stdlib only, so it runs unchanged on Windows and on a GitHub runner.

    python bench.py --url "$URL" --seconds 20 --streams 1,4,8 --reps 3 \
        --warmup-seconds 4 --json out/tunnel.json --md out/tunnel.md
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import random
import socket
import ssl
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

TOOL = "cf-tunnel-bench"
VERSION = "1.0.0"
READ_SIZE = 65536
STALL_TIMEOUT = 5.0
CONNECT_TIMEOUT = 15.0
EDGE_DOWN_URL = "https://speed.cloudflare.com/__down?bytes=25000000"
EDGE_UP_URL = "https://speed.cloudflare.com/__up"
EDGE_MAX_BYTES = 25000000


class HttpStatusError(Exception):
    def __init__(self, status: int, body: bytes = b""):
        self.status = status
        self.body = body
        super().__init__("HTTP %d" % status)


def classify_error(exc: BaseException) -> str:
    if isinstance(exc, HttpStatusError):
        return "http_%d" % exc.status
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, ssl.SSLError):
        return "tls"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError,
                        ConnectionAbortedError, BrokenPipeError)):
        return "conn"
    if isinstance(exc, http.client.HTTPException):
        return "http"
    if isinstance(exc, OSError):
        return "os"
    return type(exc).__name__


def make_spec(url: str) -> dict:
    u = urlsplit(url)
    scheme = (u.scheme or "https").lower()
    if scheme not in ("http", "https"):
        raise SystemExit("unsupported scheme: %r" % scheme)
    if not u.hostname:
        raise SystemExit("URL has no host: %r" % url)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return {
        "scheme": scheme,
        "host": u.hostname,
        "port": u.port or (443 if scheme == "https" else 80),
        "path": path,
        "headers": {
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "%s/%s" % (TOOL, VERSION),
        },
    }


def with_nonce(path: str) -> str:
    sep = "&" if "?" in path else "?"
    return "%s%sn=%d-%06d" % (path, sep, time.time_ns(), random.randrange(1 << 20))


def connect(spec: dict, timeout: float):
    if spec["scheme"] == "https":
        ctx = ssl.create_default_context()
        return http.client.HTTPSConnection(spec["host"], spec["port"], timeout=timeout, context=ctx)
    return http.client.HTTPConnection(spec["host"], spec["port"], timeout=timeout)


def stream_worker(spec: dict, deadline: float, skip_at: float, out: list) -> None:
    total = 0
    skip_bytes = None
    requests = 0
    ttfb_ms = []
    errors: dict = {}
    first_headers = None
    http_version = None
    status_ok = False
    last_error = None

    while True:
        if time.monotonic() >= deadline:
            break
        conn = None
        try:
            conn = connect(spec, CONNECT_TIMEOUT)
            t0 = time.monotonic()
            conn.request("GET", with_nonce(spec["path"]), headers=spec["headers"])
            resp = conn.getresponse()
            if resp.status != 200:
                raise HttpStatusError(resp.status, resp.read(256))
            status_ok = True
            if first_headers is None:
                first_headers = {k.lower(): v for k, v in resp.getheaders()}
                http_version = resp.version
            if conn.sock is not None:
                conn.sock.settimeout(STALL_TIMEOUT)
            first_read = True
            while True:
                if time.monotonic() >= deadline:
                    break
                if first_read:
                    ttfb_ms.append((time.monotonic() - t0) * 1000.0)
                    first_read = False
                chunk = resp.read1(READ_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if skip_bytes is None and time.monotonic() >= skip_at:
                    skip_bytes = total
            requests += 1
        except HttpStatusError as exc:
            key = "http_%d" % exc.status
            errors[key] = errors.get(key, 0) + 1
            last_error = "%s body=%r" % (exc, exc.body[:120])
            break
        except BaseException as exc:
            kind = classify_error(exc)
            errors[kind] = errors.get(kind, 0) + 1
            last_error = "%s: %s" % (type(exc).__name__, exc)
            if kind in ("dns", "tls"):
                break
            time.sleep(0.1)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    end = time.monotonic()
    steady = None
    if skip_bytes is not None and end > skip_at + 0.05:
        steady = (total - skip_bytes) / (end - skip_at)
    out.append({
        "bytes": total,
        "skip_bytes": skip_bytes,
        "steady_bytes_per_s": steady,
        "requests": requests,
        "ttfb_ms": round(statistics.median(ttfb_ms), 2) if ttfb_ms else None,
        "errors": errors,
        "status_ok": status_ok,
        "http_version": http_version,
        "first_headers": first_headers,
        "last_error": last_error,
    })


def run_sample(spec: dict, n_streams: int, seconds: float, skip: float) -> dict:
    start = time.monotonic()
    deadline = start + seconds
    skip_at = start + skip
    results: list = []
    threads = [
        threading.Thread(target=stream_worker, args=(spec, deadline, skip_at, results), daemon=True)
        for _ in range(n_streams)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=seconds + CONNECT_TIMEOUT + 10)
    elapsed = time.monotonic() - start

    window = max(0.1, seconds - skip)
    steady_bytes = 0.0
    measured_streams = 0
    for r in results:
        if r["skip_bytes"] is not None and r["bytes"] >= r["skip_bytes"]:
            steady_bytes += r["bytes"] - r["skip_bytes"]
            measured_streams += 1
    total_bytes = sum(r["bytes"] for r in results)

    errors: dict = {}
    for r in results:
        for k, v in r["errors"].items():
            errors[k] = errors.get(k, 0) + v

    first_headers = next((r["first_headers"] for r in results if r["first_headers"]), None) or {}
    colo = None
    cf_ray = first_headers.get("cf-ray")
    if cf_ray and "-" in cf_ray:
        colo = cf_ray.rsplit("-", 1)[-1]

    per_stream = []
    for r in results:
        mbps = None
        if r["skip_bytes"] is not None and r["bytes"] >= r["skip_bytes"]:
            mbps = round((r["bytes"] - r["skip_bytes"]) * 8.0 / window / 1e6, 3)
        per_stream.append({
            "mbps": mbps,
            "bytes": r["bytes"],
            "requests": r["requests"],
            "ttfb_ms": r["ttfb_ms"],
            "errors": r["errors"],
            "last_error": r["last_error"],
        })

    cache_status = (first_headers.get("cf-cache-status") or "").upper()
    status_ok = any(r["status_ok"] for r in results)
    mbps = steady_bytes * 8.0 / window / 1e6
    reason = None
    if measured_streams == 0:
        reason = "no steady-state bytes"
    elif not status_ok:
        reason = "no HTTP 200"
    elif cache_status == "HIT":
        reason = "cf-cache-status=HIT (edge served, not origin)"

    return {
        "streams": n_streams,
        "seconds": round(seconds, 3),
        "skip": round(skip, 3),
        "elapsed": round(elapsed, 3),
        "measured_streams": measured_streams,
        "mbps": round(mbps, 3),
        "mib_per_s": round(steady_bytes / window / (1 << 20), 3),
        "all_mbps": round(total_bytes * 8.0 / max(elapsed, 1e-3) / 1e6, 3),
        "steady_bytes": int(steady_bytes),
        "total_bytes": total_bytes,
        "per_stream": per_stream,
        "errors": errors,
        "http_version": next((r["http_version"] for r in results if r["http_version"]), None),
        "cf_ray": cf_ray,
        "colo": colo,
        "cf_cache_status": cache_status or None,
        "server": first_headers.get("server"),
        "content_type": first_headers.get("content-type"),
        "transfer_encoding": first_headers.get("transfer-encoding"),
        "valid": reason is None,
        "invalid_reason": reason,
    }


def run_upload(spec: dict, nbytes: int, reps: int) -> list:
    block = os.urandom(1 << 20)
    body = (block * (nbytes // len(block) + 1))[:nbytes]
    samples = []
    for rep in range(1, reps + 1):
        conn = None
        try:
            conn = connect(spec, CONNECT_TIMEOUT)
            t0 = time.monotonic()
            conn.request("POST", with_nonce(spec["path"]), body=body,
                         headers={**spec["headers"], "Content-Type": "application/octet-stream"})
            resp = conn.getresponse()
            resp.read()
            elapsed = time.monotonic() - t0
            ok = resp.status == 200
            samples.append({
                "rep": rep, "bytes": nbytes, "seconds": round(elapsed, 3),
                "mbps": round(nbytes * 8.0 / max(elapsed, 1e-3) / 1e6, 3),
                "valid": ok, "status": resp.status,
                "invalid_reason": None if ok else "HTTP %d" % resp.status,
                "errors": {},
            })
        except BaseException as exc:
            samples.append({
                "rep": rep, "bytes": nbytes, "seconds": None, "mbps": 0.0,
                "valid": False, "status": None,
                "invalid_reason": "%s: %s" % (type(exc).__name__, exc),
                "errors": {classify_error(exc): 1},
            })
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return samples


def summarize(samples: list) -> dict:
    groups: dict = {}
    for s in samples:
        if not s.get("valid"):
            continue
        groups.setdefault(s["streams"], []).append(s["mbps"])
    out = {}
    for n, vals in sorted(groups.items()):
        mean = statistics.fmean(vals)
        out[str(n)] = {
            "count": len(vals),
            "p50": round(statistics.median(vals), 3),
            "max": round(max(vals), 3),
            "min": round(min(vals), 3),
            "mean": round(mean, 3),
            "cv": round(statistics.pstdev(vals) / mean, 4) if mean > 0 and len(vals) > 1 else None,
        }
    return out


def render_md(meta: dict, samples: list) -> str:
    tgt = meta["target"]
    lines = []
    lines.append("#### %s" % tgt["label"])
    lines.append("")
    lines.append("- target: `%s://%s:%d` (path hidden)" % (tgt["scheme"], tgt["host"], tgt["port"]))
    cfg = meta["config"]
    lines.append("- sample: %ss, skip %ss, streams `%s`, reps %d" %
                 (cfg["seconds"], cfg["skip"], ",".join(str(s) for s in cfg["streams"]), cfg["reps"]))
    if meta.get("runner_edge"):
        lines.append("- runner edge: `%s`" % meta["runner_edge"])
    lines.append("")

    colos = sorted({s["colo"] for s in samples if s.get("colo")})
    caches = sorted({s["cf_cache_status"] for s in samples if s.get("cf_cache_status")})
    lines.append("- cf colo: `%s` | cf-cache-status: `%s` | http: `%s`" %
                 (",".join(colos) or "?", ",".join(caches) or "?", samples[0].get("http_version") if samples else "?"))
    lines.append("")

    lines.append("| streams | rep | Mbps | MiB/s | per-stream Mbps | errors | valid |")
    lines.append("|---:|---:|---:|---:|---|---|---|")
    for s in samples:
        per = ", ".join("%.1f" % p["mbps"] for p in s["per_stream"] if p["mbps"] is not None) or "-"
        errs = ", ".join("%s=%d" % (k, v) for k, v in s["errors"].items()) or "-"
        valid = "yes" if s["valid"] else "no: %s" % s["invalid_reason"]
        lines.append("| %d | %s | %.1f | %.2f | %s | %s | %s |" %
                     (s["streams"], s.get("rep", "-"), s["mbps"], s["mib_per_s"], per, errs, valid))
    lines.append("")

    summary = meta["summary"]
    if summary:
        lines.append("| streams | n | p50 Mbps | max Mbps | min Mbps | cv |")
        lines.append("|---:|---:|---:|---:|---:|---:|")
        for n, v in summary.items():
            cv = "%.2f" % v["cv"] if v["cv"] is not None else "-"
            lines.append("| %s | %d | %.1f | %.1f | %.1f | %s |" %
                         (n, v["count"], v["p50"], v["max"], v["min"], cv))
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="cf-tunnel-bench client")
    ap.add_argument("--url", help="download target URL (tunnel or origin)")
    ap.add_argument("--edge-down", action="store_true", help="control: Cloudflare edge download")
    ap.add_argument("--edge-up", action="store_true", help="control: Cloudflare edge upload")
    ap.add_argument("--upload-bytes", type=int, default=25_000_000)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--skip", type=float, default=3.0)
    ap.add_argument("--streams", default="1,4,8")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--warmup-seconds", type=float, default=0.0)
    ap.add_argument("--label", default="")
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--md", dest="md_path")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    chosen = [x for x in (args.url, args.edge_down, args.edge_up) if x]
    if len([1 for x in (args.url, args.edge_down, args.edge_up) if x]) != 1:
        ap.error("pick exactly one of --url / --edge-down / --edge-up")

    if args.edge_down:
        url, label = EDGE_DOWN_URL, args.label or "control: edge down"
    elif args.edge_up:
        url, label = EDGE_UP_URL, args.label or "control: edge up"
    else:
        url, label = args.url, args.label or "target"

    streams = [int(x) for x in str(args.streams).split(",") if x.strip()]
    if not streams or any(n < 1 for n in streams):
        ap.error("--streams must be positive integers, e.g. 1,4,8")
    reps = max(1, args.reps)
    seconds = max(1.0, args.seconds)
    skip = max(0.0, min(args.skip, seconds * 0.4))

    spec = make_spec(url)
    proxy_env = sorted(k for k in os.environ if k.lower() in ("http_proxy", "https_proxy", "all_proxy"))
    if proxy_env and not args.quiet:
        sys.stderr.write("note: %s set, but http.client ignores proxy env; traffic is direct\n"
                         % ",".join(proxy_env))
    meta = {
        "tool": TOOL,
        "version": VERSION,
        "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "client": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "hostname": socket.gethostname(),
        },
        "target": {"label": label, "scheme": spec["scheme"], "host": spec["host"], "port": spec["port"]},
        "config": {"seconds": seconds, "skip": skip, "streams": streams, "reps": reps},
    }

    if args.warmup_seconds > 0 and not args.edge_up:
        if not args.quiet:
            sys.stderr.write("warm-up %ss ...\n" % args.warmup_seconds)
        warm = run_sample(spec, 1, max(1.0, args.warmup_seconds), 0.0)
        if not args.quiet:
            sys.stderr.write("warm-up: %.1f Mbps (status %s)\n" %
                             (warm["mbps"], "ok" if warm["valid"] else warm["invalid_reason"]))

    samples: list = []
    if args.edge_up:
        ups = run_upload(spec, args.upload_bytes, reps)
        meta["samples"] = ups
        meta["summary"] = {}
        best = max((u["mbps"] for u in ups if u["valid"]), default=0.0)
        if not args.quiet:
            for u in ups:
                print("[%s] rep %d %s" % (label, u["rep"], ("%.1f Mbps" % u["mbps"]) if u["valid"] else u["invalid_reason"]))
            print("[%s] best %.1f Mbps over %d reps" % (label, best, reps))
    else:
        for rep in range(1, reps + 1):
            for n in streams:
                s = run_sample(spec, n, seconds, skip)
                s["rep"] = rep
                samples.append(s)
                if not args.quiet:
                    extra = "" if s["valid"] else "  (%s)" % s["invalid_reason"]
                    print("[%s] streams=%d rep=%d  %.1f Mbps  %.2f MiB/s%s" %
                          (label, n, rep, s["mbps"], s["mib_per_s"], extra))
        meta["samples"] = samples
        meta["summary"] = summarize(samples)

    if args.json_path:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_path)) or ".", exist_ok=True)
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
    if args.md_path:
        os.makedirs(os.path.dirname(os.path.abspath(args.md_path)) or ".", exist_ok=True)
        with open(args.md_path, "w", encoding="utf-8") as fh:
            fh.write(render_md(meta, samples) + "\n")
    elif not args.edge_up and not args.quiet:
        print()
        print(render_md(meta, samples))

    valid = any(s.get("valid") for s in meta["samples"])
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
