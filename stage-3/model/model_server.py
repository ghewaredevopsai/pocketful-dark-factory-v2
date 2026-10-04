"""Serve the reference model over HTTP (self-check target for driver.py; not the product).

  PORT=18090 python3 model_server.py              # faithful model
  PORT=18091 MODEL_BUG=private_leak python3 model_server.py   # fault injection, driver must catch it
  bugs: private_leak | overdraft | replay_reexecutes | held_ignored | no_expiry | capture_closed
        | no_hist_check | asof_exclusive | stmt_page_balance | snapshot_live | known_at_ignored
        | linked_mutable | stale_ignored | snapshot_live_amount
  MODEL_STAGE=1|2 serves earlier stages' shapes (targets for --stage1-base / --stage2-base)
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from model import Model

MODEL = Model(lenient=False, bug=os.environ.get("MODEL_BUG") or None, stage=int(os.environ.get("MODEL_STAGE", "3")))
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _go(self):
        u = urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        with LOCK:
            MODEL.expire_due(time.time())
            res = MODEL.handle(self.command, unquote(u.path), u.query, dict(self.headers), raw)
        if res.ok:
            status, body = res.status, res.body
        else:
            status, code = sorted(res.alts)[0]
            body = {"error": {"code": code, "message": code.replace("_", " ")}}
        data = b"" if status == 204 else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _go

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
