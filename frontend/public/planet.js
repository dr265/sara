import * as THREE from 'three';
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';
import { ShaderPass } from 'three/examples/jsm/postprocessing/ShaderPass.js';
import { GammaCorrectionShader } from 'three/examples/jsm/shaders/GammaCorrectionShader.js';
import { CopyShader } from 'three/examples/jsm/shaders/CopyShader.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { DRACOLoader } from 'three/examples/jsm/loaders/DRACOLoader.js';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import Lenis from 'https://unpkg.com/lenis@1.3.23/dist/lenis.mjs';

const CONFIG = {
  rimColor:'#c1faff', rimPower:2.4, nightLights:10, terrainDepth:0.33, terrainShade:1.3,
  oceanGlint:0.45, oceanDeep:0.12, oceanFlow:3, oceanFlowSpeed:0.8, oceanFlowScale:2.1,
  glowColor:'#3a6cff', glowIntensity:3.35, planetRadius:1.95, spin:0.03, initRotation:2.07,
  tilt:0.37, autoRotate:0,
  cloud1Height:1.005, cloud1Opacity:0.6, cloud1Spin:0.06,
  cloud2Height:1.03, cloud2Opacity:0.5, cloud2Spin:0.14,
  cloud3Height:1.075, cloud3Opacity:0.5, cloud3Spin:0.1,
  bgColor:'#040a1e', flameColor:'#3a6cff', flameColor2:'#c1faff', flameAmt:0.15,
  atmoColor:'#9fc4ff', atmoCount:320, atmoSize:22, atmoSpeed:0.8,
  starColor:'#cfe0ff', starCount:1400, starSize:1.6, starFlicker:1,
  markerColor:'#ffd27a', markerCount:60, markerSize:16, markerSpeed:0.5,
};

const LAYERS = { NONE:0, TORUS_SCENE:1, BLOOM_SCENE:2, ENTIRE_SCENE:3 };

const ASSET_BASE_URL = 'https://api.getlayers.ai/storage/v1/object/public/public/assets/ascend-d9857ad1f2';

const Lerp = (a, b, t) => a + (b - a) * t;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const hexToVec3 = (hex) => new THREE.Color(hex);
const sample = (stops, p) => {
  if (p <= stops[0].p) return stops[0].v;
  if (p >= stops[stops.length - 1].p) return stops[stops.length - 1].v;
  for (let i = 0; i < stops.length - 1; i++) {
    if (p >= stops[i].p && p <= stops[i + 1].p) {
      let t = (p - stops[i].p) / (stops[i + 1].p - stops[i].p);
      t = t * t * (3 - 2 * t); // smoothstep
      return Lerp(stops[i].v, stops[i + 1].v, t);
    }
  }
  return 0;
};

const SNOISE = `
vec4 permute(vec4 x){return mod(((x*34.0)+1.0)*x, 289.0);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159 - 0.85373472095314 * r;}
float snoise(vec3 v){
  const vec2 C = vec2(1.0/6.0, 1.0/3.0); const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);
  vec3 i = floor(v + dot(v, C.yyy)); vec3 x0 = v - i + dot(i, C.xxx);
  vec3 g = step(x0.yzx, x0.xyz); vec3 l = 1.0 - g;
  vec3 i1 = min(g.xyz, l.zxy); vec3 i2 = max(g.xyz, l.zxy);
  vec3 x1 = x0 - i1 + 1.0 * C.xxx; vec3 x2 = x0 - i2 + 2.0 * C.xxx; vec3 x3 = x0 - 1.0 + 3.0 * C.xxx;
  i = mod(i, 289.0);
  vec4 p = permute(permute(permute(i.z + vec4(0.0, i1.z, i2.z, 1.0)) + i.y + vec4(0.0, i1.y, i2.y, 1.0)) + i.x + vec4(0.0, i1.x, i2.x, 1.0));
  float n_ = 1.0/7.0; vec3 ns = n_ * D.wyz - D.xzx;
  vec4 j = p - 49.0 * floor(p * ns.z *ns.z);
  vec4 x_ = floor(j * ns.z); vec4 y_ = floor(j - 7.0 * x_);
  vec4 x = x_ *ns.x + ns.yyyy; vec4 y = y_ *ns.x + ns.yyyy; vec4 h = 1.0 - abs(x) - abs(y);
  vec4 b0 = vec4(x.xy, y.xy); vec4 b1 = vec4(x.zw, y.zw);
  vec4 s0 = floor(b0)*2.0 + 1.0; vec4 s1 = floor(b1)*2.0 + 1.0; vec4 sh = -step(h, vec4(0.0));
  vec4 a0 = b0.xzyw + s0.xzyw*sh.xxyy; vec4 a1 = b1.xzyw + s1.xzyw*sh.zzww;
  vec3 p0 = vec3(a0.xy,h.x); vec3 p1 = vec3(a0.zw,h.y); vec3 p2 = vec3(a1.xy,h.z); vec3 p3 = vec3(a1.zw,h.w);
  vec4 norm = taylorInvSqrt(vec4(dot(p0,p0), dot(p1,p1), dot(p2, p2), dot(p3,p3)));
  p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;
  vec4 m = max(0.5 - vec4(dot(x0,x0), dot(x1,x1), dot(x2,x2), dot(x3,x3)), 0.0); m = m * m;
  return 42.0 * dot(m*m, vec4(dot(p0,x0), dot(p1,x1), dot(p2,x2), dot(p3,x3)));
}
`;

