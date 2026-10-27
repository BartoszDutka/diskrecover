"""Prosty profil wydajnościowy skanowania (bez zewnętrznych zależności)."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Tuple


@dataclass
class Profiler:
    enabled: bool = True
    totals: Dict[str, float] = field(default_factory=dict)
    counts: Dict[str, int] = field(default_factory=dict)

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            self.totals[name] = self.totals.get(name, 0.0) + elapsed
            self.counts[name] = self.counts.get(name, 0) + 1

    def add(self, name: str, seconds: float) -> None:
        if not self.enabled:
            return
        self.totals[name] = self.totals.get(name, 0.0) + seconds
        self.counts[name] = self.counts.get(name, 0) + 1

    def report(self, top: int = 20) -> List[Tuple[str, float, int]]:
        items = sorted(self.totals.items(), key=lambda kv: kv[1], reverse=True)
        out: List[Tuple[str, float, int]] = []
        for name, total in items[:top]:
            out.append((name, total, self.counts.get(name, 0)))
        return out

    def as_dict(self) -> dict:
        return {
            name: {"seconds": round(sec, 6), "calls": self.counts.get(name, 0)}
            for name, sec, _ in ((n, s, self.counts.get(n, 0)) for n, s in self.totals.items())
        }

    def format_report(self, top: int = 15) -> str:
        lines = ["Profil wydajności (najwolniejsze funkcje):"]
        for name, sec, calls in self.report(top=top):
            avg = sec / calls if calls else 0.0
            lines.append(f"  {name}: {sec:.3f}s łącznie / {calls} wywołań (avg {avg*1000:.2f} ms)")
        if len(lines) == 1:
            lines.append("  (brak danych)")
        return "\n".join(lines)
