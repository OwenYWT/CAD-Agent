"""Regenerate/check the packaged native capability contract from typed host schemas."""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend'))
from app.freecad.contracts import capability_schemas
parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
content=json.dumps({'schema_version':'freecad-capabilities.v1',
    'actions':capability_schemas()},ensure_ascii=False,sort_keys=True,indent=2)+'\n'
path=ROOT/'backend/app/freecad/capabilities.json'
if args.check:
    if path.read_text()!=content:raise SystemExit('Native capability manifest is stale; regenerate it and rebuild the sandbox image')
else:path.write_text(content)
