"""Render the review-only cat frames; never imports or installs the application."""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
DATA = json.loads((ROOT / "frames.json").read_text())
BACKGROUND = "#0d1b1e"
FONT = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 24)
SMALL = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 14)
CELL_W, CELL_H = 15, 27
WIDTH, HEIGHT = 840, 600
ORIGIN_X = (WIDTH - DATA["cols"] * CELL_W) // 2
ORIGIN_Y = 92


def render(frame: dict, index: int) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((1, 1, WIDTH - 2, HEIGHT - 2), radius=15, outline="#2c4649")
    draw.text((30, 25), "CLEAN MY AGENT", font=SMALL, fill="#5edcc6")
    draw.text((WIDTH - 170, 25), "LOADING PREVIEW", font=SMALL, fill="#7b9698")
    draw.line((30, 58, WIDTH - 30, 58), fill="#243c3f")
    for y, row in enumerate(frame["text"].splitlines()):
        for x, char in enumerate(row):
            if char != " ":
                color = DATA["colors"][frame["colors"][y * DATA["cols"] + x]]
                draw.text((ORIGIN_X + x * CELL_W, ORIGIN_Y + y * CELL_H), char, font=FONT, fill=color)
    # An indeterminate indicator; this preview does not invent a scan percentage.
    for i in range(5):
        color = "#5edcc6" if i == (index // 3) % 5 else "#2a4949"
        x = WIDTH // 2 - 26 + i * 13
        draw.ellipse((x - 2, 555, x + 2, 559), fill=color)
    return image


images = [render(frame, i) for i, frame in enumerate(DATA["frames"])]
# The artwork uses a small fixed palette: retain its exact colors in every frame.
palette = images[0].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
indexed = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
durations = [round((i + 1) * 100 / DATA["fps"]) * 10 - round(i * 100 / DATA["fps"]) * 10 for i in range(len(images))]
indexed[0].save(ROOT / "cat-loading.gif", save_all=True, append_images=indexed[1:], duration=durations, loop=0, disposal=2, optimize=False)
(ROOT / "cat-loading-british-shorthair.gif").write_bytes((ROOT / "cat-loading.gif").read_bytes())
images[0].save(ROOT / "poster.png")
sheet = Image.new("RGB", (WIDTH * 3 // 2, HEIGHT), BACKGROUND)
for tile, frame_index in enumerate((0, 16, 31, 39, 47, 58)):
    tile_image = images[frame_index].resize((WIDTH // 2, HEIGHT // 2), Image.Resampling.LANCZOS)
    sheet.paste(tile_image, ((tile % 3) * WIDTH // 2, (tile // 3) * HEIGHT // 2))
sheet.save(ROOT / "contact-sheet.png")

payload = json.dumps(DATA, ensure_ascii=False).replace("</", "<\\/")
page = '''<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>胖英短翻书 · Loading 预览</title>
<style>
  :root { color-scheme:dark; background:#091315; color:#d4e5df; font-family:system-ui,sans-serif; }
  * { box-sizing:border-box; }
  body { min-height:100svh; margin:0; display:grid; place-items:center; padding:24px; }
  main { width:min(860px,100%); }
  .terminal { background:#0d1b1e; border:1px solid #2c4649; border-radius:16px; overflow:hidden; }
  header { margin:0 28px; padding:24px 0; border-bottom:1px solid #243c3f; display:flex; justify-content:space-between; gap:12px; font:12px Menlo,monospace; color:#7b9698; }
  header strong { color:#5edcc6; font-weight:500; }
  .stage { padding:32px 0 26px; overflow:hidden; text-align:center; }
  pre { display:inline-block; margin:0; text-align:left; font:clamp(8px,2.8vw,24px)/1.125 Menlo,Consolas,"Liberation Mono",monospace; font-variant-ligatures:none; white-space:pre; }
  .dots { display:flex; justify-content:center; gap:10px; }
  .dots span { width:4px; height:4px; border-radius:50%; background:#2a4949; }
  .dots .lit { background:#5edcc6; }
  footer { display:flex; align-items:center; justify-content:center; gap:14px; padding:20px 0 12px; }
  button { font:inherit; color:#d4e5df; background:#15282b; border:1px solid #355457; border-radius:8px; padding:9px 18px; cursor:pointer; }
  button:hover,button:focus-visible { background:#214044; outline:2px solid #5edcc6; outline-offset:2px; }
</style>
<main>
  <section class="terminal" aria-label="圆脸胖英短拿放大镜翻书的终端动画">
    <header><strong>CLEAN MY AGENT</strong><span>LOADING PREVIEW</span></header>
    <div class="stage"><pre id="art" aria-hidden="true"></pre><div class="dots" aria-hidden="true"><span></span><span></span><span></span><span></span><span></span></div></div>
  </section>
  <footer><button id="toggle" type="button">暂停</button><button id="restart" type="button">重播</button></footer>
</main>
<script id="frames" type="application/json">__FRAMES__</script>
<script>
const data=JSON.parse(document.querySelector('#frames').textContent);
const art=document.querySelector('#art');
const toggle=document.querySelector('#toggle'), dots=[...document.querySelectorAll('.dots span')];
const escape=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
const rendered=data.frames.map(frame=>frame.text.split('\\n').map((line,y)=>{
  let row='', start=0;
  while(start<line.length){const color=frame.colors[y*data.cols+start];let end=start+1;
    while(end<line.length && frame.colors[y*data.cols+end]===color)end++;
    row+='<span style="color:'+data.colors[color]+'">'+escape(line.slice(start,end))+'</span>';start=end;}
  return row;
}).join('\\n'));
let playing=!matchMedia('(prefers-reduced-motion: reduce)').matches, elapsed=0, previous=performance.now(), last=-1;
function paint(i){if(i===last)return;last=i;art.innerHTML=rendered[i];dots.forEach((dot,n)=>dot.classList.toggle('lit',n===Math.floor(i/3)%5));}
function label(){toggle.textContent=playing?'暂停':'播放';toggle.setAttribute('aria-label',playing?'暂停动画':'播放动画');}
function tick(now){if(playing && !document.hidden)elapsed+=Math.min(now-previous,100);previous=now;paint(Math.floor(elapsed*data.fps/1000)%data.frames.length);requestAnimationFrame(tick);}
toggle.onclick=()=>{playing=!playing;label();};
document.querySelector('#restart').onclick=()=>{elapsed=0;playing=true;label();paint(0);};
document.addEventListener('keydown',event=>{if(event.code==='Space' && event.target===document.body){event.preventDefault();toggle.click();}});
paint(0);label();requestAnimationFrame(tick);
</script>
</html>'''
(ROOT / "preview.html").write_text(page.replace("__FRAMES__", payload))
print(f"Rendered {len(images)} frames / {sum(durations) / 1000:g}s → {ROOT}")
