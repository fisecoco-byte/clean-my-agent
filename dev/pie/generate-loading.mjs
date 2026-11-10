// Pre-render ascii.rest artwork; terminal users need neither Node nor a network.
import spinners from 'ascii.rest/pieces/spinners';
import { readFileSync, writeFileSync } from 'node:fs';
import { catAnimation } from './generate-cat-loading.mjs';

const cat = catAnimation();
const height = Math.max(...cat.frames.map(frame => frame.text.trimEnd().split('\n').length));
const frames = cat.frames.map(frame => [
  frame.text.split('\n').slice(0, height).join('\n'),
  Array.from({ length: height }, (_, y) => frame.colors.slice(y * cat.cols, (y + 1) * cat.cols).join('')).join('\n'),
]);
const scan = spinners({ names: ['scan'], labels: false });
const compact = Array.from({ length: 12 }, (_, i) => scan(i / 12).split('\n')[2].trim());
const license = readFileSync(new URL('./ascii.rest-LICENSE', import.meta.url), 'utf8');
const output = '"""Frames generated with ascii.rest 0.4.0 by dev/pie/generate-loading.mjs.\n\n' +
  license + '\n"""\n\n' +
  `FPS = ${cat.fps}\nCAT_WIDTH = ${cat.cols}\nCAT_HEIGHT = ${height}\n` +
  'CAT_PALETTE = (' + cat.colors.map(c => JSON.stringify(c)).join(', ') + ',)\n\n' +
  'CAT_FRAMES = (\n' + frames.map(([text, colors]) => '    (' + JSON.stringify(text) + ', ' + JSON.stringify(colors) + '),').join('\n') + '\n)\n\n' +
  'SCAN_FRAMES = (\n' + compact.map(f => '    ' + JSON.stringify(f) + ',').join('\n') + '\n)\n';
writeFileSync(new URL('../../clean_my_agent/_loading_frames.py', import.meta.url), output);
