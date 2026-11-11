"use strict";
const $ = (s) => document.querySelector(s),
  $$ = (s) => [...document.querySelectorAll(s)];
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const id = (p) => p + crypto.randomUUID().replaceAll("-", "").slice(0, 16);
const clone = (v) => JSON.parse(JSON.stringify(v));
const cardName = (type) => ({prompt:'Prompt卡',image:'图片卡',video:'视频卡'})[type];
const cardLabel = (n) => cardName(n.type)+' @'+n.ref_name;
const app = {
  project: null,
  episode: null,
  graph: null,
  baseGraph: null,
  projects: [],
  selected: null,
  edge: null,
  link: null,
  dirty: false,
  edit: 0,
  saving: null,
  saveTimer: null,
  retryDelay: 1000,
  legacyRecovery: false,
  history: [],
  future: [],
  drag: null,
  space: false,
  menu: false,
  drawer: null,
  polling: false,
  uploadNode: null,
  file: null,
  scope: 0,
};
const LINK_TYPES = new Set(["prompt:prompt","prompt:image","prompt:video","image:prompt","image:image","image:video","video:prompt","video:video"]);
function canConnect(source,target) {
  return source && target && source.id !== target.id && !target.shared_node_id && LINK_TYPES.has(source.type+":"+target.type) &&
    !(target.type==='image' && (source.type==='video'||source.type==='prompt'&&CanvasSync.videoReferences(app.graph,source.id).length));
}
function withImageReference(text,image) {
  const names=[image.ref_name,image.title].filter(Boolean);
  if (/^图\d+$/.test(image.ref_name)) names.push('图片'+image.ref_name.slice(1));
  if (names.some(name=>new RegExp('@'+name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+(/^(?:图片|图)\d+$/.test(name)?'(?!\\d)':'')).test(text))) return text;
  const prefix=(image.shared_node_id || image.role==='character'?'人物参考：':'图片参考：')+'@'+image.ref_name+'（'+image.title+'）。\n\n';
  return prefix.length+text.length<=18000 ? prefix+text : text;
}
function withVideoReference(text,video) {
  if (new RegExp('@'+video.ref_name+'(?!\\d)').test(text)) return text;
  const prefix='视频参考：@'+video.ref_name+'。\n\n';
  return prefix.length+text.length<=18000?prefix+text:text;
}
function referenceDropTarget(element) {
  $$(".reference-drop-target").forEach(el => { if (el !== element) el.classList.remove("reference-drop-target"); });
  element?.classList.add("reference-drop-target");
}
let imageMentionPattern = /@(?:图片|图)\d+/g;
const promptEditors = new Set();
const promptHighlightState = new WeakMap();
const promptResizeObserver = new ResizeObserver(entries => {
  for (const {target} of entries) syncPromptHighlight(target);
});
function prepareImageMentions() {
  const names = [...new Set((app.graph?.nodes || [])
    .filter(n => n.type === "image")
    .flatMap(n => [n.ref_name, n.title])
    .filter(name => name && !/^(?:图片|图)\d+$/.test(name)))];
  names.sort((a,b) => b.length-a.length);
  const named = names.map(name => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  imageMentionPattern = new RegExp("@(?:" + [...named, "(?:图片|图)\\d+"].join("|") + ")", "g");
}
function highlightedPrompt(value) {
  let result = "", start = 0;
  for (const match of value.matchAll(imageMentionPattern)) {
    result += esc(value.slice(start, match.index)) + `<mark class="prompt-image-mention">${esc(match[0])}</mark>`;
    start = match.index + match[0].length;
  }
  return result + esc(value.slice(start)) + "\n";
}
function syncPromptHighlight(area) {
  const layer = area.parentElement?.querySelector(".prompt-highlight");
  if (!layer) return;
  const text = layer.firstElementChild;
  const previous = promptHighlightState.get(area);
  if (previous?.value !== area.value || previous?.pattern !== imageMentionPattern.source) {
    text.innerHTML = highlightedPrompt(area.value);
    promptHighlightState.set(area, {value:area.value, pattern:imageMentionPattern.source});
  }
  layer.style.width = area.clientWidth + "px";
  layer.style.height = area.clientHeight + "px";
  text.style.transform = `translate(${-area.scrollLeft}px, ${-area.scrollTop}px)`;
}
function setupPromptHighlights() {
  const editors = new Set($$("#nodes .prompt-editor textarea"));
  for (const area of promptEditors) {
    if (!editors.has(area)) { promptResizeObserver.unobserve(area); promptEditors.delete(area); }
  }
  for (const area of editors) {
    if (!promptEditors.has(area)) { promptResizeObserver.observe(area); promptEditors.add(area); }
    syncPromptHighlight(area);
  }
}
function syncVideoWarning(n) {
  if(n.type!=='image'||n.shared_node_id)return;
  const el=$(`.node[data-node="${n.id}"]`),button=el?.querySelector('[data-generate]'),warning=el?.querySelector('[data-video-warning]');
  const blocked=CanvasSync.videoReferences(app.graph,n.id).length>0;
  if(button)button.disabled=blocked||!!busy(nodeJob(n))||!!app.graph.composer_pending;
  if(warning)warning.hidden=!blocked;
}
const storage = {
  get(k) {
    try {
      return localStorage.getItem(k);
    } catch {
      return null;
    }
  },
  set(k, v) {
    try {
      localStorage.setItem(k, v);
    } catch {}
  },
  remove(k) {
    try {
      localStorage.removeItem(k);
    } catch {}
  },
};
function toast(msg, error = false) {
  const el = document.createElement("div");
  el.className = "toast" + (error ? " error" : "");
  el.textContent = msg;
  $("#toast-stack").append(el);
  setTimeout(() => el.remove(), error ? 11000 : 4500);
}
async function api(route, body, query = {}) {
  if(route==='graph')query={...query,display:'media'};
  const url =
    "/api/canvas/" +
    route +
    (Object.keys(query).length ? "?" + new URLSearchParams(query) : "");
  let res;
  try { res = await fetch(url, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  }); } catch (cause) { const error=new Error('连接中断，修改已暂存在本机');error.retryable=true;throw error; }
  let data;
  try {
    data = await res.json();
  } catch {
    throw new Error("服务响应异常，请检查本地工作台");
  }
  if (!res.ok) { const error=new Error(data.error || '请求失败');error.retryable=res.status>=500;throw error; }
  return data;
}
const query = () => ({ project_id: app.project.id, episode_id: app.episode });
const currentEp = () =>
  app.episode === "__characters__"
    ? {
        id: "__characters__",
        title: "项目共享人物",
        summary: "同一个人物，一套身份。全项目实时共享采用版本。",
      }
    : app.project?.episodes.find((e) => e.id === app.episode);
const currentNode = () => app.graph?.nodes.find((n) => n.id === app.selected);
const selectedAsset = (n) => n.assets?.find((a) => a.id === n.selected);
const nodeJob = (n) =>
  [...(app.project?.jobs || [])]
    .reverse()
    .find((j) => j.node_id === n.id && j.episode_id === app.episode);
const busy = (j) => j && ["queued", "running"].includes(j.status);
const draftKey = () => `story-canvas:draft:${app.project?.id}:${app.episode}`;
function savedLabel(text, error = false) {
  $("#save-status").textContent = text;
  $("#save-status").classList.toggle("error", error);
}
function backup() {
  if (app.graph && app.project)
    storage.set(draftKey(), JSON.stringify({format:2,graph:app.graph,base:app.baseGraph,recover_legacy:app.legacyRecovery}));
}
function reportSaveError(error) {
  if (app.lastSaveError!==error.message) {toast(error.message,true);app.lastSaveError=error.message;}
}
function queueSave(delay=550) {
  clearTimeout(app.saveTimer);
  app.saveTimer=setTimeout(()=>save().catch(reportSaveError),delay);
}
function changed() {
  app.dirty = true;
  app.edit++;
  backup();
  savedLabel("正在保存…");
  queueSave();
  renderHistory();
  renderEpisodes();
}
function checkpoint() {
  if (!app.graph) return;
  app.history.push(clone(app.graph));
  if (app.history.length > 50) app.history.shift();
  app.future = [];
  renderHistory();
}
function renderHistory() {
  $("#undo").disabled = !app.history.length;
  $("#redo").disabled = !app.future.length;
}
function applySyncedGraph(graph) {
  const structure=app.graph?.nodes.map(n=>n.id).join(',')!==graph.nodes.map(n=>n.id).join(',');
  const active=document.activeElement,focused=active?.dataset.text;
  const selection=focused ? {start:active.selectionStart,end:active.selectionEnd,top:active.scrollTop} : null;
  app.graph=graph;
  if (structure) {
    renderGraph();
    const area=focused && $(`[data-text="${focused}"]`);
    if (area) {area.focus({preventScroll:true});area.setSelectionRange(selection.start,selection.end);area.scrollTop=selection.top;}
  } else {
    for (const n of graph.nodes) {
      const el=$(`.node[data-node="${n.id}"]`);
      if (el) {el.style.left=n.x+'px';el.style.top=n.y+'px';}
      const area=el?.querySelector('[data-text]');
      if (area && area.value!==n.text) {area.value=n.text;syncPromptHighlight(area);}
    }
    refreshNodes();renderEpisodes();updateViewport();
  }
}
function rebaseGraph(remote,local,base) {
  if(remote.composer_version){local=CanvasCompose.embed(local);base=CanvasCompose.embed(base);}
  return CanvasSync.rebase(remote,local,base);
}
async function save() {
  clearTimeout(app.saveTimer);
  if (app.arranging) await app.arranging;
  if (app.saving) {
    await app.saving;
    if (app.dirty) return save();
    return;
  }
  if (!app.dirty || !app.graph) return;
  const scope = app.scope,
    graph = clone(app.graph),
    context = query();
  app.saving = (async () => {
    try {
      const result = await api("save", {...context,...graph,base:CanvasSync.snapshot(app.baseGraph || graph),recover_legacy:app.legacyRecovery});
      if (scope !== app.scope) return;
      const pending=rebaseGraph(result,app.graph,graph);
      app.baseGraph=clone(result);
      app.legacyRecovery=false;app.retryDelay=1000;app.lastSaveError=null;
      applySyncedGraph(pending);
      app.dirty=!CanvasSync.equal(CanvasSync.snapshot(pending),CanvasSync.snapshot(result));
      if (!app.dirty) {
        storage.remove(draftKey());
        savedLabel("已自动保存");
      } else {
        backup();
        savedLabel("正在保存…");
      }
      if (result.sync?.recovered) toast('已同步保存；同处修改的旧版本可点击顶部保存状态查看');
    } catch (e) {
      backup();
      savedLabel(e.retryable?'已暂存 · 正在重连':'已暂存 · 点击重试',true);
      if (e.retryable) {queueSave(app.retryDelay);app.retryDelay=Math.min(10000,app.retryDelay*2);}
      throw e;
    } finally {
      app.saving = null;
    }
  })();
  await app.saving;
  if (app.dirty) {if (app.drag) queueSave();else return save();}
}
async function refreshProjects() {
  app.projects = await api("projects");
  return app.projects;
}
async function openProject(pid) {
  await save();
  app.scope++;
  const scope = app.scope;
  const d = await api("project", undefined, { project_id: pid });
  if (scope !== app.scope) return;
  app.project = d;
  app.episode = null;
  app.graph = null;
  app.baseGraph = null;
  app.dirty = false;
  app.legacyRecovery = false;
  app.selected = null;
  app.history = [];
  app.future = [];
  $("#project-title").textContent = d.title;
  document.title = d.title + " · 映序画布";
  storage.set("story-canvas:project", pid);
  renderEpisodes();
  renderJobsBadge();
  if (d.episodes.length) {
    const remembered = storage.get("story-canvas:episode:" + pid);
    await openEpisode(
      remembered === "__characters__" ||
        d.episodes.some((e) => e.id === remembered)
        ? remembered
        : d.episodes[0].id,
    );
  } else {
    renderGraph();
    renderHeading();
    savedLabel(
      d.status === "initializing" ? "正在初始化项目…" : "初始化待处理",
    );
    if (d.status === "failed") showJobs();
  }
  closeMenu();
  $("#drawer").hidden = true;
  app.drawer = null;
}

