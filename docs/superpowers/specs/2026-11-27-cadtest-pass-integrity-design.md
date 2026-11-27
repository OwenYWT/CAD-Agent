# CADTest Pass Integrity

- Only trusted sealed-artifact scoring may record a benchmark pass.
- Live attachment exits zero only when the newly recorded CADTest result passes.
- DXF scoring compares geometry against manifest-bound case truth and fails closed for unsupported cases.
- Live launch and Worker share trusted process configuration and verify report store and dataset identity.
- Terminal report rebuilding preserves the CADTest quality projection.

Regression tests precede implementation. Local tests do not substitute for deployment acceptance.
