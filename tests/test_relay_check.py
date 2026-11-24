"""Offline tests: a local HTTP server plays well-behaved and broken endpoints.

Run: python3 -m unittest discover -s tests
"""

import contextlib
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import relay_check as rc  # noqa: E402

GOOD_KEY = "sk-good"


class Handler(BaseHTTPRequestHandler):
    mode = "good"  # good | html | accepts_any_key | inflated_usage | buffered | no_sse | echo_key | no_stream_options
    slow_first = False

    def log_message(self, *args):
        pass

    def _key(self):
        auth = self.headers.get("authorization", "")
        return self.headers.get("x-api-key") or (auth[7:] if auth.startswith("Bearer ") else "")

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        mode = type(self).mode
        if mode == "html":
            body = b"<!doctype html><html><body>shop</body></html>"
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self._key() != GOOD_KEY and mode != "accepts_any_key":
            self._json(401, {"error": {"type": "authentication_error", "message": "invalid key"}})
            return
        if mode == "echo_key":
            self._json(400, {"error": {"message": "bad request for key " + self._key()}})
            return
        if mode == "no_stream_options" and "stream_options" in req:
            self._json(400, {"error": {"message": "unknown field stream_options"}})
            return
        anthropic = self.path == "/v1/messages"
        in_tok = 900 if mode == "inflated_usage" else 12
        if req.get("stream") and mode != "no_sse":
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            if mode == "buffered" or type(self).slow_first:
                time.sleep(1.2)
            words = ["a", "b", "c", "d", "e", "f"] if mode in ("buffered", "long") else ["o", "k", "!"]
            for word in words:
                if anthropic:
                    ev = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": word}}
                else:
                    ev = {"choices": [{"delta": {"content": word}}]}
                self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                self.wfile.flush()
                if mode != "buffered" and not type(self).slow_first:
                    time.sleep(0.05)
            if not anthropic:
                self.wfile.write(b"data: [DONE]\n\n")
            return
        if anthropic:
            self._json(200, {"model": req["model"], "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": in_tok, "output_tokens": 1}})
        else:
            self._json(200, {"model": req["model"], "choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": in_tok, "completion_tokens": 1}})


@contextlib.contextmanager
def server(mode):
    Handler.mode = mode
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield "http://127.0.0.1:%d" % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def statuses(checks):
    return [c.status for c in checks]


class BuildURLTest(unittest.TestCase):
    def test_v1_not_doubled(self):
        self.assertEqual(rc.build_url("https://h/v1", "/v1/messages"), "https://h/v1/messages")
        self.assertEqual(rc.build_url("https://h/", "/v1/messages"), "https://h/v1/messages")
        self.assertEqual(rc.build_url("https://h/api", "/v1/messages"), "https://h/api/v1/messages")


class InvalidKeyTest(unittest.TestCase):
    def test_json_401_passes(self):
        with server("good") as base:
            for p in ("anthropic", "openai"):
                self.assertEqual(rc.check_invalid_key(base, p, 5).status, rc.PASS, p)

    def test_html_fails(self):
        with server("html") as base:
            c = rc.check_invalid_key(base, "anthropic", 5)
            self.assertEqual(c.status, rc.FAIL)
            self.assertIn("HTML", c.detail)

    def test_invalid_key_accepted_fails(self):
        with server("accepts_any_key") as base:
            c = rc.check_invalid_key(base, "openai", 5)
            self.assertEqual(c.status, rc.FAIL)
            self.assertIn("accepted", c.detail)

    def test_unreachable_fails(self):
        c = rc.check_invalid_key("http://127.0.0.1:9", "openai", 2)
        self.assertEqual(c.status, rc.FAIL)


class UsageTest(unittest.TestCase):
    def test_normal_usage_passes(self):
        with server("good") as base:
            for p in ("anthropic", "openai"):
                c = rc.check_usage(base, p, GOOD_KEY, "m", 5)
                self.assertEqual(c.status, rc.PASS, (p, c.detail))

    def test_inflated_usage_warns(self):
        with server("inflated_usage") as base:
            c = rc.check_usage(base, "openai", GOOD_KEY, "m", 5)
            self.assertEqual(c.status, rc.WARN)
            self.assertIn("input_tokens=900", c.detail)


class StreamTest(unittest.TestCase):
    def test_stream_passes_with_ttft(self):
        with server("good") as base:
            for p in ("anthropic", "openai"):
                c = rc.check_stream(base, p, GOOD_KEY, "m", 5)
                self.assertEqual(c.status, rc.PASS, (p, c.detail))
                self.assertEqual(c.data["text_events"], 3)
                self.assertIsNotNone(c.data["ttft_ms"])

    def test_stream_ignored_fails(self):
        # 端点忽略 stream=true、直接回一整段 JSON
        with server("no_sse") as base:
            c = rc.check_stream(base, "openai", GOOD_KEY, "m", 5)
            self.assertEqual(c.status, rc.FAIL)
            self.assertIn("text/event-stream", c.detail)

    def test_short_reply_after_slow_start_is_not_buffered(self):
        # 回复很短（3 个事件）时间隔本来就小，不能因为首 token 慢就判「缓冲」
        Handler.slow_first = True
        try:
            with server("good") as base:
                c = rc.check_stream(base, "anthropic", GOOD_KEY, "m", 5)
                self.assertEqual(c.status, rc.PASS, c.detail)
        finally:
            Handler.slow_first = False

    def test_stream_options_rejected_retries(self):
        with server("no_stream_options") as base:
            c = rc.check_stream(base, "openai", GOOD_KEY, "m", 5)
            self.assertEqual(c.status, rc.PASS, c.detail)

    def test_buffered_stream_warns(self):
        with server("buffered") as base:
            c = rc.check_stream(base, "anthropic", GOOD_KEY, "m", 5)
            self.assertEqual(c.status, rc.WARN, c.detail)


class RunTest(unittest.TestCase):
    def test_without_key_only_invalid_key_checks(self):
        with server("good") as base:
            args = rc.parse_args([base])
            self.assertEqual(statuses(rc.run(args)), [rc.PASS, rc.PASS, rc.SKIP])

    def test_full_run_and_exit_code(self):
        with server("good") as base:
            code = rc.main([base, "--key", GOOD_KEY, "--anthropic-model", "a", "--openai-model", "o", "--json"])
            self.assertEqual(code, 0)
        with server("html") as base:
            self.assertEqual(rc.main([base, "--json"]), 1)

    def test_echoed_key_redacted(self):
        # 端点在错误信息里回显 Key：报告里不能出现
        with server("echo_key") as base:
            import io
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc.main([base, "--key", GOOD_KEY, "--openai-model", "o", "--protocol", "openai", "--json"])
            self.assertNotIn(GOOD_KEY, out.getvalue())
            self.assertIn("***", out.getvalue())

    def test_key_not_in_report(self):
        with server("good") as base:
            args = rc.parse_args([base, "--key", GOOD_KEY, "--openai-model", "o", "--protocol", "openai"])
            report = json.dumps([c.__dict__ for c in rc.run(args)])
            self.assertNotIn(GOOD_KEY, report)


if __name__ == "__main__":
    unittest.main()