function point(clientX, clientY) {
  const r = $("#viewport").getBoundingClientRect(),
    v = app.graph.viewport;
  return {
    x: (clientX - r.left - v.x) / v.zoom,
    y: (clientY - r.top - v.y) / v.zoom,
  };
}
function updateViewport() {
  if (!app.graph) {
    $("#world").style.transform = "";
    return;
  }
  const v = app.graph.viewport;
  $("#world").style.transform = `translate(${v.x}px,${v.y}px) scale(${v.zoom})`;
  $("#zoom-label").textContent = Math.round(v.zoom * 100) + "%";
  $("#canvas-area").style.backgroundSize =
    Math.max(14,24 * v.zoom) + "px " + Math.max(14,24 * v.zoom) + "px";
  $("#canvas-area").style.backgroundPosition = v.x + "px " + v.y + "px";
  cancelAnimationFrame(app.edgeFrame);
  app.edgeFrame = requestAnimationFrame(drawEdges);
  requestAnimationFrame(()=>positionComposer());
}

function drawEdges() {
  if (!app.graph) {
    $("#edges").innerHTML = "";
    return;
  }
  const viewport = $("#viewport").getBoundingClientRect(), view = app.graph.viewport;
  const paths = app.graph.edges
    .map((e) => {
      const s = app.graph.nodes.find((n) => n.id === e.source),
        t = app.graph.nodes.find((n) => n.id === e.target);
      if (!s || !t || s.type==='prompt'||t.type==='prompt') return "";
      // Offscreen targets otherwise create hundreds of long cast-reference
      // curves across unrelated videos. The saved reference graph is unchanged.
      const tx=t.x*view.zoom+view.x, ty=t.y*view.zoom+view.y,
        height=app.graph.node_sizes?.[t.id]?.height || 250;
      if (e.id !== app.edge && (tx+310*view.zoom<0 || tx>viewport.width ||
          ty+height*view.zoom<0 || ty>viewport.height)) return "";
      return `<path class="edge ${s.type} ${app.edge === e.id ? "selected" : ""}" marker-end="url(#edge-arrow-${app.edge===e.id?'selected':s.type})" data-edge="${esc(e.id)}" d="${curve(s.x + 318, s.y + 132, t.x-12, t.y + 132)}"><title>${cardLabel(s)} → ${cardLabel(t)}；点击选中，按Delete删除</title></path>`;
    })
    .join("");
  let temp = "";
  if (app.link) {
    const s = app.graph.nodes.find((n) => n.id === app.link);
    if (s) {
      const p = app.linkPoint || {
        x: s.x + 380,
        y: s.y + 132,
      };
      temp = `<path class="edge temp" marker-end="url(#edge-arrow-selected)" d="${curve(s.x + 318, s.y + 132, p.x, p.y)}"/>`;
    }
  }
  const markers=['prompt','image','video','selected'].map(type=>`<marker id="edge-arrow-${type}" markerUnits="userSpaceOnUse" markerWidth="14" markerHeight="14" refX="12" refY="7" orient="auto"><path d="M1,1 L12,7 L1,13 Z" fill="var(--${type==='selected'?'accent':type})"/></marker>`).join('');
  $("#edges").innerHTML = '<defs>'+markers+'</defs>'+paths+temp;
}
function curve(x1, y1, x2, y2) {
  const d = Math.max(70, Math.abs(x2 - x1) * 0.45);
  return `M ${x1} ${y1} C ${x1 + d} ${y1},${x2 - d} ${y2},${x2} ${y2}`;
}
function select(nid) {
  app.selected = nid;
  app.edge = null;
  $$(".node").forEach((el) =>
    el.classList.toggle("selected", el.dataset.node === nid),
  );
  drawEdges();
  requestAnimationFrame(()=>{positionComposer(true);setupPromptHighlights();});
}
function positionComposer(pan=false) {
  const el=app.selected&&$(`.node[data-node="${app.selected}"]`),panel=el?.querySelector('.node-composer');
  if(!panel||!app.graph||app.drag)return;
  const view=$('#viewport').getBoundingClientRect(),zoom=app.graph.viewport.zoom;
  panel.style.width=Math.min(520,(view.width-28)/zoom)+'px';
  if(pan){
    const tile=el.getBoundingClientRect(),overflow=tile.bottom+14*zoom+panel.scrollHeight*zoom-view.bottom+60;
    let dx=tile.left<view.left+25?view.left+25-tile.left:tile.right>view.right-25?view.right-25-tile.right:0;
    let dy=tile.top<view.top+110?view.top+110-tile.top:overflow>0?-Math.min(overflow,tile.top-view.top-110):0;
    if(dx||dy){app.graph.viewport.x+=dx;app.graph.viewport.y+=dy;updateViewport();}
  }
  const tile=el.getBoundingClientRect();
  panel.style.maxHeight=Math.max(170,(view.bottom-60-tile.bottom-14*zoom)/zoom)+'px';
  panel.style.marginLeft='0px';
  const rect=panel.getBoundingClientRect(),shift=rect.left<view.left+14?view.left+14-rect.left:rect.right>view.right-14?view.right-14-rect.right:0;
  panel.style.marginLeft=shift/zoom+'px';
}

function addNode(type, from, requestedPosition) {
  if (!['image','video'].includes(type)) return;
  if (!app.graph) {
    toast("先创建一个项目，就可以自由添加节点");
    openProjectForm();
    return;
  }
  if (from && !canConnect(from,{id:'new-card',type})) {toast('视频参考只能连接到视频卡',true);return;}
  checkpoint();
  const r = $("#viewport").getBoundingClientRect();
  let position = point(
    r.left + r.width * 0.35,
    r.top + Math.max(200, r.height * 0.35),
  );
  if (from) position = { x: from.x + 390, y: from.y };
  else {
    position.x = Math.max(30, position.x);
    position.y = Math.max(165, position.y);
    while (
      app.graph.nodes.some(
        (n) =>
          Math.abs(n.x - position.x) < 80 && Math.abs(n.y - position.y) < 80,
      )
    ) {
      position.x += 40;
      position.y += 45;
    }
  }
  if (requestedPosition) position=requestedPosition;
  // Free cards belong to this episode, independently of the selected video.
  // Only explicit storyboard actions attach cards to a video frame.
  for (let attempt=0;attempt<100;attempt++) {
    const collision=app.graph.nodes.some(n => {
      const size=app.graph.node_sizes?.[n.id] || {width:310,height:250};
      return position.x < n.x+size.width+25 && position.x+335 > n.x &&
             position.y < n.y+size.height+25 && position.y+250 > n.y;
    });
    if (!collision) break;
    position.x+=80; position.y+=35;
  }
  const n = newNode(type, position);
  app.graph.nodes.push(n);
  if (from) {
    app.graph.edges.push({ id: id("e_"), source: from.id, target: n.id });
  }
  app.graph.layout_mode = "manual";
  select(n.id);
  changed();
  renderGraph();
  closeMenu();
  const view=app.graph.viewport;
  if (n.x*view.zoom+view.x > r.width-100 || n.y*view.zoom+view.y > r.height-100)
    locate(n.id,app.episode).catch(e => toast(e.message,true));
  return n;
}

function deleteSelection(nid = app.selected) {
  if (!app.graph) return;
  if (nid) {
    const n = app.graph.nodes.find((n) => n.id === nid);
    if (!n) return;
    if (busy(nodeJob(n))) {
      toast("请等这个节点的生成任务保存完成", true);
      return;
    }
  } else if (!app.edge) return;
  checkpoint();
  const removed=app.graph.edges.filter(e=>nid?e.source===nid||e.target===nid:e.id===app.edge);
  for (const edge of removed) {
    const source=app.graph.nodes.find(n=>n.id===edge.source),target=app.graph.nodes.find(n=>n.id===edge.target);
      if (source?.type==='video' && target) target.text=target.text.replace('视频参考：@'+source.ref_name+'。\n\n','');
  }
  if (nid) {
    app.graph.nodes = app.graph.nodes.filter((n) => n.id !== nid);
    app.graph.edges = app.graph.edges.filter(
      (e) => e.source !== nid && e.target !== nid,
    );
    app.selected = null;
  } else {
    app.graph.edges = app.graph.edges.filter((e) => e.id !== app.edge);
    app.edge = null;
  }
  changed();
  renderGraph();
  toast("已删除，可按 ⌘Z 撤销");
}
function undo(redo = false) {
  const src = redo ? app.future : app.history,
    dst = redo ? app.history : app.future;
  if (!src.length) return;
  dst.push(clone(app.graph));
  const old = src.pop(),
    live = new Map(app.graph.nodes.map((n) => [n.id, n]));
  for (const n of old.nodes) {
    if (live.has(n.id)) {
      n.assets = live.get(n.id).assets;
      n.selected = live.get(n.id).selected;
      n.selected_ids = live.get(n.id).selected_ids;
    }
  }
  old.revision = app.graph.revision;
  app.graph = old;
  app.selected = null;
  changed();
  renderGraph();
}
function zoom(value, cx, cy) {
  if (!app.graph) return;
  const r = $("#viewport").getBoundingClientRect();
  cx = cx ?? r.width / 2;
  cy = cy ?? r.height / 2;
  const v = app.graph.viewport,
    newZoom = Math.max(0.15, Math.min(2, value)),
    factor = newZoom / v.zoom;
  v.x = cx - (cx - v.x) * factor;
  v.y = cy - (cy - v.y) * factor;
  v.zoom = newZoom;
  updateViewport();
  changed();
}
function fit() {
  if (!app.graph?.nodes.length) return;
  const ns = app.graph.nodes,
    r = $("#viewport").getBoundingClientRect(),
    minX = Math.min(...ns.map((n) => n.x)),
    minY = Math.min(...ns.map((n) => n.y)),
    maxX = Math.max(...ns.map((n) => n.x + 340)),
    maxY = Math.max(...ns.map((n) => n.y + 390));
  const z = Math.max(
    0.15,
    Math.min(
      1,
      (r.width - 90) / (maxX - minX),
      (r.height - 240) / (maxY - minY),
    ),
  );
  app.graph.viewport = {
    zoom: z,
    x: (r.width - (maxX - minX) * z) / 2 - minX * z,
    y: 170 - minY * z,
  };
  updateViewport();
  changed();
}

function closeMenu() {
  app.menu = false;
  app.nextFrom = null;
  $("#add-menu").hidden = true;
}

