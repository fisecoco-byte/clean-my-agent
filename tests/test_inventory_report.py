import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from clean_my_agent import inventory, inventory_report, policy
from clean_my_agent.policy import DAY_NS
from clean_my_agent.scan import layout


class InventoryReport(unittest.TestCase):
    now = 1_790_000_000_000_000_000

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        env = patch.dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.home/'.codex'),
                         CLAUDE_CONFIG_DIR=str(self.home/'.claude'), XDG_CONFIG_HOME=str(self.home/'config'),
                         XDG_CACHE_HOME=str(self.home/'cache'), XDG_STATE_HOME=str(self.home/'state'))
        env.start()
        self.addCleanup(env.stop)
        self.roots = layout()[0]

    def new(self, k, n, days, archive=False, extra_ns=0):
        sid = f'01234567-89ab-cdef-9012-{n:012x}'
        path = (self.roots[k] / ('archived_sessions' if archive else 'sessions') / f'rollout-t-{sid}.jsonl'
                if k == 'codex' else self.roots[k] / 'projects' / 'project' / (sid+'.jsonl'))
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text('DO_NOT_EXPORT_CONTENT')
        t = self.now - DAY_NS*days + extra_ns
        os.utime(path, ns=(t, t))
        return sid, path

    def report(self, **kwargs):
        return inventory.inspect(self.roots, policy.load(), now_ns=self.now, **kwargs)

    def test_exact_boundaries_and_uncapped_totals(self):
        for n,d in enumerate([5, 5, 10, 15, 20]):
            self.new('codex', n, d, extra_ns=1 if n==0 else 0)
        self.new('codex', 10, 20, archive=True)
        doc, rows = self.report(limit=1)
        cd = doc['agents']['codex']
        self.assertEqual(list(cd['histogram'].values()), [1,1,1,1,2])
        self.assertEqual(cd['recommendations']['archive']['count'], 3)
        self.assertEqual(cd['recommendations']['delete']['count'], 1)
        payload = inventory_report.html_payload(doc, rows)
        for a in policy.SESSION_DAYS:
            for d in policy.SESSION_DAYS:
                target = payload['agents']['codex']['plans'][f'{a}/{d}']
                expected = self.report(archive_days=a, delete_days=d, limit=1)[0]['agents']['codex']['recommendations']
                self.assertEqual(target, expected)
                self.assertLessEqual(len(target['archive']['entries']), 1)

    def test_claude_sidecars_protection_and_deduplication(self):
        sid,p = self.new('claude', 1, 25)
        self.new('claude', 2, 20)
        self.new('claude', 3, 14)
        _, prot = self.new('claude', 4, 30)
        sub = p.parent / sid / 'subagents'
        sub.mkdir(parents=True)
        nested = sub / 'agent-child.jsonl'
        nested.write_text('ignore')
        for f in (sub.parent,sub,nested):
            os.utime(f, ns=(self.now-2*DAY_NS, self.now-2*DAY_NS))
        cfg = policy.config_path();cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({'protect':['claude:'+str(prot.relative_to(self.roots['claude']))]}))
        cd = self.report(agent='claude')[0]['agents']
        self.assertEqual(list(cd), ['claude'])
        agent = cd['claude']
        self.assertEqual(agent['session_count'],4)
        self.assertEqual(agent['held'],1)
        self.assertEqual([agent['recommendations'][k]['count'] for k in ('archive','delete')],[1,1])
        self.assertNotIn('claude rm', str(agent['guidance']))

    def test_index_recency_and_pinned_override_old_file(self):
        one,_ = self.new('codex', 1, 30)
        two,_ = self.new('codex', 2, 30, True)
        three,_ = self.new('codex', 3, 30, True)
        self.new('codex', 4, 35)
        with sqlite3.connect(self.roots['codex']/'state_5.sqlite') as db:
            db.execute('create table threads (id, archived, is_pinned, updated_at_ms, archived_at)')
            db.executemany('insert into threads values(?,?,?,?,?)',[(one,0,0,self.now//10**6,0),
                (two,1,1,0,0),(three,1,0,0,(self.now-DAY_NS)//10**9)])
        cd = self.report(agent='codex')[0]['agents']['codex']
        self.assertEqual(cd['activity_source'],'index+files')
        self.assertEqual(cd['held'],2)
        self.assertEqual([cd['recommendations'][k]['count'] for k in ('archive','delete')],[0,0])

    def test_independent_defaults_and_read_only_export(self):
        self.new('codex',1,15)
        self.new('claude',2,15)
        cfg = policy.config_path();cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({'protect':['claude:cache'], 'retention_days':{'logs':45}}))
        policy.save_session_defaults('claude', {'archive_days':5,'delete_days':10})
        self.assertEqual(policy.load().retention['logs'],45)
        self.assertEqual(policy.load().protect,('claude:cache',))
        doc, rows = self.report()
        self.assertEqual(doc['agents']['claude']['recommendations']['delete']['count'],1)
        self.assertEqual(doc['agents']['codex']['policy']['delete_days'],20)
        out = inventory_report.export(self.home/'report.html',doc,rows)
        self.assertNotIn('DO_NOT_EXPORT',out.read_text())
        self.assertNotIn(str(self.home),out.read_text())
        self.assertEqual(out.stat().st_mode & 0o777,0o600)
        with self.assertRaises(ValueError):
            inventory_report.export(out,doc,rows)
        with self.assertRaises(ValueError):
            policy.save_session_defaults('codex', {'delete_days':0})
        self.assertEqual(policy.load().sessions['codex']['delete_days'],20)

    def test_unreadable_index_withholds_only_codex_recommendations(self):
        self.new('codex',1,30)
        self.new('claude',1,30)
        (self.roots['codex']/'state_5.sqlite').write_bytes(b'not a sqlite database')
        doc, _ = self.report()
        self.assertTrue(doc['errors'])
        self.assertEqual(doc['agents']['codex']['recommendations']['archive']['count'],0)
        self.assertEqual(doc['agents']['claude']['recommendations']['delete']['count'],1)

    def test_titles_precedence_and_safe_export_without_prompts(self):
        sid, _ = self.new('codex', 1, 12)
        other, _ = self.new('codex', 2, 22, archive=True)
        root = self.roots['codex']
        with sqlite3.connect(root/'state_5.sqlite') as db:
            db.execute('create table threads (id, archived, name, title, first_user_message)')
            db.executemany('insert into threads values(?,?,?,?,?)', [
                (sid, 0, None, '默认名称', 'PRIVATE_BODY'), (other, 1, '用户重命名', '旧名称', 'PRIVATE_BODY')])
        (root/'session_index.jsonl').write_text('broken\n'+json.dumps({'id':sid,'thread_name':'\x1b[31m复核\n</script>\u202e周报\x1b[0m'}))
        csid, chat = self.new('claude', 1, 12)
        csid2, chat2 = self.new('claude', 2, 22)
        csid3, chat3 = self.new('claude', 3, 12)
        (chat.parent/'sessions-index.json').write_text(json.dumps({'entries':[
            {'sessionId':csid,'summary':'旧摘要','firstPrompt':'PRIVATE_BODY'},
            {'sessionId':csid2,'summary':'来自项目索引的摘要'},
            {'sessionId':csid3,'firstPrompt':'PRIVATE_BODY'}]}))
        def record(kind, **fields):
            return json.dumps(dict(type=kind, **fields), ensure_ascii=False) + '\n'
        chat.write_text(record('custom-title',customTitle='旧标题',sessionId=csid)
                        + record('assistant',message='PRIVATE_BODY'*9000)
                        + record('custom-title',customTitle='新的自定义标题',sessionId=csid)
                        + record('ai-title',aiTitle='机器标题',sessionId=csid)
                        + record('custom-title',customTitle='别的会话',sessionId=other))
        chat3.write_text(record('user',message={'type':'summary','summary':'PRIVATE_BODY'}))
        for path in [chat, chat3]:
            os.utime(path, ns=(self.now-12*DAY_NS,)*2)
        doc, rows = self.report()
        labels = {r['session_id']:r['title'] for r in rows['codex']}
        self.assertEqual(labels, {sid:'复核 </script> 周报',other:'用户重命名'})
        self.assertEqual({r['session_id']:r['title'] for r in rows['claude']},
                         {csid:'新的自定义标题',csid2:'来自项目索引的摘要',csid3:None})
        html = inventory_report.export(self.home/'safe.html', doc, rows).read_text()
        self.assertNotIn('复核 </script>', html)
        self.assertIn('新的自定义标题', html)
        self.assertNotIn('PRIVATE_BODY', html + str(doc))

    def test_missing_optional_labels_do_not_block_or_change_candidates(self):
        sid, chat = self.new('claude', 1, 14)
        (chat.parent/'sessions-index.json').symlink_to(chat)
        chat.write_text('{not json}\n'+json.dumps({'type':'ai-title','aiTitle':'自动标题','sessionId':sid}))
        os.utime(chat, ns=(self.now-14*DAY_NS,)*2)
        sid2,_ = self.new('codex', 2, 15)
        (self.roots['codex']/'session_index.jsonl').mkdir()
        doc, _ = self.report()
        self.assertFalse(doc['errors'])
        c = doc['agents']['claude']['recommendations']['archive']['entries'][0]
        self.assertEqual((c['session_id'],c['title'],c['idle_days']), (sid,'自动标题',14))
        r = doc['agents']['codex']['recommendations']['archive']['entries'][0]
        self.assertEqual((r['session_id'],r['title']), (sid2,None))
