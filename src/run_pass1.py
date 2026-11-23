"""Resume-safe Pass 1 runner with explicit failure/low-confidence logging.

Reads data/input_apps.json, runs the Composio + Tavily research for each app,
writes incremental checkpoints to data/pass1_results.json, logs every failed
stage to data/pass1_failures.jsonl, and builds data/pass2_queue.json for the
verification pass.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

try:
    from .common import (
        SEARCH_QUERY_A,
        SEARCH_QUERY_B,
        SYSTEM_PROMPT,
        USER_ID,
        ToolExecutionError,
        ToolInputRequiredError,
        build_llm_clients,
        collect_text,
        execute_tool_async,
        load_records,
        now_iso,
        read_jsonl,
        save_records,
        synthesize_structured,
        top_official_urls,
    )
    from .schema import AppResearchRecord
except ImportError:  # allows `python src/run_pass1.py`
    from common import (  # type: ignore[no-redef]
        SEARCH_QUERY_A,
        SEARCH_QUERY_B,
        SYSTEM_PROMPT,
        USER_ID,
        ToolExecutionError,
        ToolInputRequiredError,
        build_llm_clients,
        collect_text,
        execute_tool_async,
        load_records,
        now_iso,
        read_jsonl,
        save_records,
        synthesize_structured,
        top_official_urls,
    )
    from schema import AppResearchRecord  # type: ignore[no-redef]

try:
    from composio import Composio
except ImportError:
    Composio = None  # type: ignore[assignment]

CONSOLE = Console()

QUEUE_TARGET_MIN = 35
QUEUE_TARGET_MAX = 50
QUEUE_TARGET = 42


class _FailureLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")
        self.count = 0

    def append(self, *, app: str, stage: str, message: str, log_id: str | None = None) -> None:
        entry = {
            "app_name": app,
            "stage": stage,
            "error_message": message,
            "log_id": log_id,
            "timestamp": now_iso(),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        self.count += 1


class Pass1Runner:
    def __init__(
        self,
        input_path: Path,
        pass1_out: Path,
        failures_path: Path,
        queue_out: Path,
        concurrency: int = 5,
        limit: int | None = None,
    ) -> None:
        self.input_path = input_path
        self.pass1_out = pass1_out
        self.failures_path = failures_path
        self.queue_out = queue_out
        self.concurrency = max(1, concurrency)
        self.limit = limit
        self.failures = _FailureLog(failures_path)
        self.composio_calls = 0
        self.llm_calls = 0
        self.records: dict[str, AppResearchRecord] = {}

    def run(self) -> None:
        load_dotenv()
        self._check_env()

        if not self.input_path.exists():
            CONSOLE.print(f"[red]Input file not found: {self.input_path}[/red]")
            sys.exit(1)

        apps = json.loads(self.input_path.read_text(encoding="utf-8"))
        if not isinstance(apps, list) or not all(isinstance(a, str) for a in apps):
            raise SystemExit("input_apps.json must be a JSON list of app name strings")
        if self.limit is not None and self.limit > 0:
            apps = apps[: self.limit]

        self.records = load_records(self.pass1_out, AppResearchRecord)
        remaining = [a for a in apps if a not in self.records]
        CONSOLE.print(
            f"[bold]Pass 1[/bold] — {len(remaining)} of {len(apps)} remaining "
            f"({len(self.records)} already checkpointed)"
        )

        session = Composio().sessions.create(user_id=USER_ID, toolkits=["tavily"])  # type: ignore[union-attr]
        asyncio.run(self._amain(session, remaining))
        self._build_queue(apps)
        self._print_summary(len(apps))

    def _check_env(self) -> None:
        if Composio is None:
            CONSOLE.print("[red]composio SDK not installed. Run: pip install -r requirements.txt[/red]")
            sys.exit(1)
        missing = [v for v in ("COMPOSIO_API_KEY",) if not os.getenv(v)]
        has_llm = os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or os.getenv("NIM_API_KEY")
        if missing or not has_llm:
            msg = "Missing required env vars"
            if missing:
                msg += f": {', '.join(missing)}"
            if not has_llm:
                msg += " and OPENAI_API_KEY or ANTHROPIC_API_KEY or NIM_API_KEY"
            CONSOLE.print(f"[red]{msg}[/red]")
            sys.exit(1)

    async def _amain(self, session: Any, apps: list[str]) -> None:
        openai_client, anthropic_client, nim_client = build_llm_clients()
        sem = asyncio.Semaphore(self.concurrency)

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
                try:
                    record = await self._research_with_retry(session, app, openai_client, anthropic_client, nim_client)
                except ToolInputRequiredError as exc:
                    self.failures.append(
                        app=app, stage="tool_input_required", message=str(exc), log_id=getattr(exc, "log_id", None)
                    )
                except ToolExecutionError as exc:
                    self.failures.append(app=app, stage=exc.stage, message=str(exc), log_id=exc.log_id)
                except asyncio.TimeoutError:
                    self.failures.append(app=app, stage="timeout", message="async operation timed out")
                except Exception as exc:  # noqa: BLE001 - logged, pipeline continues
                    self.failures.append(app=app, stage="unknown", message=repr(exc))
                else:
                    self.records[app] = record
                    self._save()
                progress.advance(task_id)

        with progress:
            for coro in asyncio.as_completed([process(a) for a in apps]):
                try:
                    await coro
                except Exception as exc:  # noqa: BLE001 - defensive
                    CONSOLE.print(f"[red]Unhandled: {exc}[/red]")

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_not_exception_type(ToolInputRequiredError),
        reraise=True,
    )
    async def _research_with_retry(
        self, session: Any, app: str, openai_client: Any, anthropic_client: Any, nim_client: Any
    ) -> AppResearchRecord:
        query_a = SEARCH_QUERY_A.format(app=app)
        query_b = SEARCH_QUERY_B.format(app=app)

        search_a, search_b = await asyncio.gather(
            self._execute(session, "TAVILY_SEARCH", {"query": query_a}, stage="search"),
            self._execute(session, "TAVILY_SEARCH", {"query": query_b}, stage="search"),
        )

        extract_text = ""
        urls = top_official_urls(search_a, app)
        if urls:
            try:
                extracted = await self._execute(session, "TAVILY_EXTRACT", {"urls": urls}, stage="extract")
                extract_text = collect_text(extracted)
            except ToolExecutionError as exc:
                self.failures.append(
                    app=app, stage=f"extract_soft:{exc.stage}", message=str(exc), log_id=exc.log_id
                )

        context = (
            f"SEARCH RESULTS (Query A — developer docs):\n{collect_text(search_a)}\n\n"
            f"SEARCH RESULTS (Query B — MCP server):\n{collect_text(search_b)}\n\n"
            f"EXTRACTED DOCUMENT TEXT:\n{extract_text or '(no extractable content)'}"
        )

        self.llm_calls += 1
        try:
            return await synthesize_structured(
                app_name=app,
                context=context,
                system_prompt=SYSTEM_PROMPT,
                model_cls=AppResearchRecord,
                openai_client=openai_client,
                anthropic_client=anthropic_client,
                nim_client=nim_client,
            )
        except Exception as exc:  # noqa: BLE001
            raise ToolExecutionError(f"schema/LLM synthesis failed: {exc}", stage="schema_parse") from exc

    async def _execute(self, session: Any, tool: str, arguments: dict[str, Any], *, stage: str) -> Any:
        self.composio_calls += 1
        return await execute_tool_async(session, tool, arguments, stage=stage)

    def _save(self) -> None:
        save_records(self.pass1_out, self.records)

    def _build_queue(self, all_apps: list[str]) -> None:
        failures = read_jsonl(self.failures_path)
        failed_apps = {f.get("app_name") for f in failures if f.get("app_name")}
        reasons: dict[str, list[str]] = defaultdict(list)
        queue_map: dict[str, str] = {}

        for app in all_apps:
            if app in failed_apps:
                queue_map[app] = "failure"
                reasons["failure"].append(app)

        remaining: list[str] = []
        for app, rec in self.records.items():
            if app in queue_map:
                continue
            if rec.access_model in ("Partner_Or_Sales_Gated", "Admin_Approval_Required") or rec.existing_mcp != "None_Found":
                queue_map[app] = "gated_or_mcp_flag"
                reasons["gated_or_mcp_flag"].append(app)
            elif rec.confidence_score < 4:
                queue_map[app] = "low_confidence"
                reasons["low_confidence"].append(app)
            elif app in all_apps:
                remaining.append(app)

        queue = [{"app_name": a, "reason": r} for a, r in sorted(queue_map.items())]
        target = max(QUEUE_TARGET_MIN, min(QUEUE_TARGET_MAX, QUEUE_TARGET))
        needed = max(0, target - len(queue))
        if needed > 0 and remaining:
            by_cat: dict[str, list[str]] = defaultdict(list)
            for app in remaining:
                cat = self.records[app].category
                by_cat[cat].append(app)
            cats = sorted(by_cat)
            idx = 0
            added = 0
            while added < needed:
                cat = cats[idx % len(cats)]
                if by_cat[cat]:
                    pick = by_cat[cat].pop(0)
                    queue_map[pick] = "stratified_sample"
                    reasons["stratified_sample"].append(pick)
                    added += 1
                idx += 1
                if not any(by_cat.values()):
                    break
            queue = [{"app_name": a, "reason": r} for a, r in sorted(queue_map.items())]

        payload = {
            "generated_at": now_iso(),
            "total_apps": len(all_apps),
            "pass1_records": len(self.records),
            "pass1_failures": len(failures),
            "queue_size": len(queue),
            "reasons": {k: v for k, v in reasons.items()},
            "queue": queue,
        }
        self.queue_out.parent.mkdir(parents=True, exist_ok=True)
        self.queue_out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _print_summary(self, total_apps: int) -> None:
        records = list(self.records.values())
        total = len(records)
        buildable = sum(1 for r in records if r.buildability_verdict == "Buildable_Today")
        blocked = sum(1 for r in records if r.buildability_verdict == "Blocked")
        workaround = total - buildable - blocked
        low_conf = sum(1 for r in records if r.confidence_score < 4)
        fail_count = self.failures.count

        table = Table(title="Pass 1 Summary")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")
        table.add_row("Total apps in input", str(total_apps))
        table.add_row("Records produced", str(total))
        table.add_row("Pass 1 failures logged", str(fail_count))
        table.add_row("Buildable_Today", str(buildable))
        table.add_row("Buildable_With_Workaround", str(workaround))
        table.add_row("Blocked", str(blocked))
        table.add_row("Low confidence (<4) — flagged", str(low_conf))
        table.add_row("Composio execute calls", str(self.composio_calls))
        table.add_row("LLM synthesis calls", str(self.llm_calls))
        CONSOLE.print(table)

        qp = json.loads(self.queue_out.read_text(encoding="utf-8")) if self.queue_out.exists() else {}
        if qp:
            qt = Table(title="Pass 2 Queue")
            qt.add_column("Reason", style="bold")
            qt.add_column("Count", justify="right")
            for reason, apps in qp.get("reasons", {}).items():
                qt.add_row(reason, str(len(apps)))
            qt.add_row("Queue total", str(qp.get("queue_size", 0)))
            CONSOLE.print(qt)

        if fail_count:
            CONSOLE.print(f"\n[bold yellow]Failures logged →[/bold yellow] {self.failures_path}")
        CONSOLE.print(f"[green]Results →[/green] {self.pass1_out}  [green]Queue →[/green] {self.queue_out}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Resume-safe Pass 1 runner (Composio + Tavily)")
    p.add_argument("--input", default="data/input_apps.json", help="Input apps JSON list")
    p.add_argument("--pass1-out", default="data/pass1_results.json", help="Pass 1 results JSON output")
    p.add_argument("--failures-out", default="data/pass1_failures.jsonl", help="Failures JSONL output")
    p.add_argument("--queue-out", default="data/pass2_queue.json", help="Pass 2 queue JSON output")
    p.add_argument("--pass2-out", default="data/pass2_verified.json", help="Pass 2 verified JSON output (informational)")
    p.add_argument("--concurrency", type=int, default=5, help="Max concurrent app workers")
    p.add_argument("--limit", type=int, default=None, help="Cap number of apps to process (smoke tests)")
    return p


def main() -> None:
    args = build_parser().parse_args()
    _ = args.pass2_out
    runner = Pass1Runner(
        input_path=Path(args.input),
        pass1_out=Path(args.pass1_out),
        failures_path=Path(args.failures_out),
        queue_out=Path(args.queue_out),
        concurrency=args.concurrency,
        limit=args.limit,
    )
    runner.run()


if __name__ == "__main__":
    main()