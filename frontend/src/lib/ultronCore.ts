/**
 * SARA ULTRON - holographic core.
 *
 * The visual identity of the supplied `sara_ultron.html` (the JARVIS AI-core
 * hologram) rebuilt as a real scene module rather than a standalone page: the
 * same bi-colour particle core, chaotic network web, plasma arcs, residual
 * sparks, planetary rings and polar shell, the same bloom pass, the same
 * deep-space palette (#010204 void, cyan #00f0ff, amber #ffaa00).
 *
 * The difference from the source page is that nothing here is decoratively
 * animated. Every layer is bound to a real signal the backend actually emits:
 *
 *   layer            driven by
 *   ---------------  -----------------------------------------------------
 *   particle core    run energy - spin rate and density track real progress
 *   network web      PLANNING   - the web brightens while a plan is built
 *   plasma arcs      EXECUTING  - arc count and flicker scale with real work
 *   planetary rings  VERIFYING  - the structured layer lights up while the
 *                                 report is assembled from real step results
 *   planetary rings  LISTENING  - a slow travelling pulse, one bar at a time
 *   polar shell      SPEAKING   - pulses with the utterance, once per phrase
 *   shell sweep      VISION     - a single bright pass while a screen is read
 *   shockwave        every tool_result event, success or failure
 *   amber shift      failures - decays back to neutral, never sticks
 *
 * A core that is idle looks idle because the backend says the run is idle, not
 * because a timer decided to look calm.
 *
 * Performance: geometry counts, pixel ratio and the render loop all adapt to
 * the device, the page visibility and whether the canvas is on screen. The
 * scene stops rendering entirely when the tab is hidden or the core is scrolled
 * out of view, and `dispose()` releases every GPU resource and listener.
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';

/** Real run phases, mirroring the backend's lifecycle. */
export type UltronCoreMode =
  | 'idle'
  | 'understanding'
  | 'planning'
  | 'executing'
  | 'verifying'
  | 'listening'
  | 'vision'
  | 'speaking'
  | 'error';

export interface UltronCoreState {
  mode: UltronCoreMode;
  /** Real completion of the current plan, 0..1. 0 whenever no run is active. */
  progress: number;
}

export interface UltronCoreApi {
  setState(next: UltronCoreState): void;
  /** A real backend event landed: send a shockwave outward from the core. */
  pulse(strength?: number): void;
  /** Mark one real outcome. `fail` shifts the core amber, then decays. */
  flag(kind: 'ok' | 'fail'): void;
  /** Suspend/resume rendering without tearing the scene down. */
  setSuspended(suspended: boolean): void;
  dispose(): void;
}

/* ── palette (directly from the supplied HTML) ─────────────────────────────── */
const VOID = 0x010204;
const BLUE = 0x0088ff;
const CYAN = 0x00ffff;
const ORANGE = 0xff4400;
const YELLOW = 0xffcc00;

const CORE_RADIUS = 3.5;
const CONNECT_DISTANCE = 1.1;

/* ── quality tiers: the source page's counts are the high tier ─────────────── */
interface Quality {
  particles: number;
  nodes: number;
  arcs: number;
  sparks: number;
  pixelRatio: number;
}

function pickQuality(container: HTMLElement): Quality {
  const nav = navigator as Navigator & { deviceMemory?: number };
  const memory = nav.deviceMemory ?? 8;
  const cores = navigator.hardwareConcurrency ?? 8;
  const small = Math.min(container.clientWidth, container.clientHeight) < 420;
  const lowPower =
    (typeof memory === 'number' && memory <= 4) ||
    (typeof cores === 'number' && cores <= 4) ||
    small;

  if (lowPower) {
    return { particles: 3200, nodes: 260, arcs: 6, sparks: 140, pixelRatio: 1.25 };
  }
  // Mid tier for laptops: visually indistinguishable at this bloom radius,
  // roughly half the per-frame vertex work.
  if (memory <= 8 || cores <= 8) {
    return { particles: 8000, nodes: 460, arcs: 12, sparks: 260, pixelRatio: 1.4 };
  }
  return { particles: 15000, nodes: 700, arcs: 20, sparks: 400, pixelRatio: 1.6 };
}

