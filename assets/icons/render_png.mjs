// 把本目录下的 SVG 渲染为多尺寸 PNG，输出到 src/static/icons/
// 用法：cd assets/icons && npm install sharp && node render_png.mjs
import sharp from 'sharp';
import { readFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(here, '..', '..');
const OUT = resolve(ROOT, 'src', 'static', 'icons');
mkdirSync(OUT, { recursive: true });

const SIZES = [16, 32, 48, 128, 192, 256, 512];
const ITEMS = [['prompt', 'prompt.svg'], ['mini', 'mini.svg']];

for (const [name, file] of ITEMS) {
  const svg = readFileSync(resolve(here, file));
  for (const s of SIZES) {
    await sharp(svg, { density: 300 })
      .resize(s, s, { fit: 'contain', background: { r: 0, g: 0, b: 0, alpha: 0 } })
      .png({ compressionLevel: 9 })
      .toFile(resolve(OUT, `icon_${name}_${s}.png`));
    console.log(`wrote icon_${name}_${s}.png`);
  }
}
console.log('ALL DONE');
