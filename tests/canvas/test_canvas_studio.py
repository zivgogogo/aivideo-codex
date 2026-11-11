import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from PIL import Image

from backend.canvas.workspace import node
from backend.canvas.studio import StudioCanvas, SHARED
import backend.canvas.director as director
from core.store import now


def shot(**fields):
    return dict(duration=3,title='回头',character_ids=['C001'],subject='林夏，黑发，米色针织衫',action='听到脚步后缓慢回头',scene='窗边，清晨，木桌在前景',framing='中近景，人物在左三分线',camera='50mm，平视固定，浅景深',lighting='左侧自然窗光',emotion='惊讶转为克制期待',continuity_in='右手握住信封',continuity_out='回头后右手仍握住信封',**fields)


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.ws=StudioCanvas(Path(self.temp.name)/'canvas');self.pid='studio'
        self.ws.save(dict(id=self.pid,title='林间来信',style='暖色电影质感',ratio='9:16',status='ready',created_at=now(),episodes=[dict(id='EP001',title='相遇',source_text='林夏：你来了。'),dict(id='EP002',title='重逢',source_text='林夏：又见面了。')],graphs={},jobs=[]))
        self.p=node('prompt',60,190,'镜头描述','窗边的林夏回头。')
        self.i1=node('image',450,190,'人物母版');self.i2=node('image',450,900,'窗边分镜');self.v=node('video',840,190,'回头段落')
        self.graph=director.aliases(dict(nodes=[self.p,self.i1,self.i2,self.v],edges=[],groups=[],viewport=dict(x=30,y=45,zoom=.8),revision=0))
        self.ws.mutate(self.pid,lambda d:d['graphs'].update(EP001=self.graph))

    def tearDown(self):self.ws.pool.shutdown(wait=True);self.temp.cleanup()
    def wait(self,result):
        for _ in range(300):
            job=next(j for j in self.ws.read(self.pid)['jobs'] if j['id']==result['job_id'])
            if job['status'] not in {'running','queued'}:return job
            threading.Event().wait(.01)
        self.fail('job did not complete')
    def frame(self,nid,color='blue'):
        data=io.BytesIO();Image.new('RGB',(72,128),color).save(data,'PNG')
        return self.ws.upload(self.pid,'EP001',nid,dict(data=base64.b64encode(data.getvalue()).decode()))
    def modify(self,fn):self.ws.mutate(self.pid,lambda d:fn(d['graphs']['EP001']))
    def edge(self,s,t):self.modify(lambda g:g['edges'].append(dict(id='e'+str(len(g['edges'])),source=s['id'],target=t['id'])))

    def test_multi_image_edit_and_reference_alias_order(self):
        self.frame(self.i1['id'],'blue');self.frame(self.i2['id'],'red')
        target=node('image',1230,190,'新场景','参考@图2的场景，使用@图片1的人物回头。')
        self.modify(lambda g:g['nodes'].append(target));self.edge(self.i1,target);self.edge(self.i2,target)
        c=self.ws.compile(self.pid,'EP001',target['id'])
        self.assertEqual(c['route'],'qwen-image-edit');self.assertEqual([b['node_id'] for b in c['bindings']],[self.i2['id'],self.i1['id']])
        self.assertIn('参考图1的场景，使用图2的人物',c['prompt'])
        self.assertEqual(len(c['payload']['input']['messages'][0]['content']),3)

    def test_multiframe_video_goes_to_r2v_not_first_frame(self):
        self.frame(self.i1['id']);self.frame(self.i2['id'])
        self.edge(self.p,self.v);self.edge(self.i1,self.v);self.edge(self.i2,self.v)
        self.modify(lambda g:g['nodes'][-1]['settings'].update(duration=11))
        c=self.ws.compile(self.pid,'EP001',self.v['id'])
        self.assertEqual(c['payload']['model'],'happyhorse-1.1-r2v');self.assertEqual(c['payload']['parameters']['duration'],11)
        self.assertEqual([m['type'] for m in c['payload']['input']['media']],['reference_image','reference_image'])

    def test_generated_video_and_images_route_to_edit_with_correct_parameters(self):
        self.frame(self.i1['id'])
        source=node('video',20,200,'源视频');source.update(assets=[dict(id='origin',remote_url='https://cdn.example/source.mp4',url='https://cdn.example/source.mp4')],selected='origin')
        self.modify(lambda g:g['nodes'].append(source));self.edge(source,self.v);self.edge(self.i1,self.v)
        self.modify(lambda g:next(n for n in g['nodes'] if n['id']==self.v['id']).update(text='参考@视频2和@图1，改成中世纪爱情故事。'))
        c=self.ws.compile(self.pid,'EP001',self.v['id']);p=c['payload']
        self.assertEqual(p['model'],'happyhorse-1.0-video-edit');self.assertNotIn('duration',p['parameters']);self.assertNotIn('ratio',p['parameters'])
        self.assertEqual(p['input']['media'][0],dict(type='video',url='https://cdn.example/source.mp4'))
        self.assertIn('输入视频',p['input']['prompt']);self.assertIn('[Image 1]',p['input']['prompt'])

    def test_one_image_first_frame_and_two_images_wan_frames(self):
        self.frame(self.i1['id']);self.frame(self.i2['id']);self.edge(self.p,self.v);self.edge(self.i1,self.v)
        self.assertEqual(self.ws.compile(self.pid,'EP001',self.v['id'])['payload']['model'],'happyhorse-1.1-i2v')
        self.edge(self.i2,self.v)
        self.modify(lambda g:g['nodes'][-1]['settings'].update(model='wan',mode='frames',duration=9))
        p=self.ws.compile(self.pid,'EP001',self.v['id'])['payload']
        self.assertEqual([m['type'] for m in p['input']['media']],['first_frame','last_frame'])
        self.assertEqual(p['parameters']['duration'],9)
        self.modify(lambda g:g['nodes'][-1]['settings'].update(model='happyhorse',mode='first_frame'))
        with self.assertRaisesRegex(ValueError,'首帧'):self.ws.compile(self.pid,'EP001',self.v['id'])

    def test_cycle_and_forged_shared_reference_rejected(self):
        g=self.ws.graph(self.pid,'EP001');g['edges']=[dict(source=self.i1['id'],target=self.i2['id']),dict(source=self.i2['id'],target=self.i1['id'])]
        with self.assertRaisesRegex(ValueError,'循环'):self.ws.save_graph(self.pid,'EP001',g)
        g['edges']=[];g['nodes'][1]['shared_node_id']='not-a-person'
        with self.assertRaisesRegex(ValueError,'共享人物'):self.ws.save_graph(self.pid,'EP001',g)

    def test_shared_adopted_versions_are_live_in_two_episodes(self):
        char=dict(id='C001',name='林夏',prompt='黑色长发中国年轻女性，米白针织衫，中性光单人母版。')
        shared=director.character_graph([char]);im=shared['nodes'][1]
        im.update(assets=[dict(id='a1',path='assets/1.png'),dict(id='a2',path='assets/2.png')],selected='a1')
        def setup(d):
            d['graphs'][SHARED]=shared
            for eid in ['EP001','EP002']:
                g=director.empty_graph();g['cast_attached']=True;g['nodes']=[node('image',0,0,'林夏',shared_node_id=im['id'])];d['graphs'][eid]=g
        self.ws.mutate(self.pid,setup)
        self.assertEqual(self.ws.graph(self.pid,'EP001')['nodes'][0]['selected'],'a1')
        self.ws.select(self.pid,SHARED,im['id'],'a2')
        self.assertEqual(self.ws.graph(self.pid,'EP001')['nodes'][0]['selected'],'a2')
        self.assertEqual(self.ws.graph(self.pid,'EP002')['nodes'][0]['selected'],'a2')
        self.assertEqual(self.ws.read(self.pid)['graphs']['EP001']['nodes'][0]['assets'],[])

    def test_multi_upload_selections_expand_to_multiple_reference_inputs(self):
        a=self.frame(self.i1['id']);b=self.frame(self.i1['id'],'red');self.ws.select(self.pid,'EP001',self.i1['id'],[b['id'],a['id']])
        self.edge(self.p,self.v);self.edge(self.i1,self.v)
        c=self.ws.compile(self.pid,'EP001',self.v['id'])
        self.assertEqual(len(c['images']),2);self.assertEqual([r['asset_id'] for r in c['bindings']],[b['id'],a['id']])

    def test_missing_unknown_and_self_references_are_not_silently_ignored(self):
        self.modify(lambda g:g['nodes'][-1].update(text='参考@图1'))
        with self.assertRaisesRegex(ValueError,'尚未生成'):self.ws.compile(self.pid,'EP001',self.v['id'])
        self.modify(lambda g:g['nodes'][-1].update(text='参考@图99'))
        with self.assertRaisesRegex(ValueError,'找不到'):self.ws.compile(self.pid,'EP001',self.v['id'])
        self.modify(lambda g:g['nodes'][-1].update(text='参考@视频1'))
        with self.assertRaisesRegex(ValueError,'自身'):self.ws.compile(self.pid,'EP001',self.v['id'])

    def test_edit_current_character_uses_adopted_image_without_self_cycle(self):
        self.frame(self.i1['id'])
        self.modify(lambda g:(g['nodes'][1].update(text='参考@图1，把表情改为微笑。'),g['nodes'][1]['settings'].update(use_current=True)))
        c=self.ws.compile(self.pid,'EP001',self.i1['id'])
        self.assertEqual(c['route'],'qwen-image-edit')
        self.assertEqual(c['bindings'][0]['node_id'],self.i1['id'])
        self.assertIn('参考图1',c['prompt'])

    def test_reference_limits_and_local_video_are_enforced(self):
        self.frame(self.i1['id']);self.edge(self.p,self.i2)
        self.modify(lambda g:g['nodes'][2].update(text='新场景'))
        for _ in range(4):self.frame(self.i1['id'])
        ids=[a['id'] for a in self.ws.graph(self.pid,'EP001')['nodes'][1]['assets']];self.ws.select(self.pid,'EP001',self.i1['id'],ids);self.edge(self.i1,self.i2)
        with self.assertRaisesRegex(ValueError,'3'):self.ws.compile(self.pid,'EP001',self.i2['id'])
        with self.assertRaisesRegex(ValueError,'公网'):self.ws.reference_video(self.pid,'EP001',self.v['id'],'http://127.0.0.1/test.mp4')

    def test_director_validates_timing_cast_coverage_and_five_aspects(self):
        beats=[dict(beat_id='B0001',dialogue=[dict(text='你来了。')])]
        clip=dict(title='相遇',duration=6,beat_ids=['B0001'],shots=[shot(),shot()])
        self.assertEqual(len(director.validate_clips(dict(clips=[clip]),beats,{'C001'})),1)
        bad=deepcopy(clip);bad['shots'][0]['camera']=''
        with self.assertRaisesRegex(ValueError,'摄影'):director.validate_clips(dict(clips=[bad]),beats,{'C001'})
        bad=deepcopy(clip);bad['duration']=7
        with self.assertRaisesRegex(ValueError,'之和'):director.validate_clips(dict(clips=[bad]),beats,{'C001'})
        bad=deepcopy(clip);bad['beat_ids']=[]
        with self.assertRaisesRegex(ValueError,'覆盖'):director.validate_clips(dict(clips=[bad]),beats,{'C001'})

    def test_cinematic_hierarchy_timing_edit_and_dialogue_preservation(self):
        c=dict(id='C001',name='林夏',prompt='黑发米白衣人物母版');shared=director.character_graph([c])
        clips=[dict(title='相遇',duration=6,beat_ids=['B0001'],intent='揭示来者',transition='声音桥',shots=[shot(),shot()])]
        beats=[dict(beat_id='B0001',dialogue=[dict(source_speaker='林夏',text='你来了。')])]
        g=director.cinematic_graph('EP001',clips,beats,[c],shared,'9:16')
        self.assertEqual(len(g['groups']),1);self.assertEqual(len(g['groups'][0]['shots']),2)
        vp=next(n for n in g['nodes'] if n.get('role')=='video_prompt');self.assertIn('林夏：你来了。',vp['text'])
        self.assertIn('0–3秒',vp['text']);self.assertIn('@图',vp['text'])
        video=next(n for n in g['nodes'] if n['type']=='video');video['settings']['duration']=12
        for n in g['nodes']:
            if n['type']=='image' and not n.get('shared_node_id'):
                path=self.ws.path(self.pid)/('assets/'+n['id']+'.png');path.parent.mkdir(exist_ok=True);Image.new('RGB',(72,128)).save(path)
                n.update(assets=[dict(id=n['id'],path='assets/'+n['id']+'.png')],selected=n['id'])
        self.ws.mutate(self.pid,lambda d:(d['graphs'].update({SHARED:shared,'EP001':g}),d.update(characters_initialized=True)))
        compiled=self.ws.compile(self.pid,'EP001',video['id'])
        self.assertIn('0–6秒',compiled['prompt']);self.assertIn('6–12秒',compiled['prompt']);self.assertEqual(compiled['payload']['parameters']['duration'],12)
        self.ws.mutate(self.pid,lambda d:next(n for n in d['graphs']['EP001']['nodes'] if n.get('role')=='video_prompt').update(text='视频总时长12秒；0–6秒，参考@图2；6–12秒，参考@图3。'))
        self.assertIn('0–6秒',self.ws.compile(self.pid,'EP001',video['id'])['prompt'])

    def reference_flow_graph(self):
        chars=[dict(id='C001',name='林夏',prompt='黑发米白衣女性人物母版'),dict(id='C002',name='顾川',prompt='短发深色大衣男性人物母版')]
        shared=director.character_graph(chars);s=shot();s['character_ids']=['C001','C002']
        g=director.cinematic_graph('EP001',[dict(title='相遇',duration=3,beat_ids=[],shots=[s])],[],chars,shared,'9:16')
        return shared,g

    def test_director_characters_feed_the_shot_prompt_with_explicit_mentions(self):
        _,g=self.reference_flow_graph()
        p=next(n for n in g['nodes'] if n.get('role')=='shot_prompt');im=next(n for n in g['nodes'] if n.get('role')=='shot_image')
        refs=[n for n in g['nodes'] if n.get('shared_node_id')]
        self.assertIn('@图1（林夏）',p['text']);self.assertIn('@图2（顾川）',p['text'])
        self.assertEqual({e['source'] for e in g['edges'] if e['target']==p['id']},{n['id'] for n in refs})
        self.assertEqual([e['source'] for e in g['edges'] if e['target']==im['id']],[p['id']])
        self.assertEqual(g['prompt_flow_version'],director.PROMPT_FLOW_VERSION)

    def test_existing_director_flow_migrates_without_changing_assets_or_positions(self):
        shared,g=self.reference_flow_graph();g.pop('prompt_flow_version')
        p=next(n for n in g['nodes'] if n.get('role')=='shot_prompt');im=next(n for n in g['nodes'] if n.get('role')=='shot_image')
        p['text']='用户保留的镜头描述：两人在窗边对视。';im['text']='保留这条额外修改。'
        for e in g['edges']:
            if e['target']==p['id']:e['target']=im['id']
        im.update(assets=[dict(id='existing',path='assets/already-generated.png')],selected='existing',selected_ids=['existing'])
        positions=[(n['id'],n['x'],n['y']) for n in g['nodes']];old_assets=deepcopy(im['assets']);old_revision=g['revision']
        self.ws.mutate(self.pid,lambda d:(d['graphs'].update({SHARED:shared,'EP001':g}),d.update(characters_initialized=True)))
        migrated=self.ws.graph(self.pid,'EP001');mp=next(n for n in migrated['nodes'] if n['id']==p['id']);mi=next(n for n in migrated['nodes'] if n['id']==im['id'])
        self.assertIn('用户保留的镜头描述',mp['text']);self.assertIn('@图1',mp['text']);self.assertIn('@图2',mp['text'])
        self.assertEqual(mi['text'],im['text']);self.assertEqual(mi['assets'],old_assets);self.assertEqual(mi['selected_ids'],['existing'])
        self.assertEqual(positions,[(n['id'],n['x'],n['y']) for n in migrated['nodes']])
        self.assertEqual([e['source'] for e in migrated['edges'] if e['target']==im['id']],[p['id']])
        self.assertGreater(migrated['revision'],old_revision)
        again=self.ws.graph(self.pid,'EP001');self.assertEqual(again,migrated)
        saved=self.ws.save_graph(self.pid,'EP001',migrated);self.assertEqual(saved['prompt_flow_version'],director.PROMPT_FLOW_VERSION)

    def test_reference_migration_preserves_shared_prompt_fanout_and_existing_mentions(self):
        _,g=self.reference_flow_graph();g.pop('prompt_flow_version')
        p=next(n for n in g['nodes'] if n.get('role')=='shot_prompt');im=next(n for n in g['nodes'] if n.get('role')=='shot_image')
        p['text']='保持@图片1与@顾川的人物身份。'
        for e in g['edges']:
            if e['target']==p['id']:e['target']=im['id']
        before=p['text'];director.bind_character_prompts(g);self.assertEqual(p['text'],before)
        snapshot=deepcopy(g);self.assertFalse(director.bind_character_prompts(g));self.assertEqual(snapshot,g)
        g.pop('prompt_flow_version');extra=node('image',0,0,'手动分支');g['nodes'].append(extra)
        g['edges'].append(dict(id='fanout',source=p['id'],target=extra['id']))
        for e in g['edges']:
            if e['target']==p['id']:e['target']=im['id']
        director.bind_character_prompts(g)
        self.assertEqual({e['target'] for e in g['edges'] if e['source']==p['id']},{im['id'],extra['id']})
        self.assertEqual(len([e for e in g['edges'] if e['target']==p['id']]),2)
        self.assertEqual(p['text'],before)

    def test_reference_migration_leaves_user_created_prompt_wiring_unchanged(self):
        _,g=self.reference_flow_graph();g.pop('prompt_flow_version')
        p=next(n for n in g['nodes'] if n.get('role')=='shot_prompt');im=next(n for n in g['nodes'] if n.get('role')=='shot_image')
        p.pop('role');p['text']='用户自建的主描述。'
        for e in g['edges']:
            if e['target']==p['id']:e['target']=im['id']
        before=deepcopy(g['edges']);director.bind_character_prompts(g)
        self.assertEqual(g['edges'],before);self.assertEqual(p['text'],'用户自建的主描述。')

    def test_reference_migration_waits_for_active_episode_jobs(self):
        shared,g=self.reference_flow_graph();g.pop('prompt_flow_version')
        p=next(n for n in g['nodes'] if n.get('role')=='shot_prompt');im=next(n for n in g['nodes'] if n.get('role')=='shot_image')
        for e in g['edges']:
            if e['target']==p['id']:e['target']=im['id']
        self.ws.mutate(self.pid,lambda d:(d['graphs'].update({SHARED:shared,'EP001':g}),d.update(characters_initialized=True),d['jobs'].append(dict(id='busy',kind='image',status='running',episode_id='EP001'))))
        self.assertNotIn('prompt_flow_version',self.ws.graph(self.pid,'EP001'))
        self.ws.mutate(self.pid,lambda d:d['jobs'][0].update(status='succeeded'))
        self.assertEqual(self.ws.graph(self.pid,'EP001')['prompt_flow_version'],director.PROMPT_FLOW_VERSION)

    def test_director_cache_does_not_repeat_successful_llm_batches(self):
        client=Mock();client.generate.return_value=json.dumps(dict(clips=[dict(title='相遇',duration=6,beat_ids=['B0001'],intent='相遇',transition='硬切',shots=[shot(),shot()])]))
        folder=Path(self.temp.name)/'planning';folder.mkdir()
        director.plan_episode('林夏：你来了。',[dict(id='C001',name='林夏')],{},folder,client)
        director.plan_episode('林夏：你来了。',[dict(id='C001',name='林夏')],{},folder,client)
        self.assertEqual(client.generate.call_count,1)

    def test_episode_title_is_metadata_never_dialogue(self):
        client=Mock();client.generate.return_value=json.dumps(dict(clips=[dict(title='相遇',duration=6,beat_ids=['B0001'],shots=[shot(),shot()])]))
        folder=Path(self.temp.name)/'titles';folder.mkdir()
        _,beats,speeches=director.plan_episode('第一集：门外的脚步\n林夏：你来了。',[dict(id='C001',name='林夏')],{},folder,client)
        self.assertEqual([s['speaker'] for s in speeches],['林夏'])
        self.assertEqual(beats[0]['dialogue'][0]['text'],'你来了。')

    def test_multimage_download_resume_avoids_duplicate_generation(self):
        self.edge(self.p,self.i1)
        urls=['https://cdn.example/a.png','https://cdn.example/b.png']
        response=dict(output=dict(choices=[dict(message=dict(content=[dict(image=u) for u in urls]))]))
        with patch('backend.canvas.studio.generate_image',return_value=response) as generate,patch('backend.canvas.studio.download_image',side_effect=RuntimeError('network')):
            j=self.wait(self.ws.generate(self.pid,'EP001',self.i1['id']));self.assertEqual(j['status'],'failed');self.assertEqual(j['image_urls'],urls)
        with patch('backend.canvas.studio.generate_image') as generate,patch('backend.canvas.studio.download_image',side_effect=lambda url,p:Path(p).write_bytes(b'PNG')):
            j=self.wait(self.ws.generate(self.pid,'EP001',self.i1['id']));self.assertEqual(j['status'],'succeeded');generate.assert_not_called()
        n=self.ws.graph(self.pid,'EP001')['nodes'][1];self.assertEqual(len(n['selected_ids']),2)

    def test_video_remote_url_is_saved_before_download_and_can_feed_edit(self):
        self.edge(self.p,self.v);client=Mock();client.submit.return_value={'output':{'task_id':'t1'}};client.wait_for_task.return_value={'output':{'video_url':'https://cdn.example/out.mp4'}}
        def download(url,path):
            self.assertEqual(self.ws.read(self.pid)['jobs'][-1]['remote_url'],url);Path(path).write_bytes(b'video')
        client.download_video.side_effect=download
        with patch('backend.canvas.studio.DashScopeVideoClient',return_value=client):
            j=self.wait(self.ws.generate(self.pid,'EP001',self.v['id']));self.assertEqual(j['status'],'succeeded')
        v=self.ws.graph(self.pid,'EP001')['nodes'][-1];self.assertEqual(v['assets'][0]['remote_url'],'https://cdn.example/out.mp4')

    def test_manual_video_group_and_selection_survive_save(self):
        g=self.ws.graph(self.pid,'EP001');g['nodes'][-1]['clip_id']='manual-video'
        result=self.ws.save_graph(self.pid,'EP001',g)
        self.assertEqual(result['groups'][0]['id'],'manual-video')
        self.assertEqual(self.ws.graph(self.pid,'EP001')['groups'][0]['video_node_id'],self.v['id'])
        removed=deepcopy(result);removed['nodes']=[n for n in removed['nodes'] if n['id']!=self.v['id']]
        removed=self.ws.save_graph(self.pid,'EP001',removed)
        self.assertEqual(removed['groups'],[])
        result['revision']=removed['revision']
        restored=self.ws.save_graph(self.pid,'EP001',result)
        self.assertEqual(restored['groups'][0]['video_node_id'],self.v['id'])

    def setup_characters(self):
        char=dict(id='C001',name='林夏',prompt='黑色长发年轻女性，米白针织衫，中性柔光单人母版。')
        shared=director.character_graph([char])
        self.ws.mutate(self.pid,lambda d:(d['graphs'].update({SHARED:shared}),d.update(characters_initialized=True)))
        return char,shared

    def test_global_prompt_edit_is_live_in_both_episodes_and_conflicts_are_rejected(self):
        char,shared=self.setup_characters();master=shared['nodes'][1]
        for eid in ['EP001','EP002']:
            g=director.empty_graph();g['nodes']=[node('image',0,0,'林夏',shared_node_id=master['id'])];g['cast_attached']=True
            self.ws.mutate(self.pid,lambda d:d['graphs'].update({eid:g}))
        ref=self.ws.graph(self.pid,'EP001')['nodes'][0]
        self.assertEqual(ref['global_prompt'],char['prompt'])
        self.ws.edit_character_prompt(self.pid,master['id'],'黑色短发女性，藏青羊毛大衣，中性光。',ref['prompt_revision'])
        for eid in ['EP001','EP002']:
            g=self.ws.graph(self.pid,eid)
            self.assertIn('藏青羊毛大衣',g['nodes'][0]['global_prompt']);self.assertIn('cast_group',g)
        with self.assertRaisesRegex(ValueError,'其他窗口'):
            self.ws.edit_character_prompt(self.pid,master['id'],'覆盖之前的内容',ref['prompt_revision'])

    def test_shared_prompt_is_forked_when_other_characters_use_same_prompt(self):
        char,shared=self.setup_characters();p,master=shared['nodes'];other=node('image',800,290,'另一个人')
        self.ws.mutate(self.pid,lambda d:(d['graphs'][SHARED]['nodes'].append(other),d['graphs'][SHARED]['edges'].append(dict(id='shared',source=p['id'],target=other['id']))))
        g=self.ws.graph(self.pid,'EP001');ref=next(n for n in g['nodes'] if n.get('shared_node_id'))
        self.ws.edit_character_prompt(self.pid,master['id'],'林夏独立的新人物生成设定',ref['prompt_revision'])
        updated=self.ws.graph(self.pid,SHARED);byid={n['id']:n for n in updated['nodes']}
        other_prompt=next(byid[e['source']] for e in updated['edges'] if e['target']==other['id'])
        self.assertEqual(other_prompt['text'],char['prompt'])
        main_prompt=next(byid[e['source']] for e in updated['edges'] if e['target']==master['id'])
        self.assertNotEqual(main_prompt['id'],other_prompt['id'])

    def test_measured_layout_revision_save_and_manual_reframe(self):
        self.setup_characters();g=self.ws.graph(self.pid,'EP001')
        sizes={n['id']:dict(width=310,height=1100) for n in g['nodes']}
        g=self.ws.arrange(self.pid,'EP001',g['revision'],sizes)
        unchanged=self.ws.arrange(self.pid,'EP001',g['revision'],sizes)
        self.assertEqual(unchanged['revision'],g['revision'])
        g['nodes'][-1]['x']+=240;g['layout_mode']='manual'
        saved=self.ws.save_graph(self.pid,'EP001',g)
        self.assertEqual(saved['nodes'][-1]['x'],g['nodes'][-1]['x'])
        self.assertEqual(saved['layout_mode'],'manual')
        self.assertEqual(saved['cast_group'],g['cast_group'])
        with self.assertRaisesRegex(ValueError,'已更新'):
            self.ws.arrange(self.pid,'EP001',g['revision'],sizes)

    def test_replan_archives_and_restores_assets_while_allowing_background_layout(self):
        self.setup_characters();asset=self.frame(self.i1['id']);before=self.ws.graph(self.pid,'EP001')
        beats=[dict(beat_id='B0001',dialogue=[dict(source_speaker='林夏',text='你来了。')])]
        clips=[dict(title='多分镜相遇',duration=6,beat_ids=['B0001'],shots=[shot(),shot()])]
        def planning(*args):
            g=self.ws.graph(self.pid,'EP001')
            self.ws.arrange(self.pid,'EP001',g['revision'],{n['id']:dict(width=310,height=900) for n in g['nodes']})
            return clips,beats,[]
        with patch('backend.canvas.studio.director.plan_episode',side_effect=planning):
            j=self.wait(self.ws.plan(self.pid,'EP001',rebuild=True))
        self.assertEqual(j['status'],'succeeded',j.get('error'))
        planned=self.ws.graph(self.pid,'EP001')
        self.assertEqual(planned['groups'][0]['shot_count'],2)
        self.assertEqual(planned['groups'][0]['duration'],6)
        hist=self.ws.history(self.pid,'EP001');self.assertEqual(len(hist),1)
        self.ws.restore(self.pid,'EP001',hist[0]['id'])
        restored=self.ws.graph(self.pid,'EP001')
        image=next(n for n in restored['nodes'] if n['id']==self.i1['id'])
        self.assertEqual(image['selected'],asset['id']);self.assertTrue((self.ws.path(self.pid)/asset['path']).exists())
        self.assertGreater(restored['revision'],planned['revision'])
        self.assertEqual(len(self.ws.history(self.pid,'EP001')),2)

    def test_manual_layout_expands_frames_for_new_media_without_moving_nodes(self):
        self.setup_characters();g=self.ws.graph(self.pid,'EP001')
        positions=[(n['id'],n['x'],n['y']) for n in g['nodes']]
        sizes={n['id']:dict(width=310,height=1600) for n in g['nodes']}
        updated=self.ws.arrange(self.pid,'EP001',g['revision'],sizes,mode='manual')
        self.assertEqual(updated['layout_mode'],'manual')
        self.assertEqual(positions,[(n['id'],n['x'],n['y']) for n in updated['nodes']])
        group=updated['groups'][0]
        for n in updated['nodes']:
            if n.get('clip_id')==group['id']:
                self.assertLessEqual(n['y']+1600,group['y']+group['height'])

    def test_replan_does_not_overwrite_creative_changes_during_llm_call(self):
        self.setup_characters()
        def planning(*args):
            self.modify(lambda g:next(n for n in g['nodes'] if n['id']==self.p['id']).update(text='导演工作时用户改写的Prompt'))
            return [dict(title='新规划',duration=6,beat_ids=[],shots=[shot(),shot()])],[],[]
        with patch('backend.canvas.studio.director.plan_episode',side_effect=planning):
            j=self.wait(self.ws.plan(self.pid,'EP001',rebuild=True))
        self.assertEqual(j['status'],'failed');self.assertIn('未覆盖',j['error'])
        self.assertEqual(next(n for n in self.ws.graph(self.pid,'EP001')['nodes'] if n['id']==self.p['id'])['text'],'导演工作时用户改写的Prompt')
        self.assertEqual(self.ws.history(self.pid,'EP001'),[])

    def test_director_repairs_invalid_batch_and_caches_only_valid_result(self):
        answer=dict(clips=[dict(title='相遇',duration=6,beat_ids=['B0001'],shots=[shot(),shot()])])
        invalid=deepcopy(answer);invalid['clips'][0]['shots'][0]['character_ids']=['C001','C002','C003','C004']
        client=Mock();client.generate.side_effect=[json.dumps(invalid),json.dumps(answer)]
        folder=Path(self.temp.name)/'repair';folder.mkdir()
        args=('林夏：你来了。',[dict(id='C001',name='林夏')],{},folder,client)
        clips,_,_=director.plan_episode(*args)
        self.assertEqual(len(clips[0]['shots']),2);self.assertEqual(client.generate.call_count,2)
        self.assertIn('validation_error',client.generate.call_args.args[0])
        director.plan_episode(*args);self.assertEqual(client.generate.call_count,2)
        self.assertEqual(len(list((folder/'rejected').glob('*.json'))),1)

    def test_images_can_flow_through_prompt_to_any_image_card(self):
        self.frame(self.i1['id']);self.frame(self.i2['id'],'red')
        out=node('image',1800,300,'独立图片','保持夜景。',clip_id='another-video')
        self.modify(lambda g:(g['nodes'].append(out),g['nodes'][0].update(text='使用@图片2的场景与@图1的人物。')))
        self.edge(self.i1,self.p);self.edge(self.i2,self.p);self.edge(self.p,out)
        g=self.ws.graph(self.pid,'EP001');saved=self.ws.save_graph(self.pid,'EP001',g)
        c=self.ws.compile(self.pid,'EP001',out['id'])
        self.assertEqual(c['route'],'qwen-image-edit');self.assertEqual(len(c['images']),2)
        self.assertEqual([b['node_id'] for b in c['bindings']],[self.i2['id'],self.i1['id']])
        self.assertIn('保持夜景',c['prompt']);self.assertIn('图1的场景与图2的人物',c['prompt'])
        self.assertEqual(next(n for n in saved['nodes'] if n['id']==out['id'])['clip_id'],'another-video')

    def test_prompt_diamond_deduplicates_upstream_text_and_images(self):
        self.frame(self.i1['id']);a=node('prompt',0,0,'A','侧面光');b=node('prompt',0,0,'B','低机位')
        self.modify(lambda g:g['nodes'].extend([a,b]))
        self.edge(self.i1,self.p);self.edge(self.p,a);self.edge(self.p,b);self.edge(a,self.v);self.edge(b,self.v)
        c=self.ws.compile(self.pid,'EP001',self.v['id'])
        self.assertEqual(c['prompt'].count(self.p['text']),1);self.assertEqual(len(c['bindings']),1)
        self.assertIn('侧面光',c['prompt']);self.assertIn('低机位',c['prompt'])

    def test_generated_media_are_leaf_inputs_not_their_generation_instructions(self):
        self.frame(self.i1['id']);self.frame(self.i2['id'])
        self.edge(self.i1,self.p);self.edge(self.p,self.i2);self.edge(self.i2,self.v)
        self.modify(lambda g:next(n for n in g['nodes'] if n['id']==self.v['id']).update(text='让输出图片里的场景下雨。'))
        c=self.ws.compile(self.pid,'EP001',self.v['id'])
        self.assertEqual([b['node_id'] for b in c['bindings']],[self.i2['id']])
        self.assertNotIn(self.p['text'],c['prompt'])

    def test_two_video_references_through_prompt_route_to_native_wan_inputs(self):
        self.frame(self.i1['id'])
        videos=[]
        for index in range(2):
            v=node('video',0,0,f'参考视频{index+1}')
            v.update(assets=[dict(id='remote'+str(index),remote_url=f'https://cdn.example/{index}.mp4')],selected='remote'+str(index));videos.append(v)
        self.modify(lambda g:(g['nodes'].extend(videos),g['nodes'][0].update(text='视频总时长12秒；0–12秒，参考@视频2与@视频3，在@图片1的场景交谈。'),g['nodes'][3]['settings'].update(model='auto',duration=6),g['groups'][0].update(planned_duration=12)))
        for v in videos:self.edge(v,self.p)
        self.edge(self.i1,self.p);self.edge(self.p,self.v)
        c=self.ws.compile(self.pid,'EP001',self.v['id']);p=c['payload']
        self.assertEqual(c['route'],'wan-multimodal-reference');self.assertEqual(p['model'],'wan2.7-r2v-2026-06-12')
        self.assertEqual(p['parameters']['duration'],6);self.assertIn('参考视频1与视频2，在图1',p['input']['prompt'])
        self.assertIn('0–6秒',p['input']['prompt']);self.assertIn('视频总时长6秒',p['input']['prompt'])
        self.assertEqual([m['type'] for m in p['input']['media']],['reference_image','reference_video','reference_video'])
        self.assertEqual([m['url'] for m in p['input']['media'][1:]],['https://cdn.example/0.mp4','https://cdn.example/1.mp4'])
        self.modify(lambda g:g['nodes'][3]['settings'].update(model='happyhorse'))
        with self.assertRaisesRegex(ValueError,'多视频'):self.ws.compile(self.pid,'EP001',self.v['id'])

    def test_shared_character_can_be_used_through_an_independent_prompt(self):
        _,shared=self.setup_characters();asset=self.frame(self.i1['id']);master=shared['nodes'][1]
        self.ws.mutate(self.pid,lambda d:next(n for n in d['graphs'][SHARED]['nodes'] if n['id']==master['id']).update(assets=[asset],selected=asset['id']))
        g=self.ws.graph(self.pid,'EP001');ref=next(n for n in g['nodes'] if n.get('shared_node_id'))
        self.edge(ref,self.p);self.edge(self.p,self.i2)
        c=self.ws.compile(self.pid,'EP001',self.i2['id'])
        self.assertEqual(c['route'],'qwen-image-edit');self.assertTrue(c['bindings'][0]['shared'])
        self.assertEqual(c['bindings'][0]['node_id'],ref['id'])

    def test_new_free_cards_keep_positions_and_do_not_become_video_groups(self):
        self.modify(lambda g:(g['nodes'].clear(),g['edges'].clear(),g.update(groups=[])))
        g=self.ws.graph(self.pid,'EP001')
        p=node('prompt',1720,320,'自由Prompt','测试');v=node('video',2200,320,'自由视频')
        g.update(nodes=[p,v],edges=[dict(source=p['id'],target=v['id'])],layout_mode='manual')
        self.ws.save_graph(self.pid,'EP001',g)
        saved=self.ws.graph(self.pid,'EP001')
        self.assertEqual(saved['groups'],[]);self.assertNotIn('clip_id',saved['nodes'][0])
        self.assertEqual([(n['x'],n['y']) for n in saved['nodes']],[(1720,320),(2200,320)])

    def test_new_image_prompt_cycle_is_rejected_and_many_links_save_before_model_validation(self):
        g=self.ws.graph(self.pid,'EP001')
        g['edges']=[dict(source=self.i1['id'],target=self.p['id']),dict(source=self.p['id'],target=self.i1['id'])]
        with self.assertRaisesRegex(ValueError,'循环'):self.ws.save_graph(self.pid,'EP001',g)
        g['edges']=[]
        for i in range(12):
            im=node('image',i*350,300,'参考'+str(i));g['nodes'].append(im);g['edges'].append(dict(source=im['id'],target=self.p['id']))
        saved=self.ws.save_graph(self.pid,'EP001',g)
        self.assertEqual(len(saved['edges']),12)

    def test_global_prompt_edit_keeps_inherited_images_without_duplicating_parent_text(self):
        _,shared=self.setup_characters();asset=self.frame(self.i1['id']);original,master=shared['nodes']
        base=node('prompt',0,0,'上游设定','原来的上游雀斑描述')
        reference=node('image',0,0,'服装参考');reference.update(assets=[asset],selected=asset['id'])
        def setup(d):
            g=d['graphs'][SHARED];g['nodes'].extend([base,reference])
            g['edges'].extend([dict(source=reference['id'],target=base['id']),dict(source=base['id'],target=original['id'])])
        self.ws.mutate(self.pid,setup)
        ref=next(n for n in self.ws.graph(self.pid,'EP001')['nodes'] if n.get('shared_node_id'))
        self.assertIn(base['text'],ref['global_prompt'])
        self.ws.edit_character_prompt(self.pid,master['id'],'全新的完整人物设定，黑发、米色针织衫、脸颊雀斑。',ref['prompt_revision'])
        c=self.ws.compile(self.pid,SHARED,master['id'])
        self.assertEqual(c['route'],'qwen-image-edit');self.assertEqual(len(c['images']),1)
        self.assertNotIn(base['text'],c['prompt']);self.assertEqual(c['prompt'].count('全新的完整人物设定'),1)

if __name__=='__main__':unittest.main()
