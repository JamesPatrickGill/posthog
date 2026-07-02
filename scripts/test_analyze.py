#!/usr/bin/env python3
"""Analyze the PostHog backend test suite for duration hot spots and coverage redundancy.

Two independent analyses live here:

1. Duration analysis (always runs) reads ``.test_durations`` — the pytest-split timing
   file, a flat ``{test_id: seconds}`` JSON map. ``optimize_test_durations.py`` writes two
   kinds of non-measurement values: it floors de-taxed / genuinely-fast tests to
   ``MIN_DURATION`` (0.01s), and it leaves ``DEFAULT_PLACEHOLDER_SECONDS`` (60.0 / 18.0)
   on tests it has never successfully timed. The floor is cheap and real, so it stays in
   the distribution as "fast"; the never-timed placeholders bound nothing and would poison
   the tail, so ``detect_placeholders`` isolates them into an "untrusted timing" segment
   kept out of the distribution stats.

   The file also accumulates *stale* entries: it is merge-updated (never rebuilt) and CI
   does not pass ``--filter-existing``, so timings for moved or deleted test files linger —
   a code move from ``posthog/`` to ``products/`` leaves the old path behind, often re-added
   under the new path, double-counting. ``partition_existing`` drops entries whose test file
   is gone from disk (cheap, catches whole-file moves); ``--collect`` cross-checks against
   ``pytest --collect-only`` to also catch case-level staleness (renamed cases, dropped
   parametrizations) at the cost of needing a working test env. Pass ``--keep-stale`` to
   score every recorded timing (the old behavior).

2. Coverage / redundancy analysis (opt-in) reads pytest-testmon runtime coverage — which
   test touched which production files — and finds isomorphic tests (identical coverage
   sets → safe drop candidates), trivial-coverage tests (touch no production file), and
   segments tests by duration x coverage-value (dispensable vs irreplaceable).

   Coverage data is NOT committed to the repo. Supply it with one of:
     --testmon path/to/.testmondata   (a pytest --testmon SQLite DB)
     --pickle  path/to/merged.pkl     ({'test_files': {test: [files]}, 'high_fanout': [files]})
   Without either, the redundancy sections are skipped and the report says so.

Usage:
    python3 scripts/test_analyze.py [--durations .test_durations] [--out report.md]
    python3 scripts/test_analyze.py --collect --out report.md   # accurate staleness, needs pytest
    python3 scripts/test_analyze.py --testmon .testmondata --out report.md
"""

from __future__ import annotations

import sys
import json
import pickle
import sqlite3
import argparse
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DURATIONS = REPO_ROOT / ".test_durations"
DEFAULT_HIGH_FANOUT = REPO_ROOT / "tools" / "testmon_high_fanout_files.txt"

# Floor applied by .github/scripts/optimize_test_durations.py (MIN_DURATION) to de-taxed
# / genuinely-fast tests. A test clamped here is near-zero cost, so it is treated as
# legitimately "fast" — not untrusted.
MIN_DURATION_FLOOR = 0.01
# Placeholder timings pytest-split writes for tests it has never successfully timed
# (DEFAULT_PLACEHOLDER_SECONDS in .github/scripts/optimize_test_durations.py). Unlike the
# floor, these bound nothing — the true cost is unknown and could be large — so they are
# reported as an untrusted "suspect" segment and kept out of the distribution stats.
KNOWN_PLACEHOLDERS: tuple[float, ...] = (60.0, 18.0)
# 50ms — below this a test is "near-zero cost" and not worth optimizing.
FAST_CEILING = 0.05
# A test touching more files than this is a Django-bootstrap tracer (the first test in a
# shard absorbs framework import coverage), not a real signal.
OVER_BROAD_FILES = 500
# Jaccard similarity at/above which two coverage sets are "near-isomorphic".
NEAR_ISOMORPH_JACCARD = 0.85


