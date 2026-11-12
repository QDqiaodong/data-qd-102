"""CLI for the v2 mixed-material mode: python -m recovery_lake.mixed."""
import argparse
import json
from pathlib import Path
from .pipeline import build


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a synthetic mixed-material recovery warehouse (v2)"
    )
    parser.add_argument("--materials", type=Path, default=Path("data/mixed/materials.csv"))
    parser.add_argument("--batches", type=Path, default=Path("data/mixed/batches.csv"))
    parser.add_argument("--charges", type=Path, default=Path("data/mixed/charges.csv"))
    parser.add_argument("--streams", type=Path, default=Path("data/mixed/streams.csv"))
    parser.add_argument("--output", type=Path, default=Path("run/mixed"))
    args = parser.parse_args(argv)
    try:
        result = build(args.materials, args.batches, args.charges, args.streams, args.output)
    except Exception as exc:  # validation, SQL, export or publish failure
        parser.exit(2, f"warehouse failed: {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
