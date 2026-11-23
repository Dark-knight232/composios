from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

try:
    from composio import Composio
except ImportError:  # pragma: no cover - handled at runtime
    Composio = None  # type: ignore[assignment]

try:
    from .schema import AppResearchRecord
except ImportError:  # allows `python src/agent.py` directly
    from schema import AppResearchRecord  # type: ignore[no-redef]

try:
    from .common import (
        USER_ID,
        TOOLKIT,
        SEARCH_QUERY_A,
        SEARCH_QUERY_B,
        SYSTEM_PROMPT,
        build_llm_clients,
        mcp_headers,
        print_mcp_connection,
        mcp_initialize,
        mcp_list_tools,
        mcp_call_tool,
        mcp_tavily_search,
    )
except ImportError:  # allows `python src/agent.py` directly
    from common import (
        USER_ID,
        TOOLKIT,
        SEARCH_QUERY_A,
        SEARCH_QUERY_B,
        SYSTEM_PROMPT,
        build_llm_clients,
        mcp_headers,
        print_mcp_connection,
        mcp_initialize,
        mcp_list_tools,
        mcp_call_tool,
        mcp_tavily_search,
    )  # type: ignore[no-redef]


CONSOLE = Console()


def _composio_execute(session: Any, tool: str, arguments: dict[str, Any]) -> Any:
    """Call session.execute with defensive fallbacks for SDK version differences."""
    attempts = [
        lambda: session.execute(tool, arguments=arguments),
        lambda: session.execute(action=tool, arguments=arguments),
        lambda: session.execute(toolkit=TOOLKIT, tool=tool, arguments=arguments),
    ]
    errors: list[str] = []
    for attempt in attempts:
        try:
            return attempt()
        except Exception as exc:  # noqa: BLE001 - try next fallback
            errors.append(repr(exc))
    raise RuntimeError(f"Composio session.execute failed for {tool}: {errors}")


_URL_RE = re.compile(r"https?://[^\s\"\\<>]+")


def _collect_urls(payload: Any, limit: int = 10) -> list[str]:
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


def _collect_text(payload: Any) -> str:
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


def _top_official_urls(search_results: Any, app: str) -> list[str]:
    urls = _collect_urls(search_results, limit=10)
    app_key = app.lower().replace(" ", "")
    scored: list[tuple[int, str]] = []
    for u in urls:
        lu = u.lower()
        score = sum(1 for token in ("developer", "docs", "api", "reference", "platform", "integration") if token in lu)
        if any(domain in lu for domain in ("github.com", "twitter.com", "linkedin.com", "crunchbase.com")):
            score -= 3
        scored.append((score, u))
    scored.sort(key=lambda item: -item[0])
    return [u for _, u in scored[:2]]


