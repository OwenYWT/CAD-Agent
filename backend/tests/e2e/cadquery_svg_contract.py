"""Exercise real CAD projection registration and reject invalid SVG artifacts."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from executor_entry import main

input_path = Path('/sandbox/input/input.py')
output = Path('/sandbox/output')
base = "import cadquery as cq\nresult=cq.Workplane('XY').box(40,30,1)\n"
input_path.write_text(base + "cq.exporters.export(result,'/sandbox/output/result.svg')\n")
main()
report = json.loads((output/'result.json').read_text())
assert report['status'] == 'success'
assert set(report['files']) == {'result.svg', 'result.step', 'result.stl'}
svg = ET.parse(output/'result.svg').getroot()
assert svg.tag == '{http://www.w3.org/2000/svg}svg'
assert svg.findall('.//{http://www.w3.org/2000/svg}path')

cases = ['actual_cadquery_svg_registered_with_step_and_stl']
input_path.write_text(base)
for invalid in ('<svg', '<html/>'):
    (output/'result.svg').write_text(invalid)
    try:
        main()
    except SystemExit as error:
        assert error.code == 1
    else:
        raise AssertionError('invalid SVG was advertised as a successful artifact')
    report = json.loads((output/'result.json').read_text())
    assert report['status'] == 'error' and 'error_message' in report
    cases.append('invalid_xml_rejected' if invalid == '<svg' else 'non_svg_root_rejected')

(output/'result.svg').unlink()
main()
report = json.loads((output/'result.json').read_text())
assert report['status'] == 'success' and set(report['files']) == {'result.step', 'result.stl'}
cases.append('absent_svg_never_advertised')
print('CADQUERY_SVG_CONTRACT=' + json.dumps({'passed': True, 'real_kernel': True, 'cases': cases}))
