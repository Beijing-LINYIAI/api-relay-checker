#!/usr/bin/env python3
"""api-relay-checker: check any OpenAI / Anthropic compatible endpoint.

Neutral by design: the same checks run against every endpoint. There is no
built-in ranking, no site list, and no special case for any site.

Only the Python 3.8+ standard library is used.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Optional

__version__ = "0.1.0"

USER_AGENT = "api-relay-checker/" + __version__
ANTHROPIC_VERSION = "2023-06-01"
INVALID_KEY = "sk-invalid-key-for-relay-check"
PROMPT = "Reply with the single word: ok"

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    data: dict = field(default_factory=dict)


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes
    elapsed_ms: int
    error: str = ""

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None


def build_url(base: str, path: str) -> str:
    """Join base and path without doubling /v1.

    Both "https://host" and "https://host/v1" are accepted as base.
    """
    base = base.rstrip("/")
    if base.endswith("/v1") and path.startswith("/v1/"):
        base = base[: -len("/v1")]
    return base + path


def post(url: str, headers: dict, payload: dict, timeout: float) -> Response:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("user-agent", USER_AGENT)
    for k, v in headers.items():
        req.add_header(k, v)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, body, int((time.monotonic() - start) * 1000))
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, body, int((time.monotonic() - start) * 1000))
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        return Response(0, {}, b"", int((time.monotonic() - start) * 1000), error=describe_error(e))


def describe_error(e: Exception) -> str:
    msg = str(e)
    if "CERTIFICATE_VERIFY_FAILED" in msg:
        msg += " (Python cannot find CA certificates; see README, section 'Certificate errors')"
    return msg


def looks_like_html(resp: Response) -> bool:
    if "text/html" in resp.content_type:
        return True
    head = resp.body[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def anthropic_headers(key: str) -> dict:
    return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}


def openai_headers(key: str) -> dict:
    return {"authorization": "Bearer " + key}


def anthropic_payload(model: str, max_tokens: int, stream: bool = False) -> dict:
    p = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": PROMPT}]}
    if stream:
        p["stream"] = True
    return p


def openai_payload(model: str, max_tokens: int, stream: bool = False) -> dict:
    p = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": PROMPT}]}
    if stream:
        p["stream"] = True
        p["stream_options"] = {"include_usage": True}
    return p


# ---------- check 1: invalid key ----------

def check_invalid_key(base: str, protocol: str, timeout: float) -> Check:
    """A real implementation answers an unknown key with 401/403 and a JSON error, not an HTML page."""
    if protocol == "anthropic":
        path, headers, payload = "/v1/messages", anthropic_headers(INVALID_KEY), anthropic_payload("claude-sonnet-4-5", 16)
    else:
        path, headers, payload = "/v1/chat/completions", openai_headers(INVALID_KEY), openai_payload("gpt-4o-mini", 16)
    url = build_url(base, path)
    r = post(url, headers, payload, timeout)
    data = {"url": url, "http_status": r.status, "content_type": r.content_type, "elapsed_ms": r.elapsed_ms}
    name = "invalid key -> " + path
    if r.error:
        return Check(name, FAIL, "request failed: " + r.error, data)
    if looks_like_html(r):
        return Check(name, FAIL, f"HTTP {r.status} with an HTML page: this path does not implement the API", data)
    body = r.json()
    if body is None:
        return Check(name, FAIL, f"HTTP {r.status}, body is not JSON", data)
    data["body"] = body
    if r.status in (401, 403):
        return Check(name, PASS, f"HTTP {r.status} with a JSON error", data)
    if 200 <= r.status < 300:
        return Check(name, FAIL, f"HTTP {r.status}: an invalid key was accepted", data)
    return Check(name, WARN, f"HTTP {r.status} with JSON, expected 401 or 403", data)


# ---------- check 2: usage vs request size ----------

def estimate_prompt_tokens() -> int:
    # The prompt is about 10 tokens in common tokenizers; chat templates add a few more.
    return len(PROMPT.split()) + 2


def check_usage(base: str, protocol: str, key: str, model: str, timeout: float) -> Check:
    """Send one small request and compare the reported usage with what was sent."""
    if protocol == "anthropic":
        path = "/v1/messages"
        r = post(build_url(base, path), anthropic_headers(key), anthropic_payload(model, 16), timeout)
    else:
        path = "/v1/chat/completions"
        r = post(build_url(base, path), openai_headers(key), openai_payload(model, 16), timeout)
    name = "usage -> " + path
    data = {"http_status": r.status, "elapsed_ms": r.elapsed_ms, "model": model}
    if r.error:
        return Check(name, FAIL, "request failed: " + r.error, data)
    body = r.json()
    if r.status != 200 or not isinstance(body, dict):
        data["body"] = body if body is not None else r.body[:300].decode("utf-8", "replace")
        return Check(name, FAIL, f"HTTP {r.status}, no usable response", data)
    usage = body.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    if protocol == "anthropic":
        in_tok = usage.get("input_tokens")
        out_tok = usage.get("output_tokens")
        content = body.get("content")
        blocks = content if isinstance(content, list) else []
        text = "".join(b.get("text") or "" for b in blocks if isinstance(b, dict) and isinstance(b.get("text") or "", str))
    else:
        in_tok = usage.get("prompt_tokens")
        out_tok = usage.get("completion_tokens")
        choices = body.get("choices")
        first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        msg = first.get("message") if isinstance(first.get("message"), dict) else {}
        text = msg.get("content") if isinstance(msg.get("content"), str) else ""
    data.update({"usage": usage, "reply": text[:80], "returned_model": body.get("model")})
    if not isinstance(in_tok, int) or not isinstance(out_tok, int):
        return Check(name, FAIL, "response has no usage token counts", data)
    expected = estimate_prompt_tokens()
    notes = []
    status = PASS
    # Providers wrap the prompt in a chat template, so a small prompt can show a few dozen input tokens.
    # Hundreds of tokens for a one-line prompt usually means a hidden system prompt or inflated counts.
    if in_tok > 200:
        status = WARN
        notes.append(f"input_tokens={in_tok} is high for a {expected}-token prompt (hidden system prompt or inflated count?)")
    if out_tok > 16:
        status = WARN
        notes.append(f"output_tokens={out_tok} exceeds max_tokens=16")
    if out_tok == 0 and text:
        status = WARN
        notes.append("output_tokens=0 but text was returned")
    detail = f"input_tokens={in_tok}, output_tokens={out_tok}"
    if notes:
        detail += "; " + "; ".join(notes)
    if body.get("model") and body.get("model") != model:
        detail += f"; response model is {body.get('model')!r} (requested {model!r})"
    return Check(name, status, detail, data)


# ---------- check 3: streaming and time to first token ----------

def check_stream(base: str, protocol: str, key: str, model: str, timeout: float) -> Check:
    """Stream one reply: measure time to first token and whether events arrive as SSE."""
    if protocol == "anthropic":
        path = "/v1/messages"
        headers, payload = anthropic_headers(key), anthropic_payload(model, 64, stream=True)
    else:
        path = "/v1/chat/completions"
        headers, payload = openai_headers(key), openai_payload(model, 64, stream=True)
    url = build_url(base, path)
    name = "stream -> " + path
    result = _stream_once(url, headers, payload, protocol, timeout)
    # Some compatible endpoints reject stream_options; retry once without it.
    if isinstance(result, int) and result == 400 and "stream_options" in payload:
        payload = {k: v for k, v in payload.items() if k != "stream_options"}
        result = _stream_once(url, headers, payload, protocol, timeout)
    if isinstance(result, int):
        return Check(name, FAIL, f"HTTP {result}", {"url": url})
    if isinstance(result, str):
        return Check(name, FAIL, "request failed: " + result, {"url": url})
    ctype, first_token_ms, events, arrivals, total_ms = result
    text_events = len(arrivals)
    data = {"content_type": ctype, "events": events, "text_events": text_events, "ttft_ms": first_token_ms, "total_ms": total_ms}
    if "text/event-stream" not in ctype:
        return Check(name, FAIL, f"content-type is {ctype!r}, expected text/event-stream", data)
    if text_events == 0:
        return Check(name, FAIL, "no text arrived in the stream", data)
    detail = f"time to first token {first_token_ms} ms, {text_events} text events, total {total_ms} ms"
    # Many text events landing within a few ms after a long wait means the reply was buffered, not streamed.
    # Short replies naturally arrive close together, so this needs enough events to mean anything.
    if text_events >= 5 and arrivals[-1] - arrivals[0] < 20 and first_token_ms and first_token_ms > 1000:
        return Check(name, WARN, detail + "; all text arrived at once (buffered, not streamed?)", data)
    return Check(name, PASS, detail, data)


def _stream_once(url, headers, payload, protocol, timeout):
    """Returns (content_type, ttft_ms, events, arrivals_ms, total_ms), an HTTP status int, or an error string."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("accept", "text/event-stream")
    req.add_header("user-agent", USER_AGENT)
    for k, v in headers.items():
        req.add_header(k, v)
    start = time.monotonic()
    first_token_ms: Optional[int] = None
    events = 0
    arrivals = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = r.headers.get("content-type", "")
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                events += 1
                try:
                    ev = json.loads(chunk)
                except ValueError:
                    continue
                if isinstance(ev, dict) and has_text(ev, protocol):
                    now = int((time.monotonic() - start) * 1000)
                    arrivals.append(now)
                    if first_token_ms is None:
                        first_token_ms = now
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        return describe_error(e)
    return ctype, first_token_ms, events, arrivals, int((time.monotonic() - start) * 1000)


