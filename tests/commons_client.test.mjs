import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
const source=readFileSync(new URL('../agent_world/web/commons-client.js',import.meta.url),'utf8');
const {createKey,validateKey,CommonsClient,CommonsHistory,parseRoute,encode}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const key=await createKey();assert.deepEqual(await validateKey(key),key);
const other=await createKey();await assert.rejects(validateKey({...key,public_key:other.public_key}));
await assert.rejects(validateKey({...key,version:'unknown'}));await assert.rejects(validateKey({...key,private_key:'not-a-secret'}));
const store=new Map();const storage={getItem:k=>store.get(k) ?? null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)};
const calls=[];let lose=true;
const client=new CommonsClient(async(path,options)=>{calls.push({path,...options});if(lose){lose=false;throw new Error('response lost after commit');}return {ok:true,json:async()=>({ok:true,function_id:'commons.post.create',operation_id:JSON.parse(options.body).operation_id,result:{post_id:'p'}})};},storage);
client.publicKey=key.public_key;client.token='fixture-token';
await assert.rejects(client.mutate('commons.post.create',{post_id:'p',text:'test'}),/response lost/);
const pending=client.pending();assert.ok(pending.body.operation_id);assert.ok(!JSON.stringify([...store]).includes('fixture-token'));
await assert.rejects(client.mutate('commons.post.create',{post_id:'p2',text:'another'}),/previous submission/);
const restored=new CommonsClient(client.fetcher,storage);restored.publicKey=key.public_key;restored.token='new-fixture-token';
await restored.retry();assert.equal(calls[0].body,calls[1].body);assert.equal(restored.pending(),null);
// Definitive rejection keeps its original text until an explicit safe restore.
const rejected=new CommonsClient(async()=>({ok:false,status:422,json:async()=>({error:'InvalidArguments',message:'bad input'})}),storage);rejected.publicKey=key.public_key;
rejected.universe='commons';
await assert.rejects(rejected.mutate('commons.post.create',{post_id:'bad',text:'original'}, {route:'posts:',draft_text:' original '}));assert.equal(rejected.pending().rejection.code,'InvalidArguments');
assert.deepEqual(await rejected.restoreRejected(),{route:'posts:',text:' original '});assert.equal(rejected.pending(),null);
rejected.token='expired-fixture-token';rejected.fetcher=async()=>({ok:false,status:401,json:async()=>({message:'expired'})});
await assert.rejects(rejected.mutate('commons.post.create',{post_id:'p',text:'valid'}));assert.ok(rejected.pending());assert.equal(rejected.token,null);
rejected.publicKey=other.public_key;assert.equal(rejected.pending(),null);
// A browser must not sign arbitrary content returned by a malicious challenge endpoint.
const tampered=new CommonsClient(async()=>({ok:true,json:async()=>({version:'agent-world-key-identity-v2',access_mode:'control',public_key:key.public_key,challenge_id:'id',universe:'commons',nonce:'nonce',message:encode(new TextEncoder().encode('sign this unrelated text'))})}),storage);
await assert.rejects(tampered.authenticate(key),/invalid identity challenge/);
assert.deepEqual(parseRoute('#post/p-1'),{page:'post',id:'p-1'});assert.deepEqual(parseRoute('#messages/c1'),{page:'messages',id:'c1'});assert.deepEqual(parseRoute('#post/<img>'),{page:'posts',id:null});
console.log('Commons client: key recovery, signature validation, durable same-operation retry, identity isolation and route validation passed');

for (const badResponse of [
  {ok:true,json:async()=>{throw new SyntaxError('truncated');}},
  {ok:true,json:async()=>({})},
  {ok:true,json:async()=>({ok:true,operation_id:'wrong',function_id:'commons.post.create',result:{}})},
]) {
  const isolated=new Map();const isolatedStorage={getItem:k=>isolated.get(k)??null,setItem:(k,v)=>isolated.set(k,v),removeItem:k=>isolated.delete(k)};
  const incomplete=new CommonsClient(async()=>badResponse,isolatedStorage);incomplete.publicKey=key.public_key;
  await assert.rejects(incomplete.mutate('commons.post.create',{post_id:'p',text:'x'}));assert.ok(incomplete.pending());
}
console.log('Truncated 200 bodies and incomplete/mismatched receipts retain the original operation');

for (const code of ['PermissionDenied','RoleInactive','FunctionNotFound']) {
  rejected.publicKey=key.public_key;
  rejected.fetcher=async()=>({ok:false,status:403,json:async()=>({error:code,message:'access changed'})});
  await assert.rejects(rejected.retry());assert.ok(rejected.pending());
}
console.log('Access changes retain unresolved operations');

rejected.universe='different-world';assert.equal(rejected.pending(),null);
console.log('Pending writes are scoped to the world as well as the public key');