function createGlowTexture(inner: string, outer: string): THREE.CanvasTexture {
  const canvas = document.createElement('canvas');
  canvas.width = 64;
  canvas.height = 64;
  const ctx = canvas.getContext('2d');
  if (ctx) {
    const gradient = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
    gradient.addColorStop(0, `rgba(${inner}, 1)`);
    gradient.addColorStop(0.2, `rgba(${inner}, 0.8)`);
    gradient.addColorStop(0.5, `rgba(${outer}, 0.3)`);
    gradient.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 64, 64);
  }
  return new THREE.CanvasTexture(canvas);
}

/** One energy band: the bi-colour split is the identity of the core. */
function bandColor(x: number): THREE.Color {
  return x < 0 ? new THREE.Color(BLUE) : new THREE.Color(ORANGE);
}

/**
 * The core's real entry point.
 *
 * Scene construction can genuinely fail - no WebGL context, a blocklisted
 * driver, exhausted GPU memory - and none of those may take the command center
 * down with them. A failure is reported in place, in the container, and the
 * returned API is an inert stand-in so every caller stays valid.
 */
export function createUltronCore(container: HTMLElement): UltronCoreApi {
  try {
    return buildUltronCore(container);
  } catch (err: unknown) {
    const reason = err instanceof Error ? err.message : String(err);
    // Developer-readable in the console, user-readable in the DOM.
    console.error('[ultron-core] holographic core unavailable:', reason);
    renderUnavailable(container, reason);
    return {
      setState() {},
      pulse() {},
      flag() {},
      setSuspended() {},
      dispose() {
        container.replaceChildren();
      },
    };
  }
}

/** Replace the container's contents with an honest notice. */
function renderUnavailable(container: HTMLElement, reason: string): void {
  const notice = document.createElement('div');
  notice.style.cssText = [
    'position:absolute', 'inset:0', 'display:flex', 'flex-direction:column',
    'align-items:center', 'justify-content:center', 'gap:8px', 'padding:24px',
    'text-align:center', 'font-family:ui-monospace,SFMono-Regular,Menlo,monospace',
    'font-size:11px', 'letter-spacing:1.2px', 'color:#ffaa00', 'line-height:1.7',
  ].join(';');
  const headline = document.createElement('div');
  headline.textContent = 'HOLOGRAPHIC CORE UNAVAILABLE — NO WEBGL CONTEXT';
  const detail = document.createElement('div');
  detail.style.cssText = 'color:#617080;letter-spacing:0';
  detail.textContent = reason;
  const reassurance = document.createElement('div');
  reassurance.style.cssText = 'color:#617080;letter-spacing:0';
  reassurance.textContent =
    'Every other control on this page works normally.';
  notice.append(headline, detail, reassurance);
  // replaceChildren also clears a canvas that was appended before the failure.
  container.replaceChildren(notice);
}

