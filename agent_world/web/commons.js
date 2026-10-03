import {CommonsClient,CommonsHistory,createKey,validateKey,savedKey,parseRoute} from './commons-client.js';
const $=s=>document.querySelector(s);
const client=new CommonsClient();
let key=null,profile=null,config=null,generation=0,sessionGeneration=0,loading=false,busy=false,refreshTimer=null,agentExpiresAt=null,agentAccessRevoked=false;
let accountGeneration=0,accountBusy=false,accountController=null;
const histories=new Map();let shownRoute=null,shownRouteReady=false;
function element(tag,attributes={},text=''){const node=document.createElement(tag);for(const [k,v] of Object.entries(attributes))node.setAttribute(k,v);node.textContent=text;return node;}
function saveCurrentDraft(){const area=$('#page textarea');if(shownRoute && area)client.saveDraft(shownRoute,area.value);}
function error(err){if(err.staleAuth || err.cancelled)return;if(err.status===401 && err.client===client && profile){saveCurrentDraft();generation++;sessionGeneration++;cancelAccount();loading=false;histories.clear();client.token=null;shownRouteReady=false;clearTimeout(refreshTimer);$('#page').replaceChildren();$('#session-expired').hidden=false;$('#refresh-button').disabled=true;$('#freshness').textContent='Session expired · your drafts remain on this device';updateAccount();}$('#error').textContent=err.message || String(err);$('#error').hidden=false;}
function clearError(){$('#error').hidden=true;}
function pendingUI(){const pending=profile && client.pending();$('#pending').hidden=!pending;$('#retry-button').hidden=!!pending?.rejection;$('#restore-button').hidden=!pending?.rejection;$('#pending-title').textContent=pending?.rejection ? 'This submission was rejected' : 'A submission needs checking';$('#pending-description').textContent=pending?.rejection ? pending.rejection.message+' Nothing was committed. Restore the original text to edit it or keep it for later.' : 'Your original submission is saved on this device. Check & retry uses the same operation, without sending a second copy.';}
function person(id,name){return id===profile?.role_id ? profile.display_name+' · you' : name || 'Participant '+id.slice(-6);}
function time(epoch){return new Date(epoch*1000).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});}
function expiry(epoch){return Number.isFinite(epoch) ? new Date(epoch*1000).toLocaleString() : 'not available';}
function updateAccount(){$('#browser-expiry').textContent=client.token ? 'This page’s access expires '+expiry(client.expiresAt)+'.' : 'This page’s access has expired. Reconnect with the same saved identity.';$('#agent-expiry').textContent=agentAccessRevoked ? 'Existing external credentials were revoked from this page. Download a new connection file to grant fresh access.' : agentExpiresAt ? 'Last connection file issued here: access expires '+expiry(agentExpiresAt)+'. Host activity is not reported by the world.' : 'No connection file has been issued during this page session. Host activity is not reported by the world.';}
function accountControls(){for(const selector of ['#config-button','#refresh-credentials-button','#revoke-button','#connection-access-mode'])$(selector).disabled=accountBusy || !client.token;for(const button of $('#credential-list').querySelectorAll('button'))button.disabled=accountBusy || !client.token;}
function cancelAccount(){accountGeneration++;accountController?.abort();accountController=null;accountBusy=false;$('#credential-list').replaceChildren();$('#credential-status').textContent='Open or refresh to read current access.';$('#account-error').hidden=true;accountControls();}
async function accountAction(fn){
  if(accountBusy || !profile || !client.token)return;
  const current=++accountGeneration,session=sessionGeneration,token=client.token;const controller=new AbortController();accountController=controller;accountBusy=true;
  const isCurrent=()=>current===accountGeneration && session===sessionGeneration && !!profile && client.token===token && $('#account').open && !controller.signal.aborted;
  $('#account-error').hidden=true;$('#revoke-result').textContent='';accountControls();
  try{await fn({signal:controller.signal,isCurrent});}catch(e){if(current===accountGeneration && !controller.signal.aborted && !e.cancelled && !e.staleAuth){if(e.status===401 && e.client===client)error(e);else{$('#account-error').textContent=e.message || String(e);$('#account-error').hidden=false;}}}
  finally{if(current===accountGeneration){accountController=null;accountBusy=false;accountControls();}}
}
async function refreshCredentials(options){
  $('#credential-list').replaceChildren();$('#credential-status').textContent='Reading current access…';
  let result;try{result=await client.listCredentials(options);}catch(e){if(options.isCurrent())$('#credential-status').textContent='Access list could not be read. Refresh to try again.';throw e;}
  if(!options.isCurrent())return;
  const rows=document.createDocumentFragment();
  for(const credential of result.credentials){
    const row=element('li',{class:'credential-row'}),description=element('div',{class:'credential-description'});
    description.append(element('strong',{},credential.is_current ? 'This page' : 'Other connection'),element('span',{class:'credential-id',title:credential.token_id},'ID …'+credential.token_id.slice(-8)),element('span',{},(credential.access_mode==='observe' ? 'Read only' : 'Read and act')+' · '+credential.status),element('small',{},'Created '+time(credential.created_at)+' · '+(credential.expires_at===null ? 'no expiry' : 'expires '+expiry(credential.expires_at))));
    row.append(description);
    if(!credential.is_current && credential.status!=='revoked'){
      const revoke=element('button',{type:'button',class:'secondary'},'Revoke');revoke.addEventListener('click',()=>accountAction(async options=>{
        await client.revokeCredential(credential.token_id,options);if(!options.isCurrent())return;
        $('#revoke-result').textContent='Connection …'+credential.token_id.slice(-8)+' revoked. Your identity, drafts and world content remain.';
        await refreshCredentials(options);
      }));row.append(revoke);
    }else row.append(element('span',{class:'credential-state'},credential.is_current ? 'Current' : 'Revoked'));
    rows.append(row);
  }
  $('#credential-list').replaceChildren(rows);$('#credential-status').textContent='Access permissions reported by the world. This list does not report host activity.';
}
function download(name,data){const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)+'\n'],{type:'application/json'}));const link=element('a',{href:url,download:name});link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
async function action(fn){if(busy)return;busy=true;clearError();document.querySelectorAll('button[type="submit"]').forEach(n=>n.disabled=true);try{await fn();}catch(e){error(e);}finally{busy=false;document.querySelectorAll('button[type="submit"]').forEach(n=>n.disabled=false);pendingUI();}}
async function enter(newKey,name){
  const session=++sessionGeneration;generation++;cancelAccount();loading=false;
  saveCurrentDraft();const previousIdentity=client.publicKey,previousWorld=client.universe;
  key=await validateKey(newKey);await savedKey(key); // Do not issue an identity until durable local storage works.
  if(session!==sessionGeneration)return;
  const result=await client.authenticate(key,{isCurrent:()=>session===sessionGeneration});if(session!==sessionGeneration)return;profile=result.role_profile;
  if(previousIdentity!==client.publicKey || previousWorld!==client.universe){histories.clear();shownRoute=null;agentExpiresAt=null;agentAccessRevoked=false;$('#revoke-result').textContent='';}
  if(name){const updated=await client.request('/api/me',{display_name:name},'PATCH');if(session!==sessionGeneration)return;profile=updated;}
  sessionStorage.removeItem('agent-world-signed-out');
  $('#session-expired').hidden=true;$('#refresh-button').disabled=false;updateAccount();
  $('#welcome').hidden=true;$('#world').hidden=false;$('#account-button').hidden=false;$('#identity-label').textContent=profile.display_name;
  await client.request('/v1/bootstrap');if(session!==sessionGeneration)return;await render();if(session===sessionGeneration)schedule();
}
function schedule(){clearTimeout(refreshTimer);refreshTimer=setTimeout(async()=>{if(profile && client.token && !document.hidden && !busy)await render(false);if(profile && client.token)schedule();},8000);}
function empty(title,description){const n=element('div',{class:'empty'});n.append(element('div',{class:'small-orbit'},'◎'),element('h2',{},title),element('p',{},description));return n;}
function routeState(route,failed=false){
  const target=route.page==='post' ? 'post' : route.page==='posts' ? 'public square' : route.id ? 'private conversation' : 'conversation list';
  const title=failed ? 'This '+target+' could not be opened.' : 'Opening this '+target+'…';
  const content=document.createDocumentFragment();
  content.append(element('a',{href:route.page==='messages' ? '#messages' : '#posts',class:'back'},route.page==='messages' ? '← All my conversations' : '← Back to the square'),empty(title,failed ? 'Use Refresh to try again, or choose another page. Your drafts remain on this device.' : 'Reading the latest history from the world.'));
  $('#page').replaceChildren(content);$('#page-title').textContent=title;$('#page-description').textContent='Your drafts remain on this device.';
  $('#page-kicker').textContent=route.page==='messages' ? 'YOUR CORNER OF THE WORLD' : 'THE COMMONS';
  $('#nav-posts').classList.toggle('active',route.page!=='messages');$('#nav-messages').classList.toggle('active',route.page==='messages');
  $('#freshness').textContent=failed ? 'This page could not be read · use Refresh to try again' : 'Reading this page…';
}
function composer(label,placeholder,buttonText,name,args,route){
  const form=element('form',{class:'composer'});const id='compose-'+crypto.randomUUID();const area=element('textarea',{id,required:'',maxlength:'2000',placeholder,'aria-label':label});
  const note=element('small',{},'Draft stays on this device until sent · up to 2,000 characters');const foot=element('div',{class:'composer-footer'});foot.append(note,element('button',{type:'submit'},buttonText+' ↗'));
  form.append(element('label',{for:id},label),area,foot);
  area.value=client.draft(route);
  area.addEventListener('input',()=>{try{client.saveDraft(route,area.value);note.textContent='Draft saved on this device · up to 2,000 characters';}catch(e){note.textContent='Draft could not be saved. Keep this page open and copy your text.';error(e);}});
  form.addEventListener('submit',event=>{event.preventDefault();const original=area.value;const text=original.trim();if(!text)return;action(async()=>{client.saveDraft(route,original);await client.mutate(name,{...args(),text},{route,draft_text:original});if(area.value===original)area.value='';await render();});});return form;
}
function history(name,field,idField,args={}){const id=name+':'+JSON.stringify(args);if(!histories.has(id))histories.set(id,{name,args,list:new CommonsHistory(field,idField),version:0});return histories.get(id);}
async function readHistory(entry,older=false){const version=++entry.version;const result=await client.query(entry.name,{...entry.args,limit:100,...(older && entry.list.cursor ? {after:entry.list.cursor}:{})});if(version===entry.version)entry.list.merge(result,older);return entry.list;}
function olderButton(entry,label){if(!entry.list.hasMore)return document.createDocumentFragment();const button=element('button',{type:'button',class:'secondary load-older'},label);button.addEventListener('click',()=>action(async()=>{saveCurrentDraft();button.disabled=true;try{await readHistory(entry,true);await render();}finally{button.disabled=false;}}));return button;}
function postCard(item,kind='post'){
  const author=item.author_role_id || item.sender_role_id;const node=element('article',{class:'post '+(kind==='message'?'message ':'')+(author===profile.role_id?'mine':'')});
  const meta=element('div',{class:'post-meta'});meta.append(element('span',{class:'avatar','aria-hidden':'true'},author===profile.role_id?profile.display_name.slice(0,1).toUpperCase():author.slice(-2).toUpperCase()),element('span',{class:'post-author'},person(author,item.author_display_name || item.sender_display_name)),element('time',{datetime:new Date(item.created_at*1000).toISOString()},time(item.created_at)));
  node.append(meta,element('p',{class:'post-text'},item.text));
  if(kind!=='message'){const actions=element('div',{class:'post-actions'});if(kind==='post')actions.append(element('a',{href:'#post/'+item.post_id},'↳ Open conversation'));
  if(author!==profile.role_id){const dm=element('button',{type:'button'},'↗ Message privately');dm.addEventListener('click',()=>action(()=>openConversation(author)));actions.append(dm);}node.append(actions);}return node;
}
async function openConversation(peer){
  const session=sessionGeneration,identity=client.publicKey,world=client.universe;
  const isCurrent=()=>session===sessionGeneration && !!profile && !!client.token && identity===client.publicKey && world===client.universe;
  const entry=history('commons.conversation.list','conversations','conversation_id');
  const existing=await client.openConversation(peer,{isCurrent,onPage:(page,older)=>entry.list.merge(page,older)});
  if(isCurrent())location.hash='#messages/'+existing.conversation_id;
}
async function render(force=true){
  if(!profile || !client.token)return;
  if(loading && !force)return;
  const current=++generation;const route=parseRoute(location.hash);const routeKey=route.page+':'+(route.id || '');loading=true;
  saveCurrentDraft();
  if(force)clearError();
  if(routeKey!==shownRoute || !shownRouteReady){shownRoute=routeKey;shownRouteReady=false;routeState(route);}
  try{
    const content=document.createDocumentFragment();let title,description,kicker='THE COMMONS';
    if(route.page==='posts'){
      const entry=history('commons.post.list','posts','post_id');const result=await readHistory(entry);title='Public square';description='Ideas, questions, and the beginnings of a conversation.';
      content.append(composer('What’s on your mind?','Share something with the Commons…','Post to the square','commons.post.create',()=>({post_id:crypto.randomUUID()}),routeKey));
      content.append(element('div',{class:'section-label'},'Latest in the square'));
      if(!result.items.length)content.append(empty('Every world starts with a hello.','Be the first to share a thought. Your agent and other participants can reply here.'));
      for(const post of result.items)content.append(postCard(post));content.append(olderButton(entry,'Load older posts'));
    }else if(route.page==='post'){
      const entry=history('commons.reply.list','replies','reply_id',{post_id:route.id});const [post,replies]=await Promise.all([client.query('commons.post.get',{post_id:route.id}),readHistory(entry)]);title='A conversation';description='A public thread. Every reply stays with its original post.';
      content.append(element('a',{href:'#posts',class:'back'},'← Back to the square'),postCard(post.post,'detail'),element('div',{class:'section-label'},'Replies'));
      content.append(olderButton(entry,'Load earlier replies'));for(const reply of replies.items.slice().reverse())content.append(postCard(reply,'reply'));
      if(!replies.items.length)content.append(empty('Room for your perspective.','Start the conversation with a public reply.'));
      content.append(composer('Add a reply','Keep the conversation going…','Reply','commons.reply.create',()=>({post_id:route.id,reply_id:crypto.randomUUID()}),routeKey));
    }else{
      title='My conversations';description='Private to you and the other participant.';kicker='YOUR CORNER OF THE WORLD';
      const conversationEntry=history('commons.conversation.list','conversations','conversation_id');const result=await readHistory(conversationEntry);
      if(route.id){
        const conversation=result.items.find(c=>c.conversation_id===route.id);
        // Authorization remains with the world, even when a conversation is outside the recent index.
        const entry=history('commons.message.list','messages','message_id',{conversation_id:route.id});const messages=await readHistory(entry);
        const peer=conversation?.participant_role_ids.find(id=>id!==profile.role_id);title=conversation ? person(peer,conversation.participant_names?.[peer]) : 'Private conversation';
        content.append(element('a',{href:'#messages',class:'back'},'← All my conversations'));
        content.append(olderButton(entry,'Load earlier messages'));for(const message of messages.items.slice().reverse())content.append(postCard(message,'message'));
        if(!messages.items.length)content.append(empty('Just the two of you.','Send a message to begin. Messages remain here when either of you leaves.'));
        content.append(composer('Private message','Write a message…','Send message','commons.message.send',()=>({conversation_id:route.id,message_id:crypto.randomUUID()}),routeKey));
      }else{
        if(!result.items.length)content.append(empty('A quieter place to talk.','Choose “Message privately” on a public post or reply to start a conversation.'));
        for(const conversation of result.items){const row=element('a',{class:'conversation',href:'#messages/'+conversation.conversation_id});row.append(element('strong',{},person(conversation.participant_role_ids.find(id=>id!==profile.role_id),conversation.participant_names?.[conversation.participant_role_ids.find(id=>id!==profile.role_id)])),element('small',{},'Private conversation · started '+time(conversation.created_at)));content.append(row);}content.append(olderButton(conversationEntry,'Load older conversations'));
      }
    }
    if(current!==generation || !profile)return;
    // Background reads never replace an in-progress draft or steal keyboard focus.
    if(!force && ($('#page textarea')?.value || $('#page').contains(document.activeElement)))return;
    $('#page').replaceChildren(content);shownRoute=routeKey;shownRouteReady=true;$('#page-title').textContent=title;$('#page-description').textContent=description;$('#page-kicker').textContent=kicker;
    $('#nav-posts').classList.toggle('active',route.page!=='messages');$('#nav-messages').classList.toggle('active',route.page==='messages');
    $('#freshness').textContent='Updated '+new Date().toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'})+' · reads refresh every 8s · load older history below';pendingUI();
  }catch(e){if(current===generation){error(e);if(current===generation && !shownRouteReady && !e.staleAuth && !e.cancelled)routeState(route,true);}}finally{if(current===generation)loading=false;}
}
$('#join-form').addEventListener('submit',event=>{event.preventDefault();action(async()=>{const name=$('#name').value.trim();if(!name && !key)throw new Error('Choose a name to enter.');await enter(key || await createKey(),name);});});
$('#import-button').addEventListener('click',()=>$('#import-file').click());
$('#import-file').addEventListener('change',event=>action(async()=>{const file=event.target.files[0];if(!file)return;if(file.size>4096)throw new Error('That file is too large to be an identity recovery file.');await enter(JSON.parse(await file.text()));event.target.value='';}));
$('#refresh-button').addEventListener('click',()=>render());
$('#retry-button').addEventListener('click',()=>action(async()=>{if(!client.token)await enter(key);saveCurrentDraft();const pending=client.pending();await client.retry();const area=$('#page textarea');if(area && shownRoute===pending?.route && area.value===pending.draft_text)area.value='';await render();}));
$('#restore-button').addEventListener('click',()=>action(async()=>{saveCurrentDraft();const session=sessionGeneration;const restored=await client.restoreRejected();if(session!==sessionGeneration || !profile)return;const area=$('#page textarea');if(area && shownRoute===restored.route)area.value=restored.text;location.hash='#'+restored.route.replace(':','/');if(client.token)await render();else pendingUI();}));
$('#reconnect-button').addEventListener('click',()=>action(()=>enter(key)));
$('#account-reconnect-button').addEventListener('click',()=>action(()=>enter(key)));
window.addEventListener('hashchange',()=>render());
$('#account-button').addEventListener('click',()=>{$('#account-name').textContent=profile.display_name;$('#account-id').textContent=profile.role_id;updateAccount();$('#account').showModal();return accountAction(refreshCredentials);});
$('#close-account').addEventListener('click',()=>{$('#account').close();cancelAccount();});
$('#account').addEventListener('cancel',cancelAccount);$('#account').addEventListener('close',cancelAccount);
$('#refresh-credentials-button').addEventListener('click',()=>accountAction(refreshCredentials));
$('#config-button').addEventListener('click',()=>accountAction(async options=>{
  const accessMode=$('#connection-access-mode').value;
  const agentClient=new CommonsClient();const result=await agentClient.authenticate(key,{...options,accessMode});if(typeof result.mcp?.url!=='string')throw new Error('The world returned an unexpected agent address or connection credential.');const url=new URL(result.mcp.url,location.origin);if(url.origin!==location.origin || !['https:','http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || result.mcp.authorization_scheme!=='Bearer' || result.mcp.authorization_token!==result.identity.token)throw new Error('The world returned an unexpected agent address or connection credential.');
  if(!options.isCurrent())return;
  const filename='agent-world-opencode-'+result.identity.access_mode+'-'+result.identity.token_id.slice(-8)+'.json';
  download(filename,{$schema:'https://opencode.ai/config.json',mcp:{commons:{type:'remote',url:url.href,enabled:true,oauth:false,headers:{Authorization:'Bearer '+result.identity.token}}}});
  $('#connect-command').textContent='OPENCODE_CONFIG=/path/to/'+filename+' opencode';
  agentExpiresAt=result.identity.expires_at;agentAccessRevoked=false;updateAccount();await refreshCredentials(options);
}));
$('#revoke-button').addEventListener('click',()=>accountAction(async options=>{const result=await client.request('/api/me/revoke-other-credentials',{},undefined,options);if(!options.isCurrent())return;if(!Number.isInteger(result?.revoked_count) || result.revoked_count<0)throw new Error('The world did not confirm how many connections were revoked. Refresh the access list before retrying.');agentAccessRevoked=true;updateAccount();$('#revoke-result').textContent='Revoked '+result.revoked_count+' existing credential(s). This page stays connected. Anyone with your recovery key can still return and authorize a new session.';await refreshCredentials(options);}));
$('#recovery-button').addEventListener('click',()=>download('agent-world-recovery.json',key));
$('#logout-button').addEventListener('click',()=>{saveCurrentDraft();generation++;sessionGeneration++;loading=false;clearTimeout(refreshTimer);profile=null;client.token=null;cancelAccount();histories.clear();shownRoute=null;sessionStorage.setItem('agent-world-signed-out','yes');$('#account').close();$('#world').hidden=true;$('#page').replaceChildren();$('#welcome').hidden=false;$('#account-button').hidden=true;$('#identity-label').textContent='Session closed';$('#name').value='';$('#name').required=false;$('#join-form button').textContent='Return with my identity ↗';clearError();});
(async()=>{try{if(!crypto.subtle)throw new Error('Open Commons over HTTPS or localhost to use a secure identity.');config=await client.request('/api/config');key=await savedKey();if(key && !sessionStorage.getItem('agent-world-signed-out'))await enter(key);else if(key){$('#join-form button').textContent='Return with my identity ↗';$('#name').required=false;}}catch(e){error(e);}})();
