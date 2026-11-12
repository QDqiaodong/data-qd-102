"""Read small full-batch material processing exports, preserving exact grams."""
import csv
import re
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation


def table(path,headers):
    with open(path,newline="",encoding="utf-8") as stream:
        reader=csv.DictReader(stream)
        if reader.fieldnames!=headers:raise ValueError("unexpected CSV columns")
        rows=list(reader)
    if not rows or any(None in r or any(v is None or v=="" for v in r.values()) for r in rows):
        raise ValueError("empty or malformed CSV")
    return rows


def grams(value,unit):
    if unit not in {"kg","t"}:raise ValueError("unsupported mass unit")
    if not re.fullmatch(r"\d{1,9}(?:\.\d{1,6})?",value):raise ValueError("invalid mass precision")
    try: mass=Decimal(value)*(1000 if unit=="kg" else 1000000)
    except InvalidOperation as exc:raise ValueError("invalid mass") from exc
    if mass!=mass.to_integral_value() or not 0<=mass<=10**12:raise ValueError("mass must resolve to bounded integer grams")
    return int(mass)


def load(material_path,batch_path):
    materials=[];known=set()
    for row in table(material_path,["material_id","material_name","family"]):
        if row["material_id"] in known:raise ValueError("duplicate material dimension")
        known.add(row["material_id"])
        materials.append(tuple(row[k] for k in ["material_id","material_name","family"]))
    batches=[];seen=set()
    for row in table(batch_path,["batch_id","site","completed_at","material_id","gross","tare","recovered","residue","unit"]):
        if row["batch_id"] in seen or row["material_id"] not in known:raise ValueError("duplicate batch or unknown material")
        seen.add(row["batch_id"])
        stamp=datetime.fromisoformat(row["completed_at"].replace("Z","+00:00"))
        if stamp.tzinfo is None:raise ValueError("completed_at requires timezone")
        mass=[grams(row[k],row["unit"]) for k in ["gross","tare","recovered","residue"]]
        gross,tare,recovered,residue=mass
        if gross<=tare or recovered+residue!=gross-tare:raise ValueError("positive net input and exact material mass balance required")
        batches.append((row["batch_id"],row["site"],stamp.astimezone(timezone.utc),row["material_id"],*mass))
    return materials,batches
