"""Build-time fail-closed check for the extracted official FreeCAD runtime."""

import Assembly  # noqa: F401
import FreeCAD as App
import Import  # noqa: F401
import Part  # noqa: F401
import PartDesign  # noqa: F401
import Sketcher  # noqa: F401
import Spreadsheet  # noqa: F401


if ".".join(App.Version()[:3]) != "1.1.3":
    raise RuntimeError("extracted FreeCAD runtime is not version 1.1.3")
