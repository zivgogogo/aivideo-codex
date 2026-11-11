from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from backend.canvas.workspace import node
from backend.canvas.studio import StudioCanvas
from backend.canvas.sync import snapshot


class AutosaveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.ws=StudioCanvas(Path(self.temp.name))
        self.pid=self.ws.create(dict(mode='blank',title='自由创作',ratio='16:9'))['project_id'];self.eid='CANVAS001'
        g=self.ws.graph(self.pid,self.eid)
        g['nodes']=[node('prompt',60,190,'描述','最初的描述'),node('image',450,190,'图片')]
        self.g=self.ws.save_graph(self.pid,self.eid,g);self.p,self.im=self.g['nodes']

    def tearDown(self):
        self.ws.pool.shutdown(wait=True);self.temp.cleanup()

    def save(self,edited,base=None,**extra):
        return self.ws.save_graph(self.pid,self.eid,{**edited,'base':snapshot(base or self.g),**extra})

    def test_blank_project_is_ready_without_model_calls_or_placeholder_nodes(self):
        with patch.object(self.ws,'initialize',side_effect=AssertionError('blank project must not initialize')):
            pid=self.ws.create(dict(mode='blank',title='第二个项目'))['project_id']
        detail=self.ws.detail(pid);graph=self.ws.graph(pid,'CANVAS001')
        self.assertEqual(detail['status'],'ready');self.assertEqual(detail['jobs'],[])
        self.assertEqual(graph['nodes'],[]);self.assertTrue(detail['characters_initialized'])
        self.assertEqual(next(p for p in self.ws.projects() if p['id']==pid)['mode'],'blank')

    def test_two_windows_merge_different_fields_and_settings(self):
        a=deepcopy(self.g);b=deepcopy(self.g)
        a['nodes'][0]['text']='第一个窗口编辑Prompt';a['nodes'][1]['settings']['n']=3
        self.save(a)
        b['nodes'][0]['x']=185;b['nodes'][1]['settings']['ratio']='1:1'
        merged=self.save(b)
        self.assertEqual(merged['nodes'][0]['text'],a['nodes'][0]['text'])
        self.assertEqual(merged['nodes'][0]['x'],185)
        self.assertEqual(merged['nodes'][1]['settings']['n'],3)
        self.assertEqual(merged['nodes'][1]['settings']['ratio'],'1:1')
        self.assertTrue(merged['sync']['merged']);self.assertFalse(merged['sync']['recovered'])

    def test_unacknowledged_retry_is_idempotent_and_assets_remain_server_owned(self):
        def attach(d):
            d['graphs'][self.eid]['nodes'][1].update(assets=[dict(id='real',path='assets/original.png')],selected='real')
            d['graphs'][self.eid]['revision']+=1
        self.ws.mutate(self.pid,attach)
        edited=deepcopy(self.g);edited['nodes'][0]['text']='已经发送的描述'
        edited['nodes'][1].update(assets=[dict(id='forged',url='bad')],selected='forged')
        first=self.save(edited);second=self.save(edited)
        self.assertEqual(first['nodes'],second['nodes']);self.assertEqual(second['nodes'][1]['selected'],'real')
        self.assertEqual(second['nodes'][1]['assets'][0]['id'],'real')

    def test_remote_additions_and_deleted_nodes_are_not_overwritten_by_stale_save(self):
        remote=deepcopy(self.g);remote['nodes'].append(node('video',840,190,'新视频'))
        remote['nodes']=[n for n in remote['nodes'] if n['id']!=self.im['id']]
        self.save(remote)
        local=deepcopy(self.g);local['nodes'][0]['text']='另一个窗口的改动'
        saved=self.save(local);ids={n['id'] for n in saved['nodes']}
        self.assertNotIn(self.im['id'],ids);self.assertIn(remote['nodes'][1]['id'],ids)

    def test_edges_merge_and_explicit_deletion_does_not_delete_remote_edges(self):
        g=deepcopy(self.g);g['nodes'].append(node('video',840,190,'视频'))
        g['edges']=[dict(id='first',source=self.p['id'],target=self.im['id'])]
        base=self.save(g)
        remote=deepcopy(base);remote['edges'].append(dict(id='second',source=self.im['id'],target=base['nodes'][2]['id']))
        self.save(remote,base)
        local=deepcopy(base);local['edges']=[]
        result=self.save(local,base)
        self.assertEqual([e['id'] for e in result['edges']],['second'])

    def test_same_field_uses_latest_edit_and_preserves_displaced_text_in_history(self):
        a=deepcopy(self.g);b=deepcopy(self.g)
        a['nodes'][0]['text']='其他窗口的新版本';b['nodes'][0]['text']='当前窗口的最新版本'
        self.save(a);result=self.save(b)
        self.assertEqual(result['nodes'][0]['text'],b['nodes'][0]['text'])
        self.assertEqual(result['sync']['recovered'],1)
        self.assertEqual(self.ws.save_history(self.pid,self.eid)[0]['fields'][0]['previous'],a['nodes'][0]['text'])
        self.assertNotIn('save_recovery',self.ws.detail(self.pid))

    def test_concurrent_edges_that_would_create_cycle_are_rejected_without_data_loss(self):
        g=deepcopy(self.g);g['nodes'][1]=node('prompt',450,190,'另一个描述')
        base=self.save(g)
        a=deepcopy(base);b=deepcopy(base)
        a['edges']=[dict(id='a',source=base['nodes'][0]['id'],target=base['nodes'][1]['id'])]
        b['edges']=[dict(id='b',source=base['nodes'][1]['id'],target=base['nodes'][0]['id'])]
        self.save(a,base)
        with self.assertRaisesRegex(ValueError,'循环'):self.save(b,base)
        self.assertEqual(len(self.ws.graph(self.pid,self.eid)['edges']),1)

    @unittest.skipUnless(shutil.which('node'),'Node required for frontend graph-rebase checks')
    def test_media_text_moves_to_prompt_cards_without_changing_other_outputs_or_assets(self):
        module=Path(__file__).resolve().parents[2]/'frontend/canvas/canvas-sync.js'
        script=r'''
const assert=require('node:assert/strict'),sync=require(process.argv[1]);let count=0;
const create=position=>({id:'new'+(++count),type:'prompt',title:'Prompt卡',text:'',...position,settings:{}});
const media=(id,text)=>({id,type:'image',text,x:450,y:190,assets:[{id:'adopted'}],selected:'adopted',settings:{ratio:'9:16'}});
const p={id:'p',type:'prompt',text:'原来的描述',x:60,y:190};
const g={nodes:[p,media('image','已有的局部描述')],edges:[{id:'e',source:'p',target:'image'}]};
assert(sync.separatePrompts(g,create));assert.equal(g.nodes.length,2);
assert.equal(p.text,'原来的描述\n\n已有的局部描述'.replaceAll('\\n','\n'));
assert.equal(g.nodes[1].text,'');assert.equal(g.nodes[1].selected,'adopted');assert.equal(g.nodes[1].assets[0].id,'adopted');
assert.equal(sync.separatePrompts(g,create),false);
const shared={id:'shared',type:'prompt',text:'共用描述',x:60,y:190};
const branches={nodes:[shared,media('a','仅A使用的文字'),media('b','')],edges:[{source:'shared',target:'a'},{source:'shared',target:'b'}]};
sync.separatePrompts(branches,create);assert.equal(shared.text,'共用描述');assert.equal(branches.nodes.length,4);
const added=branches.nodes.find(n=>n.id.startsWith('new'));assert.equal(added.text,'仅A使用的文字');
assert(branches.edges.some(e=>e.source===added.id&&e.target==='a'));assert(!branches.edges.some(e=>e.source===added.id&&e.target==='b'));
const standalone={nodes:[media('alone','独立图片的描述')],edges:[]};
sync.separatePrompts(standalone,create);assert.equal(standalone.nodes[0].text,'');assert.equal(standalone.nodes[1].text,'独立图片的描述');
'''
        subprocess.run(['node','-e',script,str(module)],check=True,capture_output=True,text=True)

    @unittest.skipUnless(shutil.which('node'),'Node required for frontend graph-rebase checks')
    def test_frontend_rebases_inflight_edits_and_recovers_legacy_drafts(self):
        module=Path(__file__).resolve().parents[2]/'frontend/canvas/canvas-sync.js'
        script=r'''
const assert=require('node:assert/strict'),sync=require(process.argv[1]);
const clone=g=>JSON.parse(JSON.stringify(g));
const base={nodes:[{id:'p',type:'prompt',x:1,y:2,text:'原文',settings:{}},{id:'im',type:'image',x:3,y:2,text:'',settings:{n:1,ratio:'9:16'}}],edges:[],viewport:{x:0,y:0,zoom:1},layout_mode:'manual'};
const submitted=clone(base);submitted.nodes[0].text='发出请求时的文字';
const response=clone(submitted);response.nodes[1].selected='server-image';response.nodes[1].settings.n=3;
response.nodes.push({id:'remote',type:'video',x:4,y:2,text:'',settings:{}});
const pending=clone(submitted);pending.nodes[0].text='保存期间继续输入';pending.nodes[1].settings.ratio='1:1';
const merged=sync.rebase(response,pending,submitted);
assert.equal(merged.nodes[0].text,'保存期间继续输入');assert.equal(merged.nodes[1].selected,'server-image');
assert.equal(merged.nodes[1].settings.n,3);assert.equal(merged.nodes[1].settings.ratio,'1:1');assert.equal(merged.nodes.length,3);
const newer=clone(base);newer.nodes[0].text='人物参考：@图1（人物）。\n\n原文';
const draft=clone(base);draft.nodes[0].text='上次未保存的文字';
const recovered=sync.recoverLegacy(newer,draft);assert.match(recovered.nodes[0].text,/^人物参考：@图1/);assert.match(recovered.nodes[0].text,/上次未保存的文字$/);
const deleted=clone(response);deleted.nodes=deleted.nodes.filter(n=>n.id!=='im');
assert.equal(sync.rebase(deleted,submitted,submitted).nodes.some(n=>n.id==='im'),false);
assert(sync.equal(sync.snapshot(base),sync.snapshot(clone(base))));
'''
        subprocess.run(['node','-e',script,str(module)],check=True,capture_output=True,text=True)
