import { useEffect, useState } from "react";
import type { BufferGeometry, Box3 } from "three";
import { Box3 as ThreeBox3, Vector3 } from "three";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { authFetch } from "../auth";

const API_BASE = import.meta.env.VITE_API_BASE || "";

export function useCADModel(stlUrl: string) {
  const [geometry, setGeometry] = useState<BufferGeometry | null>(null);
  const [boundingBox, setBoundingBox] = useState<Box3 | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [centerOffset, setCenterOffset] = useState<[number, number, number]>([0, 0, 0]);
  useEffect(()=>()=>{geometry?.dispose();},[geometry]);

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const loader = new STLLoader();
    const target = /^https?:\/\//.test(stlUrl) ? stlUrl : `${API_BASE}${stlUrl}`;

    const load = () => {
      setIsLoading(true); setError(null);
      return authFetch(target, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.arrayBuffer();
      })
      .then((buffer) => {
        const nextGeometry = loader.parse(buffer);
        if (!active) {
          nextGeometry.dispose();
          return;
        }
        nextGeometry.computeBoundingBox();
        const box = nextGeometry.boundingBox as ThreeBox3;
        const center = new Vector3();
        box.getCenter(center);
        nextGeometry.translate(-center.x, -center.y, -center.z);
        nextGeometry.computeBoundingBox();
        setCenterOffset([-center.x, -center.y, -center.z]);
        setGeometry(nextGeometry);
        setBoundingBox(nextGeometry.boundingBox);
        setIsLoading(false);
      })
      .catch((reason: unknown) => {
        if (!active || (reason instanceof Error && reason.name === "AbortError")) return;
        setError("模型文件无法加载");
        setIsLoading(false);
      });
    };
    void load();

    return () => {
      active = false;
      controller.abort();
    };
  }, [stlUrl]);

  return { geometry, boundingBox, isLoading, error, centerOffset };
}
