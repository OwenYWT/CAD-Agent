"""Read-only catalog of the CAD capabilities shipped with this application."""

from app.capabilities.models import CapabilityManifest
from app.capabilities.registry import get_capability, list_capabilities

__all__ = ["CapabilityManifest", "get_capability", "list_capabilities"]
