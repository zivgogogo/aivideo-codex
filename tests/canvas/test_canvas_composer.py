from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from PIL import Image
from backend.canvas.workspace import node
from backend.canvas.studio import StudioCanvas, SHARED
from backend.canvas.sync import snapshot
from backend.canvas.composer import embed_prompts
import backend.canvas.director as director
import backend.canvas.layout as layout


class ComposerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.ws=StudioCanvas(Path(self.temp.name))
        self.pid=self.ws.create(dict(mode='blank',title='合并测试',ratio='16:9'))['project_id'];self.eid='CANVAS001'
        self.source=node('image',0,0,'人物')
        path=self.ws.path(self.pid)/'assets/source.png';path.parent.mkdir(exist_ok=True);Image.new('RGB',(72,128),'blue').save(path)
        self.source.update(assets=[dict(id='adopted',path='assets/source.png')],selected='adopted',selected_ids=['adopted'])
        self.p=node('prompt',350,0,'描述','参考@图1，人物在窗边回头。')
        self.im=node('image',700,0,'画面','清晨的柔和光线。')
        self.video=node('video',1050,0,'片段','缓慢推进。')
        self.g=director.aliases(dict(nodes=[self.source,self.p,self.im,self.video],edges=[dict(id='a',source=self.source['id'],target=self.p['id']),dict(id='b',source=self.p['id'],target=self.im['id']),dict(id='c',source=self.p['id'],target=self.video['id'])],groups=[],viewport=dict(x=0,y=0,zoom=.8),revision=0,layout_version=layout.VERSION,layout_mode='manual'))
        self.put(self.g)
    def tearDown(self):self.ws.pool.shutdown(wait=True);self.temp.cleanup()
    def put(self,g,eid=None):self.ws.mutate(self.pid,lambda d:d['graphs'].update({eid or self.eid:deepcopy(g)}))
    def byid(self,g,n):return next(v for v in g['nodes'] if v['id']==n['id'])

    def test_compiled_recipe_and_assets_survive_fanout_migration(self):
        before=[self.ws.compile(self.pid,self.eid,n['id']) for n in (self.im,self.video)]
        g=self.ws.media_graph(self.pid,self.eid)
        self.assertFalse(any(n['type']=='prompt' for n in g['nodes']))
        self.assertEqual(self.byid(g,self.source)['assets'],self.source['assets'])
        self.assertEqual(self.byid(g,self.source)['selected_ids'],['adopted'])
        for n,old in zip((self.im,self.video),before):
            new=self.ws.compile(self.pid,self.eid,n['id'])
            self.assertEqual(new['prompt'],old['prompt']);self.assertEqual(new['bindings'],old['bindings']);self.assertEqual(new['payload'],old['payload'])
            self.assertEqual(self.byid(g,n)['ref_name'],n['ref_name'])
        im=self.byid(g,self.im);im['text']='仅修改这张图片'
        saved=self.ws.save_graph(self.pid,self.eid,g)
        self.assertIn(self.p['text'],self.byid(saved,self.video)['text'])

    def test_migration_is_idempotent_and_archives_the_complete_original(self):
        g=self.ws.media_graph(self.pid,self.eid);again=self.ws.media_graph(self.pid,self.eid)
        self.assertEqual(g,again)
        d=self.ws.read(self.pid);items=d['graph_history'][self.eid];self.assertEqual(len(items),1)
        archived=json.loads((self.ws.path(self.pid)/items[0]['path']).read_text())
        self.assertEqual(archived['nodes'],self.g['nodes']);self.assertEqual(archived['edges'],self.g['edges'])

    def test_prompt_chains_and_orphans_preserve_work_without_following_media_inputs(self):
        chain=node('prompt',0,0,'补充','低机位。');orphan=node('prompt',0,0,'未连出的文字','另一段尚未开始的创作。')
        g=deepcopy(self.g);g['nodes'] += [chain,orphan]
        g['edges'] += [dict(source=self.p['id'],target=chain['id']),dict(source=chain['id'],target=orphan['id'])]
        original=deepcopy(g);converted=director.aliases(embed_prompts(g));self.assertEqual(g,original)
        standalone=next(n for n in converted['nodes'] if n['id']=='media_'+orphan['id'])
        self.assertEqual(standalone['text'],'\n\n'.join([self.p['text'],chain['text'],orphan['text']]))
        self.assertTrue(any(e['source']==self.source['id'] and e['target']==standalone['id'] for e in converted['edges']))
        self.assertEqual(embed_prompts(converted),converted)

    def test_old_browser_unsaved_delta_updates_media_recipe_and_keeps_remote_fields(self):
        old=deepcopy(self.g);g=self.ws.media_graph(self.pid,self.eid)
        self.byid(g,self.im)['settings']['ratio']='1:1';self.ws.save_graph(self.pid,self.eid,g)
        self.byid(old,self.p)['text']='旧窗口里尚未保存的描述。'
        saved=self.ws.save_graph(self.pid,self.eid,{**old,'base':snapshot(self.g)})
        self.assertFalse(any(n['type']=='prompt' for n in saved['nodes']))
        self.assertEqual(self.byid(saved,self.im)['text'],'旧窗口里尚未保存的描述。\n\n清晨的柔和光线。')
        self.assertEqual(self.byid(saved,self.im)['settings']['ratio'],'1:1')
        self.assertEqual(self.byid(saved,self.source)['selected'],'adopted')
        self.assertIn('旧窗口里尚未保存',self.byid(saved,self.video)['text'])

    def test_long_composed_prompt_is_not_truncated_during_autosave(self):
        g=deepcopy(self.g);self.byid(g,self.p)['text']='长描述。'*5000;self.put(g)
        converted=self.ws.media_graph(self.pid,self.eid);before=self.byid(converted,self.im)['text']
        self.assertGreater(len(before),18000)
        saved=self.ws.save_graph(self.pid,self.eid,converted)
        self.assertEqual(self.byid(saved,self.im)['text'],before)

    def test_shared_character_recipe_remains_live_and_assets_stay_on_master(self):
        shared=director.character_graph([dict(id='C1',name='林夏',prompt='黑发，米白针织衫。')]);master=next(n for n in shared['nodes'] if n['type']=='image')
        master.update(assets=deepcopy(self.source['assets']),selected='adopted')
        self.put(shared,SHARED)
        proxy=node('image',0,0,'林夏',shared_node_id=master['id']);g=deepcopy(self.g);g['nodes'].append(proxy);self.put(g)
        self.ws.media_graph(self.pid,SHARED);episode=self.ws.media_graph(self.pid,self.eid)
        self.assertEqual(self.byid(episode,proxy)['global_prompt'],'黑发，米白针织衫。')
        shared=self.ws.graph(self.pid,SHARED);self.byid(shared,master)['text']='同一个人，表情改为微笑。';self.ws.save_graph(self.pid,SHARED,shared)
        self.assertEqual(self.byid(self.ws.media_graph(self.pid,self.eid),proxy)['global_prompt'],'同一个人，表情改为微笑。')
        raw=self.ws.read(self.pid)['graphs'][self.eid];self.assertEqual(self.byid(raw,proxy)['assets'],[])

    def test_legacy_video_reference_to_image_remains_blocked_but_canvas_can_save(self):
        source=node('video',0,0,'视频参考');g=deepcopy(self.g);g['nodes'].append(source);g['edges'].append(dict(source=source['id'],target=self.p['id']));self.put(g)
        converted=self.ws.media_graph(self.pid,self.eid);saved=self.ws.save_graph(self.pid,self.eid,converted)
        self.assertTrue(any(e['source']==source['id'] and e['target']==self.im['id'] for e in saved['edges']))
        with self.assertRaisesRegex(ValueError,'只能生成视频'):self.ws.compile(self.pid,self.eid,self.im['id'])

    def test_media_layout_contains_multiple_shots_without_overlapping_cards(self):
        g=embed_prompts(self.g);cid='clip';g['groups']=[dict(id=cid,title='多分镜段落',duration=12,video_node_id=self.video['id'])]
        self.byid(g,self.im)['clip_id']=cid;self.byid(g,self.video)['clip_id']=cid
        g['nodes'] += [node('image',0,0,'分镜',clip_id=cid) for _ in range(4)]
        layout.arrange(g);group=g['groups'][0];self.assertEqual(group['shot_count'],5)
        for n in [n for n in g['nodes'] if n.get('clip_id')==cid]:
            self.assertGreaterEqual(n['x'],group['x']);self.assertGreaterEqual(n['y'],group['y'])
            self.assertLessEqual(n['x']+310,group['x']+group['width']);self.assertLessEqual(n['y']+250,group['y']+group['height'])
        for i,a in enumerate(g['nodes']):
            for b in g['nodes'][i+1:]:self.assertTrue(abs(a['x']-b['x'])>=310 or abs(a['y']-b['y'])>=250)

    @unittest.skipUnless(shutil.which('node'),'node required')
    def test_browser_draft_conversion_matches_server_and_rebase(self):
        orphan=node('prompt',0,0,'空白创作','还有未连出的文字');g=deepcopy(self.g);g['nodes'].append(orphan)
        script="const c=require('./frontend/canvas/canvas-compose.js');const s=require('./frontend/canvas/canvas-sync.js');let g=JSON.parse(process.argv[1]);const base=c.embed(g);g.nodes.find(n=>n.type==='prompt').text='浏览器草稿';const local=c.embed(g);process.stdout.write(JSON.stringify({base,merged:s.rebase(base,local,base)}));"
        result=json.loads(subprocess.check_output(['node','-e',script,json.dumps(g)]))
        self.assertEqual(result['base'],director.aliases(embed_prompts(g)))
        self.assertIn('浏览器草稿',self.byid(result['merged'],self.im)['text'])

if __name__=='__main__':unittest.main()
