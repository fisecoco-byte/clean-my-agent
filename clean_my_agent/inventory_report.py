"""Present the inventory model. Reports have no file-management actions."""
import json
import os
from pathlib import Path

from .inventory import BUCKETS, SECTIONS, plan
from .pie import pie_lines, share
from .policy import SESSION_DAYS
from .session_titles import UNTITLED
from .terminal import NAMES, fmt, safe


def display(ui, doc, details):
    ui.header('会话盘点 / 活跃分布', '只读')
    for key, data in doc['agents'].items():
        ui.section(NAMES[key])
        p = data['policy']
        a, d = (data['recommendations'][k] for k in ('archive', 'delete'))
        pairs = [('已识别会话', str(data['session_count'])), (f"归档复核 ≥{p['archive_days']} 天", str(a['count'])),
                 (f"删除复核 ≥{p['delete_days']} 天", str(d['count']))]
        if ui.active:
            ui.metrics(pairs)
        else:
            print('  ' + '  /  '.join(f'{k}: {v}' for k, v in pairs))
        print('  未活跃天数分布 · 会话数占比')
        for spans in pie_lines(data['histogram'], ui.width - 2):
            print('  ' + ''.join(ui.ink(s, style) if style.startswith('slice') else s for s, style in spans).rstrip())
        print(f"  会话文件占用：{fmt(data['session_bytes'])}；排除复核：{data['held']} 个。")
        if key == 'codex':
            tree = data['groups']['worktrees']
            print(f"  工作树：{tree['count']} 项 · " + (fmt(tree['bytes']) if tree['bytes'] is not None else '未完整测量（--full）'))
        if data['errors']:
            print('  盘点不完整，此 agent 不生成建议。')
        for action, group in [('归档', a), ('删除', d)]:
            if group['entries']:
                print(f"  {action}复核 · {group['count']} 个，显示 {len(group['entries'])} 个")
                for row in group['entries']:
                    print('    ' + (row['title'] or UNTITLED))
                    print(f"    ID {row['session_id']}  ·  {row['idle_days']} 天  ·  {fmt(row['bytes'])}")
        if details:
            for name, g in data['groups'].items():
                ui.section(f"{SECTIONS[name]} · {g['count']} 项")
                for item in g['entries']:
                    print(f"  {fmt(item['bytes']) if item['bytes'] is not None else '未测量'}  {safe(item['path'])}")
        if key == 'codex':
            print('  归档：' + data['guidance']['archive'] + '；永久删除：' + data['guidance']['delete'])
            print('  ' + data['guidance']['worktrees'])
        else:
            print('  ' + data['guidance']['review'])
    print('仅供复核：按本地索引或文件最近修改估算，不代表进程已停止。')


def html_payload(doc, all_rows):
    agents = {}
    for key, data in doc['agents'].items():
        plans = {}
        for a in SESSION_DAYS:
            for d in SESSION_DAYS:
                plans[f'{a}/{d}'] = plan(all_rows[key], a, d, doc['limit'])
        agents[key] = dict(name=NAMES[key], count=data['session_count'], size=fmt(data['session_bytes']),
                           held=data['held'], histogram=list(data['histogram'].values()), policy=data['policy'],
                           shares=[share(n, data['session_count']) for n in data['histogram'].values()],
                           plans=plans, partial=bool(data['errors']), source=data['activity_source'],
                           worktrees=data['groups'].get('worktrees', {}).get('count'))
    return dict(agents=agents, buckets=BUCKETS, days=SESSION_DAYS, generated=doc['generated_at'], limit=doc['limit'])


