"""Codex CLI -> proxy -> upstream passthrough, against a local fake upstream (no network).

BUY/SELL's Codex CLI used a second OAuth login whose refresh rotation could log
out the proxy's. Routing the CLI through /v1/codex/responses leaves one login.
The CLI parses SSE itself, so the stream must come back byte for byte, and the
model / service_tier (Fast = "priority") must not be rewritten.
"""
import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cores.chatgpt_proxy import proxy_server

SSE = (b'event: response.created\ndata: {"type":"response.created"}\n\n'
       b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"PONG"}\n\n'
       b'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{}}}\n\n')


class _Tokens:
    def __init__(self, fail=False):
        self.fail = fail

    async def get_token(self):
        if self.fail:
            raise RuntimeError("expired")
        return "tok-123"

    async def get_account_id(self):
        return "acct-9"


def _run(monkeypatch, *, upstream_status=200, body=None, tokens=None, raw=None):
    seen = {}
    # create_app() sets a module global; let monkeypatch restore it for later tests
    # (the isolated proxy refuses to start while a native proxy looks active).
    monkeypatch.setattr(proxy_server, "_token_manager", None)

    async def upstream(request):
        seen["headers"] = dict(request.headers)
        seen["body"] = await request.json()
        if upstream_status != 200:
            return web.json_response({"detail": "usage limit"}, status=upstream_status,
                                     headers={"x-codex-primary-used-percent": "100"})
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                           "x-codex-primary-used-percent": "12", "set-cookie": "secret=1"})
        await resp.prepare(request)
        for i in range(0, len(SSE), 17):  # arbitrary chunking must not matter
            await resp.write(SSE[i:i + 17])
        await resp.write_eof()
        return resp

    async def go():
        up_app = web.Application()
        up_app.router.add_post("/backend-api/codex/responses", upstream)
        async with TestServer(up_app) as up:
            monkeypatch.setattr(proxy_server, "CHATGPT_RESPONSES_URL", str(up.make_url("/backend-api/codex/responses")))
            app = proxy_server.create_app(tokens or _Tokens())
            async with TestClient(TestServer(app)) as client:
                kwargs = {"data": raw} if raw is not None else {"json": body}
                async with client.post("/v1/codex/responses", headers={"originator": "codex_cli_rs"}, **kwargs) as r:
                    return r.status, dict(r.headers), await r.read(), seen

    return asyncio.run(go())


def test_sse_stream_is_returned_byte_for_byte_with_the_proxy_login(monkeypatch):
    body = {"model": "gpt-6-astra", "service_tier": "priority", "stream": True, "store": True,
            "include": ["reasoning.encrypted_content"], "input": [{"role": "user", "content": "hi"}]}
    status, headers, payload, seen = _run(monkeypatch, body=body)
    assert status == 200 and payload == SSE
    assert headers["Content-Type"].startswith("text/event-stream")
    assert headers["x-codex-primary-used-percent"] == "12" and "set-cookie" not in {k.lower() for k in headers}
    sent = seen["body"]
    assert sent["model"] == "gpt-6-astra" and sent["service_tier"] == "priority"  # never mapped
    assert sent["store"] is False and sent["stream"] is True
    assert sent["include"] == ["reasoning.encrypted_content"]  # not stripped, unlike /v1/responses
    assert seen["headers"]["Authorization"] == "Bearer tok-123"
    assert seen["headers"]["chatgpt-account-id"] == "acct-9"
    assert seen["headers"]["originator"] == "codex_cli_rs"


def test_upstream_error_status_is_passed_through_for_the_cli_to_handle(monkeypatch):
    status, headers, payload, _ = _run(monkeypatch, upstream_status=429, body={"model": "gpt-6-astra", "input": []})
    assert status == 429 and json.loads(payload)["detail"] == "usage limit"
    assert headers["x-codex-primary-used-percent"] == "100"


def test_auth_failure_is_401_and_never_reaches_upstream(monkeypatch):
    status, _, payload, seen = _run(monkeypatch, body={"model": "gpt-6-astra", "input": []}, tokens=_Tokens(fail=True))
    assert status == 401 and "tok" not in payload.decode() and seen == {}


def test_bad_requests_are_rejected_locally(monkeypatch):
    assert _run(monkeypatch, raw=b"{not json")[0] == 400
    assert _run(monkeypatch, body={"input": []})[0] == 400


def test_existing_responses_route_is_unchanged(monkeypatch):
    monkeypatch.setattr(proxy_server, "_token_manager", None)
    app = proxy_server.create_app(_Tokens())
    routes = {(r.method, r.resource.canonical) for r in app.router.routes()}
    assert ("POST", "/v1/responses") in routes and ("POST", "/v1/chat/completions") in routes
    assert ("POST", "/v1/codex/responses") in routes
