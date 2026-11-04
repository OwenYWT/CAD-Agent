"""Shared numerical cleanup; never fill holes or union separate bodies."""


def load_normalized_mesh(path):
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
    return meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