const FinalPass = {
  uniforms: {
    iTime:{value:0}, tDiffuse:{value:null}, torusTexture:{value:null}, bloomTexture:{value:null}, haloTexture:{value:null},
    uBg:{value:hexToVec3(CONFIG.bgColor)}, uFlameA:{value:hexToVec3(CONFIG.flameColor)},
    uFlameB:{value:hexToVec3(CONFIG.flameColor2)}, uFlameAmt:{value:CONFIG.flameAmt},
  },
  vertexShader: `varying vec2 vUv; void main(){ vUv = uv; gl_Position = vec4(position, 1.0); }`,
  fragmentShader: `
    uniform float iTime; uniform sampler2D tDiffuse; uniform sampler2D bloomTexture; uniform sampler2D torusTexture; uniform sampler2D haloTexture;
    uniform vec3 uBg; uniform vec3 uFlameA; uniform vec3 uFlameB; uniform float uFlameAmt;
    varying vec2 vUv;
    vec3 warp3d(vec3 pos, float t){ float curv=.8,a=1.9,b=0.7; pos*=2.;
      pos.x+=curv*sin(t+a*pos.y)+t*b; pos.y+=curv*cos(t+a*pos.x);
      pos.y+=curv*sin(t+a*pos.z)+t*b; pos.z+=curv*cos(t+a*pos.y);
      pos.z+=curv*sin(t+a*pos.x)+t*b; pos.x+=curv*cos(t+a*pos.z);
      return 0.5+0.5*cos(pos.xyz+vec3(1,2,4)); }
    void main(){
      vec2 uv = 2.*vUv - 1.;
      vec3 w = pow(warp3d(vec3(uv.x, sin(uv.y), uv.y), iTime*1.5), vec3(1.5));
      vec3 flame = 1.5*uFlameA*w.x; flame*=w.y; flame += uFlameB*w.z;
      flame *= smoothstep(0.25, 1., abs(uv.y));
      float md = smoothstep(-0.7, 1., -uv.y*uv.x); flame *= md*md;
      vec3 bg = uBg * (1.0 - 0.4 * length(uv));
      vec3 halo = texture2D(haloTexture, vUv).xyz;
      gl_FragColor = vec4(bg + flame*uFlameAmt + texture2D(bloomTexture, vUv).xyz + texture2D(torusTexture, vUv).xyz + texture2D(tDiffuse, vUv).xyz + halo, 1.);
    }`,
};

