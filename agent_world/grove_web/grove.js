import {createKey,validateKey,savedKey} from '/static/commons-client.js';
import {EventLedger} from '/static/stream-client.js';
import {GroveClient} from './grove-client.js';

const $=id=>document.getElementById(id);
const client=new GroveClient();
const ledger=new EventLedger(100);
const canvas=$('world-canvas'),ctx=canvas.getContext('2d');
const directions={w:[0,-1],a:[-1,0],s:[0,1],d:[1,0],ArrowUp:[0,-1],ArrowLeft:[-1,0],ArrowDown:[0,1],ArrowRight:[1,0]};
const actionNames={pickup:'拿进背包',drop:'放到脚下',open:'打开',close:'合上',store:'存入',retrieve:'取出',fill:'装水',pour:'倒水',ignite:'点燃',harvest:'采集一份',tend:'用水照料',fuel:'添入木料',dismantle:'拆回材料',switch_mode:'切换模式'};
const materialNames={wood:'木',stone:'石',cloth:'布',metal:'金属',water:'水',leather:'皮革'};
const hostNames={disconnected:'已断开',connecting:'正在连接',ready:'已连接',thinking:'思考中',paused:'已暂停',error:'连接受阻'};
const materialActions=new Set(['pickup','drop','store','retrieve','fill','pour','ignite','harvest','tend','fuel','dismantle','switch_mode']);
let key=null,identity=null,world=null,host=null,session=0,readEpoch=0,worldTimer=null,hostTimer=null;
let entering=false,playing=false,actionBusy=false,refreshFlight=null,streamCursor=null,lastWorldRead=0;
let selectedId=null,selectedTile=null,inspection=null,inspectEpoch=0,selectedFromBag=false,held=null,walkPath=[],pathTarget=null,walkTimer=null;
let tileSize=40,canvasWidth=600,canvasHeight=600,camera={x:0,y:0},motion=null,hoverTile=null,lastChatSignature='',lastHostSignature='',lastSelectionSignature='',lastInventorySignature='',lastAction='';
let importedKey=null;
let hostReadEpoch=0,lastGenerationSignature='',hostCommandBusy=false,autoFormDirty=false;
let lastObservedSeq=0,lastTriggerSeq=0,triggerFlight=false,queuedTriggerSeq=0,explorationQueued=false;
const discoveredChunks=new Set();
const chosenBuildMaterials=new Map();
const touchedBuildChoices=new Set();
let activeRainChunks=new Set();

function node(tag,text,className){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;}
function plainMessage(text){return String(text??'').replace(/\*\*([^*\n]+)\*\*/g,'$1');}
function messageNode(text){
  const el=node('p'),value=String(text??'');let offset=0;
  for(const match of value.matchAll(/\*\*([^*\n]+)\*\*/g)){el.append(document.createTextNode(value.slice(offset,match.index)),node('strong',match[1]));offset=match.index+match[0].length;}
  el.append(document.createTextNode(value.slice(offset)));return el;
}
function button(text,handler,{disabled=false,secondary=false}={}){const el=node('button',text,secondary?'secondary':'');el.type='button';el.disabled=disabled;el.addEventListener('click',()=>handler());return el;}
function announce(text,error=false){const el=$(error?'error':'notice');el.textContent=text;el.hidden=!text;if(!error&&text)setTimeout(()=>{if(el.textContent===text)el.hidden=true;},6500);}
function fail(error){if(error.cancelled)return;announce(error.message||String(error),true);if(error.status===401||error.status===403){client.token=null;held=null;walkPath=[];$('reconnect-world').hidden=false;$('world-status').textContent='网页需要重连';$('movement-feedback').textContent='授权失效：同钥重连后继续。';}renderPending();}
function run(fn){return Promise.resolve().then(fn).catch(fail);}
const current=()=>identity&&client.token;
const point=()=>world?.player;
const distance=(entity)=>entity?.container||entity?.owner===client.roleId?0:Math.abs((entity?.x??Infinity)-(point()?.x??0))+Math.abs((entity?.y??Infinity)-(point()?.y??0));
const inventory=()=>world?.inventory||[];
const ground=()=>world?.entities||[];
const entityById=id=>ground().find(e=>e.id===id)||inventory().find(e=>e.id===id)||inspection?.contents?.find(e=>e.id===id);
const caps=e=>e?.capabilities||[];
const finite=(value,fallback=0)=>Number.isFinite(value)?value:fallback;
const recipes=()=>world?.construction?.recipes||[];
const materialItems=material=>inventory().filter(e=>e.construction_material?.eligible===true&&e.construction_material.material===material);
const nearby=(x,y)=>!!world?.bounds&&Number.isFinite(x)&&Number.isFinite(y)&&x>=world.bounds.min_x&&x<=world.bounds.max_x&&y>=world.bounds.min_y&&y<=world.bounds.max_y;
function eventNearby(event){const p=event.payload||{};return nearby(p.x,p.y)||(p.affected||[]).some(a=>nearby(a.x,a.y))||(p.centers||[]).some(a=>nearby(a.x,a.y));}
function openContext(){document.body.classList.remove('agent-open');document.body.classList.add('context-open');$('agent-toggle').setAttribute('aria-expanded','false');}

