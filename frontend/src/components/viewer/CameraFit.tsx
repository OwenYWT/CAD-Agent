import { useEffect, useLayoutEffect, useRef } from 'react';
import { useThree } from '@react-three/fiber';
import { Box3, OrthographicCamera, PerspectiveCamera, Vector3 } from 'three';
import type { OrbitControls } from 'three-stdlib';

/** Revisions preserve the camera. Resizes re-fit until the user navigates. */
export default function CameraFit({bounds, request = 0}: {bounds:Box3 | null; request?:number}) {
  const get = useThree(state=>state.get);
  const size = useThree(state=>state.size);
  const controls = useThree(state=>state.controls);
  const fitted = useRef<number | null>(null);
  const viewport = useRef({ width: 0, height: 0 });
  const fitMode = useRef(true);
  useEffect(() => {
    if (!controls || !('target' in controls)) return;
    const orbit = controls as OrbitControls;
    const navigate = () => { fitMode.current = false; };
    orbit.addEventListener('start', navigate);
    return () => orbit.removeEventListener('start', navigate);
  }, [controls]);
  useLayoutEffect(()=>{
    if (!bounds || bounds.isEmpty() || !size.width || !size.height) return;
    const explicit = fitted.current !== request;
    const resized = viewport.current.width !== size.width || viewport.current.height !== size.height;
    viewport.current = { width: size.width, height: size.height };
    if (!explicit && (!resized || !fitMode.current)) return;
    const {camera, controls, invalidate} = get();
    if (!(camera instanceof PerspectiveCamera) && !(camera instanceof OrthographicCamera)) return;
    const center = bounds.getCenter(new Vector3());
    const diameter = Math.max(bounds.getSize(new Vector3()).length(), 1);
    let distance = diameter * 2;
    if (camera instanceof PerspectiveCamera) {
      camera.aspect = size.width / size.height;
      const vertical = camera.fov * Math.PI / 180;
      const horizontal = 2 * Math.atan(Math.tan(vertical / 2) * camera.aspect);
      distance = diameter / (2 * Math.sin(Math.min(vertical, horizontal) / 2)) * 1.15;
    } else {
      const aspect = size.width / size.height;
      const halfHeight = diameter * .575 / Math.min(aspect, 1);
      camera.top = halfHeight; camera.bottom = -halfHeight;
      camera.left = -halfHeight * aspect; camera.right = halfHeight * aspect;
      camera.zoom = 1;
    }
    const target = controls && 'target' in controls ? (controls as OrbitControls).target : center;
    const direction = fitted.current === null ? new Vector3(1,-1,1).normalize() : camera.position.clone().sub(target).normalize();
    if (fitted.current === null) camera.up.set(0,0,1);
    camera.position.copy(center).add(direction.multiplyScalar(distance));
    camera.near = Math.max(distance / 10000, .001); camera.far = distance * 100;
    camera.lookAt(center); camera.updateProjectionMatrix();
    if (controls && 'target' in controls) {
      const orbit = controls as OrbitControls;
      orbit.target.copy(center); orbit.update();
    }
    fitted.current = request; fitMode.current = true; invalidate();
  }, [bounds, request, get, size.width, size.height]);
  return null;
}
