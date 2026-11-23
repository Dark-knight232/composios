from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

USER_ID = "product_ops_researcher"
TOOLKIT = "tavily"
SEARCH_QUERY_A = "{app} developer API documentation authentication OAuth2 API key"
SEARCH_QUERY_B = "{app} MCP server model context protocol github"

# Composio hosted MCP endpoint — authenticated via x-consumer-api-key header
MCP_ENDPOINT = "https://connect.composio.dev/mcp"

SYSTEM_PROMPT = """You are an expert API researcher producing structured developer metadata.

Rules:
- Never hallucinate URLs. evidence_urls must ONLY contain URLs that appear in the provided search/extract tool outputs.
- Explicitly distinguish "free developer sandbox" from "gated behind paid tier / sales approval".
- Set confidence_score <= 3 if docs are behind a login wall, ambiguous, or missing clear auth specs.
- one_liner must be at most 15 words.
- Return ONLY valid JSON matching the requested schema."""


try:  # pragma: no cover - depends on installed SDK
    from composio.exceptions import ToolInputRequiredError
except Exception:  # noqa: BLE001

    class ToolInputRequiredError(Exception):
        """Fallback when composio is not installed or the exception was renamed."""


class ToolExecutionError(RuntimeError):
    """Raised when a Composio tool result is a failure or otherwise unusable."""

    def __init__(self, message: str, *, stage: str, log_id: str | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.log_id = log_id


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_URL_RE = re.compile(r"https?://[^\s\"\\<>]+")


def collect_urls(payload: Any, limit: int = 10) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, str):
            for u in _URL_RE.findall(node):
                u = u.rstrip('",)}`>;.')
                if u not in seen:
                    seen.add(u)
                    found.append(u)
                    if len(found) >= limit:
                        return
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
                if len(found) >= limit:
                    return
        elif isinstance(node, list):
            for item in node:
                walk(item)
                if len(found) >= limit:
                    return

    walk(payload)
    return found


def collect_text(payload: Any) -> str:
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return "\n".join(parts)


def top_official_urls(search_results: Any, app: str, limit: int = 2) -> list[str]:
    urls = collect_urls(search_results, limit=10)
    scored: list[tuple[int, str]] = []
    for u in urls:
        lu = u.lower()
        score = sum(1 for token in ("developer", "docs", "api", "reference", "platform", "integration") if token in lu)
        if any(domain in lu for domain in ("github.com", "twitter.com", "linkedin.com", "crunchbase.com")):
            score -= 3
        scored.append((score, u))
    scored.sort(key=lambda item: -item[0])
    return [u for _, u in scored[:limit]]


def _result_type_value(result: Any) -> str | None:
    rt = getattr(result, "result_type", None)
    if rt is None:
        return None
    value = getattr(rt, "value", rt)
    return value if isinstance(value, str) else str(value)


def check_result(result: Any, *, tool: str, stage: str) -> Any:
    """Validate a Composio ToolResult, raising ToolExecutionError on failure.

    An empty ``error`` string alone is NOT treated as success: we inspect
    ``result_type`` first and only fall back to the error field when the SDK
    does not expose a result type.
    """
    if result is None:
        return result
    rt = _result_type_value(result)
    error = getattr(result, "error", None)
    log_id = getattr(result, "log_id", None)
    if rt is not None and rt.lower() == "failed":
        raise ToolExecutionError(f"{tool} failed: {error or 'unknown error'}", stage=stage, log_id=log_id)
    if rt is None and error and not getattr(result, "data", None):
        raise ToolExecutionError(f"{tool} error: {error}", stage=stage, log_id=log_id)
    return result


def execute_tool_sync(session: Any, tool: str, arguments: dict[str, Any], *, stage: str) -> Any:
    """Call session.execute with defensive fallbacks for SDK version differences."""
    attempts = [
        lambda: session.execute(tool, arguments=arguments),
        lambda: session.execute(action=tool, arguments=arguments),
        lambda: session.execute(toolkit=TOOLKIT, tool=tool, arguments=arguments),
    ]
    signature_errors: list[str] = []
    for attempt in attempts:
        try:
            result = attempt()
        except ToolInputRequiredError:
            raise
        except TypeError as exc:  # signature mismatch -> try next form
            signature_errors.append(repr(exc))
            continue
        except Exception as exc:  # noqa: BLE001 - real execution error
            raise ToolExecutionError(f"{tool} execution error: {exc}", stage=stage) from exc
        return check_result(result, tool=tool, stage=stage)
    raise ToolExecutionError(
        f"session.execute signature mismatch for {tool}: {signature_errors}", stage=stage
    )