function openDrawer(name, title) {
  app.drawer = name;
  $("#drawer-title").textContent = title;
  $("#drawer").hidden = false;
  closeMenu();
}
async function showSource() {
  if (!app.episode) {
    toast("请先选择一集");
    return;
  }
  openDrawer("source", "本集原剧本 · " + currentEp().title);
  $("#drawer-body").innerHTML = '<div class="loading-card">正在读取剧本…</div>';
  try {
    const scope = app.scope,
      d = await api("source", undefined, query());
    if (app.drawer === "source" && scope === app.scope)
      $("#drawer-body").innerHTML =
        '<div class="source-text">' + esc(d.text) + "</div>";
  } catch (e) {
    toast(e.message, true);
  }
}
function renderJobsBadge() {
  const n = app.project?.jobs.filter(busy).length || 0;
  $("#job-count").textContent = n || "";
  $("#jobs-button .status-dot").classList.toggle("busy", n > 0);
}
function showJobs() {
  openDrawer("jobs", "生成任务");
  renderJobs();
}
function renderJobs() {
  if (app.drawer !== "jobs") return;
  const names = {
      initialize: "剧本初始化",
      characters: "共享人物与视觉圣经",
      plan: "AI分镜",
      image: "图片生成",
      video: "视频生成",
    },
    states = {
      queued: "排队中",
      running: "进行中",
      succeeded: "已完成",
      failed: "失败",
      interrupted: "已中断",
    };
  const jobs = [...(app.project?.jobs || [])].reverse().slice(0, 80);
  $("#drawer-body").innerHTML = jobs.length
    ? jobs
        .map(
          (j) =>
            `<div class="job"><div class="job-heading"><strong>${names[j.kind] || esc(j.kind)} ${esc(j.episode_id || "")}</strong><span class="job-state ${j.status}">${states[j.status] || esc(j.status)}</span></div><time>${esc(j.created_at?.replace("T", " ").slice(0, 19))}</time>${j.error ? `<div class="job-error">${esc(j.error)}</div>` : ""}${j.task_id ? `<div class="job-note">云端任务已保存 · ${esc(j.remote_status || "可继续查询")}</div>` : ""}${j.kind === "initialize" && ["failed", "interrupted"].includes(j.status) ? "<button data-reinitialize>重新初始化</button>" : ""}${j.node_id ? `<button data-locate="${esc(j.node_id)}" data-eid="${esc(j.episode_id)}">定位节点 ↗</button>` : ""}</div>`,
        )
        .join("")
    : '<div class="drawer-empty">创作从这里开始。<br>图片、视频和分镜任务会显示在这里。</div>';
}
function showAssets() {
  openDrawer("assets", "本集素材");
  const assets =
    app.graph?.nodes.flatMap((n) =>
      (n.assets || []).map((a) => ({ ...a, title: n.title })),
    ) || [];
  $("#drawer-body").innerHTML =
    '<p class="drawer-note">这里保留本集全部生成版本与上传的参考图。点击节点中的版本菜单可切换采用。</p>' +
    (assets.length
      ? '<div class="asset-list">' +
        assets
          .map(
            (a) =>
              `<div class="asset-tile">${a.kind === "image" ? `<img src="${esc(a.url)}" alt="${esc(a.title)}" loading="lazy">` : `<video src="${esc(a.url)}" controls preload="none"></video>`}<p>${esc(a.title)}<br>${esc(a.created_at?.slice(0, 16).replace("T", " "))}</p><a href="${esc(a.url)}" download>下载素材 ↗</a></div>`,
          )
          .join("") +
        "</div>"
      : '<div class="drawer-empty">本集还没有素材。<br>生成第一张分镜图，或上传参考图。</div>');
}
function downloadJSON(value, name) {
  const blob = new Blob([JSON.stringify(value, null, 2)], {
      type: "application/json",
    }),
    url = URL.createObjectURL(blob),
    a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 500);
}
function exportGraph() {
  if (!app.graph) {
    toast("当前没有可导出的画布");
    return;
  }
  downloadJSON(
    {
      project: { id: app.project.id, title: app.project.title },
      episode: currentEp(),
      graph: app.graph,
    },
    app.project.title + "-" + app.episode + "-canvas.json",
  );
  toast("画布已导出");
}
async function generateNode(nid) {
  try {
    if(app.graph.nodes.find(n=>n.id===nid)?.type==='image'&&CanvasSync.videoReferences(app.graph,nid).length)throw new Error('图片不支持视频参考，请使用视频卡');
    await save();
    const n = app.graph.nodes.find((n) => n.id === nid),
      j = nodeJob(n);
    const resumable =
      j &&
      ["failed", "interrupted"].includes(j.status) &&
      (j.task_id || j.image_url || j.image_urls) &&
      !j.terminal;
    const result = await api("generate", {
      ...query(),
      node_id: nid,
      force_new: !resumable && !!j,
    });
    toast(resumable ? "继续已有任务，不重新提交" : "生成任务已提交");
    await poll();
    refreshNodes();
    return result;
  } catch (e) {
    toast(e.message, true);
  }
}
async function uploadImage(file, nid) {
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) {
    toast("图片最大10MB", true);
    return;
  }
  try {
    await save();
    const context = query(),
      scope = app.scope;
    const data = await asBase64(file);
    await api("upload", { ...context, node_id: nid, data });
    if (scope === app.scope) {
      await syncGraph();
      refreshNodes();
      if (app.drawer === "assets") showAssets();
    }
    toast("参考图已上传，可以连接到视频节点");
  } catch (e) {
    toast(e.message, true);
  }
}
function asBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result.split(",")[1]);
    r.onerror = () => reject(new Error("文件读取失败"));
    r.readAsDataURL(file);
  });
}
function mediaKind(file) {
  if (/\.(png|jpg|jpeg|webp)$/i.test(file.name)) return 'image';
  if (/\.(mp4|mov)$/i.test(file.name)) return 'video';
  throw new Error('支持PNG/JPEG/WebP图片和MP4/MOV视频');
}
async function importMedia(files,targetId=null,promptId=null) {
  if (!files.length) return;
  if (!app.graph) {openProjectForm();return;}
  if (app.uploading) {toast('正在上传，请稍候');return;}
  const scope=app.scope,context=query(),button=$('#upload-media');
  app.uploading=true;button.disabled=true;button.querySelector('span').textContent='上传中…';let connected=0;
  try {
    for (const [index,file] of [...files].entries()) {
      const type=mediaKind(file),limit=(type==='video'?100:10)*1024*1024;
      if(!file.size || file.size>limit)throw new Error(type==='video'?'视频最大100MB':'单张图片最大10MB');
      if(scope!==app.scope)throw new Error('画布已切换，已上传的文件保留在原画布');
      const target=targetId&&app.graph.nodes.find(n=>n.id===targetId),prompt=promptId&&app.graph.nodes.find(n=>n.id===promptId);
      if(target && (target.type!==type||target.shared_node_id))throw new Error('请选择对应的图片卡或视频卡上传');
      const n=target||addNode(type,null,prompt?{x:prompt.x-390,y:prompt.y+index*320}:null);
      await save();
      const response=await fetch('/api/canvas/upload-media?'+new URLSearchParams({...context,node_id:n.id,filename:file.name}),
        {method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});
      const result=await response.json();if(!response.ok)throw new Error(result.error||'上传失败');
      if(scope===app.scope) {
        await syncGraph();refreshNodes();
        if(prompt&&canConnect(app.graph.nodes.find(x=>x.id===n.id),prompt)){connect(n.id,prompt.id);select(prompt.id);connected++;}
      }
    }
    await save();toast(connected===files.length?'已上传并连接参考素材':connected?`已连接${connected}个参考，其余素材已添加到画布`:'素材已上传，可以拖线连接到图片或视频');
  } catch(error) {toast(error.message,true);}
  finally {app.uploading=false;button.disabled=false;button.querySelector('span').textContent='上传图片/视频';}
}
function pickMedia(targetId=null,referenceId=null) {
  if(!app.graph){openProjectForm();return;}
  app.mediaUploadContext={targetId,promptId:referenceId||(!targetId&&!currentNode()?.shared_node_id&&['image','video'].includes(currentNode()?.type)?app.selected:null),scope:app.scope};
  $('#media-file').accept=targetId?'.mp4,.mov':referenceId&&currentNode()?.type==='image'?'.png,.jpg,.jpeg,.webp':'.png,.jpg,.jpeg,.webp,.mp4,.mov';
  $('#media-file').value='';$('#media-file').click();
}
$('#upload-media').onclick=()=>pickMedia();
$('#media-file').onchange=e=>{
  const context=app.mediaUploadContext;
  if(context.scope!==app.scope){toast('画布已切换，请重新选择文件');return;}
  importMedia([...e.target.files],context.targetId,context.promptId);
};
async function syncGraph() {
  if (!app.episode) return;
  const scope = app.scope;
  const g = await api("graph", undefined, query());
  if (scope !== app.scope) return;
  if (!app.dirty && !app.saving && !app.drag && !app.arranging) {
    if (g.revision !== app.graph.revision) {
      if (g.revision<app.graph.revision) return;
      app.baseGraph=clone(g);applySyncedGraph(g);
    } else {
      for (const n of app.graph.nodes) {
        const remote = g.nodes.find((v) => v.id === n.id);
        if (remote) {
          n.assets = remote.assets;
          n.selected = remote.selected;
          n.selected_ids = remote.selected_ids;
          n.missing_master = remote.missing_master;
          n.title = remote.title;
          n.global_prompt = remote.global_prompt;
          n.prompt_revision = remote.prompt_revision;
        }
      }
      app.baseGraph=clone(g);
    }
  }
}
async function poll() {
  if (app.polling || !app.project) return;
  app.polling = true;
  const pid = app.project.id,
    scope = app.scope;
  try {
    const d = await api("project", undefined, { project_id: pid });
    if (pid !== app.project?.id) return;
    const hadBusy = app.project.jobs.filter(busy).length;
    const oldEps = app.project.episodes.length;
    app.project = d;
    renderJobsBadge();
    if (oldEps !== d.episodes.length) {
      renderEpisodes();
      if (!app.episode && d.episodes.length)
        await openEpisode(d.episodes[0].id);
    }
    if (scope === app.scope && app.graph) {
      await syncGraph();
      refreshNodes();
      renderHeading();
      if (!app.dirty && !app.saving) savedLabel("已自动保存");
    }
    if (app.drawer === "jobs") renderJobs();
    maybeInitializeEpisode();
    if (hadBusy && !d.jobs.some(busy)) {
      if (d.jobs.at(-1)?.status === "succeeded") toast("生成完成，结果已保存");
      if (app.drawer === "assets") showAssets();
    }
    if (d.status === "failed" && !app.graph) {
      $("#empty-state h2").textContent = "剧本初始化需要处理";
      $("#empty-state p").textContent = d.error || "打开生成任务查看详情";
      $("#empty-add").textContent = "查看生成任务";
      $("#empty-add").disabled = false;
    }
  } catch (e) {
    savedLabel("连接中断 · 稍后自动重连", true);
  } finally {
    app.polling = false;
  }
}
async function projectsDialog() {
  try {
    await refreshProjects();
    $("#projects-list").innerHTML =
      app.projects
        .map(
          (p) =>
            `<button class="project-row ${p.id===app.project?.id?'current':''}" data-project="${esc(p.id)}"><span class="type-icon ${p.mode==='blank'?'image':'prompt'}">${icon(p.mode==='blank'?'grid':'folder')}</span><span><b>${esc(p.title)}</b><small>${p.mode==='blank'?'自由画布':p.episode_count+' 集'}${p.legacy ? " · 来自现有制作项目" : ""} · ${esc({ ready: "就绪", initializing: "初始化中", failed: "待处理" }[p.status] || p.status)}</small></span>${p.id===app.project?.id?'<small>当前项目</small>':icon("arrow")}</button>`,
        )
        .join("") ||
      '<div class="drawer-empty">创建空白画布，或导入剧本开始创作。</div>';
    $("#projects-dialog").showModal();
  } catch (e) {
    toast(e.message, true);
  }
}
async function locate(nid, eid) {
  if (eid !== app.episode) await openEpisode(eid);
  if (app.graph?.layout_mode === "tidy") await arrangeMeasured(measuredNodes());
  const n = app.graph?.nodes.find((n) => n.id === nid);
  if (!n) {
    toast("节点已删除，记录仍然保留");
    return;
  }
  const r = $("#viewport").getBoundingClientRect(),
    v = app.graph.viewport;
  v.x = r.width * (eid === SHARED ? 0.58 : 0.3) - n.x * v.zoom;
  v.y = r.height * 0.3 - n.y * v.zoom;
  select(nid);
  updateViewport();
  changed();
  $("#drawer").hidden = true;
  app.drawer = null;
}

