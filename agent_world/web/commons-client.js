// Identity secrets stay on this device; credentials are never persisted or put in URLs.
export const encode = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes))).replaceAll('+','-').replaceAll('/','_').replace(/=+$/,'');
export const decode = text => Uint8Array.from(atob(text.replaceAll('-','+').replaceAll('_','/')), c => c.charCodeAt(0));
export async function createKey() {
  const pair = await crypto.subtle.generateKey({name:'Ed25519'}, true, ['sign','verify']);
  const key = await crypto.subtle.exportKey('jwk', pair.privateKey);
  return {version:'agent-world-key-v1', private_key:key.d, public_key:key.x};
}
export async function signingKey(key) {
  if (!key || key.version !== 'agent-world-key-v1' || !/^[A-Za-z0-9_-]{43}$/.test(key.private_key) || !/^[A-Za-z0-9_-]{43}$/.test(key.public_key)) throw new Error('This is not an Agent World recovery file.');
  return crypto.subtle.importKey('jwk', {kty:'OKP', crv:'Ed25519', d:key.private_key, x:key.public_key, ext:true}, {name:'Ed25519'}, false, ['sign']);
}
export async function validateKey(key) {
  const privateKey = await signingKey(key);
  const publicKey = await crypto.subtle.importKey('raw', decode(key.public_key), {name:'Ed25519'}, false, ['verify']);
  const probe = new TextEncoder().encode('agent-world-recovery-validation');
  const signature = await crypto.subtle.sign('Ed25519', privateKey, probe);
  if (!await crypto.subtle.verify('Ed25519', publicKey, signature, probe)) throw new Error('Recovery key does not match its public identity.');
  return {version:key.version, private_key:key.private_key, public_key:key.public_key};
}
export function identityStore() {
  return new Promise((resolve,reject) => {
    const request = indexedDB.open('agent-world-commons',1);
    request.onupgradeneeded = () => request.result.createObjectStore('identity');
    request.onerror = () => reject(new Error('This browser cannot save your identity. Enable site storage first.'));
    request.onsuccess = () => resolve(request.result);
  });
}
export async function savedKey(value) {
  const db = await identityStore();
  try { return await new Promise((resolve,reject) => {
    const tx = db.transaction('identity', value === undefined ? 'readonly' : 'readwrite');
    const request = value === undefined ? tx.objectStore('identity').get('key') : tx.objectStore('identity').put(value,'key');
    let result; request.onsuccess = () => {result = request.result;};
    tx.oncomplete = () => resolve(result); tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error);
  }); } finally {db.close();}
}
export class CommonsClient {
  constructor(fetcher=globalThis.fetch.bind(globalThis), storage=localStorage, locks=globalThis.navigator?.locks) {this.fetcher=fetcher;this.storage=storage;this.locks=locks;this.token=null;this.publicKey=null;this.universe=null;this.expiresAt=null;}
  async request(path, body, method, {signal}={}) {
    const sentToken=this.token;
    const response = await this.fetcher(path,{method:method || (body ? 'POST':'GET'),cache:'no-store',headers:{'Content-Type':'application/json',...(sentToken ? {Authorization:'Bearer '+sentToken}:{})},...(body ? {body:JSON.stringify(body)}:{}),...(signal ? {signal}:{})});
    let data;
    try {data=await response.json();} catch(cause) {if(signal?.aborted)throw cause;const error=new Error('The world response was interrupted. Your submission can be checked with its original operation.');error.status=response.status;error.client=this;error.staleAuth=response.status===401 && this.token!==sentToken;if(response.status===401 && !error.staleAuth)this.token=null;throw error;}
    if (!response.ok) { const error = new Error(data.message || data.detail || 'The world could not be reached. Try again.');error.client=this;error.staleAuth=response.status===401 && this.token!==sentToken;if(response.status===401 && !error.staleAuth)this.token=null;error.status=response.status;error.code=data.error;error.recovery=data.recovery;throw error; }
    return data;
  }
  async authenticate(key,{isCurrent=()=>true}={}) {
    const active=()=>{if(!isCurrent())throw cancelledSession();};
    active();
    this.token=null; this.publicKey=key.public_key;
    const challenge=await this.request('/v1/key-identities/challenges',{public_key:key.public_key});
    active();
    if(challenge.public_key!==key.public_key || challenge.version!=='agent-world-key-identity-v1') throw new Error('The world returned an unexpected identity challenge.');
    const expected=new TextEncoder().encode(['agent-world-key-identity-v1',challenge.universe,challenge.challenge_id,challenge.nonce].join('\0'));
    if(encode(expected)!==challenge.message) throw new Error('The world returned an invalid identity challenge.');
    this.universe=challenge.universe;
    const signature=encode(await crypto.subtle.sign('Ed25519',await signingKey(key),decode(challenge.message)));
    active();
    const result=await this.request('/v1/key-identities/exchange',{challenge_id:challenge.challenge_id,signature});
    active();
    this.token=result.identity.token;this.expiresAt=result.identity.expires_at; return result;
  }
  async query(name,args={}) {return (await this.request('/v1/functions/'+name+'/invoke',{arguments:args})).result;}
  async openConversation(peer,{isCurrent=()=>true,onPage=()=>{}}={}){
    let after;const cursors=new Set(),ids=new Set();
    const active=()=>{if(!isCurrent())throw cancelledSession();};
    while(true){
      active();const page=await this.query('commons.conversation.list',{limit:100,...(after ? {after}:{})});active();
      if(!page || !Array.isArray(page.conversations) || typeof page.has_more!=='boolean' || page.conversations.length>100)throw new Error('The world returned an incomplete conversation history. No new conversation was opened.');
      if(page.has_more && (typeof page.next_cursor!=='string' || !page.next_cursor || page.next_cursor.length>4096 || cursors.has(page.next_cursor) || !page.conversations.length))throw new Error('Conversation history did not advance. No new conversation was opened.');
      if(!page.has_more && page.next_cursor!=null)throw new Error('The world returned an inconsistent conversation cursor. No new conversation was opened.');
      let advanced=false;
      for(const row of page.conversations){
        if(!row || typeof row.conversation_id!=='string' || !/^[A-Za-z0-9_-]{1,64}$/.test(row.conversation_id) || !Array.isArray(row.participant_role_ids) || row.participant_role_ids.length!==2 || !row.participant_role_ids.every(id=>typeof id==='string' && id))throw new Error('The world returned an invalid conversation. No new conversation was opened.');
        if(!ids.has(row.conversation_id))advanced=true;ids.add(row.conversation_id);
      }
      if(page.conversations.length && !advanced)throw new Error('Conversation history repeated a page. No new conversation was opened.');
      onPage(page,!!after);active();
      const existing=page.conversations.find(row=>row.participant_role_ids.includes(peer));if(existing)return existing;
      if(!page.has_more)return this.mutate('commons.conversation.open',{conversation_id:crypto.randomUUID(),peer_role_id:peer},{},isCurrent);
      after=page.next_cursor;cursors.add(after);
    }
  }
  get pendingKey(){return 'agent-world-pending:'+encodeURIComponent(this.universe)+':'+this.publicKey;}
  pending(){const raw=this.storage.getItem(this.pendingKey);return raw ? JSON.parse(raw) : null;}
  isCurrentPending(key,pending){const raw=this.storage.getItem(key);if(!raw)return false;const current=JSON.parse(raw);return current.name===pending.name && current.body.operation_id===pending.body.operation_id;}
  withPendingLock(key,fn){if(this.locks)return this.locks.request(key,fn);if(this.storage===globalThis.localStorage)throw new Error('This browser cannot safely coordinate submissions between tabs. Use a current browser with Web Locks; your draft remains on this device.');return Promise.resolve(fn());}
  draftKey(route){if(!this.universe || !this.publicKey)throw new Error('Enter with your identity before saving a draft.');return 'agent-world-draft:'+encodeURIComponent(this.universe)+':'+this.publicKey+':'+encodeURIComponent(route);}
  draft(route){return this.storage.getItem(this.draftKey(route)) || '';}
  saveDraft(route,text){const key=this.draftKey(route);if(text)this.storage.setItem(key,text);else this.storage.removeItem(key);}
  clearDraft(route,expected){if(expected===undefined || this.draft(route)===expected)this.saveDraft(route,'');}
  async restoreRejected(){
    return this.withPendingLock(this.pendingKey,()=>{
      const pending=this.pending();if(!pending?.rejection)throw new Error('The outcome is still unknown. Check the original operation before clearing it.');
      const route=pending.route || mutationRoute(pending);const text=pending.draft_text ?? pending.body.arguments.text ?? '';
      if(text){const current=this.draft(route);this.saveDraft(route,current && current!==text ? current+'\n\n'+text : text);}
      this.storage.removeItem(this.pendingKey);return {route,text:this.draft(route)};
    });
  }
  async mutate(name,args,context={},isCurrent) {
    const key=this.pendingKey;let pending;
    await this.withPendingLock(key,()=>{
      if(isCurrent && !isCurrent())throw cancelledSession();
      if(this.pending()) throw new Error('Check your previous submission before sending something new.');
      pending={name,body:{operation_id:crypto.randomUUID(),arguments:args},...context};
      // Persist before sending: refresh, timeout and a lost response must reuse this operation.
      this.storage.setItem(key,JSON.stringify(pending));
    });
    if(isCurrent && !isCurrent()){
      // Once published, another tab may already have sent this operation.
      // Keep its identity even if this page leaves before sending it itself.
      throw cancelledSession();
    }
    return this.retry();
  }
  async retry() {
    const key=this.pendingKey; const pending=this.pending();
    if(!pending) return null;
    if(pending.rejection)throw new Error('This submission was rejected without committing. Restore its text before sending a corrected submission.');
    try {
      const result=await this.request('/v1/functions/'+pending.name+'/invoke',pending.body);
      if(result.ok!==true || result.operation_id!==pending.body.operation_id || result.function_id!==pending.name || !result.result || typeof result.result!=='object' || Array.isArray(result.result)) throw new Error('The world returned an incomplete receipt. Check the original submission again.');
      await this.withPendingLock(key,()=>{if(this.isCurrentPending(key,pending)){if(pending.route && key===this.pendingKey)this.clearDraft(pending.route,pending.draft_text ?? pending.body.arguments.text);this.storage.removeItem(key);}});return result.result;
    } catch(error) {
      // Definite request/domain rejection did not commit; unknown results stay recoverable.
      if(error.status>=400 && error.status<500 && ['InvalidArguments','SchemaRejected','ResourceNotFound'].includes(error.code)) await this.withPendingLock(key,()=>{if(this.isCurrentPending(key,pending))this.storage.setItem(key,JSON.stringify({...pending,rejection:{code:error.code,message:error.message}}));});
      throw error;
    }
  }
}
function cancelledSession(){const error=new Error('This browser session changed before a conversation could be opened.');error.cancelled=true;return error;}
export function mutationRoute(pending){const args=pending.body.arguments;if(pending.name==='commons.reply.create')return 'post:'+args.post_id;if(pending.name==='commons.message.send')return 'messages:'+args.conversation_id;return 'posts:';}
// Authorized history remains in memory. Refreshing merges a recent page without
// throwing away older pages already read; older pages are fetched only on demand.
export class CommonsHistory {
  constructor(field,idField){this.field=field;this.idField=idField;this.items=[];this.hasMore=false;this.cursor=null;this.loaded=false;}
  merge(result,older=false){
    const rows=result[this.field];if(!Array.isArray(rows) || (result.has_more && !result.next_cursor))throw new Error('The world returned an incomplete history page. Refresh to try again.');
    const entries=new Map(this.items.map(item=>[item[this.idField],item]));
    const gap=this.loaded && !older && this.items.length>0 && rows.length>0 && !rows.some(item=>entries.has(item[this.idField]));
    for(const item of rows){if(typeof item[this.idField]!=='string')throw new Error('The world returned an invalid history item.');entries.set(item[this.idField],item);}
    this.items=[...entries.values()].sort((a,b)=>(b.updated_at ?? b.created_at)-(a.updated_at ?? a.created_at) || (a[this.idField]===b[this.idField] ? 0 : a[this.idField]<b[this.idField] ? 1 : -1));
    // A head refresh must not move the older-page cursor back to the first page.
    if(!this.loaded || older || gap){this.hasMore=result.has_more===true;this.cursor=result.next_cursor || null;}
    this.loaded=true;return this.items;
  }
}
export function parseRoute(hash) {
  const parts=hash.replace(/^#/,'').split('/');
  if(parts[0]==='post' && /^[A-Za-z0-9_-]{1,64}$/.test(parts[1] || '')) return {page:'post',id:parts[1]};
  if(parts[0]==='messages') return {page:'messages',id:/^[A-Za-z0-9_-]{1,64}$/.test(parts[1] || '') ? parts[1] : null};
  return {page:'posts',id:null};
}