function initScene() {
  const canvas = document.querySelector('.planet-canvas');
  if (!canvas) return;

  const w = window.innerWidth;
  const h = window.innerHeight;

  const renderer = new THREE.WebGL1Renderer({ canvas, antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setSize(w, h);
  renderer.outputEncoding = THREE.sRGBEncoding;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.VSMShadowMap;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x000000);

  const camera = new THREE.PerspectiveCamera(40, w / h, 0.1, 200);
  camera.position.set(0, 0, 8);
  camera.layers.enable(LAYERS.TORUS_SCENE);
  camera.layers.enable(LAYERS.BLOOM_SCENE);
  camera.layers.enable(LAYERS.ENTIRE_SCENE);
  scene.add(camera);

  const ambientLight = new THREE.AmbientLight(0xffffff, 1.8);
  ambientLight.layers.enable(LAYERS.ENTIRE_SCENE);
  scene.add(ambientLight);

  const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
  dirLight.position.set(0, 10, 2);
  dirLight.layers.enable(LAYERS.ENTIRE_SCENE);
  scene.add(dirLight);

  const torusComposer = new EffectComposer(renderer);
  torusComposer.renderToScreen = false;
  torusComposer.addPass(new RenderPass(scene, camera));
  torusComposer.addPass(new ShaderPass(GammaCorrectionShader));
  torusComposer.addPass(new UnrealBloomPass(new THREE.Vector2(w, h), 0.22, 0.2, 0));
  torusComposer.addPass(new ShaderPass(CopyShader));

  const bloomComposer = new EffectComposer(renderer);
  bloomComposer.renderToScreen = false;
  bloomComposer.addPass(new RenderPass(scene, camera));
  bloomComposer.addPass(new UnrealBloomPass(new THREE.Vector2(w, h), 0.5, 0.6, 0));
  bloomComposer.addPass(new ShaderPass(GammaCorrectionShader));

  const finalComposer = new EffectComposer(renderer);
  const finalPass = new ShaderPass(FinalPass);
  finalPass.uniforms.bloomTexture.value = bloomComposer.renderTarget1.texture;
  finalPass.uniforms.torusTexture.value = torusComposer.renderTarget1.texture;
  
  const haloTarget = new THREE.WebGLRenderTarget(w, h, { minFilter: THREE.LinearFilter, magFilter: THREE.LinearFilter, format: THREE.RGBAFormat });
  finalPass.uniforms.haloTexture = { value: haloTarget.texture };
  
  finalComposer.addPass(new RenderPass(scene, camera));
  finalComposer.addPass(finalPass);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.06;
  controls.enablePan = false;
  controls.enabled = false;
  controls.autoRotate = true;
  controls.autoRotateSpeed = CONFIG.autoRotate;
  controls.minDistance = 3.5;
  controls.maxDistance = 16;
  controls.target.set(0, 0, 0);

  const worldGroup = new THREE.Group();
  scene.add(worldGroup);

  const planetGroup = new THREE.Group();
  planetGroup.rotation.z = CONFIG.tilt;
  worldGroup.add(planetGroup);

  // TEST SPHERE
  const testMesh = new THREE.Mesh(
    new THREE.SphereGeometry(2, 32, 32),
    new THREE.MeshBasicMaterial({ color: 0xff0000 })
  );
  planetGroup.add(testMesh);

  const cloudGroup = new THREE.Group();
  cloudGroup.rotation.z = CONFIG.tilt;
  cloudGroup.visible = false;
  worldGroup.add(cloudGroup);

  const glowGeo = new THREE.PlaneGeometry(2, 2);
  const glowMat = new THREE.ShaderMaterial({
    uniforms: {
      uGlow: { value: hexToVec3(CONFIG.glowColor) },
      uIntensity: { value: CONFIG.glowIntensity }
    },
    vertexShader: `varying vec2 vUv; void main(){ vUv=uv; gl_Position = projectionMatrix*modelViewMatrix*vec4(position,1.0); }`,
    fragmentShader: `
      uniform vec3 uGlow; uniform float uIntensity; varying vec2 vUv;
      void main(){
        float d = length(vUv - 0.5) * 2.0;
        float a = pow(clamp(1.0 - d, 0.0, 1.0), 2.2);
        gl_FragColor = vec4(uGlow * a * uIntensity, a);
      }
    `,
    transparent: true,
    side: THREE.DoubleSide,
    depthWrite: false,
    blending: THREE.AdditiveBlending
  });
  const glowMesh = new THREE.Mesh(glowGeo, glowMat);
  const glowScale = CONFIG.planetRadius * 2.3;
  glowMesh.scale.set(glowScale, glowScale, glowScale);
  worldGroup.add(glowMesh);

  const atmoGeo = new THREE.BufferGeometry();
  const atmoPos = [];
  const atmoSize = [];
  const atmoSeed = [];
  for (let i = 0; i < CONFIG.atmoCount; i++) {
    atmoPos.push((Math.random() - 0.5) * 2, (Math.random() - 0.5) * 2, (Math.random() - 0.5) * 2);
    atmoSize.push(CONFIG.atmoSize * (0.4 + Math.random()));
    atmoSeed.push(Math.random() * 100);
  }
  atmoGeo.setAttribute('position', new THREE.Float32BufferAttribute(atmoPos, 3));
  atmoGeo.setAttribute('size', new THREE.Float32BufferAttribute(atmoSize, 1));
  atmoGeo.setAttribute('seed', new THREE.Float32BufferAttribute(atmoSeed, 1));
  const atmoMat = new THREE.ShaderMaterial({
    uniforms: {
      uTime: { value: 0 },
      uRes: { value: new THREE.Vector2(w, h) },
      uColor: { value: hexToVec3(CONFIG.atmoColor) }
    },
    vertexShader: `
      attribute float size; attribute float seed; uniform float uTime; uniform vec2 uRes;
      varying float vA;
      vec3 warp(vec3 p, float t){ float c=0.9,a=1.9,b=0.02,s=0.05; p*=2.;
        p.x+=c*sin(s*t+a*p.y)+t*b; p.y+=c*cos(s*t+a*p.x); p.y+=c*sin(s*t+a*p.z)+t*b;
        p.z+=c*cos(s*t+a*p.y); p.z+=c*sin(s*t+a*p.x)+t*b; p.x+=c*cos(s*t+a*p.z);
        return cos(p+vec3(1,2,4)); }
      void main(){
        vec3 v = position*4.0 + warp(position, uTime)*1.2;
        vec4 mv = modelViewMatrix * vec4(v, 1.0);
        float r = length(v); float farF = 1.0 - smoothstep(5.0, 6.5, r); float nearF = smoothstep(0.0, 0.5, -mv.z);
        vA = farF * nearF;
        gl_PointSize = size * uRes.y / 900.0 / -mv.z; gl_PointSize = max(gl_PointSize, 1.0);
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: `
      uniform vec3 uColor; varying float vA;
      void main(){ vec2 p = gl_PointCoord - 0.5; float l = length(p); if (l > 0.5) discard;
        float tex = smoothstep(0.5, 0.0, l); gl_FragColor = vec4(uColor * tex, tex * vA * 0.55); }
    `,
    transparent: true, depthWrite: false, depthTest: false, blending: THREE.AdditiveBlending
  });
  const atmoPoints = new THREE.Points(atmoGeo, atmoMat);
  scene.add(atmoPoints);

  const starGeo = new THREE.BufferGeometry();
  const starPos = [];
  const starSeed = [];
  const starBright = [];
  for (let i = 0; i < CONFIG.starCount; i++) {
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos((Math.random() * 2) - 1);
    const r = 90;
    starPos.push(r * Math.sin(phi) * Math.cos(theta), r * Math.sin(phi) * Math.sin(theta), r * Math.cos(phi));
    starSeed.push(Math.random() * 100);
    starBright.push(0.35 + Math.random() * 0.65);
  }
  starGeo.setAttribute('position', new THREE.Float32BufferAttribute(starPos, 3));
  starGeo.setAttribute('seed', new THREE.Float32BufferAttribute(starSeed, 1));
  starGeo.setAttribute('bright', new THREE.Float32BufferAttribute(starBright, 1));
  const starMat = new THREE.ShaderMaterial({
    uniforms: {
      uTime: { value: 0 },
      uSize: { value: CONFIG.starSize },
      uFlicker: { value: CONFIG.starFlicker },
      uColor: { value: hexToVec3(CONFIG.starColor) },
      uRes: { value: new THREE.Vector2(w, h) }
    },
    vertexShader: `
      attribute float seed; attribute float bright;
      uniform float uTime; uniform float uSize; uniform float uFlicker; uniform vec2 uRes;
      varying float vTw;
      void main(){
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        float tw = 0.6 + 0.4 * sin(uTime * uFlicker + seed);
        vTw = bright * tw;
        gl_PointSize = max(uSize * uRes.y / 900.0 * (90.0 / max(-mv.z, 1.0)), 1.0);
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: `
      uniform vec3 uColor; varying float vTw;
      void main(){
        vec2 p = gl_PointCoord - 0.5; float l = length(p); if (l > 0.5) discard;
        float core = smoothstep(0.5, 0.0, l);
        gl_FragColor = vec4(uColor, core * vTw);
      }
    `,
    transparent: true, depthWrite: false, depthTest: true, blending: THREE.AdditiveBlending
  });
  const starPoints = new THREE.Points(starGeo, starMat);
  starPoints.frustumCulled = false;
  scene.add(starPoints);

  let planetTime = 0, cloudTime = 0, starTime = 0, markerTime = 0;
  let planetLoaded = false;
  let entryY = -6.5;
  let hasEntered = false;
  let entryStart = 0;
  let baseColorMap = null;

  const dracoLoader = new DRACOLoader();
  dracoLoader.setDecoderPath('https://www.gstatic.com/draco/versioned/decoders/1.5.5/');
  const gltfLoader = new GLTFLoader();
  gltfLoader.setDRACOLoader(dracoLoader);

  let nightTex = null;
  gltfLoader.load(ASSET_BASE_URL + '/planet-lights.glb', (gltf) => {
    nightTex = gltf.scene.children[0].material.map;

    gltfLoader.load(ASSET_BASE_URL + '/planet.glb', (gltf2) => {
      const pMesh = gltf2.scene.children[0];
      baseColorMap = pMesh.material.map;
      pMesh.geometry.computeBoundingSphere();
      const s = CONFIG.planetRadius / pMesh.geometry.boundingSphere.radius;
      pMesh.scale.set(s, s, s);

      const pMat = pMesh.material.clone();
      pMat.metalness = 0; pMat.roughness = 1; pMat.envMapIntensity = 0;
      pMat.onBeforeCompile = (shader) => {
        shader.uniforms.time = { value: 0 };
        shader.uniforms.noiseScale = { value: 30 };
        shader.uniforms.speedX = { value: 1.5 };
        shader.uniforms.speedY = { value: 2.0 };
        shader.uniforms.speedZ = { value: 2.5 };
        shader.uniforms.rimColor = { value: hexToVec3(CONFIG.rimColor) };
        shader.uniforms.rimPower = { value: CONFIG.rimPower };
        shader.uniforms.nightBlendTexture = { value: nightTex };
        shader.uniforms.nightLights = { value: CONFIG.nightLights };
        shader.uniforms.terrainDepth = { value: CONFIG.terrainDepth };
        shader.uniforms.terrainShade = { value: CONFIG.terrainShade };
        shader.uniforms.oceanGlint = { value: CONFIG.oceanGlint };
        shader.uniforms.oceanDeep = { value: CONFIG.oceanDeep };
        shader.uniforms.oceanFlow = { value: CONFIG.oceanFlow };
        shader.uniforms.oceanFlowSpeed = { value: CONFIG.oceanFlowSpeed };
        shader.uniforms.oceanFlowScale = { value: CONFIG.oceanFlowScale };
        pMat.userData.shader = shader;

        shader.vertexShader = `varying vec2 vCustomUv;\n` + shader.vertexShader.replace(`void main() {`, `void main() {\n vCustomUv = uv;`);
        shader.fragmentShader = `
          varying vec2 vCustomUv;
          uniform float time; uniform float noiseScale; uniform float speedX; uniform float speedY; uniform float speedZ;
          uniform vec3 rimColor; uniform float rimPower; uniform sampler2D nightBlendTexture; uniform float nightLights;
          uniform float terrainDepth; uniform float terrainShade; uniform float oceanGlint; uniform float oceanDeep;
          uniform float oceanFlow; uniform float oceanFlowSpeed; uniform float oceanFlowScale;
          ${SNOISE}
        ` + shader.fragmentShader.replace(
          `#include <dithering_fragment>`,
          `#include <dithering_fragment>
            vec3 normalizedNormal = normalize(vNormal);
            vec3 viewDir = normalize(vViewPosition);
            float rim = 1.0 - max(dot(viewDir, normalizedNormal), 0.0);
            rim = pow(rim, rimPower); rim = pow(rim, 1.5); rim *= 0.7;
            vec3 currentColor = gl_FragColor.rgb;
            float blueDom = currentColor.b - max(currentColor.r, currentColor.g);
            float waterMask = clamp(smoothstep(-0.005, 0.03, blueDom), 0.0, 1.0);
            float shimmer = snoise(vec3(vCustomUv.x * noiseScale + time * speedX, vCustomUv.y * noiseScale - time * speedY, time * speedZ));
            gl_FragColor.rgb += waterMask * shimmer * 0.025;
            float fT = time * oceanFlowSpeed * 4.0;
            float fS = 4.0 * oceanFlowScale;
            float warp = snoise(vec3(vCustomUv.x * fS - fT * 0.5, vCustomUv.y * fS + fT * 0.4, fT * 0.5));
            float flow = snoise(vec3(vCustomUv.x * fS * 2.0 + fT * 0.6 + warp, vCustomUv.y * fS * 2.0 - fT * 0.5, fT * 0.7));
            flow = warp * 0.6 + flow * 0.4;
            gl_FragColor.rgb += waterMask * flow * 0.12 * oceanFlow;
            gl_FragColor.rgb = mix(gl_FragColor.rgb, vec3(0.01, 0.06, 0.16), waterMask * oceanDeep);
            vec3 finalColor = mix(gl_FragColor.rgb, rimColor, rim);
            gl_FragColor = vec4(finalColor, 1.0);
            vec3 surfPos = -vViewPosition;
            float terrH = dot(texture2D(map, vCustomUv).rgb, vec3(0.299, 0.587, 0.114));
            vec3 sigX = dFdx(surfPos), sigY = dFdy(surfPos);
            vec3 vR1 = cross(sigY, normalizedNormal), vR2 = cross(normalizedNormal, sigX);
            float fDet = dot(sigX, vR1);
            vec3 vGrad = sign(fDet) * (dFdx(terrH) * vR1 + dFdy(terrH) * vR2);
            vec3 bumpedNormal = normalize(abs(fDet) * normalizedNormal - terrainDepth * vGrad);
            vec3 shadeNormal = mix(bumpedNormal, normalizedNormal, waterMask);
            vec3 cityLights = texture2D(nightBlendTexture, vCustomUv).rgb * gl_FragColor.rgb * nightLights;
            vec3 viewSunDir = normalize(vec3(-0.9, 0.18, 0.4));
            float ndl = dot(normalizedNormal, viewSunDir);
            float dayAmt = smoothstep(-0.05, 0.35, ndl);
            float relief = dot(shadeNormal, viewSunDir) - ndl;
            gl_FragColor.rgb *= clamp(1.0 + relief * terrainShade * dayAmt, 0.55, 1.6);
            float nightFactor  = smoothstep(0.18, -0.30, ndl);
            float lightsFactor = smoothstep(0.30, -0.35, ndl);
            gl_FragColor.rgb = mix(gl_FragColor.rgb, gl_FragColor.rgb * 0.08, nightFactor);
            gl_FragColor.rgb += cityLights * lightsFactor;
            vec3 halfDir = normalize(viewSunDir + viewDir);
            float ripple = snoise(vec3(vCustomUv * 240.0, time * 4.0));
            float ndh = max(dot(normalizedNormal, halfDir) + ripple * 0.02, 0.0);
            float glint = pow(ndh, 140.0);
            gl_FragColor.rgb += glint * waterMask * dayAmt * oceanGlint * vec3(1.0, 0.97, 0.88);
          `
        );
      };
      pMesh.material = pMat;
      planetGroup.add(pMesh);
      planetLoaded = true;
      cloudGroup.visible = true;
      entryStart = performance.now();

      if (baseColorMap && baseColorMap.image) {
        const img = baseColorMap.image;
        const cvs = document.createElement('canvas');
        cvs.width = img.width; cvs.height = img.height;
        const ctx = cvs.getContext('2d');
        ctx.drawImage(img, 0, 0);
        const imgData = ctx.getImageData(0,0,img.width,img.height).data;
        const mPos = []; const mSeed = [];
        const posAttr = pMesh.geometry.attributes.position;
        const uvAttr = pMesh.geometry.attributes.uv;
        const indexAttr = pMesh.geometry.index;

        let added = 0, attempts = 0;
        while(added < CONFIG.markerCount && attempts < 100000) {
          attempts++;
          const tri = Math.floor(Math.random() * (indexAttr.count / 3));
          const iA = indexAttr.getX(tri * 3);
          const iB = indexAttr.getX(tri * 3 + 1);
          const iC = indexAttr.getX(tri * 3 + 2);

          let r1 = Math.random(), r2 = Math.random();
          if (r1 + r2 > 1) { r1 = 1 - r1; r2 = 1 - r2; }
          const r0 = 1 - r1 - r2;

          const u = uvAttr.getX(iA)*r0 + uvAttr.getX(iB)*r1 + uvAttr.getX(iC)*r2;
          const v = uvAttr.getY(iA)*r0 + uvAttr.getY(iB)*r1 + uvAttr.getY(iC)*r2;

          const px = Math.floor(u * img.width);
          const py = Math.floor((1 - v) * img.height);
          const idx = (py * img.width + px) * 4;
          const cr = imgData[idx], cg = imgData[idx+1], cb = imgData[idx+2];
          
          if (!(cb > cr + 6 && cb > cg + 6)) {
            const px3 = posAttr.getX(iA)*r0 + posAttr.getX(iB)*r1 + posAttr.getX(iC)*r2;
            const py3 = posAttr.getY(iA)*r0 + posAttr.getY(iB)*r1 + posAttr.getY(iC)*r2;
            const pz3 = posAttr.getZ(iA)*r0 + posAttr.getZ(iB)*r1 + posAttr.getZ(iC)*r2;
            mPos.push(px3*1.012, py3*1.012, pz3*1.012);
            mSeed.push(Math.random()*100);
            added++;
          }
        }
        
        if (added > 0) {
          const mGeo = new THREE.BufferGeometry();
          mGeo.setAttribute('position', new THREE.Float32BufferAttribute(mPos, 3));
          mGeo.setAttribute('seed', new THREE.Float32BufferAttribute(mSeed, 1));
          const mMat = new THREE.ShaderMaterial({
            uniforms: {
              uColor: { value: hexToVec3(CONFIG.markerColor) },
              uTime: { value: 0 },
              uSize: { value: CONFIG.markerSize },
              uSpeed: { value: CONFIG.markerSpeed },
              uRes: { value: new THREE.Vector2(w, h) }
            },
            vertexShader: `
              attribute float seed; uniform float uSize; uniform vec2 uRes;
              varying float vSeed; varying float vFade;
              void main(){
                vSeed = seed;
                vec4 mv = modelViewMatrix * vec4(position, 1.0);
                vec3 vn = normalize(normalMatrix * normalize(position));
                vec3 vd = normalize(-mv.xyz);
                vFade = smoothstep(0.15, 0.5, dot(vn, vd));
                gl_PointSize = max(uSize * uRes.y / 900.0 * (7.0 / max(-mv.z, 1.0)), 2.0);
                gl_Position = projectionMatrix * mv;
              }
            `,
            fragmentShader: `
              uniform vec3 uColor; uniform float uTime; uniform float uSpeed;
              varying float vSeed; varying float vFade;
              void main(){
                if (vFade <= 0.001) discard;
                vec2 p = gl_PointCoord - 0.5;
                float d = length(p) * 2.0;
                if (d > 1.0) discard;
                float core = smoothstep(0.30, 0.0, d) * 1.2;
                float ph = fract(uTime * uSpeed + vSeed);
                float ring = smoothstep(0.07, 0.0, abs(d - ph)) * (1.0 - ph);
                gl_FragColor = vec4(uColor, clamp(core + ring, 0.0, 1.0) * vFade);
              }
            `,
            transparent: true, depthWrite: false, depthTest: true, blending: THREE.AdditiveBlending
          });
          const mPoints = new THREE.Points(mGeo, mMat);
          planetGroup.add(mPoints);
          planetGroup.userData.markers = mMat;
        }
      }
    });
  });

  const texLoader = new THREE.TextureLoader();
  texLoader.load(ASSET_BASE_URL + '/planet-clouds.png', (tex) => {
    tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
    tex.repeat.set(5, 5);

    const layers = [
      { h: CONFIG.cloud1Height, o: CONFIG.cloud1Opacity, s: CONFIG.cloud1Spin, ry: 0.0, phase: 0.0 },
      { h: CONFIG.cloud2Height, o: CONFIG.cloud2Opacity, s: CONFIG.cloud2Spin, ry: 2.2, phase: 13.0 },
      { h: CONFIG.cloud3Height, o: CONFIG.cloud3Opacity, s: CONFIG.cloud3Spin, ry: 4.3, phase: 27.0 }
    ];

    layers.forEach(layer => {
      const geo = new THREE.SphereGeometry(CONFIG.planetRadius * layer.h, 64, 64);
      const mat = new THREE.MeshStandardMaterial({ map: tex, transparent: true, depthWrite: false });
      mat.onBeforeCompile = (shader) => {
        shader.uniforms.uTime = { value: 0 };
        shader.uniforms.noiseScale = { value: 20 };
        shader.uniforms.uSpeedX = { value: 1 };
        shader.uniforms.uSpeedY = { value: 2 };
        shader.uniforms.uSpeedZ = { value: 2 };
        shader.uniforms.uOpacity = { value: layer.o };
        shader.uniforms.uPhase = { value: layer.phase };
        mat.userData.shader = shader;

        shader.vertexShader = `varying vec2 vCloudUv;\n` + shader.vertexShader.replace(`void main() {`, `void main() {\n vCloudUv = uv;`);
        shader.fragmentShader = `
          uniform float uTime; uniform float noiseScale; uniform float uSpeedX; uniform float uSpeedY; uniform float uSpeedZ;
          uniform float uOpacity; uniform float uPhase;
          varying vec2 vCloudUv;
          ${SNOISE}
        ` + shader.fragmentShader.replace(
          `#include <dithering_fragment>`,
          `#include <dithering_fragment>
            gl_FragColor.rgb = vec3(1.0);
            float cloudNoise = snoise(vec3(vCloudUv.x * noiseScale + uTime * uSpeedX + uPhase, vCloudUv.y * noiseScale - uTime * uSpeedY + uPhase, uTime * uSpeedZ + uPhase));
            float cloudNdv = max(dot(normalize(vNormal), normalize(vViewPosition)), 0.0);
            float cloudEdge = pow(1.0 - cloudNdv, 3.0);
            float cloudMod = mix(cloudNoise, 1.0, cloudEdge);
            float cloudNdl = dot(normalize(vNormal), normalize(vec3(-0.9, 0.18, 0.4)));
            float cloudDay = 1.0 - smoothstep(0.30, -0.30, cloudNdl) * 0.9;
            gl_FragColor.a *= cloudMod * uOpacity * cloudDay;
          `
        );
      };
      const mesh = new THREE.Mesh(geo, mat);
      mesh.rotation.y = layer.ry; mesh.renderOrder = 2; mesh.userData = layer;
      cloudGroup.add(mesh);
    });
  });

  const STOPS_X = [{p:0,v:0},{p:0.32,v:-3.1},{p:0.64,v:3.2},{p:1,v:0}];
  const STOPS_Y = [{p:0,v:-4.5},{p:0.32,v:0.55},{p:0.64,v:0.45},{p:1,v:0.15}];
  const STOPS_S = [{p:0,v:2.15},{p:0.32,v:1.0},{p:0.64,v:0.92},{p:1,v:1.12}];
  
  let curP = 0, curX = STOPS_X[0].v, curY = STOPS_Y[0].v, curS = STOPS_S[0].v;
  let spinPhase = 0;

  worldGroup.position.set(STOPS_X[0].v, STOPS_Y[0].v, 0);
  worldGroup.scale.set(STOPS_S[0].v, STOPS_S[0].v, STOPS_S[0].v);

  let lastTime = performance.now();
  const isReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function render() {
    requestAnimationFrame(render);
    const now = performance.now();
    let dt = (now - lastTime) / 1000;
    dt = Math.min(dt, 0.05);
    lastTime = now;

    if (!isReducedMotion && planetLoaded) {
      if (!hasEntered) {
        const t = (now - entryStart) / 1900;
        if (t >= 1) { hasEntered = true; entryY = 0; }
        else { entryY = -6.5 * (1 - Math.pow(1 - t, 3)); }
      }
    } else {
      entryY = 0; hasEntered = true;
    }

    const scrollH = document.body.scrollHeight - window.innerHeight;
    let pTarget = scrollH > 0 ? clamp(window.scrollY / scrollH, 0, 1) : 0;
    if (isReducedMotion) curP = pTarget;
    else curP += (pTarget - curP) * Math.min(1, dt * 4.5);

    const sideScale = clamp(window.innerWidth / 1200, 0.5, 1);
    
    if (isReducedMotion) {
      curX = sample(STOPS_X, curP) * sideScale;
      curY = sample(STOPS_Y, curP);
      curS = sample(STOPS_S, curP);
    } else {
      curX += (sample(STOPS_X, curP) * sideScale - curX) * Math.min(1, dt * 3.2);
      curY += (sample(STOPS_Y, curP) - curY) * Math.min(1, dt * 3.2);
      curS += (sample(STOPS_S, curP) - curS) * Math.min(1, dt * 3.2);
    }

    worldGroup.position.set(curX, curY + entryY, 0);
    worldGroup.scale.set(curS, curS, curS);

    spinPhase += dt * CONFIG.spin;
    planetGroup.rotation.y = CONFIG.initRotation + spinPhase + curP * Math.PI * 1.6;

    planetTime += dt / 12; cloudTime += dt / 20; starTime += dt; markerTime += dt;

    if (planetGroup.children[0] && planetGroup.children[0].material.userData.shader) {
      planetGroup.children[0].material.userData.shader.uniforms.time.value = planetTime;
    }
    if (planetGroup.userData.markers) {
      planetGroup.userData.markers.uniforms.uTime.value = markerTime;
    }

    cloudGroup.children.forEach(mesh => {
      mesh.rotation.y += dt * mesh.userData.s;
      if (mesh.material.userData.shader) mesh.material.userData.shader.uniforms.uTime.value = cloudTime;
    });

    glowMesh.quaternion.copy(camera.quaternion);
    atmoMat.uniforms.uTime.value = (now / 1000) * CONFIG.atmoSpeed * 8;
    atmoPoints.position.copy(camera.position);

    starMat.uniforms.uTime.value = starTime;
    finalPass.uniforms.iTime.value = (now / 1000) * CONFIG.atmoSpeed * 8;

    renderer.setRenderTarget(haloTarget);
    renderer.clear();
    const oldBg = scene.background;
    scene.background = null;
    glowMesh.visible = true;
    renderer.render(scene, camera);
    scene.background = oldBg;
    renderer.setRenderTarget(null);
    glowMesh.visible = false;

    camera.layers.set(LAYERS.TORUS_SCENE); torusComposer.render();
    camera.layers.set(LAYERS.BLOOM_SCENE); bloomComposer.render();
    camera.layers.set(LAYERS.ENTIRE_SCENE); finalComposer.render();
  }
  render();

  window.addEventListener('resize', () => {
    const nw = window.innerWidth, nh = window.innerHeight;
    camera.aspect = nw / nh; camera.updateProjectionMatrix();
    renderer.setSize(nw, nh); haloTarget.setSize(nw, nh);
    torusComposer.setSize(nw, nh); bloomComposer.setSize(nw, nh); finalComposer.setSize(nw, nh);
    atmoMat.uniforms.uRes.value.set(nw, nh); starMat.uniforms.uRes.value.set(nw, nh);
    if (planetGroup.userData.markers) planetGroup.userData.markers.uniforms.uRes.value.set(nw, nh);
  });
}

function initLenisAndReveal() {
  const isReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!isReducedMotion) {
    const lenis = new Lenis({ duration: 1.15, smoothWheel: true, touchMultiplier: 1.5 });
    function raf(t) {
      lenis.raf(t);
      requestAnimationFrame(raf);
    }
    requestAnimationFrame(raf);
  } else {
    document.documentElement.style.scrollBehavior = 'auto';
  }

  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('in');
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.18, rootMargin: '0px 0px -8% 0px' });

  document.querySelectorAll('[data-reveal]').forEach(el => {
    if (isReducedMotion) el.classList.add('in');
    else observer.observe(el);
  });
}

// Since script is injected dynamically via React useEffect, the DOM is already ready.
initLenisAndReveal();
try {
  initScene();
} catch(e) {
  console.error("WebGL Scene failed to initialize:", e);
  const errDiv = document.createElement('div');
  errDiv.style.cssText = "position:fixed;top:20%;left:50%;transform:translateX(-50%);background:red;color:white;padding:20px;z-index:9999;";
  errDiv.innerText = "WebGL Error: " + e.message + "\\n" + e.stack;
  document.body.appendChild(errDiv);
}
