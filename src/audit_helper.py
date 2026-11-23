"""Human audit sheet builder + accuracy scorer.

Two subcommands:

  prepare   - merge Pass 1 + Pass 2 records, take a stratified sample, and
              emit data/human_audit_sheet.json with blank human ground-truth
              fields (optionally filled via --interactive).
  score     - read a completed audit sheet, compute Pass 1 vs Pass 2 accuracy
              against human ground truth, write data/audit_report.json, and
              apply human overrides into data/final_verified_apps.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

try:
    from .schema import AppResearchRecord
except ImportError:  # allows `python src/audit_helper.py`
    from schema import AppResearchRecord  # type: ignore[no-redef]

CONSOLE = Console()

CORE_FIELDS = ("auth_methods", "access_model", "api_surface", "existing_mcp", "buildability_verdict")

AUDIT_BASE = {
    "human_verified": False,
    "human_auth_methods": None,
    "human_access_model": None,
    "human_api_surface": None,
    "human_existing_mcp": None,
    "human_buildability_verdict": None,
    "human_primary_blocker": None,
    "pass1_was_accurate": None,
    "pass2_was_accurate": None,
    "error_category": None,
    "audit_notes": "",
}


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8") or "null")
    except json.JSONDecodeError:
        return None


def _load_records(path: Path) -> dict[str, AppResearchRecord]:
    data = _load_json(path)
    if not data:
        return {}
    records: dict[str, AppResearchRecord] = {}
    for item in data:
        try:
            rec = AppResearchRecord.model_validate(item)
            records[rec.app_name] = rec
        except Exception:
            continue
    return records


def _load_pass2(path: Path) -> dict[str, dict[str, Any]]:
    data = _load_json(path)
    if not data:
        return {}
    out: dict[str, dict[str, Any]] = {}
    if isinstance(data, list):
        for item in data:
            name = item.get("app_name")
            if name:
                out[name] = item
    elif isinstance(data, dict):
        # Could be a map of app_name -> pass2 record dict
        out = {k: v for k, v in data.items() if isinstance(v, dict)}
    return out


def _field_value(rec: AppResearchRecord | None, field: str) -> Any:
    if rec is None:
        return None
    val = getattr(rec, field, None)
    if isinstance(val, list):
        return sorted(val)
    return val


def _snapshot(rec: AppResearchRecord | None, pass2: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return core snapshot dict for a record (pass2 overrides pass1 when present)."""
    if pass2 is not None:
        p2 = pass2.get("pass2", pass2)
        changed = pass2.get("verification_diff", {}).get("changed_fields", [])
    else:
        p2 = None
        changed = []
    snap: dict[str, Any] = {}
    for f in CORE_FIELDS:
        if p2 is not None:
            snap[f] = p2.get(f)
        elif rec is not None:
            snap[f] = _field_value(rec, f)
        else:
            snap[f] = None
    snap["primary_blocker"] = (
        p2.get("primary_blocker") if p2 is not None else (_field_value(rec, "primary_blocker") if rec else None)
    )
    snap["changed_fields"] = changed
    snap["confidence_score"] = p2.get("confidence_score") if p2 is not None else (
        rec.confidence_score if rec else None
    )
    return snap


def _edge_flags(rec: AppResearchRecord | None, pass2: dict[str, Any] | None = None) -> list[str]:
    flags: list[str] = []
    if pass2 is None:
        pass2 = {}
    p2 = pass2.get("pass2", pass2) if pass2 else None
    changed = pass2.get("verification_diff", {}).get("changed_fields", [])
    if rec is not None:
        if rec.access_model in ("Partner_Or_Sales_Gated", "Admin_Approval_Required"):
            flags.append("gated_access_model")
        if rec.existing_mcp != "None_Found":
            flags.append("has_existing_mcp")
        if rec.buildability_verdict != "Buildable_Today":
            flags.append("not_buildable_today")
    if changed:
        flags.append("pass2_changed_fields")
    return flags


