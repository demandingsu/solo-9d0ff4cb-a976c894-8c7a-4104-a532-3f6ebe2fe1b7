"""SQLite persistence for compressed furnace archives.

Only the compressed archive is stored: retained sample points plus the ranges
of missing minutes.  Raw per-minute readings are never written to disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Mapping, Optional, Sequence

from .planner import (
    MAX_RUN_MINUTES,
    PlanError,
    Span,
    reconstruct_spans,
    spans_from_missing_ranges,
)
from .split import SOURCE_MEASURED, VALID_SOURCES

if TYPE_CHECKING:
    from .split import SplitPlan

SCHEMA_VERSION = "2"
PLANNER_VERSION = "2.0.0"
# Version used by archives written before retained-point provenance existed.
LEGACY_PLANNER_VERSION = "1.0.0"
DEFAULT_DB_PATH = os.environ.get("FURNACE_DB_PATH", "/data/furnace_archive.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS furnace_archives (
    run_id                 TEXT PRIMARY KEY,
    start_minute_utc       INTEGER NOT NULL,
    minute_count           INTEGER NOT NULL
                           CHECK (minute_count > 0
                                  AND minute_count <= 360),
    band_low_tenths_c      INTEGER NOT NULL,
    band_high_tenths_c     INTEGER NOT NULL,
    tolerance_tenths_c     INTEGER NOT NULL CHECK (tolerance_tenths_c >= 0),
    max_retained_points    INTEGER NOT NULL CHECK (max_retained_points >= 0),
    retained_count         INTEGER NOT NULL CHECK (retained_count >= 0),
    retained_points_json   TEXT NOT NULL,
    missing_ranges_json    TEXT NOT NULL,
    retained_sources_json  TEXT NOT NULL DEFAULT '[]',
    payload_sha256         TEXT NOT NULL,
    planner_version        TEXT NOT NULL,
    created_at_utc         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_archives_start
    ON furnace_archives (start_minute_utc);
"""

# Add the provenance column to databases created by schema version 1.
MIGRATIONS = (
    """
    ALTER TABLE furnace_archives
        ADD COLUMN retained_sources_json TEXT NOT NULL DEFAULT '[]'
    """,
)


class ArchiveConflict(Exception):
    """The run id is already archived."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id!r} is already archived")
        self.run_id = run_id


class ArchiveSummaryMismatch(Exception):
    """The summary submitted with a split does not match the stored archive."""

    def __init__(self, run_id: str, field: str) -> None:
        super().__init__(
            f"submitted summary for run {run_id!r} does not match the current "
            f"archive: field {field!r} differs"
        )
        self.run_id = run_id
        self.field = field


class ArchiveWriteError(Exception):
    """The archive could not be written."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class ArchiveUnavailable(Exception):
    """The archive database cannot be opened or reached."""


