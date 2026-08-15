from build123d import Box, Cylinder


def gen_step():
    return Box(24, 18, 6) - Cylinder(2, 6)
