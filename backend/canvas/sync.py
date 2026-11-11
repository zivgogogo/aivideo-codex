"""Merge only edits made since a client's last acknowledged graph.

Media and director metadata remain server-owned. Independent fields, nodes and
edges can be edited concurrently without resubmitting a stale whole canvas.
"""
from copy import deepcopy

NODE_FIELDS=('id','type','x','y','title','text','settings','clip_id','shot_id','role','character_id','shared_node_id','description')
GRAPH_FIELDS=('viewport','layout_mode')
MISSING=object()


def snapshot(graph):
    return dict(nodes=[{k:deepcopy(n[k]) for k in NODE_FIELDS if k in n} for n in graph.get('nodes',[])],
                edges=deepcopy(graph.get('edges',[])),
                **{k:deepcopy(graph[k]) for k in GRAPH_FIELDS if k in graph})


def merge_edits(current, incoming, base, recover=False):
    """Apply the client's delta to current; retain displaced same-field values."""
    for graph in (incoming,base):
        if not isinstance(graph,dict) or not isinstance(graph.get('nodes'),list) or not isinstance(graph.get('edges'),list):
            raise ValueError('保存草稿格式无效')
        if len(graph['nodes'])>2400 or len(graph['edges'])>12000:raise ValueError('画布超过节点或连线上限')
        ids=[n.get('id') for n in graph['nodes'] if isinstance(n,dict)]
        if len(ids)!=len(graph['nodes']) or any(not isinstance(i,str) for i in ids) or len(ids)!=len(set(ids)):
            raise ValueError('节点编号重复或无效')
    result=deepcopy(current);conflicts=[]
    before={n['id']:n for n in base['nodes']};local={n['id']:n for n in incoming['nodes']}
    live={n['id']:n for n in result['nodes']}
    for nid,previous in before.items():
        if nid not in local:live.pop(nid,None)
    for nid,n in local.items():
        previous=before.get(nid,{})
        if nid not in live:
            if nid not in before or any(n.get(k)!=previous.get(k) for k in NODE_FIELDS):live[nid]=deepcopy(n)
            continue
        target=live[nid]
        if target['type']!=n.get('type'):raise ValueError('已有节点不能改变类型')
        for key in NODE_FIELDS:
            if key in {'id','type'}:continue
            old=previous.get(key,MISSING);value=n.get(key,MISSING)
            if key=='settings' and all(isinstance(v,dict) for v in (old,value,target.get(key))):
                for setting in old.keys()|value.keys():
                    apply_field(target[key],setting,old.get(setting,MISSING),value.get(setting,MISSING),conflicts,nid,'settings.',recover)
            else:apply_field(target,key,old,value,conflicts,nid,'',recover)
    # Preserve current ordering, then append new local cards in creation order.
    result['nodes']=list(live.values())
    def edge_key(e):
        if not isinstance(e,dict) or not isinstance(e.get('source'),str) or not isinstance(e.get('target'),str):
            raise ValueError('连线格式无效')
        return (e['source'],e['target'])
    original={edge_key(e) for e in base['edges']};desired={edge_key(e):e for e in incoming['edges']}
    edges={edge_key(e):e for e in current['edges'] if edge_key(e) not in original-desired.keys()}
    for key,e in desired.items():
        if key not in original:edges[key]=deepcopy(e)
    result['edges']=[e for (s,t),e in edges.items() if s in live and t in live]
    for key in GRAPH_FIELDS:
        apply_field(result,key,base.get(key,MISSING),incoming.get(key,MISSING),[],None,'',False)
    return result,conflicts


def apply_field(target,key,old,value,conflicts,nid,prefix,recover):
    if value==old:return
    remote=target.get(key,MISSING)
    if remote!=value and remote is not MISSING and (recover or remote!=old):
        conflicts.append(dict(node_id=nid,field=prefix+key,previous=deepcopy(remote)))
    if value is MISSING:target.pop(key,None)
    else:target[key]=deepcopy(value)
