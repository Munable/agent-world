import {createKey,validateKey,savedKey} from '/static/commons-client.js';
import {FrontierClient} from './frontier-client.js';
import {FrontierHistory} from './frontier-history.js';
import {parseCheckpoint,makeCheckpoint,characterChanges,observedRange} from './frontier-observation.js';

const $=id=>document.getElementById(id), client=new FrontierClient();
const abilities={strength:'力量',dexterity:'敏捷',constitution:'体质',intelligence:'智力',wisdom:'感知',charisma:'魅力'};
const statuses={pending:'等待有人领取',assigned:'已有人领取，等待判断',offered:'等待本人接受后果',rejected:'未获允许',executed:'已经结算',withdrawn:'已经撤回'};
const states={sealed:'尚未处理',open:'可以拿取',taken:'已被携带',spent:'已经永久耗尽',obstructed:'仍有阻碍',resolved:'已经解决',unread:'尚未解读',read:'已经解读'};
const kinds={cache:'可携带的物件',barrier:'可处理的阻碍',inscription:'可解读的文字'};
const initialStates={cache:[['sealed','需要先检定'],['open','可直接拿取']],inscription:[['unread','需要先检定'],['read','可直接阅读']],barrier:[['obstructed','需要先处理这道障碍']]};
const effects={resolve_object:'解决这一物件的当前状态',none:'不改变物件状态'};
const actionNames={join:'进入',move:'前往',expand:'创作地点',add_objects:'创作新的互动内容',say:'说话',tell:'交谈',check:'检定',unlock:'使用物件解锁',take:'拿取',drop:'放下物件',transfer:'交付',rest:'休息',request:'提出自由行动',visit:'领取附加题',assigned:'领取附加题',answer:'回答附加题',accept:'接受并结算',withdraw:'撤回'};
const actionHints={
  'frontier.join':'创建人物，或接着上次的经历继续。',
  'frontier.move':'沿这条已知通路前往，人物位置会保存。',
  'frontier.expand':'创作出口另一端的地点，让其他旅人也能走进去。',
  'frontier.add_objects':'为当前地点加入新物件或可读内容，留下其他旅人也能使用的选择。',
  'frontier.say':'写一句话，说给同一地点的旅人听。',
  'frontier.tell':'向这位当地人说话；回应可由你的 Agent 创作。',
  'frontier.examine':'看看物件和已有事实，不改变现场。',
  'frontier.take':'拿取这件已打开的物品，放进你的背包。',
  'frontier.unlock':'核对对应物件和代价，再决定是否开放这条共享通路。',
  'frontier.drop':'把持有物放在当前地点，让后来的人也能拿取。',
  'frontier.transfer':'选择同一地点的旅人，把你的物品交给对方。',
  'frontier.rest':'在安全的旅店恢复生命。人物与经历继续保留。',
  'frontier.request':'提出一种新办法，等另一位旅人帮忙判断。',
  'frontier.visit':'顺手帮别人判断一个办法，自己的探索可以继续。',
  'frontier.answer':'回答你已领取的问题，实际行动由提出者决定。',
  'frontier.accept':'先核对方案与后果，再接受一次真实检定。',
  'frontier.withdraw':'撤回还没执行的办法，过去的记录仍保留。',
};
const chapters=[['entry','进入与接续'],['play','开始探索'],['creating','创作接续地点'],['helping','帮助判断他人的行动'],['recovery','断线、重试与身份']];
const classes=[['fighter','战士','力量见长，适合处理重物与阻碍。'],['rogue','游荡者','敏捷见长，适合开启容器和灵活探索。']];
let key=null,profile=null,snapshot=null,busy=false,generation=0,readSeq=0,timer=null,agentClient=null,lastRead=0,readInterrupted=false;
let mapItems=new Map(),mapCursor=0,mapMore=false,mapTotal=0,mapFlight=null;
let history=new FrontierHistory(),latestHistoryFlight=null,olderHistoryFlight=null;
let requestPages={own:{items:new Map(),cursor:0,more:false,total:0},received:{items:new Map(),cursor:0,more:false,total:0}};
let names=new Map(),objects=new Map(),sceneRenderKey='',taskRenderKey='',createContext=null,createMode='expand';
let entryCheckpoint=null,checkpointWarning='',returnHistoryFlight=null,activityRenderKey='',lastCheckpointSignature='';
let entryKeyState='checking',entryIdentityRead=0,entryCharacter=null;

