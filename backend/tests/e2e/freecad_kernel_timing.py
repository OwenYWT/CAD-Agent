"""Run inside the pinned FreeCADCmd on a read-only accepted plate checkpoint.

This measures the kernel independently of Agent, network, container startup,
export and downstream gates. It neither saves nor changes the input checkpoint.
Set CAD_NATIVE_KERNEL_INPUT to the mounted FCStd path (default /sandbox/model.FCStd).
"""
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import time

import FreeCAD as App


def main():
    path = Path(os.getenv("CAD_NATIVE_KERNEL_INPUT", "/sandbox/model.FCStd"))
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    samples = []
    for _ in range(10):
        start = time.perf_counter_ns()
        doc = App.openDocument(str(path))
        reopen_ms = (time.perf_counter_ns() - start) / 1_000_000
        doc.UndoMode = 1  # Match the production FreeCAD document loader.
        try:
            hole = doc.getObject("Hole")
            assert hole and hole.TypeId == "PartDesign::Hole"
            base = hole.Diameter.Value
            volume = hole.Shape.Volume
            doc.openTransaction("isolated-kernel-measurement")
            start = time.perf_counter_ns()
            hole.Diameter = base + 1
            doc.recompute()
            mutation_ms = (time.perf_counter_ns() - start) / 1_000_000
            assert hole.Shape.isValid() and not hole.Shape.isNull()
            assert hole.Diameter.Value == base + 1 and hole.Shape.Volume < volume
            doc.abortTransaction()
            doc.recompute()
            assert hole.Diameter.Value == base
            samples.append({"reopen_ms": reopen_ms, "property_set_recompute_ms": mutation_ms})
        finally:
            App.closeDocument(doc.Name)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == checksum
    times = sorted(row["property_set_recompute_ms"] for row in samples)
    report = {
        "schema_version": "cad-kernel-timing.v1",
        "freecad_version": list(App.Version()),
        "input_sha256": checksum,
        "operation": "Hole.Diameter + 1 mm; recompute; verify shape; abort transaction",
        "timed_scope": "property assignment and recompute only",
        "samples": samples,
        "median_ms": statistics.median(times),
        "p95_ms": times[math.ceil(len(times) * .95) - 1],
        "input_unchanged": True,
    }
    print("CAD_NATIVE_KERNEL_TIMING=" + json.dumps(report))


main()