async def execute_tool_async(session: Any, tool: str, arguments: dict[str, Any], *, stage: str) -> Any:
    if asyncio.iscoroutinefunction(session.execute):
        result = await session.execute(tool, arguments=arguments)
        return check_result(result, tool=tool, stage=stage)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: execute_tool_sync(session, tool, arguments, stage=stage))


def build_llm_clients() -> tuple[Any, Any, Any]:
    """Returns (openai_client, anthropic_client, nim_client). All may be None if their keys are absent."""
    openai_client = None
    anthropic_client = None
    nim_client = None
    if os.getenv("OPENAI_API_KEY"):
        try:
            from openai import AsyncOpenAI

            openai_client = AsyncOpenAI()
        except ImportError:
            openai_client = None
    if openai_client is None and os.getenv("ANTHROPIC_API_KEY"):
        try:
            from anthropic import AsyncAnthropic

            anthropic_client = AsyncAnthropic()
        except ImportError:
            anthropic_client = None
    if nim_client is None and os.getenv("NIM_API_KEY"):
        try:
            from openai import AsyncOpenAI

            nim_client = AsyncOpenAI(
                api_key=os.getenv("NIM_API_KEY"),
                base_url=os.getenv("NIM_API_BASE_URL", "https://integrate.api.nvidia.com/v1"),
            )
        except ImportError:
            nim_client = None
    return openai_client, anthropic_client, nim_client


async def synthesize_structured(
    *,
    app_name: str,
    context: str,
    system_prompt: str,
    model_cls: Any,
    openai_client: Any,
    anthropic_client: Any,
    nim_client: Any,
    temperature: float = 0.2,
) -> Any:
    """Run an LLM call constrained to a Pydantic model via structured outputs."""
    if openai_client is not None:
        response = await openai_client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            temperature=temperature,
            response_format=model_cls,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"App: {app_name}\n\n{context}"},
            ],
        )
        parsed = response.choices[0].message.parsed
        if parsed is None:
            raise ValueError("LLM returned no parsed structured output")
        return parsed
    if anthropic_client is not None:
        response = await anthropic_client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=1500,
            temperature=temperature,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"App: {app_name}\n\n"
                        "Return ONLY valid JSON matching this schema:\n"
                        f"{json.dumps(model_cls.model_json_schema())}\n\n{context}"
                    ),
                }
            ],
        )
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        return model_cls.model_validate_json(text)
    if nim_client is not None:
        # NIM supports OpenAI-compatible API, but structured outputs may not be available
        # Use JSON schema mode with strict prompt
        model_name = os.getenv("NIM_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
        response = await nim_client.chat.completions.create(
            model=model_name,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"App: {app_name}\n\nReturn ONLY valid JSON matching this schema:\n{json.dumps(model_cls.model_json_schema())}\n\n{context}"},
            ],
        )
        text = response.choices[0].message.content or ""
        return model_cls.model_validate_json(text)
    raise RuntimeError("No LLM client configured (set OPENAI_API_KEY, ANTHROPIC_API_KEY, or NIM_API_KEY)")


def load_records(path: Path, model_cls: Any) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "[]")
    except (json.JSONDecodeError, OSError):
        return {}
    records: dict[str, Any] = {}
    for item in data:
        try:
            rec = model_cls.model_validate(item)
            records[rec.app_name] = rec
        except Exception:
            continue
    return records


def save_records(path: Path, records: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [r.model_dump(mode="json") for r in records.values()]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


# ---------------------------------------------------------------------------
# Composio hosted MCP client (auth via x-consumer-api-key header)
# ---------------------------------------------------------------------------

def mcp_headers(api_key: str | None = None) -> dict[str, str]:
    """Build the headers Composio's hosted MCP endpoint expects."""
    key = api_key or os.getenv("COMPOSIO_API_KEY", "")
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "x-consumer-api-key": key,
    }


