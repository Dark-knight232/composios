"""Pass 2 verification: primary-doc HTTP checks + headless fallback + LLM re-evaluation.

Reads data/pass2_queue.json and data/pass1_results.json, validates each app's
evidence URLs via live HTTP (aiohttp/httpx), falls back to Playwright for
JS-heavy/gated pages, then re-evaluates the structured record and writes a
diff-enriched output to data/pass2_verified.json. Also produces a human-audit
sample (data/human_audit_sample.json).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

try:
    from .common import (
        SYSTEM_PROMPT,
        USER_ID,
        TOOLKIT,
        build_llm_clients,
        collect_text,
        load_records,
        now_iso,
        save_records,
        synthesize_structured,
        tavily_search_direct,
        top_official_urls,
    )
    from .schema import AppResearchRecord
except ImportError:  # allows `python src/verify_pass2.py`
    from common import (  # type: ignore[no-redef]
        SYSTEM_PROMPT,
        USER_ID,
        TOOLKIT,
        build_llm_clients,
        collect_text,
        load_records,
        now_iso,
        save_records,
        synthesize_structured,
        tavily_search_direct,
        top_official_urls,
    )
    from schema import AppResearchRecord  # type: ignore[no-redef]

try:
    from composio import Composio
except ImportError:
    Composio = None  # type: ignore[assignment]

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore[assignment]

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

CONSOLE = Console()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 25

LOW_VALUE_DOMAINS = {
    "medium.com",
    "dev.to",
    "reddit.com",
    "stackoverflow.com",
    "wikipedia.org",
    "youtube.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "quora.com",
    "g2.com",
    "capterra.com",
    "getapp.com",
    "softwareadvice.com",
    "trustradius.com",
}

VERIFY_PROMPT = """You are re-evaluating a prior structured research record for an app
using freshly fetched PRIMARY developer documentation.

You are given:
- PASS 1 RECORD (JSON)
- LIVE DOC TEXT extracted from official URLs
- URL CHECK RESULTS (status, final URL, content length, fetch method)

Re-check the following against the LIVE DOC TEXT:
1. auth_methods — does the primary doc explicitly list OAuth2, API Key, etc.?
2. access_model — is there a real self-serve free/trial path, or is a paid plan / enterprise tier /
   partner review / sales gate mentioned?
3. existing_mcp — is any MCP server OFFICIAL (vendor-hosted) or UNOFFICIAL (community)?
4. evidence_urls — are they live, relevant doc pages (not guesses/broken links)?

