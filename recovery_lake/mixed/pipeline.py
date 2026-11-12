"""Build a v2 mixed-material ODS/DWD/ADS snapshot.

A mixed batch pools charges of several materials and weighs its output as
several recovered/residue streams without measuring per-material outlet mass.
The material x stream cells produced here are a proportional accounting
allocation, not measured material fate.  Family is applied only in the ADS
aggregation and never feeds back into the allocation.
"""
import json
import tempfile
from collections import defaultdict
from importlib.resources import files
from pathlib import Path

import duckdb

from .allocation import allocate_batch
from .input import SOURCE_NAMES, load


def sql_path(path):
    return "'" + str(path).replace("'", "''") + "'"


def export_parquet(db, query, path):
    """Write one DWD/ADS query to a typed Parquet file (test injection point)."""
    db.execute(f"COPY ({query}) TO {sql_path(path)} (FORMAT PARQUET,COMPRESSION ZSTD)")


def write_manifest(path, manifest):
    """Write the manifest last among the file outputs (test injection point)."""
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _allocate(dataset):
    """Return one row per batch/material/stream cell for every batch."""
    charges_by_batch = defaultdict(lambda: defaultdict(int))
    for charge in dataset.charges:
        charges_by_batch[charge.batch_id][charge.material_id] += charge.net_g
    streams_by_batch = defaultdict(list)
    for stream in dataset.streams:
        streams_by_batch[stream.batch_id].append((stream.stream_id, stream.mass_g))

    cells = []
    for batch in dataset.batches:
        # Aggregate same-material charges; never allocate charge rows directly.
        materials = sorted(charges_by_batch[batch.batch_id].items())
        streams = sorted(streams_by_batch[batch.batch_id])
        allocated, _ = allocate_batch(materials, streams)
        for cell in allocated:
            cells.append((
                batch.batch_id, cell.material_id, cell.stream_id,
                cell.base, cell.remainder, cell.denominator, cell.rounded_up, cell.allocated,
            ))
    return cells


def _summary(db, cell_count):
    totals = db.execute(
        "SELECT count(*),sum(input_g),sum(recovered_g),sum(residue_g) FROM dwd_process"
    ).fetchone()
    daily = db.execute(
        "SELECT site,CAST(process_date AS VARCHAR),family,batches,"
        "CAST(input_g AS BIGINT),CAST(recovered_g AS BIGINT),CAST(residue_g AS BIGINT),"
        "recovery_rate FROM ads_daily_recovery ORDER BY site,process_date,family"
    ).fetchall()
    return {
        "batch_count": totals[0],
        "material_count": db.execute("SELECT count(*) FROM dim_material").fetchone()[0],
        "charge_count": db.execute("SELECT count(*) FROM dwd_charge").fetchone()[0],
        "stream_count": db.execute("SELECT count(*) FROM dwd_stream").fetchone()[0],
        "cell_count": cell_count,
        "input_g": totals[1],
        "recovered_g": totals[2],
        "residue_g": totals[3],
        "daily": [
            dict(zip(
                ["site", "date", "family", "batches", "input_g", "recovered_g",
                 "residue_g", "recovery_rate"], row,
            ))
            for row in daily
        ],
    }


