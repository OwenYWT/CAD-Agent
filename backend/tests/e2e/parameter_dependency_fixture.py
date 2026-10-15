"""Compile V01 from the real bounded compiler, not a hand-written substitute."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.freecad.operation_compiler import compile_common_generation

plan = compile_common_generation({'part_type': 'plate',
    'dimensions': {'width': 60, 'depth': 40, 'thickness': 5},
    'features': ['through_hole:diameter=6,count=1,position=centered'],
    'constraints': ['hole_centered=true']}, output_formats=('step',))
assert plan is not None
Path(sys.argv[1]).write_text(json.dumps({'plate': plan.model_dump(mode='json')}))
