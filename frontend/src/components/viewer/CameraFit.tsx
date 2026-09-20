import { useLayoutEffect, useRef } from 'react';
import { useThree } from '@react-three/fiber';
import { Box3, PerspectiveCamera, Vector3 } from 'three';
import type { OrbitControls } from 'three-stdlib';

/** Fit on first geometry or explicit request only; revisions never reset the camera. */
export default function CameraFit({bounds, request = 0}: {bounds:Box3 | null; request?:number}) {
  const get = useThree(state=>state.get);
  const fitted = useRef<number | null>(null);
  useLayoutEffect(()=>{
    if (!bounds || bounds.isEmpty() || fitted.current === request) return;
    const {camera, controls, invalidate} = get();
    if (!(camera instanceof PerspectiveCamera)) return;
    const center = bounds.getCenter(new Vector3());
    const vertical = camera.fov * Math.PI / 180;
    const horizontal = 2 * Math.atan(Math.tan(vertical / 2) * camera.aspect);
    const distance = Math.max(bounds.getSize(new Vector3()).length(), 1) / (2 * Math.sin(Math.min(vertical, horizontal) / 2)) * 1.15;
    camera.up.set(0,0,1);
    camera.position.copy(center).add(new Vector3(1,-1,1).normalize().multiplyScalar(distance));
    camera.near = Math.max(distance / 10000, .001); camera.far = distance * 100;
    camera.lookAt(center); camera.updateProjectionMatrix();
    if (controls && 'target' in controls) {
      const orbit = controls as OrbitControls;
      orbit.target.copy(center); orbit.update();
    }
    fitted.current = request; invalidate();
  }, [bounds, request, get]);
  return null;
}
