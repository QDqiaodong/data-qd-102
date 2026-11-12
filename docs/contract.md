# Recovery warehouse contract

## v1 — one material per batch (unchanged)

`python -m recovery_lake --output <dir>`; `recovery_lake.pipeline.build(materials,
batches, output)`.

Inputs: exact CSV headers in `data/`. Material IDs are unique; one processing
batch has one material_id and positive net input. Fields gross/tare/recovered/
residue are decimal kg or t; <=6 fractional digits, resolve exactly to integer
grams, 0..10^12 g. Gross > tare and recovered + residue == gross - tare are
mandatory. There is no shrinkage allowance, pooled batch allocation or
correction ledger. Each batch ID occurs once. Unknown materials, missing
fields, invalid dates, timezone-free timestamps and imbalance reject the full
build.

Fact grain: one processing batch. process_date is UTC completion date.
Dimension material includes name/family. DWD retains input, recovered and
residue grams; ADS groups site/date/family. Recovery rate is
SUM(recovered)/SUM(input), not the unweighted mean of batch rates. Output mass
is integer grams; rate is double. This is an engineering metric, not a
regulatory recycling certification.

ODS keeps original CSV bytes and their SHA256; DWD/ADS write typed Parquet plus
self-contained recovery.duckdb SQL tables. Manifest records schema and totals
(schema_version 1). A fresh output directory is required; existing snapshots
are never overwritten. Single writer full-batch build, temporary sibling build
then directory rename. No partition/incremental merge, schema evolution or
concurrent publisher protocol.

## v2 — mixed charges and streams (`python -m recovery_lake.mixed`)

`recovery_lake.mixed.build(materials, batches, charges, streams, output)` exposes
the same build to Python. Headers must match exactly, in this order:

| file | header |
| --- | --- |
| materials | `material_id,material_name,family` |
| batches | `batch_id,site,completed_at,gross,tare,recovered,residue,unit` |
| charges | `batch_id,charge_id,material_id,net,unit` |
| streams | `batch_id,stream_id,kind,mass,unit` |

### Input contract

- Four UTF-8 CSVs; header is line 1. Each source is read once; those exact
  bytes are parsed, hashed and copied to ODS (no second read).
- Batch IDs are unique. `charge_id` is unique within a batch; `stream_id` is
  unique within a batch. Duplicate keys are rejected even with identical
  content. Material IDs are unique in the dimension.
- Identifiers (batch_id, charge_id, stream_id, material_id) and site must be
  non-empty, without leading/trailing whitespace, and are case-sensitive
  verbatim strings. `kind` is exactly `recovered` or `residue`.
- Every batch has at least one charge and at least one stream. Every charge/
  stream batch and material reference must exist; orphan rows are rejected.
- At least one material may be charged in several charges; same-material
  charges are pooled per batch.
- Charge `net` and stream `mass` must be > 0. A batch's `recovered` or
  `residue` may be 0, in which case that kind has no streams.
- Mass text/unit precision, integer-gram resolution and the 0..10^12 g bound
  follow v1. Batch `gross`, `tare`, `recovered`, `residue` use v1 rules:
  gross > tare, recovered + residue == gross - tare, and net input > 0.
- Per batch: SUM(charge net) == gross - tare; SUM(recovered streams) ==
  recovered; SUM(residue streams) == residue.
- `completed_at` must be an ISO timestamp with a timezone. `process_date` is
  its UTC date.
- Any violation rejects the whole build with a non-zero exit and no snapshot.
  Errors name the file plus the record's 1-based start line, or the failing
  batch ID.

### Allocation contract

Aggregate charges by `material_id` to `a_i` grams, each stream to `b_j` grams,
with `N = SUM(a_i) = SUM(b_j)`. Do not allocate charge rows first and do not
pool by family. For every cell:

1. `base_ij = (a_i*b_j)//N`, `r_ij = (a_i*b_j)%N`; final
   `x_ij = base_ij + u_ij`, `u_ij` in {0,1} and `u_ij = 0` when `r_ij = 0`.
2. For every material SUM_j x_ij == a_i; for every stream SUM_i x_ij == b_j.
   Rounding is never done per column alone or by dumping error into one cell.
3. Among all feasible completions, maximise SUM(r_ij*u_ij). On ties, order
   cells by (material_id, stream_id) Unicode code point and choose the
   lexicographically largest u vector (round earlier cells up first). CSV
   order and database scan order are not used.
