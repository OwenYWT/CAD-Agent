"""Typed Autodesk Fusion 360 connector boundary.

This package deliberately has no dependency on Autodesk's ``adsk`` module.  The
real Fusion integration lives in ``fusion_addin/CADAgentFusionConnector`` and is
loaded by Fusion Desktop only.
"""

from .contract import CONTRACT_VERSION, PROTOCOL_VERSION, CadAdapter

__all__ = ["CONTRACT_VERSION", "PROTOCOL_VERSION", "CadAdapter"]
