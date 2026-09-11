"""Download immutable FCStd/STEP revisions for independent kernel measurements."""
import hashlib
import json
import os
from pathlib import Path

import httpx


def main():
    base = os.environ["CAD_NATIVE_E2E_URL"].rstrip("/")
    private = json.loads(Path(os.environ["CAD_NATIVE_E2E_PRIVATE"]).read_text())
    evidence = Path(os.environ["CAD_DEPLOY_EVIDENCE"])
    report = json.loads((evidence / "cloud-acceptance.json").read_text())
    expected = {
        report["source_generation_workflow"]: ("generated", 6),
        report["http_edit_and_permissions"]["workflow_id"]: ("http-edited", 8),
        report["browser_sketch_edit"]["workflow_id"]: ("browser-edited", 9),
    }
    output = evidence / "geometry"
    output.mkdir(exist_ok=True)
    client = httpx.Client(base_url=base, timeout=60, transport=httpx.HTTPTransport(retries=3),
                         headers={"Authorization": "Bearer " + private["owner"]["token"]})
    downloads = httpx.Client(timeout=60, transport=httpx.HTTPTransport(retries=3))
    response = client.get(f"/api/projects/{private['project_id']}/branches/{private['document_id']}/revisions")
    response.raise_for_status()
    found, measurements = [], []
    for revision in response.json():
        source = revision.get("source_workflow_run_id")
        if source not in expected:
            continue
        name, diameter = expected[source]
        r = client.get(f"/api/projects/{private['project_id']}/revisions/{revision['id']}")
        r.raise_for_status()
        artifacts = {a["artifact_kind"]: a for a in r.json()["artifacts"]}
        facts = {"case": name, "revision": revision["id"], "workflow_id": source}
        for kind, extension in (("fcstd", "FCStd"), ("step", "step")):
            a = artifacts[kind]
            # Use the actual S3 presigned HTTPS URL; never print its query string.
            downloaded = downloads.get(a["download_url"])
            downloaded.raise_for_status()
            digest = hashlib.sha256(downloaded.content).hexdigest()
            assert digest == a["sha256"] and len(downloaded.content) == a["size_bytes"]
            (output / f"{name}.{extension}").write_bytes(downloaded.content)
            facts[kind + "_sha256"] = digest
        found.append(facts)
        measurements.append({"name": name, "fcstd": f"/evidence/geometry/{name}.FCStd",
                             "step": f"/evidence/geometry/{name}.step", "dimensions": [60, 40, 8],
                             "holes": [{"x": 30, "y": 20, "diameter": diameter, "depth": 8}]})
    assert len(found) == 3 and {x["workflow_id"] for x in found} == set(expected)
    (output / "measurements.json").write_text(json.dumps(measurements, indent=2))
    (output / "download-verification.json").write_text(json.dumps(found, indent=2))
    print(json.dumps({"revisions_verified": len(found), "presigned_https_downloads": len(found) * 2}))
    client.close()
    downloads.close()


if __name__ == "__main__":
    main()
