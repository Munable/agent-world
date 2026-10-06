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

// Losing the body and leaving must not fabricate a second action.
const waiting=async(_path,{signal})=>({ok:true,status:200,json:()=>new Promise((_resolve,reject)=>
  signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true}))});
const client=new GroveClient(waiting,storage,null);client.universe='shared-grove';client.publicKey='A';
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
replay.universe='shared-grove';replay.publicKey='B';
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
conflict.universe='shared-grove';conflict.publicKey='C';
await assert.rejects(conflict.mutate('grove.interact',{action:'pour',expected_player_version:1}));
assert.equal(conflict.pending().rejection.code,'StateConflict');
await conflict.releaseRejected();assert.equal(conflict.pending(),null);
console.log('Grove client: cancellation, original receipt/replay and material conflict recovery passed');