def cmd_prepare(
    pass1_path: Path,
    pass2_path: Path,
    out_path: Path,
    n: int,
    interactive: bool,
) -> None:
    records = _load_records(pass1_path)
    pass2_map = _load_pass2(pass2_path)
    all_apps = sorted(set(records) | set(pass2_map))

    if not all_apps:
        CONSOLE.print("[red]No apps found in Pass 1 or Pass 2 inputs.[/red]")
        sys.exit(1)

    target = max(1, min(n, len(all_apps)))

    # --- Stratified selection -------------------------------------------------
    # Pass 1: guarantee at least one per category present.
    by_cat: dict[str, list[str]] = defaultdict(list)
    for app in all_apps:
        rec = records.get(app)
        cat = rec.category if rec else ("Other" if app not in pass2_map else pass2_map[app].get("pass2", {}).get("category", "Other"))
        by_cat[cat].append(app)

    chosen: dict[str, None] = {}
    cats = sorted(by_cat)
    idx = 0
    while len(chosen) < target and any(by_cat[c] for c in cats):
        cat = cats[idx % len(cats)]
        if by_cat[cat]:
            chosen[by_cat[cat].pop(0)] = None
        idx += 1
        if not any(by_cat.values()):
            break

    # Pass 2: prioritize edge cases not yet chosen.
    edge_apps: list[str] = []
    for app in all_apps:
        if app in chosen:
            continue
        rec = records.get(app)
        p2 = pass2_map.get(app)
        flags = _edge_flags(rec, p2)
        if flags:
            edge_apps.append((len(flags), app))
    edge_apps.sort(key=lambda t: -t[0])
    for _, app in edge_apps:
        if len(chosen) >= target:
            break
        chosen[app] = None

    # Pass 3: proportional fill to reach target.
    remaining = [a for a in all_apps if a not in chosen]
    while len(chosen) < target and remaining:
        chosen[remaining.pop(0)] = None

    sample = sorted(chosen)

    # --- Build sheet ----------------------------------------------------------
    sheet: list[dict[str, Any]] = []
    for app in sample:
        rec = records.get(app)
        p2 = pass2_map.get(app)
        p2_record = p2.get("pass2", p2) if p2 else None
        cat = (
            p2_record.get("category", rec.category if rec else "Other")
            if p2_record
            else (rec.category if rec else "Other")
        )
        evidence_urls = (
            p2_record.get("evidence_urls", [])
            if p2_record
            else (rec.evidence_urls if rec else [])
        )
        sheet.append(
            {
                "app_name": app,
                "category": cat,
                "evidence_urls": evidence_urls,
                "pass_1_snapshot": _snapshot(rec),
                "pass_2_snapshot": _snapshot(rec, p2),
                **dict(AUDIT_BASE),
            }
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(sheet, indent=2), encoding="utf-8")
    CONSOLE.print(f"[green]Prepared audit sheet[/green] -> {out_path}  ({len(sheet)} apps)")

    if interactive:
        _interactive_fill(out_path, sheet)


