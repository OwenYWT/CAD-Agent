"""
STEP analysis script template — runs inside the sandbox container.

Uses OCP (OpenCascade) via CadQuery to extract precise geometry data:
- Face classification (plane, cylinder, cone, sphere, etc.)
- Feature recognition (holes, pockets, fillets, chamfers)
- Precise wall thickness measurement
- Draft angle analysis per face
- Edge analysis (fillet radii, chamfer sizes)

The generated code sets a `result` dict variable that the sandbox serializes.
"""

STEP_ANALYSIS_SCRIPT = '''
import cadquery as cq
import math
import numpy as np

# OCP imports for precise geometry analysis
from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
from OCP.GeomAbs import (
    GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Cone,
    GeomAbs_Sphere, GeomAbs_Torus, GeomAbs_BSplineSurface,
    GeomAbs_Circle, GeomAbs_Line,
)
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE, TopAbs_FORWARD
from OCP.TopoDS import TopoDS
from OCP.BRep import BRep_Tool
from OCP.gp import gp_Vec, gp_Dir, gp_Pnt
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepBndLib import BRepBndLib
from OCP.Bnd import Bnd_Box

STEP_PATH = "/sandbox/input/model.step"

# -- Surface type mapping --
SURFACE_TYPE_MAP = {
    GeomAbs_Plane: "plane",
    GeomAbs_Cylinder: "cylinder",
    GeomAbs_Cone: "cone",
    GeomAbs_Sphere: "sphere",
    GeomAbs_Torus: "torus",
    GeomAbs_BSplineSurface: "bspline",
}


def cast_topods(kind, shape):
    """Cast an explorer shape across cadquery-ocp 7.7 and 7.9 bindings."""
    caster = getattr(TopoDS, f"{kind}_s", None) or getattr(TopoDS, kind, None)
    if caster is None:
        raise RuntimeError(f"OCP TopoDS has no {kind} casting API")
    return caster(shape)


def get_face_info(face, face_id, pull_dir=(0, 0, 1)):
    """Extract info from a single B-rep face."""
    adaptor = BRepAdaptor_Surface(face)
    stype = adaptor.GetType()
    face_type = SURFACE_TYPE_MAP.get(stype, "other")

    # Face area
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    area = props.Mass()

    # Face center of mass
    center_pnt = props.CentreOfMass()
    center = [round(center_pnt.X(), 3), round(center_pnt.Y(), 3), round(center_pnt.Z(), 3)]

    # Face normal at center (for planes, it's constant; for others, sample at mid-param)
    u_min = adaptor.FirstUParameter()
    u_max = adaptor.LastUParameter()
    v_min = adaptor.FirstVParameter()
    v_max = adaptor.LastVParameter()
    u_mid = (u_min + u_max) / 2
    v_mid = (v_min + v_max) / 2

    normal = [0, 0, 1]
    draft_angle = None

    try:
        pnt = gp_Pnt()
        d1u = gp_Vec()
        d1v = gp_Vec()
        adaptor.D1(u_mid, v_mid, pnt, d1u, d1v)
        n_vec = d1u.Crossed(d1v)
        if n_vec.Magnitude() > 1e-10:
            n_vec.Normalize()
            # Check face orientation
            if face.Orientation() == TopAbs_FORWARD:
                normal = [round(n_vec.X(), 6), round(n_vec.Y(), 6), round(n_vec.Z(), 6)]
            else:
                normal = [round(-n_vec.X(), 6), round(-n_vec.Y(), 6), round(-n_vec.Z(), 6)]

            # Draft angle relative to pull direction
            pull = gp_Dir(pull_dir[0], pull_dir[1], pull_dir[2])
            cos_angle = abs(n_vec.X() * pull.X() + n_vec.Y() * pull.Y() + n_vec.Z() * pull.Z())
            draft_angle = round(90 - math.degrees(math.acos(min(cos_angle, 1.0))), 2)
    except Exception:
        pass

    # Extra info per surface type
    extra = {}
    if face_type == "cylinder":
        extra["radius"] = round(adaptor.Cylinder().Radius(), 3)
    elif face_type == "cone":
        extra["semi_angle"] = round(math.degrees(adaptor.Cone().SemiAngle()), 2)
    elif face_type == "sphere":
        extra["radius"] = round(adaptor.Sphere().Radius(), 3)
    elif face_type == "torus":
        extra["major_radius"] = round(adaptor.Torus().MajorRadius(), 3)
        extra["minor_radius"] = round(adaptor.Torus().MinorRadius(), 3)

    return {
        "face_id": face_id,
        "face_type": face_type,
        "area": round(area, 4),
        "normal": normal,
        "draft_angle": draft_angle,
        "center": center,
        **extra,
    }


def get_edge_info(edge, edge_id):
    """Extract info from a single B-rep edge."""
    adaptor = BRepAdaptor_Curve(edge)
    etype = adaptor.GetType()

    # Edge length
    props = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, props)
    length = props.Mass()

    info = {
        "edge_id": edge_id,
        "length": round(length, 3),
    }

    if etype == GeomAbs_Circle:
        circle = adaptor.Circle()
        info["edge_type"] = "arc"
        info["radius"] = round(circle.Radius(), 3)
        # A complete circular boundary is normally the rim of a cylindrical
        # hole/boss, not evidence of a fillet. Keep the parameter span so
        # feature recognition can distinguish it from a partial round.
        span = abs(adaptor.LastParameter() - adaptor.FirstParameter())
        info["angle_span"] = round(math.degrees(min(span, 2 * math.pi)), 2)
    elif etype == GeomAbs_Line:
        info["edge_type"] = "line"
    else:
        info["edge_type"] = "curve"

    return info


def recognize_features(faces, edges):
    """Simple feature recognition based on face/edge topology."""
    features = []

    # Group cylindrical faces by radius to detect holes
    cylinders = [f for f in faces if f["face_type"] == "cylinder"]
    radius_groups = {}
    for cyl in cylinders:
        r = cyl.get("radius", 0)
        key = round(r, 1)  # group by 0.1mm
        if key not in radius_groups:
            radius_groups[key] = []
        radius_groups[key].append(cyl)

    for radius, group in radius_groups.items():
        if radius <= 0:
            continue
        # Multiple cylindrical faces with same radius likely form a hole or boss
        total_area = sum(f["area"] for f in group)
        avg_center = [
            sum(f["center"][i] for f in group) / len(group) for i in range(3)
        ]
        diameter = round(radius * 2, 3)

        if diameter < 50:  # likely a hole, not a large bore
            features.append({
                "feature_type": "hole",
                "dimensions": {"diameter": diameter, "count": len(group)},
                "center": [round(c, 3) for c in avg_center],
                "face_ids": [f["face_id"] for f in group],
            })

    # Detect fillets: small arc edges connecting faces
    arc_edges = [
        e
        for e in edges
        if e["edge_type"] == "arc" and e.get("angle_span", 360) < 359
    ]
    fillet_radii = set()
    for edge in arc_edges:
        r = edge.get("radius", 0)
        if 0.1 <= r <= 20:  # typical fillet range
            fillet_radii.add(round(r, 2))

    if fillet_radii:
        features.append({
            "feature_type": "fillet",
            "dimensions": {"radii": sorted(fillet_radii)},
            "center": [0, 0, 0],
            "face_ids": [],
        })

    # Detect torus faces as fillets/rounds
    torus_faces = [f for f in faces if f["face_type"] == "torus"]
    for tf in torus_faces:
        minor_r = tf.get("minor_radius", 0)
        if 0.1 <= minor_r <= 20:
            features.append({
                "feature_type": "fillet_face",
                "dimensions": {
                    "minor_radius": minor_r,
                    "major_radius": tf.get("major_radius", 0),
                },
                "center": tf["center"],
                "face_ids": [tf["face_id"]],
            })

    return features


def estimate_wall_thickness_brep(shape, n_samples=100):
    """Estimate wall thickness using BRep ray casting from face centers."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex

    thicknesses = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    face_list = []
    while explorer.More():
        face_list.append(cast_topods("Face", explorer.Current()))
        explorer.Next()

    if len(face_list) < 2:
        return thicknesses

    # Sample a subset of faces
    step = max(1, len(face_list) // n_samples)
    for i in range(0, len(face_list), step):
        face = face_list[i]
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        center = props.CentreOfMass()

        # Find distance to nearest other face
        vertex = BRepBuilderAPI_MakeVertex(center).Vertex()
        min_dist = float("inf")

        for j, other_face in enumerate(face_list):
            if j == i:
                continue
            try:
                dist_calc = BRepExtrema_DistShapeShape(vertex, other_face)
                if dist_calc.IsDone() and dist_calc.NbSolution() > 0:
                    d = dist_calc.Value()
                    if d > 0.01 and d < min_dist:
                        min_dist = d
            except Exception:
                continue

        if min_dist < float("inf"):
            thicknesses.append({
                "point": [round(center.X(), 2), round(center.Y(), 2), round(center.Z(), 2)],
                "thickness": round(min_dist, 3),
            })

    return thicknesses


# --- Main Analysis ---

try:
    shape = cq.importers.importStep(STEP_PATH)
    solid = shape.val().wrapped
except Exception as e:
    result = {"error": f"Failed to load STEP file: {e}", "faces": [], "edges": [],
              "features": [], "wall_thicknesses": [], "draft_angles": [],
              "global_properties": {}}
else:
    # Mesh the shape for potential STL correlation later
    BRepMesh_IncrementalMesh(solid, 0.1, False, 0.5, True)

    # Global properties
    vol_props = GProp_GProps()
    BRepGProp.VolumeProperties_s(solid, vol_props)
    volume = vol_props.Mass()

    surf_props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(solid, surf_props)
    surface_area = surf_props.Mass()

    # Bounding box
    bbox = Bnd_Box()
    BRepBndLib.Add_s(solid, bbox)
    xmin, ymin, zmin, xmax, ymax, zmax = bbox.Get()

    # Analyze faces
    faces = []
    explorer = TopExp_Explorer(solid, TopAbs_FACE)
    fid = 0
    while explorer.More():
        face = cast_topods("Face", explorer.Current())
        try:
            info = get_face_info(face, fid)
            faces.append(info)
        except Exception:
            pass
        fid += 1
        explorer.Next()

    # Analyze edges
    edges = []
    explorer = TopExp_Explorer(solid, TopAbs_EDGE)
    eid = 0
    while explorer.More():
        edge = cast_topods("Edge", explorer.Current())
        try:
            info = get_edge_info(edge, eid)
            edges.append(info)
        except Exception:
            pass
        eid += 1
        explorer.Next()

    # Feature recognition
    features = recognize_features(faces, edges)

    # Wall thickness (limited samples for performance)
    wall_thicknesses = estimate_wall_thickness_brep(solid, n_samples=50)

    # Draft angles (already computed per face)
    draft_angles = [
        {"face_id": f["face_id"], "angle": f["draft_angle"]}
        for f in faces
        if f["draft_angle"] is not None
    ]

    # Derived metrics
    min_wall = min((w["thickness"] for w in wall_thicknesses), default=0)
    min_fillet_radius = None
    for feat in features:
        if feat["feature_type"] == "fillet":
            radii = feat["dimensions"].get("radii", [])
            if radii:
                min_fillet_radius = min(radii)
        elif feat["feature_type"] == "fillet_face":
            r = feat["dimensions"].get("minor_radius")
            if r and (min_fillet_radius is None or r < min_fillet_radius):
                min_fillet_radius = r

    min_hole_diameter = None
    for feat in features:
        if feat["feature_type"] == "hole":
            d = feat["dimensions"].get("diameter")
            if d and (min_hole_diameter is None or d < min_hole_diameter):
                min_hole_diameter = d

    min_draft_angle = min((d["angle"] for d in draft_angles if d["angle"] is not None), default=None)

    result = {
        "faces": faces,
        "edges": edges,
        "features": features,
        "wall_thicknesses": wall_thicknesses,
        "draft_angles": draft_angles,
        "global_properties": {
            "volume": round(volume, 2),
            "surface_area": round(surface_area, 2),
            "bounding_box": {
                "x_min": round(xmin, 2), "x_max": round(xmax, 2),
                "y_min": round(ymin, 2), "y_max": round(ymax, 2),
                "z_min": round(zmin, 2), "z_max": round(zmax, 2),
            },
            "face_count": len(faces),
            "edge_count": len(edges),
        },
        "derived_metrics": {
            "min_wall_thickness": round(min_wall, 3) if min_wall > 0 else None,
            "min_fillet_radius": min_fillet_radius,
            "min_hole_diameter": min_hole_diameter,
            "min_draft_angle": min_draft_angle,
        },
    }
'''
