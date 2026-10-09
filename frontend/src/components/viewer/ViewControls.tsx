import { useLayoutEffect, useRef } from "react";
import { useThree } from "@react-three/fiber";
import { OrthographicCamera, PerspectiveCamera, Vector3 } from "three";
import type { OrbitControls as OrbitControlsType } from "three-stdlib";

export type StandardView = "isometric" | "front" | "back" | "top" | "bottom" | "left" | "right";
export default function ViewControls({ orthographic, view, request }: { orthographic: boolean; view: StandardView; request: number }) {
  const { camera, size, set, controls, invalidate } = useThree();
  const applied = useRef(-1);
  useLayoutEffect(() => {
    if (orthographic === (camera instanceof OrthographicCamera)) return;
    const target = (controls as OrbitControlsType | undefined)?.target || new Vector3();
    const distance = Math.max(camera.position.distanceTo(target), .01);
    const halfHeight = camera instanceof PerspectiveCamera ? distance * Math.tan(camera.fov * Math.PI / 360) / camera.zoom : (camera.top - camera.bottom) / (2 * camera.zoom);
    const next = orthographic ? new OrthographicCamera(-halfHeight * size.width / size.height, halfHeight * size.width / size.height, halfHeight, -halfHeight, camera.near, camera.far) : new PerspectiveCamera(50, size.width / size.height, camera.near, camera.far);
    next.position.copy(camera.position); next.quaternion.copy(camera.quaternion); next.up.copy(camera.up);
    if (next instanceof PerspectiveCamera) next.position.copy(target).add(camera.position.clone().sub(target).normalize().multiplyScalar(halfHeight / Math.tan(next.fov * Math.PI / 360)));
    next.updateProjectionMatrix(); set({ camera: next }); invalidate();
  }, [orthographic, camera, controls, set, size, invalidate]);
  useLayoutEffect(() => {
    if (applied.current === request) return;
    const orbit = controls as OrbitControlsType | undefined;
    if (!orbit) return;
    const directions: Record<StandardView, number[]> = { isometric: [1, -1, 1], front: [0, -1, 0], back: [0, 1, 0], top: [0, 0, 1], bottom: [0, 0, -1], left: [-1, 0, 0], right: [1, 0, 0] };
    const distance = Math.max(camera.position.distanceTo(orbit.target), 1);
    camera.position.copy(orbit.target).add(new Vector3(...directions[view]).normalize().multiplyScalar(distance));
    camera.up.set(0, view === "top" || view === "bottom" ? 1 : 0, view === "top" || view === "bottom" ? 0 : 1);
    camera.lookAt(orbit.target); orbit.update(); applied.current = request; invalidate();
  }, [camera, controls, view, request, invalidate]);
  return null;
}
