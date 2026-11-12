"""v2 mixed-material accounting tests, including an independent brute reference."""
import hashlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import duckdb

from recovery_lake.mixed.allocation import allocate_batch
from recovery_lake.mixed.pipeline import build as mixed_build
import recovery_lake.mixed.pipeline as mixed_pipeline

PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT / "data"


def brute_reference(materials, streams):
    """Exhaustive 0/1 reference for tiny batches only.

    Enumerates every rounding matrix, keeps those satisfying both margins and
    the zero-remainder rule, then maximises the remainder objective and the
    lexicographic u vector in (material_id, stream_id) code point order.
    """
    rows, cols = len(materials), len(streams)
    n = sum(mass for _, mass in materials)
    assert n == sum(mass for _, mass in streams)
    base = [[materials[i][1] * streams[j][1] // n for j in range(cols)] for i in range(rows)]
    rem = [[materials[i][1] * streams[j][1] % n for j in range(cols)] for i in range(rows)]
    row_deficit = [materials[i][1] - sum(base[i]) for i in range(rows)]
    col_deficit = [streams[j][1] - sum(base[i][j] for i in range(rows)) for j in range(cols)]
    order = sorted(
        ((i, j) for i in range(rows) for j in range(cols)),
        key=lambda c: (materials[c[0]][0], streams[c[1]][0]),
    )
    best = None
    for bits in itertools.product((0, 1), repeat=rows * cols):
        u = [bits[i * cols:(i + 1) * cols] for i in range(rows)]
        if any(u[i][j] and rem[i][j] == 0 for i in range(rows) for j in range(cols)):
            continue
        if any(sum(u[i]) != row_deficit[i] for i in range(rows)):
            continue
        if any(sum(u[i][j] for i in range(rows)) != col_deficit[j] for j in range(cols)):
            continue
        objective = sum(rem[i][j] * u[i][j] for i in range(rows) for j in range(cols))
        # Lexicographic comparison over the ordered u vector.
        lex = tuple(u[i][j] for i, j in order)
        candidate = (objective, lex, tuple(tuple(row) for row in u))
        if best is None or candidate > best:
            best = candidate
    return best


def matrix(cells, materials, streams):
    grid = [[None] * len(streams) for _ in materials]
    for cell in cells:
        i = [m for m, _ in materials].index(cell.material_id)
        j = [s for s, _ in streams].index(cell.stream_id)
        grid[i][j] = cell.allocated
    return grid


class AllocationTests(unittest.TestCase):
    def test_one_gram_diagonal(self):
        materials = [("A", 1), ("B", 1), ("C", 1)]
        streams = [("S1", 1), ("S2", 1), ("S3", 1)]
        cells, n = allocate_batch(materials, streams)
        self.assertEqual(n, 3)
        self.assertEqual(matrix(cells, materials, streams), [[1, 0, 0], [0, 1, 0], [0, 0, 1]])

    def test_contract_matrix(self):
        materials = [("A", 2), ("B", 1), ("C", 1)]
        streams = [("S1", 2), ("S2", 1), ("S3", 1)]
        cells, n = allocate_batch(materials, streams)
        self.assertEqual(matrix(cells, materials, streams), [[1, 1, 0], [1, 0, 0], [0, 0, 1]])

    def test_zero_remainders_never_round_up(self):
        cells, _ = allocate_batch([("A", 2), ("B", 2)], [("S", 4)])
        self.assertEqual([(c.base, c.remainder, c.rounded_up, c.allocated) for c in cells],
                         [(2, 0, 0, 2), (2, 0, 0, 2)])
        for cell in cells:
            if cell.remainder == 0:
                self.assertEqual(cell.rounded_up, 0)

    def test_products_beyond_64_bit(self):
        big = 10 ** 9  # a_i*b_j up to 10^18; use larger split below for overflow
        materials = [("A", big), ("B", big - 1), ("C", 1)]
        streams = [("S1", big), ("S2", big)]
        cells, n = allocate_batch(materials, streams)
        self.assertEqual(n, 2 * big)
        # a_i*b_j = big*big ~= 10^18 fits 64 bits, so scale up the raw grams.
        huge = 10 ** 12
        materials = [("A", huge), ("B", huge), ("C", 1)]
        streams = [("S1", huge), ("S2", huge + 1)]
        cells, n = allocate_batch(materials, streams)
        self.assertEqual(sum(c.allocated for c in cells), n)
        for cell in cells:
            self.assertEqual(cell.base, (huge * (huge if cell.stream_id == "S1" else huge + 1)
                                         if cell.material_id != "C" else
                                         1 * (huge if cell.stream_id == "S1" else huge + 1)) // n)
            self.assertIn(cell.rounded_up, (0, 1))
        self.assertEqual(matrix(cells, materials, streams)[0][0] + matrix(cells, materials, streams)[0][1], huge)
        self.assertEqual(matrix(cells, materials, streams)[1][0] + matrix(cells, materials, streams)[1][1], huge)
        self.assertEqual(matrix(cells, materials, streams)[2][0] + matrix(cells, materials, streams)[2][1], 1)

    def test_single_material_and_single_stream(self):
        cells, _ = allocate_batch([("A", 7)], [("S", 7)])
        self.assertEqual(cells[0].base, 7)
        self.assertEqual(cells[0].remainder, 0)
        self.assertEqual(cells[0].allocated, 7)

    def test_random_brute_force_reference(self):
        """Compare against the exhaustive reference over compositions."""
        import random
        random.seed(20261112)

        def splits(total, parts):
            if parts == 1:
                return [total]
            cuts = sorted(random.sample(range(1, total), parts - 1))
            return ([cuts[0]] + [cuts[t] - cuts[t - 1] for t in range(1, parts - 1)]
                    + [total - cuts[-1]])

        compared = 0
        for _ in range(60):
            rows, cols = random.randint(1, 3), random.randint(1, 3)
            total = random.randint(max(rows, cols), 12)
            materials = [(f"M{i:02d}", mass)
                         for i, mass in enumerate(splits(total, rows))]
            streams = [(f"S{j:02d}", mass)
                       for j, mass in enumerate(splits(total, cols))]
            cells, n = allocate_batch(materials, streams)
            got = [[0] * cols for _ in range(rows)]
            for cell in cells:
                i = [m for m, _ in materials].index(cell.material_id)
                j = [s for s, _ in streams].index(cell.stream_id)
                got[i][j] = cell.rounded_up
            objective = sum(materials[i][1] * streams[j][1] % n * got[i][j]
                            for i in range(rows) for j in range(cols))
            order = sorted(
                ((i, j) for i in range(rows) for j in range(cols)),
                key=lambda c: (materials[c[0]][0], streams[c[1]][0]),
            )
            lex = tuple(got[i][j] for i, j in order)
            expected = brute_reference(materials, streams)
            self.assertEqual((objective, lex), expected[:2], (materials, streams))
            compared += 1
        self.assertEqual(compared, 60)

    def test_brute_force_reference(self):
        cases = [
            ([("A", 1), ("B", 1), ("C", 1)], [("S1", 1), ("S2", 1), ("S3", 1)]),
            ([("A", 2), ("B", 1), ("C", 1)], [("S1", 2), ("S2", 1), ("S3", 1)]),
            ([("A", 3), ("B", 2)], [("S1", 1), ("S2", 4)]),
            ([("A", 5), ("B", 3), ("C", 2)], [("S1", 4), ("S2", 3), ("S3", 3)]),
            ([("A", 7), ("B", 5), ("C", 3)], [("S1", 6), ("S2", 5), ("S3", 4)]),
            ([("A", 4), ("B", 4), ("C", 4)], [("S1", 5), ("S2", 7)]),
            ([("Z", 11), ("a", 7)], [("S1", 9), ("S2", 9)]),
        ]
        for materials, streams in cases:
            cells, n = allocate_batch(materials, streams)
            got = [[cell.allocated - cell.base for cell in cells
                    if cell.material_id == materials[i][0] and cell.stream_id == streams[j][0]][0]
                   for i in range(len(materials)) for j in range(len(streams))]
            got = [got[i * len(streams):(i + 1) * len(streams)] for i in range(len(materials))]
            expected = brute_reference(materials, streams)
            self.assertIsNotNone(expected, (materials, streams))
            objective = sum(
                materials[i][1] * streams[j][1] % n * got[i][j]
                for i in range(len(materials)) for j in range(len(streams))
            )
            order = sorted(
                ((i, j) for i in range(len(materials)) for j in range(len(streams))),
                key=lambda c: (materials[c[0]][0], streams[c[1]][0]),
            )
            lex = tuple(got[i][j] for i, j in order)
            self.assertEqual((objective, lex), expected[:2], (materials, streams))

    def test_tie_break_uses_code_points_not_input_order(self):
        materials = [("b", 1), ("A", 1)]
        streams = [("2", 1), ("1", 1)]
        cells, _ = allocate_batch(materials, streams)
        grid = matrix(cells, materials, streams)
        # Ordered cells: (A,1), (A,2), (b,1), (b,2); lexicographic max u
        # gives (A,1) and (b,2) even though input order starts with b,2.
        self.assertEqual(grid, [[1, 0], [0, 1]])

    def test_bad_margins_rejected_by_allocator(self):
        with self.assertRaises(ValueError):
            allocate_batch([("A", 1)], [("S", 2)])


class MixedBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {name: self.root / f"{name}.csv" for name in
                      ("materials", "batches", "charges", "streams")}
        for name, path in self.paths.items():
            shutil.copyfile(DATA / "mixed" / f"{name}.csv", path)
        self.out = self.root / "lake"

    def build(self, output=None):
        return mixed_build(self.paths["materials"], self.paths["batches"],
                           self.paths["charges"], self.paths["streams"],
                           output or self.out)

    def write(self, name, text):
        self.paths[name].write_text(text)

    def snapshot_absent(self):
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.root.glob(".recovery-mixed-*")), [])

    def test_sample_business_values_and_layers(self):
        report = self.build()
        self.assertEqual(report["batch_count"], 3)
        self.assertEqual(report["input_g"], 2300000)
        self.assertEqual(report["recovered_g"], 1820000)
        self.assertEqual(report["residue_g"], 480000)
        fiber = next(r for r in report["daily"] if r["family"] == "fiber")
        self.assertEqual((fiber["input_g"], fiber["recovered_g"]), (1500000, 1100000))
        self.assertAlmostEqual(fiber["recovery_rate"], 11 / 15)
        for name in ("materials", "batches", "charges", "streams"):
            self.assertEqual((self.out / "ods" / f"{name}.csv").read_bytes(),
                             self.paths[name].read_bytes())
        with duckdb.connect(str(self.out / "recovery.duckdb"), read_only=True) as db:
            self.assertEqual(
                db.execute("SELECT sum(input_g),sum(recovered_g),sum(residue_g) "
                           "FROM dwd_process").fetchone(), (2300000, 1820000, 480000),
            )
            self.assertEqual(
                db.execute("SELECT sum(allocated_g) FROM dwd_material_stream "
                           "WHERE stream_kind='recovered'").fetchone()[0], 1820000,
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM dwd_material_stream").fetchone()[0], 12,
            )
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["mode"], "mixed")
        self.assertEqual(set(manifest["sources"]), {"materials", "batches", "charges", "streams"})
        for key, source in manifest["sources"].items():
            ods = self.out / source["path"]
            self.assertEqual(hashlib.sha256(ods.read_bytes()).hexdigest(), source["sha256"])

    def test_fiber_family_uses_batch_count_once(self):
        self.write("materials", "material_id,material_name,family\n"
                                 "A,Mat A,fiber\nB,Mat B,fiber\nC,Mat C,glass\n")
        self.write("batches", "batch_id,site,completed_at,gross,tare,recovered,residue,unit\n"
                              "X,Depot,2026-04-01T00:30:00+08:00,0.004,0,0.003,0.001,kg\n")
        self.write("charges", "batch_id,charge_id,material_id,net,unit\n"
                              "X,c1,A,0.002,kg\nX,c2,B,0.001,kg\nX,c3,C,0.001,kg\n")
        self.write("streams", "batch_id,stream_id,kind,mass,unit\n"
                              "X,s1,recovered,0.002,kg\nX,s2,recovered,0.001,kg\n"
                              "X,s3,residue,0.001,kg\n")
        report = self.build()
        self.assertEqual([(c.material_id, c.stream_id, c.allocated)
                          for c in allocate_batch([("A", 2), ("B", 1), ("C", 1)],
                                                  [("s1", 2), ("s2", 1), ("s3", 1)])[0]],
                         [("A", "s1", 1), ("A", "s2", 1), ("A", "s3", 0),
                          ("B", "s1", 1), ("B", "s2", 0), ("B", "s3", 0),
                          ("C", "s1", 0), ("C", "s2", 0), ("C", "s3", 1)])
        fiber = next(r for r in report["daily"] if r["family"] == "fiber")
        # A=2g, B=1g in the same fiber batch; recovered streams s1+s2=3g.
        self.assertEqual((fiber["input_g"], fiber["recovered_g"], fiber["residue_g"],
                          fiber["batches"]), (3, 3, 0, 1))
        self.assertEqual(fiber["date"], "2026-03-31")  # UTC date of +08:00 completion
        glass = next(r for r in report["daily"] if r["family"] == "glass")
        self.assertEqual((glass["input_g"], glass["recovered_g"], glass["residue_g"],
                          glass["batches"]), (1, 0, 1, 1))

    def test_contract_example_in_grams_with_two_stream_kinds(self):
        self.write("materials", "material_id,material_name,family\n"
                                 "A,Mat A,fiber\nB,Mat B,fiber\nC,Mat C,glass\n")
        self.write("batches", "batch_id,site,completed_at,gross,tare,recovered,residue,unit\n"
                              "X,Depot,2026-03-01T00:00:00Z,0.000004,0,0.000003,0.000001,t\n")
        self.write("charges", "batch_id,charge_id,material_id,net,unit\n"
                              "X,cA,A,0.000002,t\nX,cB,B,0.000001,t\nX,cC,C,0.000001,t\n")
        self.write("streams", "batch_id,stream_id,kind,mass,unit\n"
                              "X,S1,recovered,0.000002,t\nX,S2,recovered,0.000001,t\n"
                              "X,S3,residue,0.000001,t\n")
        report = self.build()
        fiber = next(r for r in report["daily"] if r["family"] == "fiber")
        glass = next(r for r in report["daily"] if r["family"] == "glass")
        self.assertEqual((fiber["input_g"], fiber["recovered_g"], fiber["residue_g"],
                          fiber["batches"]), (3, 3, 0, 1))
        self.assertEqual((glass["input_g"], glass["recovered_g"], glass["residue_g"],
                          glass["batches"]), (1, 0, 1, 1))
        with duckdb.connect(str(self.out / "recovery.duckdb"), read_only=True) as db:
            rows = db.execute(
                "SELECT material_id,stream_id,allocated_g FROM dwd_material_stream "
                "ORDER BY material_id,stream_id").fetchall()
            self.assertEqual(rows, [("A", "S1", 1), ("A", "S2", 1), ("A", "S3", 0),
                                    ("B", "S1", 1), ("B", "S2", 0), ("B", "S3", 0),
                                    ("C", "S1", 0), ("C", "S2", 0), ("C", "S3", 1)])
            self.assertEqual(db.execute(
                "SELECT material_id,input_g,recovered_g,residue_g FROM dwd_batch_material "
                "ORDER BY material_id").fetchall(),
                [("A", 2, 2, 0), ("B", 1, 1, 0), ("C", 1, 0, 1)])

    def test_contract_bound_mass_exceeds_64_bit_products(self):
        """Largest contract-legal batch: a_i*b_j reaches 10^24 (beyond 2^63)."""
        self.write("materials", "material_id,material_name,family\n"
                                 "A,Mat A,fiber\nB,Mat B,fiber\n")
        self.write("batches", "batch_id,site,completed_at,gross,tare,recovered,residue,unit\n"
                              "H,Depot,2026-03-07T00:00:00Z,1000000,0,600000,400000,t\n")
        self.write("charges", "batch_id,charge_id,material_id,net,unit\n"
                              "H,c1,A,499999.999999,t\nH,c2,B,500000.000001,t\n")
        self.write("streams", "batch_id,stream_id,kind,mass,unit\n"
                              "H,s1,recovered,600000,t\nH,s2,residue,400000,t\n")
        report = self.build()
        self.assertEqual((report["input_g"], report["recovered_g"], report["residue_g"]),
                         (10 ** 12, 600000 * 10 ** 6, 400000 * 10 ** 6))
        with duckdb.connect(str(self.out / "recovery.duckdb"), read_only=True) as db:
            rows = db.execute(
                "SELECT material_id,stream_id,base_g,remainder_num,denominator_g,"
                "rounded_up,allocated_g FROM dwd_material_stream "
                "ORDER BY material_id,stream_id").fetchall()
            # Exact bases: floor(499999999999*600000000000/10^12) =
            # 299999999999 (remainder 4*10^11), and so on.  The two rounding
            # grams go to the 6*10^11 remainders (objective 1.2*10^12 beats
            # 0.8*10^12), i.e. A/s2 and B/s1.
            self.assertEqual(rows, [
                ("A", "s1", 299999999999, 400000000000, 10 ** 12, False, 299999999999),
                ("A", "s2", 199999999999, 600000000000, 10 ** 12, True, 200000000000),
                ("B", "s1", 300000000000, 600000000000, 10 ** 12, True, 300000000001),
                ("B", "s2", 200000000000, 400000000000, 10 ** 12, False, 200000000000),
            ])
        # The raw products used above are 3*10^23 and 2*10^23, far beyond 2^63.
        self.assertGreater(499999999999 * 600000000000, 2 ** 63)

    def test_two_snapshots_share_logical_content(self):
        first = self.build(self.root / "one")
        second = self.build(self.root / "two")
        self.assertEqual(first, second)
        self.assertEqual((self.root / "one" / "manifest.json").read_bytes(),
                         (self.root / "two" / "manifest.json").read_bytes())
        manifest_text = (self.root / "one" / "manifest.json").read_text()
        self.assertNotIn(str(self.root), manifest_text)
        self.assertNotRegex(manifest_text, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z")
        tables = ["dim_material", "dwd_process", "dwd_charge", "dwd_stream",
                  "dwd_material_stream", "dwd_batch_material", "ads_daily_recovery"]
        with duckdb.connect(str(self.root / "one" / "recovery.duckdb"), read_only=True) as a, \
                duckdb.connect(str(self.root / "two" / "recovery.duckdb"), read_only=True) as b:
            for table in tables:
                columns = [row[0] for row in a.execute(f"DESCRIBE {table}").fetchall()]
                # TIMESTAMPTZ result conversion needs the optional pytz module,
                # so compare that column as text; others compare directly.
                order = ", ".join(
                    f"CAST({column} AS VARCHAR)" if column == "completed_at" else column
                    for column in columns
                )
                self.assertEqual(
                    a.execute(f"SELECT {order} FROM {table} ORDER BY ALL").fetchall(),
                    b.execute(f"SELECT {order} FROM {table} ORDER BY ALL").fetchall(), table)
            a.close(); b.close()
        parquet = sorted(p.relative_to(self.root / "one") for p in (self.root / "one").rglob("*.parquet"))
        self.assertEqual(parquet, sorted(p.relative_to(self.root / "two")
                                         for p in (self.root / "two").rglob("*.parquet")))

    def test_shuffle_and_split_charge_invariance(self):
        first = self.build(self.root / "first")
        shuffled = {}
        for name in ("materials", "batches", "charges", "streams"):
            lines = self.paths[name].read_text().splitlines()
            data = lines[1:]
            data.reverse()
            if name == "charges":
                # Split one same-material charge into two new charge IDs.
                data = [line for line in data if not line.startswith("B1,C2,P2")]
                data = ["B1,C2,P2,50,kg", "B1,C2b,P2,250,kg"] + data
            changed = [lines[0]] + data
            self.write(name, "\n".join(changed) + "\n")
            shuffled[name] = self.paths[name].read_bytes()
        second = self.build(self.root / "second")
        stable = lambda summary: {k: v for k, v in summary.items()
                                  if k not in {"daily", "charge_count"}}
        self.assertEqual(stable(first), stable(second))
        self.assertEqual(second["charge_count"], 7)  # one charge was split in two
        self.assertEqual(first["daily"], second["daily"])
        with duckdb.connect(str(self.root / "first" / "recovery.duckdb"), read_only=True) as a, \
                duckdb.connect(str(self.root / "second" / "recovery.duckdb"), read_only=True) as b:
            rows = lambda db: db.execute(
                "SELECT batch_id,material_id,stream_id,base_g,remainder_num,"
                "denominator_g,rounded_up,allocated_g FROM dwd_material_stream "
                "ORDER BY batch_id,material_id,stream_id").fetchall()
            self.assertEqual(rows(a), rows(b))
            ads = "SELECT site,CAST(process_date AS VARCHAR),family,batches,input_g," \
                  "recovered_g,residue_g FROM ads_daily_recovery ORDER BY 1,2,3"
            self.assertEqual(a.execute(ads).fetchall(), b.execute(ads).fetchall())
        manifest = json.loads((self.root / "second" / "manifest.json").read_text())
        for key, path in self.paths.items():
            self.assertEqual(manifest["sources"][key]["sha256"],
                             hashlib.sha256(path.read_bytes()).hexdigest())
        with duckdb.connect(str(self.root / "second" / "recovery.duckdb"), read_only=True) as db:
            lines = db.execute("SELECT charge_id,source_line FROM dwd_charge "
                               "WHERE batch_id='B1' ORDER BY charge_id").fetchall()
            charge_lines = (self.paths["charges"].read_text().splitlines())
            for charge_id, line in lines:
                self.assertTrue(charge_lines[line - 1].startswith(f"B1,{charge_id},"))

    def test_zero_recovered_and_zero_residue(self):
        self.write("batches", "batch_id,site,completed_at,gross,tare,recovered,residue,unit\n"
                              "Z1,Depot,2026-03-05T00:00:00Z,5,0,0,5,kg\n"
                              "Z2,Depot,2026-03-05T01:00:00Z,5,0,5,0,kg\n")
        self.write("charges", "batch_id,charge_id,material_id,net,unit\n"
                              "Z1,c1,P1,4,kg\nZ1,c2,P2,1,kg\n"
                              "Z2,c3,P1,2,kg\nZ2,c4,P2,3,kg\n")
        self.write("streams", "batch_id,stream_id,kind,mass,unit\n"
                              "Z1,s1,residue,5,kg\nZ2,s2,recovered,5,kg\n")
        report = self.build()
        self.assertEqual(report["recovered_g"], 5000)
        self.assertEqual(report["residue_g"], 5000)
        with duckdb.connect(str(self.out / "recovery.duckdb"), read_only=True) as db:
            self.assertEqual(db.execute(
                "SELECT batch_id,sum(allocated_g) FROM dwd_material_stream "
                "GROUP BY batch_id ORDER BY batch_id").fetchall(),
                [("Z1", 5000), ("Z2", 5000)])
            self.assertEqual(db.execute(
                "SELECT count(*) FROM dwd_material_stream WHERE base_g=0 AND allocated_g=0"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT batch_id,material_id,recovered_g,residue_g FROM dwd_batch_material "
                "ORDER BY batch_id,material_id").fetchall(),
                [("Z1", "P1", 0, 4000), ("Z1", "P2", 0, 1000),
                 ("Z2", "P1", 2000, 0), ("Z2", "P2", 3000, 0)])

    def test_single_material_and_single_stream_end_to_end(self):
        self.write("batches", "batch_id,site,completed_at,gross,tare,recovered,residue,unit\n"
                              "S,Depot,2026-03-06T00:00:00Z,2,0,2,0,kg\n")
        self.write("charges", "batch_id,charge_id,material_id,net,unit\nS,c1,P1,2,kg\n")
        self.write("streams", "batch_id,stream_id,kind,mass,unit\nS,s1,recovered,2,kg\n")
        report = self.build()
        self.assertEqual(report["batch_count"], 1)
        with duckdb.connect(str(self.out / "recovery.duckdb"), read_only=True) as db:
            self.assertEqual(db.execute("SELECT allocated_g,base_g,rounded_up FROM "
                                        "dwd_material_stream").fetchone(), (2000, 2000, False))

    def assert_contract_failure(self, **changes):
        for name, text in changes.items():
            self.write(name, text)
        with self.assertRaises(ValueError):
            self.build()
        self.snapshot_absent()

    def test_contract_violations_do_not_publish(self):
        original = {name: path.read_text() for name, path in self.paths.items()}
        cases = []
        # unknown batch, material and dangling stream batch
        cases.append({"charges": original["charges"] + "NOPE,C,P1,1,kg\n"})
        cases.append({"charges": original["charges"].replace("C6,G2", "C6,GX")})
        cases.append({"streams": original["streams"] + "NOPE,S,residue,1,kg\n"})
        # duplicate keys with identical content
        cases.append({"charges": original["charges"] + "B1,C1,P1,700,kg\n"})
        cases.append({"streams": original["streams"] + "B1,S1,recovered,800,kg\n"})
        cases.append({"batches": original["batches"] + original["batches"].splitlines()[1] + "\n"})
        cases.append({"materials": original["materials"] + "P1,Mixed paper,fiber\n"})
        # wrong charge total and wrong per-kind stream totals
        cases.append({"charges": original["charges"].replace("B1,C1,P1,700", "B1,C1,P1,701")})
        cases.append({"streams": original["streams"].replace("B1,S1,recovered,800", "B1,S1,recovered,801")})
        cases.append({"streams": original["streams"].replace("B1,S2,residue,200", "B1,S2,residue,199")})
        # invalid masses / units / kind
        cases.append({"charges": original["charges"].replace("B1,C1,P1,700,kg", "B1,C1,P1,0,kg")})
        cases.append({"charges": original["charges"].replace("B1,C1,P1,700,kg", "B1,C1,P1,-1,kg")})
        cases.append({"charges": original["charges"].replace("B1,C1,P1,700,kg", "B1,C1,P1,700,lbs")})
        cases.append({"charges": original["charges"].replace("B1,C1,P1,700,kg", "B1,C1,P1,700.00001,kg")})
        cases.append({"streams": original["streams"].replace("B1,S1,recovered", "B1,S1,product")})
        cases.append({"streams": original["streams"].replace("B1,S1,recovered,800", "B1,S1,recovered,0")})
        # batch bound violations
        cases.append({"batches": original["batches"].replace(
            "1200,200,800,200,kg", "1200,200,800,201,kg")})
        cases.append({"batches": original["batches"].replace(
            "1200,200,800,200,kg", "1200,1200,0,0,kg")})
        # identifiers must be non-empty and have no surrounding whitespace
        for bad in (" B1", "B1 ", "B 1"):
            cases.append({"charges": original["charges"].replace("B1,C1", f'"{bad}",C1')})
        for index, change in enumerate(cases):
            for name, text in original.items():
                self.write(name, text)
            with self.subTest(case=index):
                self.assert_contract_failure(**{k: v for k, v in change.items()})

    def test_error_message_locates_record_or_batch(self):
        self.write("charges", self.paths["charges"].read_text() + "NOPE,C,P1,1,kg\n")
        with self.assertRaisesRegex(ValueError, r"charges\.csv: record starting at line 8"):
            self.build()
        # Restore and make the batch totals fail.
        shutil.copyfile(DATA / "mixed" / "charges.csv", self.paths["charges"])
        self.write("charges", self.paths["charges"].read_text().replace("B1,C1,P1,700", "B1,C1,P1,701"))
        with self.assertRaisesRegex(ValueError, r"batch 'B1'"):
            self.build()
        self.snapshot_absent()

    def test_publish_failure_cleans_and_preserves_existing(self):
        existing = self.root / "existing"
        self.build(existing)
        before = {p.relative_to(existing): p.read_bytes() for p in existing.rglob("*") if p.is_file()}
        targets = [
            (mixed_pipeline.export_parquet,
             mock.patch.object(mixed_pipeline, "export_parquet",
                               side_effect=RuntimeError("injected parquet failure"))),
            (mixed_pipeline.write_manifest,
             mock.patch.object(mixed_pipeline, "write_manifest",
                               side_effect=RuntimeError("injected manifest failure"))),
        ]
        for target, patcher in targets:
            with patcher:
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    self.build()
            self.snapshot_absent()
            after = {p.relative_to(existing): p.read_bytes() for p in existing.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(self.build(self.root / f"after-{target.__name__}")["batch_count"], 3)

    def test_existing_output_rejected(self):
        self.build()
        with self.assertRaises(FileExistsError):
            self.build()


class MixedCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        for name in ("materials", "batches", "charges", "streams"):
            shutil.copyfile(DATA / "mixed" / f"{name}.csv", self.inputs / f"{name}.csv")
        self.out = self.root / "lake"

    def run_cli(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PROJECT)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        console = Path(sys.executable).with_name("recovery-lake-mixed")
        if console.exists():
            # Exercise the installed console entry point when available.
            command = [str(console)]
        else:
            command = [sys.executable, "-m", "recovery_lake.mixed"]
        process = subprocess.run(
            command + ["--materials", str(self.inputs / "materials.csv"),
                       "--batches", str(self.inputs / "batches.csv"),
                       "--charges", str(self.inputs / "charges.csv"),
                       "--streams", str(self.inputs / "streams.csv"),
                       "--output", str(self.out)],
            cwd=str(PROJECT), env=env, capture_output=True, text=True,
        )
        return process

    def test_cli_subprocess_snapshot_parquet_and_relocation(self):
        process = self.run_cli()
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual((report["batch_count"], report["input_g"], report["recovered_g"],
                          report["residue_g"]), (3, 2300000, 1820000, 480000))
        manifest = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(report, manifest["summary"])
        dwd = self.out / "dwd"
        db = duckdb.connect(str(self.out / "recovery.duckdb"), read_only=True)
        try:
            # Compare every DWD fact key by key with its Parquet export.
            checks = {
                "dwd_process": ("batch_id", "batch_id"),
                "dwd_charge": ("batch_id,charge_id", "batch_id,charge_id"),
                "dwd_stream": ("batch_id,stream_id", "batch_id,stream_id"),
                "dwd_material_stream": ("batch_id,material_id,stream_id",
                                        "batch_id,material_id,stream_id"),
                "dwd_batch_material": ("batch_id,material_id", "batch_id,material_id"),
                "dim_material": ("material_id", "material_id"),
            }
            for table, (key, _) in checks.items():
                path = str(dwd / f"{table}.parquet")
                cols = [row[0] for row in db.execute(f"DESCRIBE {table}").fetchall()]
                projection = ", ".join(cols)
                missing_a = db.execute(
                    f"SELECT {projection} FROM {table} EXCEPT SELECT {projection} FROM read_parquet('{path}')"
                ).fetchall()
                missing_b = db.execute(
                    f"SELECT {projection} FROM read_parquet('{path}') EXCEPT SELECT {projection} FROM {table}"
                ).fetchall()
                self.assertEqual((missing_a, missing_b), ([], []), table)
            ads = self.out / "ads" / "ads_daily_recovery.parquet"
            self.assertEqual(
                db.execute("SELECT site,CAST(process_date AS VARCHAR),family,batches,input_g,"
                           "recovered_g,residue_g,recovery_rate FROM ads_daily_recovery ORDER BY 1,2,3"
                           ).fetchall(),
                db.execute(f"SELECT site,CAST(process_date AS VARCHAR),family,batches,input_g,"
                           f"recovered_g,residue_g,recovery_rate FROM read_parquet('{ads}') ORDER BY 1,2,3"
                           ).fetchall(),
            )
            rows = db.execute(
                "SELECT batch_id,material_id,stream_id,base_g,remainder_num,denominator_g,"
                "rounded_up,allocated_g FROM dwd_material_stream ORDER BY 1,2,3").fetchall()
            cells = db.execute(
                "SELECT batch_id,material_id,stream_id,base_g,remainder_num,denominator_g,"
                "rounded_up,allocated_g FROM alloc_cells ORDER BY 1,2,3").fetchall()
            self.assertEqual(rows, cells)
            charges = db.execute("SELECT source_sha256,source_line FROM dwd_charge").fetchall()
            streams = db.execute("SELECT source_sha256,source_line FROM dwd_stream").fetchall()
            self.assertEqual(len({s for s, _ in charges}), 1)
            self.assertEqual(len({s for s, _ in streams}), 1)
            self.assertEqual(charges[0][0], manifest["sources"]["charges"]["sha256"])
            self.assertEqual(streams[0][0], manifest["sources"]["streams"]["sha256"])
        finally:
            db.close()

        # Relocate the snapshot, make the original inputs unreadable, requery.
        moved = self.root / "moved"
        self.out.rename(moved)
        shutil.rmtree(self.inputs)
        self.assertFalse(self.inputs.exists())
        with duckdb.connect(str(moved / "recovery.duckdb"), read_only=True) as db2:
            self.assertEqual(db2.execute(
                "SELECT sum(input_g),sum(recovered_g),sum(residue_g) FROM dwd_process"
            ).fetchone(), (2300000, 1820000, 480000))
            for key, source in manifest["sources"].items():
                ods = moved / source["path"]
                self.assertEqual(hashlib.sha256(ods.read_bytes()).hexdigest(), source["sha256"])
                self.assertNotIn(str(self.root), source["path"])
        self.assertNotIn(str(self.root), (moved / "manifest.json").read_text())

    def test_cli_reports_failure_and_publishes_nothing(self):
        charges = self.inputs / "charges.csv"
        charges.write_text(charges.read_text().replace("B1,C1,P1,700", "B1,C1,P1,701"))
        process = self.run_cli()
        self.assertEqual(process.returncode, 2)
        self.assertIn("batch 'B1'", process.stderr)
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.root.glob(".recovery-mixed-*")), [])


if __name__ == "__main__":
    unittest.main()
