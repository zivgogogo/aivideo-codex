"""One layout contract for saved canvases and measured browser node sizes."""
VERSION=4
WIDTH=310
PADDING=28
GAP=60
HEADER=100


def node_size(n,sizes):
    fallback=520 if n.get('shared_node_id') else 540 if n['type']=='prompt' else 800
    value=sizes.get(n['id'],{})
    h=value.get('height',fallback)
    w=value.get('width',WIDTH)
    return (max(WIDTH,min(1200,w)),max(120,min(2200,h)))


def bounds(group,nodes,sizes):
    if not nodes:return
    left=min(n['x'] for n in nodes)-PADDING
    top=min(n['y'] for n in nodes)-HEADER
    right=max(n['x']+node_size(n,sizes)[0] for n in nodes)+PADDING
    bottom=max(n['y']+node_size(n,sizes)[1] for n in nodes)+PADDING
    group.update(x=left,y=top,width=max(500,right-left),height=bottom-top)


def arrange(graph,shared=False,sizes=None):
    if graph.get('composer_version'):return arrange_media(graph,shared,sizes)
    sizes=sizes or graph.get('node_sizes',{})
    graph['node_sizes']=sizes
    nodes=graph['nodes'];byid={n['id']:n for n in nodes};placed=set();y=190
    refs=[n for n in nodes if n.get('shared_node_id')]
    if shared:
        refs=[]
        people=[n for n in nodes if n['type']=='image']
        rowtop=290
        stride=max([node_size(n,sizes)[0] for n in nodes] or [WIDTH])+GAP
        for start in range(0,len(people),2):
            height=0
            for col,im in enumerate(people[start:start+2]):
                linked=[byid[e['source']] for e in graph['edges'] if e['target']==im['id'] and e['source'] in byid and byid[e['source']]['type']=='prompt' and e['source'] not in placed]
                x=63+col*stride*2;py=rowtop
                for p in linked:
                    p.update(x=x,y=py);placed.add(p['id']);py+=node_size(p,sizes)[1]+GAP
                im.update(x=x+stride,y=rowtop);placed.add(im['id'])
                height=max(height,py-rowtop-GAP if linked else 0,node_size(im,sizes)[1])
            rowtop+=height+GAP
        if people:
            box={'id':'project-cast','title':'全局人物 · 生成 Prompt 与母版','kind':'cast'}
            bounds(box,[n for n in nodes if n['id'] in placed],sizes);graph['cast_group']=box
            y=box['y']+box['height']+100
        else:graph.pop('cast_group',None)
    elif refs:
        columns=min(4,len(refs));rowtop=y+HEADER
        for start in range(0,len(refs),columns):
            row=refs[start:start+columns]
            for i,n in enumerate(row):n.update(x=63+i*370,y=rowtop);placed.add(n['id'])
            rowtop+=max(node_size(n,sizes)[1] for n in row)+GAP
        box={'id':'episode-cast','title':'本集人物 · 全局共享 Prompt 与采用图','kind':'cast'}
        bounds(box,refs,sizes);graph['cast_group']=box;y=box['y']+box['height']+100
    else:graph.pop('cast_group',None)
    for group in graph.get('groups',[]):
        members=[n for n in nodes if n.get('clip_id')==group['id'] and not n.get('shared_node_id')]
        if not members:continue
        primary=byid.get(group.get('video_node_id'))
        videos=[n for n in members if n['type']=='video']
        if primary not in videos:primary=videos[0] if videos else None
        vp=byid.get(group.get('prompt_node_id'))
        if vp not in members:vp=next((n for n in members if n.get('role')=='video_prompt'),None)
        images=[n for n in members if n['type']=='image']
        if vp is None:
            input_ids={e['source'] for e in graph['edges'] if primary and e['target']==primary['id']}
            shot_inputs={e['source'] for e in graph['edges'] if e['target'] in {im['id'] for im in images}}
            vp=next((n for n in members if n['type']=='prompt' and n['id'] in input_ids-shot_inputs and n.get('role')!='shot_prompt'),None)
        if vp is None and not images:vp=next((n for n in members if n['type']=='prompt'),None)
        if vp:group['prompt_node_id']=vp['id']
        if primary:group['video_node_id']=primary['id']
        rows=[];taken=set()
        for index,im in enumerate(images):
            linked=[byid[e['source']] for e in graph['edges'] if e['target']==im['id'] and e['source'] in {n['id'] for n in members} and byid[e['source']]['type']=='prompt' and e['source'] not in taken and byid[e['source']] is not vp]
            prompt=linked[0] if linked else next((n for n in members if n.get('shot_id') and n.get('shot_id')==im.get('shot_id') and n['type']=='prompt' and n['id'] not in taken),None)
            row=[p for p in [prompt,im] if p]
            taken.update(n['id'] for n in row)
            rows.append((row,im.get('shot_id') or f"{group['id']}-S{index+1:02}"))
        for n in members:
            if n is not vp and n not in videos and n['id'] not in taken:
                rows.append(([n],n.get('shot_id')));taken.add(n['id'])
        rowtop=y+HEADER;shot_rows=[]
        for index,(row,sid) in enumerate(rows):
            for n in row:n.update(x=63+(0 if n['type']=='prompt' else 370),y=rowtop);placed.add(n['id'])
            h=max(node_size(n,sizes)[1] for n in row)
            shot_rows.append({'id':sid or f"{group['id']}-row{index}",'node_ids':[n['id'] for n in row],
                              'title':f'分镜 {index+1:02}' if any(n['type']=='image' for n in row) else '补充 Prompt','x':48,'y':rowtop-28,'width':735,'height':h+44})
            rowtop+=h+GAP
        righttop=y+HEADER
        if vp:vp.update(x=803 if images else 63,y=righttop);placed.add(vp['id'])
        for index,v in enumerate(videos):
            v.update(x=1173 if images else 433,y=righttop);placed.add(v['id'])
            righttop+=max(node_size(v,sizes)[1],node_size(vp,sizes)[1] if vp and index==0 else 0)+GAP
        group['shot_rows']=shot_rows
        group['shot_count']=len(images)
        group['shot_image_ids']=[im['id'] for im in images]
        bounds(group,members,sizes)
        group['width']=max(group['width'],1510 if images else 800)
        y=group['y']+group['height']+100
    # Independent cards form a flow by graph depth when explicitly tidied.
    # Image/video outputs are still separate cards; frames are presentation only.
    free=[n for n in nodes if n['id'] not in placed];free_ids={n['id'] for n in free}
    indegree={nid:0 for nid in free_ids};children={nid:[] for nid in free_ids};rank={nid:0 for nid in free_ids}
    for edge in graph['edges']:
        if edge['source'] in free_ids and edge['target'] in free_ids:
            children[edge['source']].append(edge['target']);indegree[edge['target']]+=1
    queue=[n['id'] for n in free if not indegree[n['id']]]
    for nid in queue:
        for child in children[nid]:
            rank[child]=max(rank[child],rank[nid]+1);indegree[child]-=1
            if not indegree[child]:queue.append(child)
    columns={}
    for n in free:columns.setdefault(rank[n['id']],[]).append(n)
    x=63
    for level,column in sorted(columns.items()):
        top=y+HEADER
        for n in column:
            n.update(x=x,y=top);top+=node_size(n,sizes)[1]+GAP
        x+=max(node_size(n,sizes)[0] for n in column)+GAP
    graph.update(layout_version=VERSION,layout_mode='tidy')
    return graph


