'use client';

import React, { useEffect, useRef } from 'react';
import { createOrbScene } from '@/lib/orbScene';

export default function HologramCanvas() {
  const mountRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!mountRef.current) return;

    // Initialize the 3D scene; dispose() releases all resources on unmount
    const scene = createOrbScene(mountRef.current);

    return () => {
      scene.dispose();
    };
  }, []);

  return (
    <div 
      ref={mountRef} 
      className="absolute inset-0 w-full h-full z-0 pointer-events-none overflow-hidden rounded-3xl"
    />
  );
}