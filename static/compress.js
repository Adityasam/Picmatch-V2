// Shrink an image in the browser before upload: scale down to fit maxW x maxH, re-encode as JPEG.
// Returns the original file if the browser can't decode it or re-encoding wouldn't make it smaller.
// ponytail: lossy and drops EXIF; the original is not kept anywhere
const compressCanvas = document.createElement('canvas');

async function compressImage(file, maxW, maxH, quality) {
  try {
    const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' }); // applies EXIF rotation
    const k = Math.min(1, maxW / bmp.width, maxH / bmp.height);
    const c = compressCanvas;
    c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.width, c.height); // transparent PNG → white, not black
    ctx.drawImage(bmp, 0, 0, c.width, c.height);
    bmp.close();
    const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', quality));
    if (!blob || (k === 1 && blob.size >= file.size)) return file; // already small: keep as is
    return new File([blob], file.name.replace(/\.\w+$/, '') + '.jpg', { type: 'image/jpeg' });
  } catch {
    return file; // browser can't decode it: upload the original
  }
}
