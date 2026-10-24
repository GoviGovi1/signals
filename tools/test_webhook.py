"""Local checks for the webhook command path. No Telegram calls, no tokens."""
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_plan_never_polls_while_a_webhook_exists():
    assert bot.serve_plan(True, True, "https://old.example/hook") == "webhook"
    assert bot.serve_plan(True, False, "https://old.example/hook") == "broadcast-only"
    assert bot.serve_plan(False, False, "https://old.example/hook") == "broadcast-only"
    assert bot.serve_plan(True, False, "") == "poll"
    assert bot.serve_plan(False, False, "") == "poll"


def test_getupdates_is_only_in_the_poll_paths():
    lines = open(bot.__file__).read().splitlines()
    owner = None
    hits = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("def "):
            owner = stripped.split("(")[0][4:]
        if 'api("getUpdates"' in line:
            hits.append((owner, i))
    assert [o for o, _ in hits] == ["loop", "poll_once"], hits


def test_secret_rules():
    old = dict(os.environ)
    try:
        os.environ.pop("SIGNALS_WEBHOOK_URL", None)
        os.environ.pop("SIGNALS_WEBHOOK_SECRET", None)
        assert bot.webhook_env() == (None, None)
        os.environ["SIGNALS_WEBHOOK_URL"] = "https://bot.example"
        os.environ["SIGNALS_WEBHOOK_SECRET"] = "short"
        assert bot.webhook_env() == ("https://bot.example", "short")
        os.environ["SIGNALS_WEBHOOK_SECRET"] = "bad secret"
        assert bot.webhook_env() == (None, None)
        os.environ["SIGNALS_WEBHOOK_URL"] = "http://bot.example"
        os.environ["SIGNALS_WEBHOOK_SECRET"] = "ok"
        assert bot.webhook_env() == (None, None)
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_events_and_tombstones():
    subs = {"9": {"joined": 10, "type": "private"}}
    assert bot.apply_event(subs, {"op": "stop", "chat": "9", "ts": 200})
    assert subs["9"]["stopped"] is True
    assert not bot.apply_event(subs, {"op": "start", "chat": "9", "ts": 100, "joined": 100, "type": "private"})
    assert subs["9"]["stopped"] is True
    assert bot.apply_event(subs, {"op": "start", "chat": "9", "ts": 300, "joined": 300, "type": "group"})
    assert bot.is_live(subs["9"])
    assert bot.live_ids(subs) == ["9"]
    subs["1"] = {"stopped": True, "ts": 1}
    pruned = bot.prune_subs(subs, now=1 + bot.TOMBSTONE_TTL + 1)
    assert "1" not in pruned and "9" in pruned
    parts = bot.text_parts("a" * 50 + "\n" + "b" * 50, limit=60)
    assert all(len(p) <= 60 for p in parts)
    assert "".join(parts) == "a" * 50 + "b" * 50


def test_reply_text_matches_the_live_formatters():
    bundle = bot.reply_bundle()
    assert set(bot.REPLY_KEYS) <= set(bundle)
    assert bundle["help"] == bot.help_text()
    assert bundle["stop"] == bot.STOP_TEXT
    assert bundle["welcome"] == bot.welcome()
    assert bundle["signals"] == bot.open_text()
    assert bundle["stats"] == bot.stats_text()
    fb = json.load(open(os.path.join(ROOT, "worker", "fallback.json")))
    assert fb["help"] == bot.help_text()
    assert fb["welcome"] == bot.welcome()
    assert fb["stop"] == bot.STOP_TEXT


class Handler(BaseHTTPRequestHandler):
    def _auth(self):
        return self.headers.get("Authorization") == "Bearer " + self.server.secret

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            self._send(200 if self.server.healthy else 503, {"ok": self.server.healthy})
            return
        if self.path == "/admin/events":
            if not self._auth():
                self._send(401, {})
                return
            self._send(200, {"events": list(self.server.events)})
            return
        self._send(404, {})

    def do_POST(self):
        if not self._auth():
            self._send(401, {})
            return
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/admin/events/ack":
            keys = set(body.get("keys") or [])
            self.server.events = [e for e in self.server.events if e["key"] not in keys]
            self.server.acked.extend([k for k in body.get("keys") or [] if str(k).startswith("ev:")])
            self._send(200, {"ok": True, "deleted": [k for k in body.get("keys") or [] if str(k).startswith("ev:")]})
            return
        if self.path == "/admin/replies":
            self.server.replies = body
            self._send(200, {"ok": True})
            return
        self._send(404, {})

    def log_message(self, fmt, *args):
        return


def test_sync_roundtrip(tmp):
    bot.SUBS = os.path.join(tmp, "subscribers.json")
    bot.STATE = os.path.join(tmp, "bot_state.json")
    bot.REPLIES = os.path.join(tmp, "replies.json")
    bot.jsave(bot.SUBS, {"7": {"joined": 1, "type": "private"}})
    bot.jsave(bot.STATE, {})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.secret = "s3cret"
    httpd.healthy = True
    now = bot.now_ms()
    stop7 = f"ev:{now - 100:013d}:7:stop:aa"
    httpd.events = [
        {"key": stop7, "op": "stop", "chat": "7", "ts": now - 100, "joined": 1, "type": "private"},
        {"key": f"ev:{now - 300:013d}:8:start:bb", "op": "start", "chat": "8", "ts": now - 300, "joined": 100, "type": "private"},
        {"key": f"ev:{now - 50:013d}:8:stop:cc", "op": "stop", "chat": "8", "ts": now - 50, "joined": 100, "type": "private"},
    ]
    httpd.acked = []
    httpd.replies = None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{httpd.server_address[1]}"
    os.environ["SIGNALS_WEBHOOK_URL"] = host
    os.environ["SIGNALS_WEBHOOK_SECRET"] = "s3cret"
    try:
        assert bot.worker_healthy(host)
        httpd.healthy = False
        assert not bot.worker_healthy(host)
        httpd.healthy = True
        bundle = {k: k + "-text" for k in bot.REPLY_KEYS}
        assert bot.push_replies(bundle)
        assert httpd.replies["signals"] == "signals-text"
        assert bot.jload(bot.STATE, {}).get("replies_fp")
        assert bot.push_replies(bundle)  # unchanged: no error, still one body
        n = bot.sync_events()
        assert n == 3
        subs = bot.jload(bot.SUBS, {})
        assert subs["7"]["stopped"] is True
        assert subs["8"]["stopped"] is True
        assert bot.live_ids(subs) == []
        assert bot.jload(bot.STATE, {})["ack_events"]
        # second pass acks the keys and does not resurrect chat 7 from an older start
        httpd.events = [{"key": f"ev:{now - 500:013d}:7:start:dd", "op": "start", "chat": "7",
                         "ts": now - 500, "joined": 50, "type": "private"}]
        bot.sync_events()
        assert bot.jload(bot.SUBS, {})["7"]["stopped"] is True
        assert stop7 in httpd.acked
    finally:
        httpd.shutdown()
        os.environ.pop("SIGNALS_WEBHOOK_URL", None)
        os.environ.pop("SIGNALS_WEBHOOK_SECRET", None)


def main():
    test_plan_never_polls_while_a_webhook_exists()
    test_getupdates_is_only_in_the_poll_paths()
    test_secret_rules()
    test_events_and_tombstones()
    test_reply_text_matches_the_live_formatters()
    with tempfile.TemporaryDirectory() as tmp:
        test_sync_roundtrip(tmp)
    print("webhook tests passed")


if __name__ == "__main__":
    main()