async function readIdentity(){
  try{key=await savedKey();if(key){key=await validateKey(key);$('identity-status').textContent='本机身份已保存。进入后接续原来的位置与背包。';}else $('identity-status').textContent='首次进入会在本机保存一把身份密钥。';}
  catch(error){$('identity-status').textContent=error.message;}
  $('enter').disabled=false;$('enter').textContent='连接我的 Agent，进入世界';
  try{const prefs=JSON.parse(localStorage.getItem('shared-grove-preferences')||'{}');$('entry-name').value=prefs.name||'';$('entry-model').value=prefs.model||'';$('entry-host').value=prefs.host||'';}catch{}
}
function prefs(){return {name:$('entry-name').value.trim()||point()?.name||'旅人',model:$('entry-model').value.trim(),host:$('entry-host').value.trim()};}
function savePrefs(){try{localStorage.setItem('shared-grove-preferences',JSON.stringify(prefs()));}catch{}}
async function authenticate(candidate){
  const epoch=session,nextKey=await validateKey(candidate);
  if(epoch!==session){const error=new Error('连接已经改变。');error.cancelled=true;throw error;}
  const response=await client.authenticate(nextKey,{isCurrent:()=>epoch===session&&(entering||playing)});
  if(epoch!==session)return;
  key=nextKey;identity=response.role_profile;await savedKey(nextKey);
}
async function enter(){
  if(entering)return;
  ++session;autoFormDirty=false;entering=true;const epoch=session;
  announce('');$('error').hidden=true;$('enter').disabled=true;$('entry-cancel').hidden=false;$('entry-progress').replaceChildren(node('p','正在用密钥连接世界…'));
  try{
    await authenticate(importedKey||key||await createKey());if(epoch!==session)return;savePrefs();
    const value=prefs();
    // Recovery does not fabricate a new completion; the connector retains the real gate.
    host=await client.connector('recover',{token:client.token,public_key:key.public_key,name:value.name});if(epoch!==session)return;
    if(host?.entered&&host?.world_read&&host?.first_response_success){await showGame(epoch);return;}
    if(host.status==='disconnected'){
      host=await client.connector('start',{token:client.token,public_key:key.public_key,name:value.name,...(value.model?{model:value.model}:{})});
      if(epoch!==session)return;
    }
    renderHost();scheduleHost(350);
  }catch(error){if(epoch===session){entering=false;$('enter').disabled=false;$('entry-cancel').hidden=true;fail(error);}}
}
async function showGame(epoch=session){
  if(epoch!==session||!current())return;
  playing=true;entering=false;document.body.classList.add('playing');$('entry').hidden=true;$('game').hidden=false;$('entry-cancel').hidden=true;$('enter').disabled=false;
  $('game-host').value=host?.host_url||$('entry-host').value;$('game-model').value=host?.model||$('entry-model').value;
  $('world-status').textContent='共同世界 · 已进入';resize();renderHost();
  if(client.pending())renderPending();
  await refreshWorld(true);scheduleWorld();scheduleHost();
  try{$('chat-text').value=client.draft('grove-chat');$('generation-text').value=client.draft('grove-generation');}catch{}
}
async function cancelEntry(){
  const epoch=++session;entering=false;$('entry-cancel').hidden=true;$('enter').disabled=false;
  if(current())await client.connector('cancel').catch(fail);
  if(epoch!==session)return;$('entry-progress').replaceChildren(node('p','连接已中止。身份保留，可继续连接。'));clearTimeout(hostTimer);
}
async function pollHost(){
  if(!current()||(!entering&&!playing))return;const epoch=session,reading=++hostReadEpoch;
  try{const value=await client.connector('state');if(epoch!==session||reading!==hostReadEpoch)return;host=value;renderHost();
    if(entering&&host.entered&&host.world_read&&host.first_response_success)await showGame(epoch);
    if(entering&&['error','disconnected','paused'].includes(host.status)){entering=false;$('enter').disabled=false;$('entry-cancel').hidden=true;}
  }catch(error){if(epoch===session){$('agent-status').textContent='宿主暂不可达';$('agent-detail').textContent='你的世界操作仍可继续。恢复宿主后再提问或共创。';if(entering){entering=false;$('enter').disabled=false;fail(error);}}}
  finally{scheduleHost();}
}
function scheduleHost(delay=1200){clearTimeout(hostTimer);if(current()&&(entering||playing)&&host?.status!=='disconnected')hostTimer=setTimeout(pollHost,delay);}
function renderHost(){
  if(!host)return;const busy=hostCommandBusy||['connecting','thinking'].includes(host.status);
  renderModelChoices();$('game-host').value=host.host_url||'';
  $('agent-status').textContent=hostNames[host.status]||host.status||'状态待核对';$('agent-status').dataset.status=host.status;
  $('agent-detail').textContent=host.error?.message||(busy?'Agent 正在读取、解释或共创。你可以继续亲手操作世界。':host.entered?'已完成首次真实回应与世界读取。提问只交给你自己的 Agent。':'完成首次真实回应与现场读取后，即可进入。');
  $('chat-send').disabled=busy||!['ready','paused'].includes(host.status);$('agent-cancel').hidden=!busy;$('agent-reconnect').hidden=!['disconnected','error','paused'].includes(host.status);$('agent-disconnect').disabled=host.status==='disconnected';
  const generationPending=['unknown','published_readback_pending'].includes(host.generation?.status);
  $('generation-send').disabled=busy||generationPending||!['ready','paused'].includes(host.status);$('generation-recover').hidden=!generationPending;$('generation-recover').disabled=busy;renderGeneration();
  $('generation-cancel').hidden=!busy||!['claiming','generating','correcting','publishing'].includes(host.generation?.status);
  const messages=host.messages||[];
  const lastReply=[...messages].reverse().find(m=>!['user','event'].includes(m.role)&&m.text);
  $('companion-update').hidden=!busy&&!lastReply;
  $('companion-update').textContent=busy?'伙伴正在看现场。你可以继续走动与操作。':lastReply?'Agent 解释：'+plainMessage(lastReply.text):'';
  const signature=JSON.stringify(messages);
  if(signature!==lastChatSignature){lastChatSignature=signature;$('agent-messages').replaceChildren(...messages.slice(-12).map(m=>{const el=node('div',undefined,'chat-message '+(m.role==='user'?'user':m.role==='event'?'event':'agent'));el.append(node('small',m.role==='user'?'你':m.role==='event'?'现场变化':'你的 Agent'),messageNode(m.text));return el;}));$('agent-messages').scrollTop=$('agent-messages').scrollHeight;}
  if(!messages.length&&host.text)$('agent-messages').replaceChildren(messageNode(host.text));
  const auto=host.automatic||{};if(!autoFormDirty){$('auto-enabled').checked=!!auto.enabled;if(auto.max_count)$('auto-budget').value=auto.max_count;if(auto.interval_seconds)$('auto-interval').value=auto.interval_seconds;}
  const remaining=finite(auto.remaining,Math.max(0,finite(auto.max_count,4)-finite(auto.count)));
  $('auto-status').textContent=(auto.enabled?'自动参与已开启':'自动参与已关闭')+' · 已用 '+finite(auto.count)+' / '+finite(auto.max_count,4)+' 次模型请求，失败与纠正也计入。实际变化才唤醒，忙时合并，用完停止。';
  $('auto-summary').textContent=auto.enabled?'Agent 随实际探索参与 · 剩余 '+remaining+' / '+finite(auto.max_count,4)+' 次请求':remaining===0?'Agent 自动额度已用完 · 亲手操作可继续':'Agent 自动参与已停止 · 剩余 '+remaining+' 次请求';
  $('auto-stop').disabled=!auto.enabled||hostCommandBusy;
  const progress=(host.progress||[]).slice(-6);const entrySignature=JSON.stringify([host.status,progress,host.error]);
  if(entrySignature!==lastHostSignature){lastHostSignature=entrySignature;$('entry-progress').replaceChildren(...progress.map(p=>node('p',p.message||p.kind||'')));if(host.error)$('entry-progress').append(node('p',host.error.message,'error-text'));}
}
async function hostCommand(path,body,{refresh=false}={}){
  if(hostCommandBusy||!playing||!current())return false;
  const epoch=session;++hostReadEpoch;hostCommandBusy=true;renderHost();
  try{const value=await client.connector(path,body);if(epoch!==session||!playing)return false;host=value;if(path==='automatic')autoFormDirty=false;renderHost();if(refresh)await refreshWorld(true);return epoch===session&&playing;}
  finally{if(epoch===session){hostCommandBusy=false;renderHost();scheduleHost(250);}}
}
async function restartHost(){
  return hostCommand('start',{token:client.token,public_key:key.public_key,name:point()?.name||prefs().name,...($('game-model').value.trim()?{model:$('game-model').value.trim()}:{})});
}
function renderModelChoices(){
  for(const id of ['entry-model','game-model']){
    const select=$(id),selected=select.value,models=host.available_models||[];
    for(const model of models)if(![...select.options].some(o=>o.value===model)){const option=node('option',model.split('/').slice(1).join('/'));option.value=model;select.append(option);}
    if(selected)select.value=selected;
  }
}
function renderGeneration(){
  const generation=host?.generation,signature=JSON.stringify([generation||null,(world?.processes||[]).map(p=>[p.id,p.status,p.start_tick<=finite(world?.clock?.sequence)])]);if(signature===lastGenerationSignature)return;lastGenerationSignature=signature;
  const status=generation?.status||'idle';$('generation-status').dataset.status=status;$('generation-result').replaceChildren();
  if(!generation){$('generation-status').textContent='尚未开始';$('generation-detail').textContent='使用你自己的模型连接；完成后直接公开。';return;}
  if(!generation.published){
    const phases={idle:['尚未开始','使用你自己的模型连接；完成后直接公开。'],claiming:['读取附近','正在读取这片地方能创作的机会。'],generating:['正在创作','你的 Agent 正在创作；你仍可以亲手走动与操作。'],correcting:['正在调整','正在按世界规则调整内容；你仍可以亲手操作。'],publishing:['正在发表','正在交给共同世界保存，等待实际结果。'],unavailable:['暂时没有机会','附近暂时没有可用的创作机会。继续走动、探索，之后再试。'],error:['创作受阻',generation.error?.message||host.error?.message||'这一轮没有确认发表。可以调整想法后再试。'],unknown:['结果待核对','发表回应中断，原操作保留。请先核对发表结果，避免重复发表。'],cancelled:['已中止','这一轮没有发表内容，已中止。可以继续亲手操作世界。']};
    const phase=phases[status]||['状态待核对','请重新连接，读取这轮创作的实际状态。'];$('generation-status').textContent=phase[0];$('generation-detail').textContent=phase[1];return;
  }
  const confirmed=generation.readback?.receipt_confirmed&&generation.readback?.viewport_read;
  $('generation-status').textContent=confirmed?'已发表并核对':'已发表 · 待读回';$('generation-detail').textContent=confirmed?'已从共同世界读取发表结果。这里保留发表记录；当前内容物、数量与状态以地图物件详情为准。':'世界已确认发表，现场读回暂未完成。请核对原发表结果，已保存的内容不会重复发表。';
  const entities=Array.isArray(generation.entities)?generation.entities:[];
  for(const entity of entities){if(typeof entity!=='object')continue;const location=Number.isFinite(entity.x)&&Number.isFinite(entity.y)?' · '+entity.x+'，'+entity.y:'';$('generation-result').append(node('p',(entity.name||'共同物件')+location+' · 发表时状态：'+stateLine(entity)));if(entity.authored_description)$('generation-result').append(node('p','发表时的说明：'+entity.authored_description,'small'));}
  const processes=Array.isArray(generation.process)?generation.process:generation.process?[generation.process]:[];
  for(const process of processes){if(typeof process!=='object')continue;const label=process.kind==='rain'?'短雨':process.kind==='drift'?'漂来容器':'世界过程';const currentProcess=world?.processes?.find(p=>p.id===process.id);const active=currentProcess?.status==='active'&&finite(world?.clock?.sequence)>=finite(currentProcess.start_tick);const state=currentProcess?(active?'正在发生':'已安排，等待世界推进'):'已保存，当前变化见地图现场';$('generation-result').append(node('p',label+' · '+state));}
}
async function refreshWorld(reset=false){
  if(!playing||!current())return;
  if(refreshFlight&&!reset)return refreshFlight;
  const epoch=session,reading=++readEpoch;
  const task=(async()=>{
    try{
      const view=await client.viewport({reset});if(epoch!==session||reading!==readEpoch)return;
      const value=client.unpackView(view);if(value?.player)acceptWorld(value);lastWorldRead=Date.now();$('map-loading').hidden=true;$('reconnect-world').hidden=true;$('world-status').textContent='共同世界 · 已连接';
      const changes=await client.readChanges(streamCursor);if(epoch!==session||reading!==readEpoch)return;
      const historical=!streamCursor;streamCursor=changes.cursor;const added=ledger.ingest(changes.events,{historical});renderActivity();
      for(const event of changes.events||[])lastObservedSeq=Math.max(lastObservedSeq,finite(event.seq));
      const changesForAgent=added.filter(e=>!historical&&eventNearby(e)&&(e.kind==='build'||e.kind==='interact'&&materialActions.has(e.payload?.action)));
      if(host?.automatic?.enabled&&(changesForAgent.length||explorationQueued)){
        const seq=changesForAgent.length?Math.max(...changesForAgent.map(e=>finite(e.seq))):lastObservedSeq;
        explorationQueued=false;if(seq>0)triggerCompanion(seq);
      }
    }catch(error){if(epoch!==session||reading!==readEpoch)return;
      if(['StreamResetRequired','CursorExpired','CursorInvalid'].includes(error.code)){streamCursor=null;return;}
      $('world-status').textContent='现场连接中断';$('freshness').textContent='上次读取 '+(lastWorldRead?new Date(lastWorldRead).toLocaleTimeString():'尚未成功');if(error.status===401||error.status===403)fail(error);else $('movement-feedback').textContent='世界暂不可达；显示上次现场。刷新后继续。';
    }
  })();refreshFlight=task;try{return await task;}finally{if(refreshFlight===task)refreshFlight=null;}
}
function scheduleWorld(delay=1200){clearTimeout(worldTimer);if(playing&&current())worldTimer=setTimeout(async()=>{if(!actionBusy&&!document.hidden)await refreshWorld();scheduleWorld();},delay);}
function acceptWorld(value){
  const previous=world?.player;world=value;
  activeRainChunks=new Set((value.processes||[]).filter(p=>p.kind==='rain'&&p.status==='active'&&p.start_tick<=finite(value.clock?.sequence)).flatMap(p=>p.chunk_ids||[]));
  for(const chunk of value.chunks||[]){if(previous&&!discoveredChunks.has(chunk.id))explorationQueued=true;discoveredChunks.add(chunk.id);}
  if(previous&&(previous.x!==value.player.x||previous.y!==value.player.y))motion={from:{x:previous.x,y:previous.y},to:{x:value.player.x,y:value.player.y},start:performance.now(),duration:160};
  if(!previous){camera.x=value.player.x;camera.y=value.player.y;}
  $('player-name').textContent=value.player.name;$('position').textContent='坐标 '+value.player.x+'，'+value.player.y;
  $('freshness').textContent='刚刚读取';$('map-scale').textContent='只显示身边的区域'+(value.truncated?' · 更多物件需靠近查看':'');
  const rain=(value.processes||[]).find(p=>p.kind==='rain'&&p.status==='active');
  $('weather').textContent=rain?(rain.start_tick>finite(value.clock?.sequence)?'附近短雨将到来':'附近有雨 · 蓝色覆盖区以世界状态为准'):'附近无雨 · 湿料会继续干燥';
  if(selectedId){const selected=entityById(selectedId);if(!selected){selectedId=null;inspection=null;}else if(inspection?.entity?.version!==selected.version||previous&&(previous.x!==value.player.x||previous.y!==value.player.y)){inspection=null;const captured=selectedId;select(captured,selectedFromBag,true).catch(fail);}}
  renderInventory();renderSelection();renderActivity();renderPending();
}
function renderPending(){
  if(!identity)return;let pending;try{pending=client.pending();}catch{return;}
  $('pending').hidden=!pending;
  if(pending){$('pending-detail').textContent=pending.rejection?'服务器拒绝了原操作：'+pending.rejection.message:'回应中断，结果尚未核对。操作 '+pending.body.operation_id.slice(0,8)+' 保留；不会自动发出替代操作。';$('release-operation').hidden=!pending.rejection;$('recover-operation').hidden=!!pending.rejection;}
}
async function perform(name,args,label){
  if(!playing||!current()||!point()||actionBusy)return false;
  const epoch=session,isCurrent=()=>epoch===session&&playing&&current();
  if(client.pending()){announce('先核对上一个操作，再继续。');renderPending();return false;}
  actionBusy=true;++readEpoch;lastAction=label;$('error').hidden=true;$('movement-feedback').textContent=label+'，等待世界保存…';
  try{
    const prepared=typeof args==='function'?await args(isCurrent):args;
    if(!isCurrent())return false;
    const result=await client.mutate(name,prepared,{},isCurrent);if(!isCurrent())return false;
    if(result.viewport?.player){client.world.view=null;acceptWorld(result.viewport);}else await refreshWorld(true);
    if(!isCurrent())return false;
    if(result.changes?.length)announce(result.changes.map(c=>typeof c==='string'?c:c.summary||'').filter(Boolean).join(' · '));
    $('movement-feedback').textContent=result.changes?.map(c=>typeof c==='string'?c:c.summary||'').filter(Boolean).join(' · ')||label+'已由世界保存。';
    if(selectedId)await select(selectedId,selectedFromBag,true);
    if(!isCurrent())return false;
    scheduleWorld(200);return true;
  }catch(error){
    if(!isCurrent())return false;
    held=null;walkPath=[];pathTarget=null;
    if(client.pending()?.rejection){const rejection=await client.releaseRejected();if(!isCurrent())return false;await refreshWorld(true);if(!isCurrent())return false;announce((rejection.code==='StateConflict'?'物件或位置刚被改变，已读取最新现场。':'这次行动没有保存。')+' '+rejection.message+' 请按当前状态重新选择。',true);}
    else fail(error);
    $('movement-feedback').textContent=client.pending()?'原操作需要核对，走动已暂停。':'行动未保存；可以重新选择。';return false;
  }finally{if(epoch===session)actionBusy=false;if(isCurrent()){renderPending();renderSelection();}}
}
async function triggerCompanion(observedSeq){
  if(!playing||!current()||!host?.automatic?.enabled||observedSeq<=lastTriggerSeq)return;
  queuedTriggerSeq=Math.max(queuedTriggerSeq,observedSeq);if(triggerFlight)return;
  const epoch=session;triggerFlight=true;
  try{while(epoch===session&&playing&&host?.automatic?.enabled&&queuedTriggerSeq>lastTriggerSeq){
    const seq=queuedTriggerSeq;queuedTriggerSeq=0;const reading=++hostReadEpoch;
    const value=await client.connector('trigger',{observed_seq:seq});if(epoch!==session)return;lastTriggerSeq=seq;
    if(reading===hostReadEpoch){host=value;renderHost();}scheduleHost(250);
  }}catch{if(epoch===session)$('agent-detail').textContent='自动参与暂未开始；你的行动已保存，可以继续操作。';}
  finally{if(epoch===session)triggerFlight=false;}
}
async function move(dx,dy){return perform('grove.move',{dx,dy,expected_player_version:point().version},'走动');}
function stopWalking(){held=null;walkPath=[];pathTarget=null;clearTimeout(walkTimer);}
async function walk(){
  clearTimeout(walkTimer);if(!playing||!current()||!point()||actionBusy)return;
  let step;if(held)step=directions[held];else if(walkPath.length){const next=walkPath.shift();step=[next.x-point().x,next.y-point().y];}
  if(!step)return;
  if(Math.abs(step[0])+Math.abs(step[1])!==1){walkPath=[];return;}
  const success=await move(...step);if(!success)return;
  if(held||walkPath.length)walkTimer=setTimeout(()=>run(walk),90);else if(pathTarget){$('movement-feedback').textContent='已沿路到达。';pathTarget=null;}
}
function terrain(x,y){
  const cx=Math.floor(x/16),cy=Math.floor(y/16),chunk=(world?.chunks||[]).find(c=>c.cx===cx&&c.cy===cy);
  if(!chunk)return null;return chunk.tiles[(y-cy*16)*16+(x-cx*16)];
}
function walkable(x,y){
  const b=world?.bounds;if(!b||x<b.min_x||y<b.min_y||x>b.max_x||y>b.max_y)return false;
  const t=terrain(x,y);if(!t||t==='rock')return false;
  return !ground().some(e=>e.x===x&&e.y===y&&caps(e).includes('blocking')&&!e.state?.destroyed);
}
function routeTo(x,y,adjacent=false){
  const origin=point();if(!origin)return [];
  const keyOf=(x,y)=>x+','+y,start=keyOf(origin.x,origin.y),seen=new Set([start]),queue=[{x:origin.x,y:origin.y,path:[]}];
  for(let i=0;i<queue.length&&i<350;i++){const now=queue[i],distance=Math.abs(now.x-x)+Math.abs(now.y-y);if(distance===(adjacent?1:0))return now.path;
    if(now.path.length>=30)continue;for(const [dx,dy] of [[0,-1],[1,0],[0,1],[-1,0]]){const nx=now.x+dx,ny=now.y+dy,key=keyOf(nx,ny);if(seen.has(key)||!walkable(nx,ny))continue;seen.add(key);queue.push({x:nx,y:ny,path:[...now.path,{x:nx,y:ny}]});}
  }return null;
}
function goTo(x,y,adjacent=false){
  if(actionBusy||client.pending()){announce('先等当前行动完成，或核对原操作。');return;}
  stopWalking();const path=routeTo(x,y,adjacent);if(!path){announce('附近没有能走通的路线。可以绕开障碍，或走近后再选择。');return;}
  walkPath=path;pathTarget={x,y};if(!path.length){$('movement-feedback').textContent='已经在这里。';return;}$('movement-feedback').textContent='按四方向沿路走动；按键可改向，点击可重选路线。';run(walk);
}
async function select(id,fromBag=false,force=false){
  if(!current()||!world)return;selectedId=id;selectedTile=null;selectedFromBag=fromBag;if(fromBag)document.querySelector('.inventory-card').open=false;openContext();const epoch=++inspectEpoch,sessionEpoch=session;
  if(!force&&inspection?.entity?.id===id){renderSelection();return;}
  inspection=null;renderSelection();
  try{const result=await client.query('grove.inspect',{entity_id:id});if(epoch!==inspectEpoch||sessionEpoch!==session||selectedId!==id)return;inspection=result;renderSelection();}
  catch(error){if(epoch===inspectEpoch&&sessionEpoch===session){$('selection-note').textContent=error.message||'这件物件暂不可查看。';}}
}
function renderInventory(){
  const bag=inventory().find(e=>e.id===point()?.backpack_id),items=inventory().filter(e=>e.id!==point()?.backpack_id);
  const signature=JSON.stringify([selectedId,inventory()]);if(signature===lastInventorySignature)return;lastInventorySignature=signature;
  const used=bag?.state?.contents?.length??items.length;
  $('inventory-capacity').textContent=used+' / '+(bag?.state?.capacity??'?');
  $('supply-capacity').textContent=used+' / '+(bag?.state?.capacity??'?');
  for(const material of ['wood','stone','cloth']){const heldItems=items.filter(e=>e.material===material&&caps(e).includes('transport')&&!caps(e).includes('container')&&!caps(e).includes('ignition')&&!caps(e).includes('personal')&&!e.state?.destroyed);$('material-'+material).textContent='可用 '+materialItems(material).length+'\n持有 '+heldItems.length;$('material-'+material).parentElement.title='可用：现在能投料；持有：背包原料总件数';}
  const wetBlocked=items.filter(e=>!e.construction_material?.eligible&&e.construction_material?.reason?.includes('湿')).length;
  $('material-caption').textContent=wetBlocked?'有 '+wetBlocked+' 件木料或布料太湿，暂不能投料。可先晾干，或放到炉旁烘干。':'可用：现在能投料的件数。持有：背包里的原料件数，包含暂不能用的湿料。';
  $('material-caption').dataset.blocked=String(wetBlocked>0);
  const vessels=items.filter(e=>e.state?.water_capacity>0),water=vessels.reduce((total,e)=>total+finite(e.state.water),0),waterCapacity=vessels.reduce((total,e)=>total+finite(e.state.water_capacity),0);
  $('supply-water').textContent=water+' / '+waterCapacity;
  $('supply-tools').textContent='点火 '+items.filter(e=>caps(e).includes('ignition')).reduce((total,e)=>total+finite(e.state?.uses),0)+' 次';
  $('inventory-list').replaceChildren(...items.map(e=>{const el=button('',()=>run(()=>select(e.id,true)),{secondary:true});el.className='inventory-item'+(e.id===selectedId?' selected':'');const icon=node('span',entityGlyph(e),'item-icon '+(e.material||''));el.append(icon,node('span',e.name),node('small',stateLine(e)));return el;}));
  $('inventory-water').textContent=items.filter(e=>e.state?.water_capacity>0).map(e=>e.name+'：'+finite(e.state.water)+' / '+e.state.water_capacity+' 水').join(' · ')||'装水需要一件可盛水的容器。';
}
function stat(label,value){const el=node('div');el.append(node('small',label),node('strong',String(value)));return el;}
function stateLine(e){const s=e?.state||{};return [materialNames[e.material]||e.material,s.destroyed?'已毁坏':s.burning?'正在燃烧':null,s.wetness>0?'湿度 '+s.wetness:null,s.water_capacity>0?'水 '+finite(s.water)+'/'+s.water_capacity:null].filter(Boolean).join(' · ');}
function entityGlyph(e){if(e.state?.recipe==='walkway')return '═';if(e.state?.recipe==='canopy')return '⌂';if(e.state?.recipe==='hearth')return '♨';if(e.state?.kind==='resource_node')return '♧';if(caps(e).includes('water_source'))return '≈';if(caps(e).includes('ignition'))return '✧';if(caps(e).includes('container'))return '▣';return e.material==='wood'?'▰':e.material==='stone'?'⬟':e.material==='cloth'?'▱':'◆';}
function selectTile(tile){
  stopWalking();++inspectEpoch;selectedId=null;selectedFromBag=false;inspection=null;
  if(selectedTile?.x!==tile.x||selectedTile?.y!==tile.y){chosenBuildMaterials.clear();touchedBuildChoices.clear();}
  selectedTile={x:tile.x,y:tile.y};lastSelectionSignature='';openContext();renderSelection();renderInventory();
}
function renderTileSelection(){
  const tile=selectedTile,t=terrain(tile.x,tile.y),d=Math.abs(tile.x-point().x)+Math.abs(tile.y-point().y);
  const occupied=ground().filter(e=>e.x===tile.x&&e.y===tile.y&&!e.state?.destroyed);
  $('selection-name').textContent='选择位置 · '+tile.x+'，'+tile.y;$('selection-distance').textContent=d===0?'脚下':d+' 格';
  $('selection-description').textContent={grass:'草地，可以在规则允许的位置留下设施。',dirt:'泥地，可以在规则允许的位置留下设施。',water:'浅溪仍可涉水；随身可湿物件会受湿，包括合上容器里的物件。木板路可以免去这一格的涉水受湿。',rock:'岩壁不能通行，也不能搭建设施。'}[t]||'这块地方还没有读取。靠近后再选择。';
  $('selection-stats').replaceChildren(stat('地面',{grass:'草地',dirt:'泥地',water:'浅溪',rock:'岩壁'}[t]||'未读取'),stat('距离',d+' 格'));
  $('selection-history').replaceChildren();$('selection-content').replaceChildren();$('selection-effects').replaceChildren();$('selection-actions').replaceChildren();
  const disabled=actionBusy||!!client.pending();
  $('selection-actions').append(button(d?'沿路走到这里':'已经在这里',()=>goTo(tile.x,tile.y),{disabled:disabled||!d||!walkable(tile.x,tile.y),secondary:true}));
  for(const entity of occupied)$('selection-content').append(button('查看 '+entity.name,()=>run(()=>select(entity.id)),{secondary:true}));
  for(const recipe of recipes()){
    const section=node('section',undefined,'build-option'),cost=recipe.cost||{},costText=Object.entries(cost).map(([material,count])=>(materialNames[material]||material)+' '+count).join(' + ');
    section.append(node('h4',recipe.name||recipe.id),node('p',(recipe.use||'')+' · 投入 '+costText,'small'));
    const allowedTerrain=recipe.terrain||[],reason=!t?'请靠近，读取这块地面':allowedTerrain.length&&!allowedTerrain.includes(t)?'需要'+allowedTerrain.map(x=>({water:'水面',grass:'草地',dirt:'泥地'}[x]||x)).join('或'):d>1?'先走到相邻一格':occupied.some(e=>caps(e).includes('blocking'))?'这里有障碍，需要先移开':occupied.some(e=>e.state?.recipe||e.state?.kind==='resource_node')?'这里已有设施或资源节点':'';
    let chosen=chosenBuildMaterials.get(recipe.id);
    const candidates=Object.keys(cost).flatMap(material=>materialItems(material));
    if(!chosen||!touchedBuildChoices.has(recipe.id)||chosen.some(id=>!candidates.some(e=>e.id===id))){chosen=Object.entries(cost).flatMap(([material,count])=>materialItems(material).slice(0,count).map(e=>e.id));chosenBuildMaterials.set(recipe.id,chosen);}
    const choice=node('details',undefined,'material-choice');choice.append(node('summary','选择投入的物件'));
    for(const item of candidates){const label=node('label',undefined,'material-pick'),input=node('input');input.type='checkbox';input.value=item.id;input.checked=chosen.includes(item.id);input.disabled=disabled;label.append(input,node('span',item.name+' · '+stateLine(item)));choice.append(label);}
    if(!candidates.length)choice.append(node('p','背包没有可投入的原料。可采集或取用公共材料。','small'));
    const status=node('p',undefined,'small'),submit=button('在这里搭建',()=>build(recipe.id,[...choice.querySelectorAll('input:checked')].map(e=>e.value)),{disabled:true});
    const update=()=>{const ids=[...choice.querySelectorAll('input:checked')].map(e=>e.value);chosenBuildMaterials.set(recipe.id,ids);const missing=Object.entries(cost).filter(([material,count])=>ids.filter(id=>candidates.find(e=>e.id===id)?.material===material).length!==count);submit.disabled=disabled||!!reason||!!missing.length;status.textContent=reason|| (missing.length?'需要准确投入 '+costText:'投入后成为公共设施；别人也可以使用或拆回材料。');};
    choice.addEventListener('change',()=>{touchedBuildChoices.add(recipe.id);update();});section.append(choice,status,submit);update();$('selection-actions').append(section);
  }
  if(!recipes().length)$('selection-actions').append(node('p','附近搭建方式暂未读取，请刷新现场。','small'));
  $('selection-note').textContent='材料数量按可投入的物件计算。盛水容器、点火工具和私人物件保留在背包。';
}
async function build(recipe,materialIds){
  if(!selectedTile)return;const target={...selectedTile},epoch=session;stopWalking();
  const saved=await perform('grove.build',isCurrent=>{if(!isCurrent())return;return {recipe,x:target.x,y:target.y,expected_player_version:point().version,material_ids:materialIds};},'搭建设施');
  if(saved&&epoch===session){const structure=ground().find(e=>e.x===target.x&&e.y===target.y&&e.state?.recipe===recipe&&!e.state?.destroyed);if(structure)await select(structure.id);}
}
function renderSelection(){
  const entity=inspection?.entity?.id===selectedId?inspection.entity:entityById(selectedId);
  const signature=JSON.stringify([selectedId,selectedTile,entity,inspection,entity?.state?.regrowth_tick!=null?world?.clock?.sequence:null,world?.construction,point()?.x,point()?.y,actionBusy,!!identity&&client.pending(),inventory().map(e=>[e.id,e.version])]);
  if(signature===lastSelectionSignature)return;
  const editing=document.activeElement?.closest('#selection-actions');
  if(editing&&lastSelectionSignature&&selectedId===editing.dataset.entityId&&!actionBusy)return;
  lastSelectionSignature=signature;$('selection-actions').dataset.entityId=selectedId||'';
  if(selectedTile){renderTileSelection();return;}
  if(!entity){$('selection-name').textContent='选择物件或位置';$('selection-description').textContent='浅溪可以直接涉过，但会弄湿随身木料和布；湿料要先干燥才能投料。通路免去涉水受湿，棚可以遮雨保护地面材料，也可以切成集雨换得存水。点物件或地面，看看你想保留什么、改变什么。';$('selection-distance').textContent='';$('selection-stats').replaceChildren();$('selection-actions').replaceChildren();$('selection-content').replaceChildren();$('selection-effects').replaceChildren();$('selection-history').replaceChildren();$('selection-note').textContent='';return;}
  const d=distance(entity),own=inventory().some(e=>e.id===entity.id),s=entity.state||{},near=d<=1;
  $('selection-name').textContent=entity.name;$('selection-description').textContent='当前状态：'+stateLine(entity);$('selection-distance').textContent=own?'随身':d===0?'脚下':d+' 格';
  $('selection-history').replaceChildren();if(entity.authored_description){const detail=node('details');detail.append(node('summary','发表时的说明'),node('p',entity.authored_description,'small'));$('selection-history').append(detail);}
  renderEffects(entity);
  const stats=[stat('材质',materialNames[entity.material]||entity.material||'—')];if(caps(entity).includes('wettable'))stats.push(stat('湿度',finite(s.wetness)));if(caps(entity).includes('flammable'))stats.push(stat('燃料',finite(s.fuel)));if(s.water_capacity>0)stats.push(stat('存水',finite(s.water)+' / '+s.water_capacity));if(caps(entity).includes('container'))stats.push(stat('容量',s.capacity));if(caps(entity).includes('flammable'))stats.push(stat('火',s.destroyed?'已毁坏':s.burning?'正在燃烧':'未燃烧'));if(caps(entity).includes('container'))stats.push(stat('容器',s.open?'敞开':'合上'));if(caps(entity).includes('water_source'))stats.push(stat('可取水',finite(s.water_remaining)));if(caps(entity).includes('ignition'))stats.push(stat('余下点火',finite(s.uses,20)));
  if(s.kind==='resource_node'){stats.push(stat('余下原料',finite(s.stock)+' / '+finite(s.stock_capacity)));if(s.regrowth_tick!==null&&s.regrowth_tick!==undefined)stats.push(stat('正在再生','约 '+Math.max(0,s.regrowth_tick-finite(world.clock?.sequence))+' 秒'));}
  if(s.recipe==='canopy')stats.push(stat('模式',s.mode==='collect'?'集雨':'遮雨'));
  if(s.recipe==='hearth')stats.push(stat('炉火',s.lit?'正在烘干':'未点燃'),stat('炉内燃料',finite(s.hearth_fuel)));
  $('selection-stats').replaceChildren(...stats);
  $('selection-actions').replaceChildren();$('selection-content').replaceChildren();
  $('selection-note').textContent=near?'行动按服务器当前状态结算；多人改变同一件物件时，会先读取新状态。':'走到相邻一格才能动手。选择“走近”沿路前往，不会穿过障碍。';
  if(!own&&d>1)$('selection-actions').append(button('走近这件物件',()=>goTo(entity.x,entity.y,true),{disabled:actionBusy}));
  if(!inspection){$('selection-actions').append(node('p','正在读取可用行动…','small'));return;}
  const available=(inspection.available_actions||[]).map(a=>typeof a==='string'?a:a.action).filter(Boolean);
  const disabled=actionBusy||!near||!!client.pending();
  if(!own&&Number.isFinite(entity.x)&&Number.isFinite(entity.y))$('selection-actions').append(button('选择这里的地面',()=>selectTile({x:entity.x,y:entity.y}),{secondary:true}));
  const direct=['pickup','drop','open','close','harvest','dismantle'];
  for(const action of direct.filter(a=>available.includes(a)))$('selection-actions').append(button(actionNames[action],()=>interact(action,entity),{disabled,secondary:action==='close'}));
  const contents=inspection.contents||[];
  if(caps(entity).includes('container')&&s.open){const section=node('div',undefined,'contents-list');section.append(node('h4','当前里面的物件 · '+contents.length+' 件'));if(!contents.length)section.append(node('p','容器现在是空的。','small'));for(const item of contents){const row=node('div',undefined,'contents-row');row.append(node('span',item.name),node('small',stateLine(item)));if(available.includes('retrieve'))row.append(button('取出',()=>interact('retrieve',entity,item.id),{disabled}));section.append(row);}$('selection-content').append(section);}
  const waterItems=inventory().filter(e=>e.id!==point()?.backpack_id&&e.id!==entity.id&&e.state?.water_capacity>0&&e.state?.open),igniters=inventory().filter(e=>caps(e).includes('ignition')&&finite(e.state?.uses)>0);
  if(available.includes('store'))controlAction('store',entity,inventory().filter(e=>e.id!==point()?.backpack_id&&e.id!==entity.id),disabled,'选择背包物件');
  if(available.includes('fill'))controlAction('fill',entity,waterItems.filter(e=>finite(e.state?.water)<e.state.water_capacity),disabled,'选择盛水容器');
  if(available.includes('pour'))controlAction('pour',entity,waterItems.filter(e=>finite(e.state?.water)>0),disabled,'选择水容器',true);
  if(available.includes('ignite'))controlAction('ignite',entity,igniters,disabled,'选择点火工具');
  if(available.includes('tend'))controlAction('tend',entity,waterItems.filter(e=>finite(e.state?.water)>=5),disabled,'选择至少有 5 份水的容器');
  if(available.includes('fuel'))controlAction('fuel',entity,materialItems('wood').filter(e=>finite(e.state?.fuel)>=40),disabled,'选择投入的一件完整木料');
  if(available.includes('switch_mode'))$('selection-actions').append(button(s.mode==='collect'?'切为遮雨':'切为集雨',()=>interact('switch_mode',entity,undefined,undefined,{mode:s.mode==='collect'?'shelter':'collect'}),{disabled}));
  const options=inspection.action_options||[];
  for(const option of options.filter(o=>!o.available&&o.reason))$('selection-actions').append(node('p',(actionNames[option.action]||option.action)+'：'+option.reason,'action-reason'));
  if(!available.length&&!s.destroyed)$('selection-actions').append(node('p','这件物件当前没有可用行动。','small'));
}
function renderEffects(entity){
  const s=entity.state||{},lines=[];const recipe=recipes().find(r=>r.id===s.recipe);
  const structure=entity.construction||(world?.construction?.nearby||[]).find(f=>f.id===entity.id);
  if(structure){lines.push(...(structure.effects||[]));if(structure.coverage?.length&&!(s.recipe==='canopy'&&s.mode==='collect'))lines.push('地图已标出设施邻域 · '+structure.coverage.length+' 格');}
  else if(recipe?.use)lines.push(recipe.use);
  if(s.recipe&&!s.destroyed){
    if(s.recipe==='walkway')lines.push('浅溪仍可直接涉过；保留通路可免去这一格的随身可湿物件受湿。');
    if(s.recipe==='canopy'&&!s.burning)lines.push(s.mode==='collect'?'集雨会让雨继续淋到周围材料，换得棚内存水；切为遮雨可保留地面干料。':'地面材料可以继续取用、点燃或投料；切为集雨可换得存水，棚自身仍会淋湿。');
    if(s.recipe==='hearth')lines.push('也可以保留木料，等材料自然干燥。烘干仅作用于公共地面物件，可把湿料放到炉旁。');
    lines.push('拆回未烧尽的原投料到当前操作者背包；已耗掉的燃料不返。有水、有内容或正在燃烧时不能拆。');
  }
  if(s.kind==='resource_node')lines.push(s.resource_kind==='stone'?'石料有限，采集后不会再生。':'原料有限。采集后可用 5 份本人存水照料，世界运行约 6 秒后再生一份；期间可以做别的事。');
  if(caps(entity).includes('blocking')&&!s.destroyed)lines.push('这件物件挡住一格通路。绕行保留原地状态；拿走保留材料但占背包；烧毁会永久消耗它。');
  if(entity.construction_material?.eligible)lines.push('可投入搭建'+(entity.material==='wood'?'，也可作为石炉燃料。':'。'));
  else if(entity.construction_material?.reason&&!s.recipe&&s.kind!=='resource_node')lines.push('目前不能投入搭建：'+entity.construction_material.reason);
  const actionOptions=inspection?.entity?.id===entity.id?inspection.action_options||[]:[];
  for(const option of actionOptions.filter(o=>o.available&&o.use))lines.push((actionNames[option.action]||option.action)+'：'+option.use+(option.cost?' · '+formatCost(option.cost):''));
  $('selection-effects').replaceChildren(...[...new Set(lines)].map(line=>node('p',line,'small')));
}
function formatCost(cost){if(typeof cost==='string')return cost;return Object.entries(cost).map(([key,value])=>(materialNames[key]||({water:'水',stored_water:'棚内存水',uses:'点火次数',ignition_uses:'点火次数',stock:'公共原料'}[key])||key)+' '+value).join(' · ');}
function controlAction(action,entity,items,disabled,placeholder,amount=false){
  const row=node('div',undefined,'action-control'),selectEl=node('select');selectEl.setAttribute('aria-label',placeholder);
  if(!items.length){const reason={store:'背包中没有可存的物件',fill:'没有敞开且未装满的容器',pour:'没有敞开且有水的容器',ignite:'背包中没有点火工具',tend:'需要敞开且至少有 5 份水的容器',fuel:'没有可用的干木料'}[action]||'没有适合的物件';selectEl.append(node('option',reason));selectEl.disabled=true;}else for(const item of items){const option=node('option',item.name+(item.state?.water_capacity?' · 水 '+finite(item.state.water):''));option.value=item.id;selectEl.append(option);}
  row.append(selectEl);let amountEl;if(amount){amountEl=node('input');amountEl.type='number';amountEl.min='1';amountEl.max='30';amountEl.value='5';amountEl.setAttribute('aria-label','用水量');row.append(amountEl);}
  row.append(button(actionNames[action],()=>interact(action,entity,selectEl.value,amountEl?Number(amountEl.value):undefined),{disabled:disabled||!items.length}));$('selection-actions').append(row);
}
async function interact(action,entity,itemId,amount,extra={}){
  stopWalking();
  if(amount!==undefined&&(!Number.isInteger(amount)||amount<1)){announce('请输入大于零的整数用水量。',true);return;}
  await perform('grove.interact',async(isCurrent)=>{
    // Processes may change an object while a person is choosing water/tool inputs.
    // Read immediately before publishing the operation, rather than retain a closure's old version.
    const latest=await client.query('grove.inspect',{entity_id:entity.id});
    if(!isCurrent())return;
    if(selectedId===entity.id){inspection=latest;renderSelection();}
    if(!(latest.available_actions||[]).includes(action))throw new Error('这件物件现在不能'+actionNames[action]+'。已读取最新状态，请重新选择。');
    const args={action,expected_player_version:point().version,...extra};
    if(action==='drop')args.item_id=entity.id;else{args.target_id=entity.id;args.expected_target_version=latest.entity.version;if(itemId)args.item_id=itemId;}
    if(amount!==undefined)args.amount=amount;
    return args;
  },actionNames[action]);
}
function renderActivity(){
  if(!world)return;const items=ledger.items().filter(eventNearby).slice(-12).reverse();
  $('activity-list').replaceChildren(...items.map(e=>{const row=node('div',undefined,'activity-row');row.append(node('span',e.kind==='content'?'✦':e.kind==='process'?'≈':'·','activity-icon'),node('p',e.payload?.summary||e.kind),node('small',new Date(e.occurred_at*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})));return row;}));
  if(!items.length){const changes=(world.changes||[]).slice(-4);$('activity-list').replaceChildren(...(changes.length?changes.map(c=>node('p',typeof c==='string'?c:c.summary||'','small')):[node('p','附近还没有已读的变化。试着搬动一件物件，或给它浇一点水。','small')]));}
}
function rainsAt(x,y){
  const chunk='chunk_'+Math.floor(x/16)+'_'+Math.floor(y/16);
  return activeRainChunks.has(chunk);
}
function drawCoverage(){
  for(const structure of world?.construction?.nearby||[]){
    const selected=structure.id===selectedId,entity=entityById(structure.id),active=structure.active&&!entity?.state?.burning&&(structure.recipe==='canopy'&&structure.mode==='shelter'||structure.recipe==='hearth'&&structure.lit);
    if(!selected&&!active)continue;
    const color=structure.recipe==='hearth'?'#efa457':structure.mode==='collect'?'#86c9da':'#e3d89b';
    const tiles=structure.recipe==='canopy'&&structure.mode==='collect'?structure.active?[{x:structure.x,y:structure.y}]:[]:structure.coverage||[];
    for(const tile of tiles){if(!terrain(tile.x,tile.y))continue;const p=screen(tile.x,tile.y);if(active){ctx.fillStyle=color+(selected?'33':'15');ctx.fillRect(p.x-tileSize/2+1,p.y-tileSize/2+1,tileSize-2,tileSize-2);}ctx.strokeStyle=color+(selected?'aa':'55');ctx.lineWidth=selected?1.5:1;ctx.strokeRect(p.x-tileSize/2+2,p.y-tileSize/2+2,tileSize-4,tileSize-4);}
  }
}

