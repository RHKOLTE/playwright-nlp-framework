#!/usr/bin/env python3
"""
Playwright-NLP Offline Test Runner
===================================
Usage:
  python run_tests.py tests/zoho_mail.txt
  python run_tests.py tests/           # run all .txt files in folder
  python run_tests.py tests/ --headless
  python run_tests.py tests/ --browser firefox
"""

import sys
import os
import glob
import time
import argparse
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

# Make sure core/ is importable
sys.path.insert(0, os.path.dirname(__file__))

from core.nlp_parser import NLPParser
from core.executor import PlaywrightExecutor
from core.reporter import generate_html_report

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
            str(i),
            status,
            s.step.action,
            (s.step.target or s.step.condition or "—")[:20],
            f"{s.duration_ms:.0f}ms",
            (s.error or "—")[:45],
        )

    console.print(table)
    summary_color = "green" if result.passed else "red"
    console.print(f"[{summary_color}]  {'✓ PASSED' if result.passed else '✗ FAILED'} "
                  f"| {result.total - result.failed_count}/{result.total} steps passed "
                  f"| {result.duration_ms/1000:.2f}s[/{summary_color}]")
    console.print()


def main():
    parser = argparse.ArgumentParser(description="Playwright-NLP Offline Test Runner")
    parser.add_argument("path", help="Test file or folder path")
    parser.add_argument("--browser", default="chromium", choices=["chromium", "firefox", "webkit"])
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    parser.add_argument("--slow-mo", type=int, default=600, help="Slow-mo ms between actions")
    parser.add_argument("--timeout", type=int, default=15000, help="Action timeout ms")
    parser.add_argument("--report", default="reports/report.html", help="Output HTML report path")
    parser.add_argument("--parse-only", action="store_true", help="Only parse, don't execute")
    args = parser.parse_args()

    print_banner()

    test_files = collect_test_files(args.path)
    if not test_files:
        console.print("[red]No test files found![/red]")
        sys.exit(1)

    console.print(f"[bold]Found {len(test_files)} test file(s)[/bold]")
    console.print()

    parser_engine = NLPParser()
    executor = PlaywrightExecutor(
        browser_type=args.browser,
        headless=args.headless,
        slow_mo=args.slow_mo,
        timeout=args.timeout,
    )

    all_results = []

    for test_file in test_files:
        file_name = Path(test_file).name
        console.rule(f"[bold cyan]{file_name}[/bold cyan]")

        # 1. Parse
        console.print(f"[dim]Parsing {test_file}...[/dim]")
        steps = parser_engine.parse_file(test_file)
        print_parsed_steps(steps, file_name)

        if args.parse_only:
            continue

        # 2. Execute
        console.print(f"[dim]Executing {len(steps)} steps in {args.browser}...[/dim]")
        console.print()
        result = executor.run(steps, file_name)
        all_results.append(result)

        # 3. Print per-file result
        print_results(result)

    if all_results:
        # 4. Generate HTML report
        report_path = generate_html_report(all_results, args.report)
        console.print(Panel(
            f"[bold green]📊 HTML Report:[/bold green] {report_path}\n"
            f"[dim]Open in your browser to view the full test report[/dim]",
            border_style="green",
        ))

        # Exit code
        all_passed = all(r.passed for r in all_results)
        sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
