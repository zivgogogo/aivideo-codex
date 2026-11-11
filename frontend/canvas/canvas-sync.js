/* Pure graph edit rebasing, shared by autosave and draft recovery. */
"use strict";
const CanvasSync = (() => {
  const fields=['id','type','x','y','title','text','settings','clip_id','shot_id','role','character_id','shared_node_id','description'];
  const copy=v=>v===undefined?undefined:JSON.parse(JSON.stringify(v));
  const equal=(a,b)=>JSON.stringify(canonical(a))===JSON.stringify(canonical(b));
  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value==='object') return Object.fromEntries(Object.keys(value).sort().map(k=>[k,canonical(value[k])]));
    return value;
  }
  function snapshot(g) {
    return {nodes:g.nodes.map(n=>Object.fromEntries(fields.filter(k=>k in n).map(k=>[k,copy(n[k])]))),
      edges:g.edges.map(e=>({id:e.id,source:e.source,target:e.target})),viewport:copy(g.viewport),layout_mode:g.layout_mode};
  }
  function field(target,key,local,base) {
    if (equal(local,base)) return;
    if (local===undefined) delete target[key]; else target[key]=copy(local);
  }
  function rebase(remote,local,base) {
    const result=copy(remote),before=new Map(base.nodes.map(n=>[n.id,n])),desired=new Map(local.nodes.map(n=>[n.id,n])),live=new Map(result.nodes.map(n=>[n.id,n]));
    for (const [nid] of before) if (!desired.has(nid)) live.delete(nid);
    for (const [nid,n] of desired) {
      const old=before.get(nid)||{};
      if (!live.has(nid)) {
        if (!before.has(nid) || fields.some(k=>!equal(n[k],old[k]))) live.set(nid,copy(n));
        continue;
      }
      const target=live.get(nid);
      for (const key of fields) {
        if (key==='id'||key==='type') continue;
        if (key==='settings' && old.settings && n.settings && target.settings) {
          for (const setting of new Set([...Object.keys(old.settings),...Object.keys(n.settings)])) field(target.settings,setting,n.settings[setting],old.settings[setting]);
        } else field(target,key,n[key],old[key]);
      }
    }
    result.nodes=[...live.values()];
    const key=e=>JSON.stringify([e.source,e.target]),original=new Set(base.edges.map(key)),wanted=new Map(local.edges.map(e=>[key(e),e]));
    const edges=new Map(remote.edges.filter(e=>!original.has(key(e))||wanted.has(key(e))).map(e=>[key(e),e]));
    for (const [k,e] of wanted) if (!original.has(k)) edges.set(k,copy(e));
    result.edges=[...edges.values()].filter(e=>live.has(e.source)&&live.has(e.target));
    for (const key of ['viewport','layout_mode']) field(result,key,local[key],base[key]);
    return result;
  }
  function recoverLegacy(remote,draft) {
    const result=copy(remote),live=new Map(result.nodes.map(n=>[n.id,n]));
    for (const n of draft.nodes) {
      const target=live.get(n.id);
      if (!target) {live.set(n.id,copy(n));continue;}
      // Keep the explicit character-reference header introduced by the director.
      const header=target.text?.match(/^人物参考：[^\n]+\n\n/)?.[0];
      for (const key of fields) if (key!=='id'&&key!=='type'&&key in n) target[key]=copy(n[key]);
      if (header && !target.text.startsWith('人物参考：') && header.length+target.text.length<=18000) target.text=header+target.text;
    }
    result.nodes=[...live.values()];
    const keys=new Set(result.edges.map(e=>e.source+':'+e.target));
    for (const e of draft.edges) if (live.has(e.source)&&live.has(e.target)&&!keys.has(e.source+':'+e.target)) {
      // A pre-upgrade actor-to-image edge is already represented through Prompt.
      const source=live.get(e.source),target=live.get(e.target);
      if (source.shared_node_id && target.role==='shot_image' && result.edges.some(link=>link.source===source.id&&live.get(link.target)?.shot_id===target.shot_id&&live.get(link.target)?.role==='shot_prompt')) continue;
      result.edges.push(copy(e));keys.add(e.source+':'+e.target);
    }
    result.viewport=copy(draft.viewport);result.layout_mode=draft.layout_mode||result.layout_mode;
    return result;
  }
  function separatePrompts(graph,create) {
    let changed=false;
    for (const media of [...graph.nodes]) {
      if (media.type==='prompt' || media.shared_node_id || !media.text?.trim()) continue;
      const incoming=graph.edges.filter(e=>e.target===media.id).map(e=>graph.nodes.find(n=>n.id===e.source)).filter(n=>n?.type==='prompt');
      const main=incoming.length===1?incoming[0]:null;
      if (main && graph.edges.filter(e=>e.source===main.id).length===1 && main.text.length+media.text.length+2<=18000) {
        main.text=[main.text,media.text].filter(Boolean).join('\n\n');
      } else {
        const position={x:media.x-390,y:media.y};
        for (let i=0;i<100;i++) {
          const collision=graph.nodes.find(n=>Math.abs(n.x-position.x)<335 && position.y<n.y+(graph.node_sizes?.[n.id]?.height||540)+35 && position.y+540>n.y);
          if (!collision) break;
          position.y=collision.y+(graph.node_sizes?.[collision.id]?.height||540)+60;
        }
        const prompt=create(position);prompt.text=media.text;
        if (media.clip_id) prompt.clip_id=media.clip_id;
        graph.nodes.push(prompt);graph.edges.push({id:'e_'+prompt.id,source:prompt.id,target:media.id});
      }
      media.text='';changed=true;
    }
    return changed;
  }
  function videoReferences(graph,targetId) {
    const nodes=new Map(graph.nodes.map(n=>[n.id,n])),seen=new Set(),videos=new Set();
    const names=graph.nodes.filter(n=>n.type==='video').flatMap(n=>[n.ref_name,n.title].filter(Boolean).map(name=>[name,n.id]));
    function walk(id,root=false) {
      if (seen.has(id)) return;seen.add(id);
      const n=nodes.get(id);if (!n) return;
      if (!root && n.type!=='prompt') {if(n.type==='video')videos.add(n.id);return;}
      const text=n.text||'';
      for (const [name,nid] of names) {
        const pattern=new RegExp('@'+name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+(/^视频\d+$/.test(name)?'(?!\\d)':''));
        if (pattern.test(text) && nid!==targetId) videos.add(nid);
      }
      for (const edge of graph.edges) if(edge.target===id)walk(edge.source);
    }
    walk(targetId,true);return [...videos];
  }
  return {snapshot,rebase,recoverLegacy,separatePrompts,videoReferences,equal};
})();
if (typeof module!=='undefined') module.exports=CanvasSync;
