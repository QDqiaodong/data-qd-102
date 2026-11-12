CREATE TABLE dwd_batch AS
SELECT b.batch_id,b.site,CAST(b.completed_at AS DATE) AS process_date,
 b.material_id,m.material_name,m.family,b.gross_g-b.tare_g AS input_g,
 b.recovered_g,b.residue_g
FROM ods_batches b JOIN dim_material m USING(material_id);

CREATE TABLE ads_daily_recovery AS
SELECT site,process_date,family,count(*) AS batches,
 sum(input_g) AS input_g,sum(recovered_g) AS recovered_g,sum(residue_g) AS residue_g,
 CAST(sum(recovered_g) AS DOUBLE)/sum(input_g) AS recovery_rate
FROM dwd_batch GROUP BY site,process_date,family;
