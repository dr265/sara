import * as THREE from 'three';

export interface TopologySceneApi {
  /** Set the health color of a node by id, e.g. '#10b981' or '#ef4444'. */
  setNodeStatus(nodeId: string, color: string): void;
  /** Re-derive node colors from the current topology state. */
  setStatuses(statuses: Record<string, string>): void;
  dispose(): void;
}

interface NodeSpec {
  id: string;
  label: string;
  position: [number, number, number];
  size: number;
  wireframe: boolean;
}

const NODE_SPECS: NodeSpec[] = [
  { id: 'node_core', label: 'SARA VOICE CORE', position: [0, 0, 0], size: 0.85, wireframe: false },
  { id: 'node_tel', label: 'TELEPHONY', position: [-3.4, 1.7, -1.5], size: 0.5, wireframe: true },
  { id: 'node_mem', label: 'MEMORY', position: [3.4, 1.7, -1.5], size: 0.5, wireframe: true },
  { id: 'node_know', label: 'KNOWLEDGE BASE', position: [3.4, -1.7, -1.5], size: 0.5, wireframe: true },
  { id: 'node_comm', label: 'CUSTOMER ENGINE', position: [-3.4, -1.7, -1.5], size: 0.5, wireframe: true },
];

const EDGES: Array<[string, string]> = [
  ['node_core', 'node_tel'],
  ['node_core', 'node_mem'],
  ['node_core', 'node_know'],
  ['node_core', 'node_comm'],
];

const ZINC = new THREE.Color('#52525b');
const EMERALD = new THREE.Color('#10b981');
const AMBER = new THREE.Color('#f59e0b');
const RED = new THREE.Color('#ef4444');

export function createVoiceTopologyScene(
  container: HTMLDivElement,
): TopologySceneApi {
  const scene = new THREE.Scene();

  const camera = new THREE.PerspectiveCamera(
    45,
    container.clientWidth / container.clientHeight,
    0.1,
    100,
  );
  camera.position.set(0, 2, 10);
  camera.lookAt(0, 0, 0);

  const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.appendChild(renderer.domElement);

  scene.add(new THREE.AmbientLight(0xffffff, 0.5));
  const key = new THREE.PointLight(0xffffff, 40, 0, 2);
  key.position.set(10, 10, 10);
  scene.add(key);

  const group = new THREE.Group();
  scene.add(group);

  const nodeMeshes = new Map<string, THREE.Mesh>();
  const nodeLabels = new Map<string, HTMLDivElement>();

  // --- nodes -------------------------------------------------------------
  for (const spec of NODE_SPECS) {
    const geo = new THREE.OctahedronGeometry(spec.size, 0);
    const mat = new THREE.MeshStandardMaterial({
      color: ZINC.clone(),
      wireframe: spec.wireframe,
      transparent: true,
      opacity: 0.85,
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.position.set(...spec.position);
    group.add(mesh);
    nodeMeshes.set(spec.id, mesh);

    // HTML label floating under the node (projected to screen space)
    const label = document.createElement('div');
    label.textContent = spec.label;
    label.style.cssText =
      'position:absolute;transform:translate(-50%,-50%);' +
      'background:rgba(9,9,11,0.85);border:1px solid #27272a;color:#a1a1aa;' +
      'font:10px ui-monospace,monospace;padding:2px 6px;border-radius:4px;' +
      'white-space:nowrap;pointer-events:none;user-select:none;backdrop-filter:blur(4px);';
    container.appendChild(label);
    nodeLabels.set(spec.id, label);
  }

  // --- edges -------------------------------------------------------------
  const edgeGeos: THREE.BufferGeometry[] = [];
  for (const [a, b] of EDGES) {
    const pa = new THREE.Vector3(...NODE_SPECS.find((n) => n.id === a)!.position);
    const pb = new THREE.Vector3(...NODE_SPECS.find((n) => n.id === b)!.position);
    const geo = new THREE.BufferGeometry().setFromPoints([pa, pb]);
    const mat = new THREE.LineBasicMaterial({
      color: new THREE.Color('#3f3f46'),
      transparent: true,
      opacity: 0.45,
    });
    group.add(new THREE.Line(geo, mat));
    edgeGeos.push(geo);
  }

  // --- animation ---------------------------------------------------------
  let raf = 0;
  const clock = new THREE.Clock();
  const v = new THREE.Vector3();

  const project = () => {
    const rect = container.getBoundingClientRect();
    for (const [id, mesh] of nodeMeshes) {
      const label = nodeLabels.get(id);
      if (!label) continue;
      mesh.getWorldPosition(v);
      v.project(camera);
      const x = (v.x * 0.5 + 0.5) * rect.width;
      const y = (-v.y * 0.5 + 0.5) * rect.height;
      label.style.left = `${x}px`;
      label.style.top = `${y + 18}px`;
      label.style.opacity = v.z < 1 ? '1' : '0';
    }
  };

  const animate = () => {
    raf = requestAnimationFrame(animate);
    const t = clock.getElapsedTime();

    group.rotation.y = t * 0.18;
    // gentle bob per node
    nodeMeshes.forEach((mesh, id) => {
      const spec = NODE_SPECS.find((n) => n.id === id)!;
      mesh.position.y = spec.position[1] + Math.sin(t * 1.2 + spec.size * 10) * 0.08;
      if (id === 'node_core') mesh.rotation.y = t * 0.4;
    });

    project();
    renderer.render(scene, camera);
  };
  animate();

  const handleResize = () => {
    camera.aspect = container.clientWidth / container.clientHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(container.clientWidth, container.clientHeight);
  };
  window.addEventListener('resize', handleResize);

  // --- status colors -----------------------------------------------------
  const setStatuses = (statuses: Record<string, string>) => {
    nodeMeshes.forEach((mesh, id) => {
      const mat = mesh.material as THREE.MeshStandardMaterial;
      const next = statuses[id];
      mat.color.set(next ? new THREE.Color(next) : ZINC);
    });
  };

  return {
    setNodeStatus(nodeId, color) {
      setStatuses({ [nodeId]: color });
    },
    setStatuses,
    dispose() {
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', handleResize);

      nodeMeshes.forEach((mesh) => {
        mesh.geometry.dispose();
        (mesh.material as THREE.Material).dispose();
      });
      edgeGeos.forEach((g) => g.dispose());
      nodeLabels.forEach((label) => label.remove());
      renderer.dispose();
      if (container.contains(renderer.domElement)) {
        container.removeChild(renderer.domElement);
      }
    },
  };
}

// Re-export common status colors for consumers
export const NODE_STATUS_COLORS: Record<string, string> = {
  CONNECTED: '#10b981',
  DEGRADED: '#f59e0b',
  ERROR: '#ef4444',
  NOT_CONFIGURED: '#52525b',
};

export { EMERALD, AMBER, RED, ZINC };