4. All arithmetic is exact integer arithmetic; intermediate products are not
   limited to 64 bits and never converted to float. The production algorithm
   must be polynomial in the cell matrix, not per gram or exhaustive over 0/1
   matrices.

**Algorithm.** Since `base` is below both margins, row/column rounding
deficits `R_i` and `C_j` are integral and equal in total. On cells with
`r_ij > 0`, the instance is a bipartite b-matching: source->material capacity
`R_i`, material->stream cell capacity 1, stream->sink capacity `C_j`. Integral
feasibility follows from total unimodularity of the capped transportation
polytope (the fractional values `r_ij/N` are a feasible point), so zero
remainders never need to round up. A single min-cost max-flow gives the
answer: cell cost weight is `r_ij*2^k + 2^(k-1-rank)` with `k` cells and `rank`
the cell's position in code-point order. The binary bonus is at most
`2^k-1`, so any one-point primary loss outweighs all bonuses; this encodes
maximise-sum-remainders then lexicographic u exactly. Successive shortest
augmenting paths with integer potentials is polynomial in cells, uses Python
big integers throughout, and raises rather than publishing if margins fail a
post-check. `recovery_lake/mixed/allocation.py` contains this solver and its
rationale.

These cell values are **proportional accounting allocations, not measured
material fate**. Family only affects ADS aggregation and never feeds back into
material allocation.

### v2 fact grain

One row per grain below; all DWD tables are exported as same-name Parquet and
as physical tables in `recovery.duckdb` (self-contained, no dependency on the
original input paths). Table/column comments document field meaning, including
the accounting-vs-measured distinction.

| table | grain | key fields |
| --- | --- | --- |
| `dim_material` | material | material_id, material_name, family |
| `dwd_process` | processing batch | batch totals, process_date (UTC), source_sha256/source_line |
| `dwd_charge` | charge line | batch_id, charge_id, material_id, net_g, source_sha256/source_line |
| `dwd_stream` | weighed stream | batch_id, stream_id, kind, mass_g, source_sha256/source_line |
| `dwd_material_stream` | batch/material/stream cell, including zeros | base_g, remainder_num, denominator_g, rounded_up, allocated_g |
| `dwd_batch_material` | batch/material | input_g, recovered_g, residue_g |

`source_sha256`/`source_line` on every charge, stream and process row point to
`ods/charges.csv`, `ods/streams.csv` and `ods/batches.csv` (header = line 1).
Joining on business keys therefore traces a material allocation back to its
charges and to the weighed outlet streams, and the digest identifies the exact
source file revision.

`ads_daily_recovery` keeps v1 field meanings at site/UTC process_date/family:
`input_g`, `recovered_g`, `residue_g` are sums over `dwd_batch_material`;
`recovery_rate = SUM(recovered_g)/SUM(input_g)`; `batches` counts distinct
batches in that family (two materials of one family in one batch count once).
Material×stream rows are never re-summed into input; material or batch rates
are never averaged. CLI summary and manifest totals use this correct grain.

### v2 snapshot layout and publication

```
<output>/
  ods/materials.csv  ods/batches.csv  ods/charges.csv  ods/streams.csv
  dwd/dim_material.parquet  dwd/dwd_process.parquet  dwd/dwd_charge.parquet
  dwd/dwd_stream.parquet  dwd/dwd_material_stream.parquet
  dwd/dwd_batch_material.parquet
  ads/ads_daily_recovery.parquet
  recovery.duckdb
  manifest.json
```

Manifest: `schema_version=2`, `mode=mixed`, mass unit, UTC accounting
timezone, allocation rule description and accounting interpretation, the four
source relative ODS paths with SHA256, and business summary. It records no
build time and no machine-absolute path, so moving the snapshot keeps it
queryable and verifiable; ODS digests can be rechecked from the relocated
directory.

Build uses a temporary sibling directory. The formal target appears only after
the database is closed and Parquet, ODS and manifest are all complete. Existing
targets are refused. Validation, calculation, SQL, export or pre-publish write
failures exit non-zero, publish nothing and remove the run's staging directory;
a corrected rerun to a new directory succeeds. Two different new directories
from identical input have identical logical tables and identical manifest
content (DuckDB/Parquet binary bytes need not match). Single writer, full
snapshot only.