function resize(){const box=canvas.getBoundingClientRect();canvasWidth=Math.max(300,box.width);canvasHeight=Math.max(360,box.height);const dpr=Math.min(devicePixelRatio||1,2);canvas.width=Math.round(canvasWidth*dpr);canvas.height=Math.round(canvasHeight*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);tileSize=Math.min(48,Math.max(30,canvasWidth/17));}
function screen(x,y){return {x:canvasWidth/2+(x-camera.x)*tileSize,y:canvasHeight/2+(y-camera.y)*tileSize};}
function tileAt(clientX,clientY){const box=canvas.getBoundingClientRect();return {x:Math.round((clientX-box.left-canvasWidth/2)/tileSize+camera.x),y:Math.round((clientY-box.top-canvasHeight/2)/tileSize+camera.y)};}
const hash=(x,y)=>Math.abs(Math.sin(x*12.9898+y*78.233)*43758.5453)%1;
function roundRect(x,y,w,h,r,fill,stroke){ctx.beginPath();ctx.roundRect(x,y,w,h,r);if(fill){ctx.fillStyle=fill;ctx.fill();}if(stroke){ctx.strokeStyle=stroke;ctx.stroke();}}
function drawTerrain(x,y,t,now){
  const p=screen(x,y),size=tileSize,h=hash(x,y);ctx.fillStyle=t==='water'?'#558c9a':t==='rock'?'#88988a':t==='dirt'?(h>.5?'#b9aa80':'#b0a178'):(h>.5?'#82956b':'#8a9d73');ctx.fillRect(p.x-size/2,p.y-size/2,size+1,size+1);
  if(t==='water'){ctx.strokeStyle='#b1d5cd88';ctx.lineWidth=1.4;const shift=Math.sin(now/1800+x+y)*2;for(let i=0;i<2;i++){ctx.beginPath();ctx.moveTo(p.x-size*.27+shift,p.y+(i-.5)*size*.3);ctx.quadraticCurveTo(p.x,p.y+(i-.5)*size*.3-3,p.x+size*.24+shift,p.y+(i-.5)*size*.3);ctx.stroke();}}
  else if(t==='grass'){ctx.strokeStyle='#657e5055';ctx.lineWidth=1;const sx=p.x+(h-.5)*size*.6,sy=p.y+(hash(y,x)-.5)*size*.6;ctx.beginPath();ctx.moveTo(sx-2,sy+3);ctx.lineTo(sx,sy);ctx.lineTo(sx+1,sy+3);ctx.stroke();}
  else if(t==='rock'){ctx.fillStyle='#70857666';ctx.beginPath();ctx.moveTo(p.x-10,p.y+6);ctx.lineTo(p.x-4,p.y-9);ctx.lineTo(p.x+9,p.y-5);ctx.lineTo(p.x+12,p.y+7);ctx.closePath();ctx.fill();}
  ctx.strokeStyle='#ffffff08';ctx.lineWidth=.7;ctx.strokeRect(p.x-size/2,p.y-size/2,size,size);
}
function drawEntity(e,now){
  if(e.x===null||e.x===undefined||e.y===undefined)return;const p=screen(e.x,e.y),s=tileSize*.64,state=e.state||{};
  ctx.save();ctx.translate(p.x,p.y);ctx.shadowColor='#24342755';ctx.shadowBlur=4;ctx.shadowOffsetY=3;
  if(state.destroyed){ctx.fillStyle='#5b6250';for(let i=0;i<4;i++){ctx.beginPath();ctx.ellipse((i-1.5)*s*.2,s*.2,s*.13,s*.06,i,0,Math.PI*2);ctx.fill();}}
  else if(state.recipe==='walkway'){
    roundRect(-tileSize*.47,-tileSize*.42,tileSize*.94,tileSize*.84,2,'#b89157','#5d5236');ctx.shadowBlur=0;ctx.strokeStyle='#665334';ctx.lineWidth=1.5;for(let i=-2;i<=2;i++){ctx.beginPath();ctx.moveTo(-tileSize*.44,i*tileSize*.15);ctx.lineTo(tileSize*.44,i*tileSize*.15);ctx.stroke();}ctx.fillStyle='#d1b97c';ctx.fillRect(-tileSize*.35,-tileSize*.41,3,tileSize*.82);ctx.fillRect(tileSize*.29,-tileSize*.41,3,tileSize*.82);
  }else if(state.recipe==='canopy'){
    ctx.strokeStyle='#715a39';ctx.lineWidth=3;for(const x of [-s*.43,s*.43]){ctx.beginPath();ctx.moveTo(x,-s*.1);ctx.lineTo(x,s*.51);ctx.stroke();}
    ctx.fillStyle=state.mode==='collect'?'#aacbd0':'#e2d3a5';ctx.strokeStyle='#766844';ctx.lineWidth=1.5;ctx.beginPath();ctx.moveTo(-s*.65,-s*.08);ctx.lineTo(0,-s*.57);ctx.lineTo(s*.65,-s*.08);ctx.lineTo(s*.44,s*.08);ctx.lineTo(0,-s*.2);ctx.lineTo(-s*.44,s*.08);ctx.closePath();ctx.fill();ctx.stroke();
    if(state.mode==='collect'){ctx.fillStyle='#72a4ae';ctx.beginPath();ctx.ellipse(0,s*.35,s*.3,s*.15,0,0,Math.PI*2);ctx.fill();ctx.strokeStyle='#d7e9dc';ctx.stroke();}
  }else if(state.recipe==='hearth'){
    ctx.fillStyle=state.lit?'#d6a15b':'#535e50';ctx.beginPath();ctx.ellipse(0,s*.15,s*.47,s*.3,0,0,Math.PI*2);ctx.fill();for(let i=0;i<7;i++){const a=i*Math.PI*2/7;roundRect(Math.cos(a)*s*.4-s*.12,Math.sin(a)*s*.22+s*.08-s*.11,s*.25,s*.21,3,'#9fa99c','#5f7062');}if(state.hearth_fuel>0)roundRect(-s*.22,s*.02,s*.44,s*.14,3,'#a67a43','#674e32');
  }else if(state.kind==='resource_node'){
    const stock=finite(state.stock);ctx.globalAlpha=stock?1:.58;
    if(state.resource_kind==='stone'){for(let i=0;i<3;i++){const x=(i-1)*s*.3,y=i===1?-s*.18:s*.08;ctx.fillStyle=i===1?'#b6c0b0':'#8b9b91';ctx.strokeStyle='#536c5f';ctx.beginPath();ctx.moveTo(x-s*.23,y+s*.2);ctx.lineTo(x-s*.18,y-s*.2);ctx.lineTo(x+s*.13,y-s*.28);ctx.lineTo(x+s*.24,y+s*.2);ctx.closePath();ctx.fill();ctx.stroke();}}
    else{ctx.strokeStyle=state.resource_kind==='cloth'?'#beae71':'#826d42';ctx.lineWidth=3;for(let i=-1;i<=1;i++){ctx.beginPath();ctx.moveTo(i*s*.18,s*.35);ctx.lineTo(i*s*.25,-s*.31);ctx.stroke();ctx.fillStyle=state.resource_kind==='cloth'?'#dccb94':'#638154';ctx.beginPath();ctx.ellipse(i*s*.25,-s*.31,s*.17,s*.23,i*.4,0,Math.PI*2);ctx.fill();}}
    ctx.globalAlpha=1;ctx.fillStyle=stock?'#efe3b9':'#cabb94';ctx.font='600 10px system-ui';ctx.textAlign='center';ctx.fillText(String(stock),0,s*.68);
  }else if(caps(e).includes('water_source')){ctx.fillStyle='#80bdc1';ctx.beginPath();ctx.ellipse(0,0,s*.47,s*.3,0,0,Math.PI*2);ctx.fill();ctx.strokeStyle='#d4ede4';ctx.beginPath();ctx.arc(0,0,s*.24,0,Math.PI);ctx.stroke();}
  else if(caps(e).includes('container')){
    roundRect(-s/2,-s*.4,s,s*.8,4,e.material==='wood'?'#a27c46':'#b6ad8a','#594d37');ctx.shadowBlur=0;ctx.strokeStyle='#675330';ctx.lineWidth=2;ctx.strokeRect(-s*.31,-s*.29,s*.62,s*.58);
    if(state.open){roundRect(-s*.33,-s*.3,s*.66,s*.48,2,'#3f4335');if(state.water>0)roundRect(-s*.29,-s*.22,s*.58,s*.33,2,'#6dacb5');ctx.fillStyle='#ceb281';ctx.fillRect(-s*.45,-s*.55,s*.9,s*.17);}else{ctx.beginPath();ctx.moveTo(-s*.35,-s*.29);ctx.lineTo(s*.35,s*.29);ctx.moveTo(s*.35,-s*.29);ctx.lineTo(-s*.35,s*.29);ctx.stroke();}
  }else if(e.material==='wood'){
    ctx.rotate(-.13);roundRect(-s*.55,-s*.23,s*1.1,s*.46,s*.2,'#926940','#58462e');ctx.fillStyle='#c6a571';ctx.beginPath();ctx.ellipse(s*.5,0,s*.1,s*.22,0,0,Math.PI*2);ctx.fill();ctx.strokeStyle='#d1b68488';ctx.beginPath();ctx.moveTo(-s*.4,-s*.08);ctx.lineTo(s*.34,-s*.08);ctx.moveTo(-s*.35,s*.1);ctx.lineTo(s*.25,s*.1);ctx.stroke();
  }else if(e.material==='stone'){
    ctx.fillStyle='#9daba6';ctx.strokeStyle='#51695f';ctx.beginPath();ctx.moveTo(-s*.5,s*.22);ctx.lineTo(-s*.36,-s*.3);ctx.lineTo(s*.08,-s*.49);ctx.lineTo(s*.48,-s*.18);ctx.lineTo(s*.42,s*.34);ctx.lineTo(-s*.1,s*.43);ctx.closePath();ctx.fill();ctx.stroke();ctx.fillStyle='#c3ccc0';ctx.beginPath();ctx.moveTo(-s*.3,-s*.22);ctx.lineTo(s*.05,-s*.4);ctx.lineTo(s*.27,-s*.15);ctx.lineTo(-s*.12,s*.06);ctx.closePath();ctx.fill();
  }else if(e.material==='cloth'){
    ctx.fillStyle='#e3c793';ctx.strokeStyle='#95794b';ctx.beginPath();ctx.moveTo(-s*.45,-s*.29);ctx.lineTo(s*.3,-s*.42);ctx.lineTo(s*.49,s*.31);ctx.lineTo(-s*.35,s*.43);ctx.closePath();ctx.fill();ctx.stroke();ctx.strokeStyle='#b6a176';ctx.beginPath();ctx.moveTo(-s*.18,-s*.31);ctx.lineTo(s*.04,s*.35);ctx.stroke();
  }else{roundRect(-s*.3,-s*.3,s*.6,s*.6,5,'#c8bf91','#766c4b');}
  ctx.shadowBlur=0;ctx.shadowOffsetY=0;
  if(state.wetness>0&&!state.destroyed){ctx.fillStyle='#c3eff0cc';ctx.beginPath();ctx.moveTo(s*.29,-s*.35);ctx.bezierCurveTo(s*.05,-s*.05,s*.4,s*.08,s*.48,-s*.08);ctx.bezierCurveTo(s*.47,-s*.18,s*.35,-s*.29,s*.29,-s*.35);ctx.fill();}
  if((state.burning||state.recipe==='hearth'&&state.lit)&&!state.destroyed){const f=1+Math.sin(now/90+e.x)*.08;ctx.fillStyle='#f5a852';ctx.beginPath();ctx.moveTo(-s*.28,s*.1);ctx.quadraticCurveTo(-s*.36,-s*.27,-s*.05,-s*.66*f);ctx.quadraticCurveTo(s*.03,-s*.18,s*.22,-s*.42);ctx.quadraticCurveTo(s*.41,-s*.04,s*.2,s*.15);ctx.closePath();ctx.fill();ctx.fillStyle='#ffe6a1';ctx.beginPath();ctx.moveTo(-s*.12,s*.12);ctx.quadraticCurveTo(-s*.17,-s*.16,s*.03,-s*.35*f);ctx.quadraticCurveTo(s*.24,0,s*.1,s*.14);ctx.fill();}
  if(caps(e).includes('blocking')&&!state.destroyed){ctx.strokeStyle='#624633';ctx.lineWidth=2;ctx.setLineDash([4,3]);ctx.strokeRect(-tileSize*.45,-tileSize*.42,tileSize*.9,tileSize*.84);ctx.setLineDash([]);}
  if(selectedId===e.id){ctx.strokeStyle='#fff0bd';ctx.lineWidth=2.5;ctx.beginPath();ctx.ellipse(0,s*.25,s*.66,s*.35,0,0,Math.PI*2);ctx.stroke();}
  ctx.restore();
  if(selectedId===e.id||hoverTile?.x===e.x&&hoverTile?.y===e.y){ctx.font='600 12px system-ui';const width=ctx.measureText(e.name).width+16;roundRect(p.x-width/2,p.y-tileSize*.92,width,23,5,'#24392ded');ctx.fillStyle='#f8f0d8';ctx.textAlign='center';ctx.fillText(e.name,p.x,p.y-tileSize*.92+16);}
}
function drawPlayer(p,own,now){
  let pos=p;if(own&&motion){const elapsed=Math.min(1,(now-motion.start)/motion.duration),ease=1-Math.pow(1-elapsed,3);pos={x:motion.from.x+(motion.to.x-motion.from.x)*ease,y:motion.from.y+(motion.to.y-motion.from.y)*ease};if(elapsed===1)motion=null;}
  const at=screen(pos.x,pos.y),s=tileSize;ctx.fillStyle='#30433555';ctx.beginPath();ctx.ellipse(at.x,at.y+s*.24,s*.28,s*.12,0,0,Math.PI*2);ctx.fill();
  roundRect(at.x-s*.2,at.y-s*.04,s*.4,s*.44,5,own?'#f0d395':'#aec4d1','#4b5947');ctx.fillStyle='#ecd9ba';ctx.beginPath();ctx.arc(at.x,at.y-s*.16,s*.15,0,Math.PI*2);ctx.fill();ctx.fillStyle=own?'#405d46':'#5b6f81';ctx.beginPath();ctx.ellipse(at.x,at.y-s*.28,s*.23,s*.1,0,0,Math.PI*2);ctx.fill();
  if(own){ctx.strokeStyle='#f9edc3';ctx.lineWidth=2;ctx.beginPath();ctx.ellipse(at.x,at.y+s*.27,s*.35,s*.15,0,0,Math.PI*2);ctx.stroke();}
  ctx.font='600 11px system-ui';ctx.textAlign='center';ctx.fillStyle='#193b2f';ctx.fillText(own?'你':p.name||'旅人',at.x,at.y+s*.68);
}
function draw(now){
  if(!playing||document.hidden){requestAnimationFrame(draw);return;}
  ctx.clearRect(0,0,canvasWidth,canvasHeight);ctx.fillStyle='#65795b';ctx.fillRect(0,0,canvasWidth,canvasHeight);
  if(world?.player){const p=point();camera.x+=(p.x-camera.x)*.14;camera.y+=(p.y-camera.y)*.14;const halfX=Math.ceil(canvasWidth/tileSize/2)+1,halfY=Math.ceil(canvasHeight/tileSize/2)+1;
    for(let y=Math.floor(camera.y)-halfY;y<=camera.y+halfY;y++)for(let x=Math.floor(camera.x)-halfX;x<=camera.x+halfX;x++){const t=terrain(x,y);if(t){drawTerrain(x,y,t,now);if(rainsAt(x,y)){const at=screen(x,y);ctx.fillStyle='#8bc8e51c';ctx.fillRect(at.x-tileSize/2,at.y-tileSize/2,tileSize+1,tileSize+1);}}else{const at=screen(x,y);ctx.fillStyle='#334a3a';ctx.fillRect(at.x-tileSize/2,at.y-tileSize/2,tileSize+1,tileSize+1);}}
    drawCoverage();
    if(walkPath.length){ctx.strokeStyle='#f6e1a99c';ctx.lineWidth=3;ctx.setLineDash([4,5]);ctx.beginPath();const start=screen(p.x,p.y);ctx.moveTo(start.x,start.y);for(const next of walkPath){const at=screen(next.x,next.y);ctx.lineTo(at.x,at.y);}ctx.stroke();ctx.setLineDash([]);}
    if(hoverTile){const at=screen(hoverTile.x,hoverTile.y);ctx.strokeStyle='#fff3c855';ctx.lineWidth=1.5;ctx.strokeRect(at.x-tileSize/2+2,at.y-tileSize/2+2,tileSize-4,tileSize-4);}
    if(selectedTile){const at=screen(selectedTile.x,selectedTile.y);ctx.strokeStyle='#fff0ae';ctx.lineWidth=2.5;ctx.strokeRect(at.x-tileSize/2+1,at.y-tileSize/2+1,tileSize-2,tileSize-2);}
    for(const e of [...ground()].sort((a,b)=>a.y-b.y))drawEntity(e,now);
    for(const other of world.players||[])if(other.id!==p.id&&other.role_id!==client.roleId)drawPlayer(other,false,now);drawPlayer(p,true,now);
    if(activeRainChunks.size){ctx.strokeStyle='#deebde75';ctx.lineWidth=1;for(let i=0;i<35;i++){const x=(i*61)%canvasWidth,y=(now*.11+i*93)%canvasHeight,tx=Math.round((x-canvasWidth/2)/tileSize+camera.x),ty=Math.round((y-canvasHeight/2)/tileSize+camera.y);if(!rainsAt(tx,ty))continue;ctx.beginPath();ctx.moveTo(x,y);ctx.lineTo(x-4,y+11);ctx.stroke();}}
    const shade=ctx.createRadialGradient(canvasWidth/2,canvasHeight/2,canvasWidth*.12,canvasWidth/2,canvasHeight/2,Math.max(canvasWidth,canvasHeight)*.7);shade.addColorStop(0,'#172a1e00');shade.addColorStop(1,'#172a1e55');ctx.fillStyle=shade;ctx.fillRect(0,0,canvasWidth,canvasHeight);
  }
  requestAnimationFrame(draw);
}