def fmt_duration(seconds: float) -> str:
    """Human-readable duration: sub-minute in seconds, else minutes/hours."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.2f}h"


def package_of(test_id: str, depth: int = 2) -> str:
    """First ``depth`` path segments of the file portion of a test id."""
    file_part = test_id.split("::", 1)[0]
    return "/".join(file_part.split("/")[:depth])


# --------------------------------------------------------------------------------------
# Duration analysis
# --------------------------------------------------------------------------------------


def load_durations(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a {{test_id: seconds}} map")
    return {str(k): float(v) for k, v in data.items()}


def partition_existing(durations: dict[str, float]) -> tuple[dict[str, float], dict[str, float]]:
    """Split entries into (live, stale) by whether the test's file exists on disk.

    ``.test_durations`` accumulates: pytest-split merges new timings into the existing
    file and CI never passes ``--filter-existing``, so entries for moved or deleted test
    files linger indefinitely (a code move from ``posthog/`` to ``products/`` leaves the
    old path behind, often re-added under the new path — double-counting the same test).
    These "stale" entries carry a duration that no longer corresponds to anything runnable,
    inflating the recorded total and poisoning the slow-outlier list. Segregate them so the
    distribution reflects only tests that still exist.

    This is a cheap, deterministic file-existence check keyed on the path portion of the id.
    It catches the bulk case (whole files moved/deleted) but not a test whose *file* still
    exists while a specific case was renamed or a parametrization was dropped — for that
    exactness, ``--collect`` cross-checks against ``pytest --collect-only``.
    """
    exists: dict[str, bool] = {}
    live: dict[str, float] = {}
    stale: dict[str, float] = {}
    for test_id, dur in durations.items():
        file_part = test_id.split("::", 1)[0]
        present = exists.get(file_part)
        if present is None:
            present = (REPO_ROOT / file_part).exists()
            exists[file_part] = present
        (live if present else stale)[test_id] = dur
    return live, stale


def collect_live_ids(paths: tuple[str, ...] = ()) -> set[str] | None:
    """Exact set of currently-collectable test ids via ``pytest --collect-only``.

    Returns None if collection fails (no test env, import error) — callers then fall back
    to the file-existence heuristic. This is accurate but slow and needs a working Django
    test environment, so it is opt-in behind ``--collect``.
    """
    import subprocess  # noqa: PLC0415 — only needed when --collect is passed

    cmd = ["python3", "-m", "pytest", "--collect-only", "-q", "--no-header", *paths]
    try:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"--collect failed ({exc}); falling back to file-existence check\n")
        return None
    ids: set[str] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith(("=", "<", "warning")):
            ids.add(line)
    if not ids:
        sys.stderr.write("--collect produced no ids; falling back to file-existence check\n")
        return None
    return ids


def detect_placeholders(durations: dict[str, float]) -> set[float]:
    """Identify high placeholder timings that are not real measurements.

    The floor (0.01s) is deliberately excluded — a floored test is genuinely cheap and
    belongs in the "fast" segment. What must be excluded from the distribution are the
    unbounded placeholders pytest-split writes for never-timed tests, because their true
    cost is unknown and would distort the tail.

    Detection combines two signals:
      * the documented constants (60.0 / 18.0), whenever they appear; and
      * an adaptive backstop for any *other* whole-second value (>= 10s) shared by an
        implausible number of tests — real measured durations carry sub-second precision,
        so a cluster of exact whole-second timings is a placeholder, not a coincidence.
    """
    counts = Counter(round(v, 6) for v in durations.values())
    placeholders = {p for p in KNOWN_PLACEHOLDERS if counts.get(p, 0) > 0}

    for value, count in counts.items():
        if value >= 10.0 and abs(value - round(value)) < 1e-9 and count >= 20:
            placeholders.add(value)

    return placeholders


@dataclass
class DurationStats:
    total_tests: int
    total_time: float
    trusted: list[float]
    placeholders: set[float]
    suspect_count: int
    suspect_time: float
    floored_count: int
    median: float
    p95: float
    p99: float
    max_time: float
    pareto_50: int
    pareto_80: int
    recorded_tests: int = 0
    recorded_time: float = 0.0
    stale_count: int = 0
    stale_time: float = 0.0
    stale_mode: str = "file-existence"


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round(pct / 100 * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def compute_duration_stats(
    durations: dict[str, float],
    stale: dict[str, float] | None = None,
    stale_mode: str = "file-existence",
) -> DurationStats:
    """Compute the distribution over *live* ``durations``; ``stale`` is reported, not scored."""
    stale = stale or {}
    placeholders = detect_placeholders(durations)
    # Trusted = everything except the unbounded placeholders. Floored tests (0.01s) stay
    # in — they are cheap but real, and belong in the distribution.
    trusted = sorted(v for v in durations.values() if round(v, 6) not in placeholders)
    suspect = [v for v in durations.values() if round(v, 6) in placeholders]
    floored = sum(1 for v in durations.values() if round(v, 6) == MIN_DURATION_FLOOR)

    total_time = sum(durations.values())

    # Pareto over every recorded value, largest first.
    desc = sorted(durations.values(), reverse=True)
    pareto_50 = pareto_80 = len(desc)
    cum = 0.0
    hit_50 = False
    for i, v in enumerate(desc, start=1):
        cum += v
        if not hit_50 and cum >= 0.5 * total_time:
            pareto_50, hit_50 = i, True
        if cum >= 0.8 * total_time:
            pareto_80 = i
            break

    return DurationStats(
        total_tests=len(durations),
        total_time=total_time,
        trusted=trusted,
        placeholders=placeholders,
        suspect_count=len(suspect),
        suspect_time=sum(suspect),
        floored_count=floored,
        median=statistics.median(trusted) if trusted else 0.0,
        p95=_percentile(trusted, 95),
        p99=_percentile(trusted, 99),
        max_time=max(durations.values()) if durations else 0.0,
        pareto_50=pareto_50,
        pareto_80=pareto_80,
        recorded_tests=len(durations) + len(stale),
        recorded_time=total_time + sum(stale.values()),
        stale_count=len(stale),
        stale_time=sum(stale.values()),
        stale_mode=stale_mode,
    )


@dataclass
class Segment:
    key: str
    description: str
    tests: list[tuple[str, float]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.tests)

    @property
    def total(self) -> float:
        return sum(d for _, d in self.tests)


def segment_by_duration(durations: dict[str, float], stats: DurationStats) -> list[Segment]:
    placeholders = stats.placeholders
    p95, p99 = stats.p95, stats.p99
    segs = {
        "suspect-duration": Segment(
            "suspect-duration",
            f"never-timed placeholder(s) {sorted(placeholders)} — true cost unknown, review",
        ),
        "slow-outliers": Segment("slow-outliers", f"> p99 ({p99:.2f}s) — strongest review candidates"),
        "slow-tail": Segment("slow-tail", f"p95–p99 ({p95:.2f}s–{p99:.2f}s)"),
        "normal": Segment("normal", f"{int(FAST_CEILING * 1000)}ms–p95 ({p95:.2f}s)"),
        "fast": Segment("fast", f"<= {int(FAST_CEILING * 1000)}ms — near-zero cost (incl. floored)"),
    }
    for test_id, dur in durations.items():
        if round(dur, 6) in placeholders:
            segs["suspect-duration"].tests.append((test_id, dur))
        elif dur > p99:
            segs["slow-outliers"].tests.append((test_id, dur))
        elif dur > p95:
            segs["slow-tail"].tests.append((test_id, dur))
        elif dur > FAST_CEILING:
            segs["normal"].tests.append((test_id, dur))
        else:
            segs["fast"].tests.append((test_id, dur))
    return list(segs.values())


def hottest_packages(durations: dict[str, float], top: int = 10) -> list[tuple[str, int, float, float, float]]:
    by_pkg: dict[str, list[float]] = defaultdict(list)
    for test_id, dur in durations.items():
        by_pkg[package_of(test_id)].append(dur)
    rows = [(pkg, len(vals), sum(vals), statistics.mean(vals), statistics.median(vals)) for pkg, vals in by_pkg.items()]
    rows.sort(key=lambda r: r[2], reverse=True)
    return rows[:top]


# --------------------------------------------------------------------------------------
# Coverage / redundancy analysis (opt-in)
# --------------------------------------------------------------------------------------

PRODUCTION_ROOTS = ("posthog/", "ee/", "products/", "common/", "dags/")


def _is_production_file(path: str) -> bool:
    if not path.startswith(PRODUCTION_ROOTS):
        return False
    lowered = path.lower()
    return "/test" not in lowered and not lowered.split("/")[-1].startswith("test_")


def load_high_fanout(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text().splitlines() if line.strip()}


def load_pickle_coverage(path: Path) -> tuple[dict[str, set[str]], set[str]]:
    data = pickle.loads(path.read_bytes())
    test_files = {t: set(files) for t, files in data["test_files"].items()}
    high_fanout = set(data.get("high_fanout", ()))
    return test_files, high_fanout


def load_testmon_coverage(path: Path) -> dict[str, set[str]]:
    """Read per-test → covered-files from a pytest-testmon .testmondata SQLite DB.

    Schema differs across testmon versions; introspect table/column names and join the
    test-execution rows to their file fingerprints defensively.
    """
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def cols(table: str) -> set[str]:
            return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}

        # Locate the file table (has a filename-like column) and the test table.
        file_table = next((t for t in tables if {"filename"} & cols(t)), None)
        test_table = next(
            (t for t in tables if {"test_name", "name"} & cols(t) and "file" not in t.lower()),
            None,
        )
        link_table = next(
            (t for t in tables if "file" in t.lower() and "fp" in t.lower() and t not in {file_table}),
            None,
        )
        if not (file_table and test_table and link_table):
            raise ValueError(f"unrecognized testmon schema; tables={sorted(tables)}. Convert to a --pickle instead.")

        test_name_col = "test_name" if "test_name" in cols(test_table) else "name"
        test_id_col = "id" if "id" in cols(test_table) else "rowid"
        link_cols = cols(link_table)
        test_fk = next(c for c in link_cols if "test" in c)
        file_fk = next(c for c in link_cols if "fp" in c or "file" in c)
        file_id_col = "id" if "id" in cols(file_table) else "rowid"

        query = (
            f"SELECT t.{test_name_col}, f.filename "
            f"FROM {test_table} t "
            f"JOIN {link_table} l ON l.{test_fk} = t.{test_id_col} "
            f"JOIN {file_table} f ON f.{file_id_col} = l.{file_fk}"
        )
        result: dict[str, set[str]] = defaultdict(set)
        for test_name, filename in con.execute(query):
            result[str(test_name)].add(str(filename))
        return dict(result)
    finally:
        con.close()


@dataclass
class RedundancyReport:
    isomorph_clusters: int
    isomorph_droppable_tests: int
    isomorph_droppable_time: float
    trivial_tests: int
    trivial_time: float
    near_isomorph_clusters: int
    near_isomorph_time: float


def analyze_redundancy(test_files: dict[str, set[str]], durations: dict[str, float]) -> RedundancyReport:
    dur = lambda t: durations.get(t, 0.0)  # noqa: E731 — local shorthand

    # Isomorphs: byte-identical production-coverage sets. Keep the fastest per cluster,
    # the rest are drop candidates.
    by_fingerprint: dict[frozenset[str], list[str]] = defaultdict(list)
    trivial_tests: list[str] = []
    for test, files in test_files.items():
        prod = frozenset(f for f in files if _is_production_file(f))
        if not prod:
            trivial_tests.append(test)
            continue
        by_fingerprint[prod].append(test)

    iso_clusters = 0
    iso_drop_tests = 0
    iso_drop_time = 0.0
    for cluster in by_fingerprint.values():
        if len(cluster) < 2:
            continue
        iso_clusters += 1
        ordered = sorted(cluster, key=dur)  # keep the cheapest
        iso_drop_tests += len(ordered) - 1
        iso_drop_time += sum(dur(t) for t in ordered[1:])

    # Near-isomorphs: within groups that share a fingerprint's neighborhood, compare
    # cluster representatives pairwise. Bounded to representatives to stay tractable.
    reps = [(fp, cluster) for fp, cluster in by_fingerprint.items() if fp]
    near_clusters = 0
    near_time = 0.0
    inverted: dict[str, list[int]] = defaultdict(list)
    for idx, (fp, _) in enumerate(reps):
        for f in fp:
            inverted[f].append(idx)
    seen_pairs: set[tuple[int, int]] = set()
    for idxs in inverted.values():
        if len(idxs) > 200:  # skip ultra-popular files — everything "shares" them
            continue
        for a in idxs:
            for b in idxs:
                if a >= b or (a, b) in seen_pairs:
                    continue
                seen_pairs.add((a, b))
                fa, fb = reps[a][0], reps[b][0]
                inter = len(fa & fb)
                if not inter:
                    continue
                jac = inter / len(fa | fb)
                if jac >= NEAR_ISOMORPH_JACCARD:
                    near_clusters += 1
                    near_time += min(sum(dur(t) for t in reps[a][1]), sum(dur(t) for t in reps[b][1]))

    return RedundancyReport(
        isomorph_clusters=iso_clusters,
        isomorph_droppable_tests=iso_drop_tests,
        isomorph_droppable_time=iso_drop_time,
        trivial_tests=len(trivial_tests),
        trivial_time=sum(dur(t) for t in trivial_tests),
        near_isomorph_clusters=near_clusters,
        near_isomorph_time=near_time,
    )


def segment_by_coverage(
    test_files: dict[str, set[str]],
    durations: dict[str, float],
    high_fanout: set[str],
    stats: DurationStats,
) -> list[Segment]:
    """Cross duration against coverage value to prioritize optimize/keep/drop."""
    freq: Counter[str] = Counter()
    for files in test_files.values():
        for f in files:
            freq[f] += 1

    def value_score(files: set[str]) -> float:
        # Sum of inverse file frequency over "unique" files (excluding high-fanout).
        return sum(1.0 / freq[f] for f in files - high_fanout if freq[f])

    slow_threshold = stats.p99
    scores = [value_score(f) for t, f in test_files.items() if t in durations]
    value_threshold = statistics.median(scores) if scores else 0.0

    segs = {
        "slow_dispensable": Segment(
            "slow_dispensable", f"OPTIMIZE OR DROP — >= {slow_threshold:.1f}s, only common files"
        ),
        "slow_irreplaceable": Segment(
            "slow_irreplaceable", f"OPTIMIZE — >= {slow_threshold:.1f}s but covers rarer code"
        ),
        "fast_valuable": Segment("fast_valuable", "KEEP — fast workhorse covering rarer code"),
        "fast_broad_only": Segment("fast_broad_only", "LOW PRIORITY — fast, only popular files"),
        "over_broad_tracer": Segment("over_broad_tracer", f"DATA NOISE — > {OVER_BROAD_FILES} files touched"),
        "missing_coverage": Segment("missing_coverage", "NO DATA — no testmon record for this test"),
        "suspect_duration": Segment("suspect_duration", "UNTRUSTED TIMING — never-timed placeholder"),
    }
    placeholders = stats.placeholders
    for test_id, dur in durations.items():
        if round(dur, 6) in placeholders:
            segs["suspect_duration"].tests.append((test_id, dur))
            continue
        files = test_files.get(test_id)
        if files is None:
            segs["missing_coverage"].tests.append((test_id, dur))
            continue
        if len(files) > OVER_BROAD_FILES:
            segs["over_broad_tracer"].tests.append((test_id, dur))
            continue
        slow = dur >= slow_threshold
        valuable = value_score(files) >= value_threshold
        key = (
            ("slow_irreplaceable" if valuable else "slow_dispensable")
            if slow
            else ("fast_valuable" if valuable else "fast_broad_only")
        )
        segs[key].tests.append((test_id, dur))
    return list(segs.values())


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def render_top(segment: Segment, n: int = 10) -> list[str]:
    lines = [f"### {segment.key} — top {n} by duration", ""]
    top = sorted(segment.tests, key=lambda t: t[1], reverse=True)[:n]
    if not top:
        lines.append("- _(none)_")
    for test_id, dur in top:
        lines.append(f"- `{dur:6.2f}s` {test_id}")
    lines.append("")
    return lines


def render_report(
    stats: DurationStats,
    duration_segments: list[Segment],
    packages: list[tuple[str, int, float, float, float]],
    coverage_segments: list[Segment] | None,
    redundancy: RedundancyReport | None,
    stale: dict[str, float] | None = None,
) -> str:
    out: list[str] = ["# Test suite analysis", ""]
    denom = stats.total_tests or 1
    out += [
        f"- Live tests: **{stats.total_tests:,}** (file on disk) — "
        f"total **{fmt_duration(stats.total_time)}** ({stats.total_time:,.0f}s, single-threaded wall)",
        f"- Distribution (trusted timings): median {stats.median * 1000:.0f}ms · "
        f"p95 {stats.p95:.2f}s · p99 {stats.p99:.2f}s · max {stats.max_time:.1f}s",
        f"- Pareto: 50% of time in **{stats.pareto_50:,}** tests "
        f"({stats.pareto_50 / denom * 100:.1f}%); "
        f"80% in **{stats.pareto_80:,}** ({stats.pareto_80 / denom * 100:.1f}%)",
        f"- Never-timed placeholders excluded from stats: **{stats.suspect_count:,}** tests "
        f"at {sorted(stats.placeholders)}; **{stats.floored_count:,}** more floored to "
        f"{MIN_DURATION_FLOOR}s (kept as fast)",
        "",
    ]
    if stats.stale_count:
        recorded_denom = stats.recorded_time or 1
        out += [
            f"> ⚠ **Stale entries excluded** ({stats.stale_mode}): the `.test_durations` file records "
            f"**{stats.recorded_tests:,}** entries totalling {fmt_duration(stats.recorded_time)}, but "
            f"**{stats.stale_count:,}** ({stats.stale_count / stats.recorded_tests * 100:.0f}%) point at test "
            f"files that no longer exist — **{fmt_duration(stats.stale_time)} of phantom time "
            f"({stats.stale_time / recorded_denom * 100:.0f}% of the recorded total)**, dropped from every "
            f"stat above. These accumulate because `.test_durations` is merge-updated and CI never prunes it.",
            "",
        ]

    if redundancy is not None:
        out += [
            "## Redundancy & staleness — drop candidates",
            "",
            f"- Isomorphs (identical production coverage): **{redundancy.isomorph_clusters:,}** clusters, "
            f"**{redundancy.isomorph_droppable_tests:,}** droppable tests "
            f"({fmt_duration(redundancy.isomorph_droppable_time)})",
            f"- Trivial coverage (no production files touched): **{redundancy.trivial_tests:,}** tests "
            f"({fmt_duration(redundancy.trivial_time)})",
            f"- Near-isomorphs (Jaccard >= {NEAR_ISOMORPH_JACCARD}, review): "
            f"**{redundancy.near_isomorph_clusters:,}** clusters "
            f"({fmt_duration(redundancy.near_isomorph_time)} potential)",
            "",
        ]

    segments = coverage_segments if coverage_segments is not None else duration_segments
    out += [
        "## Duration segments",
        "",
        "| segment | count | total time | % of suite | description |",
        "|---|---:|---:|---:|---|",
    ]
    for seg in sorted(segments, key=lambda s: s.total, reverse=True):
        pct = seg.total / stats.total_time * 100 if stats.total_time else 0
        out.append(f"| {seg.key} | {seg.count:,} | {fmt_duration(seg.total)} | {pct:.1f}% | {seg.description} |")
    out.append("")

    for seg in sorted(segments, key=lambda s: s.total, reverse=True):
        out += render_top(seg)

    if stale:
        out += ["## Stale entries (file no longer on disk)", ""]
        by_dir: dict[str, list[float]] = defaultdict(list)
        for test_id, dur in stale.items():
            by_dir[package_of(test_id)].append(dur)
        rows = sorted(((pkg, len(v), sum(v)) for pkg, v in by_dir.items()), key=lambda r: r[2], reverse=True)[:10]
        out += ["| package | stale entries | phantom time |", "|---|---:|---:|"]
        for pkg, count, total in rows:
            out.append(f"| {pkg} | {count:,} | {fmt_duration(total)} |")
        out.append("")
        out += ["### stale — top 10 by (phantom) duration", ""]
        for test_id, dur in sorted(stale.items(), key=lambda t: t[1], reverse=True)[:10]:
            out.append(f"- `{dur:6.2f}s` {test_id}")
        out.append("")

    out += ["## Hottest packages (first 2 path segments)", ""]
    out += ["| package | tests | total time | mean | median |", "|---|---:|---:|---:|---:|"]
    for pkg, count, total, mean, median in packages:
        out.append(f"| {pkg} | {count:,} | {fmt_duration(total)} | {mean:.2f}s | {median * 1000:.0f}ms |")
    out.append("")

    if coverage_segments is None:
        out += [
            "## Coverage / redundancy analysis",
            "",
            "_Skipped — no testmon coverage data supplied._ Re-run with `--testmon "
            "path/to/.testmondata` (a pytest `--testmon` SQLite DB) or `--pickle path/to/merged.pkl` "
            "to enable isomorph/near-isomorph detection and duration×coverage segmentation.",
            "",
        ]

    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--durations", type=Path, default=DEFAULT_DURATIONS, help="pytest-split .test_durations file")
    parser.add_argument(
        "--testmon", type=Path, help="pytest --testmon .testmondata SQLite DB (enables coverage analysis)"
    )
    parser.add_argument("--pickle", type=Path, help="pre-merged coverage pickle {'test_files':..., 'high_fanout':...}")
    parser.add_argument("--high-fanout", type=Path, default=DEFAULT_HIGH_FANOUT, help="high-fanout file list")
    parser.add_argument(
        "--collect",
        action="store_true",
        help="cross-check staleness against `pytest --collect-only` (accurate but slow; needs a test env)",
    )
    parser.add_argument(
        "--keep-stale",
        action="store_true",
        help="do not segregate stale entries — score every recorded timing (legacy behavior)",
    )
    parser.add_argument("--out", type=Path, help="write the markdown report here (else stdout)")
    args = parser.parse_args(argv)

    if not args.durations.exists():
        parser.error(f"durations file not found: {args.durations}")

    durations = load_durations(args.durations)

    stale: dict[str, float] = {}
    stale_mode = "file-existence"
    if not args.keep_stale:
        live_ids = collect_live_ids() if args.collect else None
        if live_ids is not None:
            stale_mode = "pytest --collect-only"
            live = {t: d for t, d in durations.items() if t in live_ids}
            stale = {t: d for t, d in durations.items() if t not in live_ids}
        else:
            live, stale = partition_existing(durations)
        durations = live

    stats = compute_duration_stats(durations, stale, stale_mode)
    duration_segments = segment_by_duration(durations, stats)
    packages = hottest_packages(durations)

    coverage_segments: list[Segment] | None = None
    redundancy: RedundancyReport | None = None
    test_files: dict[str, set[str]] | None = None
    high_fanout = load_high_fanout(args.high_fanout)

    if args.pickle:
        test_files, pickle_high_fanout = load_pickle_coverage(args.pickle)
        high_fanout |= pickle_high_fanout
    elif args.testmon:
        test_files = load_testmon_coverage(args.testmon)

    if test_files is not None:
        redundancy = analyze_redundancy(test_files, durations)
        coverage_segments = segment_by_coverage(test_files, durations, high_fanout, stats)

    report = render_report(stats, duration_segments, packages, coverage_segments, redundancy, stale)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report)
        sys.stderr.write(f"wrote {args.out}\n")
    else:
        sys.stdout.write(report + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
