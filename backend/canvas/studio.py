"""Project-wide cast, director clip hierarchy and typed media reference routing."""
from copy import deepcopy
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from backend.canvas.workspace import CanvasWorkspace, uid, node, split_script
import backend.canvas.director as director
import backend.canvas.layout as layout
from backend.canvas.sync import merge_edits
from backend.canvas.upload import VIDEO_MAX_BYTES, video_info
import backend.canvas.composer as composer
from core.store import now, find, fingerprint, safe_path
from ai.image.client import generate_image, build_payload, extract_image_urls, download_image
from ai.video import (DashScopeVideoClient, build_text_video_payload, build_image_video_payload,
                             build_reference_video_payload, build_frame_video_payload, build_video_edit_payload,
                             build_multimodal_reference_payload)

SHARED=director.CHARACTERS
ALLOWED={('prompt','prompt'),('prompt','image'),('prompt','video'),('image','prompt'),('image','image'),('image','video'),('video','prompt'),('video','video'),('video','image')}


def prompt_dependencies(graph,target_id):
    """Prompt cards carry upstream references; generated media are leaf inputs.

    Do not traverse through an image/video: its output is the input, rather than
    the instructions and references which were used to generate that output.
    """
    nodes={n['id']:n for n in graph['nodes']};incoming={nid:[] for nid in nodes}
    for edge in graph['edges']:incoming[edge['target']].append(nodes[edge['source']])
    prompts=[];media=[];seen=set();visiting={target_id}
    def walk(n):
        if n['id'] in visiting:raise ValueError('连线形成循环，请让素材流向下一个生成步骤')
        if n['id'] in seen:return
        seen.add(n['id'])
        if n['type']=='prompt':
            visiting.add(n['id'])
            for parent in incoming[n['id']]:walk(parent)
            visiting.remove(n['id']);prompts.append(n)
        else:media.append(n)
    for source in incoming[target_id]:walk(source)
    return prompts,media


def reference_key(kind,asset):
    return (kind,asset.get('legacy_path') or asset.get('path') or asset.get('remote_url') or asset.get('url') or asset['id'])


def planning_fingerprint(g):
    # Background sizing and panning may continue while the director works.
    return fingerprint({'nodes':[{k:v for k,v in n.items() if k not in {'x','y','global_prompt','prompt_revision'}} for n in g['nodes']],
                        'edges':g['edges']})


def chosen(n):
    ids=n.get('selected_ids') or ([n['selected']] if n.get('selected') else [])
    return [a for aid in ids for a in n.get('assets',[]) if a['id']==aid]


def validate_edges(nodes, edges):
    types={n['id']:n['type'] for n in nodes};seen=set();result=[]
    adjacency={key:[] for key in types}
    for e in edges:
        s,t=e.get('source'),e.get('target')
        if s not in types or t not in types or (types[s],types[t]) not in ALLOWED:
            raise ValueError('不支持的连线，请使用图片或视频卡片')
        if (s,t) in seen:continue
        seen.add((s,t));adjacency[s].append(t)
        result.append(dict(id=str(e.get('id') or uid('e_'))[:80],source=s,target=t))
    visited=set();visiting=set()
    def walk(nid):
        if nid in visiting:raise ValueError('连线形成循环，请让素材流向下一个生成步骤')
        if nid in visited:return
        visiting.add(nid)
        for target in adjacency[nid]:walk(target)
        visiting.remove(nid);visited.add(nid)
    for nid in types:walk(nid)
    return result


