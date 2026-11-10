// Shared artwork for the approved loading animation and its standalone preview.
import { Surface, Palette, stamp, line } from 'ascii.rest/kit';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const cols = 44, rows = 17, fps = 12, count = 72;
const colors = ['#b7c5d4', '#a5dfca', '#79ac9d', '#efc480', '#f7efdf', '#496d62', '#e9b66b', '#d8a6a6', '#627b8c'];
const palette = new Palette(colors);
const backslash = String.fromCharCode(92);
const sprite = (...lines) => lines.join('\n').replaceAll('§', backslash);

function stroke(s, a, b, color) {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const char = Math.abs(dx) > Math.abs(dy) * 1.8 ? '─'
    : dx === 0 ? '│' : dx * dy < 0 ? '╱' : '╲';
  line(s, ...a, ...b, { char, color });
}

function leaf(s, points) {
  // Mask the whole sheet so the book's printed lines don't show through it.
  for (let y = 6; y <= 14; y++) {
    for (let x = 4; x <= 38; x++) {
      let inside = false;
      for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
        const [ax, ay] = points[i], [bx, by] = points[j];
        if ((ay > y) !== (by > y) && x < (bx - ax) * (y - ay) / (by - ay) + ax) inside = !inside;
      }
      if (inside) s.set(x, y, ' ', 4);
    }
  }
  points.forEach((point, i) => stroke(s, point, points[(i + 1) % points.length], 4));
}

function frame(index) {
  const s = new Surface(cols, rows, { palette });
  const phase = index / fps;
  const turning = phase >= 2.5 && phase < 4.25;
  const flip = turning ? (phase - 2.5) / 1.75 : 0;
  const lensX = 33 + Math.round(1.5 * Math.sin(phase * Math.PI / 3));
  const lensY = phase >= 4.25 && phase < 5 ? 5 : 4;
  const blinking = (index >= 16 && index <= 17) || (index >= 57 && index <= 58);
  const headX = 8;
  // A blue British Shorthair: small ears, full cheeks, a broad round torso,
  // copper eyes and short paws. The book hides the lower belly.
  const tail = Math.floor(index / 12) % 2 === 0
    ? sprite('  __', ' /  )', '(  (__', ' §____)')
    : sprite('   __', '  /  )', ' (  (_', '  §___)');
  stamp(s, tail, 2, 6, { color: 0 });
  stamp(s, sprite(
    '   .-----------.',
    " .'             '.",
    '(                 )',
    " '.             .'",
    "   '-----------'",
  ), 7, 6, { color: 0, mask: null });
  // A soft belly curve gives the silhouette volume without extra UI text.
  stamp(s, '  (     )', 11, 8, { color: 8 });
  const head = sprite(
    '   _         _',
    '  / §_______/ §',
    ".'             '.",
    '(               )',
    '(  =    ▾    =  )',
    " '.     ω     .'",
    "   '---------'",
  ).split('\n').map(row => row.padEnd(17)).join('\n');
  stamp(s, head, headX, 0, { color: 0, mask: null });
  s.set(headX + 5, 3, blinking ? '─' : '●', 6);
  s.set(headX + 11, 3, blinking ? '─' : '●', 6);
  s.set(headX + 8, 4, '▾', 7);
  s.set(headX + 8, 5, 'ω', 4);
  s.set(headX + 1, 4, '·', 8);
  s.set(headX + 15, 4, '·', 8);
  // An open book: separate page blocks, visible spine and layered bottom edge.
  stamp(s, [
    '    .------------. .------------.',
    '   /              V              \\',
    '  /               |               \\',
    ' /________________|________________\\',
    ' \\_______________|________________/',
  ].join('\n'), 3, 10, { color: 1, mask: null });
  const printed = ['── ─────', '──── ──'];
  s.write(9, 11, printed[0], 2);
  s.write(8, 12, printed[1], 2);
  s.write(25, 11, printed[1], 2);
  s.write(26, 12, printed[0], 2);
  // The far paw lifts a page, carries it over the spine, and sets it down.
  let pawX = 13, pawY = 9;
  if (turning) {
    const tipX = Math.round(9 + 25 * flip);
    const tipY = Math.round(10 - 3 * Math.sin(Math.PI * flip));
    pawX = tipX;
    pawY = tipY - 1;
    stroke(s, [12, 8], [pawX, pawY], 0);
    leaf(s, [[21, 10], [tipX, tipY], [tipX + Math.round((flip - .5) * 4), tipY + 3], [21, 13]]);
    if (Math.abs(tipX - 21) >= 6) s.write(Math.round((tipX + 21) / 2) - 1, tipY + 2, '──', 2);
  } else {
    stroke(s, [12, 8], [pawX, pawY], 0);
  }
  stamp(s, '(uu)', pawX - 1, pawY, { color: 0 });
  // Near paw holds a magnifying glass. The lens scans over the right page;
  // its highlight and enlarged type move while the head follows the reading.
  const handX = lensX - 3, handY = lensY + 6;
  stroke(s, [24, 8], [26, handY], 0);
  stroke(s, [26, handY], [handX, handY], 0);
  stroke(s, [lensX - 1, lensY + 4], [handX, handY], 3);
  stamp(s, sprite('  .---.  ', ' /     § ', '(       )', ' §     / ', "  '---'  "), lensX - 4, lensY, { color: 3, mask: null });
  s.write(lensX - 1, lensY + 2, index % 12 < 6 ? '≡' : '═', 4);
  s.set(lensX + 1, lensY + 2, '·', 4);
  s.set(lensX - 2, lensY + 1, '/', 4);
  s.set(lensX - 2, lensY + 2, '·', 5);
  stamp(s, '(uu)', handX - 1, handY, { color: 0 });
  return { text: s.toString(), colors: Array.from(s.colors, color => color === 255 ? 0 : color),
    action: turning ? '翻一页，再找找' : '小英短正在认真查找' };
}

export function catAnimation() {
  return { cols, rows, fps, colors, duration: count / fps,
    frames: Array.from({ length: count }, (_, i) => frame(i)),
    attribution: 'Rendered with ascii.rest 0.4.0 Surface / stamp / line (MIT). Original cat-book sprites.' };
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  const out = new URL('../cat-loading/', import.meta.url);
  mkdirSync(out, { recursive: true });
  const data = catAnimation();
  writeFileSync(new URL('frames.json', out), JSON.stringify(data));
  console.log(data.frames[0].text);
}