const text=(tag,value,cls='')=>{const node=document.createElement(tag);node.textContent=value??'';if(cls)node.className=cls;return node;};
const time=value=>Number.isFinite(value)?new Date(value*1000).toLocaleString():'时间未提供';
const signed=value=>(value>=0?'+':'')+value;
const name=id=>{const label=names.get(id);if(label&&[...names.values()].filter(value=>value===label).length>1)return label+'（'+(id===profile?.role_id?'你':id.slice(-6))+'）';return label||(id==='system:installer'?'世界初始设定':id?'旅人 '+id.slice(-6):'未知参与者');};
const current=()=>!!profile&&!!client.token;
function renderEntryIdentity(){
  $('enter').dataset.unavailable=String(!['saved','new'].includes(entryKeyState));
  $('entry-identity-status').textContent=entryKeyState==='saved'
    ? (entryCharacter?'接续本机身份：'+entryCharacter.name+' · '+entryCharacter.className+'（本页上次实际读取）。':'本机已有密钥，点击会接续这个身份；人物将在进入后核对。')+' 已有角色的名字和职业保持原样。'
    : entryKeyState==='new'?'本机尚未保存身份。首次进入时将创建身份，并使用下方名字和职业创建人物。'
    : entryKeyState==='error'?'本机身份暂时无法读取，请检查站点存储，或导入自己的恢复文件；不会自动新建身份。'
    : '正在读取本机保存的身份…';
  controls();
}
async function readEntryIdentity(){
  const read=++entryIdentityRead;entryKeyState='checking';renderEntryIdentity();
  try{
    const stored=await savedKey();if(read!==entryIdentityRead)return;
    if(!stored||entryCharacter?.publicKey!==stored.public_key)entryCharacter=null;
    entryKeyState=stored?'saved':'new';
  }catch{if(read!==entryIdentityRead)return;entryKeyState='error';entryCharacter=null;}
  renderEntryIdentity();
}
function announce(value){$('message').textContent=value;$('message').hidden=!value;}
function download(filename,value){const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)+'\n'],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download=filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function saveDrafts(){if(!profile)return;client.saveDraft('frontier-intent',$('intent').value);client.saveDraft('frontier-say',$('say-text').value);if($('intent-target').value)client.saveDraft('frontier-intent-target',$('intent-target').value);if(createContext)saveCreation();}
function restoreDrafts(){if(!profile)return;$('intent').value=client.draft('frontier-intent');$('say-text').value=client.draft('frontier-say');renderAgentContinuation(client.draft('frontier-agent-continuation'));}
function renderAgentContinuation(value){$('agent-continuation').value=value;$('agent-continuation-card').hidden=!value;$('copy-continuation').dataset.unavailable=String(!value);}
async function copyPrompt(field){try{await navigator.clipboard.writeText(field.value);announce('已复制，可以粘贴给自己的助手。');}catch{field.focus();field.select();announce('浏览器未允许自动复制，文字已选中，可以手动复制。');}}
function appendNpcContinuation(host,result){
  const entry=result.entry,payload=entry?.payload;
  if(entry?.kind!=='tell'||entry.actor!==profile?.role_id||payload?.response_kind!=='no_response'||!Number.isInteger(entry.seq))return;
  const title=snapshot?.scene?.id===entry.scene_id?snapshot.scene.title:mapItems.get(entry.scene_id)?.title;
  const where=(title?'“'+title+'”':'这个地点')+'（地点 '+entry.scene_id+'）';
  const value='我刚在渡鸦溪的'+where+'对'+payload.npc_name+'（NPC '+payload.npc_id+'）说：“'+payload.text+'”。这句话已经保存为共同记录第 '+entry.seq+' 条，原记录只包含我的问话。\n\n陪我把这段聊下去。先读当前世界和这条记录，核对原话及后来有没有回应。若仍适合继续，就按这里的共创规则，新增一条明确署名的 NPC 接话，别再次只发布同一个问题，也不要改写旧记录或把台词当成物品、道路真的变了。如果连接只读、我已离开这里，或现在的事实对不上，先告诉我，别替我移动。这次只接一轮，再让我看实际保存的结果。';
  renderAgentContinuation(value);
  const field=document.createElement('textarea');field.rows=5;field.readOnly=true;field.value=value;
  const copy=button('复制给我的 Agent，接着聊',()=>copyPrompt(field));copy.dataset.local='true';
  host.append(label('给已接入 Agent 的续聊文字',field),copy,text('p','复制后粘贴到自己的 AI 工具。这只准备文字，不会启动 Agent 或自动回答；同钥回来可在“身份与 Agent”找回最近一条。','small'));
  try{client.saveDraft('frontier-agent-continuation',value);}catch{host.append(text('p','本机未能保存续聊文字，请现在复制；世界中的原问话仍已保存。','small'));}
}
function appendAuthoredDescription(host,value){host.append(text('p','作者原稿（当前状态另行核对）','small'),text('p',value.authored_description));}
function loadCheckpoint(){
  entryCheckpoint=null;checkpointWarning='';lastCheckpointSignature='';
  try{entryCheckpoint=parseCheckpoint(client.draft('frontier-observation'),{worldId:'living-frontier',roleId:profile.role_id});}
  catch{checkpointWarning='本机接续记录无法读取；下面只展示当前实际状态，无法判断上次看到了哪里。';}
}
function saveCheckpoint(displayedSeq){
  if(!snapshot?.character||!current()||document.hidden||!['world','history'].includes(pageId())||$(pageId()).hidden||$('risk-dialog').open||$('create-dialog').open)return;
  const seq=displayedSeq;if(!Number.isInteger(seq)||seq<0||seq>snapshot.history_latest_seq||!(seq===0&&history.latestLoaded||history.items.has(seq)))return;
  const signature=JSON.stringify([seq,snapshot.character.scene_id,snapshot.character.hp,snapshot.character.inventory]);if(signature===lastCheckpointSignature)return;
  try{
    let previous=null;try{previous=parseCheckpoint(client.draft('frontier-observation'),{worldId:snapshot.world_id,roleId:profile.role_id});}catch{}
    // Another tab may already have displayed a newer fact. Never move its mark back.
    if(previous&&previous.seen_seq>seq)return;
    client.saveDraft('frontier-observation',JSON.stringify(makeCheckpoint(snapshot,seq)));lastCheckpointSignature=signature;
  }catch{checkpointWarning='此浏览器未能保存本机接续位置；当前世界结果仍保留，下次需从共同记录核对。';}
}
function renderFreshness(){
  const last=lastRead?'最后成功读取 '+new Date(lastRead).toLocaleTimeString():'尚未读到完整现场';
  $('freshness').textContent=readInterrupted?'读取中断 · '+last+'；当前显示旧现场，页面可见时会继续读取。':lastRead?'最后读取 '+new Date(lastRead).toLocaleTimeString()+' · 页面可见时每6秒读取已保存状态，不运行外部 Agent。':'正在读取完整现场…';
}
function showError(error){
  if(error.cancelled||error.staleAuth)return;
  if(error.refreshRead&&error.status!==401){readInterrupted=true;renderFreshness();$('stale-state').textContent='现场读取暂时中断，当前显示上次读取的人物、地点和事项。页面可见时会继续读取，完整读取成功后此提示自动消失；原操作与草稿仍保留，写入结果不明时仍须核对原操作。';$('stale-state').hidden=false;if(current())schedule();controls();return;}
  const message=error.status===401?'这次访问的授权已到期或失效。请用同一密钥重连，核对最新状态后继续；身份、草稿和原操作仍已保留。':error.code==='StateConflict'?($('create-dialog').open?'这里已有新变化，这次创作没有提交。先关闭创作页，点击“取回草稿，重新选择”，读取最新现场后重新打开；草稿仍保留。':'局面已经改变，这次操作没有提交。解除被拒绝的请求，再读取最新状态后重新选择。'):error.message||String(error);
  $('error').textContent=message;$('error').hidden=false;
  const inline=$('risk-dialog').open?$('risk-error'):$('create-dialog').open?$('create-error'):null;
  if(inline){inline.textContent=message;inline.hidden=false;inline.scrollIntoView({block:'nearest'});}
  if(error.status===401&&profile){readInterrupted=false;client.token=null;announce('');$('reconnect').hidden=false;$('connection-state').textContent='本页授权到期或失效，请同钥重连。';$('freshness').textContent='旧现场：当前显示上次读取的状态，重连后更新。';$('stale-state').textContent='当前显示上次读取的人物、现场、地图、事项和连接状态。网页授权到期或失效，请同钥重连后核对最新情况；身份、草稿和原操作仍已保留。';$('stale-state').hidden=false;$('credentials-status').textContent='旧连接列表：以下是上次读取的状态，标为“有效”也不代表当前仍有效。请同钥重连后重新核对。';$('agent-status').textContent='网页授权到期或失效，暂时无法签发新连接。以前下载的连接是否有效，请重连后核对。';}
  controls();
}
function pending(){
  let value=null;try{value=profile&&client.pending();}catch(error){showError(error);}
  $('pending').hidden=!value;if(!value)return;
  $('retry').hidden=!!value.rejection;$('restore').hidden=!value.rejection;
  $('pending-detail').textContent=value.rejection?'这次请求已被明确拒绝，没有提交。请取回草稿或解除拒绝，再按最新现场重新选择。':'还没收到完整回执。操作可能已经提交；请核对原操作，不要另发一份相同动作。';
  $('restore').textContent=value.route?'取回草稿，重新选择':'解除被拒绝的请求';
}
function controls(){
  let unresolved=false;try{unresolved=!!profile&&!!client.pending();}catch{}
  $('cancel').hidden=!busy||$('entry').hidden;
  document.querySelectorAll('button').forEach(node=>{
    const keep=['leave','cancel','create-close','create-cancel'].includes(node.id)||!!node.closest('#risk-dialog');
    const offline=profile&&!client.token&&node.closest('#app')&&node.dataset.local!=='true'&&!['reconnect','leave','backup','restore','copy-prompt'].includes(node.id);
    node.disabled=node.dataset.unavailable==='true'||(busy&&!keep)||!!offline||(unresolved&&node.dataset.write==='true');
  });
  $('enter').textContent=busy&&!$('entry').hidden?'正在连接…':entryKeyState==='saved'?'接续本机密钥身份':entryKeyState==='new'?'创建身份，走进渡鸦溪':entryKeyState==='error'?'本机身份读取失败':'正在读取本机身份…';
  $('agent-access').disabled=busy;
}
function clearError(id){$(id).textContent='';$(id).hidden=true;}
async function run(fn){if(busy)return;busy=true;for(const id of ['error','create-error','risk-error'])clearError(id);controls();try{await fn();}catch(error){showError(error);}finally{busy=false;pending();controls();}}
async function mutate(functionId,args,context={}){const session=generation;++readSeq;return client.mutate(functionId,args,context,()=>session===generation&&current());}
function button(label,fn,{cls='secondary',write=false}={}){const node=text('button',label,cls);node.type='button';if(write)node.dataset.write='true';node.addEventListener('click',()=>run(fn));return node;}
function select(options,value){const node=document.createElement('select');for(const [id,label]of options){const option=text('option',label);option.value=id;node.append(option);}if(value!=null)node.value=value;return node;}
function label(caption,control){const node=text('label',caption);node.append(control);return node;}
function validatePage(page,after,field='items'){
  if(!page||!Array.isArray(page[field])||!Number.isInteger(page.next_after)||page.next_after<after||typeof page.has_more!=='boolean'||(page.has_more&&(!page[field].length||page.next_after<=after)))throw new Error('世界分页没有正确接续，请重新读取。');
}
async function enter(candidate,displayName='',classId='fighter'){
  announce('');const session=++generation,active=()=>{if(session!==generation){const error=new Error('本页连接已取消。');error.cancelled=true;throw error;}};
  const nextKey=await validateKey(await candidate);active();const response=await client.authenticate(nextKey,{isCurrent:()=>session===generation});active();
  key=nextKey;await savedKey(key);active();profile=response.role_profile;
  ++entryIdentityRead;entryKeyState='saved';if(entryCharacter?.publicKey!==key.public_key)entryCharacter=null;renderEntryIdentity();
  renderAgentContinuation('');
  loadCheckpoint();activityRenderKey='';returnHistoryFlight=null;
  $('entry').hidden=true;$('app').hidden=false;$('reconnect').hidden=true;$('connection-state').textContent='本页授权到期 '+time(client.expiresAt);$('who').textContent=profile.display_name;
  readInterrupted=false;lastRead=0;$('stale-state').hidden=true;$('credentials-status').textContent='';$('agent-status').textContent='';
  snapshot=null;sceneRenderKey='';taskRenderKey='';names.clear();objects.clear();mapItems.clear();mapCursor=0;mapMore=false;mapTotal=0;mapFlight=null;history=new FrontierHistory();latestHistoryFlight=null;olderHistoryFlight=null;
  for(const id of ['character-stats','character-abilities','inventory','character-detail','scene-npcs','scene-objects','scene-current-ground','available-actions','open-exits','nearby','my-requests','side-question','world-map','last-result','history-list','request-history','credentials','scene-activity','scene-activity-more','return-differences','return-changes'])$(id).replaceChildren();
  $('return-card').hidden=true;$('return-details').open=false;$('scene-activity-details').open=false;$('scene-activity-details').hidden=true;$('map-details').open=false;$('scene-activity-status').textContent='正在读取真实记录…';
  $('character-name').textContent='正在读取人物…';$('character-class').textContent='';$('scene-title').textContent='正在读取现场…';$('scene-description').textContent='';$('scene-author').textContent='';$('last-result-card').hidden=true;$('my-requests-card').hidden=true;
  requestPages={own:{items:new Map(),cursor:0,more:false,total:0},received:{items:new Map(),cursor:0,more:false,total:0}};
  const bootstrap=await client.request('/v1/bootstrap');active();const entry=bootstrap.world_entry_state?.view;
  if(!entry||typeof entry.joined!=='boolean')throw new Error('入口信息不完整，请同钥重连读取当前状态。');
  if(!entry.joined&&!client.pending()){await mutate('frontier.join',{name:displayName||'渡鸦溪旅人',class_id:classId});active();}
  restoreDrafts();
  if(!entry.joined&&client.pending()){
    snapshot=null;$('character-name').textContent='入场操作需要核对';$('character-class').textContent='先核对原操作，避免重复创建人物。';$('scene-title').textContent='你的身份已连接';$('scene-description').textContent='上次入场结果仍不明确，请用上方的按钮核对原操作。';pending();controls();return;
  }
  await refresh();active();await readMap();active();await showPage();schedule();
}
function schedule(){clearTimeout(timer);timer=setTimeout(async()=>{if(current()&&!document.hidden&&!busy&&!$('risk-dialog').open&&!$('create-dialog').open){try{await refresh();}catch(error){showError(error);}}if(profile)schedule();},6000);}
async function refresh(){
  const session=generation,seq=++readSeq;
  try{
    const value=await client.query('frontier.look');if(session!==generation||seq!==readSeq||!current())return;
    if(value.world_id!=='living-frontier'||!Array.isArray(value.available_actions)||!Array.isArray(value.directory))throw new Error('世界现场信息不完整，请重连后读取。');
    snapshot=value;entryCharacter=value.character?{publicKey:key.public_key,name:value.character.name,className:value.character.class_name}:null;render();
    if(Number.isInteger(snapshot.history_latest_seq)){history.observeLatest(snapshot.history_latest_seq);renderHistory();if(['world','history'].includes(pageId())&&history.needsLatest)await readLatestHistory();}
    if(session!==generation||seq!==readSeq||!current())return;
    lastRead=Date.now();if(readInterrupted){readInterrupted=false;$('stale-state').hidden=true;}renderFreshness();
  }catch(error){
    if(error.cancelled||error.staleAuth||session!==generation||seq!==readSeq||(!current()&&error.status!==401))return;
    error.refreshRead=true;throw error;
  }
}
async function risk(title,description){
  if(!$('risk-dialog').showModal)throw new Error('此浏览器无法显示风险确认，请使用新版浏览器。');
  clearError('risk-error');
  $('risk-title').textContent=title;$('risk-description').replaceChildren(text('p',description));$('risk-dialog').returnValue='';$('risk-dialog').showModal();
  return new Promise(resolve=>$('risk-dialog').addEventListener('close',()=>resolve($('risk-dialog').returnValue==='confirm'),{once:true}));
}
function terms(value){
  if(typeof value==='string')return value;if(value==null)return '';if(typeof value!=='object')return String(value);if(Array.isArray(value))return value.map(terms).join('\n');
  return Object.entries(value).map(([id,detail])=>({success:'成功',failure:'失败',cost:'代价',description:'说明',damage:'伤害',reroll:'重试',effect:'效果',check:'检定',resolve_object:'解决物件',none:'只记录检定',accept:'接受',required_key:'必须使用对应物件'}[id]||id)+'：'+terms(detail)).join('\n');
}
function roll(parent,result){
  if(!result||!Number.isInteger(result.roll))return;const row=text('div','','dice-line');row.append(text('span',result.roll,'die'),text('span',(abilities[result.ability]||result.ability)+' '+signed(result.modifier)+' · '+result.total+' / DC '+result.dc+' · '+(result.success?'成功':'失败')));parent.append(row);
}
function appendDiscovery(parent,discovery,{objectId,showEmpty=true}={}){
  if(!discovery)return;
  if(discovery.status==='revealed'&&typeof discovery.text==='string'){
    const item=text('section','','discovery');item.append(text('h3',discovery.discovered_by===null?'创建时公开的内容':'已经揭示的内容'),text('p',discovery.text));
    if(discovery.discovered_by)item.append(text('p','揭示者：'+name(discovery.discovered_by)+' · '+time(discovery.discovered_at),'small'));
    else if(discovery.discovered_by===null)item.append(text('p','作者创建时公开的内容。','small'));
    if(discovery.text_truncated){item.append(text('p','这是背包中的正文摘要，完整内容需要仔细查看。','small'));if(objectId)item.append(button('查看完整发现正文',()=>examine(objectId)));}
    parent.append(item);
  }else if(showEmpty&&discovery.status==='unrevealed')parent.append(text('p','这件物品的内容还未揭示。','small'));
  else if(showEmpty&&discovery.status==='not_authored')parent.append(text('p','没有更多已保存的线索。','small'));
}
function appendKeyRequirement(parent,object){
  const required=object?.key_requirement;if(!required)return;
  const section=text('section','','key-requirement');section.append(text('strong','对应物件：'+required.name));
  if(object.state==='resolved')section.append(text('p','这道障碍已经解决，不需要再次使用物件。','small'));
  else if(required.carried_by_you)section.append(text('p','这件物件正在你的背包里。在障碍所在的地方，可以直接使用，不掷骰。'+(object.consume_key?'使用后会永久耗尽，并从背包移除；不能再收藏或交接。':'这道障碍不消耗它，使用后仍可携带。'),'small'));
  else if(required.owner){section.append(text('p','现在由 '+(required.owner_name||name(required.owner))+' 携带。只有实际拿着这件物件的人才能使用；你可以与对方商量交接。','small'));}
  else section.append(text('p',required.state==='spent'?'对应物件已经永久耗尽，不能再次拿取或交接。':required.state==='sealed'?'这件物件还没有打开，也没有由你持有。需先打开并拿取，才能用它解除障碍。':'这件物件尚未由你持有。实际拿取后，才能用它解除障碍。','small'));
  if(object.state!=='resolved'){if(object.key_required)section.append(text('p','只能实际取得这件对应物件来解除，固定检定和自由方法都不能绕过。','small'));if(object.consume_key&&!required.carried_by_you)section.append(text('p','解除时对应物件会永久耗尽；开放的通路随后供所有旅人使用。','small'));}
  parent.append(section);
}
function storyText(row){const discovery=row.payload?.discovery;if(discovery?.status==='revealed'&&typeof discovery.text==='string'){const suffix='\n发现正文：'+discovery.text;if(row.text.endsWith(suffix))return row.text.slice(0,-suffix.length);}return row.text;}
function appendNewContentSource(parent,row){
  if(row.kind!=='add_objects')return;
  if(row.payload?.author)parent.append(text('p','新内容作者：'+name(row.payload.author)+'；这是此次创作。','small'));
  if(Array.isArray(row.payload?.objects)&&row.payload.objects.length)parent.append(text('p','新增：'+row.payload.objects.map(object=>object.name+'（'+(states[object.state]||object.state)+'）').join('、')+'。','small'));
}
function recordCard(row){
  const item=text('article','','record'),meta=text('div','','record-meta');meta.append(text('strong',name(row.actor)),text('span',actionNames[row.kind]||row.kind),text('span',time(row.occurred_at)));
  if(row.scene_id)meta.append(text('span',mapItems.get(row.scene_id)?.title||'已保存的地点'));
  item.append(meta,text('p',storyText(row)));roll(item,row.payload);appendDiscovery(item,row.payload?.discovery,{showEmpty:false});
  if(row.text_truncated)item.append(text('p','原记录正文被截短，完整内容请到共同记录读取。','small'));
  if(row.kind==='unlock'&&typeof row.payload?.key_consumed==='boolean')item.append(text('p',row.payload.key_consumed?'对应物件已永久耗尽并离开持有者背包，不能再收藏或交接；开放的通路留给所有旅人。':'这次解除没有消耗对应物件；持有和去向仍以最新状态为准。','small'));
  if(row.kind==='expand'&&row.payload?.author)item.append(text('p','地点创作作者：'+name(row.payload.author),'small'));
  appendNewContentSource(item,row);
  if(row.kind==='tell'){if(row.payload?.response_author)item.append(text('p','NPC回应由 '+name(row.payload.response_author)+' 创作，不是服务器自主推理。','small'));else item.append(text('p','这里只留下了问话；没有已保存的 NPC 回应，也没有后台等待队列。','small'));}
  return item;
}
function returnPreview(row){
  const item=text('article','','record'),meta=text('div','','record-meta');
  meta.append(text('strong',name(row.actor)),text('span',actionNames[row.kind]||row.kind),text('span',time(row.occurred_at)));
  if(row.scene_id)meta.append(text('span',mapItems.get(row.scene_id)?.title||'已保存的地点'));
  const body=storyText(row);item.append(meta,text('p',body.length>140?body.slice(0,140)+'…（摘要，全文见共同记录）':body));
  roll(item,row.payload);
  if(row.text_truncated)item.append(text('p','原记录仍有截断，全文见共同记录。','small'));
  if(row.payload?.discovery?.status==='revealed')item.append(text('p','这条记录还有已揭示的正文，可展开或到共同记录查看。','small'));
  if(row.kind==='tell')item.append(text('p',row.payload?.response_author?'NPC回应作者：'+name(row.payload.response_author)+'。':'仅记录了问话，没有已保存的 NPC 回应。','small'));
  if(row.kind==='expand'&&row.payload?.author)item.append(text('p','地点作者：'+name(row.payload.author)+'。','small'));
  appendNewContentSource(item,row);
  if(row.kind==='unlock'&&typeof row.payload?.key_consumed==='boolean')item.append(text('p',row.payload.key_consumed?'对应物件已永久耗尽。':'本次未消耗对应物件。','small'));
  return item;
}
function renderActivity(){
  if(!snapshot?.scene||!Number.isInteger(snapshot.history_latest_seq))return;
  const latest=snapshot.history_latest_seq,window=observedRange(history.items,Math.max(0,latest-40),latest),sceneRows=window.rows.filter(row=>row.scene_id===snapshot.scene.id).reverse();
  const returning=entryCheckpoint?observedRange(history.items,entryCheckpoint.seen_seq,latest):null,changes=characterChanges(entryCheckpoint,snapshot);
  const signature=JSON.stringify([snapshot.scene.id,latest,history.items.size,window.gaps,changes,entryCheckpoint?.seen_seq,checkpointWarning]);
  if(signature!==activityRenderKey){
    activityRenderKey=signature;const host=$('scene-activity');host.replaceChildren(...sceneRows.slice(0,2).map(returnPreview));
    $('scene-activity-more').replaceChildren(...sceneRows.slice(0,8).map(recordCard));$('scene-activity-details').hidden=!sceneRows.length;
    $('scene-activity-more-label').textContent='展开最近 '+Math.min(8,sceneRows.length)+' 条已读现场记录';
    if(!sceneRows.length)host.append(text('p',history.latestLoaded?'已读的近期记录里，暂时没有这个地点的交谈或行动。':'正在读取这个地点的近期交谈与行动…','small'));
    $('scene-activity-status').textContent='先显示最近 '+Math.min(2,sceneRows.length)+' 条同地点摘要，展开可看最近 '+Math.min(8,sceneRows.length)+' 条已读记录。'+(sceneRows.length>8?' 较早的已读记录请到共同记录查看。':'')+(window.complete?' 这仅覆盖近期，不是全部历史，也不代表其他旅人在线。':' 近期世界记录仍有未读缺口，不能断定这里无人互动；请到共同记录补全。');
    $('return-card').hidden=!entryCheckpoint&&!checkpointWarning;$('return-differences').replaceChildren(...changes.map(value=>text('p',value)));const returnHost=$('return-changes');returnHost.replaceChildren();
    if(entryCheckpoint){
      $('return-anchor').textContent='本机上次为这个身份显示到第 '+entryCheckpoint.seen_seq+' 条记录 · '+new Date(entryCheckpoint.saved_at).toLocaleString()+'。下面与那次观察相比，不把描写当成已执行的结果。';
      if(!changes.length)$('return-differences').append(text('p','当前位置、生命和持有物与本机留存的那次观察相同；其他变化见下面的真实记录。'));
      returnHost.append(...returning.rows.slice(-3).reverse().map(returnPreview));
      if(returning.rolledBack)$('return-status').textContent='本机留存的序号晚于当前世界记录，可能连接了不同快照；不能推断这段期间发生了什么，请核对当前世界。';
      else if(returning.gaps.length)$('return-status').textContent='上次之后已有 '+(latest-entryCheckpoint.seen_seq)+' 条世界记录，已读取 '+returning.rows.length+' 条，最近 '+Math.min(3,returning.rows.length)+' 条摘要可展开。未读范围：'+returning.gaps.slice(0,3).map(gap=>gap.from===gap.to?'第 '+gap.from+' 条':'第 '+gap.from+'～'+gap.to+' 条').join('、')+(returning.gaps.length>3?'等':'')+'；补看后才能判断完整变化。';
      else $('return-status').textContent=returning.rows.length?'上次之后 '+returning.rows.length+' 条世界记录已读取；最近 '+Math.min(3,returning.rows.length)+' 条摘要可展开，全文可在共同记录查看。':'当前世界还没有比那次观察更新的已保存记录；这不代表外部工具已经停止。';
    }else{$('return-anchor').textContent='这里没有可用的上次观察位置。';$('return-status').textContent=checkpointWarning;}
    $('return-more').hidden=!returning||returning.rolledBack||!returning.gaps.length;$('return-card').classList.toggle('has-unread',!!returning&&!returning.rolledBack&&!!returning.gaps.length);
    if(checkpointWarning&&entryCheckpoint)$('return-status').append(text('span',' '+checkpointWarning));
  }
  const displayed=pageId()==='history'?history.rows.filter(row=>row.seq<=latest):[...sceneRows.slice(0,$('scene-activity-details').open?8:2),...($('return-details').open?returning?.rows.slice(-3)||[]:[])];
  // Keep the old return anchor until every newer range is fetched. Showing the
  // latest card alone must not erase older gaps on a subsequent return.
  if(!returning||returning.complete)saveCheckpoint(displayed.length?Math.max(...displayed.map(row=>row.seq)):latest===0?0:null);
}
async function readReturnHistory(){
  if(returnHistoryFlight||!entryCheckpoint||!snapshot)return returnHistoryFlight;
  const session=generation,model=history,range=observedRange(model.items,entryCheckpoint.seen_seq,snapshot.history_latest_seq),after=range.gaps[0]?.from-1;
  if(!Number.isInteger(after)||range.rolledBack)return;
  returnHistoryFlight=(async()=>{const page=await client.query('frontier.history',{after,limit:20});if(session!==generation||!current()||history!==model)return;model.merge(page,after);renderHistory();})().finally(()=>{if(session===generation&&history===model)returnHistoryFlight=null;});return returnHistoryFlight;
}
function showResult(result){
  announce('这次操作已提交，以下为服务器保存的结果。');const host=$('last-result');host.replaceChildren();$('last-result-card').hidden=false;
  if(result.result?.method==='key_object'){host.append(text('p','用“'+(result.key_object?.name||'对应物件')+'”解除了“'+(result.object?.name||'这道障碍')+'”。'+(result.result.key_consumed?'这件对应物件已永久耗尽，并从你的背包移除，不能再收藏或交接。':'本次未消耗对应物件。')+'若障碍挡着通路，对应通路已经对所有旅人开放。'),text('p','按对应物件直接解锁，没有掷骰或奖励。','small'));}
  else if(result.created_scene){host.append(text('p','“'+result.created_scene.title+'”已经进入共享世界。提交者：'+name(result.created_scene.creator)+'。'+(result.exit?.blocked?'原出口仍被物件阻挡，先处理障碍才能前往。':'现在可以从原出口前往。')));}
  else if(Array.isArray(result.created_objects)){host.append(text('p','已为“'+result.scene.title+'”加入 '+result.created_objects.length+' 件新内容。作者：'+name(result.author)+'；它们来自这次创作。'));for(const object of result.created_objects){const item=text('section','','scene-item');item.append(text('h3',object.name+' · '+(states[object.state]||object.state)));appendAuthoredDescription(item,object);appendDiscovery(item,object.discovery,{objectId:object.id});host.append(item);}}
  else if(result.entry){host.append(text('p',result.entry.text||'这句话已经保存在共同记录中。'));}
  else if(result.request){host.append(text('p',statuses[result.request.status]||result.request.status));if(result.request.reason)host.append(text('p',result.request.reason));}
  else if(result.scene&&result.object?.state==='open'&&result.object.owner===null){host.append(text('p','“'+result.object.name+'”已放在 '+result.scene.title+'，不再归你持有；其他到场旅人可以拿取。'));}
  else if(result.scene){host.append(text('p','已来到 '+result.scene.title+'。'));}
  else if(result.object){host.append(text('p',result.object.name+' · '+(states[result.object.state]||result.object.state)));}
  else if(result.character){host.append(text('p','角色状态已经保存。生命 '+result.character.hp+' / '+result.character.max_hp+'。'));}
  else host.append(text('p','这次操作已经提交，请读取最新现场和共同记录。'));
  roll(host,result.result);
  if(Number.isInteger(result.result?.roll)){host.append(text('p',result.result.success?'检定结果已结算；实际影响见最新物件和记录。':'检定失败，按本世界规则没有伤害；同一人物不能在这个物件版本上重复掷骰。','small'));}
  appendKeyRequirement(host,result.object);
  if(result.object)appendDiscovery(host,result.object.discovery,{objectId:result.object.id});else appendDiscovery(host,result.result?.discovery,{showEmpty:false});
  appendNpcContinuation(host,result);
}
function actionButton(action){const node=button(action.label,()=>perform(action),{cls:'action-tile'+(action.function_id==='frontier.check'?' risky':''),write:action.function_id!=='frontier.examine'});if(action.function_id==='frontier.check'){const object=objects.get(action.arguments.object_id),check=object?.check;if(check)node.append(text('small',(abilities[check.ability]||check.ability)+' '+signed(Math.floor((snapshot.character.abilities[check.ability]-10)/2))+' · DC '+check.dc));}else{const hint=typeof action.hint==='string'?action.hint:actionHints[action.function_id];if(hint)node.append(text('small',hint));}return node;}
async function perform(action){
  if(!action||typeof action.function_id!=='string'||!action.arguments)throw new Error('行动信息缺失，请读取最新现场。');
  if(action.function_id==='frontier.examine'){await examine(action.arguments.target_id);return;}
  if(action.function_id==='frontier.expand'){openCreation(action.arguments);return;}
  if(action.function_id==='frontier.add_objects'){openCreation(action.arguments,'objects');return;}
  if(action.function_id==='frontier.say'){$('say-text').focus();$('say-form').scrollIntoView({block:'center',behavior:'smooth'});return;}
  if(action.function_id==='frontier.visit'){const result=await mutate('frontier.visit',{});announce(result.additional_task?'已领取一道附加题，先看原方法和世界事实。':'目前没有可领取的他人行动。');await refresh();$('side-question-card').scrollIntoView({block:'center',behavior:'smooth'});return;}
  if(action.function_id==='frontier.tell'){const form=$('npc-form-'+action.arguments.npc_id);form?.scrollIntoView({block:'center',behavior:'smooth'});form?.querySelector('textarea')?.focus();return;}
  if(action.function_id==='frontier.request'){$('intent-target').value=action.arguments.target_id;$('proposal-details').open=true;$('intent').focus();return;}
  if(action.function_id==='frontier.transfer'){announce('请在随身物品中选择同一地点的接收者，再交付。');return;}
  // Ordinary movement, taking and resting remain direct button actions.
  // Confirm random checks and using an item to permanently open a shared route.
  let description=['frontier.check','frontier.unlock','frontier.drop'].includes(action.function_id)&&typeof action.risk==='string'?action.risk:null;
  if(action.function_id==='frontier.check'){const object=objects.get(action.arguments.object_id),check=object?.check;if(check)description=(abilities[check.ability]||check.ability)+'检定 '+signed(Math.floor((snapshot.character.abilities[check.ability]-10)/2))+' · DC '+check.dc+'\n成功：'+check.success+'\n失败：'+check.failure+'\n接受后服务器掷一次d20。';}
  const session=generation;if(description&&!await risk(action.label,description)){announce('已取消，没有提交行动。');return;}if(session!==generation||!current())return;
  const result=await mutate(action.function_id,action.arguments);if(session!==generation)return;showResult(result);await refresh();
}
async function examine(targetId){
  const session=generation,result=await client.query('frontier.examine',{target_id:targetId});if(session!==generation||!current())return;
  const host=$('last-result');host.replaceChildren(text('h3',result.target.name));
  if(Object.hasOwn(kinds,result.target.kind)){host.append(text('p','当前物件：'+(states[result.target.state]||result.target.state),'small'));appendAuthoredDescription(host,result.target);appendKeyRequirement(host,result.target);appendDiscovery(host,result.target.discovery,{objectId:result.target.id});}
  else host.append(text('p','NPC 设定','small'),text('p',result.target.description));
  for(const fact of result.facts||[])host.append(text('p','作者设定：'+fact,'small'));for(const action of result.available_actions||[])host.append(actionButton(action));$('last-result-card').hidden=false;controls();
}
function renderCharacter(){
  const c=snapshot.character;for(const id of ['character-stats','character-abilities','inventory','character-detail'])$(id).replaceChildren();
  if(!c){$('character-name').textContent='尚未创建人物';$('character-class').textContent='先核对上次提交，再进入世界。';$('character-detail').append(button('创建人物，进入渡鸦溪',async()=>{showResult(await mutate('frontier.join',{name:$('name').value.trim()||profile.display_name||'渡鸦溪旅人',class_id:document.querySelector('input[name=class_id]:checked').value}));await refresh();},{write:true}));return;}
  $('who').textContent=c.name;$('character-name').textContent=c.name;$('character-class').textContent=c.class_name+' · '+c.level+'级';
  for(const[value,caption]of [[c.hp+' / '+c.max_hp,'生命值'],[c.ac,'护甲 AC'],[c.inventory.length,'随身物件']]){const item=text('div','','stat');item.append(text('strong',value),text('small',caption));$('character-stats').append(item);}
  for(const[id,value]of Object.entries(c.abilities)){const item=text('span');item.append(text('span',abilities[id]||id),text('strong',value));$('character-abilities').append(item);}
  const list=text('ul','','inventory-list');for(const id of c.inventory){const o=objects.get(id),item=text('li',o?o.name:'物件 '+id.slice(-8));appendDiscovery(item,o?.discovery,{objectId:id,showEmpty:false});item.append(button('看看这件物品',()=>examine(id)));const drop=snapshot.available_actions.find(action=>action.function_id==='frontier.drop'&&action.arguments.object_id===id);if(drop)item.append(actionButton(drop));const recipients=snapshot.nearby.filter(other=>other.role_id!==c.role_id);if(recipients.length){const preferred=client.draft('frontier-recipient:'+id),recipient=select(recipients.map(other=>[other.role_id,name(other.role_id)]),recipients.some(other=>other.role_id===preferred)?preferred:recipients[0].role_id);recipient.setAttribute('aria-label','将'+(o?.name||'物件')+'交给谁');recipient.addEventListener('change',()=>{try{client.saveDraft('frontier-recipient:'+id,recipient.value);}catch(error){showError(error);}});const actions=text('div','','actions');actions.append(recipient,button('交付',async()=>{const recipientId=recipient.value,version=snapshot.character.version;if(!await risk('交付 '+(o?.name||'物件'),'把唯一的这件物品交给 '+name(recipientId)+'。提交后，物件从你的背包移到对方背包。'))return;showResult(await mutate('frontier.transfer',{object_id:id,recipient_id:recipientId,expected_character_version:version}));await refresh();},{write:true}));item.append(actions);}list.append(item);}
  if(!list.children.length)list.append(text('li','还没有拿到独立物件。'));$('inventory').append(list);$('character-detail').append(text('p','人物和背包会保留。离开网页后，你的其他连接仍可继续行动。'));
}
function renderScene(){
  const scene=snapshot.scene,c=snapshot.character;if(!scene){$('scene-title').textContent='世界入口';$('scene-description').textContent='核对上次操作后，可以用同一身份创建人物。';for(const id of ['scene-npcs','scene-objects','scene-current-ground','available-actions','open-exits','nearby'])$(id).replaceChildren();return;}
  $('scene-title').textContent=scene.title;$('scene-description').textContent=scene.authored_description;$('scene-state').textContent='已保存 · 版本 '+scene.version;$('scene-author').textContent='地点原稿作者：'+name(scene.creator)+' · '+time(scene.created_at);
  const ground=scene.current_ground;$('scene-current-ground').replaceChildren();
  if(ground?.complete===true&&Array.isArray(ground.objects)&&ground.count===ground.objects.length){
    $('scene-current-ground').append(text('strong',ground.count?'地面现有：'+ground.objects.map(object=>object.name+'（'+(states[object.state]||object.state)+'）').join('、'):'这里目前没有地面物件。'),text('p','这是当前完整地面列表。未列出的物件不在这里的地面上，不能按旧描写直接拿取。','small'));
  }
  $('scene-current-ground').hidden=!$('scene-current-ground').children.length;
  const nearby=snapshot.nearby.filter(other=>other.role_id!==c.role_id);$('nearby').replaceChildren();if(nearby.length){$('nearby').append(text('span','同一地点：','small'));for(const other of nearby)$('nearby').append(text('span',name(other.role_id),'traveler-chip'));}else $('nearby').append(text('span','你暂时没有遇见其他旅人。','small'));
  renderFreshness();
  const signature=JSON.stringify([scene.id,scene.version,c.version,scene.objects.map(object=>[object.id,object.version,object.state,object.owner,object.key_requirement,object.key_required,object.consume_key]),scene.exits.map(exit=>[exit.id,exit.version,exit.destination_id,exit.blocked]),scene.npcs.map(npc=>[npc.id,npc.version]),nearby.map(other=>[other.role_id,other.version]),snapshot.available_actions]);
  if(signature===sceneRenderKey)return;sceneRenderKey=signature;
  const focused=document.activeElement,focusedNpc=$('scene-npcs').contains(focused)?{id:focused.id,start:focused.selectionStart,end:focused.selectionEnd}:null;
  $('scene-npcs').replaceChildren();for(const npc of scene.npcs){const item=text('article','','scene-item');item.append(text('h3',npc.name),text('p',npc.description),text('p','设定作者：'+name(npc.creator),'small'));const form=document.createElement('form');form.id='npc-form-'+npc.id;const input=document.createElement('textarea');input.id='npc-text-'+npc.id;input.rows=2;input.maxLength=1200;input.placeholder='想问点什么？';input.value=client.draft('frontier-npc:'+npc.id);input.addEventListener('input',()=>{try{client.saveDraft('frontier-npc:'+npc.id,input.value);}catch(error){showError(error);}});form.append(label('对 '+npc.name+' 说',input));const submit=text('button','说给 TA 听','secondary');submit.type='submit';submit.dataset.write='true';form.append(submit,text('p','这会保存你的问话，不会自动回答。提交后，可以把续聊文字复制给自己的 Agent，让它按世界规则创作有署名的接话。','small'));form.addEventListener('submit',event=>{event.preventDefault();run(async()=>{const value=input.value.trim();if(!value)return;const draft=input.value;showResult(await mutate('frontier.tell',{npc_id:npc.id,text:value},{route:'frontier-npc:'+npc.id,draft_text:draft}));input.value=client.draft('frontier-npc:'+npc.id);await refresh();});});item.append(form);$('scene-npcs').append(item);}
  if(focusedNpc&&$(focusedNpc.id)){const input=$(focusedNpc.id);input.focus({preventScroll:true});if(Number.isInteger(focusedNpc.start))input.setSelectionRange(focusedNpc.start,focusedNpc.end);}
  $('scene-objects').replaceChildren();for(const object of scene.objects){const item=text('article','','scene-item');const heading=text('div','','section-heading');heading.append(text('h3',object.name),text('span',states[object.state]||object.state,'tag'));item.append(heading);appendAuthoredDescription(item,object);item.append(text('p',(kinds[object.kind]||object.kind)+' · 原稿作者 '+name(object.creator),'small'));appendKeyRequirement(item,object);appendDiscovery(item,object.discovery,{objectId:object.id});const actions=text('div','','actions');actions.append(button('仔细看',()=>examine(object.id)));for(const action of snapshot.available_actions.filter(a=>a.arguments.object_id===object.id||a.arguments.target_id===object.id)){if(action.function_id==='frontier.examine')continue;actions.append(actionButton(action));}item.append(actions);$('scene-objects').append(item);}
  $('available-actions').replaceChildren();for(const action of snapshot.available_actions){if(['frontier.transfer','frontier.accept','frontier.withdraw'].includes(action.function_id))continue;$('available-actions').append(actionButton(action));}
  $('action-note').textContent=$('available-actions').children.length?'选一件想做的事。有风险或消耗时，先核对后果。':'这里暂时没有可用按钮；可以阅读现场与已有记录。';
  const selected=$('intent-target').value||client.draft('frontier-intent-target'),targets=scene.objects.filter(object=>!object.key_required&&['sealed','obstructed','unread'].includes(object.state)&&!c.attempts.includes(object.id+':'+object.version));$('intent-target').replaceChildren();if($('intent').value&&selected&&!targets.some(o=>o.id===selected)){const missing=text('option','原草稿的对象已不可用，请重新选择');missing.value='';$('intent-target').append(missing);}for(const object of targets){const option=text('option',object.name+' · '+(states[object.state]||object.state));option.value=object.id;$('intent-target').append(option);}if(targets.some(o=>o.id===selected))$('intent-target').value=selected;$('proposal').querySelector('button[type=submit]').dataset.unavailable=String(!$('intent-target').value);
  $('open-exits').replaceChildren();for(const exit of scene.exits.filter(e=>!e.destination_id)){const item=text('div','','open-exit');item.append(text('strong',exit.label),text('p',exit.description));if(exit.blocked)item.append(text('p','可以先创作另一端，通行仍须处理这个出口的障碍。','small'));item.append(button('为这里创作接续地点',()=>openCreation({scene_id:scene.id,exit_id:exit.id,expected_exit_version:exit.version}),{write:true}));$('open-exits').append(item);}if(!$('open-exits').children.length)$('open-exits').append(text('p','这里没有尚未成形的出口。可以在上方为当前地点补充互动内容，或沿已知路前往别处。','small'));
}
function render(){
  for(const item of snapshot.directory)names.set(item.role_id,item.name);for(const object of [...(snapshot.scene?.objects||[]),...(snapshot.inventory_objects||[])])objects.set(object.id,object);
  for(const row of snapshot.map)mapItems.set(row.id,row);
  if(snapshot.scene){const scene=snapshot.scene;mapItems.set(scene.id,{id:scene.id,title:scene.title,version:scene.version,exits:scene.exits});}
  mapTotal=snapshot.map_total;mapCursor=Math.max(mapCursor,snapshot.map_next_after);mapMore=mapCursor<mapTotal;
  renderCharacter();renderScene();renderMap();renderRequests();renderTask();renderActivity();pending();controls();
}
const svgElement=(tag,attrs={},value)=>{const node=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const[k,v]of Object.entries(attrs))node.setAttribute(k,String(v));if(value!==undefined)node.textContent=value;return node;};
function renderMap(){
  const host=$('world-map');host.replaceChildren();const rows=[...mapItems.values()];
  // A newly generated/current location is authoritative even before its next map page.
  const scene=snapshot?.scene;if(scene&&!mapItems.has(scene.id))rows.push({id:scene.id,title:scene.title,version:scene.version,exits:scene.exits});
  if(!rows.length){host.append(text('p','读取地图后，你们共同留下的地点会出现在这里。','small'));return;}
  const columns=Math.min(4,Math.max(2,Math.ceil(Math.sqrt(rows.length)))),width=columns*165+60,height=Math.ceil(rows.length/columns)*115+65;
  const svg=svgElement('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':'已读取'+rows.length+'个地点的地理关系图；'+(mapMore?'还有地点未读取。':'地图完整。')});svg.style.minWidth=Math.min(width,650)+'px';
  svg.append(svgElement('path',{d:`M 8 ${height*.68} Q ${width*.35} ${height*.2} ${width*.55} ${height*.65} T ${width+10} ${height*.4}`,fill:'none',stroke:'#c7d9ce','stroke-width':24,opacity:.58}));
  const positions=new Map(rows.map((row,index)=>[row.id,{x:60+(index%columns)*165,y:45+Math.floor(index/columns)*115}]));const links=new Set();
  for(const row of rows){const start=positions.get(row.id);for(const exit of row.exits){if(exit.destination_id&&positions.has(exit.destination_id)){const key=[row.id,exit.destination_id].sort().join(':');if(links.has(key))continue;links.add(key);const end=positions.get(exit.destination_id);svg.append(svgElement('line',{x1:start.x,y1:start.y,x2:end.x,y2:end.y,class:'map-path',opacity:exit.blocked?0.45:1}));}else if(!exit.destination_id){svg.append(svgElement('line',{x1:start.x+28,y1:start.y-13,x2:start.x+55,y2:start.y-33,class:'map-exit'}),svgElement('circle',{cx:start.x+59,cy:start.y-36,r:5,fill:'#f7f7e9',stroke:'#a9b994','stroke-width':2}));}}}
  for(const row of rows){const pos=positions.get(row.id),isCurrent=scene?.id===row.id;const move=snapshot?.available_actions.find(action=>action.function_id==='frontier.move'&&action.arguments.destination_id===row.id);const group=svgElement('g',{class:'map-node'+(isCurrent?' current':''),transform:`translate(${pos.x} ${pos.y})`,tabindex:'0',role:'button','aria-label':row.title+(isCurrent?'，你在这里':move?'，可以前往':'，需沿已知出口前往'),'aria-disabled':!isCurrent&&!move});group.append(svgElement('title',{},row.title),svgElement('circle',{r:27}),svgElement('path',{d:'M 0 -13 L 12 0 L 0 13 L -12 0 Z',fill:isCurrent?'#4b704e':'#a1b18b'}),svgElement('text',{y:48,'text-anchor':'middle'},row.title.length>10?row.title.slice(0,10)+'…':row.title));
    const occupants=snapshot?.directory.filter(person=>person.scene_id===row.id).length||0;if(occupants)group.append(svgElement('text',{y:65,'text-anchor':'middle',class:'map-label'},isCurrent?'你在这里 · '+occupants+'位旅人':occupants+'位旅人'));
    const activate=()=>{if(busy||!current())return;if(move)run(()=>perform(move));else announce(isCurrent?'你就在这里。':row.title+'是已保存的地点，需要沿当前地点的已知出口前往。');};group.addEventListener('click',activate);group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();activate();}});svg.append(group);
  }
  host.append(svg);$('map-count').textContent=rows.length+' / '+Math.max(mapTotal,rows.length)+' 地点';$('map-more').hidden=!mapMore;$('map-note').textContent='线段是已保存的通路，空心端点是尚未创作的出口。地图按关系排列，不代表真实距离。'+(mapMore?' 还有地点未读取，请继续补全。':' 已读取当前完整地图。');
}
async function readMap(){
  if(mapFlight)return mapFlight;const session=generation,after=mapCursor;
  mapFlight=(async()=>{const page=await client.query('frontier.map',{after,limit:32});if(session!==generation||!current())return;validatePage(page,after);for(const row of page.items){if(typeof row.id!=='string'||!Array.isArray(row.exits))throw new Error('地图信息不完整。');mapItems.set(row.id,row);}mapCursor=page.next_after;mapMore=page.has_more;mapTotal=page.total;renderMap();})().finally(()=>{if(session===generation)mapFlight=null;});return mapFlight;
}
function offerText(offer){return (abilities[offer.ability]||offer.ability)+'检定 '+signed(offer.modifier)+' · DC '+offer.dc+'\n'+terms(offer.terms)+'\n接受后才掷骰，结果不能事后撤回。';}
async function fullRequest(request){
  const box=request.owner===profile.role_id?'own':'received',session=generation;
  const page=await client.query('frontier.requests',{box,after:request.created_seq-1,limit:1});
  if(session!==generation||!current()){const error=new Error('身份已改变。');error.cancelled=true;throw error;}
  validatePage(page,request.created_seq-1);const value=page.items.find(item=>item.request_id===request.request_id);
  if(!value)throw new Error('没有读到这项行动的完整当前记录，请读取最新世界。');
  return value;
}
function requestCard(request,{historical=false}={}){
  const item=text('article','','request-card'+(historical?' history-request':'')),heading=text('div','','section-heading');heading.append(text('h3',name(request.owner)+'的办法'),text('span',statuses[request.status]||request.status,'tag'));item.append(heading,text('p',request.text),text('p','对象：'+(objects.get(request.target_id)?.name||'物件 '+request.target_id.slice(-8))+' · 提出于 '+time(request.created_at),'small'));
  if(request.assignee)item.append(text('p','领取者：'+name(request.assignee),'small'));if(request.reason)item.append(text('p','判断理由：'+request.reason));
  if(request.text_truncated||request.reason_truncated){item.append(text('p','这是方法或理由的摘要，完整内容见行动记录。','small'),button('读取完整方法与理由',async()=>{const value=await fullRequest(request);$('last-result').replaceChildren(requestCard(value,{historical:true}));$('last-result-card').hidden=false;}));}
  if(request.offer){item.append(text('div',(historical||request.status!=='offered'?'过去保存的方案\n':'当前等待本人决定的方案\n')+offerText(request.offer),'request-offer'));}
  roll(item,request.result);
  appendDiscovery(item,request.result?.discovery,{showEmpty:false});
  if(!historical&&request.owner===profile.role_id){const actions=text('div','','actions');if(request.status==='offered'&&request.offer){actions.append(button('接受风险并结算',async()=>{const session=generation;await refresh();const latest=await fullRequest(request);if(latest.status!=='offered'||!latest.offer)throw new Error('这份方案已不再等待接受，请读取最新状态。');if(snapshot.character.scene_id!==latest.scene_id)throw new Error('请先回到提出这个办法的地点，核对对象后再接受。');const object=snapshot.scene.objects.find(item=>item.id===latest.target_id);if(!object||object.key_required||object.version!==latest.target_version||!['sealed','obstructed','unread'].includes(object.state))throw new Error('方案对应的对象已经改变，旧方案不能继续结算。请撤回后按当前状态重新选择。');if(!await risk('接受这个行动方案','你的原办法：\n'+latest.text+'\n\n判断理由：\n'+latest.reason+'\n\n服务器条款：\n'+offerText(latest.offer)))return;if(session!==generation||!current())return;showResult(await mutate('frontier.accept',{request_id:latest.request_id}));await refresh();},{write:true}));}if(['pending','assigned','offered'].includes(request.status))actions.append(button('撤回',async()=>{if(!await risk('撤回这个办法','撤回后不会执行；如果对方已经回答，保存的理由仍会留在记录中。'))return;showResult(await mutate('frontier.withdraw',{request_id:request.request_id}));await refresh();},{write:true}));item.append(actions);}
  return item;
}
function renderRequests(){const rows=snapshot.own_requests;$('my-requests-card').hidden=!rows.length;$('my-requests').replaceChildren(...rows.map(row=>requestCard(row)));}
function appendTaskKeyRequirement(parent,task){
  const required=task.target?.key_requirement;if(!required)return;
  const section=text('section','','key-requirement');section.append(text('strong','这道障碍需要：'+required.name),text('p','物件标识：'+required.object_id+' · 状态：'+(states[required.state]||required.state),'small'));
  section.append(text('p',required.owner?'当前持有人：'+(required.owner_name||name(required.owner)):'当前没有持有人。','small'));
  if(required.owner===task.request.owner)section.append(text('p','提出者已经实际持有对应物件。'+(task.target.state==='obstructed'?'在障碍所在的地方，他可以考虑直接使用，不掷骰；是否采用及物件代价仍由他决定。':'不过这道障碍当前已经解决，请按最新状态判断，不能把过去的阻碍当成仍待解锁。'),'small'));
  else if(required.owner===profile.role_id)section.append(text('p','这件物件在你的背包中，不在提出者手中。帮忙判断不会自动交接它，也不能把它算作提出者的资源；必须实际交付后，他才能使用。','small'));
  else section.append(text('p','提出者尚未持有这件物件。不能把别人的物件、尚未发生的交接或文字中的持有声明，当作他已经能用的资源。','small'));
  if(task.target.key_required)section.append(text('p','这个障碍必须由实际持有人使用对应物件，检定或自由方法不能绕过。','small'));
  if(task.target.consume_key)section.append(text('p','解除时对应物件会永久耗尽；不能把它算作解除后仍可收藏或交接的资源。','small'));
  parent.append(section);
}
function renderTask(){
  const task=snapshot.additional_task,signature=JSON.stringify([task?.request?.request_id,task?.request?.version,task?.request?.status,task?.target?.version,task?.target?.state,task?.target?.key_requirement,task?.target?.key_required,task?.target?.consume_key,task?.scene?.version,task?.owner_character?.version,task?.owner_character?.scene_id]);if(signature===taskRenderKey)return;taskRenderKey=signature;const host=$('side-question');host.replaceChildren();
  if(!task){host.append(text('p','目前没有你正在处理的附加题。领取后，它会在同一身份回来时继续保留。','small'));return;}
  host.append(requestCard(task.request,{historical:true}),text('p',task.guidance,'helper-guidance'));
  if(task.request.status!=='assigned'||task.request.assignee!==profile.role_id){host.append(text('p','这道题已不再等待你的回答。过往方案只作记录。','small'));return;}
  if(task.target){host.append(text('p','当前对象：'+task.target.name,'small'));appendAuthoredDescription(host,task.target);appendTaskKeyRequirement(host,task);}if(task.scene){host.append(text('p','地点：'+task.scene.title,'helper-guidance'));appendAuthoredDescription(host,task.scene);for(const fact of task.scene.facts)host.append(text('p','作者设定：'+fact,'small'));}
  const form=document.createElement('form');form.className='helper-form';const decision=select([['reject','不允许这个办法'],['allow','允许有限检定']]);const reason=document.createElement('textarea');reason.rows=3;reason.maxLength=2000;reason.required=true;reason.value=client.draft('frontier-answer:'+task.request.request_id);reason.addEventListener('input',()=>{try{client.saveDraft('frontier-answer:'+task.request.request_id,reason.value);}catch(error){showError(error);}});
  const ability=select(task.rules.abilities.map(id=>[id,abilities[id]||id]),task.rules.fixed_check?.ability);const dc=select(task.rules.dcs.map(value=>[String(value),'DC '+value]),task.rules.fixed_check?.dc);const effect=select(task.rules.effects.map(id=>[id,effects[id]||id]));const fields=text('div');fields.hidden=true;const grid=text('div','','form-grid');grid.append(label('检定属性',ability),label('难度',dc));fields.append(grid,label('允许的效果',effect),text('p',terms(task.rules.terms),'helper-guidance'));decision.addEventListener('change',()=>fields.hidden=decision.value!=='allow');form.append(label('你的判断',decision),fields,label('公开理由',reason));const submit=text('button','提交回答','secondary');submit.type='submit';submit.dataset.write='true';form.append(submit,text('p','只判断原办法，不把新工具、法术或别人的同意写成已有事实。需要改办法时，应拒绝并建议原提出者重新提议。','small'));
  form.addEventListener('submit',event=>{event.preventDefault();run(async()=>{const value=reason.value.trim();if(!value)return;const args={request_id:task.request.request_id,decision:decision.value,reason:value};if(decision.value==='allow')Object.assign(args,{ability:ability.value,dc:Number(dc.value),effect:effect.value});showResult(await mutate('frontier.answer',args,{route:'frontier-answer:'+task.request.request_id,draft_text:reason.value}));await refresh();});});host.append(form);
}
function renderHistory(){
  const host=$('history-list');host.replaceChildren();const rows=history.rows;let previous=history.latestSeq+1;
  for(const row of rows){if(previous-row.seq>1)host.append(text('p','第 '+(row.seq+1)+'～'+(previous-1)+' 条记录尚未读取，请补全历史。','record-gap small'));host.append(recordCard(row));previous=row.seq;}
  if(rows.length&&previous>1)host.append(text('p','第 1～'+(previous-1)+' 条较早记录尚未读取。','record-gap small'));if(!rows.length)host.append(text('p',history.latestLoaded&&history.latestSeq===0?'世界里还没有已经提交的记录。':'还未读取共同记录。','small'));
  $('history-more').hidden=!history.hasMore;$('history-status').textContent='已读取 '+rows.length+' / '+history.latestSeq+' 条；'+(history.highestLoaded===history.latestSeq?'已显示当前已知的最新记录。':'新记录尚未读到，正在接续或可点“读取最新记录”。')+(history.hasMore?' 较早历史仍有未读取部分，已连续补全到第 '+history.olderAfter+' 条。':' 所有当前已知记录均已读取。');
  renderActivity();
}
async function readLatestHistory(){
  if(latestHistoryFlight)return latestHistoryFlight;
  const session=generation,model=history,anchor=model.latestSeq;
  if(!model.needsLatest)return;
  const active=()=>session===generation&&current()&&history===model;
  latestHistoryFlight=(async()=>{
    if(anchor===0){model.latestLoaded=true;renderHistory();return;}
    // Fetch the latest known record first, so a long/truncated page cannot hide it.
    const head=await client.query('frontier.history',{after:anchor-1,limit:1});if(!active())return;model.merge(head,anchor-1);renderHistory();
    // Freeze this observed tail. Concurrent arrivals wait for the next bounded read.
    let after=Math.max(0,anchor-40);
    for(let pages=0;pages<3&&after<anchor;pages++){
      const page=await client.query('frontier.history',{after,limit:Math.min(40,anchor-after)});if(!active())return;model.merge(page,after);renderHistory();if(page.next_after<=after)break;after=page.next_after;
    }
    if(!active())return;model.latestLoaded=true;renderHistory();
  })().finally(()=>{if(session===generation&&history===model)latestHistoryFlight=null;});return latestHistoryFlight;
}
async function readOlderHistory(){
  if(olderHistoryFlight)return olderHistoryFlight;const session=generation,model=history,after=model.olderAfter;
  olderHistoryFlight=(async()=>{const page=await client.query('frontier.history',{after,limit:40});if(session!==generation||!current()||history!==model)return;model.merge(page,after);renderHistory();})().finally(()=>{if(session===generation&&history===model)olderHistoryFlight=null;});return olderHistoryFlight;
}
async function readRequests(){const session=generation,box=$('request-box').value,state=requestPages[box],after=state.cursor,page=await client.query('frontier.requests',{box,after,limit:8});if(session!==generation||!current())return;validatePage(page,after);for(const row of page.items)state.items.set(row.request_id,row);state.cursor=page.next_after;state.more=page.has_more;state.total=page.total;if(box===$('request-box').value)renderRequestHistory();}
function renderRequestHistory(){const state=requestPages[$('request-box').value];$('request-history').replaceChildren(...[...state.items.values()].map(row=>requestCard(row,{historical:true})));$('request-more').hidden=!state.more;$('request-status').textContent='已读取 '+state.items.size+' / '+state.total+' 项。这里的过去方案不会显示成当前待答。';}
async function readGuide(){const session=generation,chapter=$('guide-chapter').value,page=await client.query('frontier.handbook',{chapter});if(session!==generation||!current()||chapter!==$('guide-chapter').value)return;if(typeof page.text!=='string')throw new Error('手册格式不完整。');$('guide-content').replaceChildren(text('h3',page.title),text('p',page.text));}
async function readCredentials(){const session=generation,page=await client.listCredentials({isCurrent:()=>session===generation&&current()});if(session!==generation)return;$('credentials-status').textContent='连接状态读取于 '+new Date().toLocaleTimeString()+'；不代表外部 Agent 正在运行。';const host=$('credentials');host.replaceChildren();for(const row of page.credentials){const item=text('div','','credential'),detail=text('div');detail.append(text('strong',row.is_current?'当前网页':'连接 '+row.token_id.slice(-6)),text('small',(row.access_mode==='observe'?'只读观察':'读取与行动')+' · '+({active:'有效',expired:'已到期',revoked:'已撤销'}[row.status])),text('small','签发 '+time(row.created_at)+' · 到期 '+time(row.expires_at)));item.append(detail);if(!row.is_current&&row.status==='active')item.append(button('撤销',async()=>{if(!await risk('撤销这一连接','该连接将无法继续访问；这不会停止对方本地的 AI 工具。持钥者仍可重新授权。'))return;await client.revokeCredential(row.token_id,{isCurrent:()=>session===generation&&current()});announce('已撤销这一连接的访问权限。');await readCredentials();}));host.append(item);}}
function pageId(){const id=location.hash.slice(1);return ['world','history','guide','connections'].includes(id)?id:'world';}
async function showPage(){const id=pageId(),session=generation;for(const page of ['world','history','guide','connections'])$(page).hidden=id!==page;document.querySelectorAll('.tabs a').forEach(link=>link.dataset.page===id?link.setAttribute('aria-current','page'):link.removeAttribute('aria-current'));if(!current())return;if(id==='world'){await refresh();if(session!==generation||!current()||pageId()!==id)return;renderActivity();}if(id==='history'){await refresh();if(session!==generation||!current()||pageId()!==id)return;await readLatestHistory();if(session!==generation||!current()||pageId()!==id)return;await readRequests();}if(id==='guide')await readGuide();if(id==='connections')await readCredentials();}
function leave(){try{saveDrafts();}catch(error){showError(error);}++generation;++readSeq;clearTimeout(timer);client.cancelRequests();agentClient?.cancelRequests();if($('risk-dialog').open)$('risk-dialog').close('cancel');if($('create-dialog').open)$('create-dialog').close();client.token=null;profile=null;snapshot=null;mapItems.clear();history=new FrontierHistory();latestHistoryFlight=null;olderHistoryFlight=null;names.clear();objects.clear();sceneRenderKey='';taskRenderKey='';createContext=null;$('entry').hidden=false;$('app').hidden=true;$('error').hidden=true;$('last-result-card').hidden=true;announce('已离开网页。角色与已提交内容继续保留，其他连接的权限没有被撤销。');readEntryIdentity();controls();}

