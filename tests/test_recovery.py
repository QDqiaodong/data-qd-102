import json
import tempfile
import unittest
from pathlib import Path
import duckdb
from recovery_lake.input import grams
from recovery_lake.pipeline import build

DATA=Path(__file__).resolve().parents[1]/"data"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.material=self.root/"materials.csv";self.batch=self.root/"batches.csv"
        self.material.write_bytes((DATA/"materials.csv").read_bytes())
        self.batch.write_bytes((DATA/"batches.csv").read_bytes())
        self.out=self.root/"lake"

    def build(self):return build(self.material,self.batch,self.out)

    def test_weighted_yield_and_layers(self):
        report=self.build()
        fiber=next(r for r in report["daily"] if r["family"]=="fiber")
        self.assertEqual(fiber["input_g"],1500000)
        self.assertEqual(fiber["recovered_g"],1100000)
        self.assertAlmostEqual(fiber["recovery_rate"],11/15)
        self.assertEqual((self.out/"ods/batches.csv").read_bytes(),self.batch.read_bytes())
        with duckdb.connect(str(self.out/"recovery.duckdb"),read_only=True) as db:
            self.assertEqual(db.execute("SELECT sum(input_g),sum(recovered_g),sum(residue_g) FROM dwd_batch").fetchone(),(2300000,1820000,480000))
            path=str(self.out/"dwd/dwd_batch.parquet").replace("'","''")
            self.assertEqual(db.execute(f"SELECT sum(input_g-recovered_g-residue_g) FROM read_parquet('{path}')").fetchone()[0],0)

    def test_replay_and_existing_output_protection(self):
        first=self.build()
        self.assertEqual(first,build(self.material,self.batch,self.root/"replay"))
        with self.assertRaises(FileExistsError):self.build()

    def test_imbalance_rejects_without_snapshot(self):
        self.batch.write_text(self.batch.read_text().replace("800,200,kg","801,200,kg"))
        with self.assertRaisesRegex(ValueError,"balance"):self.build()
        self.assertFalse(self.out.exists())

    def test_unknown_material_and_duplicate_batch(self):
        original=self.batch.read_text()
        for value in [original.replace(",G1,",",unknown,"),original+original.splitlines()[1]+"\n"]:
            self.batch.write_text(value)
            with self.assertRaises(ValueError):self.build()

    def test_exact_units_and_subgram_rejection(self):
        self.assertEqual(grams("0.001","t"),1000)
        self.assertEqual(grams("0.001","kg"),1)
        for value,unit in [("0.0001","kg"),("NaN","kg"),("-1","kg"),("1","lb")]:
            with self.assertRaises(ValueError):grams(value,unit)

    def test_zero_recovered_is_valid_but_zero_input_is_not(self):
        self.batch.write_text(self.batch.read_text().replace("800,200,kg","0,1000,kg"))
        self.assertEqual(self.build()["batch_count"],3)
        self.batch.write_text(self.batch.read_text().replace("1200,200,0,1000","200,200,0,0"))
        with self.assertRaises(ValueError):build(self.material,self.batch,self.root/"invalid")

    def test_timezone_boundary_and_dimension_family(self):
        self.batch.write_text(self.batch.read_text().replace("2026-03-01T09:00:00Z","2026-03-02T00:00:00+08:00"))
        report=self.build()
        self.assertTrue(all(r["date"]=="2026-03-01" for r in report["daily"]))


if __name__=="__main__":unittest.main()
