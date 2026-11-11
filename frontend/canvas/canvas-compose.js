"use strict";
const CanvasCompose=(()=>{
  function embed(graph) {
    const result=JSON.parse(JSON.stringify(graph));
    if(!result.nodes.some(n=>n.type==='prompt')) {result.composer_version=1;return result;}
    const byId=new Map(result.nodes.map(n=>[n.id,n])),incoming=new Map(result.nodes.map(n=>[n.id,[]])),outgoing=new Map(result.nodes.map(n=>[n.id,[]])),consumed=new Set();
    for(const e of result.edges){incoming.get(e.target)?.push(e.source);outgoing.get(e.source)?.push(e.target);}
    function inputs(id){
      const texts=[],media=[],seen=new Set(),visiting=new Set([id]);
      function walk(source){
        if(visiting.has(source))throw new Error('连线形成循环');
        if(seen.has(source))return;seen.add(source);const n=byId.get(source);if(!n)return;
        if(n.type==='prompt'){
          consumed.add(source);visiting.add(source);for(const p of incoming.get(source))walk(p);
          visiting.delete(source);texts.push((n.text||'').trim());
        }else media.push(source);
      }
      for(const source of incoming.get(id)||[])walk(source);return {texts,media};
    }
    const nodes=[],edges=[],used=new Set(byId.keys());
    for(const n of result.nodes){
      if(n.type==='prompt')continue;const {texts,media}=inputs(n.id);
      n.text=[...texts,(n.text||'').trim()].filter(Boolean).join('\n\n');nodes.push(n);
      for(const source of media)edges.push({id:('edge_'+edges.length+'_'+n.id).slice(0,80),source,target:n.id});
    }
    for(const old of result.nodes){
      if(old.type!=='prompt'||consumed.has(old.id)||(outgoing.get(old.id)||[]).some(id=>byId.get(id)?.type==='prompt'))continue;
      const {texts,media}=inputs(old.id),stem=('media_'+old.id).slice(-70);let id=stem,index=0;
      while(used.has(id))id=stem+'_'+(++index);used.add(id);
      const n={...old,id,type:old.role==='video_prompt'?'video':'image',text:[...texts,(old.text||'').trim()].filter(Boolean).join('\n\n'),assets:[],selected:null,selected_ids:[]};
      for(const key of ['ref_name','role','shared_node_id'])delete n[key];nodes.push(n);
      for(const source of media)edges.push({id:('edge_'+edges.length+'_'+id).slice(0,80),source,target:id});
    }
    const aliases=new Set(nodes.map(n=>n.ref_name));
    for(const n of nodes)if(!n.ref_name){const prefix=n.type==='image'?'图':'视频';let i=1;while(aliases.has(prefix+i))i++;n.ref_name=prefix+i;aliases.add(n.ref_name);}
    result.nodes=nodes;result.edges=edges;result.composer_version=1;
    for(const group of result.groups||[])delete group.prompt_node_id;return result;
  }
  return {embed};
})();
if(typeof module!=='undefined')module.exports=CanvasCompose;
