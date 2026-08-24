"""Provider registry: mock in-process + allowlisted MCP HTTP JSON-RPC.

Customers never supply provider URLs. CROSSING_PROVIDER_URLS is operator env.
SSRF: deny loopback, RFC1918, link-local, cloud metadata unless the operator
sets CROSSING_PROVIDER_INTERNAL_ALLOWLIST=1 *and* the URL is still allowlisted.
Provider tokens come from env (CROSSING_PROVIDER_TOKEN_<NAME>), never receipts/logs.
Customers cannot configure a server-side shell transport.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from crossing import mock_mcp, pricing
from crossing.policy import PolicyDenied, Reason

_METADATA_HOSTS = {
    "metadata",
    "metadata.google.internal",
    "metadata.google.internal.",
    "169.254.169.254",
}
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/29"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str = "", *, retryable: bool = False) -> None:
        super().__init__(message or code)
        self.code = code
        self.retryable = retryable


class ProviderConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Capabilities:
    supports_idempotency: bool = False
    supports_status_query: bool = False


def provider_timeout() -> float:
    raw = os.environ.get("CROSSING_PROVIDER_TIMEOUT_SECONDS") or "15"
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 15.0


def internal_allowlist_enabled() -> bool:
    return os.environ.get("CROSSING_PROVIDER_INTERNAL_ALLOWLIST", "") == "1"


def parse_provider_urls(raw: str | None = None) -> dict[str, str]:
    text = (raw if raw is not None else os.environ.get("CROSSING_PROVIDER_URLS") or "").strip()
    if not text:
        return {}
    out: dict[str, str] = {}
    if text.startswith("{"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ProviderConfigError("CROSSING_PROVIDER_URLS JSON must be an object")
        for name, url in data.items():
            out[str(name)] = str(url)
        return out
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ProviderConfigError("CROSSING_PROVIDER_URLS entries must be name=url")
        name, url = part.split("=", 1)
        name = name.strip()
        url = url.strip()
        if not name or not url:
            raise ProviderConfigError("CROSSING_PROVIDER_URLS entries must be name=url")
        out[name] = url
    return out


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    for net in _BLOCKED_NETWORKS:
        if ip in net:
            return True
    return False


def assert_public_url(url: str, *, allow_internal: bool | None = None) -> None:
    """Reject non-http(s), credentials-in-URL, and SSRF-class destinations."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ProviderConfigError("provider URL must be http or https")
    if parsed.username or parsed.password:
        raise ProviderConfigError("provider URL must not contain credentials")
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise ProviderConfigError("provider URL missing host")
    if host in _METADATA_HOSTS or host.endswith(".metadata.google.internal"):
        raise ProviderConfigError("provider URL blocked (metadata)")
    if allow_internal is None:
        allow_internal = internal_allowlist_enabled()
    # Literal IPs
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if _ip_blocked(ip) and not allow_internal:
            raise ProviderConfigError("provider URL blocked (private/link-local)")
        return
    if host == "localhost" and not allow_internal:
        raise ProviderConfigError("provider URL blocked (loopback)")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ProviderConfigError("provider URL host did not resolve") from exc
    if not infos:
        raise ProviderConfigError("provider URL host did not resolve")
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_blocked(resolved) and not allow_internal:
            raise ProviderConfigError("provider URL blocked (resolved private/link-local)")


def provider_token(name: str) -> str:
    env = "CROSSING_PROVIDER_TOKEN_" + name.upper().replace("-", "_")
    return (os.environ.get(env) or "").strip()


