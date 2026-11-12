CREATE TABLE dim_material AS
SELECT material_id,material_name,family FROM ods_materials;

CREATE TABLE dwd_process AS
SELECT b.batch_id,b.site,b.completed_at,CAST(b.completed_at AS DATE) AS process_date,
 b.gross_g,b.tare_g,CAST(b.gross_g-b.tare_g AS BIGINT) AS input_g,
 b.recovered_g,b.residue_g,b.unit,b.source_sha256,b.source_line
FROM ods_batches b;

CREATE TABLE dwd_charge AS
SELECT c.batch_id,c.charge_id,c.material_id,c.net_g,c.unit,c.source_sha256,c.source_line
FROM ods_charges c;

CREATE TABLE dwd_stream AS
SELECT s.batch_id,s.stream_id,s.kind,s.mass_g,s.unit,s.source_sha256,s.source_line
FROM ods_streams s;

CREATE TABLE dwd_material_stream AS
SELECT a.batch_id,a.material_id,a.stream_id,s.kind AS stream_kind,d.family,
 a.base_g,a.remainder_num,a.denominator_g,CAST(a.rounded_up AS BOOLEAN) AS rounded_up,
 a.allocated_g
FROM alloc_cells a
JOIN ods_streams s USING(batch_id,stream_id)
JOIN dim_material d USING(material_id);

CREATE TABLE dwd_batch_material AS
WITH charged AS (
 SELECT batch_id,material_id,sum(net_g) AS input_g
 FROM ods_charges GROUP BY batch_id,material_id
), allocated AS (
 SELECT a.batch_id,a.material_id,
  sum(CASE WHEN s.kind='recovered' THEN a.allocated_g ELSE 0 END) AS recovered_g,
  sum(CASE WHEN s.kind='residue' THEN a.allocated_g ELSE 0 END) AS residue_g
 FROM alloc_cells a JOIN ods_streams s USING(batch_id,stream_id)
 GROUP BY a.batch_id,a.material_id
)
SELECT c.batch_id,c.material_id,d.material_name,d.family,
 c.input_g,CAST(a.recovered_g AS BIGINT) AS recovered_g,
 CAST(a.residue_g AS BIGINT) AS residue_g
FROM charged c
JOIN allocated a USING(batch_id,material_id)
JOIN dim_material d USING(material_id);

CREATE TABLE ads_daily_recovery AS
SELECT p.site,p.process_date,b.family,
 count(DISTINCT b.batch_id) AS batches,
 CAST(sum(b.input_g) AS BIGINT) AS input_g,
 CAST(sum(b.recovered_g) AS BIGINT) AS recovered_g,
 CAST(sum(b.residue_g) AS BIGINT) AS residue_g,
 CAST(sum(b.recovered_g) AS DOUBLE)/sum(b.input_g) AS recovery_rate
FROM dwd_batch_material b JOIN dwd_process p USING(batch_id)
GROUP BY p.site,p.process_date,b.family;

-- Field meanings for the v2 facts.
COMMENT ON TABLE dim_material IS
 'Material dimension shared with v1; family influences ADS aggregation only.';
COMMENT ON TABLE dwd_process IS
 'One row per processing batch; masses are weighed batch totals in grams.';
COMMENT ON TABLE dwd_charge IS
 'One row per charged material line after container tare; source_sha256/source_line locate the ODS record.';
COMMENT ON TABLE dwd_stream IS
 'One row per weighed recovered/residue outlet stream; source_sha256/source_line locate the ODS record.';
COMMENT ON TABLE dwd_material_stream IS
 'One row per batch/material/stream cell, including zero-allocation cells. allocated_g is a proportional accounting allocation (base_g + rounded_up), not measured material fate. remainder_num/denominator_g allow recomputing floor(a_i*b_j/N) and the rounding.';
COMMENT ON TABLE dwd_batch_material IS
 'One row per batch/material: charged input, plus allocated recovered and residue grams. Values are proportional accounting results, not measured material fate.';
COMMENT ON TABLE ads_daily_recovery IS
 'site/UTC process_date/family aggregate. input_g, recovered_g, residue_g are sums; recovery_rate = SUM(recovered_g)/SUM(input_g). batches counts distinct batches, not material rows.';
COMMENT ON COLUMN dwd_material_stream.base_g IS 'floor(a_i*b_j/N) in grams, exact integer.';
COMMENT ON COLUMN dwd_material_stream.remainder_num IS '(a_i*b_j) mod N; zero means rounded_up must be false.';
COMMENT ON COLUMN dwd_material_stream.denominator_g IS 'N = batch net input in grams.';
COMMENT ON COLUMN dwd_material_stream.rounded_up IS 'Whether one gram was added above base_g.';
COMMENT ON COLUMN dwd_material_stream.allocated_g IS 'base_g + rounded_up; proportional accounting amount, not measured material fate.';
COMMENT ON COLUMN dwd_charge.source_line IS '1-based start line of this record in ods/charges.csv (header is line 1).';
COMMENT ON COLUMN dwd_stream.source_line IS '1-based start line of this record in ods/streams.csv (header is line 1).';