// Typing, reload, navigation, session expiry and leaving retain local drafts;
// world/key/route boundaries prevent another identity from seeing private text.
const draftStore=new Map();const draftStorage={getItem:k=>draftStore.get(k) ?? null,setItem:(k,v)=>draftStore.set(k,v),removeItem:k=>draftStore.delete(k)};
const drafts=new CommonsClient(async()=>({ok:false,status:401,json:async()=>({message:'expired'})}),draftStorage);
drafts.universe='commons';drafts.publicKey=key.public_key;drafts.token='secret-fixture-token';
drafts.saveDraft('posts:',' unsent post ');drafts.saveDraft('messages:c1','private unsent message');
const returned=new CommonsClient(drafts.fetcher,draftStorage);returned.universe='commons';returned.publicKey=key.public_key;
assert.equal(returned.draft('posts:'),' unsent post ');assert.equal(returned.draft('messages:c1'),'private unsent message');
returned.publicKey=other.public_key;assert.equal(returned.draft('messages:c1'),'');returned.publicKey=key.public_key;
returned.universe='other-world';assert.equal(returned.draft('messages:c1'),'');returned.universe='commons';
await assert.rejects(drafts.mutate('commons.message.send',{conversation_id:'c1',message_id:'m1',text:'private unsent message'},{route:'messages:c1',draft_text:'private unsent message'}));
assert.equal(drafts.token,null);assert.equal(drafts.draft('messages:c1'),'private unsent message');
await assert.rejects(drafts.restoreRejected(),/outcome is still unknown/);assert.ok(drafts.pending());
returned.token='fresh-fixture-token';returned.fetcher=async(_path,options)=>({ok:true,json:async()=>({ok:true,function_id:'commons.message.send',operation_id:JSON.parse(options.body).operation_id,result:{message_id:'m1'}})});
// A newer edit made while the original was sending must survive a successful retry.
returned.saveDraft('messages:c1','newer edit');await returned.retry();assert.equal(returned.draft('messages:c1'),'newer edit');
await returned.mutate('commons.message.send',{conversation_id:'c1',message_id:'m2',text:'newer edit'},{route:'messages:c1',draft_text:'newer edit'});assert.equal(returned.draft('messages:c1'),'');
assert.ok(!JSON.stringify([...draftStore]).includes('secret-fixture-token'));assert.ok(!JSON.stringify([...draftStore]).includes('fresh-fixture-token'));
console.log('Drafts survive return/expiry, respect identity and route boundaries, and clear only the submitted text');

returned.fetcher=async()=>({ok:false,status:404,json:async()=>({error:'ResourceNotFound',message:'post not found'})});
await assert.rejects(returned.mutate('commons.reply.create',{post_id:'missing',reply_id:'r1',text:'reply text'},{route:'post:missing',draft_text:'reply text'}));
assert.equal(returned.pending().rejection.code,'ResourceNotFound');
returned.saveDraft('post:missing','new draft');assert.deepEqual(await returned.restoreRejected(),{route:'post:missing',text:'new draft\n\nreply text'});assert.equal(returned.pending(),null);
console.log('Known missing-resource rejection safely unlocks submission and preserves both original and newer text');

const history=new CommonsHistory('messages','message_id');
history.merge({messages:[{message_id:'m3',created_at:3},{message_id:'m2',created_at:2}],has_more:true,next_cursor:'older-2'});
history.merge({messages:[{message_id:'m2',created_at:2},{message_id:'m1',created_at:1}],has_more:true,next_cursor:'older-1'},true);
history.merge({messages:[{message_id:'m4',created_at:4},{message_id:'m3',created_at:3}],has_more:true,next_cursor:'head-3'});
assert.deepEqual(history.items.map(m=>m.message_id),['m4','m3','m2','m1']);assert.equal(history.cursor,'older-1');assert.equal(history.hasMore,true);
history.merge({messages:[{message_id:'m0',created_at:0}],has_more:false,next_cursor:null},true);
history.merge({messages:[{message_id:'m5',created_at:5},{message_id:'m4',created_at:4}],has_more:true,next_cursor:'head-5'});
assert.equal(history.hasMore,false);assert.equal(history.cursor,null);assert.equal(history.items.length,6);
assert.throws(()=>history.merge({messages:[],has_more:true,next_cursor:null}),/incomplete history/);
const ties=new CommonsHistory('posts','post_id');ties.merge({posts:[{post_id:'a_',created_at:1},{post_id:'a-',created_at:1}],has_more:false,next_cursor:null});assert.deepEqual(ties.items.map(p=>p.post_id),['a_','a-']);
console.log('Paged history deduplicates overlaps, preserves stable order and retains the older cursor across refreshes');
const gap=new CommonsHistory('posts','post_id');const page=(start,end)=>Array.from({length:end-start+1},(_,i)=>({post_id:'p'+(start+i),created_at:start+i}));
gap.merge({posts:page(1,100),has_more:false,next_cursor:null});
gap.merge({posts:page(201,300),has_more:true,next_cursor:'before-201'});
assert.equal(gap.cursor,'before-201');assert.equal(gap.hasMore,true);assert.equal(gap.items.length,200);
gap.merge({posts:page(101,200),has_more:true,next_cursor:'before-101'},true);
gap.merge({posts:page(1,100),has_more:false,next_cursor:null},true);
assert.equal(gap.items.length,300);assert.equal(gap.items[0].post_id,'p300');assert.equal(gap.items.at(-1).post_id,'p1');assert.equal(gap.hasMore,false);
console.log('A head refresh with no overlap reopens pagination to recover an offline history gap');