$("#episode-list").addEventListener("click", (e) => {
  const el = e.target.closest("[data-episode]");
  if (el) openEpisode(el.dataset.episode).catch((e) => toast(e.message, true));
});
$("#episode-search").addEventListener("input", renderEpisodes);
$("#project-switch").onclick = projectsDialog;
$("#new-project").onclick = () => openProjectForm();
$("#projects-new").onclick = () => {
  $("#projects-dialog").close();
  openProjectForm();
};
$("#projects-list").onclick = async (e) => {
  const el = e.target.closest("[data-project]");
  if (el) {
    try {
      await openProject(el.dataset.project);
      $("#projects-dialog").close();
    } catch (e) {
      toast(e.message, true);
    }
  }
};
$$(".dialog-close").forEach(
  (el) => (el.onclick = () => el.closest("dialog").close()),
);
$("#add-button").onclick = projectsDialog;
$("#add-menu").onclick = (e) => {
  const el = e.target.closest("[data-add]");
  if (el) {
    const from =
      app.nextFrom && app.graph.nodes.find((n) => n.id === app.nextFrom);
    addNode(el.dataset.add, from);
    app.nextFrom = null;
  }
};
$("#empty-add").onclick = () => {
  if (app.project?.status === "failed") showJobs();
  else toggleMenu();
};
$("#episodes-button").onclick = () => {
  $("#episodes-panel").hidden = !$("#episodes-panel").hidden;
};
$("#collapse-button").onclick = () => {
  $("#episodes-panel").hidden = !$("#episodes-panel").hidden;
  $("#collapse-button svg").classList.toggle(
    "rotate",
    !$("#episodes-panel").hidden,
  );
};
$("#source-button").onclick = showSource;
$("#assets-button").onclick = showAssets;
$("#jobs-button").onclick = showJobs;
$("#export-button").onclick = exportGraph;
$("#help-button").onclick = () => $("#help-dialog").showModal();
$("#drawer-close").onclick = () => {
  $("#drawer").hidden = true;
  app.drawer = null;
};
$("#drawer-body").onclick = async (e) => {
  const el = e.target.closest("[data-locate]");
  if (el) await locate(el.dataset.locate, el.dataset.eid);
  if (e.target.closest("[data-reinitialize]")) {
    try {
      await api("initialize", { project_id: app.project.id });
      toast("正在重新初始化");
      await poll();
    } catch (e) {
      toast(e.message, true);
    }
  }
};
async function showSaveHistory() {
  if (!app.graph) return;
  const scope=app.scope,items=await api('save-history',undefined,query());
  if (scope!==app.scope) return;
  openDrawer('save-history','保存记录');
  $('#drawer-body').innerHTML='<p class="drawer-note">同一卡片的同一处内容被再次修改时，旧版本会保留在这里。可以复制回Prompt继续编辑。</p>'+
    (items.length?items.map(item=>`<div class="save-history-item"><time>${esc(item.created_at.replace('T',' ').slice(0,19))}</time>${item.fields.filter(f=>!['x','y'].includes(f.field)).map(f=>`<b>${esc(f.title)} · ${esc(f.field==='text'?'Prompt':f.field==='title'?'名称':f.field)}</b><textarea readonly aria-label="${esc(f.title)} 旧版本">${esc(typeof f.previous==='string'?f.previous:JSON.stringify(f.previous))}</textarea>`).join('')}</div>`).join(''):'<div class="drawer-empty">所有修改已自动保存，暂无需要保留的并发旧版本。</div>');
}
$('#save-status').onclick=()=> (app.dirty?save():showSaveHistory()).catch(reportSaveError);
$("#auto-layout").onclick = layout;
$("#fit").onclick = fit;
$("#zoom-in").onclick = () => app.graph && zoom(app.graph.viewport.zoom * 1.15);
$("#zoom-out").onclick = () =>
  app.graph && zoom(app.graph.viewport.zoom / 1.15);
$("#zoom-label").onclick = () => zoom(1);
$("#undo").onclick = () => undo();
$("#redo").onclick = () => undo(true);
function projectMode(mode) {
  $('#project-form [name="mode"][value="'+mode+'"]').checked=true;
  $('#script-fields').hidden=mode==='blank';
  $('#create-project').innerHTML=icon(mode==='blank'?'plus':'spark')+(mode==='blank'?'创建空白项目':'导入并初始化');
}
function openProjectForm(mode='blank') {
  $('#project-error').textContent='';projectMode(mode);$('#project-dialog').showModal();
}
$$('#project-form [name="mode"]').forEach(radio=>radio.addEventListener('change',()=>projectMode(radio.value)));
$("#script-file").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  app.file = null;
  $("#file-info").hidden = true;
  if (file.size > 12 * 1024 * 1024) {
    $("#project-error").textContent = "剧本文件最大12MB";
    return;
  }
  try {
    if (file.name.toLowerCase().endsWith(".docx")) {
      app.file = { filename: file.name, file_data: await asBase64(file) };
      $("#script-input").value = "";
      $("#script-input").placeholder =
        "已选择 Word 剧本。初始化时将提取完整正文。";
      $("#file-info").hidden = false;
      $("#file-info").textContent = "已选择：" + file.name;
    } else {
      $("#script-input").value = await file.text();
    }
    if (!$('#project-form [name="title"]').value)
      $('#project-form [name="title"]').value = file.name.replace(
        /\.[^.]+$/,
        "",
      );
    $("#project-error").textContent = "";
  } catch (e) {
    $("#project-error").textContent = e.message;
  }
};
$("#script-input").addEventListener("input", () => {
  if (app.file) {
    app.file = null;
    $("#file-info").hidden = true;
  }
});
$("#project-form").onsubmit = async (e) => {
  e.preventDefault();
  const button = $("#create-project");
  button.disabled = true;
  $("#project-error").textContent = "";
  try {
    const fields = Object.fromEntries(new FormData(e.target));
    if (fields.mode==='script' && !fields.script.trim() && !app.file) throw new Error('请粘贴剧本或选择剧本文件');
    await save();
    const result = await api("create", { ...fields, ...(fields.mode==='script'&&app.file || {}) });
    await openProject(result.project_id);
    $("#project-dialog").close();
    e.target.reset();
    app.file = null;
    $("#file-info").hidden = true;
    await refreshProjects();
    toast(fields.mode==='blank'?'空白项目已创建，点击＋选择图片或视频':'剧本已导入，正在初始化分集');
    if (fields.mode==='script') showJobs();
  } catch (e) {
    $("#project-error").textContent = e.message;
  } finally {
    button.disabled = false;
  }
};
$("#nodes").addEventListener("focusin", (e) => {
  if (e.target.matches("[data-text]")) {
    checkpoint();
    app.textEditing = e.target.dataset.text;
  }
});
$("#nodes").addEventListener("input", (e) => {
  const nid = e.target.dataset.text;
  if (nid) {
    const n = app.graph.nodes.find((n) => n.id === nid);
    n.text = e.target.value;
    syncPromptHighlight(e.target);
    syncVideoWarning(n);
    const label = e.target.closest(".node").querySelector(".node-label span");
    if (label) label.textContent = n.text.length + " 字";
    changed();
  }
});
$("#nodes").addEventListener("scroll", e => {
  if (e.target.matches(".prompt-editor textarea")) syncPromptHighlight(e.target);
}, true);
$("#nodes").addEventListener("change", async (e) => {
  const nid = e.target.dataset.node;
  if (e.target.dataset.setting && nid) {
    checkpoint();
    const n = app.graph.nodes.find((n) => n.id === nid);
    n.settings[e.target.dataset.setting] = ["duration", "n"].includes(
      e.target.dataset.setting,
    )
      ? Number(e.target.value)
      : e.target.value;
    if (e.target.dataset.setting === "duration" && n.role === "clip_video") {
      const group = app.graph.groups.find((g) => g.video_node_id === n.id);
      const prompt = n;
      const timing = prompt?.text.match(/视频总时长(\d+(?:\.\d+)?)秒/);
      if (timing && Number(timing[1])) {
        const scale = n.settings.duration / Number(timing[1]);
        prompt.text = prompt.text
          .replace(
            /(\d+(?:\.\d+)?)–(\d+(?:\.\d+)?)秒/g,
            (_, a, b) =>
              `${+(Number(a) * scale).toFixed(2)}–${+(Number(b) * scale).toFixed(2)}秒`,
          )
          .replace(
            /视频总时长\d+(?:\.\d+)?秒/,
            `视频总时长${n.settings.duration}秒`,
          );
        const area=$(`[data-text="${n.id}"]`);if(area){area.value=n.text;syncPromptHighlight(area);}
      }
      refreshNodes();
    }
    changed();
  }
  const version = e.target.dataset.version;
  if (version) {
    try {
      await save();
      await api("select", {
        ...query(),
        node_id: version,
        asset_id: e.target.value,
      });
      await syncGraph();
      refreshNodes();
    } catch (e) {
      toast(e.message, true);
    }
  }
});
$("#nodes").addEventListener("click", (e) => {
  const action = e.target.closest(
    "[data-generate],[data-next],[data-delete],[data-upload]",
  );
  if (action) {
    if (action.dataset.generate) generateNode(action.dataset.generate);
    if (action.dataset.next) showNextMenu(action.dataset.next);
    if (action.dataset.delete) deleteSelection(action.dataset.delete);
    if (action.dataset.upload) {
      app.uploadNode = action.dataset.upload;
      $("#image-file").value = "";
      $("#image-file").click();
    }
    return;
  }
  const el = e.target.closest(".node");
  if (el && !e.target.closest(".port")) select(el.dataset.node);
});
$("#image-file").onchange = (e) =>
  uploadMany([...e.target.files], app.uploadNode);
