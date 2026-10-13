import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const moduleURL = path => 'data:text/javascript;base64,'+Buffer.from(readFileSync(new URL(path,import.meta.url),'utf8')).toString('base64');
const commons=moduleURL('../agent_world/web/commons-client.js');
const world=moduleURL('../agent_world/web/world-client.js');
const source=readFileSync(new URL('../agent_world/grove_web/grove-client.js',import.meta.url),'utf8')
  .replace("'/static/commons-client.js'",JSON.stringify(commons)).replace("'/static/world-client.js'",JSON.stringify(world));
const {GroveClient}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const values=new Map(),storage={getItem:k=>values.get(k)??null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};
const reply=(value,status=200)=>({ok:status<400,status,json:async()=>value});
const projection=(seq=1,extra={})=>({projection_version:2,world_instance:'shared-grove',world_id:'shared-grove',world_version:38,viewer_role_id:'self',snapshot_seq:seq,observation_clock:{epoch:'one',sequence:seq},...extra});
const authorize=client=>{client.roleId='self';client.tokenId='token-id';client.token='existing-token';client.projectionContext=projection();};
const envelope=(name,result,seq=result.context.snapshot_seq)=>({ok:true,read_only:true,function_id:name,function_version:{'grove.client_viewport':2,'grove.client_inspect':2,'grove.client_definitions':3,'grove.client_choices':1}[name],snapshot_seq:seq,result});


// Losing the body and leaving must not fabricate a second action.
const waiting=async(_path,{signal})=>({ok:true,status:200,json:()=>new Promise((_resolve,reject)=>
  signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true}))});
const client=new GroveClient(waiting,storage,null);client.universe='shared-grove';client.publicKey='A';authorize(client);
const sending=client.mutate('grove.move',{dx:1,dy:0,expected_player_version:1});
await new Promise(resolve=>setImmediate(resolve));client.cancel();
await assert.rejects(sending);
const pending=client.pending();assert.ok(pending&&!pending.rejection);
await assert.rejects(client.releaseRejected(),/结果仍不明确/);
client.fetcher=async path=>{
  assert.equal(path,'/v1/receipts/'+pending.body.operation_id);
  return reply({ok:true,operation_id:pending.body.operation_id,function_id:'grove.move',result:{player:{x:1,y:0}}});
};
assert.equal((await client.recover()).player.x,1);assert.equal(client.pending(),null);

// A missing receipt allows exact replay, and only the original operation ID.
const replay=new GroveClient(async()=>{throw new Error('lost');},storage,null);
replay.universe='shared-grove';replay.publicKey='B';authorize(replay);
await assert.rejects(replay.mutate('grove.interact',{action:'pickup',target_id:'wood',expected_player_version:1}));
const original=replay.pending();
replay.fetcher=async(path,options)=>{
  if(path.startsWith('/v1/receipts/'))return reply({error:'ReceiptNotFound',message:'Missing'},404);
  assert.deepEqual(JSON.parse(options.body),original.body);
  return reply({ok:true,operation_id:original.body.operation_id,function_id:'grove.interact',result:{taken:true}});
};
assert.equal((await replay.recover()).taken,true);assert.equal(replay.pending(),null);

// A definite changing-material conflict can be released before another choice.
const conflict=new GroveClient(async()=>reply({error:'StateConflict',message:'Fire changed the object'},409),storage,null);
conflict.universe='shared-grove';conflict.publicKey='C';authorize(conflict);
await assert.rejects(conflict.mutate('grove.interact',{action:'pour',expected_player_version:1}));
assert.equal(conflict.pending().rejection.code,'StateConflict');
await conflict.releaseRejected();assert.equal(conflict.pending(),null);
console.log('Grove client: cancellation, original receipt/replay and material conflict recovery passed');

// A structured refusal keeps its recovery policy; it is no new intent permit.
const refusal=new GroveClient(async()=>reply({error:'DomainBusy',message:'正在结算',retryable:true,
  retry_after_seconds:0.2,recovery:'retry_same_operation',details:{domain:'work-a'}},409),storage,null);
refusal.universe='shared-grove';refusal.publicKey='D';authorize(refusal);
await assert.rejects(refusal.mutate('grove.interact',{action:'pour',expected_player_version:1}),error=>{
  assert.equal(error.retryable,true);assert.equal(error.retry_after_seconds,0.2);
  assert.equal(error.recovery,'retry_same_operation');assert.deepEqual(error.details,{domain:'work-a'});
  return true;
});
assert.ok(refusal.pending());

