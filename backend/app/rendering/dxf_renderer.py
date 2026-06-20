from pathlib import Path

import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def dxf_to_svg(dxf_path: Path, svg_path: Path) -> Path:
    svg_path.parent.mkdir(parents=True, exist_ok=True)

    doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()

    fig = plt.figure(dpi=96)
    ax = fig.add_axes([0, 0, 1, 1])
    ctx = RenderContext(doc)
    out = MatplotlibBackend(ax)
    Frontend(ctx, out).draw_layout(msp)

    ax.set_aspect("equal")
    ax.set_axis_off()

    fig.savefig(str(svg_path), format="svg", bbox_inches="tight", pad_inches=0.1)
    plt.close(fig)

    return svg_path
