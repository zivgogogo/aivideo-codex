from copy import deepcopy
import unittest

from backend.canvas.workspace import node
import backend.canvas.director as director
import backend.canvas.layout as layout
from tests.canvas.test_canvas_studio import shot


class LayoutTests(unittest.TestCase):
    def graph(self):
        chars=[dict(id=f'C{i:03}',name=f'人物{i}',prompt=f'人物{i}的具体生成设定') for i in range(1,9)]
        shared=director.character_graph(chars)
        clips=[dict(title=f'视频{i}',duration=9,beat_ids=[],shots=[shot(),shot(),shot()]) for i in range(2)]
        g=director.cinematic_graph('EP001',clips,[],chars,shared,'9:16')
        # Also exercise eight actual episode cast references.
        g['nodes']=[n for n in g['nodes'] if not n.get('shared_node_id')]
        g['nodes'] += [node('image',0,0,c['name'],shared_node_id=c['node_id']) for c in chars]
        return layout.arrange(g)

    def assert_inside(self,g):
        sizes=g.get('node_sizes',{})
        for group in [g['cast_group'],*g['groups']]:
            members=[n for n in g['nodes'] if n.get('shared_node_id')] if group['kind']=='cast' else [n for n in g['nodes'] if n.get('clip_id')==group['id']]
            for n in members:
                w,h=layout.node_size(n,sizes)
                self.assertGreaterEqual(n['x'],group['x']+layout.PADDING)
                self.assertGreaterEqual(n['y'],group['y']+layout.HEADER)
                self.assertLessEqual(n['x']+w,group['x']+group['width']-layout.PADDING)
                self.assertLessEqual(n['y']+h,group['y']+group['height']-layout.PADDING)

    def test_cast_wraps_and_every_shot_is_in_its_video_frame(self):
        g=self.graph()
        for group in g['groups']:group['kind']='video'
        self.assert_inside(g)
        refs=[n for n in g['nodes'] if n.get('shared_node_id')]
        self.assertEqual(len({n['y'] for n in refs}),2)
        self.assertEqual(len({n['x'] for n in refs}),4)
        self.assertEqual([g['shot_count'] for g in g['groups']],[3,3])
        self.assertEqual([len(g['shot_rows']) for g in g['groups']],[3,3])
        boxes=[g['cast_group'],*g['groups']]
        self.assertTrue(all(b['y']>=a['y']+a['height']+99 for a,b in zip(boxes,boxes[1:])))

    def test_browser_heights_drive_rows_and_frame_bounds(self):
        g=self.graph();original=deepcopy(g)
        sizes={n['id']:dict(width=310,height=1350 if n['type']=='image' else 780) for n in g['nodes']}
        layout.arrange(g,sizes=sizes)
        for group in g['groups']:group['kind']='video'
        self.assert_inside(g)
        self.assertGreater(g['groups'][1]['y'],original['groups'][1]['y'])
        for group in g['groups']:
            rows=group['shot_rows']
            self.assertTrue(all(b['y']>=a['y']+a['height'] for a,b in zip(rows,rows[1:])))
        before=deepcopy(g);layout.arrange(g,sizes=sizes)
        self.assertEqual(g,before)

    def test_global_cast_pairs_do_not_overlap_with_tall_or_extra_prompts(self):
        chars=[dict(id=f'C{i}',name=f'人{i}',prompt='明确人物生成设定') for i in range(3)]
        g=director.character_graph(chars)
        extra=node('prompt',0,0,'补充设定','额外服装设定');g['nodes'].append(extra)
        g['edges'].append(dict(source=extra['id'],target=chars[0]['node_id']))
        sizes={n['id']:dict(width=310,height=1100) for n in g['nodes']}
        layout.arrange(g,shared=True,sizes=sizes)
        for i,a in enumerate(g['nodes']):
            for b in g['nodes'][i+1:]:
                self.assertTrue(a['x']+310<=b['x'] or b['x']+310<=a['x'] or a['y']+1100<=b['y'] or b['y']+1100<=a['y'])
        orphan=node('prompt',0,0,'自由设定');g['nodes'].append(orphan)
        layout.arrange(g,shared=True,sizes=sizes)
        self.assertGreater(orphan['y'],g['cast_group']['y']+g['cast_group']['height'])

    def test_manual_drag_updates_frame_without_moving_any_node(self):
        g=self.graph();n=next(n for n in g['nodes'] if n.get('clip_id'))
        n['x']=-350;n['y']-=70
        before=[(n['id'],n['x'],n['y']) for n in g['nodes']]
        layout.reframe(g)
        self.assertEqual(before,[(n['id'],n['x'],n['y']) for n in g['nodes']])
        for group in g['groups']:group['kind']='video'
        self.assert_inside(g)

    def test_manual_video_without_shots_does_not_invent_one(self):
        g=director.empty_graph();p=node('prompt',0,0,'导演',clip_id='v1');v=node('video',0,0,'视频',clip_id='v1')
        g['nodes']=[p,v];g['groups']=[dict(id='v1',video_node_id=v['id'])]
        layout.arrange(g)
        self.assertEqual(g['groups'][0]['shot_count'],0)
        self.assertEqual(p['y'],v['y']);self.assertGreater(v['x'],p['x']+310)

    def test_adding_a_shot_keeps_existing_video_prompt_in_director_column(self):
        g=director.empty_graph();p=node('prompt',0,0,'导演',clip_id='v1');v=node('video',0,0,'视频',clip_id='v1')
        sp=node('prompt',0,0,'分镜',clip_id='v1',shot_id='s1',role='shot_prompt');im=node('image',0,0,'图',clip_id='v1',shot_id='s1')
        g['nodes']=[p,v,sp,im];g['groups']=[dict(id='v1',video_node_id=v['id'])]
        g['edges']=[dict(source=p['id'],target=v['id']),dict(source=sp['id'],target=im['id']),dict(source=im['id'],target=v['id'])]
        layout.arrange(g)
        self.assertEqual(g['groups'][0]['shot_count'],1)
        self.assertEqual(len(g['groups'][0]['shot_rows']),1)
        self.assertGreater(p['x'],im['x']);self.assertGreater(v['x'],p['x'])
        self.assertEqual(g['groups'][0]['prompt_node_id'],p['id'])

    def test_free_cards_tidy_by_dependency_without_creating_frames(self):
        g=director.empty_graph();a=node('image',0,0,'A');b=node('image',0,0,'B');p=node('prompt',0,0,'P');out=node('video',0,0,'V')
        g['nodes']=[a,b,p,out];g['edges']=[dict(source=a['id'],target=p['id']),dict(source=b['id'],target=p['id']),dict(source=p['id'],target=out['id'])]
        layout.arrange(g)
        self.assertEqual(g['groups'],[]);self.assertEqual(a['x'],b['x'])
        self.assertGreater(b['y'],a['y']+800);self.assertGreater(p['x'],a['x']+310);self.assertGreater(out['x'],p['x']+310)


if __name__=='__main__':unittest.main()