def _interactive_fill(path: Path, sheet: list[dict[str, Any]]) -> None:
    CONSOLE.print("\n[bold]Interactive human ground-truth entry[/bold]")
    CONSOLE.print("Press Enter to keep current value; type 'q' to quit.\n")
    for entry in sheet:
        if entry["human_verified"]:
            continue
        CONSOLE.rule(f"[bold]{entry['app_name']}[/bold] — {entry['category']}")
        CONSOLE.print(f"evidence_urls: {entry['evidence_urls']}")
        CONSOLE.print(f"Pass 1 snapshot: {json.dumps(entry['pass_1_snapshot'])}")
        CONSOLE.print(f"Pass 2 snapshot: {json.dumps(entry['pass_2_snapshot'])}")
        if Confirm.ask("Mark this app as human-verified?", default=False):
            entry["human_verified"] = True
            for field in ("auth_methods", "access_model", "api_surface", "existing_mcp", "buildability_verdict"):
                key = f"human_{field}"
                raw = Prompt.ask(f"  {key}", default=str(entry["pass_2_snapshot"].get(field, "")))
                if raw.lower() == "q":
                    _save_sheet(path, sheet)
                    return
                entry[key] = [v.strip() for v in raw.split(",") if v.strip()] if field in ("auth_methods", "api_surface") else raw
            entry["human_primary_blocker"] = Prompt.ask(
                "  human_primary_blocker", default=str(entry["pass_2_snapshot"].get("primary_blocker") or "")
            )
            entry["pass1_was_accurate"] = Confirm.ask("  pass1_was_accurate?", default=False)
            entry["pass2_was_accurate"] = Confirm.ask("  pass2_was_accurate?", default=False)
            entry["error_category"] = Prompt.ask(
                "  error_category",
                choices=["fake_self_serve_hidden_tier", "community_vs_official_mcp", "broken_or_outdated_url",
                         "ambiguous_product_scope", "schema_or_prompt_rule_gap", "none"],
                default="none",
            )
            if entry["error_category"] == "none":
                entry["error_category"] = None
            entry["audit_notes"] = Prompt.ask("  audit_notes", default="")
        _save_sheet(path, sheet)
    CONSOLE.print("[green]Audit sheet saved.[/green]")


def _save_sheet(path: Path, sheet: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(sheet, indent=2), encoding="utf-8")


def _field_match(human: Any, pipeline: Any) -> bool:
    if human is None:
        return False
    if isinstance(human, list) and isinstance(pipeline, list):
        return sorted(human) == sorted(pipeline)
    return str(human).strip().lower() == str(pipeline).strip().lower()


