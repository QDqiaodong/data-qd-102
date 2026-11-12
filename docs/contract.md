# Recovery warehouse v1

Inputs: exact CSV headers in data/. Material IDs are unique; one processing batch
has one material_id and positive net input. Fields gross/tare/recovered/residue
are decimal kg or t; <=6 fractional digits, resolve exactly to integer grams,
0..10^12 g. Gross > tare and recovered + residue == gross - tare are mandatory.
There is no shrinkage allowance, pooled batch allocation or correction ledger.
Each batch ID occurs once. Unknown materials, missing fields, invalid dates,
timezone-free timestamps and imbalance reject the full build.

Fact grain: one processing batch. process_date is UTC completion date. Dimension
material includes name/family. DWD retains input, recovered and residue grams;
ADS groups site/date/family. Recovery rate is SUM(recovered)/SUM(input), not the
unweighted mean of batch rates. Output mass is integer grams; rate is double.
This is an engineering metric, not a regulatory recycling certification.

ODS keeps original CSV bytes and their SHA256; DWD/ADS write typed Parquet plus
self-contained recovery.duckdb SQL tables. Manifest records schema and totals.
A fresh output directory is required; existing snapshots are never overwritten.
Single writer full-batch build, temporary sibling build then directory rename.
No partition/incremental merge, schema evolution or concurrent publisher protocol.
