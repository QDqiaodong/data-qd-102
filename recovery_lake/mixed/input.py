"""Read and validate the four files of the mixed-material v2 mode.

Every source file is read exactly once.  The bytes retained here are the bytes
that are hashed, copied into ODS and parsed; the parser therefore cannot see a
different revision of a file than the snapshot records.
"""
import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

MATERIAL_HEADERS = ["material_id", "material_name", "family"]
BATCH_HEADERS = ["batch_id", "site", "completed_at", "gross", "tare", "recovered", "residue", "unit"]
CHARGE_HEADERS = ["batch_id", "charge_id", "material_id", "net", "unit"]
STREAM_HEADERS = ["batch_id", "stream_id", "kind", "mass", "unit"]

SOURCE_NAMES = {
    "materials": "materials.csv",
    "batches": "batches.csv",
    "charges": "charges.csv",
    "streams": "streams.csv",
}

# A batch's net input, recovered and residue are each bounded by 10^12 g.
_MAX_MASS = 10**12


@dataclass(frozen=True)
class Source:
    key: str
    filename: str
    data: bytes
    sha256: str


@dataclass(frozen=True)
class Material:
    material_id: str
    material_name: str
    family: str
    source_line: int


@dataclass(frozen=True)
class Batch:
    batch_id: str
    site: str
    completed_at: datetime
    process_date: object
    gross_g: int
    tare_g: int
    recovered_g: int
    residue_g: int
    unit: str
    source_line: int

    @property
    def input_g(self):
        return self.gross_g - self.tare_g


@dataclass(frozen=True)
class Charge:
    batch_id: str
    charge_id: str
    material_id: str
    net_g: int
    unit: str
    source_line: int


@dataclass(frozen=True)
class Stream:
    batch_id: str
    stream_id: str
    kind: str
    mass_g: int
    unit: str
    source_line: int


@dataclass
class Dataset:
    materials: list
    batches: list
    charges: list
    streams: list
    sources: dict


def grams(value, unit):
    """Convert kg/t text to exact integer grams (the v1 precision/range rule)."""
    if unit not in {"kg", "t"}:
        raise ValueError("unsupported mass unit")
    if not re.fullmatch(r"\d{1,9}(?:\.\d{1,6})?", value):
        raise ValueError("invalid mass precision")
    try:
        mass = Decimal(value) * (1000 if unit == "kg" else 1000000)
    except InvalidOperation as exc:  # pragma: no cover - regex already excludes
        raise ValueError("invalid mass") from exc
    if mass != mass.to_integral_value() or not 0 <= mass <= _MAX_MASS:
        raise ValueError("mass must resolve to bounded integer grams")
    return int(mass)


def _error(filename, line, message):
    return ValueError(f"{filename}: record starting at line {line}: {message}")


def _identifier(filename, line, field, value):
    if value == "" or value != value.strip():
        raise _error(filename, line, f"{field} must be non-empty without surrounding whitespace")