class ResearchAgent:
    def __init__(self, input_path: Path, output_path: Path, use_mcp: bool = False) -> None:
        self.input_path = input_path
        self.output_path = output_path
        self.use_mcp = use_mcp
        self.composio_calls = 0
        self.llm_calls = 0
        self.records: dict[str, AppResearchRecord] = {}

    def run(self) -> None:
        load_dotenv()

        if self.use_mcp:
            self._run_mcp_demo()
            return

        if Composio is None:
            CONSOLE.print("[red]composio SDK not installed. Run: pip install -r requirements.txt[/red]")
            sys.exit(1)
        if not os.getenv("COMPOSIO_API_KEY"):
            CONSOLE.print("[red]Missing required env var COMPOSIO_API_KEY[/red]")
            sys.exit(1)
        if not os.getenv("OPENAI_API_KEY") and not os.getenv("ANTHROPIC_API_KEY"):
            CONSOLE.print("[red]Missing OPENAI_API_KEY or ANTHROPIC_API_KEY or NIM_API_KEY[/red]")
            sys.exit(1)

        if not self.input_path.exists():
            CONSOLE.print(f"[red]Input file not found: {self.input_path}[/red]")
            sys.exit(1)

        apps = json.loads(self.input_path.read_text(encoding="utf-8"))
        if not isinstance(apps, list) or not all(isinstance(a, str) for a in apps):
            raise SystemExit("input_apps.json must be a JSON list of app name strings")

        self.records = self._load_checkpoint()
        remaining = [a for a in apps if a not in self.records]
        CONSOLE.print(f"[bold]Pass 1 research[/bold] — {len(remaining)} apps remaining (of {len(apps)} total)")

        session = Composio().sessions.create(user_id=USER_ID, toolkits=[TOOLKIT])
        asyncio.run(self._amain(session, remaining))
        self._save_checkpoint()
        self._print_summary()

    def _run_mcp_demo(self) -> None:
        """Demo the Composio hosted MCP endpoint (x-consumer-api-key auth)."""
        load_dotenv()
        key = os.getenv("COMPOSIO_API_KEY", "")
        if not key:
            CONSOLE.print("[red]Missing COMPOSIO_API_KEY env var[/red]")
            sys.exit(1)

        print_mcp_connection(key)

        async def _probe() -> None:
            init = await mcp_initialize(api_key=key)
            CONSOLE.print(f"[green]initialize OK[/green] server={init['result']['serverInfo']}")
            tools = await mcp_list_tools(api_key=key)
            CONSOLE.print(f"[green]tools/list OK[/green] count={len(tools)}")
            for t in tools[:10]:
                CONSOLE.print(f"  - {t.get('name')}")
            if len(tools) > 10:
                CONSOLE.print(f"  ... and {len(tools) - 10} more")

        asyncio.run(_probe())

    async def _amain(self, session: Any, apps: list[str]) -> None:
        openai_client, anthropic_client, nim_client = build_llm_clients()

        sem = asyncio.Semaphore(5)

        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            console=CONSOLE,
        )
        task_id = progress.add_task(f"Researching {len(apps)} apps", total=len(apps))

        async def process(app: str) -> None:
            async with sem:
                record = await self._research_app(session, app, openai_client, anthropic_client, nim_client)
                progress.advance(task_id)
                self.records[app] = record
                self._save_checkpoint()

        with progress:
            for coro in asyncio.as_completed([process(a) for a in apps]):
                try:
                    await coro
                except Exception as exc:  # noqa: BLE001 - surfaced, pipeline continues
                    CONSOLE.print(f"[red]Failed after retries: {exc}[/red]")

    async def _execute_tool(self, session: Any, tool: str, arguments: dict[str, Any]) -> Any:
        self.composio_calls += 1
        if asyncio.iscoroutinefunction(session.execute):
            return await session.execute(tool, arguments=arguments)
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: _composio_execute(session, tool, arguments)
        )

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def _research_app(
        self,
        session: Any,
        app: str,
        openai_client: Any,
        anthropic_client: Any,
        nim_client: Any,
    ) -> AppResearchRecord:
        query_a = SEARCH_QUERY_A.format(app=app)
        query_b = SEARCH_QUERY_B.format(app=app)

        search_a, search_b = await asyncio.gather(
            self._execute_tool(session, "TAVILY_SEARCH", {"query": query_a}),
            self._execute_tool(session, "TAVILY_SEARCH", {"query": query_b}),
        )

        extract_text = ""
        urls = _top_official_urls(search_a, app)
        if urls:
            extracted = await self._execute_tool(session, "TAVILY_EXTRACT", {"urls": urls})
            extract_text = _collect_text(extracted)

        context = (
            f"SEARCH RESULTS (Query A — developer docs):\n{_collect_text(search_a)}\n\n"
            f"SEARCH RESULTS (Query B — MCP server):\n{_collect_text(search_b)}\n\n"
            f"EXTRACTED DOCUMENT TEXT:\n{extract_text or '(no extractable content)'}"
        )

        if openai_client is not None:
            record = await self._synthesize_openai(openai_client, app, context)
        elif anthropic_client is not None:
            record = await self._synthesize_anthropic(anthropic_client, app, context)
        else:
            record = await self._synthesize_nim(nim_client, app, context)
        return record

    async def _synthesize_openai(self, client: Any, app: str, context: str) -> AppResearchRecord:
        self.llm_calls += 1
        response = await client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            temperature=0.2,
            response_format=AppResearchRecord,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"App: {app}\n\n{context}"},
            ],
        )
        return response.choices[0].message.parsed  # type: ignore[return-value]

    async def _synthesize_anthropic(self, client: Any, app: str, context: str) -> AppResearchRecord:
        self.llm_calls += 1
        response = await client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=1000,
            temperature=0.2,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"App: {app}\n\n"
                        f"Return ONLY valid JSON matching this Pydantic schema:\n{AppResearchRecord.model_json_schema()}\n\n"
                        f"{context}"
                    ),
                }
            ],
        )
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        return AppResearchRecord.model_validate_json(text)

    async def _synthesize_nim(self, client: Any, app: str, context: str) -> AppResearchRecord:
        self.llm_calls += 1
        model_name = os.getenv("NIM_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
        response = await client.chat.completions.create(
            model=model_name,
            temperature=0.2,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"App: {app}\n\n"
                        f"Return ONLY valid JSON matching this Pydantic schema:\n{AppResearchRecord.model_json_schema()}\n\n"
                        f"{context}"
                    ),
                },
            ],
        )
        text = response.choices[0].message.content or ""
        return AppResearchRecord.model_validate_json(text)

    def _load_checkpoint(self) -> dict[str, AppResearchRecord]:
        if not self.output_path.exists():
            return {}
        try:
            data = json.loads(self.output_path.read_text(encoding="utf-8") or "[]")
        except (json.JSONDecodeError, OSError):
            return {}
        records: dict[str, AppResearchRecord] = {}
        for item in data:
            try:
                rec = AppResearchRecord.model_validate(item)
                records[rec.app_name] = rec
            except Exception:
                continue
        return records

    def _save_checkpoint(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [r.model_dump(mode="json") for r in self.records.values()]
        self.output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _print_summary(self) -> None:
        records = list(self.records.values())
        total = len(records)
        buildable = sum(1 for r in records if r.buildability_verdict == "Buildable_Today")
        blocked = sum(1 for r in records if r.buildability_verdict == "Blocked")
        workaround = total - buildable - blocked
        low_conf = [r.app_name for r in records if r.confidence_score < 4]

        table = Table(title="Pass 1 Research Summary")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")
        table.add_row("Total Processed", str(total))
        table.add_row("Buildable_Today", str(buildable))
        table.add_row("Buildable_With_Workaround", str(workaround))
        table.add_row("Blocked", str(blocked))
        table.add_row("Low Confidence (<4) flagged for Pass 2", str(len(low_conf)))
        table.add_row("Composio Instant execute calls", str(self.composio_calls))
        table.add_row("LLM structured calls", str(self.llm_calls))
        CONSOLE.print(table)

        if low_conf:
            CONSOLE.print("\n[bold yellow]Pass 2 browser-verification candidates:[/bold yellow] " + ", ".join(low_conf))
        CONSOLE.print(f"\n[green]Results written to {self.output_path}[/green]")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Composio AI Product Ops research pipeline")
    parser.add_argument("--input", default="data/input_apps.json", help="Path to input apps JSON list")
    parser.add_argument("--output", default="data/pass1_results.json", help="Path to output results JSON")
    parser.add_argument("--mcp", action="store_true", help="Demo native Composio MCP session and exit")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    agent = ResearchAgent(Path(args.input), Path(args.output), use_mcp=args.mcp)
    agent.run()


if __name__ == "__main__":
    main()