for(const broken of [false,true]){
  let finish;
  const renewed=new CommonsClient(()=>new Promise(resolve=>{finish=resolve;}),draftStorage);renewed.token='old-access';
  const oldRead=renewed.request('/v1/bootstrap');
  renewed.fetcher=async path=>({ok:true,json:async()=>path.endsWith('/challenges') ? {version:'agent-world-key-identity-v2',access_mode:'control',public_key:key.public_key,universe:'commons',challenge_id:'fresh-id',nonce:'fresh-nonce',message:encode(new TextEncoder().encode(['agent-world-key-identity-v2','commons','fresh-id','fresh-nonce','control'].join('\0')))} : {version:'agent-world-key-identity-v2',public_key:key.public_key,identity:{token:'fresh-access',token_id:'fresh-id',universe:'commons',role_id:'same-role',access_mode:'control',expires_at:2000000000},role_profile:{role_id:'same-role'}}});
  await renewed.authenticate(key);
  finish({ok:false,status:401,json:async()=>{if(broken)throw new SyntaxError('broken body');return {error:'InvalidIdentityToken',message:'old access expired'};}});
  await assert.rejects(oldRead,error=>error.status===401 && error.staleAuth===true);assert.equal(renewed.token,'fresh-access');
}
console.log('Late expired-access responses, including truncated bodies, preserve newly renewed credentials');

for(const rejectedOld of [false,true]){
  const shared=new Map();const sharedStorage={getItem:k=>shared.get(k) ?? null,setItem:(k,v)=>shared.set(k,v),removeItem:k=>shared.delete(k)};
  let late;
  const tabA=new CommonsClient(()=>new Promise(resolve=>{late=resolve;}),sharedStorage);const tabB=new CommonsClient(async()=>{throw new Error('new response lost');},sharedStorage);
  for(const tab of [tabA,tabB]){tab.universe='commons';tab.publicKey=key.public_key;}
  const old={name:'commons.post.create',body:{operation_id:'old-operation',arguments:{post_id:'old',text:'old text'}},route:'posts:',draft_text:'old text'};
  sharedStorage.setItem(tabA.pendingKey,JSON.stringify(old));const oldRetry=tabA.retry();
  sharedStorage.removeItem(tabB.pendingKey);tabB.saveDraft('posts:','new text');
  await assert.rejects(tabB.mutate('commons.post.create',{post_id:'new',text:'new text'},{route:'posts:',draft_text:'new text'}),/new response lost/);
  const newest=tabB.pending();
  late(rejectedOld ? {ok:false,status:404,json:async()=>({error:'ResourceNotFound',message:'old target is gone'})} : {ok:true,json:async()=>({ok:true,function_id:old.name,operation_id:old.body.operation_id,result:{post_id:'old'}})});
  if(rejectedOld)await assert.rejects(oldRetry);else await oldRetry;
  assert.deepEqual(tabB.pending(),newest);assert.equal(tabB.draft('posts:'),'new text');
}
console.log('A late success or rejection from another tab cannot clear or overwrite a newer unresolved operation');

const lockStore=new Map();const lockedStorage={getItem:k=>lockStore.get(k) ?? null,setItem:(k,v)=>lockStore.set(k,v),removeItem:k=>lockStore.delete(k)};
let lockTail=Promise.resolve(),sends=0;
const locks={request:(_name,fn)=>{const result=lockTail.then(fn);lockTail=result.catch(()=>{});return result;}};
const tabs=[0,1].map(()=>{const tab=new CommonsClient(async()=>{sends++;throw new Error('response lost');},lockedStorage,locks);tab.universe='commons';tab.publicKey=key.public_key;return tab;});
const simultaneous=await Promise.allSettled(tabs.map((tab,index)=>tab.mutate('commons.post.create',{post_id:'p'+index,text:'text'+index})));
assert.equal(sends,1);assert.ok(simultaneous.every(result=>result.status==='rejected'));assert.ok(tabs[0].pending());
console.log('A shared pending lock admits only one new operation from simultaneous same-identity tabs');