$("#viewport").addEventListener("pointerdown", (e) => {
  if (!app.graph || e.button > 1) return;
  const port = e.target.closest("[data-port]");
  if (port) {
    e.preventDefault();
    if (port.dataset.port === "output") {
      app.link = port.dataset.node;
      app.linkPoint = point(e.clientX, e.clientY);
      app.drag = { kind: "link", startX: e.clientX, startY: e.clientY };
      drawEdges();
    } else if (app.link) connect(app.link, port.dataset.node);
    return;
  }
  const edge = e.target.closest("[data-edge]");
  if (edge) {
    select(null);
    app.edge = edge.dataset.edge;
    drawEdges();
    return;
  }
  const head = e.target.closest("[data-drag]");
  if (
    head &&
    !app.space &&
    e.button !== 1 &&
    !e.target.closest("button,input")
  ) {
    e.preventDefault();
    const n = app.graph.nodes.find((n) => n.id === head.dataset.drag);
    select(n.id);
    app.drag = {
      kind: "node",
      id: n.id,
      startX: e.clientX,
      startY: e.clientY,
      x: n.x,
      y: n.y,
      checkpoint: false,
    };
    return;
  }
  if (!e.target.closest(".node") || app.space || e.button === 1) {
    e.preventDefault();
    if (app.link) {
      app.link = null;
      drawEdges();
    }
    select(null);
    app.drag = {
      kind: "pan",
      startX: e.clientX,
      startY: e.clientY,
      x: app.graph.viewport.x,
      y: app.graph.viewport.y,
    };
    $("#viewport").classList.add("panning");
    closeMenu();
  }
});
window.addEventListener("pointermove", (e) => {
  if (!app.graph) return;
  if (app.link) {
    app.linkPoint = point(e.clientX, e.clientY);
    const target=e.target.closest?.('.node'),
      source=app.graph.nodes.find(n=>n.id===app.link),
      receiver=target && app.graph.nodes.find(n=>n.id===target.dataset.node);
    referenceDropTarget(canConnect(source,receiver) ? target : null);
    drawEdges();
  }
  const d = app.drag;
  if (!d) return;
  const dx = e.clientX - d.startX,
    dy = e.clientY - d.startY;
  if (d.kind === "node") {
    if (!d.checkpoint && Math.hypot(dx, dy) > 2) {
      checkpoint();
      d.checkpoint = true;
    }
    const n = app.graph.nodes.find((n) => n.id === d.id);
    n.x = d.x + dx / app.graph.viewport.zoom;
    n.y = d.y + dy / app.graph.viewport.zoom;
    const el = $(`.node[data-node="${n.id}"]`);
    el.style.left = n.x + "px";
    el.style.top = n.y + "px";
    drawEdges();
  } else if (d.kind === "pan") {
    app.graph.viewport.x = d.x + dx;
    app.graph.viewport.y = d.y + dy;
    updateViewport();
  }
});
window.addEventListener("pointerup", (e) => {
  const d = app.drag;
  if (!d) return;
  app.drag = null;
  $("#viewport").classList.remove("panning");
  if (d.kind === "link") {
    const port = e.target.closest('[data-port="input"]'),
      moved=Math.hypot(e.clientX-d.startX,e.clientY-d.startY)>5,
      card=e.target.closest('.node');
    referenceDropTarget(null);
    if (port && app.link) connect(app.link, port.dataset.node);
    else if (moved && card && app.link && card.dataset.node !== app.link) connect(app.link,card.dataset.node);
    else if (moved) {
      app.link = null;
      drawEdges();
    }
  } else if (d.kind === "pan" || d.checkpoint) {
    if (d.kind === "node") app.graph.layout_mode = "manual";
    changed();
  }
});
$("#viewport").addEventListener(
  "wheel",
  (e) => {
    if (
      !app.graph ||
      e.target.closest("textarea,select") ||
      e.target.closest("video,.node-composer")
    )
      return;
    e.preventDefault();
    const r = $("#viewport").getBoundingClientRect();
    if (e.ctrlKey || e.metaKey)
      zoom(
        app.graph.viewport.zoom * Math.exp(-e.deltaY * 0.007),
        e.clientX - r.left,
        e.clientY - r.top,
      );
    else {
      app.graph.viewport.x -= e.deltaX;
      app.graph.viewport.y -= e.deltaY;
      updateViewport();
      changed();
    }
  },
  { passive: false },
);
$("#nodes").addEventListener('dragstart',e=>{
  const image=e.target.closest('[data-reference-source]');
  if (!image) return;
  const source=app.graph.nodes.find(n=>n.id===image.dataset.referenceSource);
  if (!source) return;
  app.mediaDrag={id:source.id,scope:app.scope};app.link=null;app.drag={kind:'media'};
  e.dataTransfer.setData('application/x-story-canvas-node',source.id);
  e.dataTransfer.setData('text/plain','@'+source.ref_name);
  e.dataTransfer.effectAllowed='link';
});
document.addEventListener('dragend',()=>{app.mediaDrag=null;if(app.drag?.kind==='media')app.drag=null;referenceDropTarget(null);});
$("#viewport").addEventListener("dragover", e=>{
  e.preventDefault();
  if (!app.mediaDrag) return;
  const card=e.target.closest('.node'), source=app.graph.nodes.find(n=>n.id===app.mediaDrag.id),
    target=card && app.graph.nodes.find(n=>n.id===card.dataset.node), valid=canConnect(source,target);
  e.dataTransfer.dropEffect=valid?'link':'none';referenceDropTarget(valid?card:null);
});
$("#viewport").addEventListener("drop", async (e) => {
  e.preventDefault();
  if (app.mediaDrag) {
    e.stopPropagation();
    const drag=app.mediaDrag, card=e.target.closest('.node');
    app.mediaDrag=null;app.drag=null;referenceDropTarget(null);
    if (card && drag.scope===app.scope) connect(drag.id,card.dataset.node);
    return;
  }
  const f = e.dataTransfer.files[0];
  if (!f) return;
  if (/\.(png|jpg|jpeg|webp|mp4|mov)$/i.test(f.name)) {
    const el=e.target.closest('.node'),target=el&&app.graph.nodes.find(n=>n.id===el.dataset.node),files=[...e.dataTransfer.files];
    const recipe=!!e.target.closest('.node-composer'),same=target&&['image','video'].includes(target.type)&&files.every(file=>mediaKind(file)===target.type);
    await importMedia(files,same&&!recipe?target.id:null,recipe?target?.id:null);
  } else if (/\.(txt|md|docx)$/i.test(f.name)) {
    openProjectForm('script');
    $('#project-form [name="title"]').value = f.name.replace(/\.[^.]+$/, "");
    if (/\.docx$/i.test(f.name)) {
      app.file = { filename: f.name, file_data: await asBase64(f) };
      $("#file-info").textContent = "已选择：" + f.name;
      $("#file-info").hidden = false;
    } else {
      $("#script-input").value = await f.text();
    }
  }
});
document.addEventListener("click", (e) => {
  if (app.menu && !e.target.closest("#add-menu,#add-button,#new-prompt,#empty-add,[data-next]"))
    closeMenu();
  if(app.selected&&!e.target.closest('.node,[data-edge],#add-menu,#drawer,dialog,#new-prompt,#upload-media,.free-card,.clip-list'))select(null);
});
document.addEventListener("keydown", (e) => {
  const editing = e.target.matches(
      "input,textarea,select,video,[contenteditable]",
    ),
    dialog = $$("dialog").some((d) => d.open);
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    save()
      .then(() => toast("画布已保存"))
      .catch((e) => toast(e.message, true));
    return;
  }
  if (e.key === "Escape") {
    app.link = null;
    drawEdges();
    closeMenu();
    select(null);
    return;
  }
  if (editing || dialog) return;
  if (e.key === 'Enter' && e.target.matches('.media-card')) {select(e.target.dataset.node);return;}
  if (e.code === "Space") {
    e.preventDefault();
    app.space = true;
    $("#viewport").classList.add("space");
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") {
    e.preventDefault();
    undo(e.shiftKey);
    return;
  }
  if (e.key === "Delete" || e.key === "Backspace") {
    e.preventDefault();
    deleteSelection();
  }
  if (e.key.toLowerCase() === "n") toggleMenu();
  if (e.key.toLowerCase() === "f") fit();
});
document.addEventListener("keyup", (e) => {
  if (e.code === "Space") {
    app.space = false;
    $("#viewport").classList.remove("space");
  }
});
window.addEventListener("blur", () => {
  app.space = false;
  $("#viewport").classList.remove("space");
});
window.addEventListener("beforeunload", (e) => {
  if (app.dirty || app.saving) {
    backup();
    e.preventDefault();
    e.returnValue = "";
  }
});
window.addEventListener('online',()=>{if(app.dirty)save().catch(reportSaveError);else poll();});
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'&&app.dirty)save().catch(reportSaveError);});
async function boot() {
  if (window.matchMedia("(max-width:850px)").matches)
    $("#episodes-panel").hidden = true;
  try {
    await refreshProjects();
    const remembered = storage.get("story-canvas:project"),
      pid =
        app.projects.find((p) => p.id === remembered)?.id ||
        app.projects.find((p) => p.legacy)?.id ||
        app.projects[0]?.id;
    if (pid) await openProject(pid);
    else {
      renderGraph();
      savedLabel("本地服务已连接");
      openProjectForm();
    }
  } catch (e) {
    savedLabel("连接失败", true);
    toast("无法连接画布服务：" + e.message, true);
  }
  setInterval(poll, 2500);
}
boot();
$("#nodes").addEventListener("click", (e) => {
  const full=e.target.closest('[data-fullscreen]');
  if (full) {
    const asset=selectedAsset(app.graph.nodes.find(n=>n.id===full.dataset.fullscreen));
    $("#media-full").src = asset.url;
    $("#media-full").alt = '图片预览';
    $("#media-dialog").showModal();
  }
});
$("#media-dialog").addEventListener("click", (e) => {
  if (e.target === $("#media-dialog")) $("#media-dialog").close();
});

// Studio v2: shared cast, video segments and a single reference-aware composer.
const SHARED = "__characters__";
const selectedAssets = (n) => {
  const ids = n.selected_ids?.length
    ? n.selected_ids
    : n.selected
      ? [n.selected]
      : [];
  return ids.map((aid) => n.assets?.find((a) => a.id === aid)).filter(Boolean);
};
function promptInputs(n) {
  const byId=new Map(app.graph.nodes.map(node=>[node.id,node])),
    incoming=new Map(app.graph.nodes.map(node=>[node.id,[]])),
    prompts=[], media=[], visited=new Set([n.id]);
  for (const edge of app.graph.edges) incoming.get(edge.target)?.push(edge.source);
  function walk(id) {
    if (visited.has(id)) return;
    visited.add(id);
    const source=byId.get(id);
    if (!source) return;
    if (source.type === "prompt") {
      for (const parent of incoming.get(id) || []) walk(parent);
      prompts.push(source);
    } else media.push(source);
  }
  for (const id of incoming.get(n.id) || []) walk(id);
  return {prompts,media};
}
function refsFor(n) {
  const {prompts,media}=promptInputs(n),
    text=[...prompts.map(p=>p.text),n.text].join("\n"),
    names=new Map(), result=[];
  for (const source of app.graph.nodes.filter(p=>p.type !== "prompt")) {
    names.set(source.ref_name,source);
    if (source.type === "image" && /^图\d+$/.test(source.ref_name))
      names.set("图片"+source.ref_name.slice(1),source);
    if (source.title) names.set(source.title,source);
  }
  if (names.size) {
    const parts=[...names.keys()].sort((a,b)=>b.length-a.length).map(name=>name.replace(/[.*+?^${}()|[\]\\]/g,"\\$&"));
    for (const match of text.matchAll(new RegExp("@("+parts.join("|")+")","g"))) {
      const source=names.get(match[1]);
      if (source.id !== n.id && !result.includes(source)) result.push(source);
    }
  }
  for (const source of media) if (source.id !== n.id && !result.includes(source)) result.push(source);
  return result;
}

