import { useState, useEffect, useRef } from "react";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import type { BufferGeometry, Box3 } from "three";
import { Box3 as ThreeBox3, Vector3 } from "three";

export function useCADModel(stlUrl: string | null) {
  const [geometry, setGeometry] = useState<BufferGeometry | null>(null);
  const [boundingBox, setBoundingBox] = useState<Box3 | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [centerOffset, setCenterOffset] = useState<[number, number, number]>([0, 0, 0]);
  const prevGeoRef = useRef<BufferGeometry | null>(null);

  useEffect(() => {
    if (!stlUrl) {
      if (prevGeoRef.current) {
        prevGeoRef.current.dispose();
        prevGeoRef.current = null;
      }
      setGeometry(null);
      setBoundingBox(null);
      setCenterOffset([0, 0, 0]);
      return;
    }

    setIsLoading(true);
    const loader = new STLLoader();

    loader.load(
      stlUrl,
      (geo) => {
        // Dispose previous geometry to free GPU memory
        if (prevGeoRef.current) {
          prevGeoRef.current.dispose();
        }

        geo.computeBoundingBox();
        const bb = geo.boundingBox as ThreeBox3;
        const center = new Vector3();
        bb.getCenter(center);
        geo.translate(-center.x, -center.y, -center.z);
        setCenterOffset([-center.x, -center.y, -center.z]);

        geo.computeBoundingBox();
        prevGeoRef.current = geo;
        setGeometry(geo);
        setBoundingBox(geo.boundingBox);
        setIsLoading(false);
      },
      undefined,
      (err) => {
        console.error("STL load error:", err);
        setIsLoading(false);
      },
    );

    return () => {
      // Cleanup on unmount
      if (prevGeoRef.current) {
        prevGeoRef.current.dispose();
        prevGeoRef.current = null;
      }
    };
  }, [stlUrl]);

  return { geometry, boundingBox, isLoading, centerOffset };
}
