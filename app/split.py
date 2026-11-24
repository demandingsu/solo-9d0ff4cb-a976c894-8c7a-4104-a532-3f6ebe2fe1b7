"""Split one archived furnace run into two independent archives.

Two consecutive furnace runs were occasionally recorded under the same run id,
so a single cold archive may hold two runs back to back.  A split cuts the
stored archive at an absolute UTC minute boundary:

* the *front* segment keeps the original run id,
* the *back* segment gets a brand-new run id,
* both segments inherit the original band limits, tolerance and retained-point
  limit, and both must contain at least one measured minute.

The archive only stores compressed anchors, never the raw readings, so the
split works on the **reconstructed** per-minute series, which is exactly what
every query already returns.  Two invariants make the split safe:

* every minute keeps the same reconstructed value, missing flag and band
  classification before and after the split — anchors are chosen so the new
  polylines reproduce the current reconstruction exactly, minute by minute;
* the provenance of every value is preserved.  Minutes that were retained
  samples of the original archive keep their original reading (``measured``);
  any anchor that has to be introduced at the cut carries an interpolated value
  and is marked ``reconstructed``.  Re-splitting an already split archive keeps
  those marks, so the distinction between a real sample and a reconstructed
  value survives every number of splits.

A segment is independently archivable only when its minimum exact anchor chain
fits inside the inherited retained-point limit; otherwise the whole split is
rejected and nothing is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .planner import (
    MAX_RUN_MINUTES,
    PlanError,
    Span,
    build_segments,
    reconstruct_spans,
)

# Provenance of a retained point's value.
SOURCE_MEASURED = "measured"
SOURCE_RECONSTRUCTED = "reconstructed"
VALID_SOURCES = frozenset({SOURCE_MEASURED, SOURCE_RECONSTRUCTED})


@dataclass(frozen=True)
class SplitSegment:
    """One half of a split, expressed in its own local minute offsets."""

    run_id: str
    start_minute_utc: int
    readings: tuple[Optional[int], ...]
    # Local offset -> value, sorted, exactly reproducing ``readings``.
    retained_points: tuple[tuple[int, int], ...]
    # Local offset -> provenance of the corresponding retained point.
    retained_sources: tuple[str, ...]
    missing_ranges: tuple[tuple[int, int], ...]
    measured_anchor_count: int
    reconstructed_anchor_count: int


@dataclass(frozen=True)
class SplitPlan:
    front: SplitSegment
    back: SplitSegment


def _exact_chain(values: Sequence[int]) -> list[int]:
    """Minimum anchor chain reproducing ``values`` exactly, point for point.

    Same dynamic program as :func:`app.planner.plan_archive`, except a pair of
    anchors is feasible only when the straight line between them reproduces
    every intermediate value *exactly* (no tolerance slack, no band check —
    equality already implies both).  Adjacent anchors are always feasible, so a
    chain always exists.
    """
    n = len(values)
    if n == 1:
        return [0]
    infinity = n + 1
    cost = [infinity] * n
    parent = [-1] * n
    cost[0] = 1
    for end in range(1, n):
        best = infinity
        best_start = -1
        for start in range(end - 1, -1, -1):
            if cost[start] + 1 >= best:
                continue
            if _exact_pair_feasible(values, start, end):
                best = cost[start] + 1
                best_start = start
                if cost[start] == 1:
                    break
        cost[end] = best
        parent[end] = best_start
    chain: list[int] = []
    cursor = n - 1
    while cursor != -1:
        chain.append(cursor)
        cursor = parent[cursor]
    chain.reverse()
    return chain


def _exact_pair_feasible(values: Sequence[int], first: int, last: int) -> bool:
    """Whether the line between two anchors reproduces all inner values exactly."""
    left_value = values[first]
    right_value = values[last]
    span = last - first
    for index in range(first + 1, last):
        numerator = left_value * (last - index) + right_value * (index - first)
        # Round halves away from zero, matching reconstruct_between.
        if numerator < 0:
            expected = -((-numerator + span // 2) // span)
        else:
            expected = (numerator + span // 2) // span
        if expected != values[index]:
            return False
    return True


def _local_missing_ranges(readings: Sequence[Optional[int]]) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    start: Optional[int] = None
    for offset, value in enumerate(readings):
        if value is None:
            if start is None:
                start = offset
        elif start is not None:
            ranges.append((start, offset - start))
            start = None
    if start is not None:
        ranges.append((start, len(readings) - start))
    return tuple(ranges)


def _plan_segment(
    *,
    run_id: str,
    start_minute_utc: int,
    readings: tuple[Optional[int], ...],
    anchor_offsets: set[int],
    anchor_values: dict[int, int],
    measured_offsets: frozenset[int],
    max_retained: int,
) -> SplitSegment:
    """Compress one cut segment while preserving exact values and provenance."""
    minute_count = len(readings)
    if minute_count <= 0 or minute_count > MAX_RUN_MINUTES:
        raise PlanError(
            f"split segment covers {minute_count} minutes; each segment must hold "
            f"between 1 and {MAX_RUN_MINUTES} minutes"
        )
    segments = build_segments(readings)
    if not segments:
        raise PlanError(
            f"split segment for run {run_id!r} contains no measured minutes"
        )

    retained: list[int] = []
    for segment in segments:
        values = list(segment.values)
        chain = _exact_chain(values)
        retained.extend(segment.offset + index for index in chain)
    retained_offsets = sorted(set(retained))

    if len(retained_offsets) > max_retained:
        raise PlanError(
            f"no feasible split: segment for run {run_id!r} needs at least "
            f"{len(retained_offsets)} retained points to reproduce its minutes "
            f"exactly, but the inherited limit is {max_retained}",
            details={
                "run_id": run_id,
                "minimum_retained_points": len(retained_offsets),
                "max_retained_points": max_retained,
            },
        )

    points = [(offset, readings[offset]) for offset in retained_offsets]
    for offset, value in points:
        if value is None:  # pragma: no cover - anchors come from real segments
            raise PlanError("retained point landed on a missing minute")
        # An offset that was an anchor of the parent archive must keep its exact
        # stored value; every other anchor is a newly interpolated value.
        if offset in anchor_offsets and anchor_values[offset] != value:
            raise PlanError(
                "split changed a previously retained measured value"
            )  # pragma: no cover

    sources = tuple(
        SOURCE_MEASURED if offset in measured_offsets else SOURCE_RECONSTRUCTED
        for offset in retained_offsets
    )

    # Independent proof that the new anchors rebuild every minute exactly.
    rebuilt = reconstruct_spans(
        spans_from_readings(readings),
        {offset: value for offset, value in points},
        minute_count,
    )
    if rebuilt != list(readings):
        raise PlanError(
            "split reconstruction diverges from the archived minutes"
        )  # pragma: no cover

    measured_anchor_count = sources.count(SOURCE_MEASURED)
    return SplitSegment(
        run_id=run_id,
        start_minute_utc=start_minute_utc,
        readings=readings,
        retained_points=tuple(points),
        retained_sources=sources,
        missing_ranges=_local_missing_ranges(readings),
        measured_anchor_count=measured_anchor_count,
        reconstructed_anchor_count=len(sources) - measured_anchor_count,
    )


def spans_from_readings(
    readings: Sequence[Optional[int]],
) -> tuple[Span, ...]:
    return tuple(
        (segment.offset, segment.end_offset) for segment in build_segments(readings)
    )


def plan_split(
    *,
    run_id: str,
    new_run_id: str,
    start_minute_utc: int,
    minute_count: int,
    spans: Sequence[Span],
    anchors: dict[int, int],
    anchor_sources: Sequence[str],
    max_retained: int,
    cut_minute_utc: int,
) -> SplitPlan:
    """Build the two segment plans for a split, or raise :class:`PlanError`.

    Parameters describe the parent archive in its own local offsets (0-based).
    ``cut_minute_utc`` is the first absolute minute belonging to the back run.
    The band limits and tolerance are not needed here: exact per-minute
    reproduction already guarantees every band classification is unchanged.
    """
    if new_run_id == run_id:
        raise PlanError("new_run_id must differ from the original run_id")
    if not (start_minute_utc < cut_minute_utc <= start_minute_utc + minute_count - 1):
        raise PlanError(
            "split cut must fall strictly inside the archived minute range"
        )

    if len(anchors) != len(anchor_sources) or any(
        source not in VALID_SOURCES for source in anchor_sources
    ):
        raise PlanError("archived retained-point provenance is inconsistent")

    # The exact per-minute series every query currently returns.
    full_values = reconstruct_spans(tuple(spans), dict(anchors), minute_count)

    cut_offset = cut_minute_utc - start_minute_utc
    front_readings = tuple(full_values[:cut_offset])
    back_readings = tuple(full_values[cut_offset:])

    # Offsets (local to each segment) that were anchors of the parent archive.
    front_anchor_offsets = {
        offset for offset in anchors if offset < cut_offset
    }
    back_anchor_offsets = {
        offset - cut_offset for offset in anchors if offset >= cut_offset
    }
    back_anchor_values = {
        offset - cut_offset: value
        for offset, value in anchors.items()
        if offset >= cut_offset
    }
    front_anchor_values = {
        offset: value for offset, value in anchors.items() if offset < cut_offset
    }

    # Only values that were genuinely sampled are "measured".  A parent anchor
    # already marked reconstructed stays reconstructed after the split.
    measured_offsets: set[int] = set()
    for offset, source in zip(sorted(anchors), anchor_sources):
        if source == SOURCE_MEASURED:
            measured_offsets.add(offset)
    front_measured = frozenset(measured_offsets)
    back_measured = frozenset(
        offset - cut_offset for offset in measured_offsets if offset >= cut_offset
    )

    front = _plan_segment(
        run_id=run_id,
        start_minute_utc=start_minute_utc,
        readings=front_readings,
        anchor_offsets=front_anchor_offsets,
        anchor_values=front_anchor_values,
        measured_offsets=front_measured,
        max_retained=max_retained,
    )
    back = _plan_segment(
        run_id=new_run_id,
        start_minute_utc=cut_minute_utc,
        readings=back_readings,
        anchor_offsets=back_anchor_offsets,
        anchor_values=back_anchor_values,
        measured_offsets=back_measured,
        max_retained=max_retained,
    )

    # Whole-run guard: concatenation reproduces the parent series exactly, so
    # per-minute reconstructed values, missing flags and (via the unchanged band
    # limits) band classifications are identical before and after the split.
    combined = list(front.readings) + list(back.readings)
    if combined != full_values:
        raise PlanError(
            "split segments do not reproduce the archived series"
        )  # pragma: no cover
    return SplitPlan(front=front, back=back)
