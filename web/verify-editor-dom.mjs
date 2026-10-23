// SPDX-License-Identifier: GPL-3.0-or-later
// Deterministic DOM/API integration with the real pure Python edit model.
// Media methods are stubbed; this is not a visual/playback browser test.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { JSDOM } from 'jsdom';
const require = createRequire(import.meta.url);
const root = fileURLToPath(new URL('../', import.meta.url));
const ts = require('typescript');
const source = readFileSync(new URL('./src/transcript-editor.ts', import.meta.url),'utf8').replace("import './transcript-editor.css';",'');
const moduleCode = ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
const dom = new JSDOM('<!doctype html><main id="root"></main>',{url:'http://localhost',pretendToBeVisual:true});
const window=dom.window;
for(const name of ['window','document','HTMLElement','HTMLInputElement','HTMLTextAreaElement','HTMLMediaElement','HTMLSelectElement','MouseEvent','Event','KeyboardEvent','AbortController','DOMException']) globalThis[name]=name==='window'?window:window[name];
window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
window.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new Event('close'));};
window.HTMLMediaElement.prototype.play=function(){this.__plays=(this.__plays||0)+1;return Promise.resolve();};
window.HTMLMediaElement.prototype.pause=function(){this.__pauses=(this.__pauses||0)+1;};
window.HTMLMediaElement.prototype.load=function(){};
const originalTimer=window.setTimeout.bind(window);
window.setTimeout=(fn,delay,...rest)=>originalTimer(fn,delay===1000?1:delay,...rest);
const {mountTranscriptEditor,readEditDocument,editedPosition,candidateDocument}=await import('data:text/javascript;base64,' + Buffer.from(moduleCode).toString('base64'));
const segments=[{id:4,start:0,end:4,text:'First sentence'},{id:9,start:4,end:8,text:'Second sentence'}];
const initial={version:1,source_duration:8,ranges:segments.map((s,i)=>({id:'r'+i,clip_id:s.id,start:s.start,end:s.end,group_id:'g'+i}))};
const transcript=[{id:100,start:0,end:4,text:'Hello um world',words:[{word:'Hello',start:0,end:1},{word:'um',start:1,end:1.5},{word:'world',start:2,end:4}]},{id:200,start:4,end:8,text:'Second sentence'}];
const candidates=[{id:'candidate-x',title:'<img src=x onerror=alert(1)>',text:'Selected statement',reason:'Local suggestion',start:3,end:6,ranges:[{start:3,end:6,transcript_id:200}],scoring_source:'local_heuristic'}];
let store={}, requests=[],patches=[],busy=[],opened=0,mode='complete',cancelled=false,lateResolve;
const response=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
globalThis.fetch=async (url,options={})=>{
 const path=new URL(url,'http://localhost').pathname.replace('/api/projects/one/api','');
 const data=options.body?JSON.parse(options.body):undefined; requests.push({path,data});
 if(path==='/editor') { if(mode==='late') return new Promise(resolve=>lateResolve=()=>resolve(response({document:initial,captions:transcript,transcript}))); return response({document:initial,captions:transcript,transcript}); }
 if(path==='/edit-preview') {try {const out=execFileSync(process.env.PYTHON || 'python3',['-c','import json,sys;from edit_model import apply_edit;x=json.load(sys.stdin);print(json.dumps({"document":apply_edit(x["document"],x["action"])}))'],{input:JSON.stringify(data),encoding:'utf8',cwd:root});return new Response(out,{status:200});}catch(error){return response({message:'Invalid edit operation'},400);}}
 if(path==='/candidates') return response({task_id:'candidates'});
 if(path==='/processing-status/candidates') return response({status:'complete',candidates});
 if(path==='/cleanup') return response({suggestions:[{id:'filler',type:'filler',start:1,end:1.5,text:'um',reason:'Review in context'}]});
 if(path==='/render') return response({task_id:'render'});
 if(path==='/cancel/render') {cancelled=true;return response({status:'ok'});}
 if(path==='/processing-status/render') return response(mode==='error'?{status:'error',message:'Render test failed'}:mode==='cancel'&&!cancelled?{status:'processing',message:'Waiting'}:cancelled?{status:'cancelled'}:{status:'complete',output_file:'/api/projects/one/final/reel.mp4',outputs:[{output_file:'/api/projects/one/final/reel.mp4'}]});
 if(path==='/edited-subtitles')return new Response('WEBVTT\n\n00:00.000 --> 00:01.000\nHello\n');
 throw new Error('Unexpected '+path);
};
const flush=()=>new Promise(resolve=>setTimeout(resolve,10));
async function until(fn){for(let i=0;i<500;i++){if(fn())return;await flush();}throw new Error('Did not settle: '+fn.toString()+' status='+document.querySelector('.te-status')?.textContent);}
const click=text=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent===text);assert.ok(b,'Button '+text);assert.equal(b.disabled,false,'Enabled '+text);b.click();};
const tab=text=>click(text);
const change=(node,value)=>{node.value=value;node.dispatchEvent(new Event('change',{bubbles:true}));};
const choose=(selector,index=0)=>{const n=document.querySelectorAll(selector)[index];n.checked=true;n.dispatchEvent(new Event('change',{bubbles:true}));};
const mount=()=>mountTranscriptEditor({container:document.getElementById('root'),projectId:'one',sourceUrl:'/api/projects/one/api/source/aa',segments,get state(){return store;},onChange:patch=>{store={...store,...patch};patches.push(patch);},onBusy:value=>busy.push(value),onOpen:()=>opened++});
let dispose=mount(); click('Open transcript editor');await until(()=>document.querySelector('.te-status').textContent.startsWith('Ready'));
assert.equal(opened,1);assert.equal(document.querySelectorAll('.te-range').length,2);
choose('.te-range-title input');click('Remove selected');await until(()=>document.querySelectorAll('.te-range').length===1);assert.equal(store.edit_document.ranges[0].clip_id,9);
click('Undo');assert.equal(store.edit_document.ranges.length,2);click('Redo');assert.equal(store.edit_document.ranges.length,1);click('Undo');
click('Select all');click('Remove selected');await until(()=>store.edit_document.ranges.length===0);assert.match(document.querySelector('.te-empty').textContent,/empty/);click('Undo');assert.equal(store.edit_document.ranges.length,2);
tab('Captions');change(document.querySelector('.te-caption textarea'),'Corrected text');assert.equal(store.edited_captions[0].text,'Corrected text');click('Undo');assert.equal(store.edited_captions[0].text,'Hello um world');click('Redo');assert.equal(store.edited_captions[0].text,'Corrected text');
tab('Sequence');const trimInputs=document.querySelector('.te-range').querySelectorAll('input[type=number]');trimInputs[0].value='0.25';trimInputs[1].value='3.75';click('Apply trim');await until(()=>store.edit_document.ranges[0].start===0.25);
click('Split range');await until(()=>store.edit_document.ranges.length===3);assert.equal(store.edit_document.ranges[1].start,2);click('Select all');click('Combine selected');await until(()=>new Set(store.edit_document.ranges.map(r=>r.group_id)).size===1);
click('Move down');await until(()=>store.edit_document.ranges[0].start===2);click('Undo');click('Undo');click('Undo');click('Undo');
tab('Transcript');const words=document.querySelectorAll('.te-word');words[1].click();assert.equal(document.activeElement?.tagName,'BODY');click('Remove passage');await until(()=>store.edit_document.ranges.length===3);assert.deepEqual(requests.filter(r=>r.path==='/edit-preview').at(-1).data.action,{type:'remove',start:1,end:1.5});click('Undo');
tab('Cleanup');click('Review cleanup suggestions');await until(()=>document.querySelector('.te-cleanup'));click('Accept removal');await until(()=>document.querySelector('.te-cleanup button:last-child').textContent==='Already absent from edit');click('Undo');assert.equal(document.querySelector('.te-cleanup button:last-child').disabled,false);
tab('Candidates');const before=JSON.stringify(store.edit_document);click('Find candidates');await until(()=>document.querySelector('.te-candidate'));assert.equal(JSON.stringify(store.edit_document),before);assert.equal(document.querySelectorAll('.te-candidate img').length,0);choose('.te-candidate input');click('Use selected candidates');assert.deepEqual(store.edit_document.ranges.map(r=>[r.clip_id,r.start,r.end]),[[4,3,4],[9,4,6]]);click('Undo');assert.equal(JSON.stringify(store.edit_document),before);
click('Play edit');assert.equal(document.querySelector('.te-video-frame video').currentTime,0);
change(document.querySelector('.te-settings select'),'9:16');assert.equal(store.export_settings.aspect_ratio,'9:16');
const burn=document.querySelector('.te-settings input[type=checkbox]');burn.checked=true;burn.dispatchEvent(new Event('change'));assert.equal(store.export_settings.burn_captions,true);
click('Export edited reel');await until(()=>document.querySelector('.te-downloads a'));assert.equal(document.querySelectorAll('.te-downloads a').length,1);assert.equal(requests.find(r=>r.path==='/render').data.settings.aspect_ratio,'9:16');
mode='error';click('Export edited reel');await until(()=>document.querySelector('.te-status').textContent==='Render test failed');assert.equal(busy.at(-1),false);
mode='cancel';click('Export edited reel');await until(()=>!document.querySelector('.te-status-bar button:last-child').hidden);click('Cancel task');await until(()=>document.querySelector('.te-status').textContent.startsWith('Task cancelled'));assert.equal(busy.at(-1),false);
click('Close editor');assert.equal(document.querySelector('dialog').open,false);click('Open transcript editor');assert.equal(opened,2);assert.equal(requests.filter(r=>r.path==='/editor').length,1);
dispose();assert.equal(document.querySelectorAll('.te-dialog,.te-launcher').length,0);
mode='late';store={};dispose=mount();click('Open transcript editor');await until(()=>!!lateResolve);const n=patches.length;dispose();lateResolve();await flush();assert.equal(patches.length,n);assert.equal(document.querySelectorAll('.te-dialog').length,0);
assert.throws(()=>readEditDocument({...initial,ranges:[initial.ranges[0],initial.ranges[0]]}));
assert.deepEqual(editedPosition([{start:10,end:12},{start:2,end:5}],2.5),{index:1,sourceTime:2.5});
assert.deepEqual(editedPosition([{start:10,end:12},{start:2,end:5}],999),{index:1,sourceTime:5});
assert.equal(editedPosition([],0),null);
assert.deepEqual(candidateDocument(candidates,segments,8).ranges.map(r=>r.clip_id),[4,9]);
mode='complete';store={edit_document:{...initial,ranges:[]}};dispose=mount();click('Open transcript editor');await until(()=>document.querySelector('.te-status').textContent.startsWith('Ready'));assert.equal(document.querySelectorAll('.te-range').length,0);click('Restore all source clips');assert.equal(store.edit_document.ranges.length,2);tab('Captions');document.querySelector('.te-caption textarea').value='Pending draft';click('Close editor');assert.equal(store.edited_captions[0].text,'Pending draft');dispose();
console.log('PASS: editor DOM/API tests: load, all edit actions, captions, undo/redo, candidates, cleanup, preview seek, crop, exports, render error, cancel, close/reopen, disposal, source-time helpers.');
dom.window.close();
