"""Fail closed when a declared mandatory runtime suite did not actually pass."""
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def validate(root, manifest=None):
    config=json.loads((ROOT/'docs/architecture/modules.json').read_text())
    required=manifest if manifest is not None else config['required_runtime_reports']
    if not required:
        raise ValueError('runtime evidence manifest is empty')
    for name,contract in required.items():
        path=root/name
        if not path.is_file():
            raise ValueError(f'missing mandatory evidence: {name}')
        if contract['kind'] == 'junit':
            cases=list(ET.parse(path).getroot().iter('testcase'))
            if not cases or any(c.find(k) is not None for c in cases for k in ('failure','error','skipped')):
                raise ValueError(f'mandatory suite did not fully pass: {name}')
        elif contract['kind'] == 'json':
            value=json.loads(path.read_text())
            for key,wanted in contract['fields'].items():
                if value.get(key) != wanted:
                    raise ValueError(f'missing successful evidence {name}:{key}')
        elif contract['kind'] == 'marker':
            text=path.read_text()
            if contract['marker'] not in text or 'Traceback (most recent call last)' in text:
                raise ValueError(f'native suite did not finish: {name}')
        else:
            raise ValueError(f'unknown evidence kind: {contract}')
    return len(required)


if __name__ == '__main__':
    print(f'{validate(Path(sys.argv[1]))} mandatory runtime reports verified')