function newNode(type, position) {
  const prefix = { prompt: "提示词", image: "图", video: "视频" }[type],
    used = new Set(app.graph?.nodes.map((n) => n.ref_name));
  let index = 1;
  while (used.has(prefix + index)) index++;
  const n = {
    id: id("n_"),
    type,
    x: position.x,
    y: position.y,
    title: cardName(type),
    ref_name: prefix + index,
    text: "",
    settings: {
      ratio: app.project?.ratio || "9:16",
      duration: 5,
      resolution: "720P",
      model: type === "video" ? "auto" : "happyhorse",
      mode: "auto",
      n: 1,
    },
    assets: [],
    selected: null,
  };
  if (type === "image" && app.episode === SHARED) {
    n.character_id = n.id;
    n.role = "character";
  }
  return n;
}
function connect(source, target) {
  if (source === target) return;
  const s = app.graph.nodes.find((n) => n.id === source),
    t = app.graph.nodes.find((n) => n.id === target);
  referenceDropTarget(null);
  if (!canConnect(s,t)) {
    toast(t?.type==='image'?'图片生成不能参考视频，请连接到视频卡':t?.shared_node_id?'共享人物请在人物画布编辑':'请将参考素材连接到图片或视频卡',true);
    return;
  }
  const reaches = (from, to, visited = new Set()) => {
    if (from === to) return true;
    if (visited.has(from)) return false;
    visited.add(from);
    return app.graph.edges
      .filter((e) => e.source === from)
      .some((e) => reaches(e.target, to, visited));
  };
  if (reaches(target, source)) {
    toast("这条连线会形成循环", true);
    return;
  }
  if (app.graph.edges.some((e) => e.source === source && e.target === target)) {
    app.link = null;
    drawEdges();
    return;
  }
  checkpoint();
  app.graph.edges.push({ id: id("e_"), source, target });
  app.graph.layout_mode = "manual";
  app.link = null;
  app.linkPoint = null;
  changed();
  refreshNodes();
  drawEdges();
  toast("已连接 · 参考素材按连线顺序编号");
}
function nodeHTML(n) {
  if(n.type==='prompt')return '';
  const type=n.type,a=selectedAsset(n),job=nodeJob(n),running=busy(job),failed=job&&['failed','interrupted'].includes(job.status),
    resumable=failed&&(job.task_id||job.image_urls||job.image_url)&&!job.terminal,
    shared=!!n.shared_node_id,refs=refsFor(n),opts=n.settings||{},pending=!!app.graph.composer_pending,
    videoInput=CanvasSync.videoReferences(app.graph,n.id).length>0,imageBlocked=type==='image'&&videoInput,
    imageCount=refs.filter(r=>r.type==='image').reduce((sum,r)=>sum+Math.max(1,selectedAssets(r).length),0),
    videoCount=refs.filter(r=>r.type==='video').reduce((sum,r)=>sum+Math.max(1,selectedAssets(r).length),0),
    wanReference=opts.model==='wan_ref'||opts.model==='auto'&&videoCount>0&&(refs.some(r=>r.type==='video'&&selectedAssets(r).some(a=>a.uploaded&&a.path))||videoCount>1||opts.mode==='reference');
  const options = (values, current) =>
    values
      .map(
        ([v, t]) =>
          `<option value="${v}" ${String(current) === String(v) ? "selected" : ""}>${t}</option>`,
      )
      .join("");
  const field = (key, label, values, current) =>
    `<select data-setting="${key}" data-node="${n.id}" ${shared||pending?'disabled':''} aria-label="${esc(cardLabel(n))} ${label}">${options(values, current)}</select>`;
    const ratio = field(
      "ratio",
      "画幅",
      [
        ["9:16", "9:16 竖屏"],
        ["16:9", "16:9 横屏"],
        ["1:1", "1:1 方形"],
      ],
      opts.ratio || "9:16",
    );
    const minDuration=wanReference || opts.model==='wan' ? 2 : 3,
      maxDuration=wanReference ? (videoCount ? 10 : 15) : opts.model==='wan' ? 30 : 15,
      durations=Array.from({length:maxDuration-minDuration+1},(_,i)=>[i+minDuration,(i+minDuration)+' 秒']);
    if (opts.duration && (opts.duration<minDuration || opts.duration>maxDuration))
      durations.push([opts.duration,opts.duration+' 秒 · 请调整至支持范围']);
    const controls =
      type === "image"
        ? ratio +
          field(
            "n",
            "数量",
            [
              [1, "1 张"],
              [2, "2 张"],
              [3, "3 张"],
              [4, "4 张"],
              [6, "6 张"],
            ],
            opts.n || 1,
          )
        : ratio +
          field(
            "duration",
            "时长",
            durations,
            opts.duration || 5,
          ) +
          field(
            "resolution",
            "分辨率",
            [
              ["480P", "480P"],
              ["720P", "720P"],
              ["1080P", "1080P"],
            ],
            opts.resolution || "720P",
          );
    const model =
      type === "image"
        ? "<b>Qwen Image 3.0 Pro · 多图编辑</b>"
        : field(
            "model",
            "模型",
            [
              ["auto", "自动选择模型"],
              ["happyhorse", "HappyHorse"],
              ["wan", "Wan 3.0"],
              ["wan_ref", "Wan 2.7 · 多视频参考"],
            ],
            opts.model || "happyhorse",
          ) +
          field(
            "mode",
            "生成模式",
            [
              ["auto", "自动识别输入"],
              ["reference", "多图参考"],
              ["first_frame", "首帧"],
              ["frames", "首尾帧 · Wan"],
              ["edit", "视频编辑"],
            ],
            opts.mode || "auto",
          );

  const recipe=shared?n.global_prompt||'':pending?[...promptInputs(n).prompts.map(p=>p.text),n.text].filter(Boolean).join('\n\n'):n.text||'';
  const referenceChips=refs.map(r=>{const asset=selectedAsset(r);return `<button class="reference-chip" data-mention="${esc(r.ref_name)}" data-target="${n.id}" ${shared||pending?'disabled':''} title="插入@${esc(r.ref_name)}">${asset&&r.type==='image'?`<img src="${esc(asset.url)}" alt="">`:icon(r.type)}<span>@${esc(r.ref_name)}</span></button>`;}).join('');
  const media=a?(type==='image'?`<img src="${esc(a.url)}" alt="${esc(n.title)}" draggable="true" data-reference-source="${n.id}" title="点击编辑Prompt；可拖到其他卡片作为参考">`:`<video src="${esc(a.url)}" controls preload="metadata" playsinline draggable="true" data-reference-source="${n.id}"></video>`):`<div class="node-placeholder">${icon(type)}<span>${type==='image'?'点击编写Prompt，生成图片':'点击编写Prompt，生成视频'}</span></div>`;
  const count=(n.assets?.length||0)>1?`<span class="media-count">${icon(type)} ${n.assets.length}</span>`:'';
  const versions=type==='image'&&n.assets?.length>1?`<div class="image-selection">${n.assets.map((asset,i)=>`<label title="参考版本${i+1}"><input type="checkbox" data-output="${n.id}" ${shared||pending?'disabled':''} value="${asset.id}" ${selectedAssets(n).some(a=>a.id===asset.id)?'checked':''}><img src="${esc(asset.url)}" alt="版本${i+1}"><span>${i+1}</span></label>`).join('')}</div>`:type==='video'&&n.assets?.length>1?`<label class="version-picker">采用版本<select data-version="${n.id}" ${shared||pending?'disabled':''}>${n.assets.map((asset,i)=>`<option value="${asset.id}" ${asset.id===n.selected?'selected':''}>版本${i+1}</option>`).join('')}</select></label>`:'';
  const generate=shared?`<button class="generate" data-global="${esc(n.shared_node_id)}">在人物画布编辑 / 生成 ↗</button>`:`<button class="generate" data-generate="${n.id}" ${running||imageBlocked||pending?'disabled':''}>${running?'<span class="spinner"></span> 正在生成…':icon('spark')+' '+(resumable?'继续已有任务':a||failed?'重新生成':type==='image'?'生成图片':'生成视频')}</button>`;
  const editor=`<section class="node-composer" aria-label="${esc(cardLabel(n))} Prompt与参数"><div class="composer-head"><b>Prompt</b><button data-close-composer="${n.id}" aria-label="收起Prompt">${icon('close')}</button></div><div class="composer-references">${referenceChips}${!shared&&!pending?`<button class="reference-add" data-pick-reference="${n.id}" title="上传参考图片或视频">${icon('plus')} 添加参考</button><button data-refs="${n.id}">@ 引用素材</button>`:''}</div><div class="prompt-editor"><div class="prompt-highlight" aria-hidden="true"><div class="prompt-highlight-text"></div></div><textarea class="node-text unified" ${shared||pending?'readonly':`data-text="${n.id}"`} aria-label="${esc(cardLabel(n))} Prompt" spellcheck="false" placeholder="描述画面、人物、动作和摄影，输入@引用素材…">${esc(recipe)}</textarea></div>${imageCount||videoCount?`<div class="input-summary">${imageCount} 张图片 · ${videoCount} 段视频</div>`:''}${type==='image'?`<p class="input-warning" data-video-warning ${imageBlocked?'':'hidden'}>已接入视频参考，请使用视频卡生成。</p>`:''}${pending?'<p class="input-warning">正在完成导演初始化，完成后可编辑。</p>':''}<div class="recipe-controls ${shared||pending?'recipe-readonly':''}"><div class="node-settings">${controls}</div><div class="node-model">${model}</div>${!shared&&type==='image'&&a?`<label class="edit-current"><input type="checkbox" data-edit-current="${n.id}" ${opts.use_current?'checked':''}>以当前图片编辑</label>`:''}${versions}</div>${generate}<div class="node-bottom">${!shared?`<button data-preview="${n.id}">查看生成方案</button>${type==='image'?`<button data-upload="${n.id}">${icon('upload')} 上传图片</button>`:`<button data-upload-video="${n.id}">${icon('upload')} 上传视频</button><button data-video-url="${n.id}">视频URL</button>`}`:''}${a?`<a href="${esc(a.url)}" download title="下载素材">${icon('download')}</a>`:''}</div>${failed?`<p class="node-error">${esc(job.error||'任务中断')}</p>`:''}</section>`;
  return `<article class="node media-card ${type} ${shared?'shared-node':''} ${app.selected===n.id?'selected':''}" tabindex="0" data-node="${n.id}" data-clip-id="${esc(n.clip_id||'')}" style="left:${n.x}px;top:${n.y}px" aria-label="${esc(cardLabel(n))}"><div class="node-header" data-drag="${n.id}"><span class="type-icon ${type}">${icon(type)}</span><span class="node-title">${type==='image'?'图片':'视频'}</span>${shared||n.role==='character'?`<span class="tile-caption">${esc(n.title)}</span>`:''}<span class="node-alias">@${esc(n.ref_name)}</span><button class="node-more" data-delete="${n.id}" aria-label="删除${esc(cardLabel(n))}">×</button></div><div class="node-preview">${media}${count}${running?'<span class="tile-status">生成中…</span>':''}${!a?'<span class="edit-indicator">点击编辑</span>':''}</div>${editor}${!shared?`<button class="port input" data-port="input" data-node="${n.id}" aria-label="${esc(cardLabel(n))} 输入连接点"></button>`:''}<button class="port output" data-port="output" data-node="${n.id}" aria-label="${esc(cardLabel(n))} 输出连接点"></button><button class="quick-next" data-next="${n.id}" aria-label="连接下一张卡片">+</button>${type==='image'&&a?`<button class="tile-expand" data-fullscreen="${n.id}" title="放大查看">${icon('fit')}</button>`:''}</article>`;
}
function renderGroups() {
  const cast=app.graph?.cast_group;
  const castHTML=cast?`<section class="clip-group cast-group" data-cast-group style="left:${cast.x}px;top:${cast.y}px;width:${cast.width}px;height:${cast.height}px"><div class="clip-group-title"><span class="clip-number">CAST</span><strong>${esc(cast.title)}</strong><span>${app.graph.nodes.filter(n=>app.episode===SHARED?n.type==='image':n.shared_node_id).length} 位人物</span>${app.episode!==SHARED?'<button data-open-shared>共享人物 ↗</button>':''}</div><p>点击图片编辑人物设定，采用图全项目共享。</p></section>`:'';
  $('#groups').innerHTML=castHTML+(app.graph?.groups||[]).map((g,i)=>`<section class="clip-group ${app.activeClip===g.id?'current':''}" data-group="${esc(g.id)}" style="left:${g.x}px;top:${g.y}px;width:${g.width}px;height:${g.height}px"><div class="clip-group-title"><span class="clip-number">VIDEO ${String(i+1).padStart(2,'0')}</span><strong>${esc(g.title)}</strong><span>${g.duration} 秒 · ${g.shot_count??g.shot_image_ids?.length??0} 个分镜 → 1 段视频</span><button data-add-shot="${esc(g.id)}">＋ 添加分镜</button><button data-clip-overview="${esc(g.id)}">总览</button><button data-clip-focus="${esc(g.id)}">定位 ↗</button></div><p>${esc(g.intent||'将分镜图片连入视频，点击卡片编辑Prompt和参数。')}</p></section>`).join('');
}
function renderEpisodes() {
  const search=$('#episode-search').value.toLowerCase(),eps=app.project?.episodes||[],blank=app.project?.mode==='blank',shared=app.episode===SHARED;
  $('#episode-total').textContent=eps.length;$('#panel-heading').textContent=blank?'项目画布':'剧集画布';$('#episode-search').placeholder=blank?'搜索画布…':'搜索剧集…';
  $('#characters-button').classList.toggle('active',shared);$('#episodes-button').classList.toggle('active',!shared);
  let html=`<button class="shared-entry ${shared?'active':''}" data-open-shared>${icon('image')}<span><b>共享人物画布</b><small>全项目统一身份与采用版本</small></span><span>↗</span></button><div class="episode-section-label">${blank?'CANVASES':'EPISODES'} <span>${eps.length} ${blank?'张画布':'集'}</span></div>`;
  for(const ep of eps.filter(e=>[e.id,e.title,e.number].join(' ').toLowerCase().includes(search))){
    const active=ep.id===app.episode,groups=active?app.graph?.groups||[]:[];
    html+=`<button class="episode-item ${active?'active':''}" data-episode="${ep.id}"><span class="episode-num">${String(ep.number||1).padStart(2,'0')}</span><span class="episode-item-text"><b>${esc(ep.title)}</b><small>${blank?(active?app.graph.nodes.filter(n=>n.type!=='prompt').length+' 张卡片':'自由创作'):active?app.graph.nodes.filter(n=>n.type==='video').length+' 个视频':ep.summary?'已保留本集原剧本':'点击初始化导演分镜'}</small></span><span class="ep-dot"></span></button>`;
    if(active){
      html+=`<div class="clip-list">${groups.map((g,i)=>`<button data-select-clip="${esc(g.id)}" class="clip-item ${app.activeClip===g.id?'active':''}"><span>${String(i+1).padStart(2,'0')}</span><b>${esc(g.title)}</b><small>${g.duration}s</small></button>`).join('')}<button class="add-clip" data-add-clip>＋ 新增视频段落</button></div>`;
      const free=app.graph.nodes.filter(n=>n.type!=='prompt'&&!n.shared_node_id&&!n.clip_id);
      if(free.length)html+=`<div class="free-cards"><div class="episode-section-label">自由卡片 <span>${free.length}</span></div>${free.map(n=>`<button class="free-card ${app.selected===n.id?'active':''}" data-free-node="${n.id}">${icon(n.type)}<span>${cardName(n.type)}</span><small>@${esc(n.ref_name)}</small></button>`).join('')}</div>`;
    }
  }
  if(!eps.length)html+='<div class="empty-project">正在保留剧本、切分剧集与提取人物…</div>';
  $('#episode-list').innerHTML=html;
}
function renderHeading() {
  const shared = app.episode === SHARED,
    ep = currentEp(),blank=app.project?.mode==='blank';
  $('#canvas-kind').textContent=blank?'自由画布':'剧集画布';
  $("#episode-title").textContent = ep?.title || "让故事，在这里发生。";
  $("#episode-label").textContent = shared
    ? "PROJECT CAST"
    : ep
      ? blank?'CANVAS 01':`EP ${String(ep.number || 1).padStart(2, "0")}`
      : "选择一集";
  $("#episode-description").textContent = shared
    ? "点击人物图片编辑Prompt，参考图可改表情、动作和服装，采用版本全项目共享。"
    : ep?.summary || "人物共享 · 视频分段 · 多分镜导演规划";
  $("#node-count").textContent = app.graph
    ? blank&&!shared?app.graph.nodes.length+' 个节点':shared
      ? app.graph.nodes.filter((n) => n.type === "image").length + " 位人物"
      : app.graph.nodes.filter(n=>n.type==='video').length + " 段视频"
    : "";
  const active = app.project?.jobs.some(
    (j) =>
      j.episode_id === app.episode &&
      ["plan", "characters"].includes(j.kind) &&
      busy(j),
  );
  const populated = !!app.graph?.nodes.some((n) => !n.shared_node_id);
  $("#plan-button").textContent = active ? "导演规划中…" : shared ? "初始化共享人物" : populated ? "重排多分镜视频" : "导演初始化";
  $("#plan-button").disabled =
    !ep || (shared && populated) || active;
  $('#plan-button').hidden=blank;
  $("#plan-history").hidden = !ep || shared || blank;
  $("#focus-cast").hidden = !app.graph?.cast_group || shared;
  $("#auto-layout").disabled = !app.graph?.nodes.length;
}
function normalizePromptCards() {
  if(!app.graph||app.graph.composer_pending||!app.graph.nodes.some(n=>n.type==='prompt'))return false;
  app.graph=CanvasCompose.embed(app.graph);return true;
}
function renderGraph() {
  if (app.graph) app.graph.nodes.forEach((n) => (n.text ??= ""));
  if (normalizePromptCards()) changed();
  prepareImageMentions();
  $("#nodes").innerHTML = app.graph?.nodes.map(nodeHTML).join("") || "";
  setupPromptHighlights();
  $("#empty-state").hidden = !!app.graph?.nodes.length;
  $("#empty-add").disabled = !app.episode;
  $("#empty-state h2").textContent =
    app.episode === SHARED
      ? "先给故事中的人，一张面孔。"
      : app.project?.mode==='blank'?'从一个想法开始。':"让导演，把故事变成镜头。";
  $("#empty-state p").innerHTML =
    app.episode === SHARED
      ? "共享人物只在这里编辑生成，每一集会引用同一个采用版本。"
      : app.project?.mode==='blank'?'点击右上方「＋」创建图片或视频。<br>点击卡片，展开Prompt、参考素材和生成参数。':"初始化会组织本集图片和视频。<br>也可以点击右上方＋，自由创作。";
  $('#empty-add').hidden=app.project?.mode==='blank';
  $("#empty-add").innerHTML = icon("plus") + "创建图片或视频";
  renderGroups();
  updateViewport();
  renderHeading();
  drawEdges();
  renderHistory();
  renderEpisodes();
  scheduleLayoutMeasure();
}
function refreshNodes() {
  if (!app.graph || app.mediaDrag) return;
  if (normalizePromptCards()) {changed();renderGraph();return;}
  prepareImageMentions();
  for (const n of app.graph.nodes) {
    const el = $(`.node[data-node="${n.id}"]`);
    if (!el) continue;
    const j = nodeJob(n),
      sig = JSON.stringify([
        n.selected,
        n.selected_ids,
        n.title,
        n.text,
        n.assets?.length,
        j?.status,
        j?.error,
        j?.remote_status,
        n.missing_master,
        n.global_prompt,
        n.prompt_revision,
        n.settings,
        promptInputs(n).prompts.map(p=>[p.id,p.text]),
        refsFor(n).map(r=>[r.id,r.title,r.selected,r.selected_ids,r.assets?.length]),
        app.graph.edges.filter((e) => e.target === n.id),
      ]);
    if (
      el.dataset.signature !== sig &&
      !el.contains(document.activeElement) &&
      ![...el.querySelectorAll("video")].some((v) => !v.paused)
    ) {
      el.outerHTML = nodeHTML(n);
      $(`.node[data-node="${n.id}"]`).dataset.signature = sig;
    }
  }
  setupPromptHighlights();
  positionComposer();
  renderGroups();
  drawEdges();
  scheduleLayoutMeasure();
}
async function openEpisode(eid) {
  if (!app.project) return;
  await save();
  app.scope++;
  app.activeClip = null;
  app.episode = eid;
  app.selected = null;
  app.edge = null;
  app.link = null;
  app.history = [];
  app.future = [];
  app.legacyRecovery = false;
  const scope = app.scope,
    g = await api("graph", undefined, query());
  if (scope !== app.scope) return;
  app.graph = g;
  app.baseGraph=clone(g);
  const draft = storage.get(draftKey());
  if (draft) {
    try {
      const parsed = JSON.parse(draft);
      if (parsed.format===2 && parsed.base) {
        app.graph=rebaseGraph(g,parsed.graph,parsed.base);
        app.legacyRecovery=!!parsed.recover_legacy;
      } else {
        const old=parsed.graph||parsed;
        app.graph=CanvasSync.recoverLegacy(g,g.composer_version?CanvasCompose.embed(old):old);
        app.legacyRecovery=true;
      }
      if (!CanvasSync.equal(CanvasSync.snapshot(app.graph),CanvasSync.snapshot(g))) {
        changed();
        toast("已恢复未保存草稿，正在同步保存");
      } else storage.remove(draftKey());
    } catch {}
  }
  storage.set("story-canvas:episode:" + app.project.id, eid);
  renderGraph();
  savedLabel(app.dirty ? "正在保存…" : "已自动保存");
  if (app.drawer === "source" && eid !== SHARED) showSource();
  if (app.drawer === "assets") showAssets();
  if (
    !app.project.characters_initialized &&
    !app.project.jobs.some((j) => j.kind === "characters") &&
    app.project.status === "ready"
  ) {
    api("prepare", { project_id: app.project.id })
      .then(() => poll())
      .catch((e) => toast(e.message, true));
  }
  maybeInitializeEpisode();
}
function maybeInitializeEpisode() {
  if (
    !app.project?.characters_initialized ||
    app.project?.mode==='blank' ||
    !app.episode ||
    app.episode === SHARED ||
    !app.graph ||
    app.graph.nodes.some((n) => !n.shared_node_id) ||
    app.dirty ||
    app.project.jobs.some(
      (j) => j.kind === "plan" && j.episode_id === app.episode,
    ) ||
    app.initializingEpisode === app.episode
  )
    return;
  app.initializingEpisode = app.episode;
  api("plan", query())
    .then(() => {
      toast("正在初始化本集视频段落与导演分镜");
      return poll();
    })
    .catch((e) => toast(e.message, true))
    .finally(() => (app.initializingEpisode = null));
}
function focusClip(cid, overview = false) {
  const g = app.graph?.groups?.find((g) => g.id === cid);
  if (!g) return;
  app.activeClip = cid;
  const r = $("#viewport").getBoundingClientRect(),
    z = overview ? Math.max(.15, Math.min(.8, (r.width-70)/g.width, (r.height-220)/g.height)) : Math.min(0.8, Math.max(0.35, (r.width - 70) / (g.width || 1585)));
  app.graph.viewport = { x: (overview ? (r.width-g.width*z)/2 : 25) - g.x * z, y: 170 - g.y * z, zoom: z };
  updateViewport();
  renderGroups();
  renderEpisodes();
  changed();
}
$("#focus-cast").onclick = async () => {
  try {
    if (app.graph?.layout_mode === "tidy") await arrangeMeasured(measuredNodes());
    const g = app.graph?.cast_group;
    if (!g) return;
    const r = $("#viewport").getBoundingClientRect(),
      z = Math.max(.15, Math.min(.8, (r.width - 70) / g.width));
    app.graph.viewport = {x:25-g.x*z, y:170-g.y*z, zoom:z};
    updateViewport(); changed();
  } catch (e) { toast(e.message, true); }
};
function addClip() {
  if (!app.graph || app.episode === SHARED) return;
  checkpoint();
  const groups = app.graph.groups || [],
    y = groups.length
      ? Math.max(...groups.map((g) => g.y + g.height)) + 120
      : 650,
    cid = id(app.episode + "-V");
  app.activeClip = cid;
  const v = newNode("video", { x: 63, y: y + 100 });
  v.title = "新视频段落";
  v.clip_id = cid;
  app.graph.nodes.push(v);
  app.graph.groups ??= [];
  app.graph.groups.push({
    id: cid,
    title: v.title,
    duration: 5,
    video_node_id: v.id,
    x: 35,
    y,
    width: 430,
    height: 390,
    manual: true,
    shot_image_ids: [],
  });
  changed();
  renderGraph();
  focusClip(cid);
  select(v.id);
}
function measuredNodes() {
  return Object.fromEntries($$("#nodes > .node").map(el =>
    [el.dataset.node, {width: el.offsetWidth, height: el.offsetHeight}]));
}
function scheduleLayoutMeasure() {
  cancelAnimationFrame(app.measureFrame);
  app.measureFrame = requestAnimationFrame(() => {
    if (!app.graph || !["tidy", "manual"].includes(app.graph.layout_mode) || app.arranging ||
        app.drag || app.dirty || app.saving ||
        $("#nodes").contains(document.activeElement)) return;
    const sizes = measuredNodes();
    if (Object.entries(sizes).some(([id, s]) =>
        !app.graph.node_sizes?.[id] ||
        Math.abs(app.graph.node_sizes[id].height - s.height) > 2 ||
        Math.abs(app.graph.node_sizes[id].width - s.width) > 2))
      arrangeMeasured(sizes, app.graph.layout_mode).catch(() => {});
  });
}
async function arrangeMeasured(sizes, mode = "tidy") {
  if (app.arranging) return app.arranging;
  await save();
  if (app.arranging) return app.arranging;
  const scope = app.scope, context = query(),submitted=clone(app.graph);
  app.arranging = (async () => {
    let g;
    try {g=await api('layout',{...context,revision:submitted.revision,sizes,mode});}
    catch(error) {
      if (!error.message.includes('画布已更新')) throw error;
      const latest=await api('graph',undefined,context);
      g=await api('layout',{...context,revision:latest.revision,sizes,mode});
    }
    if (scope !== app.scope) return;
    const pending=rebaseGraph(g,app.graph,submitted);
    app.baseGraph=clone(g);applySyncedGraph(pending);
    if (app.dirty) backup();
  })();
  try { await app.arranging; } finally { app.arranging = null; }
}
async function layout() {
  if (!app.graph?.nodes.length) return;
  try {
    checkpoint();
    if (app.arranging) await app.arranging;
    await arrangeMeasured(measuredNodes());
    toast("已整理人物与全部分镜，卡片完整放入所属框内");
  } catch (e) { toast(e.message, true); }
}
async function addShot(cid) {
  const group = app.graph?.groups.find(g => g.id === cid);
  if (!group) return;
  if (app.graph.nodes.filter(n => n.clip_id === cid && n.type === "image").length >= 9) {
    toast("单段视频最多9张分镜参考，可新增视频段落继续编排", true);
    return;
  }
  checkpoint();
  const sid = id(cid + "-S"),
    count = app.graph.nodes.filter(n => n.clip_id === cid && n.type === "image").length + 1,
    im = newNode("image", {x: 63, y: group.y + group.height});
  Object.assign(im, {clip_id: cid, shot_id: sid, role: "shot_image", title: `分镜${String(count).padStart(2, "0")} · 画面`});
  app.graph.nodes.push(im);
  const video = app.graph.nodes.find(n => n.id === group.video_node_id);
  if (video) {
    app.graph.edges.push({id: id("e_"), source: im.id, target: video.id});
    if (["auto", "first_frame"].includes(video.settings.mode)) video.settings.mode = "reference";
  }
  app.graph.layout_mode = "tidy";
  changed(); renderGraph();
  try {
    await arrangeMeasured(measuredNodes());
    focusClip(cid);
    select(im.id);
    toast("已添加分镜图片，点击图片或视频编辑各自的Prompt");
  } catch (e) { toast(e.message, true); }
}
async function showPlanHistory() {
  try {
    const scope = app.scope, items = await api("plan-history", undefined, query());
    if (scope !== app.scope) return;
    openDrawer("plan-history", "规划历史 · 素材保留");
    $("#drawer-body").innerHTML = '<p class="drawer-note">重排会保留整张旧画布、连线、提示词与已生成素材。恢复时也会先保存当前画布。</p>' +
      (items.length ? items.toReversed().map(item => `<div class="plan-history-item"><b>${esc(item.reason)}</b><p>${esc(item.created_at)} · ${item.clip_count} 段视频 · ${item.node_count} 节点</p><button class="glass" data-restore-plan="${item.id}">恢复这版画布</button></div>`).join("") : '<p class="drawer-note">暂无历史。使用「重排多分镜视频」后自动保存旧版。</p>');
  } catch (e) { toast(e.message, true); }
}
function showReferences(nid) {
  app.referenceTarget = nid;
  openDrawer("references", "引用图片与视频");
  const media = app.graph.nodes.filter(
    (n) => n.id !== nid && n.type !== "prompt" && canConnect(n,app.graph.nodes.find(target=>target.id===nid)),
  );
  $("#drawer-body").innerHTML =
    '<p class="drawer-note">点击插入 @ 引用。实际生成按采用版本解析；多图节点会展开已勾选的所有图片。</p>' +
    media
      .map((n) => {
        const a = selectedAsset(n);
        return `<button class="reference-row" data-insert-ref="${esc(n.ref_name)}">${a && n.type === "image" ? `<img src="${esc(a.url)}" alt="">` : icon(n.type)}<span><b>@${esc(n.ref_name)} · ${esc(n.shared_node_id || n.role==='character'?n.title:cardName(n.type))}</b><small>${selectedAssets(n).length ? "已采用 " + selectedAssets(n).length + " 个素材" : "尚未生成"}</small></span></button>`;
      })
      .join("");
}
function insertReference(nid, name) {
  const n = app.graph.nodes.find((n) => n.id === nid);
  if (!n) return;
  checkpoint();
  const el = $(`[data-text="${nid}"]`),
    pos = el?.selectionStart ?? n.text.length;
  n.text = n.text.slice(0, pos) + "@" + name + " " + n.text.slice(pos);
  changed();
  if (el) {
    el.value = n.text;
    el.focus();
    el.setSelectionRange(pos + name.length + 2, pos + name.length + 2);
    syncPromptHighlight(el);
    requestAnimationFrame(() => { if (el.isConnected) syncPromptHighlight(el); });
  } else refreshNodes();
}
async function showPreview(nid) {
  try {
    await save();
    const p = await api("preview", { ...query(), node_id: nid });
    openDrawer("preview", "本次生成方案");
    $("#drawer-body").innerHTML =
      `<div class="generation-summary"><span class="eyebrow">${esc(p.route)}</span><h3>${esc(p.model)}</h3><p>${esc(JSON.stringify(p.parameters))}</p><div class="reference-chips">${p.references.map((r) => `<span class="reference-chip">@${esc(r.alias)} → ${esc(r.label)}</span>`).join("")}</div><h4>实际发送的 Prompt</h4><div class="source-text">${esc(p.prompt)}</div></div>`;
  } catch (e) {
    toast(e.message, true);
  }
}
async function uploadMany(files, nid) {
  if (!files.length) return;
  try {
    await save();
    const context = query(),
      scope = app.scope,
      ids = [];
    for (const file of files) {
      if (file.size > 10 * 1024 * 1024) throw new Error("单张图片最大10MB");
      const data = await asBase64(file),
        a = await api("upload", { ...context, node_id: nid, data });
      ids.push(a.id);
    }
    await api("select", { ...context, node_id: nid, asset_ids: ids });
    if (scope === app.scope) {
      await syncGraph();
      refreshNodes();
    }
    toast(`已上传并选择 ${ids.length} 张图片`);
  } catch (e) {
    toast(e.message, true);
  }
}
$("#characters-button").onclick = () =>
  openEpisode(SHARED).catch((e) => toast(e.message, true));