class ArchiveNotFound(Exception):
    """The referenced run id has no archive."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id!r} has no archive")
        self.run_id = run_id


class ArchiveCorrupt(Exception):
    """A stored archive failed its integrity checks."""


@dataclass(frozen=True)
class StoredArchive:
    run_id: str
    start_minute_utc: int
    minute_count: int
    band_low_tenths_c: int
    band_high_tenths_c: int
    tolerance_tenths_c: int
    max_retained_points: int
    retained_count: int
    retained_points: tuple[tuple[int, int], ...]
    missing_ranges: tuple[tuple[int, int], ...]
    payload_sha256: str
    planner_version: str
    created_at_utc: str
    # One provenance tag per retained point, aligned with retained_points.
    # Empty only for pre-split archives whose checksum predates provenance.
    retained_sources: tuple[str, ...] = ()

    @property
    def spans(self) -> tuple[Span, ...]:
        return spans_from_missing_ranges(self.minute_count, self.missing_ranges)

    @property
    def end_minute_utc(self) -> int:
        """Inclusive last minute of the run."""
        return self.start_minute_utc + self.minute_count - 1

    @property
    def effective_sources(self) -> tuple[str, ...]:
        """Provenance per retained point, treating legacy archives as measured."""
        if self.retained_sources:
            return self.retained_sources
        return tuple(SOURCE_MEASURED for _ in self.retained_points)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Fields of the public archive summary that a split request must echo back
# unchanged.  Comparing the full summary (checksum included) detects any change
# to the archive, including tampering and concurrent re-archiving.
SUMMARY_FIELDS: tuple[str, ...] = (
    "run_id",
    "start_minute_utc",
    "end_minute_utc",
    "minute_count",
    "band_low_tenths_c",
    "band_high_tenths_c",
    "tolerance_tenths_c",
    "max_retained_points",
    "retained_points_count",
    "measured_minutes",
    "missing_minutes",
    "segment_count",
    "payload_sha256",
    "planner_version",
    "created_at_utc",
)


def archive_summary_dict(archive: StoredArchive) -> dict[str, object]:
    """The same summary mapping the API returns, used for optimistic checks."""
    measured = sum(end - start for start, end in archive.spans)
    return {
        "run_id": archive.run_id,
        "start_minute_utc": archive.start_minute_utc,
        "end_minute_utc": archive.end_minute_utc,
        "minute_count": archive.minute_count,
        "band_low_tenths_c": archive.band_low_tenths_c,
        "band_high_tenths_c": archive.band_high_tenths_c,
        "tolerance_tenths_c": archive.tolerance_tenths_c,
        "max_retained_points": archive.max_retained_points,
        "retained_points_count": archive.retained_count,
        "measured_minutes": measured,
        "missing_minutes": archive.minute_count - measured,
        "segment_count": len(archive.spans),
        "payload_sha256": archive.payload_sha256,
        "planner_version": archive.planner_version,
        "created_at_utc": archive.created_at_utc,
    }


def _summary_mismatch_field(
    archive: StoredArchive, expected: Mapping[str, object]
) -> Optional[str]:
    """Return the first summary field that differs, or ``None`` when identical."""
    current = archive_summary_dict(archive)
    for field in SUMMARY_FIELDS:
        if field not in expected or expected[field] != current[field]:
            return field
    return None


def missing_ranges_from_readings(
    readings: Sequence[Optional[int]],
) -> tuple[tuple[int, int], ...]:
    """Compress missing minutes into maximal ``[offset, length]`` ranges."""
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


def _canonical_payload(
    *,
    run_id: str,
    start_minute_utc: int,
    minute_count: int,
    band_low: int,
    band_high: int,
    tolerance: int,
    max_retained: int,
    retained_points: Sequence[Sequence[int]],
    missing_ranges: Sequence[Sequence[int]],
    retained_sources: Optional[Sequence[str]] = None,
    planner_version: str = PLANNER_VERSION,
) -> bytes:
    payload = {
        "run_id": run_id,
        "start_minute_utc": start_minute_utc,
        "minute_count": minute_count,
        "band_low_tenths_c": band_low,
        "band_high_tenths_c": band_high,
        "tolerance_tenths_c": tolerance,
        "max_retained_points": max_retained,
        "retained_points": [list(point) for point in retained_points],
        "missing_ranges": [list(item) for item in missing_ranges],
        "planner_version": planner_version,
    }
    # Provenance is part of the checksum for schema v2 archives; legacy v1
    # archives keep their original digest inputs so old files still verify.
    if retained_sources is not None:
        payload["retained_sources"] = list(retained_sources)
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def payload_digest(
    *,
    run_id: str,
    start_minute_utc: int,
    minute_count: int,
    band_low: int,
    band_high: int,
    tolerance: int,
    max_retained: int,
    retained_points: Sequence[Sequence[int]],
    missing_ranges: Sequence[Sequence[int]],
    retained_sources: Optional[Sequence[str]] = None,
    planner_version: str = PLANNER_VERSION,
) -> str:
    return hashlib.sha256(
        _canonical_payload(
            run_id=run_id,
            start_minute_utc=start_minute_utc,
            minute_count=minute_count,
            band_low=band_low,
            band_high=band_high,
            tolerance=tolerance,
            max_retained=max_retained,
            retained_points=retained_points,
            missing_ranges=missing_ranges,
            retained_sources=retained_sources,
            planner_version=planner_version,
        )
    ).hexdigest()


class ArchiveStore:
    """A tiny connection-per-operation SQLite store.

    Writes run in ``BEGIN IMMEDIATE`` transactions and roll back completely on
    failure, so a failed write never disturbs archives already stored.
    """

    def __init__(self, path: str = DEFAULT_DB_PATH, *, timeout: float = 5.0) -> None:
        self.path = path
        self.timeout = timeout

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.path, timeout=self.timeout, isolation_level=None
            )
        except sqlite3.Error as exc:
            raise ArchiveUnavailable(
                f"cannot open archive database at {self.path!r}: {exc}"
            ) from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        except sqlite3.Error as exc:
            connection.close()
            raise ArchiveUnavailable(
                f"cannot use archive database at {self.path!r}: {exc}"
            ) from exc
        return connection

    def init(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise ArchiveUnavailable(
                f"cannot create archive directory {directory!r}: {exc}"
            ) from exc
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            self._migrate(connection)
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, ?)",
                ("schema_version", SCHEMA_VERSION),
            )
            connection.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                (SCHEMA_VERSION,),
            )
            connection.commit()
        except sqlite3.Error as exc:
            raise ArchiveUnavailable(
                f"cannot initialise archive database at {self.path!r}: {exc}"
            ) from exc
        finally:
            connection.close()

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Bring older databases up to the current schema without rewriting rows."""
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(furnace_archives)")
        }
        if "retained_sources_json" not in columns:
            connection.execute(MIGRATIONS[0])

    def healthy(self) -> bool:
        try:
            connection = self._connect()
            try:
                connection.execute("SELECT 1 FROM furnace_archives LIMIT 1")
            finally:
                connection.close()
            return True
        except (ArchiveUnavailable, sqlite3.Error, OSError):
            return False

    def insert(
        self,
        *,
        run_id: str,
        start_minute_utc: int,
        minute_count: int,
        band_low_tenths_c: int,
        band_high_tenths_c: int,
        tolerance_tenths_c: int,
        max_retained_points: int,
        retained_points: Sequence[tuple[int, int]],
        missing_ranges: Sequence[tuple[int, int]],
        retained_sources: Optional[Sequence[str]] = None,
    ) -> StoredArchive:
        if minute_count <= 0 or minute_count > MAX_RUN_MINUTES:
            raise ArchiveWriteError(
                f"minute_count must be between 1 and {MAX_RUN_MINUTES}"
            )
        if retained_sources is None:
            retained_sources = tuple(
                SOURCE_MEASURED for _ in retained_points
            )
        if len(retained_sources) != len(retained_points) or any(
            source not in VALID_SOURCES for source in retained_sources
        ):
            raise ArchiveWriteError(
                "retained_sources must align one-to-one with retained_points"
            )
        points_json = json.dumps(
            [list(point) for point in retained_points], separators=(",", ":")
        )
        ranges_json = json.dumps(
            [list(item) for item in missing_ranges], separators=(",", ":")
        )
        sources_json = json.dumps(list(retained_sources), separators=(",", ":"))
        digest = payload_digest(
            run_id=run_id,
            start_minute_utc=start_minute_utc,
            minute_count=minute_count,
            band_low=band_low_tenths_c,
            band_high=band_high_tenths_c,
            tolerance=tolerance_tenths_c,
            max_retained=max_retained_points,
            retained_points=retained_points,
            missing_ranges=missing_ranges,
            retained_sources=retained_sources,
        )
        created_at = _utc_now()

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM furnace_archives WHERE run_id = ?", (run_id,)
            ).fetchone()
            if exists is not None:
                connection.execute("ROLLBACK")
                raise ArchiveConflict(run_id)
            connection.execute(
                """
                INSERT INTO furnace_archives (
                    run_id, start_minute_utc, minute_count,
                    band_low_tenths_c, band_high_tenths_c, tolerance_tenths_c,
                    max_retained_points, retained_count, retained_points_json,
                    missing_ranges_json, retained_sources_json, payload_sha256,
                    planner_version, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    start_minute_utc,
                    minute_count,
                    band_low_tenths_c,
                    band_high_tenths_c,
                    tolerance_tenths_c,
                    max_retained_points,
                    len(retained_points),
                    points_json,
                    ranges_json,
                    sources_json,
                    digest,
                    PLANNER_VERSION,
                    created_at,
                ),
            )
            connection.execute("COMMIT")
        except ArchiveConflict:
            raise
        except sqlite3.IntegrityError as exc:
            self._safe_rollback(connection)
            if "UNIQUE" in str(exc).upper():
                raise ArchiveConflict(run_id) from exc
            raise ArchiveWriteError(
                f"database rejected the archive: {exc}", retryable=False
            ) from exc
        except (sqlite3.Error, OSError) as exc:
            self._safe_rollback(connection)
            raise ArchiveWriteError(f"failed to write archive: {exc}") from exc
        finally:
            connection.close()

        return StoredArchive(
            run_id=run_id,
            start_minute_utc=start_minute_utc,
            minute_count=minute_count,
            band_low_tenths_c=band_low_tenths_c,
            band_high_tenths_c=band_high_tenths_c,
            tolerance_tenths_c=tolerance_tenths_c,
            max_retained_points=max_retained_points,
            retained_count=len(retained_points),
            retained_points=tuple(retained_points),
            missing_ranges=tuple(missing_ranges),
            payload_sha256=digest,
            planner_version=PLANNER_VERSION,
            created_at_utc=created_at,
            retained_sources=tuple(retained_sources),
        )

    @staticmethod
    def _safe_rollback(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def replace_with_split(
        self,
        *,
        original_run_id: str,
        new_run_id: str,
        expected_summary: Mapping[str, object],
        plan: "SplitPlan",
        band_low: int,
        band_high: int,
        tolerance: int,
        max_retained: int,
    ) -> tuple[StoredArchive, StoredArchive]:
        """Replace one archive with its two split segments in one transaction.

        Every precondition — current row existence, summary match, free new id —
        is re-checked inside the same ``BEGIN IMMEDIATE`` transaction that does
        the writes, so a concurrent change cannot slip through and any failure
        rolls the whole operation back, leaving prior archives queryable.
        """
        created_at = _utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM furnace_archives WHERE run_id = ?",
                (original_run_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise ArchiveNotFound(original_run_id)
            if new_run_id != original_run_id:
                clash = connection.execute(
                    "SELECT 1 FROM furnace_archives WHERE run_id = ?",
                    (new_run_id,),
                ).fetchone()
                if clash is not None:
                    connection.execute("ROLLBACK")
                    raise ArchiveConflict(new_run_id)

            current = self._row_to_archive(row)
            mismatch = _summary_mismatch_field(current, expected_summary)
            if mismatch is not None:
                connection.execute("ROLLBACK")
                raise ArchiveSummaryMismatch(original_run_id, mismatch)

            # Remove the parent row first so the front segment can re-use its
            # run id; everything below still runs inside this transaction, so a
            # later failure restores the parent as part of the rollback.
            connection.execute(
                "DELETE FROM furnace_archives WHERE run_id = ?",
                (original_run_id,),
            )

            written: list[StoredArchive] = []
            for segment in (plan.front, plan.back):
                digest = payload_digest(
                    run_id=segment.run_id,
                    start_minute_utc=segment.start_minute_utc,
                    minute_count=len(segment.readings),
                    band_low=band_low,
                    band_high=band_high,
                    tolerance=tolerance,
                    max_retained=max_retained,
                    retained_points=segment.retained_points,
                    missing_ranges=segment.missing_ranges,
                    retained_sources=segment.retained_sources,
                )
                connection.execute(
                    """
                    INSERT INTO furnace_archives (
                        run_id, start_minute_utc, minute_count,
                        band_low_tenths_c, band_high_tenths_c,
                        tolerance_tenths_c, max_retained_points, retained_count,
                        retained_points_json, missing_ranges_json,
                        retained_sources_json, payload_sha256, planner_version,
                        created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        segment.run_id,
                        segment.start_minute_utc,
                        len(segment.readings),
                        band_low,
                        band_high,
                        tolerance,
                        max_retained,
                        len(segment.retained_points),
                        json.dumps(
                            [list(point) for point in segment.retained_points],
                            separators=(",", ":"),
                        ),
                        json.dumps(
                            [list(item) for item in segment.missing_ranges],
                            separators=(",", ":"),
                        ),
                        json.dumps(
                            list(segment.retained_sources), separators=(",", ":")
                        ),
                        digest,
                        PLANNER_VERSION,
                        created_at,
                    ),
                )
                written.append(
                    StoredArchive(
                        run_id=segment.run_id,
                        start_minute_utc=segment.start_minute_utc,
                        minute_count=len(segment.readings),
                        band_low_tenths_c=band_low,
                        band_high_tenths_c=band_high,
                        tolerance_tenths_c=tolerance,
                        max_retained_points=max_retained,
                        retained_count=len(segment.retained_points),
                        retained_points=segment.retained_points,
                        missing_ranges=segment.missing_ranges,
                        payload_sha256=digest,
                        planner_version=PLANNER_VERSION,
                        created_at_utc=created_at,
                        retained_sources=segment.retained_sources,
                    )
                )

            connection.execute("COMMIT")
        except (ArchiveConflict, ArchiveNotFound, ArchiveSummaryMismatch):
            raise
        except sqlite3.IntegrityError as exc:
            self._safe_rollback(connection)
            if "UNIQUE" in str(exc).upper():
                raise ArchiveConflict(new_run_id) from exc
            raise ArchiveWriteError(
                f"database rejected the split: {exc}", retryable=False
            ) from exc
        except (sqlite3.Error, OSError) as exc:
            self._safe_rollback(connection)
            raise ArchiveWriteError(f"failed to write split archives: {exc}") from exc
        finally:
            connection.close()

        return written[0], written[1]

    def _row_to_archive(self, row: sqlite3.Row) -> StoredArchive:
        try:
            retained_raw = json.loads(row["retained_points_json"])
            missing_raw = json.loads(row["missing_ranges_json"])
            sources_raw = json.loads(row["retained_sources_json"])
        except (TypeError, ValueError) as exc:
            raise ArchiveCorrupt(
                f"archive {row['run_id']!r} has unreadable JSON payload"
            ) from exc

        retained: list[tuple[int, int]] = []
        try:
            for item in retained_raw:
                if len(item) != 2:
                    raise ValueError("retained point must have two entries")
                offset, value = int(item[0]), int(item[1])
                retained.append((offset, value))
            missing: list[tuple[int, int]] = []
            for item in missing_raw:
                if len(item) != 2:
                    raise ValueError("missing range must have two entries")
                start, length = int(item[0]), int(item[1])
                missing.append((start, length))
            sources: list[str] = [str(item) for item in sources_raw]
        except (TypeError, ValueError, IndexError) as exc:
            raise ArchiveCorrupt(
                f"archive {row['run_id']!r} has malformed point data"
            ) from exc

        stored_version = row["planner_version"]
        if stored_version == LEGACY_PLANNER_VERSION:
            # Legacy archives predate provenance: their digest covers the v1
            # payload only, and every retained point was a real sample.
            digest_sources = None
            if sources:
                raise ArchiveCorrupt(
                    f"legacy archive {row['run_id']!r} unexpectedly carries "
                    "retained-point provenance"
                )
            sources = [SOURCE_MEASURED] * len(retained)
        else:
            digest_sources = sources
        if any(source not in VALID_SOURCES for source in sources):
            raise ArchiveCorrupt(
                f"archive {row['run_id']!r} has an unknown retained-point source"
            )
        if len(sources) != len(retained):
            raise ArchiveCorrupt(
                f"archive {row['run_id']!r} provenance does not align with its "
                "retained points"
            )

        expected = payload_digest(
            run_id=row["run_id"],
            start_minute_utc=row["start_minute_utc"],
            minute_count=row["minute_count"],
            band_low=row["band_low_tenths_c"],
            band_high=row["band_high_tenths_c"],
            tolerance=row["tolerance_tenths_c"],
            max_retained=row["max_retained_points"],
            retained_points=retained,
            missing_ranges=missing,
            retained_sources=digest_sources,
            planner_version=stored_version,
        )
        if expected != row["payload_sha256"]:
            raise ArchiveCorrupt(
                f"archive {row['run_id']!r} failed its integrity checksum"
            )
        if len(retained) != row["retained_count"]:
            raise ArchiveCorrupt(
                f"archive {row['run_id']!r} retained count does not match payload"
            )
        if len(retained) > row["max_retained_points"]:
            raise ArchiveCorrupt(
                f"archive {row['run_id']!r} exceeds its own retained-point limit"
            )

        archive = StoredArchive(
            run_id=row["run_id"],
            start_minute_utc=row["start_minute_utc"],
            minute_count=row["minute_count"],
            band_low_tenths_c=row["band_low_tenths_c"],
            band_high_tenths_c=row["band_high_tenths_c"],
            tolerance_tenths_c=row["tolerance_tenths_c"],
            max_retained_points=row["max_retained_points"],
            retained_count=row["retained_count"],
            retained_points=tuple(retained),
            missing_ranges=tuple(missing),
            payload_sha256=row["payload_sha256"],
            planner_version=stored_version,
            created_at_utc=row["created_at_utc"],
            retained_sources=tuple(sources),
        )
        self._validate_structure(archive)
        return archive

    @staticmethod
    def _validate_structure(archive: StoredArchive) -> None:
        """Check stored anchors and gaps against each other."""
        if archive.minute_count <= 0 or archive.minute_count > MAX_RUN_MINUTES:
            raise ArchiveCorrupt(
                f"archive {archive.run_id!r} has an invalid minute count"
            )
        if archive.band_low_tenths_c > archive.band_high_tenths_c:
            raise ArchiveCorrupt(f"archive {archive.run_id!r} has an inverted band")
        try:
            spans = archive.spans
        except PlanError as exc:
            raise ArchiveCorrupt(
                f"archive {archive.run_id!r} has inconsistent missing ranges"
            ) from exc
        if not spans:
            raise ArchiveCorrupt(
                f"archive {archive.run_id!r} has no measured minutes"
            )
        anchors: dict[int, int] = {}
        for offset, value in archive.retained_points:
            if offset < 0 or offset >= archive.minute_count:
                raise ArchiveCorrupt(
                    f"archive {archive.run_id!r} has an out-of-range point"
                )
            if offset in anchors and anchors[offset] != value:
                raise ArchiveCorrupt(
                    f"archive {archive.run_id!r} has conflicting duplicate points"
                )
            anchors[offset] = value
        for span_start, span_end in spans:
            if span_start not in anchors or span_end - 1 not in anchors:
                raise ArchiveCorrupt(
                    f"archive {archive.run_id!r} does not retain the endpoints "
                    f"of span {span_start}..{span_end - 1}"
                )
        if len(anchors) != archive.retained_count:
            raise ArchiveCorrupt(
                f"archive {archive.run_id!r} has duplicate retained offsets"
            )
        # Reconstruction is bounded and deterministic; this also proves the
        # stored anchors can actually rebuild the whole run.
        reconstruct_spans(spans, anchors, archive.minute_count)

    def get(self, run_id: str) -> Optional[StoredArchive]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM furnace_archives WHERE run_id = ?", (run_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise ArchiveUnavailable(f"failed to read archive: {exc}") from exc
        finally:
            connection.close()
        if row is None:
            return None
        return self._row_to_archive(row)

    def list_recent(self, limit: int = 50) -> list[StoredArchive]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM furnace_archives ORDER BY created_at_utc DESC, "
                "run_id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ArchiveUnavailable(f"failed to list archives: {exc}") from exc
        finally:
            connection.close()
        return [self._row_to_archive(row) for row in rows]