// Repeated browser maps are authenticated queries, not durable view writes.
{
  let reads=0;
  const mapClient=new GroveClient(async(path,options)=>{
    assert.equal(path,'/v1/functions/grove.client_viewport/invoke');
    assert.equal(options.method,'POST');assert.ok(options.signal);
    assert.equal(JSON.parse(options.body).operation_id,undefined);
    reads++;
    return reply(envelope('grove.client_viewport',{context:projection(reads),player:{x:reads,y:0},entities:[],clock:{sequence:reads}}));
  },storage,null);
  mapClient.universe='shared-grove';authorize(mapClient);
  for(let i=1;i<=4;i++)assert.equal(mapClient.unpackView(await mapClient.viewport()).player.x,i);
  assert.equal(mapClient.world.view,null);assert.equal(mapClient.pending(),null);
}

// All read stages are bounded; cancelled/late replies cannot restore a view.
{
  const {WorldClient}=await import(world);
  for(const stage of ['token','fetch','json']){
    let resolveLate;
    const hangs=new Promise(resolve=>{resolveLate=resolve;});
    const reader=new WorldClient('',()=>stage==='token'?hangs:'token',async()=>stage==='fetch'?hangs:{ok:true,status:200,json:()=>hangs});
    reader.readTimeoutMs=10;
    await assert.rejects(reader.query('read',{}),error=>error.code==='ReadInterrupted');
    resolveLate(stage==='token'?'late-token':stage==='fetch'?reply({}):{});
  }
  let finish,calls=0;
  const writer=new WorldClient('',()=> 'token',async(_path,options)=>{calls++;assert.equal(options.signal,undefined);return {ok:true,status:200,json:()=>new Promise(resolve=>{finish=resolve;})};});
  writer.readTimeoutMs=1;
  const operation=writer.act('write',{},'same-original');
  await new Promise(resolve=>setTimeout(resolve,10));writer.cancelReads();
  assert.equal(calls,1);finish({operation_id:'same-original'});
  assert.equal((await operation).operation_id,'same-original');
  const snapshot={kind:'snapshot',view:'map',cursor:'late',viewer_role_id:'A',world_version:1,view_version:1,snapshot:{entities:{},resources:{},meta:{}}};
  let deliver;
  const reader=new WorldClient('',()=> 'token',async()=>({ok:true,status:200,json:()=>new Promise(resolve=>{deliver=resolve;})}));
  const pendingRead=reader.loadView('map');await new Promise(resolve=>setImmediate(resolve));reader.cancelReads();
  await assert.rejects(pendingRead);deliver(snapshot);await new Promise(resolve=>setImmediate(resolve));assert.equal(reader.view,null);
}

// Identity, version and the formal read watermark must all match before a
// DTO can enter the page or enable a new action. No raw read is a fallback.
{
  const guarded=new GroveClient(async()=>reply({}),storage,null);
  guarded.universe='shared-grove';guarded.publicKey='guarded';authorize(guarded);
  let next=envelope('grove.client_inspect',{context:projection(5),entity:{id:'selected'},player:{version:1}});
  guarded.world.fetcher=async()=>reply(next);
  assert.equal((await guarded.query('grove.client_inspect',{entity_id:'selected'})).entity.id,'selected');
  for(const context of [projection(6,{projection_version:1}),projection(6,{projection_version:99}),projection(6,{viewer_role_id:'another'}),projection(6,{world_instance:'another'}),projection(6,{snapshot_seq:7})]){
    next=envelope('grove.client_inspect',{context,entity:{id:'wrong'}},6);
    await assert.rejects(guarded.query('grove.client_inspect',{entity_id:'selected'}));
    await assert.rejects(guarded.mutate('grove.move',{dx:1,dy:0}),/客户端接口/);
    assert.equal(guarded.pending(),null);
  }
  next=envelope('grove.client_inspect',{context:projection(8),entity:{id:'fresh'}});
  await guarded.query('grove.client_inspect',{entity_id:'selected'});
  next=envelope('grove.client_inspect',{context:projection(7),entity:{id:'stale'}});
  await assert.rejects(guarded.query('grove.client_inspect',{entity_id:'selected'}),error=>error.cancelled===true);
  assert.equal(guarded.projectionContext.snapshot_seq,8);
  next=envelope('grove.client_inspect',{context:projection(9,{world_version:39}),entity:{id:'changed-rules'}});
  await assert.rejects(guarded.query('grove.client_inspect',{entity_id:'selected'}),/世界规则版本/);
  assert.equal(guarded.projectionBlocked,true);
}
{
  let deliver;
  const guarded=new GroveClient(async()=>reply({}),storage,null);
  guarded.universe='shared-grove';guarded.publicKey='late';authorize(guarded);
  guarded.world.fetcher=async()=>({ok:true,status:200,json:()=>new Promise(resolve=>{deliver=resolve;})});
  const reading=guarded.query('grove.client_inspect',{entity_id:'selected'});
  await new Promise(resolve=>setImmediate(resolve));guarded.roleId='another';
  deliver(envelope('grove.client_inspect',{context:projection(5),entity:{id:'late'}}));
  await assert.rejects(reading,error=>error.cancelled===true);
  assert.equal(guarded.projectionContext.viewer_role_id,'self');
  await assert.rejects(guarded.mutate('grove.move',{dx:1,dy:0}),/本身份/);
}

