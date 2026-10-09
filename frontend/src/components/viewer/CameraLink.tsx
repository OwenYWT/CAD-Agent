import { useEffect, useLayoutEffect, useRef } from 'react';
import { useThree } from '@react-three/fiber';
import type { OrbitControls } from 'three-stdlib';
import type { Camera } from 'three';
export interface CameraPose {source:string;position:number[];target:number[];up:number[];zoom:number}
function applyPose(camera:Camera,orbit:OrbitControls,pose:CameraPose) {
  camera.position.fromArray(pose.position);camera.up.fromArray(pose.up);
  if ('zoom' in camera) camera.zoom=pose.zoom;
  orbit.target.fromArray(pose.target);orbit.update();
  if ('updateProjectionMatrix' in camera) (camera as Camera & {updateProjectionMatrix:()=>void}).updateProjectionMatrix();
}
export default function CameraLink({id,pose,onPose}: {id:string;pose:CameraPose|null;onPose:(pose:CameraPose)=>void}) {
  const {camera,controls,invalidate}=useThree(),applying=useRef(false);
  useLayoutEffect(()=>{
    if (!pose || pose.source===id || !controls || !('target' in controls)) return;
    applying.current=true;
    applyPose(camera,controls as OrbitControls,pose);invalidate();applying.current=false;
  },[pose,id,camera,controls,invalidate]);
  useEffect(()=>{
    if (!controls || !('target' in controls)) return;
    const orbit=controls as OrbitControls;
    const change=()=>{if (!applying.current) onPose({source:id,position:camera.position.toArray(),target:orbit.target.toArray(),up:camera.up.toArray(),zoom:camera.zoom});};
    orbit.addEventListener('change',change);return()=>orbit.removeEventListener('change',change);
  },[id,camera,controls,onPose]);
  return null;
}
