"""Durable CAD tool receipts shared by execution and Agent layers."""


def execution_receipt(*, source_id: str, source_hash: str, attempt_id: str, outputs: list) -> dict:
    """Created only after real kernel execution and artifact integrity checks."""
    if not outputs or not any(item.get("format") == "fcstd" for item in outputs):
        raise ValueError("FreeCAD tool completion requires a native artifact")
    return {"name": "freecad_execute", "status": "executed", "source_id": source_id,
            "source_hash": source_hash, "attempt_id": attempt_id,
            "outputs": outputs, "engineering_validation": "pending", "saved_revision": None}
