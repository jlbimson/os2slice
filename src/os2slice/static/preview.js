// os2slice panel preview: the selected part(s), oriented as they will print, on the chosen bed.
// Z is up (like the slicer). Each part is drawn in its filament's colour. Renders on change only.
import * as THREE from "./vendor/three.module.js";
import { STLLoader } from "./vendor/STLLoader.js";
import { OrbitControls } from "./vendor/OrbitControls.js";

const root = document.getElementById("panel");
const box = document.getElementById("preview");
const note = document.getElementById("preview-note");
const form = document.getElementById("printform");
const beds = JSON.parse(root.dataset.beds || "{}");

const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
const scene = new THREE.Scene();
scene.background = new THREE.Color(dark ? 0x1d2026 : 0xf3f5f7);
THREE.Object3D.DEFAULT_UP.set(0, 0, 1);

const camera = new THREE.PerspectiveCamera(35, 1, 1, 5000);
camera.up.set(0, 0, 1);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
box.prepend(renderer.domElement);

scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.4));
const sun = new THREE.DirectionalLight(0xffffff, 1.6);
sun.position.set(-150, -250, 400);
scene.add(sun);

const controls = new OrbitControls(camera, renderer.domElement);
controls.addEventListener("change", render);

const bedGroup = new THREE.Group();
scene.add(bedGroup);
let model = null; // THREE.Group of part meshes
let bedSize = [256, 256];

function render() {
  renderer.render(scene, camera);
}

function resize() {
  const w = box.clientWidth || 300;
  const h = box.clientHeight || 230;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  render();
}
new ResizeObserver(resize).observe(box);

function buildBed([w, d]) {
  bedGroup.clear();
  const plate = new THREE.Mesh(
    new THREE.PlaneGeometry(w, d),
    new THREE.MeshStandardMaterial({ color: dark ? 0x3a3f47 : 0xd9dde2, roughness: 0.9 }),
  );
  plate.position.z = -0.05;
  bedGroup.add(plate);
  const pts = [];
  for (let x = -w / 2; x <= w / 2 + 0.01; x += 10) pts.push(x, -d / 2, 0, x, d / 2, 0);
  for (let y = -d / 2; y <= d / 2 + 0.01; y += 10) pts.push(-w / 2, y, 0, w / 2, y, 0);
  const grid = new THREE.BufferGeometry();
  grid.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
  bedGroup.add(new THREE.LineSegments(grid, new THREE.LineBasicMaterial({
    color: dark ? 0x555c66 : 0xb8bec6,
  })));
}

function frame() {
  // With parts: frame them (the bed stays as context). Without: show the whole bed.
  let target, r;
  if (model) {
    const b = new THREE.Box3().setFromObject(model);
    const size = b.getSize(new THREE.Vector3());
    target = b.getCenter(new THREE.Vector3());
    r = Math.max(size.length() * 1.5, 60);
  } else {
    target = new THREE.Vector3(0, 0, 0);
    r = Math.hypot(bedSize[0], bedSize[1]) * 1.05;
  }
  camera.position.set(target.x - r * 0.55, target.y - r * 0.95, target.z + r * 0.75);
  controls.target.copy(target);
  controls.update();
  render();
}

const setNote = (text) => { note.textContent = text; };

function currentBed() {
  // Values are "<printer>" or "<printer>|<tray id>".
  const printer = form.elements.printer ? form.elements.printer.value.split("|")[0] : "";
  return beds[printer] || [256, 256];
}

function shade(hex) {
  if (!hex || !/^#[0-9a-fA-F]{6}$/.test(hex)) return new THREE.Color("#e0a040");
  // Lift very dark filament a little so the shading still reads.
  const col = new THREE.Color(hex);
  const hsl = col.getHSL({});
  if (hsl.l < 0.22) col.setHSL(hsl.h, hsl.s, 0.22);
  return col;
}

function recolour() {
  if (!model) return;
  const colours = window.os2sliceColours ? window.os2sliceColours() : {};
  for (const m of model.children) m.material.color.copy(shade(colours[m.userData.part]));
  render();
}

let timer = 0;
let inflight = null;

function schedule() {
  clearTimeout(timer);
  timer = setTimeout(refresh, 250);
}

async function refresh() {
  const bed = currentBed();
  if (bed[0] !== bedSize[0] || bed[1] !== bedSize[1] || bedGroup.children.length === 0) {
    bedSize = bed;
    buildBed(bedSize);
    frame();
  }
  const f = form.elements;
  if (!f.p.value && !f.face.value) {
    if (model) { scene.remove(model); model = null; render(); }
    setNote("Select a part to preview it.");
    return;
  }
  const extra = f.extra.value ? f.extra.value.split(",").map((x) => x.split(":")[0]) : [];
  const ids = extra.length ? [f.p.value, ...extra] : [""];
  const params = new URLSearchParams();
  for (const k of ["d", "wv", "wvid", "e", "c", "p", "face"]) params.set(k, f[k].value);
  params.set("orient", f.orient.value);
  if (extra.length) params.set("extra", extra.join(","));
  if (inflight) inflight.abort();
  inflight = new AbortController();
  const { signal } = inflight;
  setNote(extra.length ? `Loading ${ids.length} parts…` : "Loading preview…");
  try {
    const group = new THREE.Group();
    for (const id of ids) {
      const q = new URLSearchParams(params);
      if (id) q.set("only", id);
      const res = await fetch(`/panel/preview?${q}`, { signal, credentials: "same-origin" });
      if (!res.ok) throw new Error(`preview failed (${res.status})`);
      const geometry = new STLLoader().parse(await res.arrayBuffer());
      geometry.computeVertexNormals();
      const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({
        roughness: 0.55, metalness: 0.05,
      }));
      mesh.userData.part = id || f.p.value;
      group.add(mesh);
    }
    // The slicer's auto-arrange centres the object; mirror that for the whole assembly.
    const b = new THREE.Box3().setFromObject(group);
    group.position.set(-(b.min.x + b.max.x) / 2, -(b.min.y + b.max.y) / 2, -b.min.z);
    if (model) scene.remove(model);
    model = group;
    scene.add(model);
    recolour();
    frame();
    const s = b.getSize(new THREE.Vector3());
    const dims = `${s.x.toFixed(1)} × ${s.y.toFixed(1)} × ${s.z.toFixed(1)} mm`;
    const tooBig = s.x > bedSize[0] || s.y > bedSize[1];
    const auto = f.orient.value === "auto" ? " · the slicer will pick the orientation" : "";
    const count = ids.length > 1 ? ` · ${ids.length} parts` : "";
    setNote(tooBig ? `${dims} · larger than this printer's bed!` : `${dims}${count}${auto}`);
  } catch (err) {
    if (err.name !== "AbortError") setNote(`No preview: ${err.message}`);
  }
}

document.addEventListener("os2slice:selection", schedule);
document.addEventListener("os2slice:color", recolour);
form.addEventListener("change", (ev) => {
  if (["printer", "orient"].includes(ev.target.name)) schedule();
  else if (ev.target.dataset && ev.target.dataset.part) recolour();
});
resize();
refresh();
