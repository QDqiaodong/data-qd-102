"""Build an ODS/DWD/ADS snapshot with a material dimension and lineage."""
import hashlib
import json
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path
import duckdb
from .input import load


def sql_path(path):return "'"+str(path).replace("'","''")+"'"


def build(materials_path,batches_path,output):
    output=Path(output)
    if output.exists():raise FileExistsError("output snapshot exists")
    materials,batches=load(materials_path,batches_path)
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".recovery-",dir=output.parent) as temporary:
        staging=Path(temporary)/"snapshot"
        for layer in ["ods","dwd","ads"]:(staging/layer).mkdir(parents=True,exist_ok=True)
        for path,name in [(materials_path,"materials.csv"),(batches_path,"batches.csv")]:
            shutil.copyfile(path,staging/"ods"/name)
        with duckdb.connect(str(staging/"recovery.duckdb")) as db:
            db.execute("SET TimeZone='UTC'")
            db.execute("CREATE TABLE dim_material(material_id VARCHAR PRIMARY KEY,material_name VARCHAR,family VARCHAR)")
            db.executemany("INSERT INTO dim_material VALUES (?,?,?)",materials)
            db.execute("CREATE TABLE ods_batches(batch_id VARCHAR PRIMARY KEY,site VARCHAR,completed_at TIMESTAMPTZ,material_id VARCHAR,gross_g BIGINT,tare_g BIGINT,recovered_g BIGINT,residue_g BIGINT)")
            db.executemany("INSERT INTO ods_batches VALUES (?,?,?,?,?,?,?,?)",batches)
            db.execute(files("recovery_lake").joinpath("model.sql").read_text())
            for name,layer in [("dim_material","dwd"),("dwd_batch","dwd"),("ads_daily_recovery","ads")]:
                db.execute(f"COPY {name} TO {sql_path(staging/layer/(name+'.parquet'))} (FORMAT PARQUET,COMPRESSION ZSTD)")
            values=db.execute("SELECT site,CAST(process_date AS VARCHAR),family,batches,CAST(input_g AS BIGINT),CAST(recovered_g AS BIGINT),CAST(residue_g AS BIGINT),recovery_rate FROM ads_daily_recovery ORDER BY site,process_date,family").fetchall()
            summary={"batch_count":len(batches),"daily":[dict(zip(["site","date","family","batches","input_g","recovered_g","residue_g","recovery_rate"],row)) for row in values]}
        manifest={"schema_version":1,"mass_unit":"g","accounting_timezone":"UTC","input_sha256":{n:hashlib.sha256((staging/"ods"/n).read_bytes()).hexdigest() for n in ["materials.csv","batches.csv"]},"summary":summary}
        (staging/"manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n")
        staging.rename(output)
    return summary
