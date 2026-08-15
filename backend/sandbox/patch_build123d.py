"""Make build123d's optional 3MF binding lazy on unsupported Linux ARM64.

build123d 0.11.1 imports ``lib3mf`` from its package ``__init__`` even when a
caller only needs STEP/STL. PyPI does not publish lib3mf for Linux ARM64. This
small, fail-closed compatibility patch keeps the supported build123d modeling
and STEP/STL APIs importable while making any Mesher/3MF use fail explicitly.
"""

from __future__ import annotations

import sysconfig
from pathlib import Path


package_init = Path(sysconfig.get_paths()["purelib"]) / "build123d" / "__init__.py"
source = package_init.read_text(encoding="utf-8")
original = "from build123d.mesher import *\n"
replacement = """try:
    from build123d.mesher import *
except ModuleNotFoundError as exc:
    if exc.name != "lib3mf":
        raise

    class Mesher:
        \"\"\"Unavailable 3MF adapter on platforms without a real lib3mf wheel.\"\"\"

        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "build123d Mesher/3MF is unavailable in this Linux ARM64 runtime "
                "because lib3mf does not publish a compatible distribution"
            )
"""

if source.count(original) != 1:
    raise RuntimeError("unexpected build123d __init__.py; compatibility patch not applied")
package_init.write_text(source.replace(original, replacement), encoding="utf-8")
