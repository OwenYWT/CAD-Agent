"""Shared numerical cleanup; never fill holes or union separate bodies."""


def load_normalized_components(path):
    import trimesh

    loaded = trimesh.load(path, process=False)
    # Named ASCII STL solids retain separate shells even where parts touch.
    # Welding across those identities creates non-manifold assembly edges and
    # discards coincident opposing faces. Normalize only WITHIN each component.
    meshes = list(loaded.dump()) if isinstance(loaded, trimesh.Scene) else [loaded]
    if not meshes or any(not isinstance(mesh, trimesh.Trimesh) for mesh in meshes):
        raise ValueError("input is not a triangle mesh")
    for mesh in meshes:
        mesh.merge_vertices(digits_vertex=8)
        mesh.process(validate=True)
    return meshes


def load_normalized_mesh(path):
    import trimesh
    meshes = load_normalized_components(path)
    return meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)


def export_fused_step_preview(step_path, stl_path):
    """Mesh the union of the delivered STEP bodies in their assembly coordinates."""
    import cadquery as cq

    parts = cq.importers.importStep(str(step_path)).val().Solids()
    if not parts:
        raise ValueError('assembly STEP has no solids')
    shape = parts[0].fuse(*parts[1:]) if len(parts) > 1 else parts[0]
    if not shape.isValid():
        raise ValueError('assembly preview fusion produced invalid geometry')
    cq.exporters.export(cq.Workplane().add(shape), str(stl_path), exportType='STL')
