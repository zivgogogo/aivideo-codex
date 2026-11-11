"""Lossless conversion from separate Prompt nodes to per-media composers."""
from copy import deepcopy

VERSION=1


def embed_prompts(graph):
    result=deepcopy(graph);byid={n['id']:n for n in graph['nodes']}
    if not any(n['type']=='prompt' for n in graph['nodes']):
        result['composer_version']=VERSION
        return result
    incoming={nid:[] for nid in byid};outgoing={nid:[] for nid in byid}
    for edge in graph['edges']:
        incoming[edge['target']].append(edge['source']);outgoing[edge['source']].append(edge['target'])
    consumed=set()
    def inputs(nid):
        texts=[];media=[];seen=set();visiting={nid}
        def walk(source):
            if source in visiting:raise ValueError('连线形成循环')
            if source in seen:return
            seen.add(source);n=byid[source]
            if n['type']=='prompt':
                consumed.add(source);visiting.add(source)
                for parent in incoming[source]:walk(parent)
                visiting.remove(source);texts.append(n.get('text','').strip())
            else:media.append(source)
        for source in incoming[nid]:walk(source)
        return texts,media
    nodes=[];edges=[];used=set(byid)
    for old in graph['nodes']:
        if old['type']=='prompt':continue
        n=deepcopy(old);texts,sources=inputs(n['id'])
        n['text']='\n\n'.join(filter(None,[*texts,n.get('text','').strip()]))
        nodes.append(n)
        for source in sources:edges.append(dict(id=f'edge_{len(edges)}_{n["id"]}'[:80],source=source,target=n['id']))
    # A Prompt with no output still contains user work: retain it as an empty
    # image/video card with its recipe inside, rather than discarding the text.
    for old in graph['nodes']:
        if old['type']!='prompt' or old['id'] in consumed or any(byid[t]['type']=='prompt' for t in outgoing[old['id']]):continue
        texts,sources=inputs(old['id']);stem=('media_'+old['id'])[-70:];nid=stem;index=0
        while nid in used:index+=1;nid=stem+'_'+str(index)
        used.add(nid);n=deepcopy(old)
        n.update(id=nid,type='video' if old.get('role')=='video_prompt' else 'image',
                 text='\n\n'.join(filter(None,[*texts,old.get('text','').strip()])),assets=[],selected=None,selected_ids=[])
        for key in ('ref_name','role','shared_node_id'):n.pop(key,None)
        nodes.append(n)
        for source in sources:edges.append(dict(id=f'edge_{len(edges)}_{nid}'[:80],source=source,target=nid))
    result.update(nodes=nodes,edges=edges,composer_version=VERSION)
    for group in result.get('groups',[]):group.pop('prompt_node_id',None)
    return result