def _parse(data, headers, filename):
    """Return (line_number, field list) pairs using the file's actual bytes."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{filename}: input is not valid UTF-8") from exc
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError(f"{filename}: empty or malformed CSV") from None
    if header != headers:
        raise ValueError(f"{filename}: unexpected CSV columns")
    rows = []
    previous_line = reader.line_num
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        start_line = previous_line + 1
        previous_line = reader.line_num
        if len(row) != len(headers) or any(value == "" for value in row):
            raise _error(filename, start_line, "empty or malformed CSV record")
        rows.append((start_line, row))
    if not rows:
        raise ValueError(f"{filename}: empty or malformed CSV")
    return rows


def _read(path, headers, key):
    data = Path(path).read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    rows = _parse(data, headers, SOURCE_NAMES[key])
    return Source(key, SOURCE_NAMES[key], data, sha256), rows


def _integer_mass(filename, line, value, unit, *, positive):
    try:
        mass = grams(value, unit)
    except ValueError as exc:
        raise _error(filename, line, str(exc)) from None
    if positive and mass <= 0:
        raise _error(filename, line, "mass must be greater than zero")
    return mass


def load(materials_path, batches_path, charges_path, streams_path):
    """Validate all four sources and return their typed records plus raw bytes.

    Any contract violation raises ValueError pointing at the file record or the
    failing batch; no partial dataset is returned in that case.
    """
    source_materials, material_rows = _read(materials_path, MATERIAL_HEADERS, "materials")
    source_batches, batch_rows = _read(batches_path, BATCH_HEADERS, "batches")
    source_charges, charge_rows = _read(charges_path, CHARGE_HEADERS, "charges")
    source_streams, stream_rows = _read(streams_path, STREAM_HEADERS, "streams")

    materials = []
    known_materials = set()
    for line, row in material_rows:
        material_id, material_name, family = row
        _identifier(source_materials.filename, line, "material_id", material_id)
        if material_id in known_materials:
            raise _error(source_materials.filename, line, f"duplicate material_id {material_id!r}")
        known_materials.add(material_id)
        materials.append(Material(material_id, material_name, family, line))

    batches = []
    known_batches = set()
    for line, row in batch_rows:
        batch_id, site, completed_at, gross, tare, recovered, residue, unit = row
        _identifier(source_batches.filename, line, "batch_id", batch_id)
        _identifier(source_batches.filename, line, "site", site)
        if batch_id in known_batches:
            raise _error(source_batches.filename, line, f"duplicate batch_id {batch_id!r}")
        known_batches.add(batch_id)
        try:
            stamp = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        except ValueError:
            raise _error(source_batches.filename, line, "completed_at is not an ISO timestamp") from None
        if stamp.tzinfo is None:
            raise _error(source_batches.filename, line, "completed_at requires a timezone")
        utc_stamp = stamp.astimezone(timezone.utc)
        gross_g = _integer_mass(source_batches.filename, line, gross, unit, positive=False)
        tare_g = _integer_mass(source_batches.filename, line, tare, unit, positive=False)
        recovered_g = _integer_mass(source_batches.filename, line, recovered, unit, positive=False)
        residue_g = _integer_mass(source_batches.filename, line, residue, unit, positive=False)
        if gross_g <= tare_g:
            raise _error(source_batches.filename, line, "gross must be greater than tare")
        if recovered_g + residue_g != gross_g - tare_g:
            raise _error(source_batches.filename, line, "recovered + residue must equal gross - tare")
        batches.append(Batch(
            batch_id, site, utc_stamp, utc_stamp.date(),
            gross_g, tare_g, recovered_g, residue_g, unit, line,
        ))

    charges = []
    known_charges = set()
    for line, row in charge_rows:
        batch_id, charge_id, material_id, net, unit = row
        filename = source_charges.filename
        _identifier(filename, line, "batch_id", batch_id)
        _identifier(filename, line, "charge_id", charge_id)
        _identifier(filename, line, "material_id", material_id)
        key = (batch_id, charge_id)
        if key in known_charges:
            raise _error(filename, line, f"duplicate charge_id {charge_id!r} in batch {batch_id!r}")
        known_charges.add(key)
        if batch_id not in known_batches:
            raise _error(filename, line, f"unknown batch_id {batch_id!r}")
        if material_id not in known_materials:
            raise _error(filename, line, f"unknown material_id {material_id!r}")
        net_g = _integer_mass(filename, line, net, unit, positive=True)
        charges.append(Charge(batch_id, charge_id, material_id, net_g, unit, line))

    streams = []
    known_streams = set()
    for line, row in stream_rows:
        batch_id, stream_id, kind, mass, unit = row
        filename = source_streams.filename
        _identifier(filename, line, "batch_id", batch_id)
        _identifier(filename, line, "stream_id", stream_id)
        if kind not in {"recovered", "residue"}:
            raise _error(filename, line, "stream kind must be recovered or residue")
        key = (batch_id, stream_id)
        if key in known_streams:
            raise _error(filename, line, f"duplicate stream_id {stream_id!r} in batch {batch_id!r}")
        known_streams.add(key)
        if batch_id not in known_batches:
            raise _error(filename, line, f"unknown batch_id {batch_id!r}")
        mass_g = _integer_mass(filename, line, mass, unit, positive=True)
        streams.append(Stream(batch_id, stream_id, kind, mass_g, unit, line))

    charges_by_batch = {batch.batch_id: [] for batch in batches}
    for charge in charges:
        charges_by_batch[charge.batch_id].append(charge)
    streams_by_batch = {batch.batch_id: [] for batch in batches}
    for stream in streams:
        streams_by_batch[stream.batch_id].append(stream)

    for batch in batches:
        batch_charges = charges_by_batch[batch.batch_id]
        batch_streams = streams_by_batch[batch.batch_id]
        if not batch_charges:
            raise ValueError(f"batch {batch.batch_id!r}: at least one charge is required")
        if not batch_streams:
            raise ValueError(f"batch {batch.batch_id!r}: at least one stream is required")
        if sum(charge.net_g for charge in batch_charges) != batch.input_g:
            raise ValueError(
                f"batch {batch.batch_id!r}: charge total must equal net input {batch.input_g}"
            )
        recovered = [s for s in batch_streams if s.kind == "recovered"]
        residue = [s for s in batch_streams if s.kind == "residue"]
        if sum(s.mass_g for s in recovered) != batch.recovered_g:
            raise ValueError(
                f"batch {batch.batch_id!r}: recovered stream total must equal batch recovered"
            )
        if sum(s.mass_g for s in residue) != batch.residue_g:
            raise ValueError(
                f"batch {batch.batch_id!r}: residue stream total must equal batch residue"
            )

    sources = {
        source.key: source
        for source in (source_materials, source_batches, source_charges, source_streams)
    }
    return Dataset(materials, batches, charges, streams, sources)
