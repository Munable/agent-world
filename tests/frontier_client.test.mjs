import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const shared='data:text/javascript;base64,'+Buffer.from(readFileSync(new URL('../agent_world/web/commons-client.js',import.meta.url),'utf8')).toString('base64');
const source=readFileSync(new URL('../examples/frontier_web/frontier-client.js',import.meta.url),'utf8').replace("'/static/commons-client.js'",JSON.stringify(shared));
const {FrontierClient}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const values=new Map(),storage={getItem:k=>values.get(k)??null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};

// A response header is not completion: a lost body must retain the original roll operation.
const waiting=async(_path,{signal})=>({ok:true,status:200,json:()=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true}))});
const timed=new FrontierClient(waiting,storage,undefined,{timeoutMs:20});
timed.universe='living-frontier';timed.publicKey='synthetic-player';
await assert.rejects(timed.mutate('frontier.accept',{request_id:'careful-method'}),/回应超时/);
const operation=timed.pending();assert.ok(operation&&!operation.rejection);
await assert.rejects(timed.restoreRejected(),/outcome is still unknown/);
timed.fetcher=async(_path,options)=>{
  const body=JSON.parse(options.body);assert.deepEqual(body,operation.body);
  return {ok:true,status:200,json:async()=>({ok:true,function_id:'frontier.accept',operation_id:body.operation_id,replayed:true,result:{status:'executed'}})};
};
await timed.retry();assert.equal(timed.pending(),null);

// Leaving while a body is in flight preserves the operation for the returning page.
const cancelled=new FrontierClient(waiting,storage);cancelled.universe='other-living-frontier';cancelled.publicKey='synthetic-player';
const pendingSend=cancelled.mutate('frontier.check',{object_id:'market-cache',expected_object_version:1,expected_character_version:1});
await new Promise(resolve=>setImmediate(resolve));cancelled.cancelRequests();
await assert.rejects(pendingSend,error=>error.cancelled===true);
assert.ok(cancelled.pending()&&!cancelled.pending().rejection);

// A definite conflict can be cleared while both the original and newer drafts survive.
const conflict=new FrontierClient(async()=>({ok:false,status:409,json:async()=>({error:'StateConflict',message:'Scene changed; nothing committed'})}),storage);
conflict.universe='conflict-living-frontier';conflict.publicKey='synthetic-player';
conflict.saveDraft('frontier-intent','original idea');
await assert.rejects(conflict.mutate('frontier.request',{request_id:'idea',target_id:'market-cache',expected_target_version:1,text:'original idea'},{route:'frontier-intent',draft_text:'original idea'}));
assert.equal(conflict.pending().rejection.code,'StateConflict');
conflict.saveDraft('frontier-intent','newer idea');await conflict.restoreRejected();
assert.equal(conflict.pending(),null);assert.match(conflict.draft('frontier-intent'),/original idea/);assert.match(conflict.draft('frontier-intent'),/newer idea/);

// Cancellation during signing cannot quietly issue a credential afterwards.
const {createKey,encode}=await import(shared),key=await createKey();
const challenge={version:'agent-world-key-identity-v2',access_mode:'control',public_key:key.public_key,universe:'cancel-living-frontier',challenge_id:'synthetic',nonce:'nonce'};
challenge.message=encode(new TextEncoder().encode([challenge.version,challenge.universe,challenge.challenge_id,challenge.nonce,challenge.access_mode].join('\0')));
let releaseSign,signStarted;
const ready=new Promise(resolve=>{signStarted=resolve;}),originalSign=crypto.subtle.sign,calls=[];
try{
  crypto.subtle.sign=async(...args)=>{signStarted();await new Promise(resolve=>{releaseSign=resolve;});return originalSign.apply(crypto.subtle,args);};
  const client=new FrontierClient(async path=>{calls.push(path);return {ok:true,status:200,json:async()=>challenge};},storage);
  const connecting=client.authenticate(key);await ready;client.cancelRequests();releaseSign();
  await assert.rejects(connecting,error=>error.cancelled===true);
  assert.deepEqual(calls,['/v1/key-identities/challenges']);assert.equal(client.token,null);
}finally{crypto.subtle.sign=originalSign;}
console.log('Frontier client: body timeout, same-operation replay, leave cancellation, conflict draft recovery and cancelled signing passed');