class Provider:
    name: str = ""
    capabilities: Capabilities = Capabilities()

    def quote(self, tool: str) -> int:
        raise NotImplementedError

    def invoke(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        invocation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        raise NotImplementedError

    def status(self, invocation_id: str) -> dict[str, Any] | None:
        return None


class MockProvider(Provider):
    name = "mock"
    capabilities = Capabilities(supports_idempotency=True, supports_status_query=False)

    def quote(self, tool: str) -> int:
        try:
            return pricing.quote(tool, "mock")
        except KeyError as exc:
            raise PolicyDenied(Reason.UNKNOWN_TOOL, str(exc)) from exc

    def invoke(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        invocation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        try:
            return mock_mcp.call_tool(
                tool,
                arguments or {},
                invocation_id=invocation_id,
                idempotency_key=idempotency_key,
            )
        except mock_mcp.MCPError as exc:
            raise ProviderError("MCP_ERROR", exc.__class__.__name__) from exc

    def status(self, invocation_id: str) -> dict[str, Any] | None:
        return None


class HttpJsonRpcProvider(Provider):
    """MCP streamable HTTP / HTTP JSON-RPC. URL is operator-allowlisted only."""

    def __init__(self, name: str, url: str) -> None:
        self.name = name
        self._url = url
        self.capabilities = Capabilities(supports_idempotency=True, supports_status_query=True)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        token = provider_token(self.name)
        if token:
            headers["Authorization"] = "Bearer " + token
        return headers

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        assert_public_url(self._url)
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            with httpx.Client(timeout=provider_timeout(), follow_redirects=False) as client:
                resp = client.post(self._url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ProviderError("TIMEOUT", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("HTTP", retryable=True) from exc
        if resp.status_code >= 500:
            raise ProviderError("HTTP", retryable=True)
        if resp.status_code >= 400:
            raise ProviderError("HTTP", retryable=False)
        try:
            body = resp.json()
        except json.JSONDecodeError as exc:
            raise ProviderError("PROTOCOL") from exc
        if not isinstance(body, dict):
            raise ProviderError("PROTOCOL")
        if body.get("error"):
            err = body["error"]
            msg = err.get("message") if isinstance(err, dict) else "rpc error"
            raise ProviderError("JSONRPC", str(msg)[:120])
        return body.get("result")

    def quote(self, tool: str) -> int:
        try:
            result = self._rpc("tools/quote", {"name": tool})
            if isinstance(result, dict) and "amount_cents" in result:
                return int(result["amount_cents"])
            if isinstance(result, int):
                return int(result)
        except ProviderError:
            pass
        try:
            return pricing.quote(tool, self.name)
        except KeyError as exc:
            raise PolicyDenied(Reason.UNKNOWN_TOOL, str(exc)) from exc

    def invoke(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        invocation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        result = self._rpc(
            "tools/call",
            {
                "name": tool,
                "arguments": arguments or {},
                "invocation_id": invocation_id,
                "idempotency_key": idempotency_key,
            },
        )
        return result

    def status(self, invocation_id: str) -> dict[str, Any] | None:
        if not self.capabilities.supports_status_query:
            return None
        result = self._rpc("tools/status", {"invocation_id": invocation_id})
        return result if isinstance(result, dict) else {"raw": result}


_BUILTIN: dict[str, Provider] = {"mock": MockProvider()}


def get_provider(server: str) -> Provider:
    name = (server or "mock").strip() or "mock"
    if name in ("shell", "exec", "subprocess", "local_shell"):
        raise PolicyDenied(Reason.SERVER_NOT_ALLOWED, "shell transports are not configurable")
    if name == "mock":
        return _BUILTIN["mock"]
    urls = parse_provider_urls()
    if name not in urls:
        raise PolicyDenied(Reason.SERVER_NOT_ALLOWED, "server is not allowlisted")
    url = urls[name]
    assert_public_url(url)
    return HttpJsonRpcProvider(name, url)


def quote(tool: str, server: str = pricing.DEFAULT_SERVER) -> int:
    return get_provider(server).quote(tool)


def invoke(
    server: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    invocation_id: str | None = None,
    idempotency_key: str | None = None,
) -> Any:
    return get_provider(server).invoke(
        tool,
        arguments,
        invocation_id=invocation_id,
        idempotency_key=idempotency_key,
    )


def status(server: str, invocation_id: str) -> dict[str, Any] | None:
    return get_provider(server).status(invocation_id)


def capabilities(server: str) -> Capabilities:
    return get_provider(server).capabilities
