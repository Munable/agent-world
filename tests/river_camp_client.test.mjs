import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const commonsSource=readFileSync(new URL('../agent_world/web/commons-client.js',import.meta.url),'utf8');
const commonsUrl='data:text/javascript;base64,'+Buffer.from(commonsSource).toString('base64');
const source=readFileSync(new URL('../examples/river_camp_web/camp-client.js',import.meta.url),'utf8').replace("'/static/commons-client.js'",JSON.stringify(commonsUrl));
const {CampClient}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const store=new Map(), storage={getItem:k=>store.get(k)??null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)};
const rejected=new CampClient(async()=>({ok:false,status:409,json:async()=>({error:'StateConflict',message:'inventory changed'})}),storage);
rejected.universe='river';rejected.publicKey='synthetic-key';
const original=JSON.stringify({text:'original plan',note:'original private note'});
rejected.saveDraft('camp-intent',original);
await assert.rejects(rejected.mutate('camp.propose',{intent_id:'one',text:'original plan'},{route:'camp-intent',draft_text:original}));
assert.equal(rejected.pending().rejection.code,'StateConflict');
// Edits made after a failed send must remain valid structured drafts after restore.
rejected.saveDraft('camp-intent',JSON.stringify({text:'newer plan',note:'newer private note'}));
await rejected.restoreRejected();
assert.deepEqual(JSON.parse(rejected.draft('camp-intent')),{text:'newer plan\n\noriginal plan',note:'newer private note\n\noriginal private note'});
assert.equal(rejected.pending(),null);
// Uncertain transport results stay pending; they cannot be cleared as rejected.
rejected.fetcher=async()=>{throw new Error('response lost');};
await assert.rejects(rejected.mutate('camp.propose',{intent_id:'two',text:'plan'},{route:'camp-intent',draft_text:original}),/response lost/);
await assert.rejects(rejected.restoreRejected(),/outcome is still unknown/);
const unresolved=rejected.pending();assert.ok(unresolved&&!unresolved.rejection);
// An old rejection cannot mark a newer operation as failed.
let release;
const racing=new CampClient(()=>new Promise(resolve=>{release=resolve;}),storage);
racing.universe='other-river';racing.publicKey='synthetic-key';
const sending=racing.mutate('camp.accept',{intent_id:'old',expected_intent_version:1});
await new Promise(resolve=>setImmediate(resolve));
const replacement={name:'camp.accept',body:{operation_id:'new',arguments:{intent_id:'new',expected_intent_version:1}}};
storage.setItem(racing.pendingKey,JSON.stringify(replacement));
release({ok:false,status:403,json:async()=>({error:'PermissionDenied',message:'old access revoked'})});
await assert.rejects(sending);
assert.deepEqual(racing.pending(),replacement);
console.log('River Camp client: structured draft recovery, uncertain receipt retention and stale rejection isolation passed');

// Deadlines bound the whole response body, while cancellation preserves unknown writes.
const bodyWaiter=async(_path,{signal})=>({ok:true,status:200,json:()=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true}))});
const timed=new CampClient(bodyWaiter,storage,undefined,{timeoutMs:20});
timed.universe='timeout-river';timed.publicKey='synthetic-key';
await assert.rejects(timed.mutate('camp.accept',{intent_id:'waiting',expected_intent_version:1}),/回应超时/);
const retained=timed.pending();assert.ok(retained&&!retained.rejection);
const cancelled=new CampClient(bodyWaiter,storage);
cancelled.universe='cancel-river';cancelled.publicKey='synthetic-key';
const inFlight=cancelled.mutate('camp.accept',{intent_id:'cancelled',expected_intent_version:1});
await new Promise(resolve=>setImmediate(resolve));cancelled.cancelRequests();
await assert.rejects(inFlight,error=>error.cancelled===true);
assert.ok(cancelled.pending()&&!cancelled.pending().rejection);
timed.fetcher=async(_path,options)=>({ok:true,json:async()=>({ok:true,function_id:'camp.accept',operation_id:JSON.parse(options.body).operation_id,result:{executed:true}})});
await timed.retry();assert.equal(timed.pending(),null);
console.log('River Camp client: body timeout, leave cancellation and same-operation retry passed');

// Cancellation between a challenge and signing must not issue a later credential.
const {createKey,encode}=await import(commonsUrl), authKey=await createKey();
const challenge={version:'agent-world-key-identity-v1',public_key:authKey.public_key,universe:'cancel-camp',challenge_id:'synthetic',nonce:'nonce'};
challenge.message=encode(new TextEncoder().encode([challenge.version,challenge.universe,challenge.challenge_id,challenge.nonce].join('\0')));
let releaseSign, signStarted;
const signReady=new Promise(resolve=>{signStarted=resolve;}), originalSign=crypto.subtle.sign;
const authCalls=[];
try{
  crypto.subtle.sign=async(...args)=>{signStarted();await new Promise(resolve=>{releaseSign=resolve;});return originalSign.apply(crypto.subtle,args);};
  const connecting=new CampClient(async path=>{authCalls.push(path);return {ok:true,status:200,json:async()=>challenge};},storage);
  const authentication=connecting.authenticate(authKey);
  await signReady;connecting.cancelRequests();releaseSign();
  await assert.rejects(authentication,error=>error.cancelled===true);
  assert.deepEqual(authCalls,['/v1/key-identities/challenges']);
  assert.equal(connecting.token,null);
}finally{crypto.subtle.sign=originalSign;}
console.log('River Camp client: cancellation during signing prevents a later credential exchange');