def export(path, doc, rows):
    path = Path(path).expanduser()
    if path.suffix.lower() != '.html':
        raise ValueError('报告路径请使用 .html 扩展名')
    # Keep user-supplied display names inert inside the JSON script element.
    payload = json.dumps(html_payload(doc, rows), ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    page = TEMPLATE.replace('@@PAYLOAD@@', payload)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError('报告已存在，请为新快照指定另一个文件名') from None
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write(page)
    return path.absolute()


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>会话盘点 · Clean My Agent</title>
<style>
:root{color-scheme:light;font:15px/1.6 system-ui,sans-serif;color:#192d2c;background:#f8f7f2;--accent:#176b60;--wash:#dceae3}
*{box-sizing:border-box}body{margin:0}main{max-width:1060px;margin:auto;padding:30px clamp(16px,4vw,56px) 56px}
header,nav,.legend,.control,.cardhead{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
header{font:600 12px ui-monospace,monospace;letter-spacing:.12em;padding-bottom:16px;border-bottom:1px solid #aec0b8}
time{font-weight:400;letter-spacing:0;color:#52635d}h1{font-size:clamp(32px,6vw,56px);line-height:1.05;letter-spacing:-.06em;margin:38px 0 13px}
p{margin:8px 0 18px;color:#4b5e57}nav{justify-content:flex-start;border-bottom:1px solid #c6d2ca;padding-top:8px}
button,select{font:inherit;color:inherit;cursor:pointer}button{border:0;border-bottom:3px solid transparent;background:transparent;padding:8px 15px}
button[aria-pressed=true]{border-color:var(--accent);color:var(--accent);font-weight:700}select{border:1px solid #a7b7af;background:#fff;border-radius:5px;padding:5px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:#c7d3ca;border:1px solid #c7d3ca;margin-top:20px}
.stat{background:#edf2ec;padding:16px 20px}.stat span{display:block;font-size:13px;color:#44564e}.stat b{font-size:clamp(22px,4vw,40px);font-weight:600;line-height:1.3;font-variant-numeric:tabular-nums}
article{margin-top:22px;border:1px solid #c5cfc7;background:#fdfcf8;padding:clamp(12px,3vw,24px)}
h2{font-size:19px;margin:0}small,footer{color:#53665e;font-size:13px}
.chart{display:flex;align-items:center;gap:28px;flex-wrap:wrap;margin:16px 0}.pie{width:128px;aspect-ratio:1;border-radius:50%}.pie[hidden]{display:none}
.slice{display:grid;grid-template-columns:12px auto auto auto;align-items:center;gap:10px;margin:3px 0;font-variant-numeric:tabular-nums}.slice i{width:10px;height:10px}.slice b,.slice small{text-align:right}
.control{justify-content:flex-start;margin:16px 0}
label{display:flex;align-items:center;gap:7px}table{border-collapse:collapse;width:100%;font-size:14px;table-layout:auto}
td,th{text-align:left;padding:8px;border-bottom:1px solid #dbe1db}th{color:#53665e;font-size:12px}td.session{overflow-wrap:anywhere}td.session code{display:block;color:#53665e;font-size:12px}
footer{margin-top:22px}#warning{color:#99461c}code{overflow-wrap:anywhere}
@media(max-width:520px){.stat{padding:10px 7px}.chart{gap:12px}td,th{padding:6px 3px}main{padding-top:18px}}
</style>
<main>
<header><span>CLEAN MY AGENT &nbsp;/&nbsp; INVENTORY</span><time id="stamp"></time></header>
<h1>会话盘点</h1><p>按最近可见活动查看会话。切换天数只改变本页的复核清单。</p>
<nav id="agents" aria-label="选择 agent"></nav>
<div class="stats"><div class="stat"><span>已识别会话</span><b id="total"></b></div><div class="stat"><span>归档复核</span><b id="na"></b></div><div class="stat"><span>删除复核</span><b id="nd"></b></div></div>
<article><div class="cardhead"><h2>未活跃天数分布 · 会话数占比</h2><small id="volume"></small></div><div class="chart"><div id="pie" class="pie" aria-hidden="true"></div><div id="legend"></div></div><small id="basis"></small></article>
<article><h2>复核清单</h2><div class="control"><label>归档阈值 <select id="archive"></select></label><label>删除阈值 <select id="delete"></select></label></div>
<p id="rule"></p><p id="warning"></p>
<table><thead><tr><th>建议</th><th>标题 / 摘要 · 会话 ID</th><th>未活跃</th></tr></thead><tbody id="rows"></tbody></table>
<small id="caption"></small></article><footer id="guide"></footer>
</main>
<script type="application/json" id="payload">@@PAYLOAD@@</script>
<script>
const doc=JSON.parse(document.querySelector('#payload').textContent), el=id=>document.getElementById(id);
let key=Object.keys(doc.agents)[0];const chosen={};
function node(tag,text){const n=document.createElement(tag);n.textContent=text;return n}
for(const [k,v] of Object.entries(doc.agents)){chosen[k]=[v.policy.archive_days,v.policy.delete_days];
 const b=node('button',v.name);b.dataset.key=k;b.onclick=()=>{key=k;draw()};el('agents').append(b)}
for(const [ix,id] of ['archive','delete'].entries()){
 for(const n of doc.days){const opt=node('option',n+' 天及以上');opt.value=n;el(id).append(opt)}
 el(id).onchange=e=>{chosen[key][ix]=Number(e.target.value);draw()}}
el('stamp').textContent=new Date(doc.generated).toLocaleString('zh-CN',{hour12:false});
function draw(){const v=doc.agents[key], [a,d]=chosen[key], p=v.plans[a+'/'+d];
 document.querySelectorAll('nav button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.key===key)));
 el('archive').value=a;el('delete').value=d;el('total').textContent=v.count;el('na').textContent=p.archive.count;el('nd').textContent=p.delete.count;
 el('volume').textContent='主会话文件 '+v.size+(v.worktrees===null?'':'  ·  '+v.worktrees+' 个工作树目录项');
 const colors=['#257d83','#55964b','#dbad53','#9b65a4','#b65749'],stops=[];let n=0;
 el('legend').replaceChildren();el('pie').hidden=!v.count;
 if(!v.count) el('legend').textContent='暂无会话';
 else doc.buckets.forEach((label,i)=>{const r=node('div',''),dot=node('i','');r.className='slice';dot.style.background=colors[i];
 const start=n;n+=v.histogram[i];if(n>start)stops.push(colors[i]+' '+100*start/v.count+'% '+100*n/v.count+'%');
 r.append(dot,node('span',label),node('b',v.histogram[i]+' 个'),node('small',v.shares[i]));el('legend').append(r)});
 el('pie').style.background=stops.length===1?colors[v.histogram.findIndex(n=>n>0)]:v.count?'conic-gradient('+stops.join(',')+')':'';
 el('basis').textContent=(v.source==='index+files'?'参考会话文件和 Codex 索引的最近时间。':'参考可见会话文件的最近修改时间。')+'不能据此判断进程已停止。';
 el('rule').textContent=key==='codex'?'未归档会话只建议归档；已归档会话才建议删除。':'普通本地会话的归档状态未知；两项均为人工复核，同时符合时列入删除复核。';
 el('warning').textContent=v.partial?'盘点不完整，已暂停此 agent 的建议。':'';
 el('rows').replaceChildren();let shown=0;for(const [k,label] of [['archive','归档复核'],['delete','删除复核']]){
 for(const r of p[k].entries){shown++;const tr=node('tr',''),c=node('td',r.title||'（无标题或摘要）');c.className='session';
 c.append(node('code',r.session_id));tr.append(node('td',label),c,node('td',r.idle_days+' 天'));el('rows').append(tr)}}
 el('caption').textContent=(shown?'共 '+(p.archive.count+p.delete.count)+' 个候选，显示 '+shown+' 个；每类最多 '+doc.limit+' 个。':'当前阈值没有复核候选。')+' 排除复核 '+v.held+' 个。';
 el('guide').textContent=key==='codex'?'手动管理：codex archive <SESSION_ID>  /  codex delete <SESSION_ID>（永久删除）。工作树由 Codex 桌面端管理。':'可用 claude --resume <SESSION_ID> 核对会话。此报告不调用 Claude、不删除文件。';}
draw();
</script></html>"""