function buildUltronCore(container: HTMLElement): UltronCoreApi {
  const quality = pickQuality(container);
  const reduceMotion =
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(VOID, 0.015);

  const width = Math.max(container.clientWidth, 1);
  const height = Math.max(container.clientHeight, 1);

  const camera = new THREE.PerspectiveCamera(55, width / height, 0.1, 1000);
  // Pulled back from the source page's z=10: that framing assumed a full-window
  // viewport, and in this stage's wider-than-tall frame the core filled the
  // panel and clipped its own outer shell and rings.
  camera.position.set(0, 0, 12);

  const renderer = new THREE.WebGLRenderer({
    antialias: true,
    alpha: true,
    powerPreference: 'high-performance',
  });
  renderer.setSize(width, height);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, quality.pixelRatio));
  renderer.setClearColor(VOID, 0);
  container.appendChild(renderer.domElement);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.04;
  controls.rotateSpeed = 0.6;
  controls.enablePan = false;
  controls.enableZoom = true;
  controls.minDistance = 5;
  controls.maxDistance = 18;
  controls.autoRotate = false;

  /* ── post-processing ────────────────────────────────────────────────────── */
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  // Threshold raised above the source page's 0.5 on purpose: at 0.5 the dense
  // centre of the core bloomed into a solid white disc, which buried the
  // blue/amber split that is the whole point of the visual. 0.72 keeps the
  // glow on the genuinely hot pixels only.
  const bloom = new UnrealBloomPass(
    new THREE.Vector2(width, height),
    0.4, // intensity
    0.45, // radius
    0.72, // threshold
  );
  composer.addPass(bloom);

  /* ── groups ─────────────────────────────────────────────────────────────── */
  const globalGroup = new THREE.Group();
  const coreGroup = new THREE.Group();
  const networkGroup = new THREE.Group();
  const lightningGroup = new THREE.Group();
  const structureGroup = new THREE.Group();
  globalGroup.add(coreGroup, networkGroup, lightningGroup, structureGroup);
  scene.add(globalGroup);

  const disposables: Array<{ dispose(): void }> = [];
  const particleTexture = createGlowTexture('255,255,255', '200,200,200');
  disposables.push(particleTexture);

  /* ── particle core ──────────────────────────────────────────────────────── */
  const pGeometry = new THREE.BufferGeometry();
  const pPositions = new Float32Array(quality.particles * 3);
  const pColors = new Float32Array(quality.particles * 3);
  const pSizes = new Float32Array(quality.particles);
  // Preallocated: 15k particles without a per-particle colour allocation.
  const scratch = new THREE.Color();
  const blueEnd = new THREE.Color(BLUE);
  const cyanEnd = new THREE.Color(CYAN);
  const orangeEnd = new THREE.Color(ORANGE);
  const yellowEnd = new THREE.Color(YELLOW);

  for (let i = 0; i < quality.particles; i += 1) {
    let r = Math.pow(Math.random(), 1.2) * CORE_RADIUS;
    // 30% of the points are pulled onto the outer shell, which is what gives
    // the core its readable silhouette rather than a uniform fuzzy ball.
    if (Math.random() > 0.7) r = CORE_RADIUS + (Math.random() - 0.5) * 0.8;

    const u = Math.random();
    const v = Math.random();
    const theta = u * 2.0 * Math.PI;
    const phi = Math.acos(2.0 * v - 1.0);

    const x = r * Math.sin(phi) * Math.cos(theta);
    const y = r * Math.sin(phi) * Math.sin(theta);
    const z = r * Math.cos(phi);

    pPositions[i * 3] = x;
    pPositions[i * 3 + 1] = y;
    pPositions[i * 3 + 2] = z;

    const split = (x - y * 0.2) / CORE_RADIUS + (Math.random() - 0.5) * 0.3;
    if (split < 0) scratch.lerpColors(blueEnd, cyanEnd, Math.random());
    else scratch.lerpColors(orangeEnd, yellowEnd, Math.random());

    pColors[i * 3] = scratch.r;
    pColors[i * 3 + 1] = scratch.g;
    pColors[i * 3 + 2] = scratch.b;
    pSizes[i] = Math.random() * 0.12 + 0.03;
  }

  pGeometry.setAttribute('position', new THREE.BufferAttribute(pPositions, 3));
  pGeometry.setAttribute('color', new THREE.BufferAttribute(pColors, 3));
  pGeometry.setAttribute('size', new THREE.BufferAttribute(pSizes, 1));

  const particleMaterial = new THREE.ShaderMaterial({
    uniforms: {
      pointTexture: { value: particleTexture },
      // Real reactivity lives here: the shockwave expands the points, and run
      // energy scales them, so the core visually reacts to executed work.
      uSize: { value: 1.0 },
      uDensity: { value: 0.85 },
    },
    // NOTE: `color` is deliberately NOT declared here. With `vertexColors:
    // true` three defines USE_COLOR and injects `attribute vec3 color;` into
    // this ShaderMaterial's prefix, so declaring it again fails to compile with
    // "'color' : redefinition" and the whole core renders as nothing. Only the
    // custom `size` attribute is ours to declare.
    vertexShader: `
      attribute float size;
      uniform float uSize;
      varying vec3 vColor;
      void main() {
        vColor = color;
        vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
        gl_PointSize = size * uSize * (300.0 / -mvPosition.z);
        gl_Position = projectionMatrix * mvPosition;
      }
    `,
    fragmentShader: `
      uniform sampler2D pointTexture;
      uniform float uDensity;
      varying vec3 vColor;
      void main() {
        vec4 texColor = texture2D(pointTexture, gl_PointCoord);
        gl_FragColor = vec4(vColor, uDensity) * texColor;
      }
    `,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    transparent: true,
    vertexColors: true,
  });
  disposables.push(pGeometry, particleMaterial);

  const particleSystem = new THREE.Points(pGeometry, particleMaterial);
  coreGroup.add(particleSystem);

  /* ── network web + starburst ───────────────────────────────────────────── */
  const nodes: THREE.Vector3[] = [];
  for (let i = 0; i < quality.nodes; i += 1) {
    const r = Math.pow(Math.random(), 0.8) * CORE_RADIUS;
    const theta = Math.random() * 2 * Math.PI;
    const phi = Math.acos(2 * Math.random() - 1);
    nodes.push(
      new THREE.Vector3(
        r * Math.sin(phi) * Math.cos(theta),
        r * Math.sin(phi) * Math.sin(theta),
        r * Math.cos(phi),
      ),
    );
  }

  const linePositions: number[] = [];
  const lineColors: number[] = [];
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      if (nodes[i].distanceTo(nodes[j]) < CONNECT_DISTANCE) {
        linePositions.push(nodes[i].x, nodes[i].y, nodes[i].z);
        linePositions.push(nodes[j].x, nodes[j].y, nodes[j].z);
        const base = bandColor((nodes[i].x + nodes[j].x) / 2);
        lineColors.push(base.r, base.g, base.b, base.r, base.g, base.b);
      }
    }
  }
  // Starburst: lines through the origin. Additive blending turns the
  // intersection into a white-hot centre for the eye to land on - but the
  // source page's 150 of them (each drawn with the dense web material) saturate
  // to a flat white disc, so the count is scaled down to a hot point instead.
  const starburstCount = Math.max(24, Math.round(quality.nodes / 8));
  for (let i = 0; i < starburstCount; i += 1) {
    const dir = new THREE.Vector3(
      Math.random() - 0.5,
      Math.random() - 0.5,
      Math.random() - 0.5,
    ).normalize();
    const length = CORE_RADIUS * (0.4 + Math.random() * 0.6);
    const p1 = dir.clone().multiplyScalar(length);
    const p2 = dir.clone().multiplyScalar(-length);
    linePositions.push(p1.x, p1.y, p1.z, p2.x, p2.y, p2.z);
    const c1 = p1.x < 0 ? new THREE.Color(CYAN) : new THREE.Color(YELLOW);
    const c2 = p2.x < 0 ? new THREE.Color(CYAN) : new THREE.Color(YELLOW);
    lineColors.push(c1.r, c1.g, c1.b, c2.r, c2.g, c2.b);
  }

  const networkGeometry = new THREE.BufferGeometry();
  networkGeometry.setAttribute(
    'position',
    new THREE.Float32BufferAttribute(linePositions, 3),
  );
  networkGeometry.setAttribute('color', new THREE.Float32BufferAttribute(lineColors, 3));
  const networkMaterial = new THREE.LineBasicMaterial({
    vertexColors: true,
    blending: THREE.AdditiveBlending,
    transparent: true,
    opacity: 0.2,
    depthWrite: false,
  });
  disposables.push(networkGeometry, networkMaterial);
  networkGroup.add(new THREE.LineSegments(networkGeometry, networkMaterial));

  /* ── plasma arcs ────────────────────────────────────────────────────────── */
  interface Arc {
    group: THREE.Group;
    geometry: THREE.BufferGeometry;
    materials: THREE.LineBasicMaterial[];
    flickerRate: number;
    life: number;
  }

  function createArc(): Arc {
    const points: THREE.Vector3[] = [];
    const startR = CORE_RADIUS * (0.35 + Math.random() * 0.35);
    const startDir = new THREE.Vector3(
      Math.random() - 0.5,
      Math.random() - 0.5,
      Math.random() - 0.5,
    ).normalize();
    const current = startDir.clone().multiplyScalar(startR);
    points.push(current.clone());

    const baseColor = current.x < 0 ? new THREE.Color(CYAN) : new THREE.Color(0xffaa00);
    const mainDir = startDir.clone();

    // Reach trimmed from the source page's 22 x 0.7 (~15 world units, which
    // ran off the frame and read as loose scribbles crossing the whole panel)
    // to a length that stays inside the core's neighbourhood. At 7 x 0.32 the
    // longest arc ends around radius 5 against a frame half-height of ~6 at
    // the camera distance used here, so the arcs stay attached to the core.
    for (let i = 0; i < 7; i += 1) {
      current
        .add(mainDir.clone().multiplyScalar(0.32))
        .add(
          new THREE.Vector3(
            (Math.random() - 0.5) * 0.7,
            (Math.random() - 0.5) * 0.7,
            (Math.random() - 0.5) * 0.7,
          ),
        );
      points.push(current.clone());
    }

    // The geometry is shared by the four stacked jittered lines that fake a
    // thick plasma stroke; the materials are not, because each line has its own
    // brightness in the stack. Both are owned by the arc, so recycling one
    // releases its GPU resources immediately instead of accumulating them.
    const geometry = new THREE.BufferGeometry().setFromPoints(points);
    const materials: THREE.LineBasicMaterial[] = [];

    const group = new THREE.Group();
    for (let j = 0; j < 4; j += 1) {
      const material = new THREE.LineBasicMaterial({
        color: baseColor,
        blending: THREE.AdditiveBlending,
        transparent: true,
        opacity: j === 0 ? 0.7 : 0.25,
        depthWrite: false,
      });
      materials.push(material);
      const line = new THREE.Line(geometry, material);
      if (j > 0) {
        line.position.set(
          (Math.random() - 0.5) * 0.1,
          (Math.random() - 0.5) * 0.1,
          (Math.random() - 0.5) * 0.1,
        );
      }
      group.add(line);
    }
    return {
      group,
      geometry,
      materials,
      flickerRate: Math.random() * 0.4 + 0.1,
      life: Math.random() * 40 + 20,
    };
  }

  function disposeArc(arc: Arc) {
    lightningGroup.remove(arc.group);
    arc.geometry.dispose();
    for (const material of arc.materials) material.dispose();
  }

  const arcs: Arc[] = [];
  for (let i = 0; i < quality.arcs; i += 1) {
    const arc = createArc();
    arcs.push(arc);
    lightningGroup.add(arc.group);
  }

  /* ── residual sparks ───────────────────────────────────────────────────── */
  const sparkGeometry = new THREE.BufferGeometry();
  const sparkPositions = new Float32Array(quality.sparks * 3);
  const sparkVelocities: THREE.Vector3[] = [];
  const sparkColors = new Float32Array(quality.sparks * 3);

  for (let i = 0; i < quality.sparks; i += 1) {
    sparkPositions[i * 3] = (Math.random() - 0.5) * CORE_RADIUS * 2;
    sparkPositions[i * 3 + 1] = (Math.random() - 0.5) * CORE_RADIUS * 2;
    sparkPositions[i * 3 + 2] = (Math.random() - 0.5) * CORE_RADIUS * 2;
    sparkVelocities.push(
      new THREE.Vector3(
        (Math.random() - 0.5) * 0.04,
        (Math.random() - 0.5) * 0.04,
        (Math.random() - 0.5) * 0.04,
      ),
    );
    const c = Math.random() > 0.5 ? new THREE.Color(CYAN) : new THREE.Color(ORANGE);
    sparkColors[i * 3] = c.r;
    sparkColors[i * 3 + 1] = c.g;
    sparkColors[i * 3 + 2] = c.b;
  }
  sparkGeometry.setAttribute('position', new THREE.BufferAttribute(sparkPositions, 3));
  sparkGeometry.setAttribute('color', new THREE.BufferAttribute(sparkColors, 3));

  const sparkMaterial = new THREE.PointsMaterial({
    size: 0.1,
    vertexColors: true,
    map: particleTexture,
    blending: THREE.AdditiveBlending,
    transparent: true,
    depthWrite: false,
    opacity: 0.8,
  });
  disposables.push(sparkGeometry, sparkMaterial);
  const sparkSystem = new THREE.Points(sparkGeometry, sparkMaterial);
  scene.add(sparkSystem);

  /* ── structured framework: rings + polar shell ─────────────────────────── */
  const ringMaterials: THREE.LineBasicMaterial[] = [];
  const ringGeometries: THREE.BufferGeometry[] = [];

  function createRing(radius: number, opacity: number, dashed: boolean): THREE.Line {
    const points: THREE.Vector3[] = [];
    const colors: number[] = [];
    const segments = 64;
    for (let i = 0; i <= segments; i += 1) {
      const theta = (i / segments) * Math.PI * 2;
      const x = Math.cos(theta) * radius;
      const z = Math.sin(theta) * radius;
      points.push(new THREE.Vector3(x, 0, z));
      const c = x < 0 ? new THREE.Color(CYAN) : new THREE.Color(ORANGE);
      colors.push(c.r, c.g, c.b);
    }
    const geometry = new THREE.BufferGeometry().setFromPoints(points);
    geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
    const material = dashed
      ? new THREE.LineDashedMaterial({
          vertexColors: true,
          transparent: true,
          opacity,
          dashSize: 0.15,
          gapSize: 0.1,
          blending: THREE.AdditiveBlending,
          depthWrite: false,
        })
      : new THREE.LineBasicMaterial({
          vertexColors: true,
          transparent: true,
          opacity,
          blending: THREE.AdditiveBlending,
          depthWrite: false,
        });
    ringGeometries.push(geometry);
    ringMaterials.push(material);
    const line = new THREE.Line(geometry, material);
    if (dashed) line.computeLineDistances();
    // Base opacity is remembered so verification can brighten the rings from
    // their real value instead of snapping to a hardcoded one.
    material.userData.baseOpacity = opacity;
    return line;
  }

  structureGroup.add(createRing(CORE_RADIUS * 1.05, 0.4, false));
  structureGroup.add(createRing(CORE_RADIUS * 1.15, 0.2, true));
  structureGroup.add(createRing(CORE_RADIUS * 1.25, 0.15, false));

  const shellSource = new THREE.SphereGeometry(CORE_RADIUS * 0.98, 24, 16);
  const shellEdges = new THREE.EdgesGeometry(shellSource);
  shellSource.dispose();
  const edgePositions = shellEdges.attributes.position.array as Float32Array;
  const edgeColors: number[] = [];
  for (let i = 0; i < edgePositions.length; i += 3) {
    const c = bandColor(edgePositions[i]);
    edgeColors.push(c.r, c.g, c.b);
  }
  shellEdges.setAttribute('color', new THREE.Float32BufferAttribute(edgeColors, 3));
  const shellMaterial = new THREE.LineBasicMaterial({
    vertexColors: true,
    transparent: true,
    opacity: 0.12,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  });
  shellMaterial.userData.baseOpacity = 0.12;
  disposables.push(shellEdges, shellMaterial);
  structureGroup.add(new THREE.LineSegments(shellEdges, shellMaterial));

  /* ── reactive state ────────────────────────────────────────────────────── */
  let state: UltronCoreState = { mode: 'idle', progress: 0 };
  let shock = 0;          // 1 -> 0 after a tool event
  let failure = 0;        // 1 -> 0 after a failure, shifts the core amber
  let energy = 0;         // smoothed progress
  let targetEnergy = 0;
  let speakPhase = 0;
  let speaking = 0;
  let listenPhase = 0;
  let visionSweep = 0;

  /* ── lifecycle ─────────────────────────────────────────────────────────── */
  let raf = 0;
  let running = false;
  let disposed = false;
  let suspended = false;
  let visible = true;
  let previous = performance.now();
  let elapsed = 0;

  function frame() {
    if (disposed || suspended) return;
    raf = requestAnimationFrame(frame);

    const now = performance.now();
    // Clamped: returning to a backgrounded tab must not teleport the animation.
    const dt = Math.min((now - previous) / 1000, 0.05);
    previous = now;
    elapsed += dt;
    const t = elapsed;

    // Smooth the real progress signal so a step boundary does not snap.
    energy += (targetEnergy - energy) * Math.min(1, dt * 2.2);
    shock = Math.max(0, shock - dt * 1.6);
    failure = Math.max(0, failure - dt * 0.5);

    const active = state.mode !== 'idle';
    const motion = reduceMotion ? 0.25 : 1;

    // Chaotic inner rotation scales with real work in flight.
    coreGroup.rotation.y = t * (0.1 + energy * 0.22) * motion;
    coreGroup.rotation.z = t * (0.05 + energy * 0.1) * motion;

    networkGroup.rotation.x = -t * 0.08 * motion;
    networkGroup.rotation.y = t * (0.15 + energy * 0.18) * motion;

    lightningGroup.rotation.y = t * (0.2 + energy * 0.25) * motion;

    // The structured layer moves deliberately, in contrast to the inner chaos -
    // it is the layer that represents verification.
    structureGroup.rotation.y = t * 0.05 * motion;
    structureGroup.rotation.z = Math.sin(t * 0.2) * 0.05 * motion;
    structureGroup.rotation.x = Math.cos(t * 0.1) * 0.05 * motion;

    // Violent pulse while executing; a slow breath when idle.
    const executing = state.mode === 'executing';
    const pulse = executing
      ? 1 + Math.sin(t * 12) * 0.03 + Math.cos(t * 7) * 0.02
      : 1 + Math.sin(t * 1.6) * 0.008;
    const shockScale = 1 + shock * 0.12;
    globalGroup.scale.setScalar(pulse * shockScale);

    // Particle response: bigger and denser while work is happening.
    const sizeUniform = particleMaterial.uniforms.uSize as { value: number };
    const densityUniform = particleMaterial.uniforms.uDensity as { value: number };
    sizeUniform.value = 0.78 + energy * 0.4 + shock * 0.7;
    densityUniform.value = 0.34 + energy * 0.3 + shock * 0.22;

    // Network web: bright while a plan is being built, dim otherwise.
    const planning = state.mode === 'planning' || state.mode === 'understanding';
    networkMaterial.opacity = 0.2 + (planning ? 0.35 : 0) + energy * 0.12 + shock * 0.2;

    // Lightning: arc count and flicker scale with executed work. Arcs are
    // recycled rather than recreated - the original page churned geometry every
    // few seconds, which is a steady allocation leak over a long session.
    const arcsTarget = Math.max(
      2,
      Math.round(quality.arcs * (0.25 + energy * 0.75)),
    );
    for (let i = 0; i < arcs.length; i += 1) {
      const arc = arcs[i];
      arc.group.visible = i < arcsTarget;
      if (!arc.group.visible) continue;
      if (Math.random() < arc.flickerRate) arc.group.visible = !arc.group.visible;
      arc.life -= dt * 60;
      if (arc.life <= 0) {
        disposeArc(arc);
        const replacement = createArc();
        arcs[i] = replacement;
        lightningGroup.add(replacement.group);
      }
    }
    // Bloom rises with real work and spikes on a real failure.
    bloom.strength = (active ? 0.62 : 0.4) + energy * 0.25 + shock * 0.35 + failure * 0.5;

    // Structured rings: the verification layer, and the voice layer.
    const verifying = state.mode === 'verifying';
    // Listening: a slow travelling highlight around the rings, so "the mic is
    // open" looks different from "Ultron is thinking about it".
    const listening = state.mode === 'listening';
    if (listening) listenPhase += dt * 1.1;
    // Vision: one bright pass while the accessibility tree is being read.
    const visionMode = state.mode === 'vision';
    if (visionMode) {
      visionSweep = Math.min(1, visionSweep + dt * 2.2);
    } else {
      visionSweep = Math.max(0, visionSweep - dt * 2.2);
    }
    const visionBand = visionSweep > 0
      ? Math.max(0, 1 - Math.abs(((visionSweep * 1.6) % 1) - 0.5) * 4)
      : 0;

    if (state.mode === 'speaking') {
      speaking = Math.min(1, speaking + dt * 3);
      speakPhase += dt * 6.5;
    } else {
      speaking = Math.max(0, speaking - dt * 3);
    }
    const voicePulse = speaking * (0.5 + 0.5 * Math.sin(speakPhase));
    for (let i = 0; i < ringMaterials.length; i += 1) {
      const material = ringMaterials[i];
      const base = (material.userData.baseOpacity as number) ?? 0.2;
      // Per-ring phase offset turns the listening pulse into a travelling wave.
      const listenWave = listening
        ? 0.5 + 0.5 * Math.sin(listenPhase - i * 0.9)
        : 0;
      material.opacity =
        base * (1 + (verifying ? 1.6 : 0) + voicePulse * 1.1 + failure * 0.8 +
                listenWave * 1.3 + visionBand * 1.8) +
        shock * 0.12;
    }
    shellMaterial.opacity =
      ((shellMaterial.userData.baseOpacity as number) ?? 0.12) *
        (1 + voicePulse * 2.2 + (verifying ? 1.2 : 0) + visionBand * 2.4 +
         (listening ? 0.6 : 0)) +
      shock * 0.06;

    // Sparks drift outward; the shell of the sphere recycles them so the field
    // never thins out.
    const positions = sparkGeometry.attributes.position.array as Float32Array;
    const limit = CORE_RADIUS * 2.5 * (1 + shock * 0.6);
    const v = new THREE.Vector3();
    for (let i = 0; i < quality.sparks; i += 1) {
      const speed = 1 + energy * 1.6;
      positions[i * 3] += sparkVelocities[i].x * speed;
      positions[i * 3 + 1] += sparkVelocities[i].y * speed;
      positions[i * 3 + 2] += sparkVelocities[i].z * speed;
      v.set(positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]);
      if (v.length() > limit) {
        const next = v.normalize().multiplyScalar(Math.random() * CORE_RADIUS);
        positions[i * 3] = next.x;
        positions[i * 3 + 1] = next.y;
        positions[i * 3 + 2] = next.z;
      }
    }
    sparkGeometry.attributes.position.needsUpdate = true;
    sparkMaterial.opacity = 0.45 + energy * 0.35 + shock * 0.2;
    sparkMaterial.size = 0.08 + energy * 0.05 + failure * 0.06;

    controls.update();
    composer.render();
  }

  function start() {
    if (running || disposed || suspended) return;
    running = true;
    previous = performance.now();
    raf = requestAnimationFrame(frame);
  }

  function stop() {
    running = false;
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
  }

  const onResize = () => {
    const w = Math.max(container.clientWidth, 1);
    const h = Math.max(container.clientHeight, 1);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    // updateStyle must stay on: the canvas is sized in CSS pixels from the
    // container, while the drawing buffer is scaled by the pixel ratio. Passing
    // `false` leaves no CSS size, so on a 2x display the canvas would render at
    // double the intended dimensions.
    renderer.setSize(w, h);
    composer.setSize(w, h);
  };
  window.addEventListener('resize', onResize);

  const onVisibility = () => {
    visible = !document.hidden;
    if (visible) start();
    else stop();
  };
  document.addEventListener('visibilitychange', onVisibility);

  // Off-screen suspension: a background command center must not cost frames on
  // whatever page the user actually navigated to.
  const observer =
    typeof IntersectionObserver !== 'undefined'
      ? new IntersectionObserver(
          (entries) => {
            const onScreen = entries.some((e) => e.isIntersecting);
            if (onScreen && visible) start();
            else stop();
          },
          { threshold: 0.01 },
        )
      : null;
  observer?.observe(container);

  onResize();
  start();

  return {
    setState(next) {
      state = next;
      targetEnergy = Math.max(0, Math.min(1, next.progress || 0));
      if (next.mode === 'idle') {
        // Settle back down when a run ends, so a finished run does not leave
        // the core permanently excited.
        energy = 0;
        shock = 0;
      }
    },
    pulse(strength = 1) {
      shock = Math.min(1.4, shock + strength);
    },
    flag(kind) {
      if (kind === 'fail') failure = 1;
      else shock = Math.min(1.2, shock + 0.5);
    },
    setSuspended(next) {
      suspended = next;
      if (next) stop();
      else if (visible) start();
    },
    dispose() {
      disposed = true;
      stop();
      window.removeEventListener('resize', onResize);
      document.removeEventListener('visibilitychange', onVisibility);
      observer?.disconnect();
      controls.dispose();

      for (const arc of arcs) disposeArc(arc);
      arcs.length = 0;
      for (const item of disposables) item.dispose();
      for (const geometry of ringGeometries) geometry.dispose();
      for (const material of ringMaterials) material.dispose();
      ringGeometries.length = 0;
      ringMaterials.length = 0;

      composer.dispose();
      renderer.dispose();
      if (renderer.domElement.parentNode === container) {
        container.removeChild(renderer.domElement);
      }
    },
  };
}
