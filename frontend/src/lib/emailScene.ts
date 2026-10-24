import * as THREE from 'three';

export interface EmailSceneApi {
  /** Stops the render loop and releases all GPU/JS resources. */
  dispose(): void;
}

interface MailPacket {
  mesh: THREE.Mesh;
  from: THREE.Vector3;
  to: THREE.Vector3;
  t: number;
  speed: number;
}

/**
 * Email dispatch network:
 *  - a glowing reactor core in the center,
 *  - client nodes orbiting on a tilted ring,
 *  - bezier light-beams from the core to every node,
 *  - small glowing "mail packets" constantly travelling along the beams.
 */
export function createEmailScene(container: HTMLDivElement): EmailSceneApi {
  const scene = new THREE.Scene();

  const camera = new THREE.PerspectiveCamera(
    55,
    Math.max(container.clientWidth, 1) / Math.max(container.clientHeight, 1),
    0.1,
    100
  );
  camera.position.set(0, 2.6, 7.2);
  camera.lookAt(0, 0, 0);

  const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.appendChild(renderer.domElement);

  // ---- Core: icosahedron with pulsing emissive glow ----------------------
  const coreGroup = new THREE.Group();
  const core = new THREE.Mesh(
    new THREE.IcosahedronGeometry(0.85, 2),
    new THREE.MeshStandardMaterial({
      color: 0x0ea5a4,
      emissive: 0x10b3a6,
      emissiveIntensity: 1.4,
      roughness: 0.25,
      metalness: 0.6,
      transparent: true,
      opacity: 0.95,
    })
  );
  coreGroup.add(core);

  const coreWire = new THREE.Mesh(
    new THREE.IcosahedronGeometry(1.15, 1),
    new THREE.MeshBasicMaterial({ color: 0x5eead4, wireframe: true, transparent: true, opacity: 0.35 })
  );
  coreGroup.add(coreWire);
  scene.add(coreGroup);

  const coreLight = new THREE.PointLight(0x2dd4bf, 2.4, 14);
  scene.add(coreLight);
  scene.add(new THREE.AmbientLight(0x334155, 0.9));
  const rim = new THREE.DirectionalLight(0x94a3b8, 0.7);
  rim.position.set(-4, 6, 4);
  scene.add(rim);

  // ---- Client nodes on an orbiting ring ----------------------------------
  const NODE_COUNT = 9;
  const ringGroup = new THREE.Group();
  ringGroup.rotation.x = 0.42;
  scene.add(ringGroup);

  const orbitRadius = 3.1;
  const nodes: { group: THREE.Group; angle: number; y: number; speed: number }[] = [];
  const nodeGeometry = new THREE.BoxGeometry(0.34, 0.24, 0.05);
  const nodeMaterial = new THREE.MeshStandardMaterial({
    color: 0x0f766e,
    emissive: 0x14b8a6,
    emissiveIntensity: 0.7,
    metalness: 0.55,
    roughness: 0.3,
  });

  for (let i = 0; i < NODE_COUNT; i++) {
    const group = new THREE.Group();
    const body = new THREE.Mesh(nodeGeometry, nodeMaterial);
    group.add(body);

    // envelope "flap" line
    const flap = new THREE.Mesh(
      new THREE.ConeGeometry(0.17, 0.16, 3),
      new THREE.MeshBasicMaterial({ color: 0x99f6e4, transparent: true, opacity: 0.85 })
    );
    flap.rotation.x = Math.PI;
    flap.position.z = 0.04;
    group.add(flap);

    ringGroup.add(group);
    nodes.push({
      group,
      angle: (i / NODE_COUNT) * Math.PI * 2,
      y: Math.sin(i * 2.4) * 0.35,
      speed: 0.12 + (i % 3) * 0.02,
    });
  }

  // ---- Dashed orbit ring --------------------------------------------------
  const ringCurve = new THREE.EllipseCurve(0, 0, orbitRadius, orbitRadius, 0, Math.PI * 2, false, 0);
  const ringPts = ringCurve.getPoints(120).map((p) => new THREE.Vector3(p.x, 0, p.y));
  const ringGeometry = new THREE.BufferGeometry().setFromPoints(ringPts);
  const ringLine = new THREE.Line(
    ringGeometry,
    new THREE.LineDashedMaterial({ color: 0x2dd4bf, dashSize: 0.18, gapSize: 0.14, transparent: true, opacity: 0.4 })
  );
  ringLine.computeLineDistances();
  ringGroup.add(ringLine);

  // ---- Beams + travelling mail packets ------------------------------------
  const beams: { line: THREE.Line; nodeIndex: number }[] = [];
  const packets: MailPacket[] = [];
  const packetGeometry = new THREE.SphereGeometry(0.05, 10, 10);
  const packetMaterial = new THREE.MeshBasicMaterial({ color: 0xfef08a });

  function makeBeam(nodeIndex: number): void {
    const start = new THREE.Vector3(0, 0, 0);
    const end = new THREE.Vector3(
      Math.cos(nodes[nodeIndex].angle) * orbitRadius,
      nodes[nodeIndex].y,
      Math.sin(nodes[nodeIndex].angle) * orbitRadius
    );
    const mid = start.clone().lerp(end, 0.5).add(new THREE.Vector3(0, 1.1, 0));
    const curve = new THREE.QuadraticBezierCurve3(start, mid, end);
    const geometry = new THREE.BufferGeometry().setFromPoints(curve.getPoints(40));
    const line = new THREE.Line(
      geometry,
      new THREE.LineBasicMaterial({ color: 0x14b8a6, transparent: true, opacity: 0.22 })
    );
    ringGroup.add(line);
    beams.push({ line, nodeIndex });

    const mesh = new THREE.Mesh(packetGeometry, packetMaterial.clone());
    (mesh.material as THREE.MeshBasicMaterial).opacity = 0.9;
    (mesh.material as THREE.MeshBasicMaterial).transparent = true;
    ringGroup.add(mesh);
    packets.push({
      mesh,
      from: start,
      to: end,
      t: Math.random(),
      speed: 0.25 + Math.random() * 0.4,
    });
  }
  for (let i = 0; i < NODE_COUNT; i++) makeBeam(i);

  // ---- Ambient drifting particles -----------------------------------------
  const particleCount = 260;
  const positions = new Float32Array(particleCount * 3);
  for (let i = 0; i < particleCount * 3; i += 3) {
    positions[i] = (Math.random() - 0.5) * 12;
    positions[i + 1] = (Math.random() - 0.5) * 7;
    positions[i + 2] = (Math.random() - 0.5) * 12;
  }
  const particleGeometry = new THREE.BufferGeometry();
  particleGeometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  const particles = new THREE.Points(
    particleGeometry,
    new THREE.PointsMaterial({ size: 0.03, color: 0x5eead4, transparent: true, opacity: 0.5 })
  );
  scene.add(particles);

  // ---- Interaction: gentle pointer parallax --------------------------------
  let targetRotY = 0;
  let targetRotX = 0;
  function onPointerMove(e: PointerEvent): void {
    const rect = container.getBoundingClientRect();
    const nx = (e.clientX - rect.left) / rect.width - 0.5;
    const ny = (e.clientY - rect.top) / rect.height - 0.5;
    targetRotY = nx * 0.5;
    targetRotX = ny * 0.3;
  }
  container.addEventListener('pointermove', onPointerMove);

  // ---- Resize --------------------------------------------------------------
  function onResize(): void {
    const w = Math.max(container.clientWidth, 1);
    const h = Math.max(container.clientHeight, 1);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  }
  const resizeObserver = new ResizeObserver(onResize);
  resizeObserver.observe(container);

  // ---- Render loop ----------------------------------------------------------
  const clock = new THREE.Clock();
  let raf = 0;
  let disposed = false;

  function animate(): void {
    if (disposed) return;
    raf = requestAnimationFrame(animate);
    const dt = Math.min(clock.getDelta(), 0.05);
    const elapsed = clock.elapsedTime;

    // core pulse
    const pulse = 1 + Math.sin(elapsed * 2.2) * 0.06;
    core.scale.setScalar(pulse);
    coreWire.rotation.y += dt * 0.4;
    coreWire.rotation.x += dt * 0.15;
    coreGroup.rotation.y += dt * 0.25;
    coreLight.intensity = 2.2 + Math.sin(elapsed * 2.2) * 0.7;

    // orbiting nodes
    for (const node of nodes) {
      node.angle += dt * node.speed * 0.35;
      node.group.position.set(
        Math.cos(node.angle) * orbitRadius,
        node.y + Math.sin(elapsed * 1.4 + node.angle * 3) * 0.08,
        Math.sin(node.angle) * orbitRadius
      );
      node.group.rotation.y = -node.angle + Math.PI / 2;
    }

    // packets travel core -> node, then restart
    for (const packet of packets) {
      packet.t += dt * packet.speed;
      if (packet.t > 1) {
        packet.t = 0;
        const node = nodes[Math.floor(Math.random() * NODE_COUNT)];
        packet.from.set(0, 0, 0);
        packet.to.set(Math.cos(node.angle) * orbitRadius, node.y, Math.sin(node.angle) * orbitRadius);
      }
      const p = packet.from.clone().lerp(packet.to, packet.t);
      p.y += Math.sin(packet.t * Math.PI) * 1.1;
      packet.mesh.position.copy(p);
      const m = packet.mesh.material as THREE.MeshBasicMaterial;
      m.opacity = 0.9 * Math.sin(packet.t * Math.PI) + 0.1;
    }

    // parallax easing
    ringGroup.rotation.y += (targetRotY - ringGroup.rotation.y) * 0.04;
    scene.rotation.x += (targetRotX - scene.rotation.x) * 0.04;
    particles.rotation.y += dt * 0.02;

    renderer.render(scene, camera);
  }
  animate();

  return {
    dispose(): void {
      disposed = true;
      cancelAnimationFrame(raf);
      resizeObserver.disconnect();
      container.removeEventListener('pointermove', onPointerMove);
      scene.traverse((obj) => {
        const mesh = obj as THREE.Mesh;
        if (mesh.geometry) mesh.geometry.dispose();
        const material = mesh.material as THREE.Material | THREE.Material[] | undefined;
        if (Array.isArray(material)) material.forEach((m) => m.dispose());
        else material?.dispose();
      });
      renderer.dispose();
      if (renderer.domElement.parentElement === container) {
        container.removeChild(renderer.domElement);
      }
    },
  };
}
