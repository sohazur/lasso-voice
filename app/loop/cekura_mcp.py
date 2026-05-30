"""Minimal async client for Cekura's MCP server (Streamable HTTP + SSE).

Verified protocol (2026-05-30): POST initialize -> mcp-session-id header ->
notifications/initialized -> tools/list / tools/call, all carrying the session id.
Header auth: X-CEKURA-API-KEY. Responses come back as SSE `data:` lines.

We keep this dependency-light (httpx only) and defensive: any protocol/permission
issue raises CekuraError so the loop can fall back to the local judge — never fake.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from loguru import logger


class CekuraError(RuntimeError):
    pass


def _parse_sse(text: str) -> dict[str, Any]:
    """Pull the JSON-RPC payload out of an SSE response body."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[len("data:") :].strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise CekuraError(f"no JSON-RPC payload in response: {text[:200]!r}")


class CekuraMCP:
    BASE = "https://api.cekura.ai/mcp"
    PROTOCOL = "2024-11-05"

    def __init__(self, api_key: str):
        if not api_key:
            raise CekuraError("CekuraMCP requires an API key")
        self._key = api_key
        self._sid: str | None = None
        self._id = 0

    def _headers(self) -> dict[str, str]:
        h = {
            "X-CEKURA-API-KEY": self._key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._sid:
            h["mcp-session-id"] = self._sid
        return h

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _post(self, client: httpx.AsyncClient, payload: dict) -> httpx.Response:
        return await client.post(self.BASE, headers=self._headers(), json=payload)

    async def __aenter__(self) -> CekuraMCP:
        self._client = httpx.AsyncClient(timeout=60)
        # initialize -> grab session id from the response header
        resp = await self._post(
            self._client,
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": self.PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "lasso-voice", "version": "0.1.0"},
                },
            },
        )
        if resp.status_code >= 400:
            raise CekuraError(f"initialize failed {resp.status_code}: {resp.text[:200]}")
        self._sid = resp.headers.get("mcp-session-id")
        if not self._sid:
            raise CekuraError("no mcp-session-id returned from initialize")
        # required notification before tool calls
        await self._post(self._client, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        logger.info(f"Cekura MCP session {self._sid[:8]}…")
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        resp = await self._post(
            self._client,
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        if resp.status_code >= 400:
            raise CekuraError(f"{name} HTTP {resp.status_code}: {resp.text[:200]}")
        payload = _parse_sse(resp.text)
        if "error" in payload:
            raise CekuraError(f"{name} error: {payload['error']}")
        result = payload.get("result", {})
        # MCP tool results arrive as content blocks; pull text/JSON out.
        content = result.get("content")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    txt = block.get("text", "")
                    try:
                        return json.loads(txt)
                    except (json.JSONDecodeError, TypeError):
                        return txt
        return result

    async def list_tool_names(self) -> list[str]:
        resp = await self._post(
            self._client, {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list"}
        )
        payload = _parse_sse(resp.text)
        return [t["name"] for t in payload.get("result", {}).get("tools", [])]