$("#episode-list").addEventListener("click", (e) => {
  if (e.target.closest("[data-open-shared]"))
    openEpisode(SHARED).catch((e) => toast(e.message, true));
  if (e.target.closest("[data-add-clip]")) addClip();
  const clip = e.target.closest("[data-select-clip]");
  if (clip) focusClip(clip.dataset.selectClip);
  const free=e.target.closest('[data-free-node]');
  if (free) { app.activeClip=null;locate(free.dataset.freeNode,app.episode).then(renderEpisodes).catch(e=>toast(e.message,true)); }
});
$("#groups").onclick = (e) => {
  const button = e.target.closest("[data-clip-focus]");
  if (button) focusClip(button.dataset.clipFocus);
  const overview = e.target.closest("[data-clip-overview]");
  if (overview) focusClip(overview.dataset.clipOverview, true);
  const add = e.target.closest("[data-add-shot]");
  if (add) addShot(add.dataset.addShot);
  if (e.target.closest("[data-open-shared]"))
    openEpisode(SHARED).catch(e => toast(e.message, true));
};
$("#plan-button").onclick = async () => {
  try {
    await save();
    const rebuild = app.episode !== SHARED && app.graph.nodes.some(n => !n.shared_node_id);
    await api(app.episode === SHARED ? "prepare" : rebuild ? "replan" : "plan", query());
    toast(rebuild ? "正在按剧情重排多分镜视频，旧画布及素材将保留在规划历史" : "导演初始化已提交");
    await poll();
  } catch (e) {
    toast(e.message, true);
  }
};
$("#nodes").addEventListener("click", (e) => {
  const localVideo=e.target.closest('[data-upload-video]');
  if(localVideo)pickMedia(localVideo.dataset.uploadVideo);
  const close=e.target.closest('[data-close-composer]');
  if(close)select(null);
  const uploadReference=e.target.closest('[data-pick-reference]');
  if(uploadReference)pickMedia(null,uploadReference.dataset.pickReference);
  const promptSource=e.target.closest('[data-prompt-source]');
  if (promptSource) locate(promptSource.dataset.promptSource,app.episode).catch(error=>toast(error.message,true));
  const reference=e.target.closest('[data-locate-reference]');
  if (reference) locate(reference.dataset.locateReference,app.episode).catch(error=>toast(error.message,true));
  const refs = e.target.closest("[data-refs]"),
    mention = e.target.closest("[data-mention]"),
    preview = e.target.closest("[data-preview]"),
    url = e.target.closest("[data-video-url]");
  if (refs) showReferences(refs.dataset.refs);
  if (mention) insertReference(mention.dataset.target, mention.dataset.mention);
  if (preview) showPreview(preview.dataset.preview);
  if (url) {
    app.urlTarget = url.dataset.videoUrl;
    $("#video-url-input").value = "";
    $("#video-url-dialog").showModal();
  }
  const global = e.target.closest("[data-global]");
  if (global) locate(global.dataset.global, SHARED).catch(e => toast(e.message, true));
});
$("#nodes").addEventListener("change", async (e) => {
  if (!e.target.dataset.output) return;
  const nid = e.target.dataset.output,
    ids = $$(`[data-output="${nid}"]:checked`).map((el) => el.value);
  try {
    await save();
    await api("select", { ...query(), node_id: nid, asset_ids: ids });
    await syncGraph();
    refreshNodes();
  } catch (error) {
    e.target.checked = !e.target.checked;
    toast(error.message, true);
  }
});
$("#drawer-body").addEventListener("click", (e) => {
  const el = e.target.closest("[data-insert-ref]");
  if (el) insertReference(app.referenceTarget, el.dataset.insertRef);
});
$("#plan-history").onclick = showPlanHistory;
$("#drawer-body").addEventListener("click", async e => {
  const restore = e.target.closest("[data-restore-plan]");
  if (!restore) return;
  restore.disabled = true;
  try {
    await save();
    const scope = app.scope, eid = app.episode;
    await api("restore-plan", {...query(), archive_id: restore.dataset.restorePlan});
    if (scope === app.scope) { await openEpisode(eid); await showPlanHistory(); }
    toast("已恢复画布；恢复前的版本也已保留");
  } catch (error) { toast(error.message, true); }
  finally { restore.disabled = false; }
});
$("#video-url-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await save();
    await api("reference-video", {
      ...query(),
      node_id: app.urlTarget,
      url: $("#video-url-input").value,
    });
    await syncGraph();
    refreshNodes();
    $("#video-url-dialog").close();
    toast("参考视频已接入，可连接到其他视频节点");
  } catch (error) {
    toast(error.message, true);
  }
};
$("#video-url-dialog .dialog-close").onclick = () =>
  $("#video-url-dialog").close();