class StudioCanvas(CanvasWorkspace):
    def create(self,body):
        if body.get('mode')!='blank':return super().create(body)
        ratio=body.get('ratio','9:16')
        if ratio not in {'9:16','16:9','1:1'}:raise ValueError('画幅无效')
        graph=director.empty_graph();graph.update(layout_mode='manual',layout_version=layout.VERSION,node_sizes={})
        pid=uid('drama_')
        data=dict(id=pid,title=str(body.get('title') or '未命名项目')[:100],mode='blank',ratio=ratio,
                  style=str(body.get('style') or '电影质感，真实人物，自然光影，保持角色身份与场景一致，无文字水印。')[:3000],
                  source_text='',status='ready',characters_initialized=True,created_at=now(),
                  episodes=[dict(id='CANVAS001',number=1,title='自由画布',summary='创建图片或视频，点击卡片编辑Prompt与参数，自由连接参考素材。',source_text='')],
                  graphs={'CANVAS001':graph,SHARED:director.empty_graph()},jobs=[])
        with self.lock:self.save(data)
        return {'project_id':pid,'mode':'blank'}

    def save_history(self,pid,eid):
        return [r for r in reversed(self.read(pid).get('save_recovery',[])) if r['episode_id']==eid]

    def media_graph(self,pid,eid):
        with self.lock:
            g=self.graph(pid,eid)
            if g.get('composer_version')==composer.VERSION and not any(n['type']=='prompt' for n in g['nodes']):return g
            d=self.read(pid)
            if any(j.get('episode_id') in {eid,SHARED} and j['kind'] in {'plan','characters','initialize'} and j['status'] in {'running','queued'} for j in d['jobs']):
                return {**g,'composer_pending':True}
            self._archive(d,eid,d['graphs'][eid],'合并Prompt到图片/视频卡')
            converted=composer.embed_prompts(g)
            for n in converted['nodes']:
                if n.get('shared_node_id'):
                    n.update(assets=[],selected=None,selected_ids=[])
                    for key in ('global_prompt','prompt_revision','missing_master'):n.pop(key,None)
            director.aliases(converted);converted['node_sizes']={}
            converted['viewport']={'x':35,'y':25,'zoom':.8}
            layout.arrange(converted,shared=eid==SHARED)
            converted['revision']=g['revision']+1;d['graphs'][eid]=converted;self.save(d)
            return self.graph(pid,eid)

    def source(self,pid,eid):
        if eid==SHARED:
            d=self.read(pid)
            if d.get('legacy'):return '\n\n'.join(e.get('source_text','') for e in self.legacy.read()['episodes'])
            return d.get('source_text','')
        return super().source(pid,eid)

    def graph(self,pid,eid):
        with self.lock:
            if eid==SHARED:
                d=self.read(pid)
                if SHARED not in d['graphs']:
                    d['graphs'][SHARED]=director.empty_graph();self.save(d)
                g=deepcopy(d['graphs'][SHARED])
            else:
                g=super().graph(pid,eid)
            director.aliases(g)
            # Lazy, lossless migration: old one-shot videos remain intact.
            if eid!=SHARED and not g.get('groups') and not g.get('layout_version'):
                g['groups']=[]
                for i,v in enumerate(n for n in g['nodes'] if n['type']=='video'):
                    cid=v.get('clip_id') or f'{eid}-V{i+1:03}'
                    v['clip_id']=cid
                    incoming={e['source'] for e in g['edges'] if e['target']==v['id']}
                    for n in g['nodes']:
                        if n['id'] in incoming:n.setdefault('clip_id',cid)
                    g['groups'].append(dict(id=cid,title=v['title'],duration=v['settings'].get('duration',5),video_node_id=v['id'],
                        x=35,y=max(170,v['y']-50),width=1510,height=440,legacy=True,shot_image_ids=[n['id'] for n in g['nodes'] if n['id'] in incoming and n['type']=='image']))
            d=self.read(pid)
            relayout=g.get('layout_version') != layout.VERSION
            content_upgrade=False
            if eid!=SHARED and g.get('planning') and g.get('prompt_flow_version')!=director.PROMPT_FLOW_VERSION:
                active=any(j.get('episode_id')==eid and j['status'] in {'running','queued'} for j in d['jobs'])
                if not active:content_upgrade=director.bind_character_prompts(g)
            if eid!=SHARED:
                if d.get('mode')!='blank' and d.get('characters_initialized') and g['nodes'] and not g.get('cast_attached') and not g.get('planning'):
                    characters=self.characters(pid)
                    if d.get('legacy'):
                        cast=set(find(self.legacy.read()['episodes'],eid).get('characters',[]))
                    else:
                        text=self.source(pid,eid);cast={c['id'] for c in characters if c['name'] in text}
                    refs=[]
                    for c in (c for c in characters if c['id'] in cast):
                        n=node('image',60+len(refs)*360,190,c['name'],shared_node_id=c['node_id'],character_id=c['id'],role='shared_character')
                        refs.append(n)
                    if refs and g['nodes'] and min(n['y'] for n in g['nodes'])<600:
                        for n in g['nodes']:n['y']+=480
                        for group in g.get('groups',[]):group['y']+=480
                    g['nodes']=refs+g['nodes'];g['cast_attached']=True;g['revision']+=1
                    director.aliases(g);d['graphs'][eid]=deepcopy(g);self.save(d)
                    relayout=True
            if relayout:
                layout.arrange(g,shared=eid==SHARED)
            if relayout or content_upgrade:
                g['revision']+=1
                d['graphs'][eid]=deepcopy(g);self.save(d)
            if eid!=SHARED:
                shared={n['id']:n for n in d['graphs'].get(SHARED,{}).get('nodes',[])}
                shared_graph=d['graphs'].get(SHARED,{})
                for n in g['nodes']:
                    if n.get('shared_node_id'):
                        master=shared.get(n['shared_node_id'])
                        n.update(assets=deepcopy(master.get('assets',[]) if master else []),selected=master.get('selected') if master else None,
                                 selected_ids=deepcopy(master.get('selected_ids',[]) if master else []),missing_master=master is None,
                                 title=master['title'] if master else n['title'])
                        if master:
                            prompts,_=prompt_dependencies(shared_graph,master['id'])
                            n['global_prompt']='\n\n'.join(filter(None,[*[p.get('text','') for p in prompts],master.get('text','')]))
                            n['prompt_revision']=fingerprint({'prompts':[(p['id'],p.get('text','')) for p in prompts],'extra':master.get('text','')})
            return g

    def save_graph(self,pid,eid,body):
        with self.lock:
            current=self.graph(pid,eid)
            recovery=[];merged=False
            # Normalize both sides of an old browser's delta. This carries its
            # unsaved Prompt changes into the same media IDs after migration.
            if current.get('composer_version') and 'base' in body:
                body={**composer.embed_prompts(body),'base':composer.embed_prompts(body['base'])}
            if 'base' in body:
                merged=body.get('revision')!=current['revision']
                incoming,recovery=merge_edits(current,body,body['base'],bool(body.get('recover_legacy')))
                recovery=[field for field in recovery if field['field'] not in {'x','y'}]
                incoming['revision']=current['revision']
            else:incoming=body
            result=self._save_graph(pid,eid,incoming,recovery)
            result['sync']={'merged':merged,'recovered':len(recovery)}
            return result

    def _save_graph(self,pid,eid,body,recovery):
        import math
        self.graph(pid,eid)
        nodes,edges=body.get('nodes'),body.get('edges')
        if not isinstance(nodes,list) or not isinstance(edges,list) or len(nodes)>2400 or len(edges)>12000:raise ValueError('每张画布最多2400节点、12000连线')
        ids=[];clean=[]
        for n in nodes:
            nid=n.get('id')
            if not isinstance(nid,str) or not re.fullmatch(r'[\w-]{1,80}',nid) or n.get('type') not in {'prompt','image','video'}:raise ValueError('节点编号或类型无效')
            if any(type(n.get(k)) not in (float,int) or not math.isfinite(n[k]) or abs(n[k])>1000000 for k in ('x','y')):raise ValueError('节点坐标无效')
            settings=n.get('settings',{})
            if not isinstance(settings,dict):raise ValueError('节点参数无效')
            record={k:deepcopy(n[k]) for k in ('id','type','x','y','title','text','clip_id','shot_id','role','character_id','shared_node_id','description') if k in n}
            text=str(n.get('text',''))
            if len(text)>100000:raise ValueError('单张卡片的Prompt最多10万字')
            record.update(title=str(n.get('title',''))[:120],text=text,settings={k:settings[k] for k in ('ratio','duration','resolution','model','mode','n','audio_setting','use_current') if k in settings})
            clean.append(record);ids.append(nid)
        if len(set(ids))!=len(ids):raise ValueError('节点编号重复')
        clean_edges=validate_edges(clean,edges)
        v=body.get('viewport',{})
        if any(type(v.get(k)) not in (int,float) or not math.isfinite(v[k]) for k in ('x','y','zoom')) or not .15<=v['zoom']<=2:raise ValueError('画布视角无效')
        def update(d):
            old=d['graphs'][eid]
            if body.get('revision')!=old['revision']:raise ValueError('画布已在其他窗口更新，请刷新后重试')
            archives=d.setdefault('node_archive',{});live={**archives,**{n['id']:n for n in old['nodes']}}
            shared={n['id']:n for n in d['graphs'].get(SHARED,{}).get('nodes',[])}
            for n in old['nodes']:
                if n['id'] not in ids:archives[n['id']]=n
            for n in clean:
                previous=live.get(n['id'],{})
                if previous and n['type']!=previous['type']:raise ValueError('已有节点不能改变类型')
                for key in ('assets','selected','selected_ids','ref_name'):
                    if key in previous:n[key]=deepcopy(previous[key])
                n.setdefault('assets',[]);n.setdefault('selected',None)
                if n.get('shared_node_id'):
                    if eid==SHARED or n['shared_node_id'] not in shared:raise ValueError('共享人物引用无效')
                    n.update(assets=[],selected=None,selected_ids=[])
                if eid==SHARED and n['type']=='image':n.setdefault('character_id',n['id']);n['role']='character'
            busy={j.get('node_id') for j in d['jobs'] if j.get('episode_id')==eid and j.get('node_id') and j['status'] in {'running','queued'}}
            if busy-set(ids):raise ValueError('生成中的节点暂时不能删除')
            groups=deepcopy(old.get('groups',[]))
            group_archive=d.setdefault('group_archive',{})
            wanted={n.get('clip_id') for n in clean}
            for group in groups:
                if group['id'] not in wanted:group_archive[group['id']]=deepcopy(group)
            groups=[group for group in groups if group['id'] in wanted]
            existing={g['id'] for g in groups}
            for n in clean:
                if n['type']=='video' and n.get('clip_id') and n['clip_id'] not in existing:
                    cid=n['clip_id'];existing.add(cid)
                    if cid in group_archive:
                        groups.append(deepcopy(group_archive[cid]));continue
                    related=[p for p in clean if p.get('clip_id')==cid]
                    groups.append(dict(id=cid,title=n['title'],duration=n['settings'].get('duration',5),video_node_id=n['id'],
                        x=min(p['x'] for p in related)-25,y=min(p['y'] for p in related)-75,width=1585,height=750,manual=True,shot_image_ids=[]))
            # Duration is an editable video-node parameter, reflected in clip management.
            for group in groups:
                video=next((n for n in clean if n['id']==group.get('video_node_id')),None)
                if video:
                    group['duration']=video['settings'].get('duration',group['duration'])
                    group['title']=video['title']
            g=dict(nodes=clean,edges=clean_edges,groups=groups,viewport=v,revision=old['revision']+1)
            if old.get('planning'):g['planning']=old['planning']
            if old.get('cast_attached'):g['cast_attached']=True
            for key in ('layout_version','layout_mode','cast_group','node_sizes','prompt_flow_version','composer_version'):
                if key in old:g[key]=deepcopy(old[key])
            if body.get('layout_mode') in {'manual','tidy'}:g['layout_mode']=body['layout_mode']
            if old.get('composer_version') or body.get('composer_version'):
                g=composer.embed_prompts(g)
            if g.get('layout_mode')=='tidy':layout.arrange(g,shared=eid==SHARED)
            else:layout.reframe(g,shared=eid==SHARED)
            director.aliases(g);d['graphs'][eid]=g
            if recovery:
                titles={n['id']:n['title'] for n in old['nodes']}
                d.setdefault('save_recovery',[]).append(dict(id=uid('save_'),episode_id=eid,created_at=now(),
                    fields=[{**field,'title':titles.get(field['node_id'],'已删除卡片')} for field in recovery]))
                d['save_recovery']=d['save_recovery'][-60:]
            return deepcopy(g)
        self.mutate(pid,update)
        return self.graph(pid,eid)

    def characters(self,pid):
        d=self.read(pid);g=d['graphs'].get(SHARED,{})
        result=[]
        for n in g.get('nodes',[]):
            if n['type']!='image' or not n.get('character_id'):continue
            prompts,_=prompt_dependencies(g,n['id'])
            result.append(dict(id=n['character_id'],name=n['title'],node_id=n['id'],description=n.get('description',''),prompt='\n'.join(filter(None,[*[p.get('text','') for p in prompts],n.get('text','')]))))
        return result

    def initialize(self,pid):
        with self.lock:
            d=self.read(pid)
            active=next((j for j in d['jobs'] if j['kind']=='initialize' and j['status'] in {'running','queued'}),None)
            if active:return {'job_id':active['id']}
            if d['episodes']:raise ValueError('项目已初始化')
            def work(jid):
                episodes,method=split_script(self.read(pid)['source_text'])
                self.mutate(pid,lambda d:d.update(episodes=episodes,split_method=method,status='ready',error=None))
                self.prepare(pid,auto_plan=True)
            self.mutate(pid,lambda d:d.update(status='initializing',error=None))
            return self.submit_job(pid,'initialize',work)

    def prepare(self,pid,auto_plan=False):
        with self.lock:
            d=self.read(pid)
            if d.get('characters_initialized'):return {'ok':True}
            active=next((j for j in d['jobs'] if j['kind']=='characters' and j['status'] in {'running','queued'}),None)
            if active:return {'job_id':active['id']}
            original=d['graphs'].get(SHARED,director.empty_graph())
            if original['nodes']:raise ValueError('已有人物画布，请直接编辑人物，避免覆盖')
            def work(jid):
                folder=self.path(pid)/'planning/characters';folder.mkdir(parents=True,exist_ok=True)
                if d.get('legacy'):
                    source=self.legacy.read();characters=[]
                    for c in source.get('characters',[]):
                        asset=next((a for a in c.get('assets',[]) if a.get('id')==c.get('selected',{}).get('base') and a.get('status')=='ready'),None)
                        images=[dict(id=asset['id'],legacy_path=asset['path'],url=asset.get('url') or '/media/'+asset['path'],kind='image',created_at=asset.get('created_at',now()))] if asset else []
                        characters.append(dict(id=c['id'],name=c['name'],prompt=c.get('base_prompt') or c['name']+'，单人人物母版，中性光线，清晰面部。',description=c.get('role',''),assets=images,selected=asset['id'] if asset else None))
                    bible={'style':d.get('style','')}
                else:
                    episodes=[{**e,'source_text':self.source(pid,e['id'])} for e in d['episodes']]
                    characters,bible=director.plan_characters(episodes,folder)
                graph=director.character_graph(characters,d.get('ratio','9:16'))
                def adopt(current):
                    if current['graphs'].get(SHARED,{}).get('nodes'):raise ValueError('初始化期间人物画布已编辑，未覆盖')
                    current['graphs'][SHARED]=graph;current.update(characters_initialized=True,visual_bible=bible,character_catalog=characters)
                self.mutate(pid,adopt)
                if auto_plan and d['episodes']:
                    eid=d['episodes'][0]['id']
                    if not any(not n.get('shared_node_id') for n in self.graph(pid,eid)['nodes']):self.plan(pid,eid)
            return self.submit_job(pid,'characters',work,SHARED)

    def plan(self,pid,eid,rebuild=False):
        if eid==SHARED:return self.prepare(pid)
        with self.lock:
            g=self.graph(pid,eid);d=self.read(pid)
            active=next((j for j in d['jobs'] if j['kind']=='plan' and j['episode_id']==eid and j['status'] in {'running','queued'}),None)
            if active:return {'job_id':active['id']}
            if any(not n.get('shared_node_id') for n in g['nodes']) and not rebuild:raise ValueError('本集已有画布，未覆盖；可使用“重排多分镜视频”保留旧版后重排')
            if any(j.get('episode_id')==eid and j['kind'] in {'image','video'} and j['status'] in {'running','queued'} for j in d['jobs']):raise ValueError('本集有生成任务，请等待任务保存后重排')
            if not d.get('characters_initialized'):raise ValueError('请先初始化共享人物库，再初始化本集导演分镜')
            original=planning_fingerprint(d['graphs'][eid])
            def work(jid):
                chars=self.characters(pid);folder=self.path(pid)/'planning'/eid;folder.mkdir(parents=True,exist_ok=True)
                clips,beats,_=director.plan_episode(self.source(pid,eid),chars,d.get('visual_bible',{}),folder)
                graph=director.cinematic_graph(eid,clips,beats,chars,d['graphs'][SHARED],d.get('ratio','9:16'),g['revision']+1)
                def adopt(current):
                    live=current['graphs'][eid]
                    if planning_fingerprint(live)!=original:raise ValueError('初始化期间画布已编辑，未覆盖；成功批次已缓存')
                    if rebuild:self._archive(current,eid,live,'重排多分镜视频')
                    layout.arrange(graph)
                    graph['revision']=live['revision']+1
                    current['graphs'][eid]=graph
                self.mutate(pid,adopt)
            return self.submit_job(pid,'plan',work,eid)

    def _archive(self,d,eid,g,reason):
        from core.store import write_json
        aid=uid('plan_');relative=f'planning/{eid}/history/{aid}.json'
        write_json(self.path(d['id'])/relative,g)
        item=dict(id=aid,path=relative,created_at=now(),reason=reason,node_count=len(g['nodes']),clip_count=len(g.get('groups',[])))
        d.setdefault('graph_history',{}).setdefault(eid,[]).append(item)
        return item

    def history(self,pid,eid):
        return [{k:v for k,v in item.items() if k!='path'} for item in self.read(pid).get('graph_history',{}).get(eid,[])]

    def restore(self,pid,eid,aid):
        def work(d):
            if any(j.get('episode_id')==eid and j['status'] in {'running','queued'} for j in d['jobs']):raise ValueError('请先等待本集任务完成')
            item=find(d.get('graph_history',{}).get(eid,[]),aid)
            restored=json.loads(safe_path(self.path(pid),item['path']).read_text())
            current=d['graphs'][eid];self._archive(d,eid,current,'恢复前快照')
            restored['revision']=current['revision']+1;layout.arrange(restored);d['graphs'][eid]=restored
            return {'ok':True}
        return self.mutate(pid,work)

    def arrange(self,pid,eid,revision,sizes=None,mode='tidy'):
        import math
        if mode not in {'tidy','manual'}:raise ValueError('布局模式无效')
        if sizes is not None and (not isinstance(sizes,dict) or len(sizes)>2400):raise ValueError('节点尺寸无效')
        clean={}
        for nid,size in (sizes or {}).items():
            if not isinstance(size,dict) or any(type(size.get(k)) not in (float,int) or not math.isfinite(size[k]) or not 0<size[k]<=2200 for k in ('width','height')):raise ValueError('节点尺寸无效')
            clean[nid]=size
        def work(d):
            g=d['graphs'][eid]
            if g['revision']!=revision:raise ValueError('画布已更新，请重试整理')
            before=fingerprint(g)
            if mode=='manual':
                g['node_sizes']=clean or g.get('node_sizes',{});g['layout_mode']='manual'
                layout.reframe(g,shared=eid==SHARED)
            else:layout.arrange(g,shared=eid==SHARED,sizes=clean or g.get('node_sizes'))
            if fingerprint(g)!=before:g['revision']+=1
        self.mutate(pid,work)
        return self.graph(pid,eid)

    def edit_character_prompt(self,pid,master_id,text,revision):
        if not isinstance(text,str) or not text.strip() or len(text)>18000:raise ValueError('人物Prompt需要1~18000字')
        def work(d):
            g=d['graphs'][SHARED];master=find(g['nodes'],master_id);nodes={n['id']:n for n in g['nodes']}
            prompts,media=prompt_dependencies(g,master_id)
            direct=[nodes[e['source']] for e in g['edges'] if e['target']==master_id and nodes[e['source']]['type']=='prompt']
            current=fingerprint({'prompts':[(p['id'],p.get('text','')) for p in prompts],'extra':master.get('text','')})
            if current!=revision:raise ValueError('全局人物Prompt已被其他窗口修改，请重新打开')
            primary=direct[0] if len(direct)==1 and sum(e['source']==direct[0]['id'] for e in g['edges'])==1 and not any(e['target']==direct[0]['id'] for e in g['edges']) else None
            g['edges']=[e for e in g['edges'] if not(e['target']==master_id and nodes[e['source']]['type']=='prompt')]
            if primary is None:
                primary=node('prompt',master['x']-370,master['y'],master['title']+' · 人物设定');g['nodes'].append(primary)
                direct_media={e['source'] for e in g['edges'] if e['target']==master_id}
                for source in media:
                    if source['id'] not in direct_media:g['edges'].append(dict(id=uid('e_'),source=source['id'],target=primary['id']))
            primary['text']=text.strip();master['text']=''
            g['edges'].append(dict(id=uid('e_'),source=primary['id'],target=master_id));g['revision']+=1
            director.aliases(g)
            return {'ok':True}
        return self.mutate(pid,work)

    def compile(self,pid,eid,nid):
        graph=self.graph(pid,eid);target=find(graph['nodes'],nid)
        if target.get('shared_node_id'):raise ValueError('共享人物请在全局人物画布中编辑生成')
        nodes={n['id']:n for n in graph['nodes']}
        use_current=target['type']=='image' and target.get('settings',{}).get('use_current',False)
        prompts,media=prompt_dependencies(graph,nid)
        text='\n\n'.join([s.get('text','').strip() for s in prompts]+[target.get('text','').strip()]).strip()
        if not text:raise ValueError('请填写 Prompt，或连接提示词节点')
        if use_current:media.insert(0,target)
        # @ references resolve only within this project's graph and current selected versions.
        names={n['ref_name']:n for n in graph['nodes'] if n['type'] in {'image','video'}}
        names.update({'图片'+n['ref_name'][1:]:n for n in graph['nodes'] if n['type']=='image' and re.fullmatch(r'图\d+',n['ref_name'])})
        names.update({n['title']:n for n in graph['nodes'] if n['type'] in {'image','video'} and n['title']})
        found=[]
        pattern=re.compile('@('+ '|'.join(re.escape(k) for k in sorted(names,key=len,reverse=True)) +')') if names else None
        if pattern:
            for match in pattern.finditer(text):
                n=names[match[1]]
                if n['id']==nid and not use_current:raise ValueError('不能引用正在生成的节点自身；编辑当前图片请勾选“以当前采用图编辑”')
                if n not in found:found.append(n)
        for token in re.findall(r'@(?:图片|图|视频)\d+',text):
            if token[1:] not in names:raise ValueError(f'找不到素材引用 {token}')
        media=found+[n for n in media if n not in found]
        settings=target.get('settings',{});mode=settings.get('mode','auto');model=settings.get('model','happyhorse')
        if model not in {'auto','happyhorse','wan','wan_ref'}:raise ValueError('未知生成模型')
        if target['type']=='image' and any(source['type']=='video' for source in media):
            raise ValueError('Prompt接入了视频参考，只能生成视频；移除视频引用后才能生成图片')
        video_count=len({reference_key('video',a) for s in media if s['type']=='video' for a in chosen(s)})
        local_video=any(a.get('uploaded') and a.get('path') for s in media if s['type']=='video' for a in chosen(s))
        wan_ref=target['type']=='video' and (model=='wan_ref' or model=='auto' and video_count and (local_video or video_count>1 or mode=='reference'))
        if local_video and not wan_ref:raise ValueError('本地视频参考请使用自动选择模型或Wan 2.7多视频参考')
        local_videos={}
        images=[];videos=[];mapping={};bindings=[];dedup={}
        for source in media:
            assets=chosen(source)
            if not assets:raise ValueError(f"{source['title']} 尚未生成或未选择参考素材")
            labels=[]
            for asset in assets:
                kind=source['type'];key=reference_key(kind,asset)
                if key in dedup:labels.append(dedup[key]);continue
                if kind=='image':
                    images.append(str(self.asset_path(pid,asset)));label=f'图{len(images)}' if wan_ref else f'[Image {len(images)}]'
                else:
                    if asset.get('uploaded') and asset.get('path'):
                        remote=f"oss://dashscope-instant/local/{asset['id']}.mp4"
                        local_videos[remote]={'asset_id':asset['id'],'path':str(self.asset_path(pid,asset)),'node_id':source['id']}
                    else:remote=asset.get('remote_url') or self.remote_video(pid,asset)
                    if not remote:raise ValueError('该历史视频缺少公网源地址，请为视频节点添加可访问的视频URL；不会把localhost提交给云端')
                    parsed=urlsplit(remote)
                    if remote not in local_videos and (parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.hostname in {'localhost','127.0.0.1','::1'}):raise ValueError('视频编辑需要公网可访问的视频URL')
                    videos.append(remote);label=f'视频{len(videos)}' if wan_ref else '输入视频'
                dedup[key]=label;labels.append(label)
                bindings.append(dict(node_id=source['id'],asset_id=asset['id'],alias=source['ref_name'],kind=kind,label=label,shared=bool(source.get('shared_node_id'))))
            mapping[source['ref_name']]='、'.join(labels);mapping[source['title']]='、'.join(labels)
            for name,n in names.items():
                if n['id']==source['id']:mapping[name]='、'.join(labels)
        if pattern:text=pattern.sub(lambda m:mapping[m[1]],text)
        ratio=settings.get('ratio','9:16');duration=settings.get('duration',5);resolution=settings.get('resolution','720P')
        group=next((g for g in graph.get('groups',[]) if g.get('video_node_id')==nid),None)
        timing=re.search(r'视频总时长(\d+(?:\.\d+)?)秒',text)
        base=float(timing[1]) if timing else group.get('planned_duration') if group else None
        if group and base and base!=duration and (not videos or wan_ref):
            scale=duration/base
            text=re.sub(r'(\d+(?:\.\d+)?)–(\d+(?:\.\d+)?)秒',lambda m:f'{round(float(m[1])*scale,2):g}–{round(float(m[2])*scale,2):g}秒',text)
            text=re.sub(r'视频总时长\d+秒',f'视频总时长{duration}秒',text)
        style=self.read(pid).get('style','')
        prompt='\n\n'.join(filter(None,[style,text]))
        identity_contracts=[]
        for binding in bindings:
            source=nodes[binding['node_id']]
            if binding['kind']=='image' and (source.get('shared_node_id') or source.get('role')=='character'):
                identity_contracts.append(f"{binding['label']}是人物“{source['title']}”的身份母版。默认继承五官、年龄、发型、体态与指定服装；按当前分镜改变表情、动作、光线和空间，不照搬母版的背景、站姿、文字或拼图。用户明确要求改变的项目以当前Prompt为准。")
        if identity_contracts:prompt='\n'.join(identity_contracts)+'\n\n'+prompt
        payload=None;route='';options={}
        if target['type']=='image':
            if videos:raise ValueError('Qwen图片节点不接受视频，请先提供图片参考')
            # Qwen indexes images in content order; translate labels to its natural language reference convention.
            prompt=re.sub(r'\[Image (\d+)\]',r'图\1',prompt)
            n=settings.get('n',1)
            size={'9:16':'720*1280','16:9':'1280*720','1:1':'1024*1024'}.get(ratio)
            if not size:raise ValueError('不支持的画幅')
            options=dict(images=images,size=size,n=n,prompt_extend=False)
            payload=build_payload(prompt,**options);route='qwen-image-edit' if images else 'qwen-text-to-image'
        elif target['type']=='video':
            common=dict(resolution=resolution,duration=duration)
            if wan_ref:
                if mode not in {'auto','reference'}:raise ValueError('Wan多视频参考请使用自动或多图参考模式')
                payload=build_multimodal_reference_payload(prompt,images,videos,ratio=ratio,**common);route='wan-multimodal-reference'
            elif videos:
                if len(videos)!=1:raise ValueError('HappyHorse编辑只接受1个源视频；多视频请将模型选为自动选择或Wan多视频参考')
                if model not in {'happyhorse','auto'} or mode not in {'auto','edit'}:raise ValueError('视频引用请使用自动选择或Wan多视频参考；单视频编辑用HappyHorse')
                payload=build_video_edit_payload(prompt,videos[0],reference_images=images,resolution=resolution,audio_setting=settings.get('audio_setting','auto'));route='happyhorse-video-edit'
            elif mode=='edit':raise ValueError('视频编辑模式需要连接一个源视频')
            elif model=='wan':
                if len(images)>2:raise ValueError('Wan首尾帧模式最多2张图片；多分镜参考请选HappyHorse')
                if images:payload=build_frame_video_payload(prompt,images[0],images[1] if len(images)==2 else None,ratio=ratio,**common);route='wan-frames'
                else:payload=build_text_video_payload(prompt,model='wan3.0-video',ratio=ratio,**common);route='wan-text-to-video'
            elif mode=='frames':raise ValueError('首尾帧请明确选择Wan3模型')
            elif images and (mode=='reference' or len(images)>1):
                if mode=='first_frame' and len(images)>1:raise ValueError('首帧模式只接受1张图片；多图请用参考模式')
                payload=build_reference_video_payload(prompt,images,ratio=ratio,**common);route='happyhorse-reference-to-video'
            elif images:
                payload=build_image_video_payload(prompt,images[0],**common);route='happyhorse-image-to-video'
            else:
                if mode in {'reference','first_frame'}:raise ValueError('当前模式需要连接已生成的图片')
                payload=build_text_video_payload(prompt,ratio=ratio,**common);route='happyhorse-text-to-video'
        else:raise ValueError('请选择图片或视频节点生成')
        return dict(target=target,prompt=prompt,images=images,videos=videos,bindings=bindings,route=route,payload=payload,image_options=options,local_videos=local_videos)

    def remote_video(self,pid,asset):
        """Recover provider URLs from old task records, without new submissions."""
        if asset.get('path'):
            d=self.read(pid)
            return next((j.get('remote_url') for j in reversed(d['jobs']) if j.get('asset_id')==asset['id']),None)
        if asset.get('legacy_path') and self.legacy:
            folder=safe_path(self.legacy.root,asset['legacy_path']).parent
            for name in ('response.json','task-result.json','result.json'):
                p=folder/name
                if p.exists():
                    data=json.loads(p.read_text());url=data.get('output',{}).get('video_url')
                    if url:return url
        return None

    def preview(self,pid,eid,nid):
        c=self.compile(pid,eid,nid)
        return dict(route=c['route'],model=c['payload']['model'],prompt=c['prompt'],parameters=c['payload'].get('parameters',{}),references=c['bindings'])

    def generate(self,pid,eid,nid,force=False):
        with self.lock:
            c=self.compile(pid,eid,nid);target=c['target']
            token=fingerprint(dict(prompt=c['prompt'],bindings=c['bindings'],payload={k:v for k,v in c['payload'].items() if k!='input'},settings=target['settings']))
            jobs=[j for j in self.read(pid)['jobs'] if j.get('episode_id')==eid and j.get('node_id')==nid]
            active=next((j for j in reversed(jobs) if j['status'] in {'queued','running'}),None)
            if active:return {'job_id':active['id']}
            previous=next((j for j in reversed(jobs) if j.get('fingerprint')==token),None)
            resume=previous and not force and previous['status']!='succeeded' and not previous.get('terminal') and (previous.get('task_id') or previous.get('image_urls'))
            if previous and not force and not resume:raise ValueError('已有调用记录；请显式重新生成新版本，同步中断请求请先核实控制台')
            def work(jid):
                folder=self.path(pid)/'assets';folder.mkdir(exist_ok=True);assets=[]
                if target['type']=='image':
                    urls=previous['image_urls'] if resume else None
                    if not urls:
                        response=generate_image(c['prompt'],**c['image_options']);urls=extract_image_urls(response)
                        self.update_job(pid,jid,image_urls=urls)
                    for url in urls:
                        aid=uid('a_');relative=f'assets/{aid}.png';download_image(url,self.path(pid)/relative)
                        assets.append(dict(id=aid,path=relative,url=f'/canvas-media/{pid}/{relative}',kind='image',created_at=now(),fingerprint=token))
                else:
                    client=DashScopeVideoClient();taskid=previous.get('task_id') if resume else None
                    if not taskid:
                        payload=deepcopy(c['payload'])
                        if c.get('local_videos'):
                            self.update_job(pid,jid,remote_status='UPLOADING')
                            from time import time
                            for item in payload['input']['media']:
                                local=c['local_videos'].get(item['url'])
                                if not local:continue
                                source=find(self.read(pid)['graphs'][eid]['nodes'],local['node_id'])
                                asset=find(source['assets'],local['asset_id']);cached=asset.get('provider_uploads',{}).get(payload['model'],{})
                                if cached.get('expires_at',0)>time()+600:url=cached['url']
                                else:
                                    url=client.upload_file(local['path'],payload['model'])
                                    def cache(d):
                                        record=find(find(d['graphs'][eid]['nodes'],local['node_id'])['assets'],local['asset_id'])
                                        record.setdefault('provider_uploads',{})[payload['model']]={'url':url,'expires_at':time()+47*3600}
                                    self.mutate(pid,cache)
                                item['url']=url
                        taskid=client.submit(payload)['output']['task_id'];self.update_job(pid,jid,task_id=taskid)
                    def progress(response):
                        output=response.get('output',{});self.update_job(pid,jid,remote_status=output.get('task_status'),terminal=output.get('task_status') in {'FAILED','CANCELED','UNKNOWN'})
                    response=client.wait_for_task(taskid,timeout=1800,poll_interval=8,on_progress=progress)
                    remote=response['output']['video_url'];self.update_job(pid,jid,remote_url=remote)
                    aid=uid('a_');relative=f'assets/{aid}.mp4';client.download_video(remote,self.path(pid)/relative)
                    assets=[dict(id=aid,path=relative,url=f'/canvas-media/{pid}/{relative}',remote_url=remote,kind='video',created_at=now(),fingerprint=token,task_id=taskid)]
                def adopt(d):
                    n=find(d['graphs'][eid]['nodes'],nid);n.setdefault('assets',[]).extend(assets);n.update(selected=assets[0]['id'],selected_ids=[a['id'] for a in assets])
                self.mutate(pid,adopt)
                self.update_job(pid,jid,asset_ids=[a['id'] for a in assets])
            fields={k:previous[k] for k in ('task_id','image_urls','remote_url') if resume and k in previous} if previous else {}
            return self.submit_job(pid,target['type'],work,eid,nid,fingerprint=token,route=c['route'],bindings=c['bindings'],**fields)

    def upload(self,pid,eid,nid,body):
        n=find(self.graph(pid,eid)['nodes'],nid)
        if n.get('shared_node_id'):raise ValueError('共享人物请在全局画布上传')
        result=super().upload(pid,eid,nid,body)
        self.mutate(pid,lambda d:find(d['graphs'][eid]['nodes'],nid).update(selected_ids=[result['id']]))
        return result

    def upload_media(self,pid,eid,nid,source,length,filename):
        import base64
        extension=Path(filename).suffix.lower()
        n=find(self.graph(pid,eid)['nodes'],nid)
        if n.get('shared_node_id'):raise ValueError('共享人物请在全局画布上传')
        video=extension in {'.mp4','.mov'}
        limit=VIDEO_MAX_BYTES if video else 10*1024*1024
        if not 0<length<=limit:raise ValueError('视频最大100MB，图片最大10MB')
        if video and n['type']!='video' or not video and n['type']!='image':raise ValueError('请将文件上传到对应的图片卡或视频卡')
        if not video:
            raw=source.read(length)
            if len(raw)!=length:raise ValueError('上传文件不完整，请重试')
            return self.upload(pid,eid,nid,{'data':base64.b64encode(raw).decode()})
        aid=uid('a_');relative=f'assets/{aid}{extension}';path=self.path(pid)/relative;path.parent.mkdir(exist_ok=True)
        part=path.with_suffix(extension+'.part')
        try:
            remaining=length
            with part.open('wb') as dest:
                while remaining:
                    data=source.read(min(1024*1024,remaining))
                    if not data:raise ValueError('上传文件不完整，请重试')
                    dest.write(data);remaining-=len(data)
            info=video_info(part)
            part.replace(path)
            asset=dict(id=aid,path=relative,url=f'/canvas-media/{pid}/{relative}',kind='video',uploaded=True,created_at=now(),**info)
            def adopt(d):
                target=find(d['graphs'][eid]['nodes'],nid)
                target['assets'].append(asset);target.update(selected=aid,selected_ids=[aid])
            self.mutate(pid,adopt)
            return asset
        except Exception:
            path.unlink(missing_ok=True);raise
        finally:part.unlink(missing_ok=True)

    def select(self,pid,eid,nid,aid):
        def adopt(d):
            n=find(d['graphs'][eid]['nodes'],nid)
            if n.get('shared_node_id'):raise ValueError('请在全局人物画布切换采用版本')
            ids=aid if isinstance(aid,list) else [aid]
            if not ids:raise ValueError('至少选择一个素材')
            for item in ids:find(n['assets'],item)
            n.update(selected=ids[0],selected_ids=list(dict.fromkeys(ids)))
            return {'ok':True}
        return self.mutate(pid,adopt)

    def reference_video(self,pid,eid,nid,url):
        from ai.video.media import http_url
        url=http_url(url,'视频URL');parsed=urlsplit(url)
        if parsed.hostname in {'localhost','127.0.0.1','::1'}:raise ValueError('需要公网视频URL')
        def adopt(d):
            n=find(d['graphs'][eid]['nodes'],nid)
            if n['type']!='video':raise ValueError('请选择视频节点')
            aid=uid('a_');a=dict(id=aid,url=url,remote_url=url,kind='video',created_at=now(),external=True)
            n['assets'].append(a);n.update(selected=aid,selected_ids=[aid]);return a
        return self.mutate(pid,adopt)