// The user-triggered private-message action must not treat a recent-page miss
// as proof that no historical conversation exists.
const conversation=(id,peer,created_at=1)=>({conversation_id:id,participant_role_ids:['self',peer],created_at});
const conversations=(rows,has_more=false,next_cursor=null)=>({conversations:rows,has_more,next_cursor});
function conversationFixture(pages,{locks}={}){
  const records=new Map(),reads=[],opens=[];const fixtureStorage={getItem:k=>records.get(k) ?? null,setItem:(k,v)=>records.set(k,v),removeItem:k=>records.delete(k)};
  const fixture=new CommonsClient(async(path,options)=>{
    const body=JSON.parse(options.body);
    if(path.endsWith('/commons.conversation.list/invoke')){
      reads.push(body.arguments);const result=pages.shift();if(result instanceof Error)throw result;if(result?.status)return {ok:false,status:result.status,json:async()=>({error:'InvalidIdentityToken',message:'access expired'})};
      assert.ok(result,'lookup must stop at a matching conversation or a definite end');return {ok:true,json:async()=>({result})};
    }
    assert.ok(path.endsWith('/commons.conversation.open/invoke'),'lookup must not read message history');opens.push(body);
    return {ok:true,json:async()=>({ok:true,function_id:'commons.conversation.open',operation_id:body.operation_id,result:{...body.arguments,participant_role_ids:['self',body.arguments.peer_role_id]}})};
  },fixtureStorage,locks);fixture.publicKey=key.public_key;fixture.universe='commons';fixture.token='conversation-access';return {fixture,reads,opens};
}
const secondPage=conversationFixture([
  conversations([conversation('recent-other','other',3)],true,'older-page'),
  conversations([conversation('target-latest','target',2),conversation('target-earlier','target',1)],true,'unneeded-page'),
]);const lookedUp=new CommonsHistory('conversations','conversation_id');
assert.equal((await secondPage.fixture.openConversation('target',{onPage:(page,older)=>lookedUp.merge(page,older)})).conversation_id,'target-latest');
assert.deepEqual(secondPage.reads,[{limit:100},{limit:100,after:'older-page'}]);assert.equal(secondPage.opens.length,0);assert.equal(lookedUp.items.length,3);
const firstPage=conversationFixture([conversations([conversation('newest-target','target',4),conversation('older-target','target',2)],true,'do-not-read')]);
assert.equal((await firstPage.fixture.openConversation('target')).conversation_id,'newest-target');assert.equal(firstPage.reads.length,1);assert.equal(firstPage.opens.length,0);
const newPeer=conversationFixture([conversations([conversation('unrelated','other')],true,'tail'),conversations([])]);
await newPeer.fixture.openConversation('new-peer');assert.equal(newPeer.reads.length,2);assert.equal(newPeer.opens.length,1);assert.equal(newPeer.opens[0].arguments.peer_role_id,'new-peer');assert.ok(newPeer.opens[0].operation_id);
console.log('Private-message lookup resumes the latest matching historical conversation and creates only after an explicit end');

for(const brokenPages of [
  [conversations([],true,'empty-progress')],
  [conversations([conversation('a','other')],true,null)],
  [conversations([conversation('a','other')],true,123)],
  [conversations([conversation('a','other')],true,'loop'),conversations([conversation('b','other')],true,'loop')],
  [conversations([conversation('a','other')],true,'page-2'),conversations([conversation('a','other')],false)],
  [conversations([conversation('a','other')],false,'contradictory-cursor')],
  [{conversations:[],next_cursor:null}],
  [conversations([conversation('a','other')],true,'page-2'),new Error('network interrupted')],
  [conversations([conversation('a','other')],true,'page-2'),{status:401}],
]){
  const blocked=conversationFixture(brokenPages);await assert.rejects(blocked.fixture.openConversation('target'));assert.equal(blocked.opens.length,0);assert.equal(blocked.fixture.pending(),null);
}
console.log('Failed, expired, incomplete or non-advancing conversation reads never create a replacement conversation');

let currentSession=true;
let finishLookup;
const inFlight=conversationFixture([]);inFlight.fixture.fetcher=()=>new Promise(resolve=>{finishLookup=resolve;});
const abandonedLookup=inFlight.fixture.openConversation('target',{isCurrent:()=>currentSession});currentSession=false;
finishLookup({ok:true,json:async()=>({result:conversations([])})});
await assert.rejects(abandonedLookup,error=>error.cancelled===true);assert.equal(inFlight.fixture.pending(),null);
currentSession=true;
const changed=conversationFixture([conversations([])]);
await assert.rejects(changed.fixture.openConversation('target',{isCurrent:()=>currentSession,onPage:()=>{currentSession=false;}}),error=>error.cancelled===true);
assert.equal(changed.opens.length,0);assert.equal(changed.fixture.pending(),null);
currentSession=true;
const beforePublish=conversationFixture([conversations([])],{locks:{request:async(_name,fn)=>{currentSession=false;return fn();}}});
await assert.rejects(beforePublish.fixture.openConversation('target',{isCurrent:()=>currentSession}),error=>error.cancelled===true);
assert.equal(beforePublish.opens.length,0);assert.equal(beforePublish.fixture.pending(),null);
currentSession=true;
const lockChange=conversationFixture([conversations([])],{locks:{request:async(_name,fn)=>{const result=fn();currentSession=false;return result;}}});
await assert.rejects(lockChange.fixture.openConversation('target',{isCurrent:()=>currentSession}),error=>error.cancelled===true);
assert.equal(lockChange.opens.length,0);assert.equal(lockChange.fixture.pending().name,'commons.conversation.open');

