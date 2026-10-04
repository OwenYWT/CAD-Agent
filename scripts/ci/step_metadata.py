"""Record a completed command and bind its log to the job's evidence."""
import hashlib
import json
from pathlib import Path
import sys

root, name = Path(sys.argv[1]), sys.argv[2]
path = root / 'steps.json'
records = json.loads(path.read_text()) if path.exists() else []
if any(row['step'] == name for row in records):
    raise SystemExit('duplicate step receipt: ' + name)
log = root / (name + '.log')
records.append({'step': name, 'exit_code': 0, 'command': sys.argv[3:],
                'log': log.name, 'sha256': hashlib.sha256(log.read_bytes()).hexdigest()})
path.write_text(json.dumps(records, indent=2) + '\n')
