"""Shared numerical cleanup; never fill holes or union separate bodies."""


def load_normalized_mesh(path):
    import trimesh

    mesh = trimesh.load_mesh(path, force="mesh", process=False)
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError("input is not a triangle mesh")
    mesh.merge_vertices(digits_vertex=8)
    mesh.process(validate=True)
    return mesh