function toggleMenu() {
  app.nextFrom = null;
  app.menu = !app.menu;
  $("#add-menu").hidden = !app.menu;
  $("#add-menu .eyebrow").textContent = "添加到画布";
  $$("#add-menu [data-add]").forEach((b) => (b.disabled = false));
  if(app.menu)positionMenu($('#new-prompt'));
}
function positionMenu(anchor) {
  const menu=$('#add-menu'),rect=anchor?.getBoundingClientRect();if(!rect)return;
  menu.style.left=Math.max(12,Math.min(rect.left,window.innerWidth-menu.offsetWidth-12))+'px';
  menu.style.top=Math.max(12,Math.min(rect.bottom+8,window.innerHeight-menu.offsetHeight-12))+'px';
}
function showNextMenu(nid) {
  app.nextFrom = nid;
  app.menu = true;
  $("#add-menu").hidden = false;
  $("#add-menu .eyebrow").textContent = "下一步，想生成什么？";
  const n = app.graph.nodes.find((n) => n.id === nid);
  $$("#add-menu [data-add]").forEach(
    (b) =>
      (b.disabled = !canConnect(n,{id:'new-card',type:b.dataset.add})),
  );
  positionMenu($(`.node[data-node="${nid}"] [data-next]`));
}

$("#new-prompt").onclick = toggleMenu;

$("#nodes").addEventListener("change", (e) => {
  const nid = e.target.dataset.editCurrent;
  if (!nid) return;
  checkpoint();
  app.graph.nodes.find((n) => n.id === nid).settings.use_current =
    e.target.checked;
  changed();
});