// A published operation is shared. Another tab can send it before this page
// notices Leave; deleting it would lose recovery after that response is lost.
currentSession=true;
let otherTabSent=false,otherTab;
const sharedPrepared=conversationFixture([conversations([])],{locks:{request:async(_name,fn)=>{
  const result=fn();
  await assert.rejects(otherTab.retry(),/shared response lost/);currentSession=false;return result;
}}});
otherTab=new CommonsClient(async(_path,options)=>{otherTabSent=true;assert.equal(JSON.parse(options.body).operation_id,sharedPrepared.fixture.pending().body.operation_id);throw new Error('shared response lost');},sharedPrepared.fixture.storage);
otherTab.publicKey=key.public_key;otherTab.universe='commons';otherTab.token='other-page-access';
await assert.rejects(sharedPrepared.fixture.openConversation('target',{isCurrent:()=>currentSession}),error=>error.cancelled===true);
assert.equal(otherTabSent,true);assert.equal(sharedPrepared.opens.length,0);assert.ok(otherTab.pending());
const published=otherTab.pending();assert.equal(published.name,'commons.conversation.open');
otherTab.fetcher=async(_path,options)=>({ok:true,json:async()=>({ok:true,function_id:published.name,operation_id:JSON.parse(options.body).operation_id,result:{conversation_id:published.body.arguments.conversation_id}})});
await otherTab.retry();assert.equal(otherTab.pending(),null);
console.log('A changed session stops this page; published operations stay recoverable if another tab has sent them');

// Browser fetch is a receiver-sensitive Web API. A class property call must not
// accidentally pass the CommonsClient as its receiver (Illegal invocation).
const originalFetch=globalThis.fetch;
try {
  globalThis.fetch=function(path,options) {
    assert.equal(this,globalThis,'native fetch must be invoked with its global receiver');
    return Promise.resolve({ok:true,json:async()=>({universe:'commons'})});
  };
  const nativeClient=new CommonsClient(undefined,storage);
  assert.deepEqual(await nativeClient.request('/api/config'),{universe:'commons'});
} finally {globalThis.fetch=originalFetch;}
console.log('Default native fetch retains its browser-global receiver');

// Execute the actual page controller with only the DOM operations it uses.
// This keeps route failure assertions behavioral, including saved draft state.
class PageNode {
  constructor(tag='div'){this.tag=tag;this.attributes={};this.children=[];this.textContent='';this.value='';this.listeners=new Map();this.classList={toggle(){}};}
  setAttribute(name,value){this.attributes[name]=value;}
  append(...nodes){for(const node of nodes)this.children.push(...(node.tag==='fragment' ? node.children : [node]));}
  replaceChildren(...nodes){this.children=[];this.textContent='';this.append(...nodes);}
  addEventListener(name,listener){this.listeners.set(name,listener);}
  contains(node){return this===node || this.children.some(child=>child.contains(node));}
  find(tag){if(this.tag===tag)return this;for(const child of this.children){const found=child.find(tag);if(found)return found;}return null;}
  all(){return [this,...this.children.flatMap(child=>child.all())];}
  get text(){return [this.textContent,...this.children.map(child=>child.text)].join(' ');}
  querySelectorAll(tag){return this.all().filter(node=>node.tag===tag);}
  showModal(){this.open=true;}
  close(){this.open=false;}
}
function pageFixture({externalFetcher}={}){
  const nodes=new Map(),events=new Map(),records=new Map();
  const fixtureStorage={getItem:k=>records.get(k)??null,setItem:(k,v)=>records.set(k,v),removeItem:k=>records.delete(k)};
  const node=id=>{if(!nodes.has(id))nodes.set(id,new PageNode());return nodes.get(id);};
  const document={activeElement:null,hidden:false,createElement:tag=>new PageNode(tag),createDocumentFragment:()=>new PageNode('fragment'),querySelector:selector=>{const [id,tag]=selector.split(' ');return tag ? node(id).find(tag) : node(id);},querySelectorAll:()=>[...nodes.values()].flatMap(n=>n.all()).filter(n=>n.tag==='button' && n.attributes.type==='submit')};
  node('#join-form').append(new PageNode('button'));
  class PageClient extends CommonsClient {constructor(){super((path,options)=>path==='/api/config' ? new Promise(()=>{}) : externalFetcher ? externalFetcher(path,options) : new Promise(()=>{}),fixtureStorage);}}
  const location={hash:'#posts',origin:'http://localhost'};
  const controller=readFileSync(new URL('../agent_world/web/commons.js',import.meta.url),'utf8').replace(/^import[^\n]*\n/,'');
  const app=runInNewContext(controller+'\n({client,render,downloads:[],activate(){profile={role_id:"self",display_name:"Self"};client.publicKey="page-key";client.universe="commons";client.token="page-access";client.tokenId="page-token-id";client.roleId="self";},setKey(value){key=value;},captureDownloads(records){download=(name,data)=>records.push({name,data});}})',{CommonsClient:PageClient,CommonsHistory,createKey,validateKey,savedKey:()=>{},parseRoute,document,location,crypto,AbortController,URL,sessionStorage:fixtureStorage,window:{addEventListener:(name,listener)=>events.set(name,listener)},setTimeout:()=>0,clearTimeout(){}});
  app.activate();return {...app,node,location,navigate(hash){location.hash=hash;return events.get('hashchange')();}};
}
const pageApp=pageFixture();
const pagePost=(id,text)=>({post:{post_id:id,author_role_id:'self',text,created_at:1}});
const pageHistory={replies:[],has_more:false,next_cursor:null};
pageApp.client.query=async(name,args)=>name==='commons.post.get' ? pagePost(args.post_id,'Original post body') : pageHistory;
await pageApp.navigate('#post/original');
pageApp.node('#page').find('textarea').value=' unsent original reply ';
let failMissing;
pageApp.client.query=async name=>name==='commons.post.get' ? new Promise((_resolve,reject)=>{failMissing=reject;}) : pageHistory;
const missingRoute=pageApp.navigate('#post/missing');
assert.equal(pageApp.client.draft('post:original'),' unsent original reply ');
assert.equal(pageApp.node('#page').find('form'),null,'a pending new route must remove the previous submission form');
assert.doesNotMatch(pageApp.node('#page').text,/Original post body/);
assert.match(pageApp.node('#freshness').text,/Reading/);
failMissing(Object.assign(new Error('post not found'),{status:404,client:pageApp.client}));await missingRoute;
assert.equal(pageApp.node('#page').find('form'),null);
assert.match(pageApp.node('#page').text,/could not be opened/);
assert.equal(pageApp.node('#page').find('a').attributes.href,'#posts');
assert.doesNotMatch(pageApp.node('#page').text,/Original post body/);
assert.equal(pageApp.client.draft('post:original'),' unsent original reply ');

