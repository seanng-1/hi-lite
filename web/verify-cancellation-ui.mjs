// SPDX-License-Identifier: GPL-3.0-or-later
// DOM regressions only: media playback and layout require a real browser.
import { JSDOM } from 'jsdom';
import { fileURLToPath } from 'node:url';
import { readFile, readdir } from 'node:fs/promises';
import assert from 'node:assert/strict';
const base = fileURLToPath(new URL('../', import.meta.url));
const fixture = JSON.parse(await readFile(base + '/tests/fixtures/segments.json', 'utf8'));
const js = (await readdir(base + '/web/dist/assets')).find(name => name.endsWith('.js'));
const code = await readFile(base + '/web/dist/assets/' + js, 'utf8');
const a='a'.repeat(32),b='b'.repeat(32),c='c'.repeat(32);
const store={ [a]:{id:a,name:'A',revision:0,has_media:true,state:{selected_ids:[4],instructions:'Highlights'}}, [b]:{id:b,name:'B',revision:0,has_media:false,state:{selected_ids:[]}} };
const tasks={};let sequence=0,uploads=0,cancelFailure=0,statusFailure=false;
const requests=[];
const dom=new JSDOM('<!DOCTYPE html><div id="app"></div>',{url:'http://localhost/',runScripts:'outside-only',pretendToBeVisual:true});
const w=dom.window;
w.ResizeObserver=class{observe(){} disconnect(){}};
w.HTMLElement.prototype.checkVisibility=function(){return !this.hidden};
w.HTMLElement.prototype.scrollIntoView=function(){};
w.HTMLMediaElement.prototype.load=function(){};
w.HTMLMediaElement.prototype.pause=function(){};
w.HTMLMediaElement.prototype.play=function(){return Promise.resolve()};
w.HTMLDialogElement.prototype.showModal=function(){this.open=true};
w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'))};
w.confirm=()=>true;
const timeout=w.setTimeout;
w.setTimeout=(fn,delay,...args)=>timeout(fn,delay===2000||delay===3000?10:delay,...args);
function job(id){const task='job-'+(++sequence);tasks[task]={project:id,status:'processing'};return task}
w.hiLiteDesktop={async linkProject(id){return {status:'ok',task_id:job(id)}},async relinkProject(){return {status:'ok'}}};
w.fetch=async(input,options={})=>{
 const path=String(input);requests.push(path);const json=(data,status=200)=>new Response(JSON.stringify(data),{status});
 if(path==='/api/projects'){
  if(options.method==='POST'){store[c]={id:c,name:JSON.parse(options.body).name,revision:0,has_media:false,state:{selected_ids:[]}};return json(store[c],201)}
  return json({projects:Object.values(store)});
 }
 if(path==='/api/llm-settings')return json({resolved_backend:'api',model:'fixture',base_url:'https://fixture.invalid'});
 if(path.endsWith('/api/subtitles'))return json({available:false});
 const match=path.match(/^\/api\/projects\/([^/]+)(.*)$/);
 if(!match)return json({},404);
 const project=store[match[1]],tail=match[2];
 if(!tail){if(options.method==='PUT'){const body=JSON.parse(options.body);project.state=body.state;project.revision++}return json(project)}
 if(tail==='/data/segments_manifest.json')return project.has_media?json({...fixture,source:{id:a,url:`/api/projects/${project.id}/api/source/${a}`}}):json({},404);
 if(tail==='/api/auto-generate')return json({status:'ok',task_id:job(project.id)});
 if(tail==='/api/upload'){uploads++;return json({status:'ok',task_id:job(project.id)})}
 if(tail.startsWith('/api/cancel/')){if(cancelFailure)return json({message:'Cancellation unavailable'},cancelFailure);tasks[tail.split('/').pop()].status='cancelling';return json({status:'cancelling'})}
 if(tail.startsWith('/api/processing-status/')){if(statusFailure)throw Error('Temporary status outage');const task=tasks[tail.split('/').pop()];return json({status:task.status,message:'Working'})}
 return json({},404);
};
w.eval(code.replaceAll('import.meta.url',JSON.stringify('http://localhost/assets/app.js')));
const $=selector=>w.document.querySelector(selector);
const until=async predicate=>{for(let i=0;i<250;i++){if(predicate())return;await new Promise(resolve=>setTimeout(resolve,10))}throw Error('UI did not settle')};
await until(()=>$('#autoGenerateBtn')&&!$('#autoGenerateBtn').disabled);
cancelFailure=503;$('#autoGenerateBtn').click();await until(()=>!$('#activityCancel').hidden);$('#activityCancel').click();
await until(()=>$('#activityCancel').textContent==='Retry cancellation');
assert.equal($('#autoGenerateBtn').disabled,true);assert.equal($('#projectsTrigger').disabled,true);
cancelFailure=0;$('#activityCancel').click();await until(()=>tasks['job-1'].status==='cancelling');
statusFailure=true;await until(()=>$('#activityErrors').textContent.includes('worker has stopped'));
assert.equal($('#autoGenerateBtn').disabled,true);assert.equal($('#projectsTrigger').disabled,true);
statusFailure=false;tasks['job-1'].status='cancelled';await until(()=>!$('#autoGenerateBtn').disabled);
assert.match($('#statusMessage').textContent,/Cancelled/);assert.equal($('#activityErrors').children.length,0);assert.equal($('#activityCancel').hidden,true);
assert.deepEqual([...w.document.querySelectorAll('.segment-checkbox:checked')].map(n=>Number(n.dataset.id)),[4]);
$('#projectsTrigger').click();$(`[data-project-id="${b}"]`).click();await until(()=>$('#uploadButton'));
function upload(){Object.defineProperty($('#videoInput'),'files',{configurable:true,value:[new w.File(['fixture'],'fixture.mp4',{type:'video/mp4'})]});$('#videoInput').dispatchEvent(new w.Event('change'))}
upload();await until(()=>!$('#activityCancel').hidden);$('#activityCancel').click();await until(()=>tasks['job-2'].status==='cancelling');
assert.equal($('#uploadButton').disabled,true);assert.equal($('#projectsTrigger').disabled,true);
tasks['job-2'].status='cancelled';await until(()=>!$('#uploadButton').disabled);assert.match($('#statusMessage').textContent,/Cancelled/);
upload();await until(()=>!!tasks['job-3']);store[b].has_media=true;tasks['job-3'].status='complete';await until(()=>$('#selectAll'));
assert.equal(uploads,2);
$('#projectsTrigger').click();$('#newProjectName').value='Linked';$('#newProjectForm').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await until(()=>$('#linkSource'));
$('#linkSource').click();await until(()=>!$('#activityCancel').hidden);$('#activityCancel').click();await until(()=>tasks['job-4'].status==='cancelling');
assert.equal($('#linkSource').disabled,true);assert.equal($('#projectsTrigger').disabled,true);
tasks['job-4'].status='cancelled';await until(()=>!$('#linkSource').disabled);assert.match($('#statusMessage').textContent,/Cancelled/);
assert.equal(requests.filter(path=>path.includes('/api/cancel/')).length,4);
console.log('PASS cancellation DOM: failed cancellation retry, AI status outage retention, upload cancellation and reimport, linked import cancellation, project locks, terminal-only release.');
w.close();
