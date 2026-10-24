import * as THREE from 'three';

export interface OrbSceneApi {
  /** Manual rotation delta (radians). */
  rotateBy(deltaX: number, deltaY: number): void;
  /** Multiply zoom by a factor. */
  zoomBy(factor: number): void;
  zoomIn(): void;
  zoomOut(): void;
  resetView(): void;
  /** Stops the render loop and releases all GPU/JS resources. */
  dispose(): void;
}

export function createOrbScene(container: HTMLDivElement): OrbSceneApi {
  // 1. Scene, Camera, Renderer Setup
  const scene = new THREE.Scene();
  
  const camera = new THREE.PerspectiveCamera(
    60,
    container.clientWidth / container.clientHeight,
    0.1,
    1000
  );
  camera.position.z = 5;

  const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.appendChild(renderer.domElement);

  // 2. Create Particle Sphere Geometry (Hologram Orb)
  const particleCount = 1200;
  const geometry = new THREE.BufferGeometry();
  const positions = new Float32Array(particleCount * 3);

  const radius = 1.8;
  for (let i = 0; i < particleCount * 3; i += 3) {
    // Distribute points spherically using Fibonacci/random distribution
    const phi = Math.acos(-1 + (2 * Math.random()));
    const theta = Math.random() * Math.PI * 2;

    positions[i] = radius * Math.cos(theta) * Math.sin(phi);
    positions[i + 1] = radius * Math.sin(theta) * Math.sin(phi);
    positions[i + 2] = radius * Math.cos(phi);
  }

  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));

  // 3. Material & Particle Styling (Cyan Glow)
  const material = new THREE.PointsMaterial({
    size: 0.035,
    color: new THREE.Color('#06b6d4'), // Cyan color matching Core Gateway A
    transparent: true,
    opacity: 0.8,
    blending: THREE.AdditiveBlending,
  });

  const particleSphere = new THREE.Points(geometry, material);
  scene.add(particleSphere);

  // 4. Mouse Interaction Physics
  let mouseX = 0;
  let mouseY = 0;
  let targetRotationX = 0;
  let targetRotationY = 0;

  const handleMouseMove = (event: MouseEvent) => {
    const rect = container.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    
    mouseX = (x / container.clientWidth) * 2 - 1;
    mouseY = -(y / container.clientHeight) * 2 + 1;
  };

  window.addEventListener('mousemove', handleMouseMove);

  // 5. Responsive Resize Handling
  const handleResize = () => {
    if (!container) return;
    camera.aspect = container.clientWidth / container.clientHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(container.clientWidth, container.clientHeight);
  };

  window.addEventListener('resize', handleResize);

  // 6. Manual Control State (gestures / keyboard)
  let manualRotX = 0;
  let manualRotY = 0;
  let zoom = 1;
  const MIN_ZOOM = 0.4;
  const MAX_ZOOM = 2.5;

  // 7. Animation Loop
  let animationFrameId: number;
  const clock = new THREE.Clock();

  const animate = () => {
    animationFrameId = requestAnimationFrame(animate);

    const elapsedTime = clock.getElapsedTime();

    // Auto-rotation + manual offsets (gestures) + mouse parallax (LERP physics)
    targetRotationX = mouseY * 0.3 + manualRotX + elapsedTime * 0.1;
    targetRotationY = mouseX * 0.3 + manualRotY + elapsedTime * 0.15;

    particleSphere.rotation.x += (targetRotationX - particleSphere.rotation.x) * 0.05;
    particleSphere.rotation.y += (targetRotationY - particleSphere.rotation.y) * 0.05;

    particleSphere.scale.setScalar(zoom);

    renderer.render(scene, camera);
  };

  animate();

  // 8. Strict Cleanup Function (Obliterates memory & context to prevent crashes)
  const dispose = () => {
    cancelAnimationFrame(animationFrameId);
    window.removeEventListener('mousemove', handleMouseMove);
    window.removeEventListener('resize', handleResize);

    geometry.dispose();
    material.dispose();
    renderer.dispose();

    if (container.contains(renderer.domElement)) {
      container.removeChild(renderer.domElement);
    }
  };

  return {
    rotateBy(deltaX, deltaY) {
      manualRotY += deltaX;
      manualRotX += deltaY;
    },
    zoomBy(factor) {
      zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom * factor));
    },
    zoomIn() {
      this.zoomBy(1.15);
    },
    zoomOut() {
      this.zoomBy(1 / 1.15);
    },
    resetView() {
      zoom = 1;
      manualRotX = 0;
      manualRotY = 0;
    },
    dispose,
  };
}
