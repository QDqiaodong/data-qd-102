import argparse
import json
from pathlib import Path
from .pipeline import build


def main():
    parser=argparse.ArgumentParser(description="Build a synthetic material recovery warehouse")
    parser.add_argument("--materials",type=Path,default=Path("data/materials.csv"))
    parser.add_argument("--batches",type=Path,default=Path("data/batches.csv"))
    parser.add_argument("--output",type=Path,default=Path("run/lake"))
    args=parser.parse_args()
    try:result=build(args.materials,args.batches,args.output)
    except (ValueError,OSError) as exc:parser.exit(2,f"warehouse failed: {exc}\n")
    print(json.dumps(result,sort_keys=True))


if __name__=="__main__":main()
