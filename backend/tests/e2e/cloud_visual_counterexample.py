"""Rejudge the acceptance team's genuine one-hole artifacts with the real provider.

This is a provider-boundary regression, not a new modeling workflow. Inputs are
read-only and SHA-256 checked against the original task's recorded artifacts.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess

from PIL import Image


def main():
    source = Path(os.environ["CAD_ORIGINAL_ACCEPTANCE_DIR"])
    out = Path(os.environ["CAD_VISUAL_RECHECK_DIR"])
    out.mkdir(parents=True, exist_ok=True)
    task = json.loads((source / "multihole-task.json").read_text())
    frames = []
    for view in ("front", "right", "top", "isometric"):
        artifact = next(a for a in task["artifacts"] if a["filename"].endswith("visual-" + view + ".png"))
        path = source / "multihole-artifacts" / artifact["filename"]
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
        width, height = Image.open(path).size
        frames.append({"view": view, "filename": artifact["filename"], "sha256": artifact["sha256"],
            "width": width, "height": height, "size_bytes": len(raw), "content": base64.b64encode(raw).decode()})
    data = {"frames": frames, "plan": task["agent"]["plan"]}
    script = 'payload=json.loads(' + repr(json.dumps(data)) + ')\n'
    script = 'import json\n' + script + '''
import asyncio,base64,tempfile
from pathlib import Path
from app.validation.durable_visual import DurableVisualValidator,VisualRenderEvidence
async def main():
 with tempfile.TemporaryDirectory() as folder:
  paths=[];renders=[]
  for frame in payload['frames']:
   path=Path(folder)/frame['filename'];path.write_bytes(base64.b64decode(frame['content']));paths.append(path)
   renders.append(VisualRenderEvidence(**{k:v for k,v in frame.items() if k!='content'},object_key='verified-acceptance/'+frame['filename']))
  report=await DurableVisualValidator().report(objective=payload['plan']['objective'],design_brief=payload['plan']['design_brief'],render_paths=tuple(paths),renders=tuple(renders),runtime_provenance={'source':'sha256-verified-original-acceptance-renders'})
  print('CAD_VISUAL_COUNTEREXAMPLE='+json.dumps(report.durable_evidence()),flush=True)
  assert report.outcome=='failed' and report.judgment.is_match is False and report.judgment.issues
  assert report.provider_provenance.provider_response_id
asyncio.run(main())
'''
    result = subprocess.run([os.getenv("CAD_PODMAN", "/opt/homebrew/bin/podman"), "exec", "-i", "-e", "PYTHONPATH=/app/backend",
        os.environ["CAD_NATIVE_E2E_API"], "python", "-"], input=script, text=True, capture_output=True, timeout=300)
    (out / "provider.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
    evidence = json.loads(next(line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("CAD_VISUAL_COUNTEREXAMPLE=")))
    (out / "report.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(json.dumps({"outcome": evidence["outcome"], "judgment": evidence["judgment"],
        "provider_response_id": evidence["provider_provenance"]["provider_response_id"]}), flush=True)


if __name__ == "__main__":
    main()