def has_text(ev: dict, protocol: str) -> bool:
    if protocol == "anthropic":
        d = ev.get("delta")
        return ev.get("type") == "content_block_delta" and isinstance(d, dict) and bool(d.get("text"))
    choices = ev.get("choices")
    for c in choices if isinstance(choices, list) else []:
        d = c.get("delta") if isinstance(c, dict) else None
        if isinstance(d, dict) and d.get("content"):
            return True
    return False


# ---------- report ----------

def run(args) -> list:
    protocols = ["anthropic", "openai"] if args.protocol == "both" else [args.protocol]
    checks = []
    for p in protocols:
        checks.append(check_invalid_key(args.base_url, p, args.timeout))
    if not args.key:
        checks.append(Check("usage / stream", SKIP, "no --key given; only the invalid-key checks ran"))
        return checks
    for p in protocols:
        model = args.anthropic_model if p == "anthropic" else args.openai_model
        if not model:
            checks.append(Check(f"usage / stream ({p})", SKIP, f"no --{p}-model given"))
            continue
        checks.append(check_usage(args.base_url, p, args.key, model, args.timeout))
        checks.append(check_stream(args.base_url, p, args.key, model, args.timeout))
    return checks


def redact(value, secret: str):
    """Replace every occurrence of secret in strings inside value (dicts, lists, strings)."""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "***")
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if isinstance(value, dict):
        return {k: redact(v, secret) for k, v in value.items()}
    return value