pageApp.client.query=async(name,args)=>name==='commons.post.get' ? pagePost(args.post_id,'Original post body') : pageHistory;
await pageApp.navigate('#post/original');
assert.equal(pageApp.node('#page').find('textarea').value,' unsent original reply ');
const beforeRefresh=pageApp.node('#page').children;
pageApp.client.query=async()=>{throw new Error('temporarily offline');};await pageApp.render(false);
assert.equal(pageApp.node('#page').children,beforeRefresh,'same-route background failure must retain its current content and draft');
assert.match(pageApp.node('#page').text,/Original post body/);
assert.equal(pageApp.node('#page').find('textarea').value,' unsent original reply ');

let failSlow;
pageApp.client.query=async name=>name==='commons.post.get' ? new Promise((_resolve,reject)=>{failSlow=reject;}) : pageHistory;
const slowRoute=pageApp.navigate('#post/slow');
pageApp.client.query=async(name,args)=>name==='commons.post.get' ? pagePost(args.post_id,'Latest route body') : pageHistory;
await pageApp.navigate('#post/latest');
failSlow(new Error('late abandoned route failure'));await slowRoute;
assert.match(pageApp.node('#page').text,/Latest route body/);
assert.ok(pageApp.node('#page').find('form'));
assert.equal(pageApp.node('#error').hidden,true);

pageApp.node('#page').find('textarea').value='latest draft';
pageApp.client.query=async name=>{if(name==='commons.conversation.list')return {conversations:[],has_more:false,next_cursor:null};throw Object.assign(new Error('conversation is unavailable'),{status:403,client:pageApp.client});};
await pageApp.navigate('#messages/unavailable');
assert.equal(pageApp.node('#page').find('form'),null);
assert.match(pageApp.node('#page').text,/private conversation could not be opened/);
assert.equal(pageApp.node('#page').find('a').attributes.href,'#messages');
assert.equal(pageApp.client.token,'page-access');
pageApp.client.query=async name=>{if(name==='commons.conversation.list')return {conversations:[],has_more:false,next_cursor:null};throw Object.assign(new Error('access expired'),{status:401,client:pageApp.client});};
await pageApp.navigate('#messages/private');
assert.equal(pageApp.client.token,null);
assert.equal(pageApp.node('#session-expired').hidden,false);
assert.equal(pageApp.node('#refresh-button').disabled,true);
assert.equal(pageApp.node('#page').find('form'),null);
assert.match(pageApp.node('#freshness').text,/Session expired/);
assert.equal(pageApp.client.draft('post:latest'),'latest draft');

pageApp.activate();let finishAfterLeave;
pageApp.client.query=async name=>name==='commons.post.get' ? new Promise(resolve=>{finishAfterLeave=resolve;}) : pageHistory;
const leavingRoute=pageApp.navigate('#post/leaving');
pageApp.node('#logout-button').listeners.get('click')();
finishAfterLeave(pagePost('leaving','Late content after Leave'));await leavingRoute;
assert.equal(pageApp.node('#world').hidden,true);
assert.equal(pageApp.node('#page').find('form'),null);
assert.doesNotMatch(pageApp.node('#page').text,/Late content after Leave/);
console.log('Commons page: route reads remove stale submission forms, failed routes offer safe navigation, drafts survive, same-route refresh content and session/generation recovery remain intact');

