#!/usr/bin/env python3
"""
ollama-nothink-proxy: sits in front of Ollama and turns off "thinking" on every
chat request, so small reasoning models (e.g. qwen3.5:4b) answer straight away.

  client (Obsidian Copilot, etc.)  ->  :11435 (this proxy)  ->  Ollama :11434

- Native API (/api/chat, /api/generate): adds "think": false
- OpenAI-compatible API (/v1/chat/completions, /v1/completions): adds
  "reasoning_effort": "none"; if Ollama rejects that, the original request is
  retried unchanged, so the proxy never breaks a request it couldn't improve.
- Everything else (model lists, /v1/models, pulls, CORS preflights) passes
  straight through. Streaming responses are forwarded as they arrive.

Standard library only. Settings via environment variables:
  PROXY_LISTEN   default 0.0.0.0:11435
  OLLAMA_UPSTREAM default http://127.0.0.1:11434
"""

import http.client
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

LISTEN = os.environ.get("PROXY_LISTEN", "0.0.0.0:11435")
UPSTREAM = urlsplit(os.environ.get("OLLAMA_UPSTREAM", "http://127.0.0.1:11434"))

NATIVE_PATHS = {"/api/chat", "/api/generate"}
OPENAI_PATHS = {"/v1/chat/completions", "/v1/completions"}
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host"}


def patch_body(path, body):
    """Return (patched_body, was_patched)."""
    route = path.split("?", 1)[0]
    if route not in NATIVE_PATHS and route not in OPENAI_PATHS:
        return body, False
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return body, False
    if not isinstance(data, dict):
        return body, False
    if route in NATIVE_PATHS:
        data["think"] = False
    else:
        data["reasoning_effort"] = "none"
    return json.dumps(data).encode(), True


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter journal: one line per request
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send_upstream(self, body):
        conn = http.client.HTTPConnection(UPSTREAM.hostname, UPSTREAM.port or 80, timeout=600)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["Host"] = UPSTREAM.netloc
        if body is not None:
            headers["Content-Length"] = str(len(body))
        conn.request(self.command, self.path, body=body, headers=headers)
        return conn, conn.getresponse()

    def _relay(self):
        length = int(self.headers.get("Content-Length") or 0)
        original = self.rfile.read(length) if length else None
        body, patched = patch_body(self.path, original) if original else (original, False)

        try:
            conn, resp = self._send_upstream(body)
            # OpenAI route: if Ollama didn't like the extra field, retry untouched.
            if patched and resp.status == 400 and self.path.split("?", 1)[0] in OPENAI_PATHS:
                resp.read()
                conn.close()
                conn, resp = self._send_upstream(original)
        except OSError as err:
            msg = json.dumps({"error": f"proxy can't reach Ollama at {UPSTREAM.netloc}: {err}"}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in HOP_BY_HOP:
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client gave up mid-stream (e.g. pressed stop)
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = _relay


def main():
    host, port = LISTEN.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), Proxy)
    server.daemon_threads = True
    print(f"ollama-nothink-proxy: {LISTEN} -> {UPSTREAM.geturl()}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
