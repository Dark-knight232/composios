"""Systemic error fix helper + product-ops pattern synthesis.

Reads data/audit_report.json and data/final_verified_apps.json to:
  1. Print actionable prompt/schema fixes derived from error_category / audit_notes.
  2. Apply systematic overrides to data/final_verified_apps.json.
  3. Generate data/case_study_patterns.json with headline takeaways, distributions,
     self-serve vs. gated matrix, top blockers/outreach targets, and the agent-vs-human
     boundary summary.

CLI subcommands:
  fix-prompts      - print suggested prompt/schema fixes from audit_report.json
  synthesize       - generate case_study_patterns.json
  apply-overrides  - rewrite final_verified_apps.json with systematic rule fixes
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

try:
    from .schema import AppResearchRecord
except ImportError:  # allows `python src/synthesize_patterns.py`
    from schema import AppResearchRecord  # type: ignore[no-redef]

CONSOLE = Console()

CORE_FIELDS = ("auth_methods", "access_model", "api_surface", "existing_mcp", "buildability_verdict")


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8") or "null")
    except json.JSONDecodeError:
        return None


def _load_final_apps(path: Path) -> list[dict[str, Any]]:
    data = _load_json(path)
    if isinstance(data, list):
        return data
    return []


# ---------------------------------------------------------------------------
# 1. Systemic error-fix helper
# ---------------------------------------------------------------------------

def fix_prompts_from_errors(audit_report_path: Path) -> None:
    report = _load_json(audit_report_path)
    if not report:
        CONSOLE.print(f"[red]Audit report not found: {audit_report_path}[/red]")
        sys.exit(1)

    root_causes: dict[str, int] = report.get("root_cause_breakdown", {})
    misses = report.get("hits_and_misses", [])

    CONSOLE.print("[bold]Systemic Error -> Prompt/Schema Fix Suggestions[/bold]\n")

    # fake_self_serve_hidden_tier
    if root_causes.get("fake_self_serve_hidden_tier", 0):
        CONSOLE.rule("[red]fake_self_serve_hidden_tier[/red]")
        CONSOLE.print(
            "Pass 1/Pass 2 mislabeled a 'self-serve free' app that actually hides API access "
            "behind a paid tier or partner review. Add a negative constraint to the LLM prompt:\n"
            '  "If a free tier exists but requires a credit card, sales contact, or partner '
            'review for API access, classify access_model as Self_Serve_Paid_Or_Trial or '
            'Partner_Or_Sales_Gated — never Self_Serve_Free."\n'
            "Also add schema guidance: free developer sandbox must allow API calls without "
            "sales approval."
        )
    # community_vs_official_mcp
    if root_causes.get("community_vs_official_mcp", 0):
        CONSOLE.rule("[red]community_vs_official_mcp[/red]")
        CONSOLE.print(
            "Community GitHub MCP repos were mistaken for official vendor MCPs. Tighten the "
            "existing_mcp rule:\n"
            '  Official_MCP = MCP server hosted on the vendor domain (app.com/...) or '
            'linked from official docs.\n'
            '  Community_MCP = GitHub/org repos not on the vendor domain.\n'
            "Update the LLM prompt to require evidence_urls on the vendor domain for "
            "Official_MCP."
        )
    # broken_or_outdated_url
    if root_causes.get("broken_or_outdated_url", 0):
        CONSOLE.rule("[red]broken_or_outdated_url[/red]")
        CONSOLE.print(
            "evidence_urls contained 404/redirect-off-domain/blog links. Add a validation "
            "rule: reject evidence_urls that return non-2xx or resolve to non-doc domains "
            "(medium.com, reddit.com, g2.com, etc.). Require at least one live 2xx doc URL "
            "for confidence_score >= 4."
        )
    # ambiguous_product_scope
    if root_causes.get("ambiguous_product_scope", 0):
        CONSOLE.rule("[red]ambiguous_product_scope[/red]")
        CONSOLE.print(
            "LLM was unsure whether the app exposes a public API vs. only an embed/widget. "
            "Add a disambiguation step: search for '{app} API reference' and '{app} "
            "developers portal'; if neither returns live docs, mark api_surface as None and "
            "buildability_verdict as Blocked."
        )
    # schema_or_prompt_rule_gap
    if root_causes.get("schema_or_prompt_rule_gap", 0):
        CONSOLE.rule("[red]schema_or_prompt_rule_gap[/red]")
        CONSOLE.print(
            "Schema/prompt rule mismatch. Review AppResearchRecord field constraints and the "
            "system prompt. Ensure consistency between access_model and buildability_verdict "
            "(e.g., No_Public_API -> Blocked). Add a cross-field validator in the Pydantic model."
        )

    # Per-field miss examples for prompt tuning
    if misses:
        CONSOLE.rule("[bold]Concrete miss examples for prompt tuning[/bold]")
        tbl = Table()
        tbl.add_column("App", style="bold")
        tbl.add_column("Field", style="bold")
        tbl.add_column("Human", style="dim")
        tbl.add_column("Pipeline said", style="dim")
        for ex in misses[:12]:
            for f, detail in ex.get("fields", {}).items():
                if not detail.get("pass2_ok") or not detail.get("pass1_ok"):
                    tbl.add_row(
                        ex["app_name"],
                        str(f),
                        str(detail.get("human")),
                        f"P1={detail.get('pass1')} / P2={detail.get('pass2')}",
                    )
        CONSOLE.print(tbl)


# ---------------------------------------------------------------------------
# 2. Apply systematic overrides to final_verified_apps.json
# ---------------------------------------------------------------------------

def apply_systematic_overrides(apps_path: Path, audit_report_path: Path, out_path: Path | None = None) -> None:
    apps = _load_final_apps(apps_path)
    report = _load_json(audit_report_path) or {}
    overrides_by_app: dict[str, dict[str, Any]] = {}
    for ex in report.get("hits_and_misses", []):
        app = ex.get("app_name")
        if not app:
            continue
        field_overrides: dict[str, Any] = {}
        for f in CORE_FIELDS:
            detail = ex.get("fields", {}).get(f, {})
            human = detail.get("human")
            if human is not None:
                field_overrides[f] = human
        hb = ex.get("fields", {}).get("primary_blocker", {}).get("human")
        if hb is not None:
            field_overrides["primary_blocker"] = hb
        if field_overrides:
            overrides_by_app[app] = field_overrides

    fixed = 0
    for app in apps:
        name = app.get("app_name")
        if name in overrides_by_app:
            for f, val in overrides_by_app[name].items():
                app[f] = val
            app["systematic_override_applied"] = True
            app["override_source"] = "audit_report"
            fixed += 1
        else:
            app.setdefault("systematic_override_applied", False)

    target = out_path or apps_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(apps, indent=2), encoding="utf-8")
    CONSOLE.print(f"[green]Applied systematic overrides[/green] -> {target}  ({fixed} apps patched)")


# ---------------------------------------------------------------------------
# 3. Pattern & cluster synthesis
# ---------------------------------------------------------------------------

def _pct(n: int, total: int) -> float:
    return round((n / total * 100) if total else 0.0, 1)


def synthesize_patterns(apps_path: Path, audit_report_path: Path, out_path: Path) -> None:
    apps = _load_final_apps(apps_path)
    report = _load_json(audit_report_path) or {}
    total = len(apps)

    if not total:
        CONSOLE.print(f"[red]No final verified apps found: {apps_path}[/red]")
        sys.exit(1)

    # --- Auth & API surface distribution ------------------------------------
    auth_counter: Counter[str] = Counter()
    surface_counter: Counter[str] = Counter()
    breadth_counter: Counter[str] = Counter()
    mcp_counter: Counter[str] = Counter()
    access_counter: Counter[str] = Counter()
    verdict_counter: Counter[str] = Counter()
    blocker_counter: Counter[str] = Counter()
    category_counter: Counter[str] = Counter()

    cat_access: dict[str, Counter[str]] = defaultdict(Counter)
    cat_verdict: dict[str, Counter[str]] = defaultdict(Counter)

    for app in apps:
        for f in ("auth_methods", "api_surface"):
            for v in app.get(f) or []:
                if f == "auth_methods":
                    auth_counter[v] += 1
                else:
                    surface_counter[v] += 1
        breadth_counter[app.get("endpoint_breadth") or "Unknown"] += 1
        mcp_counter[app.get("existing_mcp") or "Unknown"] += 1
        access_counter[app.get("access_model") or "Unknown"] += 1
        verdict_counter[app.get("buildability_verdict") or "Unknown"] += 1
        blocker_counter[app.get("primary_blocker") or "None"] += 1
        cat = app.get("category") or "Other"
        category_counter[cat] += 1
        cat_access[cat][app.get("access_model") or "Unknown"] += 1
        cat_verdict[cat][app.get("buildability_verdict") or "Unknown"] += 1

    # --- Headline takeaways --------------------------------------------------
    buildable = verdict_counter.get("Buildable_Today", 0)
    blocked = verdict_counter.get("Blocked", 0)
    workaround = verdict_counter.get("Buildable_With_Workaround", 0)
    official_mcp = mcp_counter.get("Official_MCP", 0)
    community_mcp = mcp_counter.get("Community_MCP", 0)
    free_self_serve = access_counter.get("Self_Serve_Free", 0)
    gated = access_counter.get("Partner_Or_Sales_Gated", 0) + access_counter.get("Admin_Approval_Required", 0)

    takeaways: list[str] = []
    takeaways.append(
        f"{_pct(buildable, total)}% of apps ({buildable}/{total}) are Buildable_Today; "
        f"{_pct(blocked, total)}% are Blocked ({blocked}) and {_pct(workaround, total)}% need a workaround ({workaround})."
    )
    takeaways.append(
        f"OAuth2 is the dominant auth method ({auth_counter.get('OAuth2', 0)} apps, "
        f"{_pct(auth_counter.get('OAuth2', 0), total)}%), followed by API Key "
        f"({auth_counter.get('API_Key', 0)} apps)."
    )
    takeaways.append(
        f"Only {_pct(official_mcp, total)}% ({official_mcp}) have an Official MCP; "
        f"{_pct(community_mcp, total)}% ({community_mcp}) rely on community MCPs — "
        f"the rest ({mcp_counter.get('None_Found', 0)}) need one built from scratch."
    )
    easy_win_cats = [
        c for c, v in cat_verdict.items()
        if v.get("Buildable_Today", 0) and v.get("Buildable_Today", 0) >= max(1, sum(v.values()) * 0.6)
    ]
    if easy_win_cats:
        takeaways.append(
            "Easy-win outreach categories (majority Buildable_Today): " + ", ".join(easy_win_cats)
        )
    if gated:
        takeaways.append(
            f"{gated} apps are partner/sales-gated — prioritize these for BD outreach rather than self-serve onboarding."
        )

    # --- Self-serve vs gated matrix -----------------------------------------
    matrix: list[dict[str, Any]] = []
    for cat in sorted(category_counter):
        row = {"category": cat, "total": category_counter[cat]}
        for am in ("Self_Serve_Free", "Self_Serve_Paid_Or_Trial", "Admin_Approval_Required",
                   "Partner_Or_Sales_Gated", "No_Public_API"):
            row[am] = cat_access[cat].get(am, 0)
        for bv in ("Buildable_Today", "Buildable_With_Workaround", "Blocked"):
            row[bv] = cat_verdict[cat].get(bv, 0)
        matrix.append(row)

    # --- Top blockers & outreach targets ------------------------------------
    top_blockers = blocker_counter.most_common()
    outreach_targets: list[dict[str, Any]] = []
    for app in apps:
        if app.get("buildability_verdict") in ("Blocked", "Buildable_With_Workaround") or app.get("access_model") in (
            "Partner_Or_Sales_Gated", "Admin_Approval_Required",
        ):
            outreach_targets.append(
                {
                    "app_name": app.get("app_name"),
                    "category": app.get("category"),
                    "access_model": app.get("access_model"),
                    "buildability_verdict": app.get("buildability_verdict"),
                    "primary_blocker": app.get("primary_blocker"),
                    "priority": "High" if app.get("buildability_verdict") == "Blocked" else "Medium",
                }
            )
    outreach_targets.sort(key=lambda t: (0 if t["priority"] == "High" else 1, t["app_name"]))

    # --- Agent vs human boundary --------------------------------------------
    human_overrides = sum(1 for a in apps if a.get("human_overrides_applied"))
    systematic = sum(1 for a in apps if a.get("systematic_override_applied"))
    low_conf = sum(1 for a in apps if (a.get("confidence_score") or 5) < 4)
    boundary = {
        "total_apps": total,
        "automated_only": total - human_overrides,
        "human_overrides_applied": human_overrides,
        "systematic_overrides_applied": systematic,
        "low_confidence_rows": low_conf,
        "pass1_row_accuracy": report.get("pass1_row_accuracy"),
        "pass2_row_accuracy": report.get("pass2_row_accuracy"),
        "net_accuracy_lift": report.get("net_accuracy_lift"),
        "notes": (
            "Automation reliably handled URL discovery, auth-method extraction, and "
            "broad categorization. Human judgment was essential for gated/paid-tier "
            "classification, official-vs-community MCP distinction, and broken-URL detection."
        ),
    }

    patterns = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_apps": total,
        "headline_takeaways": takeaways,
        "auth_methods_distribution": {
            "counts": dict(sorted(auth_counter.items(), key=lambda x: -x[1])),
            "percentages": {k: _pct(v, total) for k, v in sorted(auth_counter.items(), key=lambda x: -x[1])},
        },
        "api_surface_distribution": {
            "counts": dict(sorted(surface_counter.items(), key=lambda x: -x[1])),
            "percentages": {k: _pct(v, total) for k, v in sorted(surface_counter.items(), key=lambda x: -x[1])},
        },
        "endpoint_breadth_distribution": {
            "counts": dict(sorted(breadth_counter.items(), key=lambda x: -x[1])),
            "percentages": {k: _pct(v, total) for k, v in sorted(breadth_counter.items(), key=lambda x: -x[1])},
        },
        "existing_mcp_distribution": {
            "counts": dict(sorted(mcp_counter.items(), key=lambda x: -x[1])),
            "percentages": {k: _pct(v, total) for k, v in sorted(mcp_counter.items(), key=lambda x: -x[1])},
        },
        "access_model_distribution": {
            "counts": dict(sorted(access_counter.items(), key=lambda x: -x[1])),
            "percentages": {k: _pct(v, total) for k, v in sorted(access_counter.items(), key=lambda x: -x[1])},
        },
        "buildability_verdict_distribution": {
            "counts": dict(sorted(verdict_counter.items(), key=lambda x: -x[1])),
            "percentages": {k: _pct(v, total) for k, v in sorted(verdict_counter.items(), key=lambda x: -x[1])},
        },
        "self_serve_vs_gated_matrix": matrix,
        "top_blockers": [{"blocker": b, "count": c} for b, c in top_blockers if b != "None"],
        "outreach_targets": outreach_targets,
        "agent_vs_human_boundary": boundary,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(patterns, indent=2), encoding="utf-8")
    CONSOLE.print(f"[green]Case study patterns[/green] -> {out_path}")
    _print_patterns_summary(patterns)


def _print_patterns_summary(patterns: dict[str, Any]) -> None:
    table = Table(title="Pattern Synthesis Summary")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    b = patterns["buildability_verdict_distribution"]["counts"]
    table.add_row("Buildable_Today", str(b.get("Buildable_Today", 0)))
    table.add_row("Buildable_With_Workaround", str(b.get("Buildable_With_Workaround", 0)))
    table.add_row("Blocked", str(b.get("Blocked", 0)))
    m = patterns["existing_mcp_distribution"]["counts"]
    table.add_row("Official_MCP", str(m.get("Official_MCP", 0)))
    table.add_row("Community_MCP", str(m.get("Community_MCP", 0)))
    table.add_row("None_Found", str(m.get("None_Found", 0)))
    table.add_row("Outreach targets", str(len(patterns["outreach_targets"])))
    CONSOLE.print(table)

    take = Table(title="Headline Takeaways")
    take.add_column("#", justify="right")
    take.add_column("Takeaway")
    for i, t in enumerate(patterns["headline_takeaways"], 1):
        take.add_row(str(i), t)
    CONSOLE.print(take)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Systemic error fixes + product-ops pattern synthesis")
    sub = p.add_subparsers(dest="command", required=True)

    p_fix = sub.add_parser("fix-prompts", help="Print prompt/schema fixes from audit_report.json")
    p_fix.add_argument("--report", default="data/audit_report.json")

    p_apply = sub.add_parser("apply-overrides", help="Apply audit_report overrides to final_verified_apps.json")
    p_apply.add_argument("--apps", default="data/final_verified_apps.json")
    p_apply.add_argument("--report", default="data/audit_report.json")
    p_apply.add_argument("--out", default=None)

    p_syn = sub.add_parser("synthesize", help="Generate case_study_patterns.json")
    p_syn.add_argument("--apps", default="data/final_verified_apps.json")
    p_syn.add_argument("--report", default="data/audit_report.json")
    p_syn.add_argument("--out", default="data/case_study_patterns.json")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "fix-prompts":
        fix_prompts_from_errors(Path(args.report))
    elif args.command == "apply-overrides":
        apply_systematic_overrides(Path(args.apps), Path(args.report), Path(args.out) if args.out else None)
    elif args.command == "synthesize":
        synthesize_patterns(Path(args.apps), Path(args.report), Path(args.out))


if __name__ == "__main__":
    main()