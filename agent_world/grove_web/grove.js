import {createKey,validateKey,savedKey} from '/static/commons-client.js';
import {EventLedger} from '/static/stream-client.js';
import {GroveClient} from './grove-client.js';
import {matterLots,bodyQuantities,materialLabel,slotLabel,entitySummary,simulationStats,entityGlyph as genericGlyph,safePresentation,presentationBadgeText,publicWorkLocalTarget,publicWorkFactLines,publicWorkDirectoryLines,publicWorkMarkerGeometry,actionDescriptors,descriptorLabel,candidateLabel,previewVersionsChanged,ruleSnapshotChanged,factLabel,factValue,factRowText,factLines,summaryFact,fabricationSources,fabricationPlan,fabricationInputContract,schemaInputErrors,createMechanicalMotion,visibleWaterQuantity,receiptOutcomeLines,executionReadbackLines,executionTaskSummaryLines} from './grove-world-ui.js?v=client-projection-v2';

const $=id=>document.getElementById(id);
const client=new GroveClient();
const ledger=new EventLedger(100);
const canvas=$('world-canvas'),ctx=canvas.getContext('2d');
const directions={w:[0,-1],a:[-1,0],s:[0,1],d:[1,0],ArrowUp:[0,-1],ArrowLeft:[-1,0],ArrowDown:[0,1],ArrowRight:[1,0]};
const hostNames={disconnected:'已断开',connecting:'正在连接',ready:'已连接',thinking:'思考中',paused:'已暂停',error:'连接受阻'};
let key=null,identity=null,world=null,host=null,session=0,readEpoch=0,worldTimer=null,hostTimer=null;
let entering=false,playing=false,actionBusy=false,refreshFlight=null,streamCursor=null,lastWorldRead=0;
let worldFresh=false;
const mechanicalMotion=createMechanicalMotion();
let selectedId=null,selectedTile=null,inspection=null,inspectionError=null,inspectEpoch=0,selectedFromBag=false,held=null,walkPath=[],pathTarget=null,walkTimer=null;
let tileSize=40,canvasWidth=600,canvasHeight=600,camera={x:0,y:0},motion=null,hoverTile=null,lastChatSignature='',lastHostSignature='',lastSelectionSignature='',lastInventorySignature='',lastAction='';
let importedKey=null;
let hostReadEpoch=0,lastGenerationSignature='',hostCommandBusy=false;
let lastContentSignature='',intentDirty=false,contentHistoryId=null,contentHistoryPages=[],contentHistoryBusy=false,operationRecoveryBusy=false;
let inventoryPageBusy=false,inventoryPageCursors=[null];
let combinationCheckingForm=null,inspectionFlight=null,inspectionErrorStatus=null;
let activeRainChunks=new Set();
let goalDirty=false,executionReclaimBusy=false,hostCommandEpoch=0,manualControlRecoveryPending=false;
let definitionReadEpoch=0,definitionReadBusy=false,definitionReadValue=null,definitionReadError=null,definitionReadRef=null,definitionReadOpen=false;
let directoryEpoch=0,directoryBusy=false,directoryFilters={},directoryInputs={author:'',key:''},directoryPage=null,directoryCursors=[null],directoryError=null,directoryOpen=false,directoryDefinition=null,directoryDefinitionRef=null,directoryType=null,fabricationMenu=null,fabricationBusy=false,fabricationRows=[],fabricationError=null;
let publicWorksEpoch=0,publicWorksBusy=false,publicWorksPage=null,publicWorksFilters={},publicWorksCursors=[null],publicWorksError=null,publicWorksReadAt=0,publicWorksRenderSignature='',publicWorkMarker=null;
const factDisclosures=new Map();
const mapLabelSlots=new Map();
let activeActionKey=null,hostCommandKind=null,hostCommandFlight=null;
const actionDrafts=new Map();
const interactionSelections=new Map();
const terrainNames={grass:'草地',dirt:'泥地',water:'浅溪',rock:'岩壁'};
const actionKey=descriptor=>(descriptor.function_id||'grove.interact')+':'+descriptor.action;
function friendlyInstruction(value){return String(value||'').replace(/\bfeedstock\b/g,'原料').replace(/\bsymbol\b/g,'材料名称').replace(/\blot_id\b/g,'材料批次').replace(/\bamount\b/g,'数量').replace(/\bitem_id\b/g,'来源物件').replace(/\btransfer\b/g,'转入材料').replace(/\bstore\b/g,'整件放入').replace(/\brelease\b/g,'领取').replace(/\bcontained\b/g,'内含').replace(/\blining\b/g,'衬层').replace(/(\d+)u(wood|cloth|stone|water)\b/g,(_,quantity,material)=>quantity+' 单位'+materialLabel(material));}
function displayWorldText(value){return friendlyInstruction(value).replace(/program_[0-9a-f]{64}#[A-Za-z][A-Za-z0-9_]*/g,id=>materialLabel(id,selectedEntity())).replace(/(?:object_|seed_|item_|awr_)[A-Za-z0-9_]+/g,id=>entityById(id)?.name||'该物件');}
function rememberActionDrafts(){
  for(const form of $('selection-actions').querySelectorAll('.descriptor-action')){
    const values=[...form.querySelectorAll('[data-control-id]')].map(control=>({id:control.dataset.controlId,value:control.value,selected:control.multiple?[...control.selectedOptions].map(option=>option.value):undefined,checked:control.checked,text:control.selectedOptions?.[0]?.textContent}));
    actionDrafts.set(form.dataset.entityId+':'+form.dataset.action,{values,descriptor:form.observedDescriptor});
  }
  if(actionDrafts.size>32)actionDrafts.delete(actionDrafts.keys().next().value);
}
function restoreActionDraft(form,update){
  const values=actionDrafts.get(form.dataset.entityId+':'+form.dataset.action)?.values;if(!values)return;
  const controls=[...form.querySelectorAll('[data-control-id]')];
  const restore=control=>{const saved=values.find(row=>row.id===control.dataset.controlId);if(!saved)return;const selected=saved.selected||(saved.value?[saved.value]:[]);if(control.tagName==='SELECT')for(const value of selected)if(![...control.options].some(option=>option.value===value)){const option=node('option',(saved.text||'原选择')+' · 当前不可用，请重选');option.value=value;option.disabled=true;control.append(option);}if(control.multiple){for(const option of control.options)option.selected=selected.includes(option.value);}else control.value=saved.value;if(control.type==='checkbox')control.checked=saved.checked;};
  controls.forEach(restore);
  update(true);
  // Rebuilding source lots must not silently replace a previously chosen batch.
  controls.forEach(restore);update(false);
}
function renderTargetChoices(location){
  const area=$('target-choice');if(!area)return;area.replaceChildren();
  const objects=ground().filter(e=>e.x===location?.x&&e.y===location?.y).sort((a,b)=>a.id.localeCompare(b.id));
  area.hidden=!objects.length;if(!objects.length)return;
  area.append(node('span','这里有','small'));
  for(const [index,entity] of objects.entries()){const duplicate=objects.filter(other=>other.name===entity.name).length>1;const pick=button(entityMapLabel(entity,true)+(duplicate?' · 同名第 '+(index+1)+' 件':''),()=>run(()=>select(entity.id)),{secondary:true});pick.dataset.entityId=entity.id;pick.setAttribute('aria-pressed',String(selectedId===entity.id));area.append(pick);}
  const groundButton=button(terrainNames[terrain(location.x,location.y)]||'这块地面',()=>selectTile(location),{secondary:true});groundButton.setAttribute('aria-pressed',String(!!selectedTile));area.append(groundButton);
}

function node(tag,text,className){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;}
function plainMessage(text){return String(text??'').replace(/\*\*([^*\n]+)\*\*/g,'$1');}
function previewMessage(text){
  const value=plainMessage(text).trim(),paragraph=value.split(/\n\s*\n/)[0].replace(/\s+/g,' '),preview=Array.from(paragraph).slice(0,140).join('');
  return preview+(preview!==value.replace(/\s+/g,' ')?'… 全文见“目标与交谈”。':'');
}
function messageNode(text){
  const el=node('p'),value=String(text??'');let offset=0;
  for(const match of value.matchAll(/\*\*([^*\n]+)\*\*/g)){el.append(document.createTextNode(value.slice(offset,match.index)),node('strong',match[1]));offset=match.index+match[0].length;}
  el.append(document.createTextNode(value.slice(offset)));return el;
}
function currentAssistantReply(value){
  if(value?.status!=='ready'||value.completed!==true||value.reclaim_in_progress||Number(value.unknown_operations)>0||
    ['starting','running','unknown'].includes(value.execution?.status))return null;
  const message=[...(value.messages||[])].reverse().find(item=>item.role==='assistant'&&item.text);
  if(!message||!Object.hasOwn(message,'task_id')||!message.response_message_id||message.response_message_id!==value.message_id||
    !message.session_id||message.session_id!==(value.response_session_id||value.session_id))return null;
  if(message.task_id&&(value.execution?.status!=='completed'||message.task_id!==value.execution?.task_id||message.request_number!==value.execution?.requests_used))return null;
  return message;
}
function agentMessageLabel(message,value){
  if(message.role==='user')return '你';
  if(message.role==='event')return '现场变化';
  const sessionMatches=message.session_id&&message.session_id===(value.response_session_id||value.session_id);
  const taskMatches=message.task_id&&message.task_id===value.execution?.task_id;
  const request=Number.isInteger(message.request_number)?'第 '+message.request_number+' 段':'';
  const isCurrent=currentAssistantReply(value)===message;
  const source=isCurrent?(taskMatches?'本次委托回复':'本次交谈回复'):taskMatches&&sessionMatches?'本次委托历史':message.task_id?'历史委托':sessionMatches&&message.response_message_id?'交谈历史':'历史回复 · 请求来源未记录';
  const at=Number.isFinite(message.time)?new Date(message.time*1000).toLocaleString('zh-CN'):'';
  return ['你的 Agent',source,request,at].filter(Boolean).join(' · ');
}
function objectFactLines(value){
  const entity=value?.entity;if(!entity)return [];
  const lines=[];
  if(Number.isFinite(entity.x)&&Number.isFinite(entity.y))lines.push('在地面 '+entity.x+'，'+entity.y);
  if(entity.summary_facts)lines.push(...factLines(entity.summary_facts).slice(0,8));
  return lines;
}
function actionTargetReadbackLines(execution){
  if(execution.readback_actual!==true)return [];
  const entries=execution.readback?.action_targets;if(!Array.isArray(entries))return [];
  return entries.slice(0,8).flatMap(entry=>{
    if(entry.unavailable)return ['原任务对象读取受阻：'+(entry.unavailable.message||'详情未读到')];
    const at=Number.isFinite(entry.observed_at)?' · 原任务读取于 '+new Date(entry.observed_at*1000).toLocaleString('zh-CN'):'';
    return ['原任务读取对象：'+(entry.name||entityById(entry.entity_id)?.name||'物件')+at+'；当前详情需重新读取。'];
  });
}
function button(text,handler,{disabled=false,secondary=false}={}){const el=node('button',text,secondary?'secondary':'');el.type='button';el.disabled=disabled;el.addEventListener('click',()=>handler());return el;}
function announce(text,error=false){const el=$(error?'error':'notice');el.textContent=text;el.hidden=!text;if(!error&&text)setTimeout(()=>{if(el.textContent===text)el.hidden=true;},6500);}
function fail(error){if(error.cancelled)return;const message=['Failed to fetch','Load failed','NetworkError when attempting to fetch resource.'].includes(error.message)?(client.pending()?'连接中断。重连后先核对原操作结果。':'连接中断，请检查连接后重试。'):error.message||String(error);announce(message,true);if(error.status===401&&!error.staleAuth){client.token=null;held=null;walkPath=[];$('reconnect-world').hidden=false;$('world-status').textContent='网页需要重连';$('movement-feedback').textContent='授权失效：同钥重连后继续。';}renderPending();}
function run(fn){return Promise.resolve().then(fn).catch(fail);}
const current=()=>identity&&client.token;
// The connector owns model-start eligibility; this layer only adds in-flight
// browser guards and validates the person's continuation input.
function agentAdmission(kind){
  const admission=host?.admission?.[kind];
  if(!playing||!current())return {allowed:false,reason:'进入世界并恢复连接后再使用 Agent。'};
  if(hostCommandBusy||executionReclaimBusy)return {allowed:false,reason:'先等待当前 Agent 请求或收回操作完成。'};
  if(manualControlRecoveryPending)return {allowed:false,reason:'原行动结果尚未核对；恢复连接后先核对原回执。'};
  if(['goal','resume','extend'].includes(kind)&&(actionBusy||client.pending()))return {allowed:false,reason:'先完成或核对当前世界操作，再授权 Agent 行动。'};
  if(typeof admission?.allowed!=='boolean')return {allowed:false,reason:'Agent 资格尚未读取；恢复连接后再提问、创作或委托。'};
  return {allowed:admission.allowed,reason:admission.allowed?'':admission.reason||'当前不能开始这项 Agent 请求，请恢复连接后核对状态。'};
}
function executionLimits(){
  const limits=host?.execution_limits;
  return limits&&['default_requests','max_requests','additional_requests','max_actions'].every(key=>Number.isInteger(limits[key])&&limits[key]>0)?limits:null;
}
function continuationAdmission(){
  const execution=host?.execution||{},limits=executionLimits(),additional=Number($('execution-additional-requests').value);
  if(!limits)return {allowed:false,reason:'执行额度尚未读取；恢复连接后再接续。'};
  const limit=Math.max(0,Math.min(limits.additional_requests,limits.max_requests-(execution.max_requests||0)));
  if(!Number.isInteger(additional)||additional<0||additional>limit)return {allowed:false,reason:'每批最多追加 '+limits.additional_requests+' 次请求，总计最多 '+limits.max_requests+' 次；追加数须为非负整数。'};
  return agentAdmission(additional>0?'extend':'resume');
}
function applyAdmission(id,admission){
  const control=$(id);control.disabled=!admission.allowed;control.title=admission.reason;
  if(admission.reason)control.setAttribute('aria-description',admission.reason);else control.removeAttribute('aria-description');
}
const point=()=>world?.player;
const entityLocation=entity=>entity?.location||{x:entity?.x,y:entity?.y};
const distance=(entity)=>entity?.custody?.holder_id===client.roleId?0:Math.abs((entityLocation(entity).x??Infinity)-(point()?.x??0))+Math.abs((entityLocation(entity).y??Infinity)-(point()?.y??0));
const inventory=()=>world?.inventory||[];
const ground=()=>world?.entities||[];
const entityById=id=>ground().find(e=>e.id===id)||inventory().find(e=>e.id===id)||(inspection?.entity?.id===id?inspection.entity:null)||inspection?.contents?.find(e=>e.id===id)||inspection?.attachments?.find(e=>e.id===id)||(world?.public_content?.anchors||[]).find(anchor=>anchor.entity_id===id)?.entity;
const selectedEntity=()=>selectedId?(inspection?.entity?.id===selectedId?inspection.entity:entityById(selectedId)):null;
const finite=(value,fallback=0)=>Number.isFinite(value)?value:fallback;
const nearby=(x,y)=>!!world?.bounds&&Number.isFinite(x)&&Number.isFinite(y)&&x>=world.bounds.min_x&&x<=world.bounds.max_x&&y>=world.bounds.min_y&&y<=world.bounds.max_y;
function eventNearby(event){const p=event.payload||{};return nearby(p.x,p.y)||(p.affected||[]).some(a=>nearby(a.x,a.y))||(p.centers||[]).some(a=>nearby(a.x,a.y));}
function closePanels(){document.body.classList.remove('agent-open','context-open','inventory-open');$('inventory-panel').open=false;for(const id of ['agent-toggle','context-toggle','inventory-toggle'])$(id).setAttribute('aria-expanded','false');}
function openContext(){closePanels();document.body.classList.add('context-open');$('context-toggle').setAttribute('aria-expanded','true');}
function openAgent(){closePanels();document.body.classList.add('agent-open');$('agent-toggle').setAttribute('aria-expanded','true');}

async function readIdentity(){
  try{key=await savedKey();if(key){key=await validateKey(key);$('identity-status').textContent='本机身份已保存。进入后接续原来的位置与背包。';}else $('identity-status').textContent='首次进入会在本机保存一把身份密钥。';}
  catch(error){$('identity-status').textContent=error.message;}
  try{const response=await fetch('/health',{cache:'no-store'});if(!response.ok)throw new Error('本机入口暂未响应。');const config=await response.json();if(typeof config.default_model!=='string'||!config.default_model)throw new Error('本机入口没有提供默认模型。');for(const id of ['entry-model','game-model'])$(id).options[0].textContent='默认：'+config.default_model;}
  catch(error){$('identity-status').textContent=error.message||'默认模型暂未读到，请刷新后重试。';return;}
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
  worldFresh=false;mechanicalMotion.reset();client.world.cancelReads();++readEpoch;
  ++session;entering=true;const epoch=session;lastHostSignature='';
  announce('');$('error').hidden=true;$('enter').disabled=true;$('entry-cancel').hidden=false;$('entry-progress').replaceChildren(node('p','正在用密钥连接世界…'));
  try{
    await authenticate(importedKey||key||await createKey());if(epoch!==session)return;savePrefs();
    const value=prefs();
    // Recovery does not fabricate a new completion; the connector retains the real gate.
    host=await client.connector('recover',{token:client.token,public_key:key.public_key,name:value.name});if(epoch!==session)return;
    if(host?.entered&&host?.world_read&&host?.first_response_success){await showGame(epoch);return;}
    if(['disconnected','error','paused'].includes(host.status)){
      host=await client.connector('start',{token:client.token,public_key:key.public_key,name:value.name,...(value.model?{model:value.model}:{})});
      if(epoch!==session)return;
    }
    renderHost();scheduleHost(350);
  }catch(error){if(epoch===session){entering=false;$('enter').disabled=false;$('entry-cancel').hidden=true;$('entry-progress').replaceChildren(node('p','连接未完成，请重试。'));fail(error);}}
}
async function showGame(epoch=session){
  if(epoch!==session||!current())return;
  playing=true;entering=false;document.body.classList.add('playing');$('entry').hidden=true;$('game').hidden=false;$('entry-cancel').hidden=true;$('enter').disabled=false;
  $('game-host').value=host?.host_url||$('entry-host').value;$('game-model').value=host?.model||$('entry-model').value;
  $('world-status').textContent='共同世界 · 已进入';window.scrollTo(0,0);resize();renderHost();
  if(client.pending())renderPending();
  await refreshWorld(true);scheduleWorld();scheduleHost();
  try{
    $('chat-text').value=client.draft('grove-chat');$('generation-text').value=client.draft('grove-generation');
    const savedIntent=host?.context?.intent||'',draftIntent=client.storage.getItem(client.draftKey('grove-intent'));
    $('host-intent').value=draftIntent??savedIntent;intentDirty=draftIntent!==null&&draftIntent!==savedIntent;
    const draftGoal=client.storage.getItem(client.draftKey('grove-goal')),savedGoal=host?.execution?.goal||'';
    $('execution-goal').value=draftGoal??savedGoal;goalDirty=draftGoal!==null&&draftGoal!==savedGoal;
  }catch{}
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
  }catch(error){if(epoch===session){if(host){host={...host,admission:null};renderHost();}$('agent-status').textContent='宿主暂不可达';$('agent-detail').textContent=manualControlRecoveryPending?'原行动结果尚未核对；恢复宿主后先核对原回执，再继续行动。':'你的世界操作仍可继续。恢复宿主后再提问或共创。';if(entering){entering=false;$('enter').disabled=false;fail(error);}}}
  finally{scheduleHost();}
}
function scheduleHost(delay=1200){clearTimeout(hostTimer);if(current()&&(entering||playing)&&host?.status!=='disconnected')hostTimer=setTimeout(pollHost,delay);}
function renderHost(){
  if(!host)return;const busy=hostCommandBusy||executionReclaimBusy||host.reclaim_in_progress===true||['connecting','thinking'].includes(host.status);
  const execution=host.execution;
  // Recovery is asynchronous: clear the local gate only from settled receipts
  // and a closed world grant, never from the /recover acknowledgement alone.
  if(manualControlRecoveryPending&&host.admission&&['ready','paused'].includes(host.status)&&host.reclaim_in_progress===false&&host.unknown_operations===0&&
    execution?.delegation_active===false&&['idle','revoked','reclaimed','exhausted'].includes(execution.control_status)&&
    !['starting','running','unknown'].includes(execution.status)&&execution.request_status==='idle_confirmed'&&execution.settlement?.status==='idle_confirmed'&&
    execution.readback_actual===true&&Number.isInteger(execution.actions_count)&&execution.readback_actions_count===execution.actions_count&&
    Array.isArray(execution.receipts)&&execution.receipts.every(receipt=>['confirmed','rejected'].includes(receipt.status)))manualControlRecoveryPending=false;
  const conversation=agentAdmission('conversation'),creation=agentAdmission('creation');
  renderModelChoices();$('game-host').value=host.host_url||'';
  $('agent-status').textContent=hostNames[host.status]||host.status||'状态待核对';$('agent-status').dataset.status=host.status;
  $('agent-detail').textContent=manualControlRecoveryPending?'收回或原行动结果尚未核对，连接恢复后先核对原回执，再继续行动。':host.error?.message||(host.reclaim_in_progress?'行动权已收回，原模型请求正在收尾。已确认的后果保留，可以亲手继续。':busy?'你的 Agent 正在读取、解释、创作或执行已授权目标。你可以继续亲手操作世界。':playing&&!conversation.allowed?conversation.reason:host.entered?'你的 Agent 已读取世界。普通提问只解释现场；明确委托目标后，它可以用同一角色行动。':'完成首次真实回应与现场读取后，即可进入。');
  if(!intentDirty&&typeof host.context?.intent==='string'&&document.activeElement!==$('host-intent'))$('host-intent').value=host.context.intent;
  $('intent-save').disabled=hostCommandBusy;
  applyAdmission('chat-send',conversation);applyAdmission('ask-selection',conversation);$('agent-cancel').hidden=!busy;$('agent-reconnect').hidden=!['disconnected','error','paused'].includes(host.status);$('agent-disconnect').disabled=host.status==='disconnected';
  const generationPending=['unknown','published_readback_pending'].includes(host.generation?.status);
  applyAdmission('generation-send',creation);$('generation-recover').hidden=!generationPending;$('generation-recover').disabled=busy;renderGeneration();
  $('generation-cancel').hidden=!busy||!['claiming','generating','correcting','publishing'].includes(host.generation?.status);
  const messages=host.messages||[];
  const lastReply=currentAssistantReply(host);
  const failed=host.status==='error';
  $('companion-update').hidden=!failed&&!busy&&!lastReply;
  $('companion-update').textContent=manualControlRecoveryPending?'原行动结果尚未核对；恢复连接后先核对原回执，再继续行动。':failed?'当前 Agent 请求未完成：'+previewMessage(host.error?.message||'回应暂不可用，请打开“目标与交谈”查看。'):busy?'你的 Agent 正在看现场。你可以继续走动与操作。':lastReply?'Agent 回应：'+previewMessage(lastReply.text):'';
  const signature=JSON.stringify([messages,host.status,host.completed,host.message_id,host.execution?.task_id,host.execution?.requests_used,host.session_id,host.response_session_id,host.reclaim_in_progress]);
  if(signature!==lastChatSignature){lastChatSignature=signature;$('agent-messages').replaceChildren(...messages.slice(-12).map(m=>{const el=node('div',undefined,'chat-message '+(m.role==='user'?'user':m.role==='event'?'event':'agent'));if(m.task_id)el.dataset.taskId=m.task_id;if(m.response_message_id)el.dataset.messageId=m.response_message_id;el.append(node('small',agentMessageLabel(m,host)),messageNode(m.text));return el;}));$('agent-messages').scrollTop=$('agent-messages').scrollHeight;}
  if(!messages.length&&host.text)$('agent-messages').replaceChildren(node('small','历史回复 · 请求来源未记录'),messageNode(host.text));
  $('auto-summary').textContent='普通操作与现场刷新不调用模型；提问、创作与明确委托使用你自己的模型连接。';
  const progress=(host.progress||[]).slice(-6);const entrySignature=JSON.stringify([host.status,progress,host.error]);
  if(entrySignature!==lastHostSignature){lastHostSignature=entrySignature;$('entry-progress').replaceChildren(...progress.map(p=>node('p',p.message||p.kind||'')));if(host.error)$('entry-progress').append(node('p',host.error.message,'error-text'));}
  renderExecution();
}
async function hostCommand(path,body,{refresh=false,interrupt=false}={}){
  if(hostCommandBusy&&!interrupt||!playing||!current())return false;
  const kind={prompt:'conversation',generate:'creation',execute:'goal'}[path]||(path==='execute-resume'?(Number(body?.additional_requests)>0?'extend':'resume'):null);
  if(kind){const admission=agentAdmission(kind);if(!admission.allowed){announce(admission.reason,true);return false;}}
  const epoch=session,command=++hostCommandEpoch;++hostReadEpoch;hostCommandBusy=true;hostCommandKind=path;renderHost();
  try{hostCommandFlight=client.connector(path,body);const value=await hostCommandFlight;if(epoch!==session||command!==hostCommandEpoch||!playing)return false;host=value;renderHost();if(refresh)await refreshWorld(true);return epoch===session&&playing;}
  catch(error){if(epoch!==session||command!==hostCommandEpoch||!playing)return false;host={...host,admission:null};renderHost();throw error;}
  finally{if(epoch===session&&command===hostCommandEpoch){hostCommandBusy=false;hostCommandKind=null;hostCommandFlight=null;renderHost();scheduleHost(250);}}
}
function renderExecution(){
  const execution=host?.execution||{},status=execution.status||'idle',active=execution.delegation_active===true,busy=['starting','running'].includes(status),unknown=manualControlRecoveryPending||status==='unknown'||Number(host?.unknown_operations)>0||(execution.receipts||[]).some(receipt=>receipt.status==='unknown');
  const goalAdmission=agentAdmission('goal'),resumeAdmission=agentAdmission('resume'),extendAdmission=agentAdmission('extend');
  const goalResult=execution.goal_result||{},goalClaimed=status==='completed'&&!!execution.task_id&&goalResult.status==='completed'&&goalResult.completion_source==='agent_claim';
  const labels={idle:'尚未委托',starting:'正在授权',running:'正在执行目标',completed:'Agent报告完成',result_unconfirmed:'目标结果未确认',paused:'目标已暂停',blocked:'目标受阻',error:'执行受阻',unknown:'结果待核对',budget_exhausted:'目标额度已用完'};
  const displayedStatus=unknown?'unknown':status==='completed'&&!goalClaimed?'result_unconfirmed':status,requestLabels={idle:'尚未开始',preparing:'正在读取现场',summarizing:'正在整理会话历史',summary_failed:'会话历史整理失败',running:'推理与行动中',returned:'这一段请求已返回',timed_out:'这一段请求超时',settling:'正在核对原结果',idle_confirmed:'原请求已停止并核对',unsettled:'原请求状态待核对'};
  $('execution-status').textContent=labels[displayedStatus]||displayedStatus;$('execution-status').dataset.status=displayedStatus;
  if(!goalDirty&&typeof execution.goal==='string'&&document.activeElement!==$('execution-goal'))$('execution-goal').value=execution.goal;
  $('execution-current-goal').textContent=execution.goal?'委托目标：'+execution.goal+'（'+(execution.interaction_mode==='play'?'游玩体验':'创作与复杂任务')+'）':'明确给出目标后才允许 Agent 行动。保存想法或普通提问不会授权。';
  const count=Number.isInteger(execution.actions_count)?String(execution.actions_count):'待读取',limit=Number.isInteger(execution.max_actions)?String(execution.max_actions):'待读取';
  const requests=Number.isInteger(execution.requests_used)?String(execution.requests_used):'待读取',requestLimit=Number.isInteger(execution.max_requests)?String(execution.max_requests):'待读取';
  $('execution-budget').textContent=execution.goal?'世界动作 '+count+' / '+limit+(execution.task_id?' · 这个目标模型请求 '+requests+' / '+requestLimit:' · 原请求记录，尚无目标接续额度'):'';
  $('execution-request').textContent=execution.goal&&execution.task_id?'当前请求：'+(requestLabels[execution.request_status]||execution.request_status||'待读取')+(execution.budget?' · 每段建议 '+execution.budget.max_steps+' 个推理步骤、最多 '+execution.budget.max_tool_calls+' 次世界读取或行动':''):'';
  const recovery=execution.history_recovery||{},recoveryLabels={not_started:recovery.requested?'尚未开始整理会话历史':'未请求整理会话历史',running:'正在整理会话历史',completed:'会话历史整理完成；目标仍以行动与核对结果判断',failed:'会话历史整理失败；本目标不会自动重试整理',uncertain:'会话历史整理结果待核对；本目标不会自动重试整理'};
  $('execution-history-recovery').hidden=!execution.task_id||!recovery.status;
  $('execution-history-recovery').textContent=[recoveryLabels[recovery.status]||recovery.status,recovery.attempts>0?'摘要请求已计入这个目标的模型请求总额度，原记录保留。':'',recovery.error?.message].filter(value=>typeof value==='string'&&value).join('\n');
  const detail=unknown?'原行动结果未确认。先核对原回执，才可接续或开始新目标；核对不会自动继续行动。':goalClaimed?'Agent已结束本次委托。这是它对目标的判断；程序确认的具体世界结果另列。':status==='completed'?'原执行记录已结束，目标结果未确认。已经保存的行动保留；旧完成记录没有验证来源，不能将请求结束视为成功。':active?'行动授权仍有效。Agent 可在总额度内继续；收回或亲手行动会停止后续行动，已保存的结果保留。':resumeAdmission.allowed?'当前没有行动授权。点击“接续这个目标”才再次授权；它会读取最新现场，继续原目标与剩余额度。':execution.goal?'当前没有行动授权。已保存的行动保留；请求结束或额度用完不代表目标完成。':'人可以直接玩，也可以在这里明确授权 Agent 帮你实现目标。';
  $('execution-detail').textContent=[execution.stop_reason,execution.error?.message,detail,goalAdmission.reason].filter((value,index,values)=>typeof value==='string'&&value&&values.indexOf(value)===index).join('\n');
  const progress=execution.progress||{},progressNodes=[];
  for(const line of executionTaskSummaryLines(execution))progressNodes.push(node('p',line,'small'));
  if(goalClaimed&&typeof goalResult.summary==='string'&&goalResult.summary)progressNodes.push(node('p','Agent完成声明：'+goalResult.summary,'small'));
  else if(typeof progress.summary==='string'&&progress.summary)progressNodes.push(node('p','最近发现与行动：'+progress.summary,'small'));
  if(typeof progress.next_intent==='string'&&progress.next_intent)progressNodes.push(node('p','下一步打算：'+progress.next_intent,'small'));
  const worldResults=(Array.isArray(execution.world_results)?execution.world_results:Array.isArray(goalResult.world_results)?goalResult.world_results:[]).filter(result=>result?.kind==='world_contract'&&result.entity_id===execution.context_entity_id);
  const contractLabels={unoffered:'尚未提出条款',awaiting_acceptance:'等待双方接受',active:'进行中',complete:'已完成',failed:'已失败'};
  const functionLabels={'grove.move':'走动','grove.interact':'操作物件','grove.rule_action':'使用物件的做法','grove.build':'搭建','grove.publish':'留下共同内容','grove.control_grant':'授权目标','grove.control_revoke':'接回角色'};
  for(const result of worldResults){
    progressNodes.push(node('p','所选合同事实：'+(result.entity_name||'这份合同')+' · '+(contractLabels[result.server_status]||result.server_status||'状态待核对'),'small'));
    progressNodes.push(node('p','累计交付 '+result.delivered_u+' 单位 / 约定 '+result.quantity_u+' 单位','small'));
    const verification=result.verification?.status;
    if(verification==='reference_mismatch'){
      progressNodes.push(node('p','当前合同与本目标起始时的物件、材料或条款不匹配，当前结算不作为原约定的结果。','small'));
    }else if(verification==='verified_complete')progressNodes.push(node('p','服务器已确认固定引用的合同条款完成。它只证明该合同结算，不覆盖取回收益等附加目标，也不证明整段委托。','small'));
    else if(verification==='not_complete')progressNodes.push(node('p','这是固定引用的合同条款状态；目标仍按你的实际意图核对。','small'));
    else progressNodes.push(node('p','这个目标尚无已核对的起始合同引用；以上只是当前读取到的合同事实。','small'));
  }
  const evidence=Array.isArray(progress.evidence)?progress.evidence:[],lastConfirmed=evidence[evidence.length-1]||[...(execution.receipts||[])].reverse().find(receipt=>['committed','confirmed'].includes(receipt.status));
  if(lastConfirmed)progressNodes.push(node('p','最后已确认行动：'+(functionLabels[lastConfirmed.function]||'世界行动')+(Number.isInteger(lastConfirmed.commit_seq)?' · 记录 '+lastConfirmed.commit_seq:''),'small'));
  $('execution-progress').replaceChildren(...progressNodes);$('execution-progress').hidden=progressNodes.length===0;
  const receipts=Array.isArray(execution.receipts)?execution.receipts:[],receiptLabels={committed:'已保存',confirmed:'已核对',rejected:'已拒绝',unknown:'待核对'};
  $('execution-actions').replaceChildren(...receipts.slice(-8).map(receipt=>{
    const row=node('div',undefined,'execution-receipt');row.append(node('strong',(functionLabels[receipt.function]||'世界行动')+' · '+(receiptLabels[receipt.status]||receipt.status||'状态未读')));
    for(const line of receiptOutcomeLines(receipt))row.append(node('p',line,'small'));
    const outcome=receipt.confirmed_outcome;
    if(['committed','confirmed'].includes(receipt.status)&&outcome?.kind==='server_receipt'){
      for(const line of objectFactLines({entity:outcome.entity}))row.append(node('p',line,'small'));
      row.append(node('p','以上是这次提交时的程序确认结果'+(Number.isInteger(receipt.commit_seq)?' · 记录 '+receipt.commit_seq:'')+'，不代表最新现场。','small'));
    }
    return row;
  }));
  if(receipts.length>8)$('execution-actions').append(node('p','显示最近 8 个回执，共 '+receipts.length+' 个；它们记录行动，不替代当前现场。','small'));
  for(const line of actionTargetReadbackLines(execution))$('execution-actions').append(node('p',line,'small'));
  for(const line of executionReadbackLines(execution))$('execution-actions').append(node('p',line,'small'));
  const readbacks=[execution.readback?.selected_inspection,...(execution.readback?.action_targets||[])],shownTargets=new Set();
  for(const entry of readbacks){const request=entry?.client_read_request,id=request?.arguments?.entity_id;if(request?.function_id!=='grove.client_inspect'||!id||shownTargets.has(id))continue;shownTargets.add(id);$('execution-actions').append(button('重新读取当前详情 · '+(entry.name||entityById(id)?.name||'原任务对象'),()=>run(()=>select(id,false,true)),{secondary:true}));}
  applyAdmission('execution-start',goalAdmission);
  const limits=executionLimits(),requestControl=$('execution-max-requests'),additionalControl=$('execution-additional-requests'),additionalLimit=limits?Math.max(0,Math.min(limits.additional_requests,limits.max_requests-(execution.max_requests||0))):0;
  if(limits){if(!requestControl.dataset.limitsInitialized){requestControl.value=String(limits.default_requests);requestControl.dataset.limitsInitialized='true';}requestControl.max=String(limits.max_requests);$('execution-max-actions').max=String(limits.max_actions);$('execution-budget-help').textContent='单目标最多 '+limits.max_requests+' 次模型请求、'+limits.max_actions+' 次实际动作。接段自动继续，额度耗尽才需要你选择接续。';}
  additionalControl.max=String(additionalLimit);applyAdmission('execution-additional-requests',extendAdmission);
  if(Number(additionalControl.value)>additionalLimit)additionalControl.value=String(additionalLimit);
  $('execution-resume').hidden=(execution.resume_allowed!==true&&execution.extension_allowed!==true)||!execution.task_id||unknown;
  applyAdmission('execution-resume',continuationAdmission());
  $('execution-reclaim').disabled=executionReclaimBusy||!(active||busy||unknown);
  $('execution-reclaim').textContent=executionReclaimBusy?'正在收回…':'收回行动权';
  $('execution-recover').hidden=!unknown;$('execution-recover').disabled=hostCommandBusy||executionReclaimBusy;
  const summary=unknown?'有原行动待核对':active?'Agent 正在为你行动 · 随时可接回':executionReclaimBusy?'正在接回角色…':'你正在亲手操作';
  $('execution-summary').textContent=summary;$('execution-reclaim-quick').hidden=!(active||busy||unknown);$('execution-reclaim-quick').disabled=executionReclaimBusy;
}
async function reclaimExecution(){
  if(executionReclaimBusy||!playing||!current())return;
  const epoch=session;stopWalking();executionReclaimBusy=true;renderHost();
  try{if(await takeManualControl(()=>epoch===session&&playing&&current()))announce('行动权已收回。已保存的行动保留，你可以亲手接着玩。');}
  finally{if(epoch===session){executionReclaimBusy=false;if(playing)renderHost();}}
}
async function restartHost(){
  return hostCommand('start',{token:client.token,public_key:key.public_key,name:point()?.name||prefs().name,...($('game-model').value.trim()?{model:$('game-model').value.trim()}:{})});
}
function renderModelChoices(){
  for(const id of ['entry-model','game-model']){
    const select=$(id),selected=select.value,models=host.available_models||[];
    for(const model of models)if(![...select.options].some(o=>o.value===model)){const option=node('option',model.split('/').slice(1).join('/'));option.value=model;select.append(option);}
    if(id==='game-model'&&!select.dataset.populated&&host.model){select.value=host.model;select.dataset.populated='true';}else if(selected)select.value=selected;
  }
}
function renderGeneration(){
  const admission=agentAdmission('creation'),generation=host?.generation,signature=JSON.stringify([generation||null,admission,(world?.processes||[]).map(p=>[p.id,p.status,p.start_tick<=finite(world?.clock?.sequence)])]);if(signature===lastGenerationSignature)return;lastGenerationSignature=signature;
  const detail=text=>{$('generation-detail').textContent=[text,admission.reason].filter((value,index,values)=>value&&values.indexOf(value)===index).join('\n');};
  const status=generation?.status||'idle';$('generation-status').dataset.status=status;$('generation-result').replaceChildren();
  if(!generation){$('generation-status').textContent='尚未开始';detail('使用你自己的模型连接；完成后直接公开。');return;}
  if(!generation.published){
    const phases={idle:['尚未开始','使用你自己的模型连接；完成后直接公开。'],claiming:['读取附近','正在读取这片地方能创作的机会。'],generating:['正在创作','你的 Agent 正在创作；你仍可以亲手走动与操作。'],correcting:['正在调整','正在按世界规则调整内容；你仍可以亲手操作。'],publishing:['正在发表','正在交给共同世界保存，等待实际结果。'],unavailable:['暂时没有机会','附近暂时没有可用的创作机会。继续走动、探索，之后再试。'],error:['创作受阻',generation.error?.message||host.error?.message||'这一轮没有确认发表。可以调整想法后再试。'],unknown:['结果待核对','发表回应中断，原操作保留。请先核对发表结果，避免重复发表。'],cancelled:['已中止','这一轮没有发表内容，已中止。可以继续亲手操作世界。']};
    const phase=phases[status]||['状态待核对','请重新连接，读取这轮创作的实际状态。'];$('generation-status').textContent=phase[0];detail(phase[1]);return;
  }
  const confirmed=generation.readback?.receipt_confirmed&&generation.readback?.viewport_read;
  $('generation-status').textContent=confirmed?'已发表并核对':'已发表 · 待读回';detail(confirmed?'已从共同世界读取发表结果。这里保留发表记录；当前内容物、数量与状态以地图物件详情为准。':'世界已确认发表，现场读回暂未完成。请核对原发表结果，已保存的内容不会重复发表。');
  const entities=Array.isArray(generation.entities)?generation.entities:[];
  for(const entity of entities){if(typeof entity!=='object')continue;const location=Number.isFinite(entity.x)&&Number.isFinite(entity.y)?' · '+entity.x+'，'+entity.y:'';$('generation-result').append(node('p',(entity.name||'共同物件')+location+' · 已发表；当前事实需重新读取'));if(entity.authored_description)$('generation-result').append(node('p','发表时的说明：'+entity.authored_description,'small'));}
  const processes=Array.isArray(generation.process)?generation.process:generation.process?[generation.process]:[];
  for(const process of processes){if(typeof process!=='object')continue;const currentProcess=world?.processes?.find(p=>p.id===process.id),label=currentProcess?.label||process.label||'世界过程';$('generation-result').append(node('p',label+' · '+(currentProcess?'本次已读过程标记：'+currentProcess.status:'已保存，当前事实需重新读取')));}
}
async function refreshWorld(reset=false,options={}){
  if(!playing||!current())return;
  if(refreshFlight&&!reset)return refreshFlight;
  const epoch=session,reading=++readEpoch;
  const task=(async()=>{
    let readAccepted=false;
    try{
      const view=await client.viewport({reset,...options});if(epoch!==session||reading!==readEpoch)return false;
      const value=client.unpackView(view);if(value?.player){acceptWorld(value);readAccepted=true;}lastWorldRead=Date.now();$('map-loading').hidden=true;$('reconnect-world').hidden=true;$('world-status').textContent='共同世界 · 已连接';
      const changes=await client.readChanges(streamCursor);if(epoch!==session||reading!==readEpoch)return;
      const historical=!streamCursor;streamCursor=changes.cursor;ledger.ingest(changes.events,{historical});renderActivity();
      return readAccepted;
    }catch(error){if(epoch!==session||reading!==readEpoch)return;
      if(['StreamResetRequired','CursorExpired','CursorInvalid'].includes(error.code)){streamCursor=null;return readAccepted;}
      $('world-status').textContent='现场连接中断';$('freshness').textContent='上次读取 '+(lastWorldRead?new Date(lastWorldRead).toLocaleTimeString():'尚未成功');if(error.status===401||error.status===403)fail(error);else $('movement-feedback').textContent='世界暂不可达；显示上次现场。刷新后继续。';
      worldFresh=false;mechanicalMotion.reset();
      return readAccepted;
    }
  })();refreshFlight=task;try{return await task;}finally{if(refreshFlight===task)refreshFlight=null;}
}
function scheduleWorld(delay=1200){clearTimeout(worldTimer);if(playing&&current())worldTimer=setTimeout(async()=>{try{if(!actionBusy&&!document.hidden)await refreshWorld();}finally{scheduleWorld();}},delay);}
function acceptWorld(value){
  if(lastWorldRead&&Date.now()-lastWorldRead>8000)mechanicalMotion.reset();
  if($('movement-feedback').textContent==='世界暂不可达；显示上次现场。刷新后继续。')$('movement-feedback').textContent='已重新读取现场，可以继续。';
  if(client.inventoryWasReset){inventoryPageCursors=[null];client.inventoryWasReset=false;announce('背包清单已改变，已重新读取第一页。原操作与身份保留。');}
  const previous=world?.player;world=value;
  worldFresh=true;lastWorldRead=Date.now();mechanicalMotion.accept(value.entities,value.clock,performance.now());
  activeRainChunks=new Set(value.weather?.rain_chunks||[]);
  if(previous&&(previous.x!==value.player.x||previous.y!==value.player.y))motion={from:{x:previous.x,y:previous.y},to:{x:value.player.x,y:value.player.y},start:performance.now(),duration:160};
  if(!previous){camera.x=value.player.x;camera.y=value.player.y;}
  $('player-name').textContent=value.player.name;$('position').textContent='坐标 '+value.player.x+'，'+value.player.y;
  $('freshness').textContent=value.clock?.frame_status==='open'?'刚刚读取 · 世界正在结算':'刚刚读取';$('map-scale').textContent='只显示身边的区域'+(value.truncated?' · 更多物件需靠近查看':'');
  $('weather').textContent=value.weather?.label||'天气尚未读取';
  if(selectedId){
    const projected=ground().find(entity=>entity.id===selectedId)||inventory().find(entity=>entity.id===selectedId)||(value.public_content?.anchors||[]).find(anchor=>anchor.entity_id===selectedId)?.entity;
    const inaccessible=!projected&&[403,404].includes(inspectionErrorStatus);
    if(!inaccessible&&inspectionFlight?.id!==selectedId&&(!projected||inspection?.entity?.version!==projected.version||previous&&(previous.x!==value.player.x||previous.y!==value.player.y)||previewVersionsChanged(inspection,[...ground(),...inventory()],value.player)||ruleSnapshotChanged(inspection,value.clock))){const captured=selectedId;select(captured,selectedFromBag,true).catch(fail);}
  }
  renderInventory();renderSelection();renderActivity();renderPublicContent();renderPublicWorks();renderPending();
}
function renderPending(){
  if(!identity)return;let pending;try{pending=client.pending();}catch{return;}
  for(const id of ['move-up','move-left','move-down','move-right'])$(id).disabled=actionBusy||executionReclaimBusy||operationRecoveryBusy||!!pending||!playing||!point();
  $('pending').hidden=!pending;
  $('recover-operation').disabled=operationRecoveryBusy||actionBusy;$('release-operation').disabled=operationRecoveryBusy||actionBusy;
  if(pending){$('pending-detail').textContent=pending.rejection?'服务器拒绝了原操作：'+pending.rejection.message:'回应中断，结果尚未核对。操作 '+pending.body.operation_id.slice(0,8)+' 保留；不会自动发出替代操作。';$('release-operation').hidden=!pending.rejection;$('recover-operation').hidden=!!pending.rejection;}
  if(playing&&host)renderExecution();
  $('rule-catalog-result').querySelector('.fabrication-editor')?.dispatchEvent(new Event('refresh-fabrication-state'));
}
async function recoverWorldOperation(release=false){
  if(operationRecoveryBusy||actionBusy||!playing||!client.pending())return;
  stopWalking();const epoch=session,original=client.pending();operationRecoveryBusy=true;renderPending();
  try{const result=release?await client.releaseRejected():await client.recover();if(epoch!==session||!playing)return;if(result?.rejected){renderPending();return;}$('error').hidden=true;await refreshWorld(true);if(epoch!==session||!playing)return;if(!release&&original.name==='grove.fabricate')await clearRecoveredFabricationDraft(original.body.arguments);announce(release?'已读取当前状态，可以重新选择行动。':'原操作结果已核对。');}
  catch(error){if(epoch===session&&playing)throw error;}
  finally{if(epoch===session){operationRecoveryBusy=false;renderPending();renderSelection();}}
}
async function takeManualControl(isCurrent){
  // A goal admission returns before model inference. Settle that short response
  // first so a late /execute cannot authorize after the person's reclaim.
  if(['execute','execute-resume'].includes(hostCommandKind)&&hostCommandFlight){
    const admitted=await hostCommandFlight;if(!isCurrent())return false;host=admitted;
  }
  const execution=host?.execution;
  const unknown=execution?.status==='unknown'||(execution?.receipts||[]).some(receipt=>receipt.status==='unknown')||Number(host?.unknown_operations)>0;
  if(execution?.delegation_active!==true&&!['starting','running'].includes(execution?.status)&&!manualControlRecoveryPending){
    if(unknown){announce('仍有原操作结果待核对，先核对后继续。',true);return false;}
    return true;
  }
  $('movement-feedback').textContent='接回你的角色，已保存的后果保留…';
  const epoch=session,command=++hostCommandEpoch;++hostReadEpoch;hostCommandBusy=true;renderHost();
  try{
    let state,receiptsChecked=false;
    try{state=await client.connector('reclaim',{background:true});receiptsChecked=true;}
    catch(error){
      // Revoke future world writes, but a cached zero is not a receipt read.
      manualControlRecoveryPending=true;
      if(error.status===409)throw error;
      if(execution?.delegation_active===true||['starting','running'].includes(execution?.status))await client.mutate('grove.control_revoke',{}, {},isCurrent);
      state={...host,admission:null,execution:{...execution,delegation_active:false,stop_reason:'后续行动权已收回；原行动结果尚未核对，连接恢复后先核对原回执。'}};
    }
    if(!isCurrent()||command!==hostCommandEpoch)return false;
    if(receiptsChecked)manualControlRecoveryPending=false;
    host=state;renderHost();await refreshWorld(true);
    if(manualControlRecoveryPending||state.execution?.delegation_active!==false||state.execution?.status==='unknown'||Number(state.unknown_operations)>0||(state.execution?.receipts||[]).some(receipt=>receipt.status==='unknown')){announce(manualControlRecoveryPending?'后续行动权已收回；连接恢复后先核对原回执，再继续行动。':state.execution?.delegation_active!==false?'行动权尚未确认收回，先恢复连接并核对后继续。':'后续行动权已收回；仍有原操作结果待核对，先核对后继续。',true);return false;}
    return isCurrent();
  }finally{if(epoch===session&&command===hostCommandEpoch){hostCommandBusy=false;hostCommandKind=null;hostCommandFlight=null;renderHost();scheduleHost(250);}}
}
async function perform(name,args,label){
  if(!playing||!current()||!point()||actionBusy||executionReclaimBusy)return false;
  const epoch=session,isCurrent=()=>epoch===session&&playing&&current(),initiator=document.activeElement;
  if(client.pending()){announce('先核对上一个操作，再继续。');renderPending();return false;}
  actionBusy=true;++readEpoch;lastAction=label;$('error').hidden=true;$('movement-feedback').textContent=label+'，等待世界保存…';renderPending();renderSelection();
  try{
    if(!await takeManualControl(isCurrent))return false;
    const prepared=typeof args==='function'?await args(isCurrent):args;
    if(!isCurrent()||prepared==null)return false;
    const result=await client.mutate(name,prepared,{},isCurrent);if(!isCurrent())return false;
    await refreshWorld(true);
    if(!isCurrent())return false;
    const changes=result.changes?.map(c=>displayWorldText(typeof c==='string'?c:c.summary||'')).filter(Boolean).join(' · ');
    if(changes)announce(changes);
    $('movement-feedback').textContent=changes||label+'已由世界保存。';
    if(selectedId)await select(selectedId,selectedFromBag,true);
    if(!isCurrent())return false;
    if(initiator?.tagName==='BUTTON'&&[initiator,document.body].includes(document.activeElement))canvas.focus({preventScroll:true});
    scheduleWorld(200);return true;
  }catch(error){
    if(!isCurrent())return false;
    held=null;walkPath=[];pathTarget=null;
    if(client.pending()?.rejection){const rejection=await client.releaseRejected();if(!isCurrent())return false;await refreshWorld(true);if(!isCurrent())return false;announce((rejection.code==='StateConflict'?'物件或位置刚被改变，已读取最新现场。':'这次行动没有保存。')+' '+rejection.message+' 请按当前状态重新选择。',true);}
    else fail(error);
    $('movement-feedback').textContent=client.pending()?'原操作需要核对，走动已暂停。':'行动未保存；可以重新选择。';return false;
  }finally{if(epoch===session)actionBusy=false;if(isCurrent()){renderPending();renderSelection();}}
}
async function move(dx,dy){return perform('grove.move',()=>({dx,dy,expected_player_version:point().version}),'走动');}
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
  return world?.navigation?.cells?.find(cell=>cell.x===x&&cell.y===y)?.passable===true;
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
  if(!current()||!world)return null;if(selectedId!==id){rememberActionDrafts();activeActionKey=null;}selectedId=id;selectedTile=null;selectedFromBag=fromBag;if(fromBag&&!force)document.querySelector('.inventory-card').open=false;if(!force)openContext();const epoch=++inspectEpoch,sessionEpoch=session;
  if(!force&&inspection?.entity?.id===id&&!inspection.reading&&!inspectionError){renderSelection();return inspection;}
  const previousEntity=entityById(id),flight={id};inspectionFlight=flight;inspection=previousEntity?{entity:previousEntity,reading:true}:null;inspectionError=null;inspectionErrorStatus=null;renderSelection();
  try{const result=await client.query('grove.client_inspect',{entity_id:id});if(epoch!==inspectEpoch||sessionEpoch!==session||selectedId!==id)return null;inspection=result;renderSelection();return result;}
  catch(error){if(epoch===inspectEpoch&&sessionEpoch===session&&selectedId===id){inspectionError=error.message||'这件物件暂不可查看。';inspectionErrorStatus=error.status;renderSelection();}return null;}
  finally{if(inspectionFlight===flight)inspectionFlight=null;}
}
function renderInventory(){
  const items=inventory().filter(e=>e.id!==point()?.backpack_id);
  const signature=JSON.stringify([selectedId,inventory(),world.inventory_total,world.inventory_next,inventoryPageCursors,inventoryPageBusy,actionBusy]);if(signature===lastInventorySignature)return;lastInventorySignature=signature;
  const total=Number.isInteger(world.inventory_total)?Math.max(0,world.inventory_total-1):null;
  $('inventory-capacity').textContent=total!==null?total+' 件随身物件':'本页 '+items.length+' 件';
  $('supply-capacity').textContent=Number.isInteger(world.inventory_total)?world.inventory_total+' 对象（含包）':'本页 '+inventory().length+' 对象';
  const quantities=new Map();
  for(const item of items)for(const lot of bodyQuantities(item)){const value=quantities.get(lot.material)||{name:lot.material_name,quantity:0};value.quantity+=lot.quantity;quantities.set(lot.material,value);}
  $('material-quantities').replaceChildren(...[...quantities].map(([material,value])=>{const row=node('div',undefined,'material-count');row.append(node('span',value.name||materialLabel(material)),node('strong','本页本体 '+value.quantity+' 单位'));return row;}));
  if(!quantities.size)$('material-quantities').append(node('p','本页暂无可见材料批次','small'));
  $('material-caption').textContent='本页材料数量来自已读批次；操作资格与可投入数量由所选行动的服务器菜单提供。';
  const vessels=items.filter(e=>summaryFact(e,'water_capacity'));
  const totalFact=id=>vessels.some(e=>summaryFact(e,id)?.availability!=='known')?'?':vessels.reduce((n,e)=>n+summaryFact(e,id).value,0);
  $('supply-water').textContent=totalFact('water')+' / '+totalFact('water_capacity');
  const uses=items.map(e=>summaryFact(e,'uses')).filter(Boolean);
  $('supply-tools').textContent=uses.some(row=>row.availability!=='known')?'点火次数待读取':'点火 '+uses.reduce((n,row)=>n+row.value,0)+' 次';
  $('inventory-list').replaceChildren(...items.map(e=>{const el=button('',()=>run(()=>select(e.id,true)),{secondary:true});el.className='inventory-item'+(e.id===selectedId?' selected':'');el.append(node('span',entityGlyph(e),'item-icon'),node('span',e.name),node('small',stateLine(e)));return el;}));
  $('inventory-water').textContent=vessels.map(e=>e.name+'：'+factRowText(summaryFact(e,'water'))+' / '+factRowText(summaryFact(e,'water_capacity'))).join(' · ')||'本页没有已读存水事实。';
  $('inventory-page-note').textContent='第 '+inventoryPageCursors.length+' 页 · 本页 '+items.length+' 件'+(total!==null?' · 共 '+total+' 件':'')+'。本页清单不是全部库存。';
  $('inventory-first').disabled=inventoryPageBusy||actionBusy||!client.inventoryAfter;$('inventory-previous').disabled=inventoryPageBusy||actionBusy||inventoryPageCursors.length<2;$('inventory-next').disabled=inventoryPageBusy||actionBusy||!world.inventory_next;
}
async function changeInventoryPage(direction){
  if(inventoryPageBusy||actionBusy||!playing)return;stopWalking();const epoch=session,previous=[...inventoryPageCursors],previousAfter=client.inventoryAfter;
  const cursors=direction==='first'?[null]:direction==='previous'?inventoryPageCursors.slice(0,-1):world?.inventory_next?[...inventoryPageCursors,world.inventory_next]:null;if(!cursors?.length)return;
  inventoryPageCursors=cursors;inventoryPageBusy=true;lastInventorySignature='';renderInventory();
  try{const accepted=await refreshWorld(true,{inventory_after:cursors.at(-1)});if(epoch!==session||!playing)return;if(accepted===false){inventoryPageCursors=previous;client.inventoryAfter=previousAfter;client.world.view=null;}}
  finally{if(epoch===session){inventoryPageBusy=false;lastInventorySignature='';renderInventory();}}
}
function stat(label,value){const el=node('div');el.append(node('small',label),node('strong',String(value)));return el;}
function stateLine(entity){return entitySummary(entity,point()?.backpack_id);}
function entityGlyph(entity){return genericGlyph(entity);}
function selectTile(tile){
  rememberActionDrafts();activeActionKey=null;stopWalking();selectedId=null;selectedFromBag=false;selectedTile={x:tile.x,y:tile.y};inspection=null;inspectionError=null;inspectionErrorStatus=null;inspectionFlight=null;
  lastSelectionSignature='';openContext();renderSelection();renderInventory();run(()=>readSelectedTile());
}
async function readSelectedTile(){
  if(!selectedTile||!current())return;
  const tile={...selectedTile},epoch=++inspectEpoch,sessionEpoch=session;
  inspection={reading:true};inspectionError=null;renderSelection();
  try{const result=await client.query('grove.client_inspect',{tile});if(epoch!==inspectEpoch||sessionEpoch!==session||!selectedTile||tile.x!==selectedTile.x||tile.y!==selectedTile.y)return;inspection=result;}
  catch(error){if(epoch===inspectEpoch&&sessionEpoch===session)inspectionError=error.message;}
  finally{if(epoch===inspectEpoch&&sessionEpoch===session){lastSelectionSignature='';renderSelection();}}
}
function renderTileSelection(){
  const tile=selectedTile,observed=inspection?.tile,d=Math.abs(tile.x-point().x)+Math.abs(tile.y-point().y),disabled=actionBusy||!!client.pending()||inspection?.reading;
  const preserved=$('selection-actions').querySelector('.descriptor-action');
  const keep=preserved?.dataset.actionKey===activeActionKey&&preserved.dataset.entityId==='tile:'+tile.x+','+tile.y?preserved:null;
  if(keep)keep.remove();
  $('selection-name').textContent=observed?.name||'正在读取地面';$('selection-distance').textContent=d===0?'脚下':d+' 格';renderTargetChoices(tile);
  $('selection-description').textContent=inspectionError||'地面、通行与建设资格来自服务器本次读取。';
  $('selection-stats').replaceChildren(stat('地面',observed?.name||'待读取'),stat('当前通行',observed?observed.passable?'可通行':'不可通行':'待读取'));
  for(const id of ['selection-history','selection-content','selection-effects','selection-actions'])$(id).replaceChildren();
  $('selection-actions').append(button(d?'沿路走到这里':'已经在这里',()=>goTo(tile.x,tile.y),{disabled:disabled||!d||!walkable(tile.x,tile.y),secondary:true}),button('重新读取这里',()=>run(readSelectedTile),{disabled:actionBusy||inspection?.reading,secondary:true}));
  for(const entity of inspection?.entities||[])$('selection-content').append(button('查看 '+entity.name,()=>run(()=>select(entity.id)),{secondary:true}));
  const descriptors=actionDescriptors(inspection),active=descriptors.find(row=>actionKey(row)===activeActionKey);
  if(keep){$('selection-actions').append(keep);keep.dispatchEvent(new Event('refresh-candidate-preview'));}
  else if(active){$('selection-actions').append(button('返回其他做法',()=>{rememberActionDrafts();activeActionKey=null;lastSelectionSignature='';renderSelection();},{secondary:true}));renderDescriptorEditor(active,{id:'tile:'+tile.x+','+tile.y,name:observed?.name||'所选地面'},disabled);}
  else for(const descriptor of descriptors)renderDescriptorAction(descriptor,{id:'tile:'+tile.x+','+tile.y,name:observed?.name||'所选地面'},disabled);
  $('selection-note').textContent='建设输入按服务器提供的候选选择；未选择的可选投入保留服务器默认行为。';
}
function renderSelection(){
  const entity=selectedId?(inspection?.entity?.id===selectedId?inspection.entity:entityById(selectedId)):null;
  const signature=JSON.stringify([selectedId,selectedTile,activeActionKey,entity,inspection,inspectionError,point()?.version,actionBusy,!!identity&&client.pending(),definitionReadBusy,definitionReadValue,definitionReadError]);
  if(signature===lastSelectionSignature)return;
  rememberActionDrafts();lastSelectionSignature=signature;$('selection-actions').dataset.entityId=selectedId||'';
  if(selectedTile){renderTileSelection();return;}
  const preserved=$('selection-actions').querySelector('.descriptor-action');
  const keep=preserved?.dataset.actionKey===activeActionKey&&preserved.dataset.entityId===selectedId?preserved:null;
  if(keep)keep.remove();
  for(const id of ['selection-stats','selection-actions','selection-content','selection-effects','selection-history'])$(id).replaceChildren();
  if(!entity){$('selection-name').textContent=selectedId?'正在读取所选物件':'选择物件或位置';$('selection-description').textContent=inspectionError||'点选地图或背包中的物件，读取事实与可用行动。';$('selection-distance').textContent='';$('selection-note').textContent=selectedId?'取得当前操作资格后才能行动。':'';if(selectedId)$('selection-actions').append(button('重新读取',()=>run(()=>select(selectedId,selectedFromBag,true)),{disabled:actionBusy,secondary:true}));return;}
  const d=distance(entity),own=inventory().some(e=>e.id===entity.id),waiting=inspection?.reading||!inspection?.context,disabled=actionBusy||!!client.pending()||waiting;
  $('selection-name').textContent=entity.name;$('selection-description').textContent=(waiting||inspectionError?'上次已读状态：':'本次已读状态：')+stateLine(entity);$('selection-distance').textContent=own?'随身':d===0?'脚下':Number.isFinite(d)?d+' 格':'位置未读';
  const clock=inspection?.context?.observation_clock;
  $('selection-note').textContent=(Number.isInteger(clock?.sequence)?'读取世界时刻 '+clock.sequence+' · 水位 '+inspection.context.snapshot_seq+'。':'地图只提供发现摘要。')+' 操作前核对原选择，服务器结算真实后果。';
  $('selection-stats').replaceChildren(...simulationStats(entity).slice(0,6).map(([label,value])=>stat(label,value)));
  const location=entityLocation(entity);renderTargetChoices(location);
  if(!own&&d>1&&Number.isFinite(location.x)&&Number.isFinite(location.y))$('selection-actions').append(button('走近这件物件',()=>goTo(location.x,location.y,true),{disabled:actionBusy,secondary:true}));
  if(!own&&Number.isFinite(location.x)&&Number.isFinite(location.y))$('selection-actions').append(button('选择这里的地面',()=>selectTile(location),{secondary:true}));
  if(waiting||inspectionError){$('selection-actions').append(node('p',inspectionError||'正在读取当前事实与行动…','small'));if(inspectionError)$('selection-actions').append(button('重新读取',()=>run(()=>select(entity.id,selectedFromBag,true)),{disabled:actionBusy,secondary:true}));}
  else {
    const descriptors=actionDescriptors(inspection),active=descriptors.find(row=>actionKey(row)===activeActionKey&&row.available===true);
    if(keep){$('selection-actions').append(keep);keep.dispatchEvent(new Event('refresh-candidate-preview'));}
    else if(active){$('selection-actions').append(button('返回其他做法',()=>{rememberActionDrafts();activeActionKey=null;lastSelectionSignature='';renderSelection();},{secondary:true}));renderDescriptorEditor(active,entity,disabled);}
    else for(const descriptor of descriptors.filter(row=>row.available===true))renderDescriptorAction(descriptor,entity,disabled);
    const unavailable=descriptors.filter(row=>row.available!==true);
    if(unavailable.length){const detail=node('details');detail.append(node('summary','暂时不能做 · '+unavailable.length+' 项'));for(const descriptor of unavailable)detail.append(node('p',descriptorLabel(descriptor)+'：'+(descriptor.reason||'当前不可用'),'small'));$('selection-actions').append(detail);}
    if(!descriptors.length)$('selection-actions').append(node('p','当前读取没有行动描述。','small'));
  }
  if(keep&&!keep.isConnected){$('selection-actions').append(keep);keep.dispatchEvent(new Event('refresh-candidate-preview'));}
  renderCreative(entity,waiting);
  const children=entity.relations?.children||[];
  if(children.length){const detail=node('details');detail.append(node('summary','关联完整物件 · '+children.length+' 件'));for(const child of children)detail.append(button('查看 '+(child.name||'关联物件'),()=>run(()=>select(child.id)),{disabled:actionBusy,secondary:true}));$('selection-content').append(detail);}
  if(entity.authored_description){const detail=node('details');detail.append(node('summary','发表时的说明'),node('p',entity.authored_description,'small'));$('selection-history').append(detail);}
  if(entity.created_by)$('selection-history').append(node('p','创作来源：'+authorLabel(entity.created_by),'small'));
}
function appendFactRows(parent,rows,scope='facts'){
  for(const row of rows||[]){
    if(row.type==='group'){
      const detail=node('details',undefined,'fact-group'),key=scope+':'+row.id;detail.open=factDisclosures.get(key)===true;
      detail.append(node('summary',row.label||'详细事实'));appendFactRows(detail,row.rows,scope);
      detail.addEventListener('toggle',()=>{if(detail.isConnected)factDisclosures.set(key,detail.open);});parent.append(detail);
    }else{const item=node('div',undefined,'fact-row'),temporal={historical:'历史保存',published:'发表说明'}[row.temporal_scope];item.append(node('small',(row.label||'事实')+(temporal?' · '+temporal:'')),node('span',factRowText(row)));parent.append(item);}
  }
}
function appendFactSections(parent,sections,scope){
  for(const section of sections||[]){
    if(!section.rows?.length)continue;
    const detail=node('details',undefined,'fact-group'),key=scope+':section:'+section.id;detail.open=factDisclosures.get(key)===true;
    detail.append(node('summary',section.label||'详细事实'));appendFactRows(detail,section.rows,scope);
    detail.addEventListener('toggle',()=>{if(detail.isConnected)factDisclosures.set(key,detail.open);});parent.append(detail);
  }
}
function appendFacts(parent,value,depth=0,scope='facts',path=[]){
  if(!value||typeof value!=='object'){parent.append(node('p',factValue(value),'small'));return;}
  if(depth>=6){parent.append(node('p','更深的结构未在此展开。','small'));return;}
  const entries=Object.entries(value),limit=40,processRecord=path.at(-1)==='process'&&typeof value.status_meaning==='string';
  for(const [key,item] of entries.slice(0,limit)){
    const label=processRecord&&key==='status'?'保存的过程标记':processRecord&&key==='last_tick'?'过程记录更新时间':key==='process'&&typeof item?.status_meaning==='string'?'保存的过程记录':Array.isArray(value)?'记录 '+(Number(key)+1):factLabel(key);
    if(item!==null&&typeof item==='object'){
      const disclosureKey=scope+':'+[...path,key].join('.'),detail=node('details',undefined,'fact-group');detail.open=factDisclosures.get(disclosureKey)??false;
      detail.addEventListener('toggle',()=>{if(!detail.isConnected)return;factDisclosures.set(disclosureKey,detail.open);if(factDisclosures.size>160)factDisclosures.delete(factDisclosures.keys().next().value);});
      detail.append(node('summary',label+' · '+factValue(item)));appendFacts(detail,item,depth+1,scope,[...path,key]);parent.append(detail);
    }else{const row=node('div',undefined,'fact-row'),display=processRecord&&key==='status'?({idle:'闲置',running:'运行',waiting:'等待',error:'错误（当前已暂停）'}[item]||factValue(item)):factValue(item);row.append(node('small',label),node('span',display));parent.append(row);}
  }
  if(entries.length>limit)parent.append(node('p','此处展开前 '+limit+' 项，共 '+entries.length+' 项；不是完整历史。','small'));
  if(!entries.length)parent.append(node('p',Array.isArray(value)?'当前没有已读记录。':'当前没有已读属性。','small'));
}
function renderCreative(entity,previous=false){
  appendFactSections($('selection-content'),entity.fact_sections,entity.id);
  const request=entity.definition_request;if(!request)return;
  const ref=request.arguments.definition_ref;
  if(definitionReadRef!==ref){definitionReadRef=ref;definitionReadValue=null;definitionReadError=null;definitionReadBusy=false;definitionReadOpen=false;++definitionReadEpoch;}
  const detail=node('details',undefined,'rule-definition');detail.open=definitionReadOpen;
  detail.addEventListener('toggle',()=>{if(detail.isConnected)definitionReadOpen=detail.open;});
  detail.append(node('summary','所选固定版本的详细做法'),button(definitionReadBusy?'正在读取…':'读取详细做法',()=>run(()=>readRuleDefinition(request,entity.id)),{disabled:previous||definitionReadBusy,secondary:true}));
  if(definitionReadError)detail.append(node('p',definitionReadError,'action-reason'));
  if(definitionReadValue)appendFactSections(detail,definitionReadValue.fact_sections,ref);
  $('selection-content').append(detail);
}
async function readRuleDefinition(request,id){
  if(definitionReadBusy)return;const epoch=session,reading=++definitionReadEpoch;definitionReadBusy=true;definitionReadOpen=true;definitionReadError=null;renderSelection();
  try{const value=await client.query(request.function_id,request.arguments);if(epoch!==session||reading!==definitionReadEpoch||selectedId!==id)return;definitionReadValue=value;}
  catch(error){if(epoch===session&&reading===definitionReadEpoch)definitionReadError=error.message||'详细做法尚未读取。';}
  finally{if(epoch===session&&reading===definitionReadEpoch){definitionReadBusy=false;lastSelectionSignature='';renderSelection();}}
}
function fabricationDraftRoute(){return 'grove-fabrication:'+directoryDefinitionRef+':'+directoryType;}
async function clearRecoveredFabricationDraft(args){
  const route='grove-fabrication:'+args.definition_ref+':'+args.type_id;let rows;try{rows=JSON.parse(client.draft(route)||'null');}catch{}
  const fields=value=>({entity_id:value.entity_id,lot_id:value.lot_id,quantity_u:Number(value.quantity_u)});
  if(!Array.isArray(rows)||JSON.stringify(rows.map(fields))!==JSON.stringify((args.sources||[]).map(fields)))return;
  client.saveDraft(route,'[]');
  if(directoryDefinitionRef===args.definition_ref&&directoryType===args.type_id){fabricationRows=[];fabricationMenu=null;await readFabricationMenu();renderDefinitionDirectory();}
}
function saveFabricationDraft(){
  if(!current()||!directoryDefinitionRef||!directoryType)return;
  try{client.saveDraft(fabricationDraftRoute(),JSON.stringify(fabricationRows));client.saveDraft('grove-fabrication-selection',JSON.stringify({definition_ref:directoryDefinitionRef,type_id:directoryType}));}catch(error){announce('成型草稿未能保存在本机：'+error.message,true);}
}
function loadFabricationDraft(){
  let saved=null;try{saved=JSON.parse(client.draft(fabricationDraftRoute())||'null');}catch{}
  fabricationRows=Array.isArray(saved)?saved.filter(row=>row&&typeof row.row_id==='string'&&typeof row.entity_id==='string'&&typeof row.lot_id==='string').map(row=>({...row})):[];
  if(saved===null)fabricationRows.push({row_id:crypto.randomUUID(),entity_id:'',lot_id:'',material_id:'',quantity_u:''});
}
async function readDefinitionDirectory({after=directoryCursors.at(-1),filters=directoryFilters,reset=false,direction=null}={}){
  if(directoryBusy||fabricationBusy||!playing||!current())return;
  if(filters.key&&!filters.author){directoryError='按规则系列查询时，请同时填写作者身份。';renderDefinitionDirectory();return;}
  const epoch=session,reading=++directoryEpoch;directoryOpen=true;directoryBusy=true;directoryError=null;
  if(reset){directoryFilters={...filters};directoryCursors=[null];directoryPage=null;after=null;}
  const cursors=direction==='next'?[...directoryCursors,after]:direction==='previous'?directoryCursors.slice(0,-1):[...directoryCursors];renderDefinitionDirectory();
  try{const page=await client.query('grove.client_definitions',{mode:'list',...filters,limit:8,...(after?{after}:{})});if(epoch!==session||reading!==directoryEpoch||!playing)return;directoryPage=page;directoryCursors=cursors;}
  catch(error){if(epoch===session&&reading===directoryEpoch)directoryError=error.message||'规则目录尚未读到；原选择保留。';}
  finally{if(epoch===session&&reading===directoryEpoch){directoryBusy=false;renderDefinitionDirectory();}}
}
async function readPublishedDefinition(ref,{type_id=null}={}){
  if(directoryBusy||fabricationBusy||actionBusy||!playing||!current())return;
  saveFabricationDraft();const epoch=session,reading=++directoryEpoch;
  directoryDefinitionRef=ref;directoryDefinition=null;directoryType=null;fabricationMenu=null;fabricationRows=[];fabricationError=null;directoryError=null;directoryBusy=true;renderDefinitionDirectory();
  try{const value=await client.query('grove.client_definitions',{mode:'detail',definition_ref:ref});if(epoch!==session||reading!==directoryEpoch||!playing)return;if(value.definition_ref!==ref)throw new Error('读取结果不属于所选规则版本。');directoryDefinition=value;}
  catch(error){if(epoch===session&&reading===directoryEpoch)directoryError=error.message||'详细定义尚未读到。';}
  finally{if(epoch===session&&reading===directoryEpoch){directoryBusy=false;renderDefinitionDirectory();}}
  if(epoch===session&&reading===directoryEpoch&&directoryDefinition&&type_id&&directoryDefinition.types?.some(row=>row.id===type_id&&row.can_fabricate===true))await readFabricationMenu(type_id);
}
async function readFabricationMenu(type=directoryType){
  if(directoryBusy||fabricationBusy||actionBusy||!directoryDefinition||!type||!playing||!current())return;
  if(directoryType!==type){saveFabricationDraft();directoryType=type;fabricationMenu=null;loadFabricationDraft();}
  const epoch=session,reading=++directoryEpoch,ref=directoryDefinitionRef;fabricationBusy=true;fabricationError=null;renderDefinitionDirectory();saveFabricationDraft();
  try{const request=directoryDefinition.types?.find(row=>row.id===type)?.creation_request;if(!request)throw new Error('所选类型没有服务器创建读取请求。');const menu=await client.query(request.function_id,request.arguments);if(epoch!==session||reading!==directoryEpoch||!playing)return;if(menu.definition_ref!==ref||menu.type_id!==type)throw new Error('成型菜单不属于所选规则和类型。');fabricationMenu=menu;}
  catch(error){if(epoch===session&&reading===directoryEpoch){fabricationMenu=null;fabricationError=error.message||'尚未读到成型来源。';}}
  finally{if(epoch===session&&reading===directoryEpoch){fabricationBusy=false;renderDefinitionDirectory();}}
}
function renderDefinitionDirectory(){
  const area=$('rule-catalog-result');if(!directoryOpen)return;area.replaceChildren();
  const filters=node('form',undefined,'descriptor-action'),author=node('input'),series=node('input');author.type=series.type='text';author.maxLength=128;series.maxLength=80;author.value=directoryInputs.author;series.value=directoryInputs.key;
  for(const [label,control] of [['作者身份（可选）',author],['规则系列（须同时指定作者）',series]]){const field=node('label',label);field.append(control);filters.append(field);}
  author.addEventListener('input',()=>directoryInputs.author=author.value);series.addEventListener('input',()=>directoryInputs.key=series.value);
  const search=button('读取这一页',()=>{}, {disabled:directoryBusy||fabricationBusy});search.type='submit';filters.append(search);
  filters.addEventListener('submit',event=>{event.preventDefault();if(search.disabled)return;const values={};if(author.value.trim())values.author=author.value.trim();if(series.value.trim())values.key=series.value.trim();run(()=>readDefinitionDirectory({filters:values,reset:true}));});area.append(filters);
  if(directoryError)area.append(node('p',directoryError,'action-reason'));
  if(directoryBusy)area.append(node('p','正在读取所选目录或规则版本…','small'));
  const definitions=directoryPage?.definitions||[];
  if(directoryFilters.source_definition_ref)area.append(node('p','正在查看基于所选固定版本的直接改作；原作和旧物件保留。','small'));
  if(directoryPage){area.append(node('p','第 '+directoryCursors.length+' 页 · '+definitions.length+' 份已发表定义'+(directoryPage.has_more?' · 后面还有内容':' · 已到这一查询的末页'),'small'));}
  for(const definition of definitions){
    const section=node('section',undefined,'content-contribution');section.append(node('strong',(definition.name||definition.key)+' · 版本 '+definition.version),node('p','作者：'+authorLabel(definition.author),'small'),node('p',definition.author+' · '+definition.key+' · '+definition.definition_ref,'fact-reference'));
    section.append(button('读取类型与详细做法',()=>run(()=>readPublishedDefinition(definition.definition_ref)),{disabled:directoryBusy||fabricationBusy||actionBusy,secondary:true}));area.append(section);
  }
  if(directoryPage&&!definitions.length)area.append(node('p','这一查询没有已发表定义；可以清空筛选重新读取。','small'));
  const pagination=node('div',undefined,'content-pagination');
  pagination.append(button('上一页',()=>run(()=>readDefinitionDirectory({after:directoryCursors.at(-2),direction:'previous'})),{disabled:directoryBusy||fabricationBusy||directoryCursors.length<2,secondary:true}),button('下一页',()=>run(()=>readDefinitionDirectory({after:directoryPage.next_cursor,direction:'next'})),{disabled:directoryBusy||fabricationBusy||!directoryPage?.has_more||!directoryPage?.next_cursor,secondary:true}));area.append(pagination);
  if(directoryDefinition){
    const section=node('section',undefined,'creative-state'),definition=directoryDefinition;section.append(node('h4',definition.name||'所选规则'),node('p',directoryDefinitionRef,'fact-reference'));
    const detail=node('details',undefined,'rule-definition');detail.append(node('summary','所选不可变版本的完整说明'));appendFactSections(detail,directoryDefinition.fact_sections,directoryDefinitionRef);section.append(detail);
    const source=definition.source_definition,sourceRequest=source?.read_request,revisions=definition.revisions_request;
    if(sourceRequest?.function_id==='grove.client_definitions'&&sourceRequest.arguments?.mode==='detail')section.append(button('读取原作 · '+(source.name||source.key)+' · '+(source.author_name||'创作者'),()=>run(()=>readPublishedDefinition(sourceRequest.arguments.definition_ref)),{disabled:directoryBusy||fabricationBusy||actionBusy,secondary:true}));
    if(revisions?.function_id==='grove.client_definitions'&&revisions.arguments?.mode==='list')section.append(button('查看基于此版本的改作',()=>run(()=>readDefinitionDirectory({filters:{source_definition_ref:revisions.arguments.source_definition_ref},reset:true})),{disabled:directoryBusy||fabricationBusy||actionBusy,secondary:true}));
    const types=definition.types||[],choices=node('select');choices.setAttribute('aria-label','按真实批次成型的类型');const empty=node('option','请选择按真实材料成型的类型');empty.value='';choices.append(empty);
    for(const kind of types.filter(row=>row.can_fabricate===true&&row.creation_request)){const option=node('option',kind.label);option.value=kind.id;choices.append(option);}choices.value=directoryType||'';choices.disabled=directoryBusy||fabricationBusy||actionBusy;
    const field=node('label','成型类型');field.append(choices);section.append(field);choices.addEventListener('change',()=>{if(choices.value)run(()=>readFabricationMenu(choices.value));else{saveFabricationDraft();directoryType=null;fabricationMenu=null;fabricationRows=[];fabricationError=null;renderDefinitionDirectory();}});
    if(choices.options.length===1)section.append(node('p','这份规则没有真实批次成型类型；其他类型仍按其公开创建规则使用。','small'));
    if(directoryType)section.append(button('刷新原类型的来源与数量',()=>run(()=>readFabricationMenu()),{disabled:directoryBusy||fabricationBusy||actionBusy,secondary:true}));
    if(fabricationBusy)section.append(node('p','正在读取当前可用的真实来源；保留原选择。','small'));
    if(fabricationError)section.append(node('p',fabricationError,'action-reason'));
    if(fabricationMenu)renderFabricationEditor(section);area.append(section);
  }
  const language=node('details',undefined,'rule-definition'),languageResult=node('div');language.append(node('summary','规则语言能力（不展开发表历史）'),button('读取语言目录',()=>run(async()=>{const epoch=session;languageResult.replaceChildren(node('p','正在读取…','small'));try{const result=await client.query('grove.rule_catalog',{topic:'all'});if(epoch===session&&language.isConnected){languageResult.replaceChildren();appendFacts(languageResult,result);}}catch(error){if(epoch===session&&language.isConnected)languageResult.replaceChildren(node('p',error.message,'action-reason'));}}),{secondary:true}),languageResult);area.append(language);
}
function renderFabricationEditor(container){
  const menu=fabricationMenu,form=node('form',undefined,'descriptor-action fabrication-editor'),rowsArea=node('div'),summary=node('div',undefined,'interaction-preview'),note=node('p',undefined,'small');
  const contract=fabricationInputContract(menu),quantitySchema=contract.quantitySchema||{},materials=menu.materials||[],choices=fabricationSources(menu),materialName=id=>materialLabel(materials.find(value=>value.material_id===id)||id);
  form.append(node('p','用原材料成型一件物件','descriptor-label'),node('p','所用身份：'+(menu.player?.name||point()?.name||'当前角色'),'small'),node('p',menu.player?.id||'身份尚未读到','fact-reference'));
  for(const [id,quantity] of Object.entries(menu.cost_u||{}))form.append(node('p','需要 '+quantity+' 单位 '+materialName(id),'small'),node('p',id,'fact-reference'));
  form.append(node('p','这里按材料单位 u 计量，与区域原料份数不同。只取明确选择的批次，成品放入自己的背包；名称相同不会合并材料身份。','small'),rowsArea);
  for(const row of fabricationRows){
    const section=node('section',undefined,'creative-state'),sourceSelect=node('select'),lotSelect=node('select'),amount=node('input');amount.type='number';amount.step=String(quantitySchema.multipleOf??(quantitySchema.type==='integer'?1:'any'));if(Number.isFinite(quantitySchema.minimum))amount.min=String(quantitySchema.minimum);if(Number.isFinite(quantitySchema.maximum))amount.max=String(quantitySchema.maximum);amount.inputMode='numeric';amount.value=row.quantity_u;sourceSelect.setAttribute('aria-label','成型材料来源');lotSelect.setAttribute('aria-label','成型材料批次');
    const sourceEmpty=node('option','请选择真实来源');sourceEmpty.value='';sourceSelect.append(sourceEmpty);
    for(const source of choices){const option=node('option',candidateLabel(source,choices,'单位'));option.value=source.id;sourceSelect.append(option);}
    if(row.entity_id&&!choices.some(source=>source.id===row.entity_id)){const invalid=node('option',(row.source_name||'原来源')+' · 当前不可用，请重选');invalid.value=row.entity_id;invalid.disabled=true;sourceSelect.append(invalid);}sourceSelect.value=row.entity_id;
    const source=choices.find(value=>value.id===row.entity_id),lotEmpty=node('option','请选择材料批次');lotEmpty.value='';lotSelect.append(lotEmpty);
    for(const lot of source?.lots||[]){const option=node('option',materialName(lot.material)+' · '+slotLabel(lot.slot)+' · 当前 '+lot.quantity+' 单位');option.value=lot.id;lotSelect.append(option);}
    if(row.lot_id&&!source?.lots.some(lot=>lot.id===row.lot_id&&lot.material===row.material_id)){const invalid=node('option',(row.material_name||'原批次')+' · 当前不可用，请重选');invalid.value=row.lot_id;invalid.disabled=true;lotSelect.append(invalid);}lotSelect.value=row.lot_id;
    for(const [label,control] of [['从哪里取',sourceSelect],['其中哪一批',lotSelect],['投入数量（单位）',amount]]){const field=node('label',label);field.append(control);section.append(field);}
    const identity=node('p',undefined,'fact-reference');section.append(identity);
    function showIdentity(){identity.textContent=[row.entity_id?'来源 '+row.entity_id:null,row.lot_id?'批次 '+row.lot_id:null,row.material_id?'原材料 '+row.material_id:null].filter(Boolean).join(' · ');}showIdentity();
    sourceSelect.addEventListener('change',()=>{row.entity_id=sourceSelect.value;const chosen=choices.find(value=>value.id===row.entity_id);row.source_name=chosen?.name||'';row.expected_entity_version=chosen?.version;row.lot_id='';row.material_id='';row.material_name='';saveFabricationDraft();renderDefinitionDirectory();});
    lotSelect.addEventListener('change',()=>{const lot=source?.lots.find(value=>value.id===lotSelect.value);row.lot_id=lotSelect.value;row.material_id=lot?.material||'';row.material_name=lot?materialName(lot.material):'';saveFabricationDraft();showIdentity();update();});
    amount.addEventListener('input',()=>{row.quantity_u=amount.value;saveFabricationDraft();update();});
    if(source&&row.expected_entity_version!==source.version)section.append(button('按本次读取重新确认同一来源',()=>{row.expected_entity_version=source.version;saveFabricationDraft();renderDefinitionDirectory();},{secondary:true}));
    section.append(button('删除这一来源行',()=>{fabricationRows=fabricationRows.filter(value=>value.row_id!==row.row_id);saveFabricationDraft();renderDefinitionDirectory();},{secondary:true}));rowsArea.append(section);
  }
  const add=button('添加来源行'+(Number.isFinite(contract.maxItems)?'（最多'+contract.maxItems+'行）':''),()=>{fabricationRows.push({row_id:crypto.randomUUID(),entity_id:'',lot_id:'',material_id:'',quantity_u:String(contract.defaultQuantity??'')});saveFabricationDraft();renderDefinitionDirectory();},{secondary:true}),controls=node('div',undefined,'content-pagination'),submit=button('用所选原材料成型',()=>{}, {disabled:true}),cancel=button('取消材料选择',()=>{fabricationRows=[];saveFabricationDraft();renderDefinitionDirectory();},{secondary:true});submit.type='submit';
  controls.append(submit,cancel);form.append(add,summary,note,controls);
  function update(){
    const plan=fabricationPlan(menu,fabricationRows),busy=actionBusy||executionReclaimBusy||operationRecoveryBusy||fabricationBusy||!!client.pending(),available=menu.action?.available??menu.available;
    for(const control of rowsArea.querySelectorAll('input,select,button'))control.disabled=busy;add.disabled=busy||contract.errors.length>0||fabricationRows.length>=contract.maxItems;cancel.disabled=busy;
    submit.disabled=busy||available!==true||!Number.isInteger(menu.player?.version)||plan.errors.length>0;
    summary.replaceChildren(...Object.entries(menu.cost_u||{}).map(([id,quantity])=>node('p',materialName(id)+'：已选 '+(plan.totals[id]||0)+' / '+quantity+' 单位','small')));
    note.textContent=client.pending()?'先核对原操作结果，草稿保留，不发出替代成型。':busy?'当前操作正在保存或恢复；原材料选择保留。':available!==true?menu.action?.reason||menu.reason||'当前没有取得成型资格。':plan.errors[0]||'提交前会再次读取同一规则、类型和原来源；只核对原批次，不自动换料。';
  }
  form.addEventListener('refresh-fabrication-state',update);
  form.addEventListener('submit',event=>{event.preventDefault();update();if(submit.disabled)return;run(fabricateSelected);});update();container.append(form);
}
async function fabricateSelected(){
  const ref=directoryDefinitionRef,type=directoryType,request=directoryDefinition.types?.find(row=>row.id===type)?.creation_request,functionId=fabricationMenu?.intent?.function_id,rows=fabricationRows.map(row=>({...row})),draft=JSON.stringify(rows);saveFabricationDraft();stopWalking();
  if(typeof functionId!=='string')throw new Error('先读取正式成型操作接口。');
  const success=await perform(functionId,async isCurrent=>{
    const menu=await client.query(request.function_id,request.arguments);if(!isCurrent())return null;
    if(directoryDefinitionRef!==ref||directoryType!==type||JSON.stringify(fabricationRows)!==draft)throw new Error('选择已改变，成型尚未提交。请确认原材料选择后再点一次。');
    if(menu.definition_ref!==ref||menu.type_id!==type)throw new Error('成型菜单与原规则引用不一致，尚未提交。');fabricationMenu=menu;
    const plan=fabricationPlan(menu,rows);renderDefinitionDirectory();
    if((menu.action?.available??menu.available)!==true||plan.errors.length)throw new Error(menu.action?.reason||menu.reason||plan.errors[0]||'当前不能成型。');
    if(menu.player?.id!==client.roleId||!Number.isInteger(menu.player?.version))throw new Error('当前身份与成型菜单未能一致核对，尚未提交。');
    if(menu.intent?.function_id!==functionId)throw new Error('成型操作接口尚未一致核对。');
    return {...structuredClone(menu.intent.fixed_arguments),sources:plan.sources};
  },'用原材料成型');
  if(success&&directoryDefinitionRef===ref&&directoryType===type&&JSON.stringify(fabricationRows)===draft){fabricationRows=[];saveFabricationDraft();fabricationMenu=null;await readFabricationMenu(type);}
  renderDefinitionDirectory();
}
function actionReadTarget(){return selectedTile?{tile:{...selectedTile}}:{entity_id:selectedId};}
async function openDescriptor(descriptor,entity){
  rememberActionDrafts();activeActionKey=actionKey(descriptor);
  if(descriptor.expanded===false){
    const epoch=session,reading=++inspectEpoch,target=actionReadTarget(entity);inspection={...inspection,reading:true};lastSelectionSignature='';renderSelection();
    try{const request=descriptor.read_request,result=await client.query(request.function_id,request.arguments);if(epoch!==session||reading!==inspectEpoch||JSON.stringify(actionReadTarget(entity))!==JSON.stringify(target))return;inspection=result;}
    finally{if(epoch===session&&reading===inspectEpoch){lastSelectionSignature='';renderSelection();}}
  }else{lastSelectionSignature='';renderSelection();}
}
function renderDescriptorAction(descriptor,entity,disabled){
  const choose=()=>run(()=>openDescriptor(descriptor,entity));
  $('selection-actions').append(button(descriptorLabel(descriptor),choose,{disabled:disabled||descriptor.available!==true}));
  if(descriptor.description)$('selection-actions').append(node('p',descriptor.description,'small'));
  if(descriptor.available!==true)$('selection-actions').append(node('p',descriptor.reason||'当前不可用','action-reason'));
}
const setArgument=(args,path,value)=>{let cursor=args;for(const part of path.slice(0,-1))cursor=cursor[part]??=(Object.create(null));cursor[path.at(-1)]=value;};
const sameValue=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
function controlChoices(control,values){return (control.choices||[]).filter(choice=>Object.entries(choice.when||{}).every(([id,allowed])=>allowed.some(value=>sameValue(value,values[id]))));}
const choiceAvailable=choice=>choice?.validation?.available!==false&&choice?.preview?.available!==false;
function readChoicePage(page){
  const request=value=>value&&typeof value.function_id==='string'&&value.arguments&&typeof value.arguments==='object'&&!Array.isArray(value.arguments);
  if(!page||!Array.isArray(page.choices)||typeof page.complete!=='boolean'||!(page.next_request===null||request(page.next_request))||page.complete!==(page.next_request===null)||page.choices.some(choice=>!choice||!Object.hasOwn(choice,'value')||typeof choice.label!=='string'||!choice.arguments_patch||typeof choice.arguments_patch!=='object'||Array.isArray(choice.arguments_patch)||!request(choice.read_request)||typeof choice.validation?.available!=='boolean'||!(choice.validation.reason===null||typeof choice.validation.reason==='string'))){
    client.projectionBlocked=true;throw new Error('候选读取合同尚未支持；原选择保留，请重新发现接口。');
  }
  return page;
}
function renderDescriptorEditor(descriptor,entity,disabled){
  const saved=actionDrafts.get(entity.id+':'+descriptor.action)?.descriptor;
  if(saved?.id===descriptor.id&&saved.expanded!==false)descriptor=saved;
  if(descriptor.expanded===false){
    $('selection-actions').append(node('p',descriptor.candidate_requests?.length?'选择实际候选后读取这项行动的输入。':'这项行动尚未完整展开，请重新读取。','small'));
    for(const candidate of descriptor.candidate_requests||[])$('selection-actions').append(button(candidate.label||'读取候选',()=>run(()=>openDescriptor({...descriptor,read_request:candidate.read_request},entity)),{disabled,secondary:true}));
    return;
  }
  const form=node('form',undefined,'descriptor-action');form.dataset.entityId=entity.id;form.dataset.action=descriptor.action;form.dataset.actionKey=actionKey(descriptor);
  form.observedDescriptor=descriptor;
  form.append(node('p',descriptorLabel(descriptor),'descriptor-label'));
  if(descriptor.description)form.append(node('p',descriptor.description,'small'));
  const preconditions=node('details');preconditions.append(node('summary','原观察的操作条件（提交前重新读取）'));appendFactRows(preconditions,descriptor.preconditions,descriptor.id);if(descriptor.preconditions?.length)form.append(preconditions);
  if(descriptor.cost_facts){const cost=node('div',undefined,'interaction-preview');appendFactRows(cost,descriptor.cost_facts,descriptor.id);form.append(cost);}
  const entries=[],previews=node('div',undefined,'interaction-preview'),note=node('p',undefined,'small'),submit=button('确认'+descriptorLabel(descriptor),()=>{},{disabled:true});submit.type='submit';
  let discoveryBusy=false;
  for(const control of descriptor.controls||[]){
    const field=node('label',control.label||'输入'),schema=control.schema||{};let input;
    if(['choice','multi_choice'].includes(control.kind)){input=node('select');input.multiple=control.kind==='multi_choice';}
    else{input=node('input');input.type=control.kind==='boolean'?'checkbox':['integer','number'].includes(control.kind)?'number':'text';if(input.type==='number'){input.step=control.kind==='integer'?'1':'any';if(Number.isFinite(schema.minimum))input.min=schema.minimum;if(Number.isFinite(schema.maximum))input.max=schema.maximum;}if(Number.isInteger(schema.maxLength))input.maxLength=schema.maxLength;}
    input.required=control.required===true&&control.kind!=='boolean';input.disabled=disabled;input.dataset.controlId=control.id;field.append(input);form.append(field);
    const entry={control,input,unsupported:!['choice','multi_choice','boolean','integer','number','string'].includes(control.kind)};entries.push(entry);
    if(entry.unsupported)field.append(node('p','当前客户端尚不支持此输入类型。','action-reason'));
    if(control.discovery_request){
      const cacheKey=entity.id+':'+descriptor.id+':'+control.id,cache=interactionSelections.get(cacheKey)||{choices:[],nextRequest:null};interactionSelections.set(cacheKey,cache);
      entry.discovery=cache;
      const discover=button('读取候选目录',()=>run(async()=>{
        if(actionBusy||discoveryBusy||client.pending())return;
        const epoch=session;discoveryBusy=true;update();
        try{const request=cache.nextRequest||control.discovery_request,page=await client.query(request.function_id,request.arguments);if(epoch!==session||!form.isConnected)return;readChoicePage(page);cache.choices=[...new Map([...cache.choices,...page.choices].map(row=>[JSON.stringify(row.value),row])).values()];cache.nextRequest=page.next_request;discover.textContent=page.next_request?'读取下一页候选':'重新读取候选';update();}finally{discoveryBusy=false;if(form.isConnected)update();}
      }),{disabled,secondary:true});field.append(discover);
    }
    if(control.default!==null&&control.default!==undefined){if(input.type==='checkbox')input.checked=control.default;else if(input.tagName!=='SELECT')input.value=control.default;}
    input.addEventListener('change',update);input.addEventListener('input',update);
  }
  function readValues(){return Object.fromEntries(entries.map(({control,input})=>[control.id,input.multiple?[...input.selectedOptions].map(option=>JSON.parse(option.value)):input.tagName==='SELECT'?input.value?JSON.parse(input.value):undefined:input.type==='checkbox'?input.checked:input.type==='number'?input.value===''?undefined:Number(input.value):input.value||undefined]));}
  function choicesFor(entry,values){return controlChoices({...entry.control,choices:[...(entry.control.choices||[]),...(entry.discovery?.choices||[])]},values);}
  function update(){
    const values=readValues(),live=actionDescriptors(inspection).find(row=>row.id===descriptor.id),waiting=inspection?.reading||!inspection?.context;
    previews.replaceChildren(node('p','原观察（提交前重新读取）','small'));let invalid=actionBusy||!!client.pending()||discoveryBusy||waiting||live?.available!==true;
    const reasons=[];
    for(const entry of entries){const {control,input}=entry;input.disabled=actionBusy||discoveryBusy||waiting||!!client.pending();
      if(input.tagName==='SELECT'){
        const previous=values[control.id],choices=choicesFor(entry,values);input.replaceChildren();
        if(!input.multiple){const placeholder=node('option',control.required?'请选择':'可选：未指定');placeholder.value='';input.append(placeholder);}
        for(const choice of choices){const option=node('option',choice.label||'候选');option.value=JSON.stringify(choice.value);option.disabled=!choiceAvailable(choice);option.selected=input.multiple?(previous||[]).some(value=>sameValue(value,choice.value)):sameValue(previous,choice.value);input.append(option);}
        const selected=input.multiple?previous||[]:previous===undefined?[]:[previous];
        for(const value of selected)if(!choices.some(choice=>sameValue(choice.value,value))){const option=node('option','原选择当前不可用，请重选');option.value=JSON.stringify(value);option.selected=true;option.disabled=true;input.append(option);invalid=true;reasons.push(control.label+'的原候选已不可用。');}
        for(const value of selected){const choice=choices.find(choice=>sameValue(choice.value,value));if(choice?.preview)appendFactRows(previews,choice.preview.rows,descriptor.id);if(choice&&!choiceAvailable(choice)){invalid=true;reasons.push(choice.validation?.reason||choice.preview?.reason||'所选候选当前不可用');}}
      }
      const value=values[control.id];
      if(input.type==='number'&&control.maximum_from_control_id){
        const source=entries.find(row=>row.control.id===control.maximum_from_control_id),choice=source?choicesFor(source,values).find(row=>sameValue(row.value,values[source.control.id])):null;
        const maximum=Math.min(control.schema?.maximum??Infinity,Number.isFinite(choice?.maximum)?choice.maximum:Infinity);
        if(Number.isFinite(maximum))input.max=String(maximum);else input.removeAttribute('max');
      }
      if(entry.unsupported||control.required&&(value===undefined||Array.isArray(value)&&!value.length)||!input.checkValidity())invalid=true;
      if(value!==undefined&&!(Array.isArray(value)&&!value.length&&!control.required)&&schemaInputErrors(control.schema,value,control.label).length)invalid=true;
    }
    submit.disabled=invalid;note.textContent=reasons[0]||(client.pending()?'先核对原操作；原选择保留。':discoveryBusy?'正在读取候选，原选择保留。':'提交前重新读取同一目标并核对原候选、固定条款与版本；不会自动替换选择。');
  }
  form.append(previews,note,submit,button('按新事实重新确认输入',()=>run(async()=>{
    if(actionBusy||client.pending())return;
    const request=descriptor.read_request,epoch=session,reading=++inspectEpoch,latest=await client.query(request.function_id,request.arguments);
    if(epoch!==session||reading!==inspectEpoch)return;
    inspection=latest;actionDrafts.delete(entity.id+':'+descriptor.action);form.remove();lastSelectionSignature='';renderSelection();
  }),{disabled,secondary:true}),button('取消选择',()=>{actionDrafts.delete(entity.id+':'+descriptor.action);form.remove();activeActionKey=null;lastSelectionSignature='';renderSelection();},{disabled,secondary:true}));
  form.addEventListener('submit',event=>{event.preventDefault();update();if(submit.disabled)return;const values=readValues(),selected=entries.map(entry=>({control:entry.control,value:values[entry.control.id],choices:choicesFor(entry,values).filter(choice=>(Array.isArray(values[entry.control.id])?values[entry.control.id]:[values[entry.control.id]]).some(value=>sameValue(value,choice.value)))}));run(()=>submitDescriptor(descriptor,entity,selected));});
  form.addEventListener('refresh-candidate-preview',update);update();restoreActionDraft(form,update);$('selection-actions').append(form);
}
async function submitDescriptor(original,entity,selected){
  const request=original.read_request,target=actionReadTarget(entity),epoch=session;
  stopWalking();
  const saved=await perform(original.function_id,async isCurrent=>{
    const latest=await client.query(request.function_id,request.arguments);if(!isCurrent())return null;
    if(epoch!==session||JSON.stringify(actionReadTarget(entity))!==JSON.stringify(target))throw new Error('所选目标已改变；行动尚未提交。');
    const current=actionDescriptors(latest).find(row=>row.id===original.id);inspection=latest;lastSelectionSignature='';renderSelection();
    if(!current||current.available!==true||current.expanded===false)throw new Error(current?.reason||'这项行动的当前资格未读到，请重新选择。');
    const originalFixed={...original.fixed_arguments},currentFixed={...current.fixed_arguments};delete originalFixed.expected_player_version;delete currentFixed.expected_player_version;
    const confirmationKeys=[...new Set([...(original.confirmation_keys||[]),...(current.confirmation_keys||[])])];
    if(confirmationKeys.some(key=>!sameValue(originalFixed[key],currentFixed[key])))throw new Error('需要确认的目标事实或固定条款已改变。原行动尚未提交，请核对新事实后再选择。');
    const values=Object.fromEntries(selected.map(entry=>[entry.control.id,entry.value])),args=structuredClone(original.fixed_arguments||{});args.expected_player_version=latest.player.version;
    for(const entry of selected){
      const control=current.controls?.find(row=>row.id===entry.control.id);if(!control)throw new Error('原输入已不在当前菜单中，请重新选择。');
      if(entry.value===undefined||Array.isArray(entry.value)&&!entry.value.length){if(control.required)throw new Error('请完整填写当前输入。');continue;}
      const schemaErrors=schemaInputErrors(control.schema,entry.value,control.label);if(schemaErrors.length)throw new Error(schemaErrors[0]);
      if(['number','integer'].includes(control.kind)){
        const source=control.maximum_from_control_id?current.controls.find(row=>row.id===control.maximum_from_control_id):null,choice=source?controlChoices(source,values).find(row=>sameValue(row.value,values[source.id])):null;
        const maximum=Math.min(control.schema?.maximum??Infinity,Number.isFinite(choice?.maximum)?choice.maximum:Infinity);
        if(!Number.isFinite(entry.value)||control.kind==='integer'&&!Number.isInteger(entry.value)||entry.value<(control.schema?.minimum??-Infinity)||entry.value>maximum)throw new Error('原数量已超出本次读取的允许范围，请核对后重选。');
      }
      if(['choice','multi_choice'].includes(control.kind)){
        const valuesChosen=Array.isArray(entry.value)?entry.value:[entry.value];
        for(const value of valuesChosen){
          let candidate=controlChoices(control,values).find(row=>sameValue(row.value,value));
          const before=entry.choices.find(row=>sameValue(row.value,value));
          if(before?.read_request){
            const request=before.read_request,read=await client.query(request.function_id,request.arguments);if(!isCurrent())return null;
            const page=readChoicePage(read);candidate=controlChoices({...control,choices:page.choices},values).find(row=>sameValue(row.value,value));
          }
          if(!candidate||!choiceAvailable(candidate)||confirmationKeys.some(key=>!sameValue(before?.arguments_patch?.[key],candidate.arguments_patch?.[key])))throw new Error(candidate?.validation?.reason||candidate?.preview?.reason||'原候选需要确认的观察事实或组合条件已改变，请核对后重选。');
          Object.assign(args,before?.arguments_patch||{});
        }
      }
      setArgument(args,control.path,entry.value);
    }
    return args;
  },descriptorLabel(original));
  if(saved&&epoch===session){actionDrafts.delete(entity.id+':'+original.action);$('selection-actions').querySelector('.descriptor-action')?.remove();activeActionKey=null;lastSelectionSignature='';if(selectedTile)await readSelectedTile();else renderSelection();}
}
function authorLabel(author){if(typeof author==='string')return (world?.players||[]).find(player=>player.id===author)?.name||(author.startsWith('awr_')?'世界中的另一位玩家':author);if(author&&typeof author==='object')return String(author.name||'创作者');return '创作者';}
function publicWorksFiltersFromInputs(){
  const author_name=$('public-works-author').value.trim(),name=$('public-works-name').value.trim();
  return {...(author_name?{author_name}:{}),...(name?{name}:{})};
}
async function readPublicWorks({filters=publicWorksFilters,cursors=[null]}={}){
  if(!playing||!current()||publicWorksBusy)return;
  const epoch=session,reading=++publicWorksEpoch,requestedFilters={...filters},requestedCursors=[...cursors],after=requestedCursors.at(-1);
  publicWorksBusy=true;publicWorksError=null;renderPublicWorks();
  try{
    const page=await client.query('grove.public_works',{...requestedFilters,limit:6,...(after?{after}:{})});
    if(epoch!==session||reading!==publicWorksEpoch||!playing)return;
    if(page?.scope!=='current_public_top_level_creations'||!Array.isArray(page.works))throw new Error('公开作品目录未能读取；原结果保留。');
    publicWorksPage=page;publicWorksFilters=requestedFilters;publicWorksCursors=requestedCursors;publicWorksReadAt=Date.now();
  }catch(error){if(epoch===session&&reading===publicWorksEpoch)publicWorksError=error.cancelled?'目录读取已中断；可重新读取。':error.message||'公开作品目录暂不可达。';}
  finally{if(epoch===session&&reading===publicWorksEpoch){publicWorksBusy=false;renderPublicWorks();}}
}
function localPublicWork(work){return publicWorkLocalTarget(work,world,worldFresh&&Date.now()-lastWorldRead<8000);}
function markPublicWork(work){
  if(!Number.isInteger(work?.location?.x)||!Number.isInteger(work?.location?.y))return;
  publicWorkMarker={entity_id:work.entity_id,name:work.name,location:{...work.location},read_at:publicWorksReadAt};renderPublicWorks();
  announce('地图已标记目录位置。它不提供远处地形或通路；走到附近后再读取现场。');
}
async function selectPublicWork(work){
  const entity=localPublicWork(work);
  if(!entity){announce('当前附近地图没有读到这件作品。先靠近目录位置，再刷新现场。');return;}
  stopWalking();await select(entity.id);
}
function renderPublicWorks(){
  const page=publicWorksPage,works=page?.works||[],matches=page?.author_matches||[];
  for(const id of ['public-works-search','public-works-recent','public-works-refresh'])$(id).disabled=publicWorksBusy||!playing||!current();
  $('public-works-previous').disabled=publicWorksBusy||publicWorksCursors.length<2;
  $('public-works-next').disabled=publicWorksBusy||page?.has_more!==true||!page.next_cursor;
  $('public-works-summary').textContent=publicWorksBusy?'正在读取目录':page?'第 '+publicWorksCursors.length+' 页 · '+works.length+' 件公开作品':'按作者或作品名称寻找';
  const notes=publicWorkDirectoryLines(page);
  if(publicWorksBusy)notes.unshift('正在读取；下方保留上次结果。');
  if(publicWorksError)notes.unshift('读取失败：'+publicWorksError+(page?' 下方仍是上次目录结果。':''));
  if(publicWorksReadAt)notes.push('上次目录读取 '+new Date(publicWorksReadAt).toLocaleTimeString('zh-CN')+'；位置与字段之后可能改变。到场后再读详情。');
  $('public-works-note').textContent=notes.join(' ');
  const markerNote=$('public-work-marker-note');markerNote.hidden=!publicWorkMarker;$('public-work-marker-clear').hidden=!publicWorkMarker;
  if(publicWorkMarker)markerNote.textContent='目录位置标记：'+(publicWorkMarker.name||'公开作品')+' · '+publicWorkMarker.location.x+'，'+publicWorkMarker.location.y+'（'+new Date(publicWorkMarker.read_at).toLocaleTimeString('zh-CN')+' 读取）。这不是路线，位置可能已变。';
  const available=works.map(work=>!!localPublicWork(work));
  const signature=JSON.stringify([page,publicWorksBusy,available]);
  if(signature===publicWorksRenderSignature)return;publicWorksRenderSignature=signature;
  const list=$('public-works-list');list.replaceChildren();
  for(const match of matches){
    if(typeof match.author!=='string')continue;
    const card=node('article',undefined,'public-work-card');card.append(node('strong',match.name||'未提供公开姓名'),node('p','作者身份：'+match.author,'small public-work-author-id'));
    card.append(button('查看这位作者的作品',()=>run(()=>readPublicWorks({filters:{author:match.author,...(publicWorksFilters.name?{name:publicWorksFilters.name}:{})}})),{secondary:true,disabled:publicWorksBusy}));list.append(card);
  }
  works.forEach((work,index)=>{
    const card=node('article',undefined,'public-work-card');card.append(node('h4',work.name||'未命名作品'));
    card.append(node('p','作者：'+(work.created_by_name||'未提供公开姓名'),'small'),node('p','作者身份：'+(work.created_by||'未读取'),'small public-work-author-id'));
    const hasLocation=Number.isInteger(work.location?.x)&&Number.isInteger(work.location?.y);
    card.append(node('p',hasLocation?'目录位置：'+work.location.x+'，'+work.location.y:'目录位置未读取','small'));
    for(const text of publicWorkFactLines(work))card.append(node('p',text,'small'));
    card.append(node('p',available[index]?'当前附近地图已读到；选择后读取详情。':'当前附近地图未读到；目录不提供远程操作。','small'));
    const controls=node('div',undefined,'public-work-controls');
    controls.append(button('标记这个位置',()=>markPublicWork(work),{secondary:true,disabled:!hasLocation}),button('选择附近物件',()=>run(()=>selectPublicWork(work)),{secondary:true,disabled:!available[index]}));
    card.append(controls);list.append(card);
  });
}