function creationRoute(){return 'frontier-create:'+createContext.scene_id+':'+(createMode==='objects'?'add-objects':createContext.exit_id);}
function creationValue(){const value={title:$('create-name').value,description:$('create-description').value,facts:$('create-facts').value,paired_barrier:$('create-object-use-key').checked,npcs:[],objects:[],exits:[]};for(let i=0;i<2;i++){value.npcs.push({name:$('create-npc-name-'+i).value,description:$('create-npc-description-'+i).value});value.objects.push({name:$('create-object-name-'+i).value,description:$('create-object-description-'+i).value,kind:$('create-object-kind-'+i).value,initial_state:$('create-object-state-'+i).value,blocks_exit:$('create-object-block-'+i).value,key_required:i===1&&$('create-key-required').checked,consume_key:i===1&&$('create-consume-key').checked,discovery_text:$('create-object-discovery-'+i).value});value.exits.push({label:$('create-exit-name-'+i).value,description:$('create-exit-description-'+i).value});}return value;}
function saveCreation(){if(createContext)client.saveDraft(creationRoute(),JSON.stringify(creationValue()));}
function syncCreationObjectState(index,preferred){
  const kind=$('create-object-kind-'+index).value,control=$('create-object-state-'+index),previous=preferred??control.value,options=initialStates[kind];
  const desired=options.some(([id])=>id===previous)?previous:(kind!=='barrier'&&['open','read'].includes(previous)?options[1][0]:options[0][0]);
  control.replaceChildren(...options.map(([id,caption])=>{const option=text('option',caption);option.value=id;return option;}));control.value=desired;control.disabled=kind==='barrier';
  const publicContent=['open','read'].includes(desired);$('create-object-discovery-label-'+index).firstChild.textContent=(publicContent?'直接可见的正文':'成功后揭示的正文')+'（文字类必填，其他可选）';
  $('create-object-discovery-note-'+index).textContent=publicContent?'先保存这段内容，物件创建时即公开，其他旅人不用检定就能阅读。若未填写，就没有更多已保存的线索。':'先保存这段内容，成功解决物件后再揭示。若未填写，成功不会读出新线索。创作者知道自己写的正文；这不是对创作者保密的谜底。';
}
function syncCreationPairing(){
  const eligible=createMode!=='objects'&&$('create-object-kind-0').value==='cache',paired=eligible&&$('create-object-use-key').checked;
  $('create-object-pairing').hidden=!eligible;
  if(paired)$('create-object-kind-1').value='barrier';
  $('create-object-kind-1').disabled=paired;
  $('create-key-options').hidden=!paired;
  for(let i=0;i<2;i++){$('create-object-block-'+i).closest('label').hidden=createMode==='objects'||$('create-object-kind-'+i).value!=='barrier';syncCreationObjectState(i);}
}
function creationPackage(value){
  const adding=createMode==='objects',facts=adding?[]:value.facts.split('\n').map(line=>line.trim()).filter(Boolean);
  if(facts.length>6||facts.some(fact=>fact.length>300))throw new Error('事实最多六项，每项最多300字。');
  const paired=!adding&&value.paired_barrier&&value.objects[0].kind==='cache';
  if(paired&&value.objects.some(o=>!o.name.trim()||!o.description.trim()))throw new Error('配对时，请填写第一件容器和第二件障碍的名字、描述。容器会成为这道障碍实际需要的物件。');
  const exitPositions=adding?[]:value.exits.map((exit,index)=>exit.label.trim()||exit.description.trim()?index:null).filter(index=>index!==null);
  const packageValue={title:value.title.trim(),description:value.description.trim(),facts,
    npcs:adding?[]:value.npcs.filter(n=>n.name.trim()||n.description.trim()).map(n=>({name:n.name.trim(),description:n.description.trim()})),
    objects:value.objects.map((o,index)=>({...o,index})).filter(o=>o.name.trim()||o.description.trim()||o.discovery_text.trim()).map(o=>({
      name:o.name.trim(),description:o.description.trim(),kind:o.kind,initial_state:o.initial_state,
      ...(!adding&&o.kind==='barrier'&&o.blocks_exit!==''?{blocks_exit:exitPositions.indexOf(Number(o.blocks_exit))}:{}),
      ...(paired&&o.index===1?{key_object:0,key_required:o.key_required===true,consume_key:o.consume_key===true}:{}),
      ...(o.discovery_text.trim()?{discovery_text:o.discovery_text.trim()}:{})})),
    exits:exitPositions.map(index=>({label:value.exits[index].label.trim(),description:value.exits[index].description.trim()}))};
  if(packageValue.npcs.some(n=>!n.name||!n.description)||packageValue.objects.some(o=>!o.name||!o.description)||packageValue.exits.some(e=>!e.label||!e.description))throw new Error('加入 NPC、物件或出口时，请同时填写名字和描述。');
  if(packageValue.objects.some(o=>!initialStates[o.kind]?.some(([state])=>state===o.initial_state)))throw new Error('物件的初始状态与交互规则不一致，请重新选择。');
  if(packageValue.objects.some(o=>o.kind==='inscription'&&!o.discovery_text))throw new Error('文字类物件必须先保存实际可读的正文，不能只留一段空白文字供模型事后编造。');
  if(adding&&(!packageValue.objects.length||packageValue.objects.length>2))throw new Error('请填写一至两件新物件，空白内容不会提交。');
  if(adding&&packageValue.objects.some(o=>!['cache','inscription'].includes(o.kind)))throw new Error('在当前地点追加内容只支持可携带物件或可读文字，不能新增障碍、钥物关系或封路。');
  if(packageValue.objects.some(o=>o.blocks_exit!==undefined&&(o.blocks_exit<0||o.blocks_exit>=packageValue.exits.length)))throw new Error('这道障碍所挡住的新出口还未填写。请填写对应出口的名字和描述，或选择不挡住出口。');
  return adding?{objects:packageValue.objects}:packageValue;
}
function openCreation(args,mode='expand'){
  if(!$('create-dialog').showModal)throw new Error('此浏览器无法打开创作表单，请使用新版浏览器。');clearError('create-error');createContext={...args};createMode=mode;const adding=mode==='objects';
  $('create-eyebrow').textContent=adding?'为当前地点留下新的选择':'填写已有出口的另一端';$('create-title').textContent=adding?'创作新的互动内容':'创作一个接续地点';
  $('create-source').textContent=adding?'为“'+snapshot.scene.title+'”追加现在创作的一至两件新物件。新内容会记录作者；不会改写旧物件或已结算结果。':'从“'+snapshot.scene.title+'”的“'+snapshot.scene.exits.find(e=>e.id===args.exit_id)?.label+'”继续。提交即正式写入世界；不会自动移动人物。';
  for(const id of ['create-name','create-description','create-facts']){$(id).closest('label').hidden=adding;if(id!=='create-facts')$(id).required=!adding;}
  $('create-npcs').hidden=adding;$('create-exits').hidden=adding;$('create-extra-summary').hidden=adding;$('create-extra').open=adding;$('create-dialog').classList.toggle('objects-only',adding);
  $('create-objects-note').textContent=adding?'新内容可直接拿取或阅读，也可需要先检定。地面最多容纳八件未被携带的物件；实际容量提交时重新核对。':'普通物件可以直接拿取或阅读，也可以设为需要先检定；障碍仍须处理。还可以让第一件容器对应第二件障碍：拿着容器的人能直接解除障碍，让所有人继续走。是否加入这种通路，由你决定。';
  $('create-limit-note').textContent=adding?'你是这些新内容的作者。它们从这次提交起进入共同世界，不改变已有物件、旧发现正文、检定失败或出口；文字中的奖励与描写不等于已结算的状态。':'世界会自动添加一条回到原地点的路。新地点不能自行添加休息权限、改写已有状态或给人物凭空发物品。文字中的传闻与描写，不等于已结算的动作。';
  $('create-submit').textContent=adding?'核对并加入当前地点':'核对并把地点写入世界';
  let value={};const raw=client.draft(creationRoute());if(raw){try{value=JSON.parse(raw);}catch{throw new Error('这个地点的本地草稿格式损坏。已保留原草稿，请先保存身份，联系维护者恢复。');}}
  $('create-name').value=value.title||'';$('create-description').value=value.description||'';$('create-facts').value=value.facts||'';
  for(let i=0;i<2;i++)for(const field of ['name','description']){$('create-npc-'+field+'-'+i).value=value.npcs?.[i]?.[field]||'';$('create-object-'+field+'-'+i).value=value.objects?.[i]?.[field]||'';}
  for(let i=0;i<2;i++){const kind=$('create-object-kind-'+i),allowed=Object.entries(kinds).filter(([id])=>!adding||id!=='barrier');kind.replaceChildren(...allowed.map(([id,caption])=>{const option=text('option',caption);option.value=id;return option;}));kind.value=allowed.some(([id])=>id===value.objects?.[i]?.kind)?value.objects[i].kind:'cache';syncCreationObjectState(i,value.objects?.[i]?.initial_state||initialStates[kind.value][0][0]);$('create-object-block-'+i).value=value.objects?.[i]?.blocks_exit??'';$('create-object-discovery-'+i).value=value.objects?.[i]?.discovery_text||'';$('create-exit-name-'+i).value=value.exits?.[i]?.label||'';$('create-exit-description-'+i).value=value.exits?.[i]?.description||'';}
  $('create-object-use-key').checked=!adding&&(value.paired_barrier===true||value.objects?.[1]?.key_object===0);$('create-key-required').checked=!adding&&value.objects?.[1]?.key_required===true;$('create-consume-key').checked=!adding&&value.objects?.[1]?.consume_key===true;syncCreationPairing();
  $('create-dialog').showModal();controls();
}
function closeCreation(){try{saveCreation();}catch(error){showError(error);return;}const uncertain=busy&&!!client.pending();if(uncertain)client.cancelRequests();$('create-dialog').close();createContext=null;announce(uncertain?'创作页已关闭，原提交仍需核对。草稿和操作已保留，请不要另发一份。':createMode==='objects'?'新物件草稿已保留，没有提交内容。':'创作草稿已保留，没有提交地点。');}
function createFields(){
  for(let i=0;i<2;i++){
    for(const type of ['npc','object','exit']){const group=text('div','','create-group');group.append(text('h3',({npc:'NPC',object:'物件',exit:'继续探索的出口'}[type])+' '+(i+1)+'（可留空）'));const input=document.createElement('input');input.id='create-'+type+'-name-'+i;input.maxLength=60;const description=document.createElement('textarea');description.id='create-'+type+'-description-'+i;description.rows=2;description.maxLength=type==='exit'?600:800;group.append(label(type==='exit'?'出口名称':'名字',input),label('描述',description));if(type==='object'){const kind=select(Object.entries(kinds));kind.id='create-object-kind-'+i;const block=select([['','不挡住出口'],['0','挡住下面第一个新出口'],['1','挡住下面第二个新出口']]);block.id='create-object-block-'+i;const discovery=document.createElement('textarea');discovery.id='create-object-discovery-'+i;discovery.rows=3;discovery.maxLength=1200;const initialState=select(initialStates.cache);initialState.id='create-object-state-'+i;group.append(label('交互规则',kind),label('这件物品初次出现时',initialState));if(i===1){const pairing=text('section','','object-pairing');pairing.id='create-object-pairing';const choice=text('label','','create-pair-choice'),checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.id='create-object-use-key';choice.append(checkbox,text('span','用第一件容器解除这道障碍（可选）'));pairing.append(choice,text('p','第二件障碍将与第一件容器配对。默认可保留物件、也可用检定尝试；下方两项可分别改变这些规则。关联出口开放后，所有旅人都能走过去。','small'));const options=text('div');options.id='create-key-options';for(const [id,caption]of [['create-key-required','必须实际使用对应物件，不允许检定或自由方法绕过'],['create-consume-key','解除时永久耗尽对应物件，从背包移除且不能再收藏或交接']]){const row=text('label','','create-pair-choice'),flag=document.createElement('input');flag.type='checkbox';flag.id=id;row.append(flag,text('span',caption));options.append(row);}pairing.append(options);group.append(pairing);}const discoveryLabel=label('成功后揭示的正文（文字类必填，其他可选）',discovery);discoveryLabel.id='create-object-discovery-label-'+i;const discoveryNote=text('p','','small');discoveryNote.id='create-object-discovery-note-'+i;group.append(label('这道障碍挡住哪条路（可选）',block),discoveryLabel,discoveryNote);}$('create-'+({npc:'npcs',object:'objects',exit:'exits'}[type])).append(group);}
  }
}
createFields();
syncCreationPairing();
for(const[id,caption,detail]of classes){const item=text('label','','class-option'),radio=document.createElement('input');radio.type='radio';radio.name='class_id';radio.value=id;radio.checked=id==='fighter';const copy=text('span');copy.append(text('strong',caption),text('small',detail));item.append(radio,copy);$('class-options').append(item);}
for(const[id,caption]of chapters){const option=text('option',caption);option.value=id;$('guide-chapter').append(option);}
function syncAgentPrompt(){
  $('agent-prompt').value=$('agent-access').value==='observe'
    ? '我只想观察渡鸦溪。这份连接只有只读权限。先 world.bootstrap 读取当前世界和本身份是否已有角色，再读 frontier.handbook 的 entry；已入场时用 frontier.look 看现场、人物和未决事项，按需分页读取 frontier.history、frontier.map 或 frontier.requests。若还未入场，请告诉我需要先在网页创建人物，不调用 join 或其他写入动作。不生成地点、不发言、不领题、不回答、不检定，也不代替任何人行动。按实际已保存的记录解释发生了什么，区分作者描写与真正状态变化；没有结果就说明没有结果。我的观察问题与本轮调用预算是：'
    : '我想来渡鸦溪玩一小段。先 world.bootstrap 看当前世界和本身份是否已入场，再读 frontier.handbook 的 entry；仅未入场时用 frontier.join 按我选择的名字、职业创建人物。接着重新 world.bootstrap 保存通知锚点、frontier.look 读取最新现场与未决事项；其他说明需要时再读。下面是我想做的事和这一轮的边界，按这个推进，新的风险先跟我商量。可以按世界共创规则延伸未知出口、写有作者署名的 NPC 对话；不替其他玩家作决定，不编结果或重抽失败骰子。结束时告诉我真做成了什么、停在哪、下次怎么继续。我的目标与本轮边界是：';
}
syncAgentPrompt();
$('agent-access').addEventListener('change',()=>{syncAgentPrompt();$('agent-status').textContent='已选择'+($('agent-access').value==='observe'?'只读观察':'读取与行动')+'。请下载对应的新配置；以前下载的文件权限不会随这个选择改变。';});
$('enter').addEventListener('click',()=>run(()=>enter(savedKey().then(value=>value||createKey()),$('name').value.trim(),document.querySelector('input[name=class_id]:checked').value)));
$('import-key').addEventListener('change',()=>run(async()=>{const file=$('import-key').files[0];if(!file)return;try{if(file.size>8192)throw new Error('恢复文件过大，请选择之前保存的身份文件。');let candidate;try{candidate=JSON.parse(await file.text());}catch{throw new Error('恢复文件不是有效 JSON，当前身份未被替换。');}await enter(candidate,$('name').value.trim(),document.querySelector('input[name=class_id]:checked').value);}finally{$('import-key').value='';}}));
for(const id of ['intent','say-text'])$(id).addEventListener('input',()=>{try{saveDrafts();}catch(error){showError(error);}});
$('intent-target').addEventListener('change',()=>{try{client.saveDraft('frontier-intent-target',$('intent-target').value);$('proposal').querySelector('button[type=submit]').dataset.unavailable=String(!$('intent-target').value);controls();}catch(error){showError(error);}});
$('proposal').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const value=$('intent').value.trim(),object=snapshot?.scene?.objects.find(item=>item.id===$('intent-target').value);if(!value)return;if(!object)throw new Error('这里没有可提出自由行动的物件。');saveDrafts();showResult(await mutate('frontier.request',{request_id:crypto.randomUUID(),target_id:object.id,text:value,expected_target_version:object.version},{route:'frontier-intent',draft_text:$('intent').value}));restoreDrafts();await refresh();});});
$('say-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const value=$('say-text').value.trim();if(!value)return;saveDrafts();showResult(await mutate('frontier.say',{text:value},{route:'frontier-say',draft_text:$('say-text').value}));restoreDrafts();await refresh();});});
function creationChanged(){try{syncCreationPairing();saveCreation();}catch(error){showError(error);}}
$('create-form').addEventListener('input',creationChanged);$('create-form').addEventListener('change',creationChanged);
$('create-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{
  if(!createContext)return;syncCreationPairing();saveCreation();
  const value=creationValue(),packageValue=creationPackage(value),session=generation,adding=createMode==='objects',functionId=adding?'frontier.add_objects':'frontier.expand',args=adding?{...createContext,objects:packageValue.objects}:{...createContext,package:packageValue},route=creationRoute(),draft=JSON.stringify(value);
  const title=adding?'为“'+snapshot.scene.title+'”创作新的互动内容':'让“'+packageValue.title+'”进入共同世界';
  const description=adding?'这会在当前地点正式追加 '+packageValue.objects.length+' 件由你现在创作的新内容。其他旅人可按所选规则尝试检定、阅读或拿取；作者和提交时间会保留；不会改写旧物件、重置失败检定或改变出口。':'这会正式创建地点、回程路和你填写的可交互内容。所有参与者都可以遇见它；不会自动移动你或修改别人的角色。';
  if(!await risk(title,description))return;if(session!==generation||!current())return;
  const result=await mutate(functionId,args,{route,draft_text:draft});if(session!==generation)return;
  $('create-dialog').close();createContext=null;showResult(result);await refresh();if(!adding)await readMap();
});});
$('create-close').addEventListener('click',closeCreation);$('create-cancel').addEventListener('click',closeCreation);$('create-dialog').addEventListener('cancel',event=>{event.preventDefault();closeCreation();});
$('refresh').addEventListener('click',()=>run(async()=>{await refresh();mapCursor=0;await readMap();}));$('reconnect').addEventListener('click',()=>run(()=>enter(key)));$('leave').addEventListener('click',leave);$('cancel').addEventListener('click',leave);$('backup').addEventListener('click',()=>download('ravenbrook-identity-recovery.json',key));
$('retry').addEventListener('click',()=>run(async()=>{const result=await client.retry();if(result)showResult(result);restoreDrafts();sceneRenderKey='';taskRenderKey='';await refresh();await readMap();schedule();}));$('restore').addEventListener('click',()=>run(async()=>{await client.restoreRejected();restoreDrafts();if(current()&&snapshot)await refresh();else{leave();announce('被拒绝的入场已解除。身份已保留，可以重新进入。');}}));
$('visit').addEventListener('click',()=>run(async()=>{const result=await mutate('frontier.visit',{});announce(result.additional_task?'已领取一道附加题，先看原方法和世界事实。':'目前没有可领取的他人行动。等待没有自动超时或改派。');await refresh();}));$('visit').dataset.write='true';
for(const id of ['proposal','say-form','create-form'])$(id).querySelector('button[type=submit]').dataset.write='true';
$('map-more').addEventListener('click',()=>run(readMap));$('history-more').addEventListener('click',()=>run(readOlderHistory));$('history-refresh').addEventListener('click',()=>run(async()=>{const session=generation;await refresh();if(session!==generation||!current())return;await readLatestHistory();}));$('request-more').addEventListener('click',()=>run(readRequests));$('request-box').addEventListener('change',()=>run(async()=>{renderRequestHistory();await readRequests();}));$('guide-load').addEventListener('click',()=>run(readGuide));$('credentials-refresh').addEventListener('click',()=>run(readCredentials));
$('return-more').addEventListener('click',()=>run(readReturnHistory));
for(const id of ['scene-activity-details','return-details'])$(id).addEventListener('toggle',()=>{if(snapshot)renderActivity();});
$('connect-agent').addEventListener('click',()=>run(async()=>{const session=generation,accessMode=$('agent-access').value,agent=new FrontierClient();agentClient=agent;try{await agent.authenticate(key,{accessMode,isCurrent:()=>session===generation&&current()});if(session!==generation)return;download('ravenbrook-opencode.json',{$schema:'https://opencode.ai/config.json',mcp:{agent_world:{type:'remote',url:location.origin+'/mcp',oauth:false,headers:{Authorization:'Bearer '+agent.token}}}});$('agent-status').textContent=(accessMode==='observe'?'只读观察':'读取与行动')+'连接已签发，到期 '+time(agent.expiresAt)+'。请按上方启动方式在自己的 OpenCode 中加载，再复制与该权限对应的第一句话；下载没有启动 Agent。';await readCredentials();}finally{if(agentClient===agent)agentClient=null;}}));
$('copy-prompt').addEventListener('click',()=>run(()=>copyPrompt($('agent-prompt'))));
$('copy-continuation').addEventListener('click',()=>run(()=>copyPrompt($('agent-continuation'))));
window.addEventListener('hashchange',()=>showPage().catch(showError));window.addEventListener('pagehide',()=>{try{saveDrafts();}catch{}client.cancelRequests();agentClient?.cancelRequests();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&current()&&!busy)refresh().catch(showError);});
showPage().catch(showError);controls();readEntryIdentity();
