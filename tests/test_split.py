"""Tests for the run-splitting API: provenance, invariants and rollback."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app


@pytest.fixture()
def store_path(tmp_path: Any) -> str:
    return str(tmp_path / "archive.sqlite3")


@pytest.fixture()
def client(store_path: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FURNACE_DB_PATH", store_path)
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": "HT-2026-0042",
        "start_minute_utc": 29_000_000,
        "readings": [700, 750, 800, 800, 800, 790, 780, 780],
        "band_low_tenths_c": 780,
        "band_high_tenths_c": 820,
        "tolerance_tenths_c": 5,
        "max_retained_points": 8,
    }
    payload.update(overrides)
    return payload


def _archive(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post("/api/v1/archives", json=_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()["archive"]


def _minutes(client: TestClient, run_id: str, start: int, end: int) -> list[dict[str, Any]]:
    response = client.get(
        f"/api/v1/archives/{run_id}/minutes",
        params={"start_minute_utc": start, "end_minute_utc": end},
    )
    assert response.status_code == 200, response.text
    return response.json()["minutes"]


def _split(
    client: TestClient,
    run_id: str,
    summary: dict[str, Any],
    cut: int,
    new_run_id: str,
) -> Any:
    return client.post(
        f"/api/v1/archives/{run_id}/split",
        json={
            "expected_summary": summary,
            "back_run_start_minute_utc": cut,
            "new_run_id": new_run_id,
        },
    )


def test_split_keeps_every_minute_identical(client: TestClient) -> None:
    summary = _archive(client)
    before = _minutes(client, "HT-2026-0042", 29_000_000, 29_000_007)

    response = _split(
        client, "HT-2026-0042", summary, cut=29_000_003, new_run_id="HT-2026-0043"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "split"
    assert body["split_at_minute_utc"] == 29_000_003
    front_summary = body["front_archive"]
    back_summary = body["back_archive"]
    assert front_summary["run_id"] == "HT-2026-0042"
    assert back_summary["run_id"] == "HT-2026-0043"
    # Both segments inherit the band, tolerance and point limit.
    for segment in (front_summary, back_summary):
        assert segment["band_low_tenths_c"] == 780
        assert segment["band_high_tenths_c"] == 820
        assert segment["tolerance_tenths_c"] == 5
        assert segment["max_retained_points"] == 8
    assert front_summary["end_minute_utc"] == 29_000_002
    assert back_summary["start_minute_utc"] == 29_000_003
    assert back_summary["end_minute_utc"] == 29_000_007

    front = _minutes(client, "HT-2026-0042", 29_000_000, 29_000_002)
    back = _minutes(client, "HT-2026-0043", 29_000_003, 29_000_007)
    after = front + back

    # Reconstructed values, missing flags and band states match minute by minute.
    assert len(after) == len(before)
    for old, new in zip(before, after):
        assert new["minute_utc"] == old["minute_utc"]
        assert new["value_tenths_c"] == old["value_tenths_c"]
        assert new["missing"] == old["missing"]
        assert new["band_state"] == old["band_state"]


def test_query_distinguishes_measured_and_reconstructed_minutes(client: TestClient) -> None:
    _archive(client)
    minutes = _minutes(client, "HT-2026-0042", 29_000_000, 29_000_007)
    sources = {m["minute_utc"]: (m["value_source"], m["retained"]) for m in minutes}
    # Retained anchors are measured originals; interpolated minutes are not.
    retained_minutes = [
        minute for minute, (_, retained) in sources.items() if retained
    ]
    assert retained_minutes  # at least the segment endpoints
    for minute, (source, retained) in sources.items():
        if retained:
            assert source == "measured"
        else:
            assert source == "reconstructed"


def test_split_introduces_reconstructed_anchor_at_cut(client: TestClient) -> None:
    # A straight ramp archives with only its two endpoints; cutting in the
    # middle forces a new anchor whose value is interpolated.
    summary = _archive(
        client,
        run_id="HT-RAMP",
        readings=[700, 710, 720, 730, 740, 750],
        tolerance_tenths_c=0,
        max_retained_points=6,
    )
    assert summary["retained_points_count"] == 2

    response = _split(client, "HT-RAMP", summary, 29_000_004, "HT-RAMP-2")
    assert response.status_code == 200, response.text
    front = response.json()["front_archive"]
    back = response.json()["back_archive"]
    # Front keeps the original first anchor and gains a reconstructed endpoint
    # at the cut; the back keeps the original last anchor and gains a
    # reconstructed endpoint at the cut (the cut minute was not retained).
    assert front["measured_anchor_count"] == 1
    assert front["reconstructed_anchor_count"] == 1
    assert back["measured_anchor_count"] == 1
    assert back["reconstructed_anchor_count"] == 1

    front_minutes = _minutes(client, "HT-RAMP", 29_000_000, 29_000_003)
    back_minutes = _minutes(client, "HT-RAMP-2", 29_000_004, 29_000_005)
    cut_anchor = front_minutes[-1]
    assert cut_anchor["retained"] is True
    assert cut_anchor["value_source"] == "reconstructed"
    assert cut_anchor["value_tenths_c"] == 730
    back_first = back_minutes[0]
    assert back_first["retained"] is True
    assert back_first["value_source"] == "reconstructed"
    assert back_first["value_tenths_c"] == 740
    # Exact values survive the split.
    assert [m["value_tenths_c"] for m in front_minutes] == [700, 710, 720, 730]
    assert [m["value_tenths_c"] for m in back_minutes] == [740, 750]
    # The original final sample stays a measured anchor in the back run.
    assert back_minutes[-1]["value_source"] == "measured"


def test_re_split_preserves_provenance(client: TestClient) -> None:
    summary = _archive(
        client,
        run_id="HT-RAMP",
        readings=[700, 710, 720, 730, 740, 750],
        tolerance_tenths_c=0,
        max_retained_points=6,
    )
    first = _split(client, "HT-RAMP", summary, 29_000_004, "HT-RAMP-2")
    assert first.status_code == 200
    front_summary = first.json()["front_archive"]

    # Split the front segment again. Its reconstructed anchor must stay
    # reconstructed even though it is now a segment endpoint.
    second = _split(client, "HT-RAMP", front_summary, 29_000_002, "HT-RAMP-3")
    assert second.status_code == 200, second.text
    deepest = second.json()["back_archive"]
    minutes = _minutes(client, "HT-RAMP-3", 29_000_002, 29_000_003)
    # Minute 29_000_002 was an interpolated anchor introduced by the first split.
    assert minutes[0]["value_source"] == "reconstructed"
    assert minutes[0]["retained"] is True
    assert deepest["reconstructed_anchor_count"] >= 1
    assert [m["value_tenths_c"] for m in minutes] == [720, 730]


def test_split_keeps_missing_minutes_and_band_intervals(client: TestClient) -> None:
    readings = [800, 800, None, None, 800, 700, 800]
    summary = _archive(
        client,
        run_id="HT-GAP", readings=readings, max_retained_points=10
    )
    before = client.get(
        "/api/v1/archives/HT-GAP/minutes",
        params={"start_minute_utc": 29_000_000, "end_minute_utc": 29_000_006},
    ).json()

    response = _split(client, "HT-GAP", summary, 29_000_005, "HT-GAP-2")
    assert response.status_code == 200, response.text

    front = client.get(
        "/api/v1/archives/HT-GAP/minutes",
        params={"start_minute_utc": 29_000_000, "end_minute_utc": 29_000_004},
    ).json()
    back = client.get(
        "/api/v1/archives/HT-GAP-2/minutes",
        params={"start_minute_utc": 29_000_005, "end_minute_utc": 29_000_006},
    ).json()
    assert front["missing_minutes"] == [29_000_002, 29_000_003]
    assert back["missing_minutes"] == []
    # Missing minutes are not bridged: the front run keeps two separate spans.
    assert front["in_band_intervals"] == [
        {"start_minute_utc": 29_000_000, "end_minute_utc": 29_000_001,
         "duration_minutes": 2},
        {"start_minute_utc": 29_000_004, "end_minute_utc": 29_000_004,
         "duration_minutes": 1},
    ]
    assert back["in_band_intervals"] == [
        {"start_minute_utc": 29_000_006, "end_minute_utc": 29_000_006,
         "duration_minutes": 1},
    ]
    combined_missing = front["missing_minutes"] + back["missing_minutes"]
    assert combined_missing == before["missing_minutes"]
    combined = front["minutes"] + back["minutes"]
    for old, new in zip(before["minutes"], combined):
        assert old["missing"] == new["missing"]
        assert old["band_state"] == new["band_state"]
        assert old["value_tenths_c"] == new["value_tenths_c"]


def test_split_rejected_when_a_segment_has_no_measured_minute(client: TestClient) -> None:
    readings = [800, 800, None, None, 800]
    summary = _archive(client, run_id="HT-EDGE", readings=readings, max_retained_points=10)
    # Cut at the first missing minute: the back segment starts with two gaps.
    response = _split(client, "HT-EDGE", summary, 29_000_002, "HT-EDGE-2")
    assert response.status_code == 200  # back still has minute 29_000_004 measured

    summary_front = client.get("/api/v1/archives/HT-EDGE").json()
    # Cutting so one side is entirely missing must fail.
    response = _split(
        client, "HT-EDGE", summary_front, 29_000_002, "HT-EDGE-3"
    )
    assert response.status_code == 422
    assert response.json()["reason_code"] == "no_feasible_split"


def test_split_at_run_boundaries_is_rejected(client: TestClient) -> None:
    summary = _archive(client)
    for cut in (29_000_000, 29_000_008):
        response = _split(client, "HT-2026-0042", summary, cut, "HT-X")
        assert response.status_code == 422
        assert response.json()["reason_code"] in {
            "invalid_request",
            "no_feasible_split",
        }


def test_split_unknown_run_returns_404(client: TestClient) -> None:
    summary = _archive(client)
    response = _split(client, "NOPE", summary, 29_000_003, "HT-X")
    assert response.status_code == 404
    assert response.json()["reason_code"] == "archive_not_found"


def test_split_with_stale_summary_is_rejected_and_rolled_back(client: TestClient) -> None:
    summary = _archive(client)
    stale = dict(summary)
    stale["payload_sha256"] = "0" * 64
    response = _split(client, "HT-2026-0042", stale, 29_000_003, "HT-STALE")
    assert response.status_code == 409
    assert response.json()["reason_code"] == "summary_mismatch"
    # Nothing changed: original archive still spans all eight minutes.
    assert (
        client.get("/api/v1/archives/HT-2026-0042").json()["end_minute_utc"]
        == 29_000_007
    )
    assert client.get("/api/v1/archives/HT-STALE").status_code == 404


def test_split_with_occupied_new_run_id_is_rejected(client: TestClient) -> None:
    summary = _archive(client)
    _archive(client, run_id="HT-TAKEN", readings=[800, 810, 820])
    response = _split(client, "HT-2026-0042", summary, 29_000_003, "HT-TAKEN")
    assert response.status_code == 409
    assert response.json()["reason_code"] == "run_already_archived"
    # The original archive is untouched.
    assert (
        client.get("/api/v1/archives/HT-2026-0042").json()["minute_count"] == 8
    )


def test_split_planner_rejects_when_inherited_limit_does_not_fit_segment() -> None:
    # Unit-level guard: each split segment must fit the inherited retained-point
    # limit on its own. For a healthy archive this always holds mathematically
    # (a cut can add at most one anchor per segment while the other segment
    # keeps at least one parent anchor), so the cap is exercised directly with
    # an artificially tight limit.
    from app.split import PlanError, _plan_segment

    with pytest.raises(PlanError) as excinfo:
        _plan_segment(
            run_id="HT-UNIT",
            start_minute_utc=100,
            readings=(800, 805, 795, 805),
            anchor_offsets=set(),
            anchor_values={},
            measured_offsets=frozenset(),
            max_retained=2,
        )
    # The jittery segment needs four exact anchors but only two are allowed.
    assert excinfo.value.details["minimum_retained_points"] == 4
    assert excinfo.value.details["max_retained_points"] == 2
    assert "HT-UNIT" in excinfo.value.message


def test_plan_split_unit_reproduces_series_and_inherits_provenance() -> None:
    from app.planner import reconstruct_spans
    from app.split import SOURCE_RECONSTRUCTED, plan_split

    # Ramp 100,110,120,130,140 stored with two endpoint anchors.  The cut at
    # absolute minute 2 leaves the front run covering offsets 0..1 (two
    # collinear points) and the back run covering offsets 2..4.
    anchors = {0: 100, 4: 140}
    plan = plan_split(
        run_id="R",
        new_run_id="R-B",
        start_minute_utc=0,
        minute_count=5,
        spans=((0, 5),),
        anchors=anchors,
        anchor_sources=("measured", "measured"),
        max_retained=10,
        cut_minute_utc=2,
    )
    assert plan.front.readings == (100, 110)
    assert plan.back.readings == (120, 130, 140)
    # Front keeps the measured first anchor and gains a reconstructed endpoint
    # at its last minute; the back's first anchor (the cut minute) is
    # reconstructed, its final anchor is the original measured endpoint.
    assert plan.front.retained_points == ((0, 100), (1, 110))
    assert plan.front.retained_sources == ("measured", SOURCE_RECONSTRUCTED)
    assert plan.back.retained_points == ((0, 120), (2, 140))
    assert plan.back.retained_sources == (SOURCE_RECONSTRUCTED, "measured")
    # The two segments concatenate back to the parent reconstruction.
    parent_values = reconstruct_spans(((0, 5),), anchors, 5)
    assert list(plan.front.readings) + list(plan.back.readings) == parent_values


def test_split_write_failure_rolls_back_everything(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = _archive(client)

    def boom(self: db.ArchiveStore, **_: Any) -> tuple[Any, Any]:
        raise db.ArchiveWriteError("disk full (simulated)")

    monkeypatch.setattr(db.ArchiveStore, "replace_with_split", boom)
    response = _split(client, "HT-2026-0042", summary, 29_000_003, "HT-FAIL")
    assert response.status_code == 503
    body = response.json()
    assert body["reason_code"] == "archive_write_failed"
    assert body["retryable"] is True

    assert client.get("/api/v1/archives/HT-2026-0042").status_code == 200
    assert client.get("/api/v1/archives/HT-FAIL").status_code == 404


def test_split_then_both_runs_are_independently_queryable(client: TestClient) -> None:
    summary = _archive(client)
    split = _split(client, "HT-2026-0042", summary, 29_000_004, "HT-2026-0043")
    assert split.status_code == 200
    listing = client.get("/api/v1/archives").json()["archives"]
    ids = {item["run_id"] for item in listing}
    assert ids == {"HT-2026-0042", "HT-2026-0043"}

    # Each segment enforces its own range boundary on queries.
    out_of_range = client.get(
        "/api/v1/archives/HT-2026-0043/minutes",
        params={"start_minute_utc": 29_000_000, "end_minute_utc": 29_000_003},
    )
    assert out_of_range.status_code == 416
    front = client.get(
        "/api/v1/archives/HT-2026-0042/minutes",
        params={"start_minute_utc": 29_000_004, "end_minute_utc": 29_000_004},
    )
    assert front.status_code == 416


def test_split_archives_pass_integrity_checks(
    client: TestClient, store_path: str
) -> None:
    summary = _archive(client)
    split = _split(client, "HT-2026-0042", summary, 29_000_003, "HT-2026-0043")
    assert split.status_code == 200

    connection = sqlite3.connect(store_path)
    try:
        rows = connection.execute(
            "SELECT run_id, retained_points_json, retained_sources_json, "
            "planner_version FROM furnace_archives ORDER BY run_id"
        ).fetchall()
    finally:
        connection.close()
    assert [row[0] for row in rows] == ["HT-2026-0042", "HT-2026-0043"]
    for _run_id, points_json, sources_json, version in rows:
        points = json.loads(points_json)
        sources = json.loads(sources_json)
        assert len(points) == len(sources)
        assert set(sources) <= {"measured", "reconstructed"}
        assert version == "2.0.0"

    # Tampering with a split archive is still detected.
    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT retained_points_json FROM furnace_archives WHERE run_id = ?",
            ("HT-2026-0043",),
        ).fetchone()
        points = json.loads(row[0])
        points[0][1] += 1
        connection.execute(
            "UPDATE furnace_archives SET retained_points_json = ? WHERE run_id = ?",
            (json.dumps(points), "HT-2026-0043"),
        )
        connection.commit()
    finally:
        connection.close()
    tampered = client.get(
        "/api/v1/archives/HT-2026-0043/minutes",
        params={"start_minute_utc": 29_000_003, "end_minute_utc": 29_000_003},
    )
    assert tampered.status_code == 500
    assert tampered.json()["reason_code"] == "archive_corrupt"
    # The other segment stays readable.
    assert client.get("/api/v1/archives/HT-2026-0042").status_code == 200


def test_legacy_v1_database_migrates_and_splits(
    client: TestClient, store_path: str
) -> None:
    # Create a v1-shaped row by hand, exactly what the old service wrote.
    _archive(client, run_id="HT-LEGACY")
    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT * FROM furnace_archives WHERE run_id = 'HT-LEGACY'"
        ).fetchone()
        columns = [item[1] for item in connection.execute(
            "PRAGMA table_info(furnace_archives)"
        )]
        # Drop the v2 column and rewrite the row in its v1 form with the v1
        # digest (no provenance in the checksum input).
        connection.execute(
            "ALTER TABLE furnace_archives RENAME TO furnace_archives_v2"
        )
        connection.execute(
            "CREATE TABLE furnace_archives ("
            "run_id TEXT PRIMARY KEY, start_minute_utc INTEGER NOT NULL, "
            "minute_count INTEGER NOT NULL, band_low_tenths_c INTEGER NOT NULL, "
            "band_high_tenths_c INTEGER NOT NULL, tolerance_tenths_c INTEGER NOT NULL, "
            "max_retained_points INTEGER NOT NULL, retained_count INTEGER NOT NULL, "
            "retained_points_json TEXT NOT NULL, missing_ranges_json TEXT NOT NULL, "
            "payload_sha256 TEXT NOT NULL, planner_version TEXT NOT NULL, "
            "created_at_utc TEXT NOT NULL)"
        )
        data = dict(zip(columns, row))
        legacy_digest = db.payload_digest(
            run_id=data["run_id"],
            start_minute_utc=data["start_minute_utc"],
            minute_count=data["minute_count"],
            band_low=data["band_low_tenths_c"],
            band_high=data["band_high_tenths_c"],
            tolerance=data["tolerance_tenths_c"],
            max_retained=data["max_retained_points"],
            retained_points=json.loads(data["retained_points_json"]),
            missing_ranges=json.loads(data["missing_ranges_json"]),
            retained_sources=None,
            planner_version=db.LEGACY_PLANNER_VERSION,
        )
        connection.execute(
            "INSERT INTO furnace_archives VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                data["run_id"], data["start_minute_utc"], data["minute_count"],
                data["band_low_tenths_c"], data["band_high_tenths_c"],
                data["tolerance_tenths_c"], data["max_retained_points"],
                data["retained_count"], data["retained_points_json"],
                data["missing_ranges_json"], legacy_digest,
                db.LEGACY_PLANNER_VERSION, data["created_at_utc"],
            ),
        )
        connection.execute("DROP TABLE furnace_archives_v2")
        connection.commit()
    finally:
        connection.close()

    # Reopen via a fresh app so the migration runs again.
    app = create_app()
    with TestClient(app) as migrated_client:
        legacy = migrated_client.get("/api/v1/archives/HT-LEGACY")
        assert legacy.status_code == 200
        body = legacy.json()
        # Legacy anchors are all real measured samples.
        assert body["measured_anchor_count"] == body["retained_points_count"]
        assert body["reconstructed_anchor_count"] == 0

        minutes = migrated_client.get(
            "/api/v1/archives/HT-LEGACY/minutes",
            params={"start_minute_utc": 29_000_000, "end_minute_utc": 29_000_007},
        ).json()
        assert all(
            m["value_source"] == "measured" for m in minutes["minutes"] if m["retained"]
        )

        response = _split(
            migrated_client,
            "HT-LEGACY",
            body,
            cut=29_000_003,
            new_run_id="HT-LEGACY-2",
        )
        assert response.status_code == 200, response.text
        front = response.json()["front_archive"]
        assert front["planner_version"] == "2.0.0"
        # The original measured anchors keep their provenance after migration.
        assert front["measured_anchor_count"] >= 1


@pytest.mark.parametrize("seed", range(25))
def test_split_invariants_hold_on_random_archives(
    client: TestClient, seed: int
) -> None:
    # Property test: for any archivable random run and any valid cut, the two
    # split archives reproduce the original minutes exactly, keep every
    # missing flag and band state, and preserve measured/reconstructed
    # provenance through repeated splits.
    import random

    from app.planner import classify

    rng = random.Random(seed)
    n = rng.randint(4, 40)
    readings: list[Any] = []
    value = rng.randint(700, 850)
    for _ in range(n):
        if rng.random() < 0.15:
            readings.append(None)
            continue
        value += rng.randint(-20, 20)
        readings.append(value)
    if all(item is None for item in readings):
        readings[0] = 800
    low, high = 600, 1000
    summary = _archive(
        client,
        run_id=f"HT-FUZZ-{seed}",
        readings=readings,
        band_low_tenths_c=low,
        band_high_tenths_c=high,
        tolerance_tenths_c=rng.randint(0, 15),
        max_retained_points=n,
    )
    start = summary["start_minute_utc"]
    end = summary["end_minute_utc"]
    before = {
        m["minute_utc"]: m
        for m in _minutes(client, f"HT-FUZZ-{seed}", start, end)
    }

    measured_offsets = [
        start + i for i, item in enumerate(readings) if item is not None
    ]
    cut = rng.randint(1, len(measured_offsets) - 1)
    cut_minute = measured_offsets[cut]

    response = _split(
        client, f"HT-FUZZ-{seed}", summary, cut_minute, f"HT-FUZZ-{seed}-B"
    )
    assert response.status_code == 200, response.text

    front = _minutes(client, f"HT-FUZZ-{seed}", start, cut_minute - 1)
    back = _minutes(client, f"HT-FUZZ-{seed}-B", cut_minute, end)
    after = {m["minute_utc"]: m for m in front + back}
    assert set(after) == set(before)
    for minute, old in before.items():
        new = after[minute]
        assert new["value_tenths_c"] == old["value_tenths_c"]
        assert new["missing"] == old["missing"]
        assert new["band_state"] == old["band_state"]
        if not old["missing"]:
            assert classify(
                new["value_tenths_c"], low, high
            ) == classify(old["value_tenths_c"], low, high)
        # Every retained anchor carries a valid, explicit provenance tag;
        # non-retained measured minutes are interpolated, missing ones unknown.
        if new["retained"]:
            assert new["value_source"] in {"measured", "reconstructed"}
        elif new["missing"]:
            assert new["value_source"] == "unknown"
        else:
            assert new["value_source"] == "reconstructed"

    # Re-split the front run once more; provenance must still be consistent.
    front_summary = response.json()["front_archive"]
    if cut_minute - 1 > start:
        again = _split(
            client,
            f"HT-FUZZ-{seed}",
            front_summary,
            start + 1,
            f"HT-FUZZ-{seed}-C",
        )
        if again.status_code == 422:
            # A cut on a missing minute may leave one side without measured
            # minutes; that is the documented no-solution path, not an invariant
            # breach. The parent split archives must remain queryable.
            assert again.json()["reason_code"] == "no_feasible_split"
        else:
            assert again.status_code == 200, again.text
        re_front = _minutes(client, f"HT-FUZZ-{seed}", start, start)
        assert re_front[0]["value_tenths_c"] == before[start]["value_tenths_c"]