canvas.addEventListener('pointermove',event=>{hoverTile=tileAt(event.clientX,event.clientY);});canvas.addEventListener('pointerleave',()=>{hoverTile=null;});
canvas.addEventListener('pointerdown',event=>{if(!playing||!world)return;const tile=tileAt(event.clientX,event.clientY);canvas.focus({preventScroll:true});const objects=ground().filter(e=>e.x===tile.x&&e.y===tile.y);if(objects.length){stopWalking();run(()=>select(objects.find(e=>!e.state?.destroyed)?.id||objects[0].id));}else selectTile(tile);});
canvas.addEventListener('dblclick',event=>{if(!playing||!world)return;const tile=tileAt(event.clientX,event.clientY);if(selectedTile?.x===tile.x&&selectedTile?.y===tile.y)goTo(tile.x,tile.y);});
window.addEventListener('keydown',event=>{if(event.key==='Escape'){stopWalking();document.body.classList.remove('context-open','agent-open');$('agent-toggle').setAttribute('aria-expanded','false');return;}const pressed=event.key.length===1?event.key.toLowerCase():event.key;if(!playing||!directions[pressed]||event.altKey||event.ctrlKey||event.metaKey||['INPUT','TEXTAREA','SELECT','BUTTON'].includes(document.activeElement?.tagName))return;event.preventDefault();if(event.repeat||held===pressed)return;held=pressed;walkPath=[];pathTarget=null;run(walk);});
window.addEventListener('keyup',event=>{const released=event.key.length===1?event.key.toLowerCase():event.key;if(held===released){held=null;clearTimeout(walkTimer);}});window.addEventListener('blur',stopWalking);
for(const el of document.querySelectorAll('[data-direction]'))el.addEventListener('click',()=>{stopWalking();if(point())run(()=>move(...el.dataset.direction.split(',').map(Number)));});
$('entry-form').addEventListener('submit',event=>{event.preventDefault();run(enter);});$('entry-cancel').addEventListener('click',()=>run(cancelEntry));
$('import-key').addEventListener('change',event=>run(async()=>{const file=event.target.files?.[0];if(!file)return;if(file.size>16384)throw new Error('恢复文件过大，请选择原身份 JSON 文件。');importedKey=await validateKey(JSON.parse(await file.text()));key=importedKey;$('identity-status').textContent='已选中恢复身份。进入后会保存它并接续原角色。';event.target.value='';}));
$('refresh').addEventListener('click',()=>run(async()=>{stopWalking();const epoch=session;await refreshWorld(true);if(epoch===session&&playing)announce('已读取最新附近现场。');}));
$('backup').addEventListener('click',()=>{if(!key)return;const blob=new Blob([JSON.stringify(key,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=node('a');a.href=url;a.download='shared-grove-identity.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);announce('恢复文件包含你的身份密钥，请保存在自己的安全位置。');});
$('follow-player').addEventListener('click',()=>{if(point()){camera={x:point().x,y:point().y};canvas.focus({preventScroll:true});}});$('dismiss-onboarding').addEventListener('click',()=>{$('onboarding').hidden=true;});
$('selection-close').addEventListener('click',()=>{document.body.classList.remove('context-open');canvas.focus({preventScroll:true});});
$('agent-toggle').addEventListener('click',()=>{const open=document.body.classList.toggle('agent-open');if(open)document.body.classList.remove('context-open');$('agent-toggle').setAttribute('aria-expanded',String(open));if(open){if(window.matchMedia('(min-width:761px)').matches)$('agent-panel').scrollIntoView({block:'nearest'});$('chat-text').focus({preventScroll:true});}});
$('ask-selection').addEventListener('click',()=>run(async()=>{if(!world)return;const entity=entityById(selectedId),tile=selectedTile;const text=entity?'我正在看'+entity.name+'（'+(entity.x??point().x)+'，'+(entity.y??point().y)+'）。请读取当前现场，解释它能怎样用，以及不同办法的材料和环境取舍。':tile?'我正在看位置（'+tile.x+'，'+tile.y+'）。请读取当前现场，解释在这里走动或搭建设施有哪些取舍。':'请读取我身边的现场，解释我现在有哪些不同的办法。';document.body.classList.add('agent-open');document.body.classList.remove('context-open');$('agent-toggle').setAttribute('aria-expanded','true');await hostCommand('prompt',{text});}));
$('recover-operation').addEventListener('click',()=>run(async()=>{stopWalking();const epoch=session,result=await client.recover();if(epoch!==session||!playing)return;if(result?.rejected){renderPending();return;}$('error').hidden=true;announce('原操作结果已核对。');await refreshWorld(true);if(epoch===session)renderPending();}));
$('release-operation').addEventListener('click',()=>run(async()=>{const epoch=session;await client.releaseRejected();if(epoch!==session)return;await refreshWorld(true);if(epoch!==session)return;renderPending();announce('已读取当前状态，可以重新选择行动。');}));
$('reconnect-world').addEventListener('click',()=>run(async()=>{const epoch=session;await authenticate(key);if(epoch!==session)return;if(await hostCommand('recover',{token:client.token,public_key:key.public_key,name:point()?.name||prefs().name},{refresh:true})){scheduleWorld();scheduleHost();}}));
$('chat-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const text=$('chat-text').value.trim();if(!text)return;if(await hostCommand('prompt',{text})){$('chat-text').value='';client.saveDraft('grove-chat','');}});});
$('generation-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const text=$('generation-text').value.trim();if(!text)return;await hostCommand('generate',{text,kind:$('generation-kind').value});});});
$('generation-recover').addEventListener('click',()=>run(()=>hostCommand('recover',{token:client.token,public_key:key.public_key,name:point()?.name||prefs().name},{refresh:true})));
$('chat-text').addEventListener('input',()=>{if(current())try{client.saveDraft('grove-chat',$('chat-text').value);}catch{}});
$('generation-text').addEventListener('input',()=>{if(current())try{client.saveDraft('grove-generation',$('generation-text').value);}catch{}});
$('generation-kind').addEventListener('change',()=>{$('generation-text').placeholder={layout:'在附近放一个能盛水的石碗，方便大家取水。',rain:'让附近下一场短雨，看看木头与布怎样渐渐变湿。',drift:'顺流漂来一个装着布卷的木箱，让路过的人能取用。'}[$('generation-kind').value];});
$('agent-cancel').addEventListener('click',()=>run(async()=>{if(await hostCommand('cancel',{}))announce('已请求中止当前 Agent 请求；已提交的世界变化保留。');}));
$('generation-cancel').addEventListener('click',()=>run(async()=>{if(await hostCommand('cancel',{}))announce('已请求中止创作；已经发表的内容保留，结果以世界读回为准。');}));
$('auto-stop').addEventListener('click',()=>run(async()=>{if(await hostCommand('automatic',{enabled:false}))announce('自动参与已停止；已提交的内容保留。你可以继续亲手操作。');}));
$('agent-disconnect').addEventListener('click',()=>run(async()=>{if(await hostCommand('disconnect',{})){clearTimeout(hostTimer);announce('Agent 已断开。仍可以亲手走动和操作。');}}));$('agent-reconnect').addEventListener('click',()=>run(restartHost));
$('auto-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{if(await hostCommand('automatic',{enabled:$('auto-enabled').checked,interval_seconds:Number($('auto-interval').value),max_count:Number($('auto-budget').value)}))announce('已保存 Agent 的请求上限。');});});
$('auto-form').addEventListener('input',()=>{autoFormDirty=true;});
$('leave').addEventListener('click',()=>{stopWalking();++session;++readEpoch;++hostReadEpoch;++inspectEpoch;playing=false;entering=false;actionBusy=false;hostCommandBusy=false;triggerFlight=false;queuedTriggerSeq=0;explorationQueued=false;lastTriggerSeq=0;lastObservedSeq=0;discoveredChunks.clear();chosenBuildMaterials.clear();touchedBuildChoices.clear();activeRainChunks.clear();clearTimeout(worldTimer);clearTimeout(hostTimer);client.cancel();client.token=null;identity=null;world=null;inspection=null;selectedId=null;selectedTile=null;ledger.clear();streamCursor=null;lastSelectionSignature='';lastInventorySignature='';lastChatSignature='';lastGenerationSignature='';document.body.classList.remove('playing','context-open','agent-open');$('agent-toggle').setAttribute('aria-expanded','false');$('game').hidden=true;$('entry').hidden=false;$('world-status').textContent='已离开 · 可接续';$('entry-progress').replaceChildren();$('enter').disabled=false;announce('已离开网页。身份与变化保留；自动参与若已开启，仍遵守保存的请求上限。');});
window.addEventListener('resize',resize);window.addEventListener('pagehide',()=>{stopWalking();client.cancel();});document.addEventListener('visibilitychange',()=>{if(document.hidden)stopWalking();else if(playing){run(()=>refreshWorld());scheduleHost(250);}});
window.addEventListener('pageshow',event=>{if(event.persisted&&playing){run(()=>refreshWorld(true));scheduleWorld();scheduleHost(250);}});
new ResizeObserver(resize).observe(canvas);requestAnimationFrame(draw);readIdentity();