def _parse_mcp_response(text: str) -> dict[str, Any]:
    """Parse a text/event-stream MCP response body into the last JSON-RPC message."""
    last: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                last = json.loads(payload)
            except json.JSONDecodeError:
                continue
    if last is None:
        raise RuntimeError(f"Could not parse MCP response: {text[:300]}")
    return last


async def mcp_request(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: int = 1,
    api_key: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Send a single JSON-RPC request to the Composio hosted MCP endpoint."""
    import httpx

    body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(MCP_ENDPOINT, headers=mcp_headers(api_key), json=body)
        resp.raise_for_status()
        data = _parse_mcp_response(resp.text)
    if "error" in data and data["error"]:
        raise ToolExecutionError(f"MCP {method} error: {data['error']}", stage="mcp")
    return data


async def mcp_initialize(api_key: str | None = None) -> dict[str, Any]:
    return await mcp_request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "kilo", "version": "1.0"},
        },
        request_id=1,
        api_key=api_key,
    )


async def mcp_list_tools(api_key: str | None = None) -> list[dict[str, Any]]:
    data = await mcp_request("tools/list", {}, request_id=2, api_key=api_key)
    return data.get("result", {}).get("tools", [])


async def mcp_call_tool(name: str, arguments: dict[str, Any], *, api_key: str | None = None) -> Any:
    data = await mcp_request(
        "tools/call", {"name": name, "arguments": arguments}, request_id=3, api_key=api_key
    )
    result = data.get("result", {})
    content = result.get("content", [])
    texts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            texts.append(block.get("text", ""))
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return joined


async def mcp_tavily_search(query: str, *, api_key: str | None = None, max_results: int = 5) -> Any:
    """Run a Tavily search through the Composio MCP proxy endpoint."""
    return await mcp_call_tool(
        "COMPOSIO_PROXY_EXECUTE",
        {
            "toolkit_slug": TOOLKIT,
            "endpoint": "search",
            "method": "POST",
            "query": query,
            "max_results": max_results,
        },
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Direct Tavily API fallback (when MCP connection is unavailable)
# ---------------------------------------------------------------------------

TAVILY_API_URL = "https://api.tavily.com/search"


async def tavily_search_direct(
    query: str,
    *,
    api_key: str | None = None,
    max_results: int = 5,
    search_depth: str = "basic",
    include_answer: bool = False,
    include_raw_content: bool = False,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Call the Tavily REST API directly. Requires TAVILY_API_KEY env var."""
    key = api_key or os.getenv("TAVILY_API_KEY", "")
    if not key:
        raise ToolExecutionError("TAVILY_API_KEY not set", stage="tavily_direct")

    import httpx

    payload = {
        "api_key": key,
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_answer": include_answer,
        "include_raw_content": include_raw_content,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(TAVILY_API_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
    if not data.get("success", True):
        raise ToolExecutionError(f"Tavily direct error: {data}", stage="tavily_direct")
    return data


async def tavily_search(
    query: str,
    *,
    api_key: str | None = None,
    max_results: int = 5,
    use_mcp: bool = False,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Best-effort search: try MCP proxy first, fall back to direct Tavily API."""
    if use_mcp:
        try:
            return await mcp_tavily_search(query, api_key=api_key, max_results=max_results)
        except ToolExecutionError:
            pass
    return await tavily_search_direct(query, api_key=api_key, max_results=max_results, timeout=timeout)


def print_mcp_connection(api_key: str | None = None) -> None:
    """Print the MCP server URL + headers for Kilo Code MCP configuration."""
    key = api_key or os.getenv("COMPOSIO_API_KEY", "")
    print("Composio MCP server configuration")
    print("  url:", MCP_ENDPOINT)
    print("  headers:", json.dumps({"x-consumer-api-key": key}, indent=2))
    print()
    print("Kilo Code / VS Code MCP settings JSON:")
    print(
        json.dumps(
            {
                "mcpServers": {
                    "composio": {
                        "url": MCP_ENDPOINT,
                        "headers": {"x-consumer-api-key": key},
                    }
                }
            },
            indent=2,
        )
    )
