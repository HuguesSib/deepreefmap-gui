"""Durable machine-local performance observations and per-registry delivery state."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path

import platformdirs

NAMESPACE = uuid.UUID("f41de56b-4ce7-41c2-b1d8-cbd65942d967")


def journal_path() -> Path:
    """Return the global journal path, independent of any survey output root."""
    override = os.environ.get("DEEPREEFMAP_PERFORMANCE_JOURNAL")
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir("deepreefmap", appauthor=False)) / "performance.sqlite3"


def _connect() -> sqlite3.Connection:
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS observation (
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            recorded_at TEXT
        );
        CREATE TABLE IF NOT EXISTS delivery (
            server TEXT NOT NULL,
            device_id TEXT NOT NULL,
            observation_id TEXT NOT NULL REFERENCES observation(id) ON DELETE CASCADE,
            payload_hash TEXT NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (server, device_id, observation_id)
        );
        CREATE TABLE IF NOT EXISTS import_source (
            path TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL
        );
        """
    )
    return connection


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def store(payload: dict) -> str:
    """Insert one immutable observation, returning its stable ID."""
    observation_id = str(payload["id"])
    document = _canonical(payload)
    digest = hashlib.sha256(document.encode()).hexdigest()
    with _connect() as connection:
        existing = connection.execute(
            "SELECT payload FROM observation WHERE id = ?", (observation_id,)
        ).fetchone()
        if existing:
            retained = json.loads(existing[0])
            measured = ("observation", "stage_peaks")
            if any(retained.get(key) != payload.get(key) for key in measured):
                raise ValueError(
                    f"Performance observation {observation_id} has conflicting measurements"
                )
            if retained.get("run_id") or not payload.get("run_id"):
                return observation_id
            connection.execute(
                "UPDATE observation SET payload = ?, payload_hash = ?, recorded_at = ? WHERE id = ?",
                (document, digest, payload["observation"].get("recorded_at"), observation_id),
            )
            return observation_id
        connection.execute(
            "INSERT INTO observation(id, payload, payload_hash, recorded_at) VALUES (?, ?, ?, ?)",
            (observation_id, document, digest, payload["observation"].get("recorded_at")),
        )
    return observation_id


def observations() -> list[dict]:
    """Return every retained observation, newest dated entries first."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT payload FROM observation ORDER BY recorded_at DESC NULLS LAST, rowid"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def pending(server: str, device_id: str, limit: int = 100) -> list[dict]:
    """Return observations not acknowledged by this registry and device."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT o.payload FROM observation o
            LEFT JOIN delivery d ON d.observation_id = o.id AND d.server = ? AND d.device_id = ?
            WHERE d.observation_id IS NULL OR d.payload_hash != o.payload_hash
            ORDER BY o.recorded_at NULLS FIRST, o.rowid LIMIT ?
            """,
            (server.rstrip("/"), device_id, limit),
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


def acknowledge(server: str, device_id: str, observation_ids: list[str]) -> None:
    """Remember which immutable payloads a registry accepted."""
    if not observation_ids:
        return
    with _connect() as connection:
        for observation_id in observation_ids:
            connection.execute(
                """
                INSERT INTO delivery(server, device_id, observation_id, payload_hash)
                SELECT ?, ?, id, payload_hash FROM observation WHERE id = ?
                ON CONFLICT(server, device_id, observation_id)
                DO UPDATE SET payload_hash = excluded.payload_hash, delivered_at = CURRENT_TIMESTAMP
                """,
                (server.rstrip("/"), device_id, observation_id),
            )


def _legacy_payload(entry: dict, source: str, group: str, index: int) -> dict | None:
    meta = entry.get("performance_observation")
    peaks = entry.get("stage_peaks") or {}
    if not meta and not peaks:
        return None
    if meta:
        observation_id = str(meta["id"])
        source_kind = "device"
    else:
        fingerprint = _canonical({"source": source, "group": group, "index": index, "entry": entry})
        observation_id = str(uuid.uuid5(NAMESPACE, fingerprint))
        profile = entry.get("system_profile") or {}
        gpu = profile.get("gpu") or {}
        meta = {
            "version": 0,
            "id": observation_id,
            "settings": entry.get("params") or {},
            "hardware": {
                **{
                    key: profile.get(key)
                    for key in ("cpu_logical", "cpu_physical", "total_ram_bytes", "total_swap_bytes")
                },
                "gpu_name": gpu.get("name"),
                "gpu_kind": gpu.get("kind"),
                "total_vram_bytes": gpu.get("total_vram_bytes"),
            },
            "basis": "process" if entry.get("version", 1) >= 2 else "machine",
            "frames": entry.get("frames"),
            "timing_complete": False,
            "status": "completed",
            "duration_s": entry.get("run_seconds"),
            "recorded_at": None,
        }
        source_kind = "legacy"
    return {
        "id": observation_id,
        "run_id": None,
        "observation": meta,
        "stage_peaks": peaks,
        "source": source_kind,
    }


def import_legacy(timings_path: Path) -> None:
    """Import changed JSON history files once without collapsing repeated runs."""
    for path in (timings_path, timings_path.with_name("performance_runs.json")):
        if not path.exists():
            continue
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        with _connect() as connection:
            seen = connection.execute(
                "SELECT content_hash FROM import_source WHERE path = ?", (str(path),)
            ).fetchone()
        if seen:
            continue
        document = json.loads(raw)
        for group, entries in document.items():
            for index, entry in enumerate(entries):
                payload = _legacy_payload(entry, path.name, group, index)
                if payload is not None:
                    store(payload)
        with _connect() as connection:
            connection.execute(
                """INSERT INTO import_source(path, content_hash) VALUES (?, ?)
                ON CONFLICT(path) DO UPDATE SET content_hash = excluded.content_hash""",
                (str(path), digest),
            )