function accessChallenge(accessMode='control'){
  const challenge={version:'agent-world-key-identity-v2',public_key:key.public_key,universe:'commons',challenge_id:'access-proof',nonce:'access-nonce',access_mode:accessMode};
  challenge.message=encode(new TextEncoder().encode([challenge.version,challenge.universe,challenge.challenge_id,challenge.nonce,accessMode].join('\0')));return challenge;
}
function accessExchange(accessMode='control'){
  return {version:'agent-world-key-identity-v2',public_key:key.public_key,identity:{token:'issued-secret',token_id:'issued-token-id',universe:'commons',role_id:'self',access_mode:accessMode,created_at:1,expires_at:2000000000},role_profile:{role_id:'self'},mcp:{url:'/mcp',authorization_scheme:'Bearer',authorization_token:'issued-secret'}};
}
const success=data=>({ok:true,status:200,json:async()=>data});
const proofPublic=await crypto.subtle.importKey('raw',Uint8Array.from(Buffer.from(key.public_key,'base64url')),{name:'Ed25519'},false,['verify']);
for(const mode of ['control','observe']){
  const paths=[],challenge=accessChallenge(mode);
  const scoped=new CommonsClient(async(path,options)=>{
    paths.push(path);const body=JSON.parse(options.body);
    if(path.endsWith('/challenges')){assert.equal(body.access_mode,mode);return success(challenge);}
    assert.deepEqual(Object.keys(body).sort(),['challenge_id','signature']);
    assert.ok(await crypto.subtle.verify('Ed25519',proofPublic,Uint8Array.from(Buffer.from(body.signature,'base64url')),Uint8Array.from(Buffer.from(challenge.message,'base64url'))));
    return success(accessExchange(mode));
  },storage);
  await scoped.authenticate(key,{accessMode:mode});assert.equal(scoped.accessMode,mode);assert.equal(scoped.token,'issued-secret');assert.equal(paths.length,2);
}
let invalidModeCalls=0;
const invalidMode=new CommonsClient(async()=>{invalidModeCalls++;},storage);
await assert.rejects(invalidMode.authenticate(key,{accessMode:'owner'}),/Read and act or Read only/);assert.equal(invalidModeCalls,0);
for(const mismatch of ['challenge-mode','signed-mode','exchange-mode','exchange-world','exchange-key','exchange-version']){
  const paths=[];
  const mismatched=new CommonsClient(async path=>{
    paths.push(path);
    if(path.endsWith('/challenges')){
      const challenge=accessChallenge('observe');if(mismatch==='challenge-mode')challenge.access_mode='control';if(mismatch==='signed-mode')challenge.message=accessChallenge('control').message;return success(challenge);
    }
    const result=accessExchange('observe');if(mismatch==='exchange-mode')result.identity.access_mode='control';if(mismatch==='exchange-world')result.identity.universe='other-world';if(mismatch==='exchange-key')result.public_key=other.public_key;if(mismatch==='exchange-version')result.version='agent-world-key-identity-v1';return success(result);
  },storage);
  await assert.rejects(mismatched.authenticate(key,{accessMode:'observe'}),/identity challenge|returned access/);assert.equal(mismatched.token,null);
  if(mismatch.startsWith('challenge') || mismatch.startsWith('signed'))assert.deepEqual(paths,['/v1/key-identities/challenges']);
}
console.log('Access proof: both permission choices are signed and granted exactly; challenge/message/exchange scope mismatches are rejected');

const credential=(token_id,access_mode='control',status='active')=>({token_id,universe:'commons',role_id:'self',access_mode,created_at:1,expires_at:2000000000,revoked_at:status==='revoked'?2:null,is_current:token_id==='page-token-id',status});
const credentialList=(credentials=[credential('page-token-id'),credential('external-read-token','observe')])=>({universe:'commons',role_id:'self',current_token_id:'page-token-id',credentials});
const controlsClient=new CommonsClient(async()=>success(credentialList([{...credential('page-token-id'),token:'should-never-render'},{...credential('external-read-token','observe'),authorization_token:'should-never-render'}])),storage);
controlsClient.universe='commons';controlsClient.roleId='self';controlsClient.tokenId='page-token-id';controlsClient.token='page-access';
const listed=await controlsClient.listCredentials();assert.equal(listed.credentials.length,2);assert.ok(!JSON.stringify(listed).includes('should-never-render'));
controlsClient.fetcher=async()=>success({...credentialList(),role_id:'another-role'});await assert.rejects(controlsClient.listCredentials(),/invalid access list/);
controlsClient.fetcher=async()=>success(credentialList([credential('external-read-token','observe')]));await assert.rejects(controlsClient.listCredentials(),/incomplete access list/);
let revokeCalls=0;
controlsClient.fetcher=async(path,options)=>{revokeCalls++;assert.equal(path,'/api/me/credentials/external-read-token');assert.equal(options.method,'DELETE');return success({token_id:'external-read-token',revoked:true});};
await assert.rejects(controlsClient.revokeCredential('page-token-id'),/Keep this page connected/);assert.equal(revokeCalls,0);
await controlsClient.revokeCredential('external-read-token');assert.equal(revokeCalls,1);assert.equal(controlsClient.token,'page-access');
controlsClient.fetcher=async()=>({ok:false,status:403,json:async()=>({error:'PermissionDenied',message:'revocation denied'})});await assert.rejects(controlsClient.revokeCredential('external-read-token'),/revocation denied/);
controlsClient.fetcher=async()=>success({token_id:'different-token',revoked:true});await assert.rejects(controlsClient.revokeCredential('external-read-token'),/did not confirm/);
console.log('Credential control: scoped metadata excludes bearers; per-item revocation checks confirmation and refuses the current page');