function renderPublicContent(){
  const content=world?.public_content;
  if(contentHistoryId!==content?.id){contentHistoryId=content?.id||null;contentHistoryPages=[];contentHistoryBusy=false;}
  const signature=JSON.stringify([content||null,contentHistoryPages,contentHistoryBusy]);if(signature===lastContentSignature)return;lastContentSignature=signature;
  const list=$('public-content-list');list.replaceChildren();
  if(!content){$('public-content-note').textContent='当前没有已读的公共内容';list.append(node('p','公共物件和真实操作仍可在地图查看。','small'));return;}
  const anchors=content.anchors||[],page=contentHistoryPages.at(-1),contributions=page?.contributions||content.contributions||[],outcomes=page?.outcome_refs||content.outcome_refs||[],history=page?.history||content.history||{};
  $('public-content-note').textContent='这处地点的共同记录';
  list.append(node('p','这是当前获准读取的一页公共记录。物件详情显示当前事实；创作说明保留发表时的想法，不代表结果已发生。','small'));
  const anchorSection=node('section',undefined,'content-section');anchorSection.append(node('h4','同一现场的对象'));
  for(const anchor of anchors){const entity=anchor.entity,row=node('div',undefined,'content-anchor');row.append(button(entity?.name||anchor.name||anchor.entity_id,()=>run(()=>select(anchor.entity_id)),{secondary:true}),node('p',entity?stateLine(entity):'当前状态需查看物件','small'),node('small','原对象 '+anchor.entity_id+' · 版本 '+(entity?.version??anchor.version),'fact-reference'));anchorSection.append(row);}
  if(!anchors.length)anchorSection.append(node('p','当前页没有可见对象。','small'));list.append(anchorSection);
  if(content.anchors_truncated)list.append(node('p','这里仅列部分获准读取的公共对象。其他对象仍可从地图或记录中选择查看。','small'));
  const contributionSection=node('section',undefined,'content-section');contributionSection.append(node('h4',(page?'历史页 · ':'当前页 · ')+'作者留下的贡献'));
  for(const contribution of contributions){const row=node('article',undefined,'content-contribution');row.append(node('strong',authorLabel(contribution.author)+' · '+contribution.id),node('p','发表时的说明：'+(contribution.description||'未附说明'),'small'));
    const basedOn=contribution.based_on;if(basedOn){const refs=Array.isArray(basedOn)?basedOn.map(ref=>typeof ref==='string'?ref:ref.id||ref.entity_id||JSON.stringify(ref)):typeof basedOn==='object'?Object.entries(basedOn).map(([key,value])=>key+': '+(Array.isArray(value)?value.join('、'):String(value))):[String(basedOn)];row.append(node('p','承接来源：'+refs.join(' · '),'fact-reference'));}
    for(const id of contribution.entity_ids||[])row.append(button('查看对象 '+(entityById(id)?.name||id),()=>run(()=>select(id)),{secondary:true}));contributionSection.append(row);}
  if(!contributions.length)contributionSection.append(node('p','当前页没有创作贡献。','small'));list.append(contributionSection);
  const outcomeSection=node('section',undefined,'content-section');outcomeSection.append(node('h4',(page?'历史页 · ':'当前页 · ')+'已经发生的行动与后果'));
  for(const outcome of outcomes){const row=node('article',undefined,'content-outcome');row.append(node('p',outcome.summary||outcome.action||'世界变化'),node('small',authorLabel(outcome.author)+' · '+outcome.id+(outcome.entity_id?' · 原对象 '+outcome.entity_id:''),'fact-reference'));outcomeSection.append(row);}
  if(!outcomes.length)outcomeSection.append(node('p','当前页没有已读后果。','small'));list.append(outcomeSection);
  const more=history.has_more_contributions||history.has_more_outcomes,controls=node('div',undefined,'content-pagination');
  if(Number.isInteger(history.contribution_count)||Number.isInteger(history.outcome_count))list.append(node('p','保存的记录：'+finite(history.contribution_count)+' 项贡献 · '+finite(history.outcome_count)+' 项后果。当前页只显示其中一部分。','small'));
  if(page){controls.append(button('上一页',()=>{contentHistoryPages.pop();renderPublicContent();},{disabled:contentHistoryBusy,secondary:true}),button('回到当前页',()=>{contentHistoryPages=[];renderPublicContent();},{disabled:contentHistoryBusy,secondary:true}));}
  if(more)controls.append(button(contentHistoryBusy?'读取中…':'更早的记录',()=>run(readContentHistory),{disabled:contentHistoryBusy,secondary:true}));
  if(controls.childNodes.length)list.append(controls);
  if(more)list.append(node('p','还有其他公共记录；当前页不能代表全部历史。','small'));
}
async function readContentHistory(){
  const content=world?.public_content;if(!content||contentHistoryBusy)return;
  const history=contentHistoryPages.at(-1)?.history||content.history;if(!history?.cursor)return;
  const epoch=session,id=content.id;contentHistoryBusy=true;renderPublicContent();
  try{const page=await client.query('grove.content_history',{content_id:id,cursor:history.cursor});if(epoch!==session||world?.public_content?.id!==id)return;contentHistoryPages.push(page);renderPublicContent();}
  catch(error){if(epoch===session&&world?.public_content?.id===id)throw error;}
  finally{if(epoch===session&&world?.public_content?.id===id){contentHistoryBusy=false;renderPublicContent();}}
}
function renderActivity(){
  if(!world)return;const items=ledger.items().filter(eventNearby).slice(-12).reverse();
  $('activity-list').replaceChildren(...items.map(e=>{const row=node('div',undefined,'activity-row');row.append(node('span',e.kind==='content'?'✦':e.kind==='process'?'≈':'·','activity-icon'),node('p',e.payload?.summary||e.kind),node('small','记录 '+e.seq));return row;}));
  if(!items.length){const changes=(world.changes||[]).slice(-4);$('activity-list').replaceChildren(...(changes.length?changes.map(c=>node('p',typeof c==='string'?c:c.summary||'','small')):[node('p','附近还没有已读的变化。试着搬动一件物件，或给它浇一点水。','small')]));}
}
function rainsAt(x,y){
  const chunk='chunk_'+Math.floor(x/16)+'_'+Math.floor(y/16);
  return activeRainChunks.has(chunk);
}
function drawCoverage(){
  for(const coverage of world?.coverage||[]){
    const selected=coverage.entity_id===selectedId;if(!selected&&!coverage.active)continue;
    const color=/^#[0-9a-f]{6}$/i.test(coverage.color)?coverage.color:'#e3d89b';
    for(const tile of coverage.cells||[]){if(!terrain(tile.x,tile.y))continue;const at=screen(tile.x,tile.y);if(coverage.active){ctx.fillStyle=color+(selected?'33':'15');ctx.fillRect(at.x-tileSize/2+1,at.y-tileSize/2+1,tileSize-2,tileSize-2);}ctx.strokeStyle=color+(selected?'aa':'55');ctx.lineWidth=selected?1.5:1;ctx.strokeRect(at.x-tileSize/2+2,at.y-tileSize/2+2,tileSize-4,tileSize-4);}
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
function drawEntity(entity,now){
  if(!Number.isFinite(entity.x)||!Number.isFinite(entity.y))return;
  const at=screen(entity.x,entity.y),appearance=safePresentation(entity),overlay=appearance.overlays,s=tileSize*.64*appearance.size;
  ctx.save();ctx.translate(at.x,at.y);ctx.shadowColor='#24342755';ctx.shadowBlur=3;ctx.shadowOffsetY=3;
  if(overlay.consumed){ctx.fillStyle='#5b6250';for(let i=0;i<4;i++){ctx.beginPath();ctx.ellipse((i-1.5)*s*.2,s*.2,s*.13,s*.06,i,0,Math.PI*2);ctx.fill();}}
  else{
    ctx.save();if(appearance.pose==='folded')ctx.scale(1,.4);
    if(appearance.shape==='bowl'){ctx.fillStyle=appearance.color;ctx.strokeStyle='#536451';ctx.beginPath();ctx.ellipse(0,s*.04,s*.47,s*.32,0,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.fillStyle=appearance.container_open?'#394b3c':'#b4b69c';ctx.beginPath();ctx.ellipse(0,-s*.06,s*.36,s*.19,0,0,Math.PI*2);ctx.fill();}
    else if(appearance.shape==='beam'){ctx.rotate(-.1);roundRect(-s*.52,-s*.18,s*1.04,s*.36,4,appearance.color,'#536451');}
    else if(appearance.shape==='cloth'){ctx.fillStyle=appearance.color;ctx.strokeStyle='#536451';ctx.beginPath();ctx.moveTo(-s*.42,-s*.28);ctx.lineTo(s*.34,-s*.37);ctx.lineTo(s*.44,s*.29);ctx.lineTo(-s*.35,s*.36);ctx.closePath();ctx.fill();ctx.stroke();}
    else if(appearance.shape==='raft'){roundRect(-s*.48,-s*.31,s*.96,s*.62,4,appearance.color,'#536451');ctx.strokeStyle='#647054';for(const y of [-.15,0,.15]){ctx.beginPath();ctx.moveTo(-s*.45,y*s);ctx.lineTo(s*.45,y*s);ctx.stroke();}}
    else{roundRect(-s*.4,-s*.36,s*.8,s*.72,appearance.shape==='stone'?8:4,appearance.color,'#536451');}
    ctx.shadowBlur=0;ctx.fillStyle='#f7f2d8';ctx.font='600 '+Math.round(s*.38)+'px system-ui';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(appearance.glyph,0,0);ctx.restore();
  }
  ctx.shadowBlur=0;ctx.shadowOffsetY=0;
  if(overlay.broken){ctx.strokeStyle='#513c31';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(-s*.12,-s*.25);ctx.lineTo(s*.05,-s*.06);ctx.lineTo(-s*.03,s*.12);ctx.lineTo(s*.14,s*.3);ctx.stroke();}
  const mechanical=mechanicalMotion.read(entity,now,worldFresh);
  if(mechanical){ctx.save();ctx.lineWidth=2;ctx.strokeStyle=mechanical.replaying?'#ffe4a0':'#526858';
    if(mechanical.role==='actuator'){const fraction=Number.isFinite(mechanical.travel)&&mechanical.travel>0?Math.min(1,Math.max(0,mechanical.position/mechanical.travel)):0;roundRect(-s*.45,s*.2,s*.9,s*.2,2,'#2e443a','#c6d2ba');roundRect(-s*.41+fraction*s*.62,s*.16,s*.2,s*.28,2,'#ffe4a0','#526858');}
    else{ctx.rotate(mechanical.angle*Math.PI*2);ctx.beginPath();ctx.arc(0,0,s*.43,0,Math.PI*2);ctx.stroke();for(let i=0;i<5;i++){const angle=i*Math.PI*2/5;ctx.beginPath();ctx.moveTo(Math.cos(angle)*s*.13,Math.sin(angle)*s*.13);ctx.lineTo(Math.cos(angle)*s*.4,Math.sin(angle)*s*.4);ctx.stroke();}ctx.fillStyle='#ffe4a0';ctx.beginPath();ctx.arc(s*.43,0,3,0,Math.PI*2);ctx.fill();}ctx.restore();}
  if(Number.isFinite(overlay.water_quantity)&&overlay.water_quantity>0||Number.isFinite(overlay.wetness)&&overlay.wetness>0){ctx.fillStyle='#74d4ee';ctx.strokeStyle='#e0f4ff';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(s*.38,-s*.48);ctx.quadraticCurveTo(s*.2,-s*.2,s*.38,-s*.18);ctx.quadraticCurveTo(s*.57,-s*.2,s*.38,-s*.48);ctx.fill();ctx.stroke();}
  if(overlay.burning&&!overlay.consumed){const flicker=1+Math.sin((worldFresh?now:0)/90+entity.x)*.08;ctx.fillStyle='#f5a852';ctx.beginPath();ctx.moveTo(-s*.28,s*.1);ctx.quadraticCurveTo(-s*.36,-s*.27,-s*.05,-s*.66*flicker);ctx.quadraticCurveTo(s*.03,-s*.18,s*.22,-s*.42);ctx.quadraticCurveTo(s*.41,-s*.04,s*.2,s*.15);ctx.closePath();ctx.fill();}
  if(entity.navigation?.blocks){ctx.strokeStyle='#624633';ctx.lineWidth=2;ctx.setLineDash([4,3]);ctx.strokeRect(-tileSize*.45,-tileSize*.42,tileSize*.9,tileSize*.84);ctx.setLineDash([]);}
  if(selectedId===entity.id){ctx.strokeStyle='#fff0bd';ctx.lineWidth=2.5;ctx.beginPath();ctx.ellipse(0,s*.25,s*.66,s*.35,0,0,Math.PI*2);ctx.stroke();}
  ctx.restore();
}
function entityMapLabel(entity,includeName=false){
  const badge=presentationBadgeText(entity),water=visibleWaterQuantity(entity),unknown=['unavailable','unmapped'].includes(entity.appearance?.binding_status);
  return [includeName?entity.name:entityGlyph(entity),badge,water===null?'':water>0?'水 '+water+'u':'',unknown&&!badge?'?':''].filter(Boolean).join(' · ');
}
function drawMapLabels(){
  const groups=new Map(),occupied=[];
  const add=(x,y,row)=>{const key=x+','+y,group=groups.get(key)||{x,y,rows:[]};group.rows.push(row);groups.set(key,group);};
  for(const entity of ground()){
    if(!Number.isFinite(entity.x)||!Number.isFinite(entity.y))continue;
    const at=screen(entity.x,entity.y);occupied.push({x:at.x-tileSize*.34,y:at.y-tileSize*.38,w:tileSize*.68,h:tileSize*.76});
    const focus=entity.id===selectedId||hoverTile?.x===entity.x&&hoverTile?.y===entity.y;
    const label=entityMapLabel(entity,focus);if(presentationBadgeText(entity)||visibleWaterQuantity(entity)>0||focus||['unavailable','unmapped'].includes(entity.appearance?.binding_status))add(entity.x,entity.y,{id:entity.id,text:label,priority:entity.id===selectedId?0:2});
  }
  const players=[...(world.players||[]).filter(player=>player.id!==point().id&&player.role_id!==client.roleId),point()];
  for(const player of players){const at=screen(player.x,player.y);occupied.push({x:at.x-tileSize*.3,y:at.y-tileSize*.4,w:tileSize*.6,h:tileSize*.82});add(player.x,player.y,{id:player.id,text:player.id===point().id?'你':player.name||'旅人',priority:1});}
  const overlaps=(a,b)=>a.x<b.x+b.w+3&&a.x+a.w+3>b.x&&a.y<b.y+b.h+3&&a.y+a.h+3>b.y;
  ctx.save();ctx.font='600 10px system-ui';ctx.textBaseline='middle';ctx.textAlign='left';
  let markerLabel=null;
  if(publicWorkMarker){const location=publicWorkMarker.location,at=publicWorkMarkerGeometry(screen(location.x,location.y),canvasWidth,canvasHeight);if(at){const text='目录标记 '+location.x+'，'+location.y,w=Math.min(200,ctx.measureText(text).width+12),rect={x:Math.min(canvasWidth-w-8,Math.max(8,at.x-w/2)),y:Math.min(canvasHeight-28,Math.max(8,at.y+(at.y>canvasHeight/2?-26:14))),w,h:20};markerLabel={text,rect};occupied.push(rect);}}
  const ordered=[...groups.values()].sort((a,b)=>Math.min(...a.rows.map(row=>row.priority))-Math.min(...b.rows.map(row=>row.priority))||a.y-b.y||a.x-b.x);
  for(const group of ordered){
    const at=screen(group.x,group.y);if(at.x<-tileSize||at.y<-tileSize||at.x>canvasWidth+tileSize||at.y>canvasHeight+tileSize)continue;
    const rows=group.rows.sort((a,b)=>a.priority-b.priority||a.id.localeCompare(b.id)),shown=rows.slice(0,3),omitted=rows.length-shown.length;
    const lines=shown.map(row=>row.text);if(omitted)lines.push('＋'+omitted+' · 点选查看全部');
    const cacheKey=group.x+','+group.y+':'+rows.map(row=>row.id).join('|'),previous=mapLabelSlots.get(cacheKey);
    let width=Math.min(200,Math.max(previous?.width||0,Math.max(...lines.map(line=>ctx.measureText(line).width))+14)),height=lines.length*16+8,rect=null;
    const positions=[[-width/2,-tileSize*.56-height],[-width/2,tileSize*.56],[tileSize*.6,-height/2],[-tileSize*.6-width,-height/2]];
    const slots=[0,1,2,3].sort((a,b)=>(a===previous?.slot?-1:b===previous?.slot?1:0));
    for(const slot of slots){const [dx,dy]=positions[slot];
      const candidate={x:Math.min(canvasWidth-width-5,Math.max(5,at.x+dx)),y:Math.min(canvasHeight-height-5,Math.max(5,at.y+dy)),w:width,h:height};
      if(!occupied.some(other=>overlaps(candidate,other))){rect=candidate;mapLabelSlots.set(cacheKey,{slot,width});if(mapLabelSlots.size>512)mapLabelSlots.delete(mapLabelSlots.keys().next().value);break;}
    }
    if(!rect){
      // Dense maps keep a compact disclosure count; each real value remains
      // available in the same-cell selector, never merged with another object.
      const text='＋'+rows.length,widthSmall=ctx.measureText(text).width+12;
      const candidate={x:Math.min(canvasWidth-widthSmall-5,Math.max(5,at.x-widthSmall/2)),y:Math.min(canvasHeight-23,Math.max(5,at.y+tileSize*.52)),w:widthSmall,h:18};
      if(occupied.some(other=>overlaps(candidate,other)))continue;
      rect=candidate;lines.splice(0,lines.length,text);width=widthSmall;height=18;
    }
    roundRect(rect.x,rect.y,width,height,4,'#24392ded');ctx.fillStyle='#f8f0d8';lines.forEach((line,index)=>ctx.fillText(line,rect.x+7,rect.y+12+index*16,width-14));occupied.push(rect);
  }
  if(markerLabel){const {text,rect}=markerLabel;roundRect(rect.x,rect.y,rect.w,rect.h,4,'#243a30ed');ctx.fillStyle='#ffebaa';ctx.fillText(text,rect.x+6,rect.y+10,rect.w-12);}
  ctx.restore();
}
function drawPlayer(p,own,now){
  let pos=p;if(own&&motion){const elapsed=Math.min(1,(now-motion.start)/motion.duration),ease=1-Math.pow(1-elapsed,3);pos={x:motion.from.x+(motion.to.x-motion.from.x)*ease,y:motion.from.y+(motion.to.y-motion.from.y)*ease};if(elapsed===1)motion=null;}
  const at=screen(pos.x,pos.y),s=tileSize;ctx.fillStyle='#30433555';ctx.beginPath();ctx.ellipse(at.x,at.y+s*.24,s*.28,s*.12,0,0,Math.PI*2);ctx.fill();
  roundRect(at.x-s*.2,at.y-s*.04,s*.4,s*.44,5,own?'#f0d395':'#aec4d1','#4b5947');ctx.fillStyle='#ecd9ba';ctx.beginPath();ctx.arc(at.x,at.y-s*.16,s*.15,0,Math.PI*2);ctx.fill();ctx.fillStyle=own?'#405d46':'#5b6f81';ctx.beginPath();ctx.ellipse(at.x,at.y-s*.28,s*.23,s*.1,0,0,Math.PI*2);ctx.fill();
  if(own){ctx.strokeStyle='#f9edc3';ctx.lineWidth=2;ctx.beginPath();ctx.ellipse(at.x,at.y+s*.27,s*.35,s*.15,0,0,Math.PI*2);ctx.stroke();}

}
function drawPublicWorkMarker(){
  if(!publicWorkMarker)return;
  const location=publicWorkMarker.location,at=publicWorkMarkerGeometry(screen(location.x,location.y),canvasWidth,canvasHeight);
  if(!at)return;
  ctx.save();ctx.translate(at.x,at.y);ctx.strokeStyle='#ffebaa';ctx.fillStyle='#ffebaa';ctx.lineWidth=2;
  if(at.offscreen){ctx.rotate(at.angle);ctx.beginPath();ctx.moveTo(10,0);ctx.lineTo(-7,-7);ctx.lineTo(-7,7);ctx.closePath();ctx.fill();ctx.rotate(-at.angle);}
  else{ctx.setLineDash([3,3]);ctx.strokeRect(-tileSize/2,-tileSize/2,tileSize,tileSize);ctx.setLineDash([]);}
  ctx.restore();
}
function draw(now){
  if(!playing||document.hidden){requestAnimationFrame(draw);return;}
  if(worldFresh&&lastWorldRead&&Date.now()-lastWorldRead>8000){worldFresh=false;mechanicalMotion.reset();$('freshness').textContent='现场已过时 · 显示上次读取，等待恢复';}
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
    drawPublicWorkMarker();
    drawMapLabels();
  }
  requestAnimationFrame(draw);
}

canvas.addEventListener('pointermove',event=>{hoverTile=tileAt(event.clientX,event.clientY);});canvas.addEventListener('pointerleave',()=>{hoverTile=null;});
canvas.addEventListener('pointerdown',event=>{if(!playing||!world)return;const tile=tileAt(event.clientX,event.clientY);canvas.focus({preventScroll:true});const objects=ground().filter(e=>e.x===tile.x&&e.y===tile.y);if(objects.length){stopWalking();run(()=>select(objects.find(e=>!e.appearance?.overlays?.consumed)?.id||objects[0].id));}else selectTile(tile);});
canvas.addEventListener('dblclick',event=>{if(!playing||!world)return;const tile=tileAt(event.clientX,event.clientY);if(selectedTile?.x===tile.x&&selectedTile?.y===tile.y)goTo(tile.x,tile.y);});
window.addEventListener('keydown',event=>{if(event.key==='Escape'){stopWalking();if(activeActionKey){const form=$('selection-actions').querySelector('.descriptor-action');if(form){actionDrafts.delete(form.dataset.entityId+':'+form.dataset.action);form.remove();}activeActionKey=null;lastSelectionSignature='';renderSelection();}else closePanels();canvas.focus({preventScroll:true});return;}const pressed=event.key.length===1?event.key.toLowerCase():event.key;if(!playing||!directions[pressed]||event.altKey||event.ctrlKey||event.metaKey||['INPUT','TEXTAREA','SELECT','BUTTON'].includes(document.activeElement?.tagName))return;event.preventDefault();if(event.repeat||held===pressed)return;held=pressed;walkPath=[];pathTarget=null;run(walk);});
window.addEventListener('keyup',event=>{const released=event.key.length===1?event.key.toLowerCase():event.key;if(held===released){held=null;clearTimeout(walkTimer);}});window.addEventListener('blur',stopWalking);
for(const el of document.querySelectorAll('[data-direction]'))el.addEventListener('click',()=>{stopWalking();if(point())run(()=>move(...el.dataset.direction.split(',').map(Number)));});
$('entry-form').addEventListener('submit',event=>{event.preventDefault();run(enter);});$('entry-cancel').addEventListener('click',()=>run(cancelEntry));
$('import-key').addEventListener('change',event=>run(async()=>{
  const input=event.target,file=input.files?.[0];if(!file)return;
  try{
    if(file.size>16384)throw new Error('恢复文件过大，请选择原身份 JSON 文件。');
    const restored=await validateKey(JSON.parse(await file.text()));
    importedKey=restored;key=restored;$('error').hidden=true;
    $('identity-status').textContent='已选中恢复身份。进入后会保存它并接续原角色。';
  }finally{input.value='';}
}));
$('refresh').addEventListener('click',()=>run(async()=>{
  stopWalking();const epoch=session,worldRead=await refreshWorld(true);if(epoch!==session||!playing)return;
  const id=selectedId,selectedRead=id?await select(id,selectedFromBag,true):null;if(directoryDefinition&&directoryType)await readFabricationMenu();if(epoch!==session||!playing)return;
  const selectedReadCurrent=id&&selectedId===id&&selectedRead===inspection&&inspection?.entity?.id===id&&!inspection.reading&&!inspectionError;
  if(worldRead===true)announce(selectedReadCurrent?'已读取最新附近现场与所选物件。':'已读取最新附近现场。');else if(selectedReadCurrent)announce('已读取所选物件；附近现场尚未刷新。');
}));
$('backup').addEventListener('click',()=>{if(!key)return;const blob=new Blob([JSON.stringify(key,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=node('a');a.href=url;a.download='shared-grove-identity.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);announce('恢复文件包含你的身份密钥，请保存在自己的安全位置。');});
$('follow-player').addEventListener('click',()=>{if(point()){camera={x:point().x,y:point().y};canvas.focus({preventScroll:true});}});$('dismiss-onboarding').addEventListener('click',()=>{$('onboarding').hidden=true;});
$('selection-close').addEventListener('click',()=>{rememberActionDrafts();closePanels();canvas.focus({preventScroll:true});});
$('context-toggle').addEventListener('click',()=>{if(document.body.classList.contains('context-open'))closePanels();else openContext();});
$('inventory-toggle').addEventListener('click',()=>{const open=!$('inventory-panel').open;rememberActionDrafts();closePanels();$('inventory-panel').open=open;document.body.classList.toggle('inventory-open',open);$('inventory-toggle').setAttribute('aria-expanded',String(open));});
$('inventory-panel').addEventListener('toggle',()=>{document.body.classList.toggle('inventory-open',$('inventory-panel').open);$('inventory-toggle').setAttribute('aria-expanded',String($('inventory-panel').open));});
$('inventory-first').addEventListener('click',()=>run(()=>changeInventoryPage('first')));
$('inventory-previous').addEventListener('click',()=>run(()=>changeInventoryPage('previous')));
$('inventory-next').addEventListener('click',()=>run(()=>changeInventoryPage('next')));
$('public-works-panel').addEventListener('toggle',()=>{if($('public-works-panel').open&&!publicWorksPage&&!publicWorksBusy&&playing)run(()=>readPublicWorks());});
$('public-works-form').addEventListener('submit',event=>{event.preventDefault();run(()=>readPublicWorks({filters:publicWorksFiltersFromInputs()}));});
$('public-works-recent').addEventListener('click',()=>{$('public-works-author').value='';$('public-works-name').value='';run(()=>readPublicWorks({filters:{}}));});
$('public-works-refresh').addEventListener('click',()=>run(()=>readPublicWorks({cursors:publicWorksCursors})));
$('public-works-previous').addEventListener('click',()=>{if(publicWorksCursors.length>1)run(()=>readPublicWorks({cursors:publicWorksCursors.slice(0,-1)}));});
$('public-works-next').addEventListener('click',()=>{if(publicWorksPage?.has_more&&publicWorksPage.next_cursor)run(()=>readPublicWorks({cursors:[...publicWorksCursors,publicWorksPage.next_cursor]}));});
$('public-work-marker-clear').addEventListener('click',()=>{publicWorkMarker=null;renderPublicWorks();});
$('agent-toggle').addEventListener('click',()=>{const open=!document.body.classList.contains('agent-open');rememberActionDrafts();if(open){openAgent();$('chat-text').focus({preventScroll:true});}else closePanels();});
$('agent-close').addEventListener('click',()=>{closePanels();canvas.focus({preventScroll:true});});
$('ask-selection').addEventListener('click',()=>run(async()=>{if(!world)return;const entity=selectedEntity(),tile=selectedTile,location=entityLocation(entity);const text=entity?'我正在看'+entity.name+'（'+(location.x??point().x)+'，'+(location.y??point().y)+'）。请读取当前现场，解释它能怎样用，以及不同办法的材料和环境取舍。':tile?'我正在看位置（'+tile.x+'，'+tile.y+'）。请读取当前现场，解释在这里走动或搭建设施有哪些取舍。':'请读取我身边的现场，解释我现在有哪些不同的办法。';openAgent();await hostCommand('prompt',{text,intent:$('host-intent').value.trim(),...(entity?{context_entity_id:entity.id}:{})});}));
$('recover-operation').addEventListener('click',()=>run(()=>recoverWorldOperation()));
$('release-operation').addEventListener('click',()=>run(()=>recoverWorldOperation(true)));
$('reconnect-world').addEventListener('click',()=>run(async()=>{const epoch=session;await authenticate(key);if(epoch!==session)return;if(await hostCommand('recover',{token:client.token,public_key:key.public_key,name:point()?.name||prefs().name},{refresh:true})){scheduleWorld();scheduleHost();}}));
$('chat-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const text=$('chat-text').value.trim();if(!text)return;const entity=selectedEntity();if(await hostCommand('prompt',{text,intent:$('host-intent').value.trim(),...(entity?{context_entity_id:entity.id}:{})})){$('chat-text').value='';client.saveDraft('grove-chat','');intentDirty=false;}});});
$('execution-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{
  const goal=$('execution-goal').value.trim(),maxActions=Number($('execution-max-actions').value),maxRequests=Number($('execution-max-requests').value);
  if(!goal)return;
  const interactionMode=$('execution-interaction-mode').value;
  if(!['play','full'].includes(interactionMode))throw new Error('请选择委托方式。');
  if(goal.length>2000)throw new Error('委托目标最多 2000 字。');
  const limits=executionLimits();if(!limits)throw new Error('执行额度尚未读取，请恢复连接。');
  if(!Number.isInteger(maxActions)||maxActions<1||maxActions>limits.max_actions)throw new Error('动作上限须为 1 至 '+limits.max_actions+' 的整数。');
  if(!Number.isInteger(maxRequests)||maxRequests<1||maxRequests>limits.max_requests)throw new Error('模型请求上限须为 1 至 '+limits.max_requests+' 的整数。');
  stopWalking();const entity=selectedEntity();
  // This choice belongs to the private host delegation, never public world facts.
  if(await hostCommand('execute',{goal,interaction_mode:interactionMode,max_actions:maxActions,max_requests:maxRequests,compact_history:$('execution-compact-history').checked,...(entity?{context_entity_id:entity.id}:{})})){goalDirty=false;$('execution-compact-history').checked=false;client.saveDraft('grove-goal',goal);announce('目标已交给你的 Agent。它可在总额度内接续执行，你可以随时收回后续行动权。');}
});});
$('execution-goal').addEventListener('input',()=>{goalDirty=true;if(current())try{client.storage.setItem(client.draftKey('grove-goal'),$('execution-goal').value);}catch{}});
$('execution-resume').addEventListener('click',()=>run(async()=>{
  const execution=host?.execution||{};
  const additional=Number($('execution-additional-requests').value),admission=continuationAdmission();
  if(!admission.allowed){announce(admission.reason,true);return;}
  stopWalking();
  if(await hostCommand('execute-resume',{task_id:execution.task_id,compact_history:$('execution-compact-history').checked,additional_requests:additional})){$('execution-compact-history').checked=false;$('execution-additional-requests').value='0';announce('已再次授权接续原目标；保留原行动记录与已用额度，先读取最新现场。');}
}));
$('execution-additional-requests').addEventListener('input',renderExecution);
$('execution-reclaim').addEventListener('click',()=>run(reclaimExecution));$('execution-reclaim-quick').addEventListener('click',()=>run(reclaimExecution));
$('execution-recover').addEventListener('click',()=>run(()=>hostCommand('recover',{token:client.token,public_key:key.public_key,name:point()?.name||prefs().name},{refresh:true})));
$('rule-catalog-read').textContent='浏览已发表规则与成型';
$('rule-catalog-read').closest('.rule-catalog').querySelector('summary').textContent='规则目录与材料成型';
$('rule-catalog-read').addEventListener('click',()=>run(async()=>{
  if(directoryBusy||fabricationBusy||!playing||!current())return;const epoch=session,control=$('rule-catalog-read');control.disabled=true;
  try{await readDefinitionDirectory();if(epoch!==session||!playing||directoryDefinition)return;let saved;try{saved=JSON.parse(client.draft('grove-fabrication-selection')||'null');}catch{}if(saved?.definition_ref&&saved?.type_id)await readPublishedDefinition(saved.definition_ref,{type_id:saved.type_id});}
  finally{if(epoch===session)control.disabled=false;}
}));
$('generation-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const text=$('generation-text').value.trim();if(!text)return;if(await hostCommand('generate',{text,kind:$('generation-kind').value,intent:$('host-intent').value.trim()}))intentDirty=false;});});
$('intent-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const intent=$('host-intent').value.trim();if(await hostCommand('context',{intent})){intentDirty=false;client.saveDraft('grove-intent',intent);announce('想法已保存在本机，供你的 Agent 下一轮使用。未调用模型，未发表到世界。');}});});
$('host-intent').addEventListener('input',()=>{intentDirty=true;if(current())try{client.storage.setItem(client.draftKey('grove-intent'),$('host-intent').value);}catch{}});
$('generation-recover').addEventListener('click',()=>run(()=>hostCommand('recover',{token:client.token,public_key:key.public_key,name:point()?.name||prefs().name},{refresh:true})));
$('chat-text').addEventListener('input',()=>{if(current())try{client.saveDraft('grove-chat',$('chat-text').value);}catch{}});
$('generation-text').addEventListener('input',()=>{if(current())try{client.saveDraft('grove-generation',$('generation-text').value);}catch{}});
$('generation-kind').addEventListener('change',()=>{$('generation-text').placeholder={layout:'结合这里已经发生的变化，留下能让我尝试另一种办法的内容。',rain:'我想观察雨水怎样改变这里；请按当前机会决定能留下什么。',drift:'请依据这条溪和已有内容安排一件可交互的漂来物。'}[$('generation-kind').value]||'说说你希望尝试什么。';});
$('agent-cancel').addEventListener('click',()=>run(async()=>{if(await hostCommand('cancel',{}, {interrupt:true,refresh:true}))announce('已请求中止当前 Agent 请求并收回后续行动权。已提交变化保留，是否完成收回以当前状态为准。');}));
$('generation-cancel').addEventListener('click',()=>run(async()=>{if(await hostCommand('cancel',{}))announce('已请求中止创作；已经发表的内容保留，结果以世界读回为准。');}));
$('agent-disconnect').addEventListener('click',()=>run(async()=>{if(await hostCommand('disconnect',{})){clearTimeout(hostTimer);announce('Agent 已断开。仍可以亲手走动和操作。');}}));$('agent-reconnect').addEventListener('click',()=>run(restartHost));
$('leave').addEventListener('click',()=>{
  worldFresh=false;mechanicalMotion.reset();lastWorldRead=0;
  saveFabricationDraft();stopWalking();++session;++readEpoch;++hostReadEpoch;++inspectEpoch;++hostCommandEpoch;++definitionReadEpoch;++directoryEpoch;++publicWorksEpoch;
  playing=false;entering=false;actionBusy=false;hostCommandBusy=false;executionReclaimBusy=false;
  activeRainChunks.clear();factDisclosures.clear();mapLabelSlots.clear();actionDrafts.clear();activeActionKey=null;hostCommandKind=null;hostCommandFlight=null;
  clearTimeout(worldTimer);clearTimeout(hostTimer);client.cancel();client.token=null;identity=null;world=null;inspection=null;inspectionError=null;inspectionErrorStatus=null;inspectionFlight=null;selectedId=null;selectedTile=null;combinationCheckingForm=null;
  ledger.clear();streamCursor=null;lastSelectionSignature='';lastInventorySignature='';lastChatSignature='';lastGenerationSignature='';lastContentSignature='';contentHistoryId=null;contentHistoryPages=[];contentHistoryBusy=false;operationRecoveryBusy=false;inventoryPageBusy=false;inventoryPageCursors=[null];intentDirty=false;goalDirty=false;
  definitionReadBusy=false;definitionReadValue=null;definitionReadError=null;definitionReadRef=null;definitionReadOpen=false;$('rule-catalog-result').replaceChildren();$('rule-catalog-read').disabled=false;
  directoryBusy=false;directoryFilters={};directoryInputs={author:'',key:''};directoryPage=null;directoryCursors=[null];directoryError=null;directoryOpen=false;directoryDefinition=null;directoryDefinitionRef=null;directoryType=null;fabricationMenu=null;fabricationBusy=false;fabricationRows=[];fabricationError=null;
  publicWorksBusy=false;publicWorksPage=null;publicWorksFilters={};publicWorksCursors=[null];publicWorksError=null;publicWorksReadAt=0;publicWorksRenderSignature='';publicWorkMarker=null;$('public-works-author').value='';$('public-works-name').value='';$('public-works-panel').open=false;renderPublicWorks();
  closePanels();document.body.classList.remove('playing');$('game').hidden=true;$('entry').hidden=false;$('world-status').textContent='已离开 · 可接续';$('entry-progress').replaceChildren();$('enter').disabled=false;
  announce('已离开网页。身份、目标进度与变化保留；已委托行动仍受这个目标的总额度约束。离开网页不等于收回行动权。');
});
function suspendScene(){worldFresh=false;mechanicalMotion.reset();++readEpoch;client.world.cancelReads();refreshFlight=null;stopWalking();}
window.addEventListener('resize',resize);window.addEventListener('pagehide',()=>{saveFabricationDraft();suspendScene();client.cancel();});document.addEventListener('visibilitychange',()=>{if(document.hidden)suspendScene();else if(playing){run(()=>refreshWorld(true));scheduleHost(250);}});
window.addEventListener('pageshow',event=>{if(event.persisted&&playing){run(()=>refreshWorld(true));scheduleWorld();scheduleHost(250);}});
new ResizeObserver(resize).observe(canvas);requestAnimationFrame(draw);readIdentity();
