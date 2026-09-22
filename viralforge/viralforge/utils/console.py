"""Terminal output.  Uses rich when available, degrades to plain print."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator, Optional

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn,
    )
    _RICH = True
except ImportError:  # pragma: no cover - rich is a declared dependency
    _RICH = False
    Console = None  # type: ignore


class _PlainConsole:
    def print(self, *args, **kwargs) -> None:
        text = " ".join(str(a) for a in args)
        for tag in ("[bold]", "[/bold]", "[dim]", "[/dim]", "[red]", "[/red]",
                    "[yellow]", "[/yellow]", "[green]", "[/green]", "[cyan]",
                    "[/cyan]", "[bold cyan]", "[bold red]", "[bold yellow]"):
            text = text.replace(tag, "")
        print(text)


console = Console() if _RICH else _PlainConsole()

_STEP = 0


def step(message: str) -> None:
    global _STEP
    _STEP += 1
    console.print(f"[bold cyan]▶ {_STEP}.[/bold cyan] {message}")


def info(message: str) -> None:
    console.print(f"  [dim]{message}[/dim]")


def warn(message: str) -> None:
    console.print(f"  [yellow]! {message}[/yellow]")


def error(message: str) -> None:
    console.print(f"[bold red]✗ {message}[/bold red]")


@contextmanager
def progress_bar(description: str, total: Optional[float] = None) -> Iterator:
    """Yields an ``update(completed)`` callable."""
    if not _RICH or not sys.stderr.isatty():
        info(description)
        yield lambda _completed: None
        return
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        TextColumn("{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as prog:
        task = prog.add_task(f"  {description}", total=total or 100.0)
        yield lambda completed: prog.update(task, completed=completed)