Return an UPDATED AppResearchRecord (same schema, same app_name). Adjust fields ONLY where
the live doc evidence supports a change; otherwise keep Pass 1 values.
Set confidence_score based on evidence quality (1-5).
Never include URLs not present in the provided checks or Pass 1 evidence.
Return ONLY valid JSON matching the requested schema."""


@dataclass
class UrlCheck:
    url: str
    http_status: int | None = None
    final_url: str | None = None
    is_official_domain_guess: bool = False
    content_length: int = 0
    fetch_method: str = "http"
    error: str | None = None
    text: str = ""
    ok: bool = False

    def to_dict(self, include_text: bool = False) -> dict[str, Any]:
        d = {
            "url": self.url,
            "http_status": self.http_status,
            "final_url": self.final_url,
            "is_official_domain_guess": self.is_official_domain_guess,
            "content_length": self.content_length,
            "fetch_method": self.fetch_method,
            "error": self.error,
            "ok": self.ok,
        }
        if include_text:
            d["text"] = self.text
        return d


def _is_low_value(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return any(d in host for d in LOW_VALUE_DOMAINS)
    except Exception:
        return True


def _slugify(app: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", app.lower())


def _guess_official(url: str, app: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        slug = _slugify(app)
        if slug and slug in host:
            return True
        if any(keyword in host for keyword in ("developer.", "docs.", "api.", "platform.", "reference.", "integration.")):
            return True
    except Exception:
        pass
    return False


async def _fetch_http_aiohttp(session: aiohttp.ClientSession, url: str, timeout: int) -> UrlCheck:
    check = UrlCheck(url=url, fetch_method="aiohttp")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True) as resp:
            check.http_status = resp.status
            check.final_url = str(resp.url)
            body = await resp.text(errors="ignore")
            check.content_length = len(body)
            check.text = body
            check.ok = 200 <= resp.status < 300 and len(body) >= 200
            if not check.ok:
                check.error = f"HTTP {resp.status}"
    except asyncio.TimeoutError:
        check.error = "timeout"
    except Exception as exc:  # noqa: BLE001
        check.error = repr(exc)
    return check


async def _fetch_http_httpx(url: str, timeout: int) -> UrlCheck:
    check = UrlCheck(url=url, fetch_method="httpx")
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(url)
            check.http_status = resp.status_code
            check.final_url = str(resp.url)
            body = resp.text
            check.content_length = len(body)
            check.text = body
            check.ok = 200 <= resp.status_code < 300 and len(body) >= 200
            if not check.ok:
                check.error = f"HTTP {resp.status_code}"
    except httpx.TimeoutException:
        check.error = "timeout"
    except Exception as exc:  # noqa: BLE001
        check.error = repr(exc)
    return check


async def fetch_http(url: str, timeout: int = DEFAULT_TIMEOUT) -> UrlCheck:
    if aiohttp is not None:
        return await _fetch_http_aiohttp(aiohttp.ClientSession(), url, timeout)
    if httpx is not None:
        return await _fetch_http_httpx(url, timeout)
    return UrlCheck(url=url, error="no http client installed (aiohttp/httpx)")


async def fetch_browser(url: str, timeout: int = DEFAULT_TIMEOUT * 2) -> UrlCheck:
    """Headless browser fetch for JS-heavy or login-wall pages. Returns UrlCheck with fetch_method='playwright'."""
    check = UrlCheck(url=url, fetch_method="playwright")
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        check.error = "playwright not installed"
        return check
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(user_agent=USER_AGENT)
            await page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            text = await page.inner_text("body")
            check.text = text
            check.content_length = len(text)
            check.http_status = 200
            check.final_url = page.url
            check.ok = len(text) >= 500
            await browser.close()
    except Exception as exc:  # noqa: BLE001
        check.error = repr(exc)
    return check


def _needs_browser(checks: list[UrlCheck]) -> list[int]:
    """Indices of checks that likely need browser rendering (blocked, thin, JS-heavy)."""
    indices = []
    for i, c in enumerate(checks):
        if not c.ok or c.content_length < 500 or ("login" in c.text.lower() and "sign in" in c.text.lower()):
            indices.append(i)
    return indices


def _needs_discovery(evidence: list[str], checks: list[UrlCheck]) -> bool:
    """Determine if we should run a doc discovery search for official URLs."""
    if not evidence:
        return True
    if not checks:
        return True
    ok_checks = [c for c in checks if c.ok]
    if not ok_checks:
        return True
    if all(_is_low_value(c.final_url or c.url) for c in ok_checks):
        return True
    return False


def _build_context(app: str, pass1: AppResearchRecord | None, checks: list[UrlCheck]) -> str:
    pass1_json = pass1.model_dump_json(indent=2) if pass1 else "null"
    doc_sections = []
    for c in checks:
        if c.ok:
            snippet = c.text[:8000]
            doc_sections.append(f"URL: {c.final_url or c.url} (method={c.fetch_method})\n{snippet}")
    docs = "\n\n---\n\n".join(doc_sections) if doc_sections else "(no live content extracted)"
    return f"PASS 1 RECORD:\n{pass1_json}\n\nLIVE DOC TEXT:\n{docs}"


async def _discover_docs(app: str) -> list[str]:
    """Run a Tavily search for official docs using the direct Tavily API."""
    try:
        result = await tavily_search_direct(f"{app} official API documentation site:dev OR site:developer OR site:docs", max_results=3, include_raw_content=False)
        return top_official_urls(result, app, limit=2)
    except Exception:
        return []


def _compute_diff(
    before: AppResearchRecord,
    after: AppResearchRecord,
    checks: list[UrlCheck],
) -> dict[str, Any]:
    b = before.model_dump(mode="json")
    a = after.model_dump(mode="json")
    changed: list[str] = []
    before_after: dict[str, dict[str, Any]] = {}
    for key in b:
        if b[key] != a.get(key):
            changed.append(key)
            before_after[key] = {"before": b[key], "after": a.get(key)}
    verified = sum(1 for c in checks if c.ok)
    broken = sum(1 for c in checks if not c.ok)
    final_conf = after.confidence_score
    priority = _audit_priority(changed, final_conf, broken)
    return {
        "changed_fields": changed,
        "before_after": before_after,
        "verified_url_count": verified,
        "broken_url_count": broken,
        "final_confidence_score": final_conf,
        "human_audit_priority": priority,
    }


def _audit_priority(changed: list[str], conf: int, broken: int) -> str:
    if broken > 0 or conf <= 2 or "access_model" in changed or "buildability_verdict" in changed:
        return "High"
    if conf == 3 or any(f in changed for f in ("auth_methods", "existing_mcp", "evidence_urls", "endpoint_breadth")):
        return "Medium"
    return "Low"


def _build_audit_sample(entries: list[dict[str, Any]], target: int = 18) -> list[dict[str, Any]]:
    if not entries:
        return []
    target = min(target, len(entries))
    chosen: list[dict[str, Any]] = []
    names: set[str] = set()

    highs = [e for e in entries if e["verification_diff"]["human_audit_priority"] == "High"]
    for e in highs[:target]:
        chosen.append(e)
        names.add(e["app_name"])

    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in entries:
        if e["app_name"] in names:
            continue
        cat = e.get("pass2", {}).get("category", "Other")
        by_cat[cat].append(e)

    cats = sorted(by_cat)
    idx = 0
    while len(chosen) < target and any(by_cat[c] for c in cats):
        cat = cats[idx % len(cats)]
        if by_cat[cat]:
            e = by_cat[cat].pop(0)
            chosen.append(e)
            names.add(e["app_name"])
        idx += 1

    sample = []
    for e in chosen:
        diff = e["verification_diff"]
        sample.append(
            {
                "app_name": e["app_name"],
                "category": e.get("pass2", {}).get("category"),
                "human_audit_priority": diff["human_audit_priority"],
                "final_confidence_score": diff["final_confidence_score"],
                "changed_fields": diff["changed_fields"],
                "reason": "gated/changed row" if diff["changed_fields"] else "category coverage",
            }
        )
    return sample


class Pass2Verifier:
    def __init__(
        self,
        queue_path: Path,
        pass1_path: Path,
        pass2_out: Path,
        audit_out: Path,
        concurrency: int = 5,
        limit: int | None = None,
    ) -> None:
        self.queue_path = queue_path
        self.pass1_path = pass1_path
        self.pass2_out = pass2_out
        self.audit_out = audit_out
        self.concurrency = max(1, concurrency)
        self.limit = limit
        self.verified: list[dict[str, Any]] = []
        self.queue: list[dict[str, Any]] = []

    def run(self) -> None:
        load_dotenv()
        self._load_inputs()
        asyncio.run(self._amain())
        self._print_summary()
        self._save_audit_sample()

    def _load_inputs(self) -> None:
        if not self.queue_path.exists():
            CONSOLE.print(f"[red]Queue file not found: {self.queue_path}[/red]")
            sys.exit(1)
        if not self.pass1_path.exists():
            CONSOLE.print(f"[red]Pass 1 results not found: {self.pass1_path}[/red]")
            sys.exit(1)

        qdata = json.loads(self.queue_path.read_text(encoding="utf-8"))
        self.queue = qdata.get("queue", [])
        if self.limit is not None and self.limit > 0:
            self.queue = self.queue[: self.limit]

        self.pass1_records = load_records(self.pass1_path, AppResearchRecord)
        self.verified = []
        if self.pass2_out.exists():
            try:
                self.verified = json.loads(self.pass2_out.read_text(encoding="utf-8"))
            except Exception:
                self.verified = []

    async def _amain(self) -> None:
        openai_client, anthropic_client, nim_client = build_llm_clients()
        has_llm = openai_client is not None or anthropic_client is not None or nim_client is not None

        session = None
        if Composio is not None and os.getenv("COMPOSIO_API_KEY"):
            try:
                session = Composio().sessions.create(user_id=USER_ID, toolkits=[TOOLKIT])  # type: ignore[union-attr]
            except Exception:
                session = None

        sem = asyncio.Semaphore(self.concurrency)
        processed = {e["app_name"] for e in self.verified}

        progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            console=CONSOLE,
        )
        task_id = progress.add_task(f"Verifying {len(self.queue)} apps", total=len(self.queue))

        async def process(entry: dict[str, Any]) -> None:
            app = entry["app_name"]
            if app in processed:
                progress.advance(task_id)
                return
            async with sem:
                out = await self._verify_one(app, session, openai_client, anthropic_client, nim_client, has_llm)
            self.verified.append(out)
            self._save_verified()
            progress.advance(task_id)

        with progress:
            for coro in asyncio.as_completed([process(e) for e in self.queue]):
                try:
                    await coro
                except Exception as exc:  # noqa: BLE001
                    CONSOLE.print(f"[red]Unhandled verification error: {exc}[/red]")

    async def _verify_one(
        self,
        app: str,
        session: Any,
        openai_client: Any,
        anthropic_client: Any,
        nim_client: Any,
        has_llm: bool,
    ) -> dict[str, Any]:
        pass1 = self.pass1_records.get(app)
        evidence = pass1.evidence_urls if pass1 else []

        checks = await asyncio.gather(*[fetch_http(u) for u in evidence])
        for i, c in enumerate(checks):
            if c.is_official_domain_guess is False:
                c.is_official_domain_guess = _guess_official(c.final_url or c.url, app)

        browser_indices = _needs_browser(checks)
        for i in browser_indices:
            rendered = await fetch_browser(checks[i].final_url or checks[i].url)
            if rendered.ok and (not checks[i].ok or len(rendered.text) > len(checks[i].text)):
                checks[i] = rendered

        if _needs_discovery(evidence, checks):
            discovered = await _discover_docs(app)
            for u in discovered:
                if u not in evidence:
                    c = await fetch_http(u)
                    c.is_official_domain_guess = _guess_official(c.final_url or c.url, app)
                    checks.append(c)

        if pass1 is None and has_llm:
            fresh = await self._synthesize_fresh(app, checks, openai_client, anthropic_client, nim_client)
            before = fresh
            after = fresh
        elif pass1 is not None and has_llm:
            context = _build_context(app, pass1, checks)
            after = await synthesize_structured(
                app_name=app,
                context=context,
                system_prompt=VERIFY_PROMPT,
                model_cls=AppResearchRecord,
                openai_client=openai_client,
                anthropic_client=anthropic_client,
                nim_client=nim_client,
            )
            before = pass1
        else:
            after = pass1 or AppResearchRecord(
                app_name=app,
                category="Other",
                one_liner="Unable to research",
                auth_methods=["Unknown"],
                access_model="No_Public_API",
                access_model_notes="No LLM available for synthesis",
                api_surface=["None"],
                endpoint_breadth="None",
                existing_mcp="None_Found",
                buildability_verdict="Blocked",
                primary_blocker="No LLM available",
                evidence_urls=[],
                confidence_score=1,
                pass_1_raw_notes="No LLM available for Pass 2",
            )
            before = after

        diff = _compute_diff(before, after, checks)
        return {
            "app_name": app,
            "pass1": before.model_dump(mode="json"),
            "pass2": after.model_dump(mode="json"),
            "url_checks": [c.to_dict() for c in checks],
            "verification_diff": diff,
            "verified_at": now_iso(),
        }

    async def _synthesize_fresh(
        self, app: str, checks: list[UrlCheck], openai_client: Any, anthropic_client: Any, nim_client: Any
    ) -> AppResearchRecord:
        context = _build_context(app, None, checks)
        return await synthesize_structured(
            app_name=app,
            context=context,
            system_prompt=SYSTEM_PROMPT,
            model_cls=AppResearchRecord,
            openai_client=openai_client,
            anthropic_client=anthropic_client,
            nim_client=nim_client,
        )

    def _save_verified(self) -> None:
        self.pass2_out.parent.mkdir(parents=True, exist_ok=True)
        self.pass2_out.write_text(json.dumps(self.verified, indent=2), encoding="utf-8")

    def _print_summary(self) -> None:
        total = len(self.verified)
        changed_apps = sum(1 for e in self.verified if e["verification_diff"]["changed_fields"])
        field_counts = Counter(f for e in self.verified for f in e["verification_diff"]["changed_fields"])
        verified_urls = sum(e["verification_diff"]["verified_url_count"] for e in self.verified)
        broken_urls = sum(e["verification_diff"]["broken_url_count"] for e in self.verified)

        table = Table(title="Pass 2 Verification Summary")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")
        table.add_row("Total verified", str(total))
        table.add_row("Apps with ≥1 field changed", str(changed_apps))
        table.add_row("Live evidence URLs", str(verified_urls))
        table.add_row("Broken/unreachable URLs", str(broken_urls))
        if field_counts:
            top = ", ".join(f"{k}({v})" for k, v in field_counts.most_common(5))
            table.add_row("Top changed fields", top)
        CONSOLE.print(table)

        qdata = json.loads(self.queue_path.read_text(encoding="utf-8")) if self.queue_path.exists() else {}
        qt = Table(title="Pass 2 Queue Composition")
        qt.add_column("Reason", style="bold")
        qt.add_column("Count", justify="right")
        for reason, apps in qdata.get("reasons", {}).items():
            qt.add_row(reason, str(len(apps)))
        qt.add_row("Queue total", str(qdata.get("queue_size", len(self.queue))))
        CONSOLE.print(qt)

        CONSOLE.print(f"\n[green]Verified →[/green] {self.pass2_out}")

    def _save_audit_sample(self) -> None:
        sample = _build_audit_sample(self.verified, target=18)
        self.audit_out.parent.mkdir(parents=True, exist_ok=True)
        self.audit_out.write_text(json.dumps(sample, indent=2), encoding="utf-8")
        CONSOLE.print(f"[green]Audit sample →[/green] {self.audit_out}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pass 2 — primary-doc verification & re-evaluation")
    p.add_argument("--input", default="data/pass1_results.json", help="Pass 1 results JSON (alias for --pass1-out)")
    p.add_argument("--pass1-out", default="data/pass1_results.json", help="Pass 1 results JSON")
    p.add_argument("--queue-out", default="data/pass2_queue.json", help="Pass 2 queue JSON input")
    p.add_argument("--pass2-out", default="data/pass2_verified.json", help="Pass 2 verified JSON output")
    p.add_argument("--audit-out", default="data/human_audit_sample.json", help="Human audit sample JSON output")
    p.add_argument("--concurrency", type=int, default=5, help="Max concurrent app workers")
    p.add_argument("--limit", type=int, default=None, help="Cap number of apps to verify (smoke tests)")
    return p


def main() -> None:
    args = build_parser().parse_args()
    verifier = Pass2Verifier(
        queue_path=Path(args.queue_out),
        pass1_path=Path(args.pass1_out if args.pass1_out else args.input),
        pass2_out=Path(args.pass2_out),
        audit_out=Path(args.audit_out),
        concurrency=args.concurrency,
        limit=args.limit,
    )
    verifier.run()


if __name__ == "__main__":
    main()