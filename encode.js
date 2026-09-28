// usage: node encode.js data/<slug>
// Encodes every image in <dir>/images not yet in <dir>/faces.json. Output: [{n: name, d: [[128 floats], ...]}]
// Also writes a small JPEG thumbnail per image to <dir>/thumbs/<name>.jpg for the admin grid.
// Same setup as the old C:\Picmatch\encoder\face_encoder.js (canvas + tiny face detector).
// tfjs-node 4.22 still calls util.isNullOrUndefined, removed in Node 23+
const util = require('util');
util.isNullOrUndefined ??= v => v === null || v === undefined;
const faceapi = require('@vladmandic/face-api');
const tf = require('@tensorflow/tfjs-node'); // Load TensorFlow backend
const canvas = require('canvas');
const path = require('path');
const fs = require('fs');

const { Canvas, Image, ImageData } = canvas;
faceapi.env.monkeyPatch({ Canvas, Image, ImageData });

const dir = process.argv[2];
const out = path.join(dir, 'faces.json');
const MODEL_PATH = path.join(__dirname, 'models');
const THUMB = 320; // long side px; admin grid tiles are ~95-150px, so this covers 2x screens
const thumbPath = n => path.join(dir, 'thumbs', n + '.jpg');

function saveThumb(img, n) {
  const k = Math.min(1, THUMB / Math.max(img.width, img.height));
  const c = canvas.createCanvas(Math.round(img.width * k), Math.round(img.height * k));
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.width, c.height); // transparent PNG → white, not black
  ctx.drawImage(img, 0, 0, c.width, c.height);
  fs.writeFileSync(thumbPath(n), c.toBuffer('image/jpeg', { quality: 0.7 }));
}

async function loadModels() {
  await faceapi.nets.tinyFaceDetector.loadFromDisk(MODEL_PATH);
  await faceapi.nets.faceRecognitionNet.loadFromDisk(MODEL_PATH);
  await faceapi.nets.faceLandmark68Net.loadFromDisk(MODEL_PATH);
}

async function processImage(imagePath) {
  const img = await canvas.loadImage(imagePath);
  saveThumb(img, path.basename(imagePath));
  const detections = await faceapi
    .detectAllFaces(img, new faceapi.TinyFaceDetectorOptions({ inputSize: 608 })) // bigger = finds smaller faces, slower
    .withFaceLandmarks()
    .withFaceDescriptors();
  return detections.map(d => Array.from(d.descriptor));
}

function save(faces) {
  // skip photos the admin deleted while we were running
  const keep = faces.filter(f => fs.existsSync(path.join(dir, 'images', f.n)));
  fs.writeFileSync(out + '.tmp', JSON.stringify(keep));
  fs.renameSync(out + '.tmp', out);
}

(async () => {
  await loadModels();
  fs.mkdirSync(path.join(dir, 'thumbs'), { recursive: true });
  const faces = fs.existsSync(out) ? JSON.parse(fs.readFileSync(out)) : [];

  for (;;) { // loop: admin may upload more while we run
    const done = new Set(faces.map(f => f.n));
    const todo = fs.readdirSync(path.join(dir, 'images')).filter(n => !done.has(n));
    if (!todo.length) break;
    for (const [i, n] of todo.entries()) {
      let d = [];
      try { d = await processImage(path.join(dir, 'images', n)); } catch (e) { console.error(n, e.message); }
      faces.push({ n, d }); // d: [] marks unreadable/no-face images as done
      console.log(`${i + 1}/${todo.length} ${n} faces=${d.length}`);
      if ((i + 1) % 10 === 0) save(faces); // progress for admin page; attendees get partial results early
    }
    save(faces);
  }
  // backfill thumbnails for photos encoded before thumbnails existed
  for (const n of fs.readdirSync(path.join(dir, 'images'))) {
    if (fs.existsSync(thumbPath(n))) continue;
    try { saveThumb(await canvas.loadImage(path.join(dir, 'images', n)), n); } catch (e) { console.error(n, e.message); }
  }
  setTimeout(() => process.exit(0), 100);
})().catch(e => { console.error(e); process.exit(1); });
