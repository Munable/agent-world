import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const helperSource=readFileSync(new URL('../agent_world/grove_web/grove-world-ui.js',import.meta.url),'utf8');
const ui=await import('data:text/javascript;base64,'+Buffer.from(helperSource).toString('base64'));
const source=readFileSync(new URL('../agent_world/grove_web/grove.js',import.meta.url),'utf8');

// A directory hint cannot authorize a distant or stale inspect, even when its
// nearby flag is true or a previous inspect cache still holds the object.
{
  const work={entity_id:'creation',nearby:true,location:{x:90,y:90}};
  const bounds={min_x:-8,max_x:8,min_y:-8,max_y:8};
  const world={bounds,entities:[],inventory:[{id:'creation',custody:{scope:'private',holder_id:'self',container_id:null}}]};
  assert.equal(ui.publicWorkLocalTarget(work,world,true),null);
  world.entities=[{id:'creation',x:90,y:90,custody:{scope:'public',holder_id:null,container_id:null}}];
  assert.equal(ui.publicWorkLocalTarget(work,world,true),null);
  world.entities=[{id:'creation',x:2,y:3,custody:{scope:'public',holder_id:null,container_id:null}}];
  assert.equal(ui.publicWorkLocalTarget(work,world,false),null);
  assert.equal(ui.publicWorkLocalTarget(work,world,true),world.entities[0]);
  world.entities[0].custody.container_id='closed-container';
  assert.equal(ui.publicWorkLocalTarget(work,world,true),null);
}

{
  const page={filters:{author_name:'同名'},works:[],author_matches:[],index_build_complete:false,has_more:true};
  const lines=ui.publicWorkDirectoryLines(page);
  assert(lines.some(line=>line.includes('仍在建立')));
  assert(lines.some(line=>line.includes('空页不代表')));
  assert(lines.some(line=>line.includes('不是全部结果')));
  assert(!lines.some(line=>line.includes('在线')));
  page.author_identity_required=true;page.author_matches=[{author:'one',name:'同名'},{author:'two',name:'同名'}];
  assert(ui.publicWorkDirectoryLines(page).some(line=>line.includes('不会合并')));
  const fields=ui.publicWorkFactLines({fields:{speed:0,enabled:false},fields_omitted_count:4,lifecycle:'intact'});
  assert(fields.some(line=>line.includes('保存字段')));
  assert(fields.some(line=>line.includes('4 个字段')));
  assert(!fields.some(line=>line.includes('正在运行')||line.includes('已停止')));
  const hidden=ui.publicWorkFactLines({contents_visibility:'hidden',fields:{secret:999},fields_omitted_count:12});
  assert(hidden.some(line=>line.includes('未公开')));
  assert(!hidden.some(line=>line.includes('999')||line.includes('12')));
}

{
  const far=ui.publicWorkMarkerGeometry({x:4000,y:4000},600,400);
  assert(far.offscreen&&far.x<=544&&far.y<=344);
  const nearby=ui.publicWorkMarkerGeometry({x:200,y:200},600,400);
  assert.deepEqual(nearby,{x:200,y:200,offscreen:false,angle:Math.PI});
  assert.equal(ui.publicWorkMarkerGeometry({x:NaN,y:1},600,400),null);
}

// Exercise the actual reader, including failed paging and a late response
// after leaving. Neither path can replace accepted results or fetch details.
{
  const reader=source.slice(source.indexOf('async function readPublicWorks('),source.indexOf('function localPublicWork('));
  const queries=[];
  let reply={scope:'current_public_top_level_creations',filters:{},works:[{entity_id:'one'}],has_more:true,next_cursor:'page-two'},failure=null;
  const context={Date,Error,playing:true,session:1,publicWorksEpoch:0,publicWorksBusy:false,publicWorksPage:null,publicWorksFilters:{},publicWorksCursors:[null],publicWorksError:null,publicWorksReadAt:0,
    current:()=>true,renderPublicWorks:()=>{},client:{query:async(id,args)=>{queries.push({id,args});if(failure)throw failure;return reply;}}};
  vm.createContext(context);vm.runInContext(reader,context);
  await context.readPublicWorks();
  assert.equal(queries[0].id,'grove.public_works');
  assert.equal(context.publicWorksPage.works[0].entity_id,'one');
  assert.equal(context.publicWorksCursors.length,1);
  failure=new Error('offline');
  await context.readPublicWorks({filters:{author:'selected-author'},cursors:[null,'page-two']});
  assert.equal(context.publicWorksPage.works[0].entity_id,'one');
  assert.equal(context.publicWorksCursors.length,1);
  assert.equal(context.publicWorksFilters.author,undefined);
  assert.equal(context.publicWorksError,'offline');
  let release;
  context.client.query=async()=>await new Promise(resolve=>{release=resolve;});
  const pending=context.readPublicWorks({filters:{name:'new'}});
  ++context.session;++context.publicWorksEpoch;context.playing=false;
  release({scope:'current_public_top_level_creations',works:[{entity_id:'late'}]});await pending;
  assert.equal(context.publicWorksPage.works[0].entity_id,'one');
  assert(queries.every(query=>query.id==='grove.public_works'));
}

// The actual click handler rechecks the latest map before using the normal
// select path. It never navigates, selects remotely, or writes a world action.
{
  const handler=source.slice(source.indexOf('async function selectPublicWork('),source.indexOf('function renderPublicWorks('));
  const world={bounds:{min_x:-8,max_x:8,min_y:-8,max_y:8},entities:[]},calls=[];
  const context={localPublicWork:work=>ui.publicWorkLocalTarget(work,world,true),announce:message=>calls.push(['notice',message]),stopWalking:()=>calls.push(['stop']),select:async id=>calls.push(['select',id])};
  vm.createContext(context);vm.runInContext(handler,context);
  const work={entity_id:'public',nearby:true,location:{x:100,y:100}};
  await context.selectPublicWork(work);
  assert(!calls.some(([kind])=>kind==='select'||kind==='stop'));
  world.entities=[{id:'public',x:1,y:2,custody:{scope:'public',holder_id:null,container_id:null}}];await context.selectPublicWork(work);
  assert.equal(calls.filter(([kind])=>kind==='select').length,1);
  assert.deepEqual(calls.at(-1),['select','public']);
}

console.log('Public works UI regression sources passed');