// An old identity's failed DTO read cannot sign the new identity out.
{
  let deliver;
  const guarded=new GroveClient(async()=>reply({}),storage,null);
  guarded.universe='shared-grove';authorize(guarded);
  guarded.world.fetcher=async()=>({ok:false,status:401,json:()=>new Promise(resolve=>{deliver=resolve;})});
  const reading=guarded.query('grove.client_inspect',{entity_id:'selected'});
  await new Promise(resolve=>setImmediate(resolve));
  guarded.token='new-token';guarded.tokenId='new-token-id';guarded.roleId='another';
  deliver({error:'InvalidIdentityToken',message:'Old identity expired'});
  await assert.rejects(reading,error=>error.cancelled===true&&error.staleAuth===true);
  assert.equal(guarded.token,'new-token');
}

// All current projection surfaces declare their function versions explicitly.
{
  const definitionClient=new GroveClient(async()=>reply(envelope('grove.client_definitions',{context:projection(1),mode:'list',definitions:[]})),storage,null);
  definitionClient.universe='shared-grove';definitionClient.publicKey='revision-dto';authorize(definitionClient);
  assert.equal((await definitionClient.query('grove.client_definitions',{mode:'list'})).mode,'list');
  definitionClient.world.fetcher=async()=>reply({...envelope('grove.client_definitions',{context:projection(2),mode:'list',definitions:[]}),function_version:1});
  await assert.rejects(definitionClient.query('grove.client_definitions',{mode:'list'}));
}

// An unsupported DTO cannot create a new intent, but exact old recovery remains
// available even when ReceiptNotFound requires replay of the original envelope.
{
  const guarded=new GroveClient(async()=>{throw new Error('lost original response');},storage,null);
  guarded.universe='shared-grove';guarded.publicKey='unknown-dto-recovery';authorize(guarded);
  await assert.rejects(guarded.mutate('grove.interact',{action:'opaque',target_id:'original',expected_player_version:1}));
  const original=guarded.pending();
  guarded.world.fetcher=async()=>reply(envelope('grove.client_choices',{context:projection(2,{projection_version:99}),choices:[],next_request:null,complete:true}));
  await assert.rejects(guarded.query('grove.client_choices',{entity_id:'original',action:'opaque',control_id:'choice'}));
  await assert.rejects(guarded.mutate('grove.move',{dx:1,dy:0}),/客户端接口/);
  guarded.fetcher=async(path,options)=>{
    if(path.startsWith('/v1/receipts/'))return reply({error:'ReceiptNotFound',message:'Not committed yet'},404);
    assert.deepEqual(JSON.parse(options.body),original.body);
    return reply({ok:true,operation_id:original.body.operation_id,function_id:original.name,result:{saved:true}});
  };
  assert.equal((await guarded.recover()).saved,true);assert.equal(guarded.pending(),null);
  await assert.rejects(guarded.query('grove.client_unknown',{}),/读取合同/);
}
{
  const choicesClient=new GroveClient(async()=>reply(envelope('grove.client_choices',{context:projection(),choices:[],next_request:null,complete:true})),storage,null);
  choicesClient.universe='shared-grove';authorize(choicesClient);
  assert.equal((await choicesClient.query('grove.client_choices',{entity_id:'target',action:'opaque',control_id:'choice'})).complete,true);
  choicesClient.world.fetcher=async()=>reply({...envelope('grove.client_choices',{context:projection(2),choices:[],next_request:null,complete:true}),function_version:2});
  await assert.rejects(choicesClient.query('grove.client_choices',{entity_id:'target',action:'opaque',control_id:'choice'}));
}
