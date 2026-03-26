#!/usr/bin/env python3
"""
Playwright-NLP Offline Test Runner
====================================
Usage:
  python run_tests.py tests/zoho_mail.txt
  python run_tests.py tests/
  python run_tests.py tests/ --headless
  python run_tests.py tests/ --video
  python run_tests.py tests/ --browser firefox
  python run_tests.py tests/ --parse-only

Report folder structure:
  reports/
  └── <test_name>/
      └── <run_id (UTC datetime)>/
          ├── index.html
          ├── screenshots/
          │   ├── step01_before.png
          │   ├── step01_after.png
          │   └── ...
          └── video/
              └── recording.webm   (if --video enabled)
"""

import sys
import os
import glob
import argparse
from pathlib import Path
from datetime import datetime, timezone
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

sys.path.insert(0, os.path.dirname(__file__))

from core.nlp_parser import NLPParser
from core.executor import PlaywrightExecutor
from core.reporter import generate_html_report
from core.knowledge import get_store

console = Console()


def collect_test_files(path: str) -> list[str]:
    p = Path(path)
    if p.is_file():
        return [str(p)]
    elif p.is_dir():
        return sorted(glob.glob(str(p / "*.txt")) + glob.glob(str(p / "*.test")))
    else:
        console.print(f"[red]Path not found: {path}[/red]")
        sys.exit(1)


def make_run_id() -> str:
    """UTC datetime string safe for use as a folder name."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def print_banner():
    console.print(Panel.fit(
        "[bold cyan]🎭 Playwright-NLP Offline Test Runner[/bold cyan]\n"
        "[dim]Parse plain-English test scripts → Execute → Report[/dim]",
        border_style="cyan",
    ))
    console.print()


def print_parsed_steps(steps, file_name):
    table = Table(title=f"[bold]Parsed Steps: {file_name}[/bold]",
                  box=box.ROUNDED, show_lines=True, border_style="dim")
    table.add_column("#", style="dim", width=3)
    table.add_column("Action", style="cyan", width=10)
    table.add_column("Target", style="yellow", width=18)
    table.add_column("Value", style="green", width=28)
    table.add_column("Raw Instruction", style="white", width=45)

    for i, step in enumerate(steps, 1):
        table.add_row(
            str(i),
            step.action,
            step.target or "—",
            (step.value or step.condition or "—")[:50],
            step.raw[:60],
        )
    console.print(table)
    console.print()


def print_results(result):
    table = Table(title=f"[bold]Results: {result.test_file}[/bold]",
                  box=box.ROUNDED, show_lines=True, border_style="dim")
    table.add_column("#", style="dim", width=3)
    table.add_column("Status", width=8)
    table.add_column("Action", style="cyan", width=10)
    table.add_column("Target", style="yellow", width=18)
    table.add_column("Duration", width=8)
    table.add_column("Error", style="red", width=40)

    for i, s in enumerate(result.steps, 1):
        status = "[green]✓ PASS[/green]" if s.passed else "[red]✗ FAIL[/red]"
        table.add_row(
            str(i), status, s.step.action,
            (s.step.target or s.step.condition or "—")[:20],
            f"{s.duration_ms:.0f}ms",
            (s.error or "—")[:45],
        )
    console.print(table)

    color = "green" if result.passed else "red"
    console.print(
        f"[{color}]  {'✓ PASSED' if result.passed else '✗ FAILED'} "
        f"| {result.total - result.failed_count}/{result.total} steps passed "
        f"| {result.duration_ms/1000:.2f}s[/{color}]"
    )
    if result.video_path:
        console.print(f"  🎬 Video: {result.video_path}")
    console.print()


def main():
    parser = argparse.ArgumentParser(description="Playwright-NLP Offline Test Runner")
    parser.add_argument("path", help="Test file or folder path")
    parser.add_argument("--browser",  default="chromium", choices=["chromium", "firefox", "webkit"])
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--video",    action="store_true", help="Record video of each test run")
    parser.add_argument("--slow-mo",  type=int, default=600)
    parser.add_argument("--timeout",  type=int, default=15000)
    parser.add_argument("--reports",  default="reports", help="Base reports directory")
    parser.add_argument("--parse-only", action="store_true")
    args = parser.parse_args()

    print_banner()

    # Single run_id shared across all files in this invocation
    run_id = make_run_id()
    console.print(f"[dim]Run ID: {run_id}[/dim]")
    console.print()

    test_files = collect_test_files(args.path)
    if not test_files:
        console.print("[red]No test files found![/red]")
        sys.exit(1)

    console.print(f"[bold]Found {len(test_files)} test file(s)[/bold]")
    console.print()

    # Show knowledge store status
    store = get_store()
    field_count  = len(store.field_selectors)
    button_count = len(store.button_selectors)
    console.print(f"[dim]📚 Knowledge: {field_count} field types, {button_count} button types[/dim]")
    console.print()

    nlp_parser = NLPParser()
    executor = PlaywrightExecutor(
        browser_type=args.browser,
        headless=args.headless,
        slow_mo=args.slow_mo,
        timeout=args.timeout,
        record_video=args.video,
        reports_base=args.reports,
    )

    all_results = []

    for test_file in test_files:
        file_name = Path(test_file).name
        console.rule(f"[bold cyan]{file_name}[/bold cyan]")

        console.print(f"[dim]Parsing {test_file}...[/dim]")
        steps = nlp_parser.parse_file(test_file)
        print_parsed_steps(steps, file_name)

        if args.parse_only:
            continue

        console.print(f"[dim]Executing {len(steps)} steps → Run {run_id}[/dim]")
        console.print()

        result = executor.run(steps, file_name, run_id)
        all_results.append(result)
        print_results(result)

    if all_results:
        # Write index.html inside each test's run folder
        report_paths = []
        for result in all_results:
            safe_name = __import__('re').sub(r"[^\w\-]", "_", Path(result.test_file).stem)
            run_dir   = Path(args.reports) / safe_name / run_id
            report_path = str(run_dir / "index.html")
            generate_html_report([result], report_path)
            report_paths.append(report_path)

        console.print(Panel(
            "\n".join([
                f"[bold green]📊 Reports generated:[/bold green]",
                *[f"  {p}" for p in report_paths],
                f"",
                f"[dim]Folder: reports/<test_name>/{run_id}/index.html[/dim]",
                f"[dim]Video:  reports/<test_name>/{run_id}/video/  (if --video used)[/dim]",
            ]),
            border_style="green",
        ))

        all_passed = all(r.passed for r in all_results)
        sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