def build(materials_path, batches_path, charges_path, streams_path, output):
    """Build a new v2 snapshot directory and return its business summary.

    All four inputs are each read once; those bytes are hashed, stored in ODS
    and parsed.  Nothing is published until the database is closed and the
    Parquet, ODS and manifest files are complete.  An existing target is never
    overwritten, and failures remove this run's staging directory.
    """
    output = Path(output)
    if output.exists():
        raise FileExistsError("output snapshot exists")
    dataset = load(materials_path, batches_path, charges_path, streams_path)
    cells = _allocate(dataset)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".recovery-mixed-", dir=output.parent) as temporary:
        staging = Path(temporary) / "snapshot"
        for layer in ["ods", "dwd", "ads"]:
            (staging / layer).mkdir(parents=True, exist_ok=True)

        # ODS is written from the exact bytes that were hashed and parsed.
        for key, source in dataset.sources.items():
            (staging / "ods" / SOURCE_NAMES[key]).write_bytes(source.data)

        with duckdb.connect(str(staging / "recovery.duckdb")) as db:
            db.execute("SET TimeZone='UTC'")
            db.execute(
                "CREATE TABLE ods_materials("
                "material_id VARCHAR,material_name VARCHAR,family VARCHAR,"
                "source_sha256 VARCHAR,source_line BIGINT)"
            )
            db.executemany(
                "INSERT INTO ods_materials VALUES (?,?,?,?,?)",
                [(m.material_id, m.material_name, m.family,
                  dataset.sources["materials"].sha256, m.source_line)
                 for m in dataset.materials],
            )
            db.execute(
                "CREATE TABLE ods_batches(batch_id VARCHAR,site VARCHAR,"
                "completed_at TIMESTAMPTZ,gross_g BIGINT,tare_g BIGINT,"
                "recovered_g BIGINT,residue_g BIGINT,unit VARCHAR,"
                "source_sha256 VARCHAR,source_line BIGINT)"
            )
            db.executemany(
                "INSERT INTO ods_batches VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(b.batch_id, b.site, b.completed_at, b.gross_g, b.tare_g,
                  b.recovered_g, b.residue_g, b.unit,
                  dataset.sources["batches"].sha256, b.source_line)
                 for b in dataset.batches],
            )
            db.execute(
                "CREATE TABLE ods_charges(batch_id VARCHAR,charge_id VARCHAR,"
                "material_id VARCHAR,net_g BIGINT,unit VARCHAR,"
                "source_sha256 VARCHAR,source_line BIGINT)"
            )
            db.executemany(
                "INSERT INTO ods_charges VALUES (?,?,?,?,?,?,?)",
                [(c.batch_id, c.charge_id, c.material_id, c.net_g, c.unit,
                  dataset.sources["charges"].sha256, c.source_line)
                 for c in dataset.charges],
            )
            db.execute(
                "CREATE TABLE ods_streams(batch_id VARCHAR,stream_id VARCHAR,"
                "kind VARCHAR,mass_g BIGINT,unit VARCHAR,"
                "source_sha256 VARCHAR,source_line BIGINT)"
            )
            db.executemany(
                "INSERT INTO ods_streams VALUES (?,?,?,?,?,?,?)",
                [(s.batch_id, s.stream_id, s.kind, s.mass_g, s.unit,
                  dataset.sources["streams"].sha256, s.source_line)
                 for s in dataset.streams],
            )
            db.execute(
                "CREATE TABLE alloc_cells(batch_id VARCHAR,material_id VARCHAR,"
                "stream_id VARCHAR,base_g BIGINT,remainder_num BIGINT,"
                "denominator_g BIGINT,rounded_up TINYINT,allocated_g BIGINT)"
            )
            db.executemany(
                "INSERT INTO alloc_cells VALUES (?,?,?,?,?,?,?,?)", cells,
            )
            db.execute(files("recovery_lake").joinpath("model_v2.sql").read_text())

            exports = [
                ("dim_material", "dwd", "SELECT * FROM dim_material ORDER BY material_id"),
                ("dwd_process", "dwd", "SELECT * FROM dwd_process ORDER BY batch_id"),
                ("dwd_charge", "dwd",
                 "SELECT * FROM dwd_charge ORDER BY batch_id,charge_id"),
                ("dwd_stream", "dwd",
                 "SELECT * FROM dwd_stream ORDER BY batch_id,stream_id"),
                ("dwd_material_stream", "dwd",
                 "SELECT * FROM dwd_material_stream "
                 "ORDER BY batch_id,material_id,stream_id"),
                ("dwd_batch_material", "dwd",
                 "SELECT * FROM dwd_batch_material ORDER BY batch_id,material_id"),
                ("ads_daily_recovery", "ads",
                 "SELECT * FROM ads_daily_recovery ORDER BY site,process_date,family"),
            ]
            for name, layer, query in exports:
                export_parquet(db, query, staging / layer / (name + ".parquet"))
            summary = _summary(db, len(cells))

        manifest = {
            "schema_version": 2,
            "mode": "mixed",
            "mass_unit": "g",
            "accounting_timezone": "UTC",
            "allocation": {
                "cell_mass": "floor(a_i*b_j/N) plus a 0/1 rounding gram",
                "rounding_rule": (
                    "maximize sum((a_i*b_j)%N * rounded_up); ties choose the "
                    "lexicographically largest rounded_up vector ordered by "
                    "(material_id,stream_id) Unicode code point; exact integers"
                ),
                "interpretation": (
                    "proportional accounting allocation, not measured material fate"
                ),
            },
            "sources": {
                key: {
                    "path": f"ods/{SOURCE_NAMES[key]}",
                    "sha256": dataset.sources[key].sha256,
                }
                for key in ("materials", "batches", "charges", "streams")
            },
            "summary": summary,
        }
        write_manifest(staging / "manifest.json", manifest)
        staging.rename(output)
    return summary