let accountHandler=async()=>success(credentialList()),uiRequests=[];
const accountApp=pageFixture({externalFetcher:async(path,options)=>{uiRequests.push({path,options});return accountHandler(path,options);}});
accountApp.setKey(key);accountApp.captureDownloads(accountApp.downloads);
await accountApp.node('#account-button').listeners.get('click')();
assert.match(accountApp.node('#credential-list').text,/This page/);assert.match(accountApp.node('#credential-list').text,/Read only · active/);assert.match(accountApp.node('#credential-list').text,/expires/);assert.equal(accountApp.node('#credential-list').querySelectorAll('button').length,1);
accountApp.client.saveDraft('posts:','preserved while managing access');
accountHandler=async path=>path.startsWith('/api/me/credentials/') ? ({ok:false,status:403,json:async()=>({message:'revocation denied'})}) : success(credentialList());
await accountApp.node('#credential-list').find('button').listeners.get('click')();
assert.match(accountApp.node('#account-error').text,/revocation denied/);assert.equal(accountApp.node('#revoke-result').text,'');assert.match(accountApp.node('#credential-list').text,/Read only · active/);
assert.equal(accountApp.client.draft('posts:'),'preserved while managing access');assert.equal(accountApp.client.token,'page-access');
accountHandler=async path=>path.startsWith('/api/me/credentials/') ? success({token_id:'external-read-token',revoked:true}) : success(credentialList([credential('page-token-id'),credential('external-read-token','observe','revoked')]));
await accountApp.node('#credential-list').find('button').listeners.get('click')();assert.match(accountApp.node('#revoke-result').text,/revoked/);assert.match(accountApp.node('#credential-list').text,/Read only · revoked/);assert.equal(accountApp.node('#credential-list').querySelectorAll('button').length,0);
assert.equal(accountApp.node('#account-error').hidden,true);

let finishDownload,challengeCalls=0;
accountApp.node('#connection-access-mode').value='observe';
accountHandler=async(path,options)=>{
  if(path.endsWith('/challenges')){challengeCalls++;assert.equal(JSON.parse(options.body).access_mode,'observe');return success(accessChallenge('observe'));}
  if(path.endsWith('/exchange'))return new Promise(resolve=>{finishDownload=resolve;});return success(credentialList());
};
const firstDownload=accountApp.node('#config-button').listeners.get('click')();
await accountApp.node('#config-button').listeners.get('click')();
while(!finishDownload)await new Promise(resolve=>setImmediate(resolve));
assert.equal(challengeCalls,1);assert.equal(accountApp.node('#connection-access-mode').disabled,true);
finishDownload(success(accessExchange('observe')));await firstDownload;
assert.equal(accountApp.downloads.length,1);assert.equal(accountApp.downloads[0].data.mcp.commons.headers.Authorization,'Bearer issued-secret');assert.equal(accountApp.client.token,'page-access');assert.equal(accountApp.node('#config-button').disabled,false);
assert.equal(accountApp.downloads[0].name,'agent-world-opencode-observe-token-id.json');assert.match(accountApp.node('#connect-command').textContent,/observe-token-id\.json/);
assert.ok(!accountApp.node('#credential-list').text.includes('issued-secret'));

let finishList;
accountHandler=async()=>new Promise(resolve=>{finishList=resolve;});
const abandonedList=accountApp.node('#refresh-credentials-button').listeners.get('click')();
accountApp.node('#close-account').listeners.get('click')();
finishList(success(credentialList()));await abandonedList;assert.equal(accountApp.node('#credential-list').children.length,0);
accountHandler=async()=>success(credentialList());await accountApp.node('#account-button').listeners.get('click')();

let finishRevoke;
accountHandler=async()=>new Promise(resolve=>{finishRevoke=resolve;});
const abandonedRevoke=accountApp.node('#credential-list').find('button').listeners.get('click')();
accountApp.node('#account').listeners.get('cancel')();finishRevoke(success({token_id:'external-read-token',revoked:true}));await abandonedRevoke;
assert.equal(accountApp.node('#revoke-result').text,'');assert.equal(accountApp.node('#credential-list').children.length,0);
assert.equal(accountApp.client.draft('posts:'),'preserved while managing access');

accountHandler=async()=>success(credentialList());await accountApp.node('#account-button').listeners.get('click')();
finishDownload=null;accountHandler=async path=>path.endsWith('/challenges') ? success(accessChallenge('observe')) : new Promise(resolve=>{finishDownload=resolve;});
const cancelledDownload=accountApp.node('#config-button').listeners.get('click')();
while(!finishDownload)await new Promise(resolve=>setImmediate(resolve));
accountApp.node('#logout-button').listeners.get('click')();finishDownload(success(accessExchange('observe')));await cancelledDownload;
assert.equal(accountApp.downloads.length,1);assert.equal(accountApp.node('#world').hidden,true);assert.equal(accountApp.client.draft('posts:'),'preserved while managing access');
console.log('Commons access UI: permission choice, one download per double click, scoped list, revoke failure/success and Close/Escape/Leave cancellation preserve identity and drafts');