def print_text(base: str, checks: list) -> None:
    print(f"api-relay-checker {__version__}")
    print(f"endpoint: {base}")
    print()
    for c in checks:
        print(f"[{c.status}] {c.name}")
        print(f"       {c.detail}")
    print()
    counts = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARN, FAIL, SKIP)}
    print("summary: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    print("These checks show how the endpoint behaves; they do not prove which upstream model answered.")


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Check an OpenAI / Anthropic compatible API endpoint.")
    ap.add_argument("base_url", help="endpoint base URL, e.g. https://api.example.com (with or without /v1)")
    ap.add_argument("--protocol", choices=["anthropic", "openai", "both"], default="both")
    ap.add_argument("--key", default="", help="your API key for the usage and stream checks (or set RELAY_CHECK_KEY)")
    ap.add_argument("--anthropic-model", default="", help="model ID for Anthropic protocol checks")
    ap.add_argument("--openai-model", default="", help="model ID for OpenAI protocol checks")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    if not args.key:
        import os
        args.key = os.environ.get("RELAY_CHECK_KEY", "")
    u = urllib.parse.urlparse(args.base_url)
    if u.scheme not in ("http", "https") or not u.netloc or any(ch.isspace() for ch in args.base_url):
        ap.error("base_url must be an http:// or https:// URL without spaces")
    return args


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    # Some endpoints echo the key back in error messages; never let it reach the report.
    checks = [Check(c.name, c.status, redact(c.detail, args.key), redact(c.data, args.key)) for c in run(args)]
    if args.json:
        print(json.dumps({"endpoint": args.base_url, "version": __version__, "checks": [asdict(c) for c in checks]}, ensure_ascii=False, indent=2))
    else:
        print_text(args.base_url, checks)
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