def cmd_score(sheet_path: Path, pass1_path: Path, pass2_path: Path, report_path: Path, final_path: Path) -> None:
    sheet = _load_json(sheet_path)
    if not sheet or not isinstance(sheet, list):
        CONSOLE.print(f"[red]Audit sheet not found or empty: {sheet_path}[/red]")
        sys.exit(1)
    verified = [e for e in sheet if e.get("human_verified") is True]
    if not verified:
        CONSOLE.print("[yellow]No human-verified rows found; nothing to score.[/yellow]")
        sys.exit(1)

    records = _load_records(pass1_path)
    pass2_map = _load_pass2(pass2_path)

    pass1_hits = 0
    pass2_hits = 0
    field_hits_pass1: Counter[str] = Counter()
    field_hits_pass2: Counter[str] = Counter()
    field_total: Counter[str] = Counter()
    root_cause_counter: Counter[str] = Counter()
    hits_and_misses: list[dict[str, Any]] = []

    for entry in verified:
        app = entry["app_name"]
        rec = records.get(app)
        p2 = pass2_map.get(app)
        p2_record = p2.get("pass2", p2) if p2 else None
        p2_diff = p2.get("verification_diff", {}) if p2 else {}
        changed_fields = set(p2_diff.get("changed_fields", []))

        human_vals = {f: entry.get(f"human_{f}") for f in CORE_FIELDS}
        human_blocker = entry.get("human_primary_blocker")

        p1_vals = {f: _field_value(rec, f) for f in CORE_FIELDS}
        p1_blocker = _field_value(rec, "primary_blocker") if rec else None

        p2_vals = {f: (p2_record.get(f) if p2_record else None) for f in CORE_FIELDS}
        p2_blocker = p2_record.get("primary_blocker") if p2_record else None

        p1_exact = all(_field_match(human_vals[f], p1_vals[f]) for f in CORE_FIELDS)
        p1_blocker_match = str(human_blocker or "").strip().lower() == str(p1_blocker or "").strip().lower()
        p1_row_ok = p1_exact and p1_blocker_match

        p2_exact = all(_field_match(human_vals[f], p2_vals[f]) for f in CORE_FIELDS)
        p2_blocker_match = str(human_blocker or "").strip().lower() == str(p2_blocker or "").strip().lower()
        p2_row_ok = p2_exact and p2_blocker_match

        pass1_hits += int(p1_row_ok)
        pass2_hits += int(p2_row_ok)

        for f in CORE_FIELDS:
            field_total[f] += 1
            if _field_match(human_vals[f], p1_vals[f]):
                field_hits_pass1[f] += 1
            if _field_match(human_vals[f], p2_vals[f]):
                field_hits_pass2[f] += 1

        # Root cause attribution (from human error_category, plus classifier fallback).
        error_category = entry.get("error_category")
        notes = (entry.get("audit_notes") or "").lower()
        if not error_category or error_category == "none":
            if not p2_row_ok and p1_row_ok:
                error_category = "schema_or_prompt_rule_gap"
            elif any("oauth" in notes or "review" in notes or "partner" in notes for _ in [0]):
                error_category = "fake_self_serve_hidden_tier"
            elif any("community" in notes or "unofficial" in notes for _ in [0]):
                error_category = "community_vs_official_mcp"
            elif any("404" in notes or "broken" in notes or "url" in notes for _ in [0]):
                error_category = "broken_or_outdated_url"
            elif any("ambiguous" in notes or "scope" in notes for _ in [0]):
                error_category = "ambiguous_product_scope"
            else:
                error_category = "schema_or_prompt_rule_gap"
        root_cause_counter[error_category or "other"] += 1

        row_example = {
            "app_name": app,
            "category": entry.get("category"),
            "pass1_was_accurate": entry.get("pass1_was_accurate"),
            "pass2_was_accurate": entry.get("pass2_was_accurate"),
            "error_category": error_category,
            "audit_notes": entry.get("audit_notes"),
            "fields": {},
        }
        for f in CORE_FIELDS:
            row_example["fields"][f] = {
                "human": human_vals[f],
                "pass1": p1_vals[f],
                "pass1_ok": _field_match(human_vals[f], p1_vals[f]),
                "pass2": p2_vals[f],
                "pass2_ok": _field_match(human_vals[f], p2_vals[f]),
                "changed": f in changed_fields,
            }
        row_example["fields"]["primary_blocker"] = {
            "human": human_blocker,
            "pass1": p1_blocker,
            "pass1_ok": p1_blocker_match,
            "pass2": p2_blocker,
            "pass2_ok": p2_blocker_match,
            "changed": "primary_blocker" in changed_fields,
        }
        hits_and_misses.append(row_example)

    total = len(verified)
    p1_row_acc = pass1_hits / total if total else 0.0
    p2_row_acc = pass2_hits / total if total else 0.0
    p1_field_acc = {f: field_hits_pass1[f] / field_total[f] if field_total[f] else 0.0 for f in field_total}
    p2_field_acc = {f: field_hits_pass2[f] / field_total[f] if field_total[f] else 0.0 for f in field_total}
    net_lift = p2_row_acc - p1_row_acc

    report = {
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "audited_rows": total,
        "pass1_row_accuracy": round(p1_row_acc, 4),
        "pass2_row_accuracy": round(p2_row_acc, 4),
        "net_accuracy_lift": round(net_lift, 4),
        "pass1_field_accuracy": {f: round(p1_field_acc[f], 4) for f in field_total},
        "pass2_field_accuracy": {f: round(p2_field_acc[f], 4) for f in field_total},
        "root_cause_breakdown": dict(sorted(root_cause_counter.items())),
        "hits_and_misses": hits_and_misses,
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Apply human overrides -> final_verified_apps.json
    final = _apply_overrides(records, pass2_map, verified)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_text(json.dumps(final, indent=2), encoding="utf-8")

    _print_score_report(report)
    CONSOLE.print(f"[green]Audit report[/green] -> {report_path}")
    CONSOLE.print(f"[green]Final verified apps[/green] -> {final_path}")


def _apply_overrides(
    records: dict[str, AppResearchRecord],
    pass2_map: dict[str, dict[str, Any]],
    verified: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for app, rec in records.items():
        entry = next((e for e in verified if e.get("app_name") == app), None)
        p2 = pass2_map.get(app)
        p2_record = p2.get("pass2", p2) if p2 else None

        base = rec.model_dump(mode="json")
        base["_pass2_snapshot"] = p2_record
        if entry and entry.get("human_verified") is True:
            for f in CORE_FIELDS:
                human_val = entry.get(f"human_{f}")
                if human_val is not None:
                    base[f] = human_val
            base["primary_blocker"] = entry.get("human_primary_blocker") or base.get("primary_blocker")
            base["confidence_score"] = 5
            base["human_overrides_applied"] = True
            base["human_error_category"] = entry.get("error_category")
            base["human_audit_notes"] = entry.get("audit_notes")
        else:
            base["human_overrides_applied"] = False
        out.append(base)
    return out


def _print_score_report(report: dict[str, Any]) -> None:
    table = Table(title="Audit Accuracy Report")
    table.add_column("Metric", style="bold")
    table.add_column("Pass 1", justify="right")
    table.add_column("Pass 2", justify="right")
    table.add_column("Delta", justify="right")
    table.add_row(
        "Row-level accuracy",
        f"{report['pass1_row_accuracy']*100:.1f}%",
        f"{report['pass2_row_accuracy']*100:.1f}%",
        f"{report['net_accuracy_lift']*100:+.1f}%",
    )
    for f in CORE_FIELDS:
        table.add_row(
            f"Field: {f}",
            f"{report['pass1_field_accuracy'][f]*100:.1f}%",
            f"{report['pass2_field_accuracy'][f]*100:.1f}%",
            f"{(report['pass2_field_accuracy'][f]-report['pass1_field_accuracy'][f])*100:+.1f}%",
        )
    CONSOLE.print(table)

    rc = Table(title="Root Cause Breakdown")
    rc.add_column("Error Category", style="bold")
    rc.add_column("Count", justify="right")
    for cat, cnt in sorted(report["root_cause_breakdown"].items(), key=lambda x: -x[1]):
        rc.add_row(cat, str(cnt))
    CONSOLE.print(rc)

    hm = Table(title="Hits & Misses (sample)")
    hm.add_column("App", style="bold")
    hm.add_column("P1 Acc", justify="center")
    hm.add_column("P2 Acc", justify="center")
    hm.add_column("Error Category", style="bold")
    hm.add_column("Notes", style="dim")
    for ex in report["hits_and_misses"][:10]:
        hm.add_row(
            ex["app_name"],
            "OK" if ex["pass1_was_accurate"] else "MISS",
            "OK" if ex["pass2_was_accurate"] else "MISS",
            ex["error_category"] or "—",
            (ex["audit_notes"] or "")[:40],
        )
    CONSOLE.print(hm)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Human audit sheet builder and accuracy scorer")
    sub = p.add_subparsers(dest="command", required=True)

    p_prep = sub.add_parser("prepare", help="Build the human audit sheet from Pass 1 + Pass 2")
    p_prep.add_argument("--pass1", default="data/pass1_results.json")
    p_prep.add_argument("--pass2", default="data/pass2_verified.json")
    p_prep.add_argument("--out", default="data/human_audit_sheet.json")
    p_prep.add_argument("--n", type=int, default=18, help="Stratified sample size (default 18)")
    p_prep.add_argument("--interactive", action="store_true", help="Prompt to fill human ground-truth fields")

    p_score = sub.add_parser("score", help="Score Pass 1 vs Pass 2 against human ground truth")
    p_score.add_argument("--sheet", default="data/human_audit_sheet.json")
    p_score.add_argument("--pass1", default="data/pass1_results.json")
    p_score.add_argument("--pass2", default="data/pass2_verified.json")
    p_score.add_argument("--report", default="data/audit_report.json")
    p_score.add_argument("--final", default="data/final_verified_apps.json")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "prepare":
        cmd_prepare(Path(args.pass1), Path(args.pass2), Path(args.out), args.n, args.interactive)
    elif args.command == "score":
        cmd_score(Path(args.sheet), Path(args.pass1), Path(args.pass2), Path(args.report), Path(args.final))


if __name__ == "__main__":
    main()