def arrange_media(graph,shared=False,sizes=None):
    """Compact media tiles; the open composer floats below its selected tile."""
    sizes=sizes or graph.get('node_sizes',{})
    sizes={**sizes,**{n['id']:sizes.get(n['id'],{'width':310,'height':250}) for n in graph['nodes']}}
    graph['node_sizes']=sizes
    nodes=[n for n in graph['nodes'] if n['type']!='prompt'];placed=set();y=220
    refs=[n for n in nodes if n.get('shared_node_id') or shared and n['type']=='image']
    def height(n):return min(420,max(230,sizes.get(n['id'],{}).get('height',250)))
    if refs:
        for i,n in enumerate(refs):n.update(x=63+(i%4)*390,y=y+(i//4)*320);placed.add(n['id'])
        box=dict(id='project-cast' if shared else 'episode-cast',title='全局人物' if shared else '本集人物',kind='cast')
        bounds(box,refs,{n['id']:{'height':height(n),'width':310} for n in refs});graph['cast_group']=box
        y=box['y']+box['height']+130
    else:graph.pop('cast_group',None)
    for group in graph.get('groups',[]):
        members=[n for n in nodes if n.get('clip_id')==group['id'] and n['id'] not in placed]
        if not members:continue
        images=[n for n in members if n['type']=='image'];videos=[n for n in members if n['type']=='video']
        rows=[]
        for i,n in enumerate(images):
            n.update(x=63+(i%2)*390,y=y+(i//2)*320);placed.add(n['id'])
            rows.append(dict(id=n.get('shot_id') or n['id'],node_ids=[n['id']],title=f'分镜 {i+1:02}',x=n['x']-12,y=n['y']-24,width=334,height=height(n)+36))
        for i,n in enumerate(videos):n.update(x=843+i*390,y=y);placed.add(n['id'])
        group.update(shot_rows=rows,shot_count=len(images),shot_image_ids=[n['id'] for n in images]);group.pop('prompt_node_id',None)
        bounds(group,members,{n['id']:{'height':height(n),'width':310} for n in members});y=group['y']+group['height']+130
    free=[n for n in nodes if n['id'] not in placed];ids={n['id'] for n in free};rank={nid:0 for nid in ids};children={nid:[] for nid in ids};degree={nid:0 for nid in ids}
    for edge in graph['edges']:
        if edge['source'] in ids and edge['target'] in ids:children[edge['source']].append(edge['target']);degree[edge['target']]+=1
    queue=[n['id'] for n in free if not degree[n['id']]]
    for nid in queue:
        for target in children[nid]:
            rank[target]=max(rank[target],rank[nid]+1);degree[target]-=1
            if not degree[target]:queue.append(target)
    counts={}
    for n in free:
        level=rank[n['id']];row=counts.get(level,0);counts[level]=row+1
        n.update(x=63+level*390,y=y+row*320)
    graph.update(layout_version=VERSION,layout_mode='tidy')
    return graph


def reframe(graph,shared=False):
    sizes=graph.get('node_sizes',{})
    if graph.get('composer_version'):
        for n in graph['nodes']:sizes.setdefault(n['id'],{'width':310,'height':250})
        graph['node_sizes']=sizes
    for group in graph.get('groups',[]):
        members=[n for n in graph['nodes'] if n.get('clip_id')==group['id'] and not n.get('shared_node_id')]
        bounds(group,members,sizes)
        group['shot_image_ids']=[n['id'] for n in members if n['type']=='image']
        group['shot_count']=len(group['shot_image_ids'])
        group['shot_rows']=[row for row in group.get('shot_rows',[]) if any(n['id'] in row['node_ids'] for n in members)]
        for row in group.get('shot_rows',[]):
            items=[n for n in members if n['id'] in row['node_ids']]
            if items:
                row.update(x=min(n['x'] for n in items)-15,y=min(n['y'] for n in items)-28,
                           width=max(n['x']+node_size(n,sizes)[0] for n in items)-min(n['x'] for n in items)+30,
                           height=max(n['y']+node_size(n,sizes)[1] for n in items)-min(n['y'] for n in items)+44)
    if graph.get('cast_group'):
        members=[n for n in graph['nodes'] if n.get('shared_node_id')]
        if shared:
            images={n['id'] for n in graph['nodes'] if n['type']=='image'}
            prompt_ids={e['source'] for e in graph['edges'] if e['target'] in images}
            members=[n for n in graph['nodes'] if n['id'] in images|prompt_ids]
        if members:bounds(graph['cast_group'],members,sizes)
        else:graph.pop('cast_group',None)
