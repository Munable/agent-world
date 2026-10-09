import {createKey,validateKey,savedKey} from '/static/commons-client.js';
import {EventLedger} from '/static/stream-client.js';
import {GroveClient} from './grove-client.js';
import {matterLots,bodyQuantities,materialLabel,slotLabel,lifecycleLabel,entitySummary,simulationStats,entityGlyph as genericGlyph,safePresentation,actionIds,actionDescriptors,descriptorLabel,descriptorCandidates,candidateLabel,candidatePreviewLines,previewVersionsChanged,descriptorLots,quantityBounds,factLabel,factValue,creativeState,ruleChoices,conditionObservationLines,reactionObservationLines,operationIntentLabel} from './grove-world-ui.js?v=playable-context';

const $=id=>document.getElementById(id);
const client=new GroveClient();
const ledger=new EventLedger(100);
const canvas=$('world-canvas'),ctx=canvas.getContext('2d');
const directions={w:[0,-1],a:[-1,0],s:[0,1],d:[1,0],ArrowUp:[0,-1],ArrowLeft:[-1,0],ArrowDown:[0,1],ArrowRight:[1,0]};
const actionNames={pickup:'拿进背包',drop:'放到脚下',open:'打开',close:'合上',store:'存入',retrieve:'取出',fill:'装水',pour:'倒水',ignite:'点燃',harvest:'采集一份',tend:'用水照料',fuel:'添入木料',dismantle:'拆回材料',switch_mode:'切换模式',attach:'绑定部件',detach:'解绑到地面',launch:'下水',transfer:'转移材料'};
const materialNames={wood:'木',stone:'石',cloth:'布',metal:'金属',water:'水',leather:'皮革'};
const hostNames={disconnected:'已断开',connecting:'正在连接',ready:'已连接',thinking:'思考中',paused:'已暂停',error:'连接受阻'};
let key=null,identity=null,world=null,host=null,session=0,readEpoch=0,worldTimer=null,hostTimer=null;
let entering=false,playing=false,actionBusy=false,refreshFlight=null,streamCursor=null,lastWorldRead=0;
let selectedId=null,selectedTile=null,inspection=null,inspectionError=null,inspectEpoch=0,selectedFromBag=false,held=null,walkPath=[],pathTarget=null,walkTimer=null;
let tileSize=40,canvasWidth=600,canvasHeight=600,camera={x:0,y:0},motion=null,hoverTile=null,lastChatSignature='',lastHostSignature='',lastSelectionSignature='',lastInventorySignature='',lastAction='';
let importedKey=null;
let hostReadEpoch=0,lastGenerationSignature='',hostCommandBusy=false;
let lastContentSignature='',intentDirty=false,contentHistoryId=null,contentHistoryPages=[],contentHistoryBusy=false,operationRecoveryBusy=false;
let inventoryPageBusy=false,inventoryPageCursors=[null];
let combinationCheckingForm=null,inspectionFlight=null,inspectionErrorStatus=null;
const chosenBuildMaterials=new Map();
const touchedBuildChoices=new Set();
let activeRainChunks=new Set();
let goalDirty=false,executionReclaimBusy=false,hostCommandEpoch=0,manualControlRecoveryPending=false;
let definitionReadEpoch=0,definitionReadBusy=false,definitionReadValue=null,definitionReadError=null,definitionReadRef=null,definitionReadOpen=false;
const factDisclosures=new Map();
let activeActionKey=null,hostCommandKind=null,hostCommandFlight=null;
const actionDrafts=new Map();
const terrainNames={grass:'草地',dirt:'泥地',water:'浅溪',rock:'岩壁'};
const readableCandidate=candidate=>({...entityById(candidate?.id),...candidate});
const actionKey=descriptor=>(descriptor.function_id||'grove.interact')+':'+descriptor.action;
function friendlyInstruction(value){return String(value||'').replace(/\bfeedstock\b/g,'原料').replace(/\bsymbol\b/g,'材料名称').replace(/\blot_id\b/g,'材料批次').replace(/\bamount\b/g,'数量').replace(/\bitem_id\b/g,'来源物件').replace(/\btransfer\b/g,'转入材料').replace(/\bstore\b/g,'整件放入').replace(/\brelease\b/g,'领取').replace(/\bcontained\b/g,'内含').replace(/\blining\b/g,'衬层').replace(/(\d+)u(wood|cloth|stone|water)\b/g,(_,quantity,material)=>quantity+' 单位'+materialLabel(material));}
function displayWorldText(value){return friendlyInstruction(value).replace(/program_[0-9a-f]{64}#[A-Za-z][A-Za-z0-9_]*/g,id=>materialLabel(id,selectedEntity())).replace(/(?:object_|seed_|item_|awr_)[A-Za-z0-9_]+/g,id=>entityById(id)?.name||'该物件');}
function rememberActionDrafts(){
  for(const form of $('selection-actions').querySelectorAll('.descriptor-action')){
    const values=[...form.querySelectorAll('input,select,textarea')].map(control=>({value:control.value,checked:control.checked,text:control.selectedOptions?.[0]?.textContent}));
    actionDrafts.set(form.dataset.entityId+':'+form.dataset.action,values);
  }
  if(actionDrafts.size>32)actionDrafts.delete(actionDrafts.keys().next().value);
}
function restoreActionDraft(form,update){
  const values=actionDrafts.get(form.dataset.entityId+':'+form.dataset.action);if(!values)return;
  const controls=[...form.querySelectorAll('input,select,textarea')];
  const restore=(control,index)=>{const saved=values[index];if(!saved)return;if(control.tagName==='SELECT'&&saved.value&&![...control.options].some(option=>option.value===saved.value)){const option=node('option',(saved.text||'原选择')+' · 当前不可用，请重选');option.value=saved.value;option.disabled=true;control.append(option);}control.value=saved.value;if(control.type==='checkbox')control.checked=saved.checked;};
  controls.forEach(restore);
  update(true);
  // Rebuilding source lots must not silently replace a previously chosen batch.
  controls.forEach(restore);update(false);
}
function renderTargetChoices(location){
  const area=$('target-choice');if(!area)return;area.replaceChildren();
  const objects=ground().filter(e=>e.x===location?.x&&e.y===location?.y);
  area.hidden=!objects.length;if(!objects.length)return;
  area.append(node('span','这里有','small'));
  for(const entity of objects){const pick=button(entity.name,()=>run(()=>select(entity.id)),{secondary:true});pick.setAttribute('aria-pressed',String(selectedId===entity.id));area.append(pick);}
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
function button(text,handler,{disabled=false,secondary=false}={}){const el=node('button',text,secondary?'secondary':'');el.type='button';el.disabled=disabled;el.addEventListener('click',()=>handler());return el;}
function announce(text,error=false){const el=$(error?'error':'notice');el.textContent=text;el.hidden=!text;if(!error&&text)setTimeout(()=>{if(el.textContent===text)el.hidden=true;},6500);}
function fail(error){if(error.cancelled)return;const message=['Failed to fetch','Load failed','NetworkError when attempting to fetch resource.'].includes(error.message)?(client.pending()?'连接中断。重连后先核对原操作结果。':'连接中断，请检查连接后重试。'):error.message||String(error);announce(message,true);if(error.status===401||error.status===403){client.token=null;held=null;walkPath=[];$('reconnect-world').hidden=false;$('world-status').textContent='网页需要重连';$('movement-feedback').textContent='授权失效：同钥重连后继续。';}renderPending();}
function run(fn){return Promise.resolve().then(fn).catch(fail);}
const current=()=>identity&&client.token;
const point=()=>world?.player;
const entityLocation=entity=>entity?.location||{x:entity?.x,y:entity?.y};
const distance=(entity)=>entity?.owner===client.roleId?0:Math.abs((entityLocation(entity).x??Infinity)-(point()?.x??0))+Math.abs((entityLocation(entity).y??Infinity)-(point()?.y??0));
const inventory=()=>world?.inventory||[];
const ground=()=>world?.entities||[];
const entityById=id=>ground().find(e=>e.id===id)||inventory().find(e=>e.id===id)||(inspection?.entity?.id===id?inspection.entity:null)||inspection?.contents?.find(e=>e.id===id)||inspection?.attachments?.find(e=>e.id===id)||(world?.public_content?.anchors||[]).find(anchor=>anchor.entity_id===id)?.entity;
const selectedEntity=()=>selectedId?(inspection?.entity?.id===selectedId?inspection.entity:entityById(selectedId)):null;
const caps=e=>e?.capabilities||[];
const blocks=e=>caps(e).includes('blocking')&&!e.state?.destroyed&&(e.components?.barrier?.active??e.barrier_active)!==false;
const finite=(value,fallback=0)=>Number.isFinite(value)?value:fallback;
const recipes=()=>world?.construction?.recipes||[];
const materialItems=material=>inventory().filter(e=>e.construction_material?.eligible===true&&e.construction_material.material===material);
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
  }catch(error){if(epoch===session){$('agent-status').textContent='宿主暂不可达';$('agent-detail').textContent=manualControlRecoveryPending?'原行动结果尚未核对；恢复宿主后先核对原回执，再继续行动。':'你的世界操作仍可继续。恢复宿主后再提问或共创。';if(entering){entering=false;$('enter').disabled=false;fail(error);}}}
  finally{scheduleHost();}
}
function scheduleHost(delay=1200){clearTimeout(hostTimer);if(current()&&(entering||playing)&&host?.status!=='disconnected')hostTimer=setTimeout(pollHost,delay);}
function renderHost(){
  if(!host)return;const busy=hostCommandBusy||host.reclaim_in_progress===true||['connecting','thinking'].includes(host.status);
  const execution=host.execution;
  // Recovery is asynchronous: clear the local gate only from settled receipts
  // and a closed world grant, never from the /recover acknowledgement alone.
  if(manualControlRecoveryPending&&['ready','paused'].includes(host.status)&&host.reclaim_in_progress===false&&host.unknown_operations===0&&
    execution?.delegation_active===false&&['idle','revoked','reclaimed','exhausted'].includes(execution.control_status)&&
    !['starting','running','unknown'].includes(execution.status)&&execution.request_status==='idle_confirmed'&&execution.settlement?.status==='idle_confirmed'&&
    execution.readback_actual===true&&Number.isInteger(execution.actions_count)&&execution.readback_actions_count===execution.actions_count&&
    Array.isArray(execution.receipts)&&execution.receipts.every(receipt=>['confirmed','rejected'].includes(receipt.status)))manualControlRecoveryPending=false;
  renderModelChoices();$('game-host').value=host.host_url||'';
  $('agent-status').textContent=hostNames[host.status]||host.status||'状态待核对';$('agent-status').dataset.status=host.status;
  $('agent-detail').textContent=manualControlRecoveryPending?'收回或原行动结果尚未核对，连接恢复后先核对原回执，再继续行动。':host.error?.message||(host.reclaim_in_progress?'行动权已收回，原模型请求正在收尾。已确认的后果保留，可以亲手继续。':busy?'你的 Agent 正在读取、解释、创作或执行已授权目标。你可以继续亲手操作世界。':host.entered?'你的 Agent 已读取世界。普通提问只解释现场；明确委托目标后，它可以用同一角色行动。':'完成首次真实回应与现场读取后，即可进入。');
  if(!intentDirty&&typeof host.context?.intent==='string'&&document.activeElement!==$('host-intent'))$('host-intent').value=host.context.intent;
  $('intent-save').disabled=hostCommandBusy;
  $('chat-send').disabled=busy||!['ready','paused'].includes(host.status);$('agent-cancel').hidden=!busy;$('agent-reconnect').hidden=!['disconnected','error','paused'].includes(host.status);$('agent-disconnect').disabled=host.status==='disconnected';
  const generationPending=['unknown','published_readback_pending'].includes(host.generation?.status);
  $('generation-send').disabled=busy||generationPending||!['ready','paused'].includes(host.status);$('generation-recover').hidden=!generationPending;$('generation-recover').disabled=busy;renderGeneration();
  $('generation-cancel').hidden=!busy||!['claiming','generating','correcting','publishing'].includes(host.generation?.status);
  const messages=host.messages||[];
  const lastReply=[...messages].reverse().find(m=>!['user','event'].includes(m.role)&&m.text);
  const failed=host.status==='error';
  $('companion-update').hidden=!failed&&!busy&&!lastReply;
  $('companion-update').textContent=manualControlRecoveryPending?'原行动结果尚未核对；恢复连接后先核对原回执，再继续行动。':failed?'当前 Agent 请求未完成：'+previewMessage(host.error?.message||'回应暂不可用，请打开“目标与交谈”查看。'):busy?'你的 Agent 正在看现场。你可以继续走动与操作。':lastReply?'Agent 回应：'+previewMessage(lastReply.text):'';
  const signature=JSON.stringify(messages);
  if(signature!==lastChatSignature){lastChatSignature=signature;$('agent-messages').replaceChildren(...messages.slice(-12).map(m=>{const el=node('div',undefined,'chat-message '+(m.role==='user'?'user':m.role==='event'?'event':'agent'));el.append(node('small',m.role==='user'?'你':m.role==='event'?'现场变化':'你的 Agent'),messageNode(m.text));return el;}));$('agent-messages').scrollTop=$('agent-messages').scrollHeight;}
  if(!messages.length&&host.text)$('agent-messages').replaceChildren(messageNode(host.text));
  $('auto-summary').textContent='普通操作与现场刷新不调用模型；提问、创作与明确委托使用你自己的模型连接。';
  const progress=(host.progress||[]).slice(-6);const entrySignature=JSON.stringify([host.status,progress,host.error]);
  if(entrySignature!==lastHostSignature){lastHostSignature=entrySignature;$('entry-progress').replaceChildren(...progress.map(p=>node('p',p.message||p.kind||'')));if(host.error)$('entry-progress').append(node('p',host.error.message,'error-text'));}
  renderExecution();
}
async function hostCommand(path,body,{refresh=false,interrupt=false}={}){
  if(hostCommandBusy&&!interrupt||!playing||!current())return false;
  const epoch=session,command=++hostCommandEpoch;++hostReadEpoch;hostCommandBusy=true;hostCommandKind=path;renderHost();
  try{hostCommandFlight=client.connector(path,body);const value=await hostCommandFlight;if(epoch!==session||command!==hostCommandEpoch||!playing)return false;host=value;renderHost();if(refresh)await refreshWorld(true);return epoch===session&&playing;}
  catch(error){if(epoch!==session||command!==hostCommandEpoch||!playing)return false;throw error;}
  finally{if(epoch===session&&command===hostCommandEpoch){hostCommandBusy=false;hostCommandKind=null;hostCommandFlight=null;renderHost();scheduleHost(250);}}
}
function renderExecution(){
  const execution=host?.execution||{},status=execution.status||'idle',active=execution.delegation_active===true,busy=['starting','running'].includes(status),unknown=manualControlRecoveryPending||status==='unknown'||Number(host?.unknown_operations)>0||(execution.receipts||[]).some(receipt=>receipt.status==='unknown');
  const goalResult=execution.goal_result||{},goalClaimed=status==='completed'&&!!execution.task_id&&goalResult.status==='completed'&&goalResult.completion_source==='agent_claim';
  const labels={idle:'尚未委托',starting:'正在授权',running:'正在执行目标',completed:'Agent报告完成',result_unconfirmed:'目标结果未确认',paused:'目标已暂停',blocked:'目标受阻',error:'执行受阻',unknown:'结果待核对',budget_exhausted:'目标额度已用完'};
  const displayedStatus=unknown?'unknown':status==='completed'&&!goalClaimed?'result_unconfirmed':status,requestLabels={idle:'尚未开始',preparing:'正在读取现场',summarizing:'正在整理会话历史',summary_failed:'会话历史整理失败',running:'推理与行动中',returned:'这一段请求已返回',timed_out:'这一段请求超时',settling:'正在核对原结果',idle_confirmed:'原请求已停止并核对',unsettled:'原请求状态待核对'};
  $('execution-status').textContent=labels[displayedStatus]||displayedStatus;$('execution-status').dataset.status=displayedStatus;
  if(!goalDirty&&typeof execution.goal==='string'&&document.activeElement!==$('execution-goal'))$('execution-goal').value=execution.goal;
  $('execution-current-goal').textContent=execution.goal?'委托目标：'+execution.goal:'明确给出目标后才允许 Agent 行动。保存想法或普通提问不会授权。';
  const count=Number.isInteger(execution.actions_count)?String(execution.actions_count):'待读取',limit=Number.isInteger(execution.max_actions)?String(execution.max_actions):'待读取';
  const requests=Number.isInteger(execution.requests_used)?String(execution.requests_used):'待读取',requestLimit=Number.isInteger(execution.max_requests)?String(execution.max_requests):'待读取';
  $('execution-budget').textContent=execution.goal?'世界动作 '+count+' / '+limit+(execution.task_id?' · 这个目标模型请求 '+requests+' / '+requestLimit:' · 原请求记录，尚无目标接续额度'):'';
  $('execution-request').textContent=execution.goal&&execution.task_id?'当前请求：'+(requestLabels[execution.request_status]||execution.request_status||'待读取')+(execution.budget?' · 每段建议 '+execution.budget.max_steps+' 个推理步骤、最多 '+execution.budget.max_tool_calls+' 次世界读取或行动':''):'';
  const recovery=execution.history_recovery||{},recoveryLabels={not_started:recovery.requested?'尚未开始整理会话历史':'未请求整理会话历史',running:'正在整理会话历史',completed:'会话历史整理完成；目标仍以行动与核对结果判断',failed:'会话历史整理失败；本目标不会自动重试整理',uncertain:'会话历史整理结果待核对；本目标不会自动重试整理'};
  $('execution-history-recovery').hidden=!execution.task_id||!recovery.status;
  $('execution-history-recovery').textContent=[recoveryLabels[recovery.status]||recovery.status,recovery.attempts>0?'摘要请求已计入这个目标的模型请求总额度，原记录保留。':'',recovery.error?.message].filter(value=>typeof value==='string'&&value).join('\n');
  const detail=unknown?'原行动结果未确认。先核对原回执，才可接续或开始新目标；核对不会自动继续行动。':goalClaimed?'Agent已结束本次委托。这是它对目标的判断；程序确认的具体世界结果另列。':status==='completed'?'原执行记录已结束，目标结果未确认。已经保存的行动保留；旧完成记录没有验证来源，不能将请求结束视为成功。':active?'行动授权仍有效。Agent 可在总额度内继续；收回或亲手行动会停止后续行动，已保存的结果保留。':execution.resume_allowed===true?'当前没有行动授权。点击“接续这个目标”才再次授权；它会读取最新现场，继续原目标与剩余额度。':execution.goal?'当前没有行动授权。已保存的行动保留；请求结束或额度用完不代表目标完成。':'人可以直接玩，也可以在这里明确授权 Agent 帮你实现目标。';
  $('execution-detail').textContent=[execution.stop_reason,execution.error?.message,detail].filter((value,index,values)=>typeof value==='string'&&value&&values.indexOf(value)===index).join('\n');
  const progress=execution.progress||{},progressNodes=[];
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
  $('execution-actions').replaceChildren(...receipts.slice(-8).map(receipt=>{const row=node('div',undefined,'execution-receipt');row.append(node('strong',(functionLabels[receipt.function]||'世界行动')+' · '+(receiptLabels[receipt.status]||receipt.status||'状态未读')));return row;}));
  if(receipts.length>8)$('execution-actions').append(node('p','显示最近 8 个回执，共 '+receipts.length+' 个；它们记录行动，不替代当前现场。','small'));
  if(execution.readback_actual===true)$('execution-actions').append(node('p','这个目标已有实际世界读回。物件的最新状态仍以地图与详情为准。','small'));
  $('execution-start').disabled=hostCommandBusy||host.reclaim_in_progress===true||busy||active||unknown||!['ready','paused'].includes(host?.status)||actionBusy||!!client.pending();
  const additionalControl=$('execution-additional-requests'),additionalLimit=Math.max(0,8-(execution.max_requests||0));
  additionalControl.max=String(additionalLimit);additionalControl.disabled=execution.extension_allowed!==true;
  if(Number(additionalControl.value)>additionalLimit)additionalControl.value=String(additionalLimit);
  const additional=Number(additionalControl.value),canResume=execution.resume_allowed===true||(execution.extension_allowed===true&&Number.isInteger(additional)&&additional>0&&additional<=additionalLimit);
  $('execution-resume').hidden=(execution.resume_allowed!==true&&execution.extension_allowed!==true)||!execution.task_id||unknown;
  $('execution-resume').disabled=!canResume||hostCommandBusy||host.reclaim_in_progress===true||executionReclaimBusy||busy||active||unknown||['connecting','thinking','disconnected'].includes(host?.status)||actionBusy||!!client.pending();
  $('execution-reclaim').disabled=executionReclaimBusy||!(active||busy||unknown);
  $('execution-reclaim').textContent=executionReclaimBusy?'正在收回…':'收回行动权';
  $('execution-recover').hidden=!unknown;$('execution-recover').disabled=hostCommandBusy||executionReclaimBusy;
  const summary=unknown?'有原行动待核对':active?'Agent 正在为你行动 · 随时可接回':executionReclaimBusy?'正在接回角色…':'你正在亲手操作';
  $('execution-summary').textContent=summary;$('execution-reclaim-quick').hidden=!(active||busy||unknown);$('execution-reclaim-quick').disabled=executionReclaimBusy;
}
async function reclaimExecution(){
  if(executionReclaimBusy||!playing||!current())return;
  const epoch=session;stopWalking();executionReclaimBusy=true;renderExecution();
  try{if(await takeManualControl(()=>epoch===session&&playing&&current()))announce('行动权已收回。已保存的行动保留，你可以亲手接着玩。');}
  finally{if(epoch===session){executionReclaimBusy=false;if(playing)renderExecution();}}
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
      return readAccepted;
    }
  })();refreshFlight=task;try{return await task;}finally{if(refreshFlight===task)refreshFlight=null;}
}
function scheduleWorld(delay=1200){clearTimeout(worldTimer);if(playing&&current())worldTimer=setTimeout(async()=>{if(!actionBusy&&!document.hidden)await refreshWorld();scheduleWorld();},delay);}
function acceptWorld(value){
  if($('movement-feedback').textContent==='世界暂不可达；显示上次现场。刷新后继续。')$('movement-feedback').textContent='已重新读取现场，可以继续。';
  if(client.inventoryWasReset){inventoryPageCursors=[null];client.inventoryWasReset=false;announce('背包清单已改变，已重新读取第一页。原操作与身份保留。');}
  const previous=world?.player;world=value;
  activeRainChunks=new Set((value.processes||[]).filter(p=>p.kind==='rain'&&p.status==='active'&&p.start_tick<=finite(value.clock?.sequence)).flatMap(p=>p.chunk_ids||[]));
  if(previous&&(previous.x!==value.player.x||previous.y!==value.player.y))motion={from:{x:previous.x,y:previous.y},to:{x:value.player.x,y:value.player.y},start:performance.now(),duration:160};
  if(!previous){camera.x=value.player.x;camera.y=value.player.y;}
  $('player-name').textContent=value.player.name;$('position').textContent='坐标 '+value.player.x+'，'+value.player.y;
  $('freshness').textContent='刚刚读取';$('map-scale').textContent='只显示身边的区域'+(value.truncated?' · 更多物件需靠近查看':'');
  const rain=(value.processes||[]).find(p=>p.kind==='rain'&&p.status==='active');
  $('weather').textContent=rain?(rain.start_tick>finite(value.clock?.sequence)?'附近短雨将到来':'附近有雨 · 蓝色覆盖区以世界状态为准'):'附近无雨 · 湿料会继续干燥';
  if(selectedId){
    const projected=ground().find(entity=>entity.id===selectedId)||inventory().find(entity=>entity.id===selectedId)||(value.public_content?.anchors||[]).find(anchor=>anchor.entity_id===selectedId)?.entity;
    const inaccessible=!projected&&[403,404].includes(inspectionErrorStatus);
    if(!inaccessible&&inspectionFlight?.id!==selectedId&&(!projected||inspection?.entity?.version!==projected.version||previous&&(previous.x!==value.player.x||previous.y!==value.player.y)||previewVersionsChanged(inspection,[...ground(),...inventory()],value.player))){const captured=selectedId;select(captured,selectedFromBag,true).catch(fail);}
  }
  renderInventory();renderSelection();renderActivity();renderPublicContent();renderPending();
}
function renderPending(){
  if(!identity)return;let pending;try{pending=client.pending();}catch{return;}
  for(const id of ['move-up','move-left','move-down','move-right'])$(id).disabled=actionBusy||executionReclaimBusy||operationRecoveryBusy||!!pending||!playing||!point();
  $('pending').hidden=!pending;
  $('recover-operation').disabled=operationRecoveryBusy||actionBusy;$('release-operation').disabled=operationRecoveryBusy||actionBusy;
  if(pending){$('pending-detail').textContent=pending.rejection?'服务器拒绝了原操作：'+pending.rejection.message:'回应中断，结果尚未核对。操作 '+pending.body.operation_id.slice(0,8)+' 保留；不会自动发出替代操作。';$('release-operation').hidden=!pending.rejection;$('recover-operation').hidden=!!pending.rejection;}
}
async function recoverWorldOperation(release=false){
  if(operationRecoveryBusy||actionBusy||!playing||!client.pending())return;
  stopWalking();const epoch=session;operationRecoveryBusy=true;renderPending();
  try{const result=release?await client.releaseRejected():await client.recover();if(epoch!==session||!playing)return;if(result?.rejected){renderPending();return;}$('error').hidden=true;await refreshWorld(true);if(epoch!==session||!playing)return;announce(release?'已读取当前状态，可以重新选择行动。':'原操作结果已核对。');}
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
  const epoch=session,command=++hostCommandEpoch;++hostReadEpoch;hostCommandBusy=true;
  try{
    let state,receiptsChecked=false;
    try{state=await client.connector('reclaim',{background:true});receiptsChecked=true;}
    catch(error){
      // Revoke future world writes, but a cached zero is not a receipt read.
      manualControlRecoveryPending=true;
      if(error.status===409)throw error;
      if(execution?.delegation_active===true||['starting','running'].includes(execution?.status))await client.mutate('grove.control_revoke',{}, {},isCurrent);
      state={...host,execution:{...execution,delegation_active:false,stop_reason:'后续行动权已收回；原行动结果尚未核对，连接恢复后先核对原回执。'}};
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
    if(result.viewport?.player&&!client.inventoryAfter){client.world.view=null;acceptWorld(result.viewport);}else await refreshWorld(true);
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
  const b=world?.bounds;if(!b||x<b.min_x||y<b.min_y||x>b.max_x||y>b.max_y)return false;
  const t=terrain(x,y);if(!t||t==='rock')return false;
  return !ground().some(e=>e.x===x&&e.y===y&&blocks(e));
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
  if(!current()||!world)return;if(selectedId!==id){rememberActionDrafts();activeActionKey=null;}selectedId=id;selectedTile=null;selectedFromBag=fromBag;if(fromBag&&!force)document.querySelector('.inventory-card').open=false;if(!force)openContext();const epoch=++inspectEpoch,sessionEpoch=session;
  if(!force&&inspection?.entity?.id===id&&!inspection.reading&&!inspectionError){renderSelection();return;}
  const previousEntity=entityById(id),flight={id};inspectionFlight=flight;inspection=previousEntity?{entity:previousEntity,reading:true}:null;inspectionError=null;inspectionErrorStatus=null;renderSelection();
  try{const result=await client.query('grove.inspect',{entity_id:id});if(epoch!==inspectEpoch||sessionEpoch!==session||selectedId!==id)return;inspection=result;renderSelection();}
  catch(error){if(epoch===inspectEpoch&&sessionEpoch===session&&selectedId===id){inspectionError=error.message||'这件物件暂不可查看。';inspectionErrorStatus=error.status;renderSelection();}}
  finally{if(inspectionFlight===flight)inspectionFlight=null;}
}
function renderInventory(){
  const items=inventory().filter(e=>e.id!==point()?.backpack_id);
  const signature=JSON.stringify([selectedId,inventory(),world.inventory_total,world.inventory_next,inventoryPageCursors,inventoryPageBusy,actionBusy,world.construction?.material_totals]);if(signature===lastInventorySignature)return;lastInventorySignature=signature;
  const total=Number.isInteger(world.inventory_total)?Math.max(0,world.inventory_total-1):null;
  $('inventory-capacity').textContent=total!==null?total+' 件随身物件':'本页 '+items.length+' 件';
  $('supply-capacity').textContent=Number.isInteger(world.inventory_total)?world.inventory_total+' 对象（含包）':'本页 '+inventory().length+' 对象';
  const quantities=new Map();for(const item of items){for(const lot of bodyQuantities(item).filter(lot=>lot.material!=='water')){const value=quantities.get(lot.material)||{held:0,available:0};value.held+=lot.quantity;quantities.set(lot.material,value);}}
  for(const [material,quantity] of Object.entries(world.construction?.material_totals||{})){const value=quantities.get(material)||{held:0,available:0};value.available=finite(quantity);quantities.set(material,value);}
  $('material-quantities').replaceChildren(...[...quantities].map(([material,value])=>{const row=node('div',undefined,'material-count');row.append(node('span',materialLabel(material)),node('strong','全库存可投 '+value.available+' 单位\n本页本体 '+value.held+' 单位'));return row;}));
  if(!quantities.size)$('material-quantities').append(node('p','本页暂无材料批次','small'));
  const wetBlocked=items.filter(e=>!e.construction_material?.eligible&&e.construction_material?.reason?.includes('湿')).length;
  $('material-caption').textContent=(wetBlocked?'本页有 '+wetBlocked+' 件原料因附水暂不能投料。':'')+'材料按数量单位计，不是物件件数。可投量来自服务器的全库存真实数量；本体材料数量、水与点火次数仅汇总本页。切换背包页查看其他物件。';
  $('material-caption').dataset.blocked=String(wetBlocked>0);
  const vessels=items.filter(e=>e.state?.water_capacity>0),water=vessels.reduce((total,e)=>total+finite(e.state.water),0),waterCapacity=vessels.reduce((total,e)=>total+finite(e.state.water_capacity),0);
  $('supply-water').textContent=water+' / '+waterCapacity;
  const ignitionTools=items.filter(e=>caps(e).includes('ignition')),unknownUses=ignitionTools.some(e=>!Number.isFinite(e.state?.uses)),knownUses=ignitionTools.reduce((total,e)=>total+finite(e.state?.uses),0);
  $('supply-tools').textContent=unknownUses?'点火次数待读取':'点火 '+knownUses+' 次';
  $('inventory-list').replaceChildren(...items.map(e=>{const el=button('',()=>run(()=>select(e.id,true)),{secondary:true});el.className='inventory-item'+(e.id===selectedId?' selected':'');const icon=node('span',entityGlyph(e),'item-icon '+(e.material||''));el.append(icon,node('span',e.name),node('small',stateLine(e)));return el;}));
  $('inventory-water').textContent=items.filter(e=>e.state?.water_capacity>0).map(e=>e.name+'：'+finite(e.state.water)+' / '+e.state.water_capacity+' 水').join(' · ')||'装水需要一件可盛水的容器。';
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
function stateLine(e){const s=e?.state||{};if(matterLots(e).length)return entitySummary(e,point()?.backpack_id);const body=bodyQuantities(e),material=body.length?body.map(lot=>'本体 '+materialLabel(lot.material)+' '+lot.quantity+' 单位').join('，'):materialLabel(e.material);return [material,lifecycleLabel(e),s.burning?'有材料正在燃烧':null,s.wetness>0?'湿度 '+s.wetness:null,s.water_capacity>0?Number.isFinite(s.water)?'水 '+s.water+'/'+s.water_capacity:'存水未公开':null].filter(Boolean).join(' · ');}
function entityGlyph(e){return genericGlyph(e);}
function selectTile(tile){
  rememberActionDrafts();activeActionKey=null;stopWalking();++inspectEpoch;selectedId=null;selectedFromBag=false;inspection=null;inspectionError=null;inspectionErrorStatus=null;inspectionFlight=null;
  if(selectedTile?.x!==tile.x||selectedTile?.y!==tile.y){chosenBuildMaterials.clear();touchedBuildChoices.clear();}
  selectedTile={x:tile.x,y:tile.y};lastSelectionSignature='';openContext();renderSelection();renderInventory();
}
function renderTileSelection(){
  const tile=selectedTile,t=terrain(tile.x,tile.y),d=Math.abs(tile.x-point().x)+Math.abs(tile.y-point().y);
  const occupied=ground().filter(e=>e.x===tile.x&&e.y===tile.y&&!e.state?.destroyed);
  $('selection-name').textContent=terrainNames[t]||'未读取的地面';$('selection-distance').textContent=d===0?'脚下':d+' 格';renderTargetChoices(tile);
  $('selection-description').textContent={grass:'草地，可以在规则允许的位置留下设施。',dirt:'泥地，可以在规则允许的位置留下设施。',water:'浅溪仍可涉水；随身可湿物件会受湿，包括合上容器里的物件。木板路可以免去这一格的涉水受湿。',rock:'岩壁不能通行，也不能搭建设施。'}[t]||'这块地方还没有读取。靠近后再选择。';
  $('selection-stats').replaceChildren(stat('地面',{grass:'草地',dirt:'泥地',water:'浅溪',rock:'岩壁'}[t]||'未读取'),stat('距离',d+' 格'));
  $('selection-history').replaceChildren();$('selection-content').replaceChildren();$('selection-effects').replaceChildren();$('selection-actions').replaceChildren();
  const disabled=actionBusy||!!client.pending();
  $('selection-actions').append(button(d?'沿路走到这里':'已经在这里',()=>goTo(tile.x,tile.y),{disabled:disabled||!d||!walkable(tile.x,tile.y),secondary:true}));
  if(t==='water'){
    const sources=ground().filter(e=>caps(e).includes('water_source'));
    $('selection-actions').append(node('p','涉水会弄湿随身材料。装水要选择可取水的水源和你的容器。','small'));
    for(const source of sources)$('selection-actions').append(button('查看取水处 · '+source.name,()=>run(()=>select(source.id)),{secondary:true}));
  }
  if(t==='rock')$('selection-note').textContent='岩壁挡路，可以选择相邻空地绕行。';
  for(const entity of occupied)$('selection-content').append(button('查看 '+entity.name,()=>run(()=>select(entity.id)),{secondary:true}));
  for(const recipe of recipes()){
    if(t&&Array.isArray(recipe.terrain)&&!recipe.terrain.includes(t))continue;
    const section=node('details',undefined,'build-option'),cost=recipe.cost||{},costUnits=recipe.material_cost_u||{},costText=Object.entries(cost).map(([material,count])=>(materialNames[material]||material)+' '+(costUnits[material]??count)+' 单位').join(' + '),totals=world.construction?.material_totals||{};
    section.append(node('summary','搭建 '+(recipe.name||recipe.id)),node('p',(recipe.use||'')+' · 投入 '+costText,'small'));
    section.append(node('p','全库存当前可投：'+Object.keys(cost).map(material=>materialLabel(material)+' '+finite(totals[material])+' 单位').join(' · '),'small'));
    const allowedTerrain=recipe.terrain||[],reason=!t?'请靠近，读取这块地面':allowedTerrain.length&&!allowedTerrain.includes(t)?'需要'+allowedTerrain.map(x=>({water:'水面',grass:'草地',dirt:'泥地'}[x]||x)).join('或'):d>1?'先走到相邻一格':occupied.some(blocks)?'这里有障碍，需要先移开':occupied.some(e=>e.state?.recipe||e.state?.kind==='resource_node')?'这里已有设施或资源节点':'';
    let chosen=chosenBuildMaterials.get(recipe.id);
    const candidates=Object.keys(cost).flatMap(material=>materialItems(material));
    if(!chosen||!touchedBuildChoices.has(recipe.id)||chosen.some(id=>!candidates.some(e=>e.id===id))){chosen=[];for(const [material,count] of Object.entries(cost)){let remaining=costUnits[material]??count;for(const item of materialItems(material)){if(remaining<=0)break;chosen.push(item.id);remaining-=finite(item.construction_material?.quantity_u);}}chosenBuildMaterials.set(recipe.id,chosen);}
    const choice=node('details',undefined,'material-choice');choice.append(node('summary','可选：指定本页投入的物件'));
    const manualLabel=node('label',undefined,'material-pick'),manual=node('input');manual.type='checkbox';manual.checked=touchedBuildChoices.has(recipe.id);manual.disabled=disabled;manualLabel.append(manual,node('span','使用下面选定的物件；否则由世界从全库存取所需数量'));choice.append(manualLabel);
    for(const item of candidates){const label=node('label',undefined,'material-pick'),input=node('input');input.type='checkbox';input.value=item.id;input.checked=chosen.includes(item.id);input.disabled=disabled;label.append(input,node('span',item.name+' · '+stateLine(item)));choice.append(label);}
    if(!candidates.length)choice.append(node('p','背包没有可投入的原料。可采集或取用公共材料。','small'));
    const selectedMaterials=()=>[...choice.querySelectorAll('input:checked')].filter(input=>input!==manual).map(input=>input.value);
    const status=node('p',undefined,'small'),submit=button('在这里搭建',()=>build(recipe.id,manual.checked?selectedMaterials():undefined),{disabled:true});
    const update=()=>{const ids=selectedMaterials();chosenBuildMaterials.set(recipe.id,ids);if(manual.checked)touchedBuildChoices.add(recipe.id);else touchedBuildChoices.delete(recipe.id);const missing=Object.entries(cost).filter(([material,count])=>(manual.checked?ids.reduce((total,id)=>{const item=candidates.find(e=>e.id===id);return total+(item?.construction_material?.material===material?finite(item.construction_material.quantity_u):0);},0):finite(totals[material]))<(costUnits[material]??count));submit.disabled=disabled||!!reason||!!missing.length||(manual.checked&&ids.length>8);status.textContent=reason||(manual.checked&&ids.length>8?'一次最多指定 8 件物件；可改用全库存自动取料。':missing.length?(manual.checked?'所选物件':'全库存')+'可投入的真实数量须达到 '+costText:'只扣配方所需数量，余量留在原物件中。设施成为公共内容；拆回只返未消耗的材料。');};
    choice.addEventListener('change',update);section.append(choice,status,submit);update();$('selection-actions').append(section);
  }
  if(!recipes().length)$('selection-actions').append(node('p','附近搭建方式暂未读取，请刷新现场。','small'));
  $('selection-note').textContent='所选物件可以部分投入，成本与剩余量由世界核对。盛水容器、点火工具和私人物件按当前规则保留。';
}
async function build(recipe,materialIds){
  if(!selectedTile)return;const target={...selectedTile},epoch=session;stopWalking();
  const saved=await perform('grove.build',isCurrent=>{if(!isCurrent())return;return {recipe,x:target.x,y:target.y,expected_player_version:point().version,...(materialIds?{material_ids:materialIds}:{})};},'搭建设施');
  if(saved&&epoch===session){const structure=ground().find(e=>e.x===target.x&&e.y===target.y&&e.state?.recipe===recipe&&!e.state?.destroyed);if(structure)await select(structure.id);}
}
function renderSelection(){
  const entity=inspection?.entity?.id===selectedId?inspection.entity:entityById(selectedId);
  const signature=JSON.stringify([selectedId,selectedTile,activeActionKey,entity,inspection,inspectionError,entity?.state?.regrowth_tick!=null?world?.clock?.sequence:null,world?.construction,point()?.x,point()?.y,actionBusy,!!identity&&client.pending(),inventory().map(e=>[e.id,e.version]),definitionReadRef,definitionReadBusy,definitionReadValue,definitionReadError]);
  if(signature===lastSelectionSignature)return;
  const checkingForm=combinationCheckingForm?.isConnected&&combinationCheckingForm.dataset.entityId===selectedId?combinationCheckingForm:null;
  const activeForm=$('selection-actions').querySelector('.descriptor-action');
  const editingForm=checkingForm||(activeForm?.dataset.actionKey===activeActionKey?activeForm:document.activeElement?.closest('.descriptor-action'));
  const editing=editingForm?.closest('#selection-actions');
  const preserveInputs=editingForm&&editing&&activeActionKey===editingForm.dataset.actionKey&&lastSelectionSignature&&selectedId===editing.dataset.entityId&&!inspectionError;
  if(!preserveInputs)rememberActionDrafts();
  lastSelectionSignature=signature;$('selection-actions').dataset.entityId=selectedId||'';
  if(selectedTile){renderTileSelection();return;}
  if(!entity){$('selection-name').textContent=selectedId?inspectionError?'当前无法查看物件':'正在读取所选物件':'选择物件或位置';$('selection-description').textContent=selectedId?inspectionError?([403,404].includes(inspectionErrorStatus)?'当前不可访问：':'尚未取得当前状态：')+inspectionError:'正在按你的权限重新读取。物件未出现在地面列表，不代表已经消失。':'点一件物件，查看它当前的材料、数量、关系与可用行动。你可以亲手使用，也可以明确委托 Agent 行动。';$('selection-distance').textContent='';$('selection-stats').replaceChildren();$('selection-actions').replaceChildren();$('selection-content').replaceChildren();$('selection-effects').replaceChildren();$('selection-history').replaceChildren();$('selection-note').textContent=selectedId?'取得当前状态与操作资格后才能动手。':'';if(selectedId&&inspectionError)$('selection-actions').append(button('重新读取这件物件',()=>run(()=>select(selectedId,selectedFromBag,true)),{disabled:actionBusy,secondary:true}));return;}
  const d=distance(entity),own=inventory().some(e=>e.id===entity.id),s=entity.state||{},near=d<=1;
  $('selection-name').textContent=entity.name;$('selection-description').textContent=(inspectionError||inspection?.reading?'上次已读状态：':'当前状态：')+stateLine(entity);$('selection-distance').textContent=own?'随身':d===0?'脚下':Number.isFinite(d)?d+' 格':'位置未读';
  $('selection-history').replaceChildren();if(entity.authored_description){const detail=node('details');detail.append(node('summary','发表时的说明'),node('p',entity.authored_description,'small'));$('selection-history').append(detail);}
  if(entity.created_by)$('selection-history').append(node('p','创作来源：'+authorLabel(entity.created_by),'small'));
  const references=(world?.public_content?.contributions||[]).filter(contribution=>(contribution.entity_ids||[]).includes(entity.id));
  for(const reference of references)$('selection-history').append(node('p','公共贡献 · '+authorLabel(reference.author),'small'));
  renderEffects(entity);
  const lots=matterLots(entity),stats=[stat('本体材质',materialLabel(entity.material,entity))];
  if(entity.simulation){stats.push(...simulationStats(entity,point()?.backpack_id).map(([label,value])=>stat(label,value)));stats.push(stat('结构与残材',lifecycleLabel(entity)||'结构完整'),stat('材料反应',lots.some(lot=>lot.burning)||(entity.simulation.burning_lots||[]).length?'有批次正在燃烧':'未燃烧'));}
  else{if(caps(entity).includes('wettable'))stats.push(stat('湿度',finite(s.wetness)));if(caps(entity).includes('flammable'))stats.push(stat('燃料',finite(s.fuel)));if(caps(entity).includes('flammable'))stats.push(stat('火',s.destroyed?'材料已耗尽':s.burning?'正在燃烧':'未燃烧'));}
  if(s.water_capacity>0)stats.push(stat('存水',Number.isFinite(s.water)?s.water+' / '+s.water_capacity:'未公开'));if(caps(entity).includes('container'))stats.push(stat('物件容量',s.capacity),stat('容器',s.open?'敞开':'合上'));if(caps(entity).includes('water_source'))stats.push(stat('可取水',finite(s.water_remaining)));if(caps(entity).includes('ignition'))stats.push(stat('余下点火',Number.isFinite(s.uses)?s.uses:'待读取'));
  if(s.kind==='resource_node'){stats.push(stat('余下原料',finite(s.stock)+' / '+finite(s.stock_capacity)));if(s.regrowth_tick!==null&&s.regrowth_tick!==undefined)stats.push(stat('正在再生','剩 '+Math.max(0,s.regrowth_tick-finite(world.clock?.sequence))+' 个世界逻辑步'));}
  if(s.recipe==='canopy')stats.push(stat('模式',s.mode==='collect'?'集雨':'遮雨'));
  if(s.recipe==='hearth')stats.push(stat('炉火',s.lit?'正在烘干':'未点燃'),stat('炉内燃料',Number.isFinite(s.hearth_fuel)?s.hearth_fuel:'未公开'));
  $('selection-stats').replaceChildren(...stats);
  if(!preserveInputs)$('selection-actions').replaceChildren();$('selection-content').replaceChildren();
  $('selection-note').textContent=near?'行动按服务器当前状态结算；多人改变同一件物件时，会先读取新状态。':'走到相邻一格才能动手。选择“走近”沿路前往，不会穿过障碍。';
  const location=entityLocation(entity);
  renderTargetChoices(location);
  if(!preserveInputs&&!own&&d>1&&Number.isFinite(location.x)&&Number.isFinite(location.y))$('selection-actions').append(button('走近这件物件',()=>goTo(location.x,location.y,true),{disabled:actionBusy}));
  if(!inspection||inspection.reading){if(preserveInputs){for(const form of $('selection-actions').querySelectorAll('.descriptor-action'))form.dispatchEvent(new Event('refresh-candidate-preview'));}else{$('selection-actions').append(node('p',inspectionError?([403,404].includes(inspectionErrorStatus)?'当前不可访问：':'')+inspectionError:'正在读取可用行动…',inspectionError?'action-reason':'small'));if(inspectionError)$('selection-actions').append(button('重新读取这件物件',()=>run(()=>select(entity.id,selectedFromBag,true)),{disabled:actionBusy,secondary:true}));}if(inspectionError)$('selection-note').textContent='没有取得当前操作资格。原操作与身份保留；重新读取后再选择。';renderMatter(entity);renderRelatedObjects(entity,actionBusy||!!client.pending());renderCreative(entity,true);return;}
  const available=actionIds(inspection.available_actions);
  const disabled=actionBusy||!near||!!client.pending();
  if(!preserveInputs&&!own&&Number.isFinite(location.x)&&Number.isFinite(location.y))$('selection-actions').append(button('选择这里的地面',()=>selectTile(location),{secondary:true}));
  renderMatter(entity);
  renderRelatedObjects(entity,disabled);
  renderCreative(entity);
  if(preserveInputs){for(const form of $('selection-actions').querySelectorAll('.descriptor-action'))form.dispatchEvent(new Event('refresh-candidate-preview'));$('selection-note').textContent='材料和关系已刷新。保留你正在选择的输入；提交前会再次核对当前资格、版本与数量。';return;}
  const descriptors=actionDescriptors(inspection);
  const unavailable=descriptors.filter(descriptor=>descriptor.available===false);
  const active=descriptors.find(descriptor=>actionKey(descriptor)===activeActionKey&&descriptor.available!==false);
  if(active){$('selection-actions').append(button('返回其他做法',()=>{rememberActionDrafts();activeActionKey=null;lastSelectionSignature='';renderSelection();},{secondary:true}));renderDescriptorEditor(active,entity,disabled);}
  else for(const descriptor of descriptors.filter(descriptor=>descriptor.available!==false))renderDescriptorAction(descriptor,entity,disabled);
  if(unavailable.length){const detail=node('details',undefined,'unavailable-actions');detail.append(node('summary','暂时不能做 · '+unavailable.length+' 项'));for(const descriptor of unavailable)detail.append(node('p',descriptorLabel(descriptor)+'：'+friendlyInstruction(descriptor.reason||'当前条件不满足'),'small'));$('selection-actions').append(detail);}
  if(!available.length&&!s.destroyed)$('selection-actions').append(node('p','这件物件当前没有可用行动。','small'));
  if(available.length&&!descriptors.length)$('selection-actions').append(node('p','可用行动的描述尚未读取，请刷新当前物件。','small'));
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
  if(s.kind==='resource_node')lines.push(s.resource_kind==='stone'?'石料有限，采集后不会再生。':'原料有限。采集后可用 5 份本人存水照料，世界推进约 6 个逻辑步后再生一份；期间可以做别的事。');
  if(blocks(entity))lines.push('这件物件当前挡住一格通路。绕行保留原地状态；拿走保留材料但占背包；烧毁会永久消耗它。');
  if(entity.construction_material?.eligible)lines.push('可投入搭建'+(entity.material==='wood'?'，也可作为石炉燃料。':'。'));
  else if(entity.construction_material?.reason&&!s.recipe&&s.kind!=='resource_node')lines.push('目前不能投入搭建：'+entity.construction_material.reason);
  const actionOptions=inspection?.entity?.id===entity.id?inspection.action_options||[]:[];
  for(const option of actionOptions.filter(o=>o.available&&o.use))lines.push(descriptorLabel(option)+'：'+friendlyInstruction(option.use)+(option.cost?' · '+formatCost(option.cost):''));
  $('selection-effects').replaceChildren(...[...new Set(lines)].map(line=>node('p',line,'small')));
}
function formatCost(cost){if(typeof cost==='string')return cost;return Object.entries(cost).map(([key,value])=>(materialNames[key]||({water:'水',stored_water:'棚内存水',uses:'点火次数',ignition_uses:'点火次数',stock:'公共原料'}[key])||key)+' '+value).join(' · ');}
function renderMatter(entity){
  const lots=matterLots(entity);if(!lots.length)return;
  const section=node('section',undefined,'matter-list');section.append(node('h4','当前材料批次'),node('p','数量由世界保存；湿度由实际附水计算。关闭的容器只显示你获准查看的批次。','small'));
  for(const lot of lots){const row=node('div',undefined,'matter-row'),burning=lot.burning||(entity.simulation?.burning_lots||[]).includes(lot.id);row.append(node('strong',slotLabel(lot.slot)+' · '+materialLabel(lot,entity)),node('span',lot.quantity+' 单位'+(burning?' · 燃烧中':'')));section.append(row);}
  $('selection-content').append(section);
}
function renderRelatedObjects(entity,disabled){
  const children=entity.relations?.children||[];
  if(children.length){const section=node('section',undefined,'contents-list');section.append(node('h4','当前完整子物件与绑定 · '+children.length+' 件'),node('p','这些物件保留自己的身份和材料；其本体不计入父物件的反应投料。','small'));
    for(const child of children){const row=node('div',undefined,'contents-row');row.append(node('span',child.name||child.id),node('small',child.relation==='attached'?'绑定部件':child.relation==='contained'?'容纳物件':String(child.relation||'')),button('查看',()=>run(()=>select(child.id)),{disabled,secondary:true}));section.append(row);}$('selection-content').append(section);}
  const links=entity.process_refs||[],references=new Set(links.map(reference=>typeof reference==='string'?reference:reference.id)),processes=(world?.processes||[]).filter(process=>references.has(process.id)||process.entity_id===entity.id||(process.participants||[]).includes(entity.id));
  if(processes.length||links.length){const section=node('section',undefined,'process-list');section.append(node('h4','实际过程与保存的引用'));
    for(const process of processes)section.append(node('p',(process.label||'物件变化')+' · '+(process.status==='active'?'正在发生':process.status==='complete'?'已结束':'当前状态待读取'),'small'));
    for(const link of links){const id=typeof link==='string'?link:link.id;if(!processes.some(process=>process.id===id))section.append(node('p','视口未返回这个过程的当前状态。','small'));}$('selection-content').append(section);}
}
function appendFacts(parent,value,depth=0,scope='facts',path=[]){
  if(!value||typeof value!=='object'){parent.append(node('p',factValue(value),'small'));return;}
  if(depth>=6){parent.append(node('p','更深的结构未在此展开。','small'));return;}
  const entries=Object.entries(value),limit=40;
  for(const [key,item] of entries.slice(0,limit)){
    const label=Array.isArray(value)?'记录 '+(Number(key)+1):factLabel(key);
    if(item!==null&&typeof item==='object'){
      const disclosureKey=scope+':'+[...path,key].join('.'),detail=node('details',undefined,'fact-group');detail.open=factDisclosures.get(disclosureKey)??(depth===0);
      detail.addEventListener('toggle',()=>{if(!detail.isConnected)return;factDisclosures.set(disclosureKey,detail.open);if(factDisclosures.size>160)factDisclosures.delete(factDisclosures.keys().next().value);});
      detail.append(node('summary',label+' · '+factValue(item)));appendFacts(detail,item,depth+1,scope,[...path,key]);parent.append(detail);
    }else{const row=node('div',undefined,'fact-row');row.append(node('small',label),node('span',factValue(item)));parent.append(row);}
  }
  if(entries.length>limit)parent.append(node('p','此处展开前 '+limit+' 项，共 '+entries.length+' 项；不是完整历史。','small'));
  if(!entries.length)parent.append(node('p',Array.isArray(value)?'当前没有已读记录。':'当前没有已读属性。','small'));
}
function renderCreative(entity,previous=false){
  const creative=creativeState(previous?null:inspection,entity);if(!creative)return;
  const section=node('section',undefined,'creative-state');section.append(node('h4',previous?'规则对象 · 上次已读状态':'规则对象 · 当前事实'),node('p','规则字段、组分、连接与记录来自服务器。逻辑步不是现实经过的秒数，叙述不决定结果。','small'));
  for(const line of reactionObservationLines(creative,entity))section.append(node('p',line,'small'));
  const expanded=Object.fromEntries(Object.entries(creative).filter(([key])=>!['reaction_inputs','reaction_observations','reaction_observation_count','reaction_observations_projection','reaction_observations_truncated'].includes(key)));
  appendFacts(section,expanded,0,entity.id);
  if(creative.hidden===true)section.append(node('p','这个公共容器已合上。当前只读到规则引用，内部配料、运行与记录未公开。','small'));
  const ref=creative.definition_ref;
  if(ref&&typeof ref==='string'){
    if(definitionReadRef!==ref){definitionReadRef=ref;definitionReadValue=null;definitionReadError=null;definitionReadBusy=false;definitionReadOpen=false;++definitionReadEpoch;}
    const detail=node('details',undefined,'rule-definition');detail.open=definitionReadOpen;detail.addEventListener('toggle',()=>{if(detail.isConnected)definitionReadOpen=detail.open;});detail.append(node('summary','这件对象的详细做法'),button(definitionReadBusy?'正在读取…':'读取详细做法',()=>run(()=>readRuleDefinition(ref,entity.id)),{disabled:previous||definitionReadBusy,secondary:true}));
    if(definitionReadError)detail.append(node('p',definitionReadError,'action-reason'));
    if(definitionReadValue)appendFacts(detail,definitionReadValue,0,ref);section.append(detail);
  }
  $('selection-content').append(section);
}
async function readRuleDefinition(ref,id){
  if(definitionReadBusy)return;const epoch=session,reading=++definitionReadEpoch;definitionReadBusy=true;definitionReadOpen=true;definitionReadError=null;renderSelection();
  try{const value=await client.query('grove.definition_read',{definition_ref:ref});if(epoch!==session||reading!==definitionReadEpoch||selectedId!==id)return;definitionReadValue=value;}
  catch(error){if(epoch===session&&reading===definitionReadEpoch)definitionReadError=error.message||'尚未读到规则定义。';}
  finally{if(epoch===session&&reading===definitionReadEpoch){definitionReadBusy=false;lastSelectionSignature='';renderSelection();}}
}
function renderRuleAction(descriptor,entity,disabled){
  const label=descriptorLabel(descriptor);
  if(descriptor.available===false){$('selection-actions').append(node('p',label+'：'+(descriptor.reason||'当前条件不满足。'),'action-reason'));for(const line of conditionObservationLines(descriptor.condition_observation,entity))$('selection-actions').append(node('p',line,'small'));return;}
  const form=node('form',undefined,'descriptor-action rule-action');form.dataset.action=descriptor.action;form.dataset.actionKey=actionKey(descriptor);form.dataset.entityId=entity.id;form.append(node('p',label,'descriptor-label'));
  if(descriptor.use)form.append(node('p',friendlyInstruction(descriptor.use),'small'));
  for(const line of conditionObservationLines(descriptor.condition_observation,entity))form.append(node('p',line,'small'));
  const fields=[],unsupported=[],fixed=descriptor.arguments||{},topSchema=descriptor.input_schema||{type:'object',properties:{},required:[]};
  const candidateKeys={item_id:'items',other_id:'others',material:'materials'};
  const generatedFields=new Set(['action','target_id','expected_target_version','expected_player_version','expected_item_version','expected_other_version','params','delegation']);
  const topProperties=Object.fromEntries(Object.entries(topSchema.properties||{}).filter(([key])=>!generatedFields.has(key)));
  for(const [argument,key] of Object.entries(candidateKeys))if(Object.hasOwn(descriptor.choices||{},key)&&!topProperties[argument])topProperties[argument]={type:'string',title:factLabel(argument)};
  const topRequired=new Set([...(topSchema.required||[]),...Object.keys(candidateKeys).filter(argument=>Object.hasOwn(descriptor.choices||{},candidateKeys[argument]))]);
  function addFields(schema,prefix,defaults,requiredNames,depth=0,container=form){
    if(depth>4){unsupported.push('参数嵌套过深');return;}
    for(const [name,property] of Object.entries(schema||{})){
      if(['__proto__','constructor','prototype'].includes(name))continue;
      const path=[...prefix,name],required=requiredNames.has(name),fallback=defaults?.[name]??property.default;
      if(prefix.length===0&&name==='terms_version'&&Object.hasOwn(fixed,name)){
        continue;
      }
      if(property.type==='object'){
        const section=node('fieldset',undefined,'schema-group');section.append(node('legend',property.title||factLabel(name)));container.append(section);
        addFields(property.properties,path,fallback,new Set(property.required||[]),depth+1,section);continue;
      }
      const field=node('label',(property.title||factLabel(name))+(required?'':'（可选）'));
      const choiceKey=prefix.length===0?candidateKeys[name]:null,candidates=choiceKey?ruleChoices(descriptor,choiceKey):[];
      let control,enumValues=null;
      if(choiceKey&&Object.hasOwn(descriptor.choices||{},choiceKey)){
        control=node('select');const empty=node('option','请选择');empty.value='';control.append(empty);
        for(const candidate of candidates){const option=node('option',candidateLabel(readableCandidate(candidate),candidates.map(readableCandidate)));option.value=candidate.id;control.append(option);}
      }else if(Array.isArray(property.enum)||property.type==='boolean'){
        control=node('select');enumValues=Array.isArray(property.enum)?property.enum:[true,false];const empty=node('option','请选择');empty.value='';control.append(empty);
        enumValues.forEach((value,index)=>{const option=node('option',factValue(value));option.value=String(index);control.append(option);});
      }else if(['integer','number'].includes(property.type)){
        control=node('input');control.type='number';control.inputMode='decimal';control.step=property.type==='integer'?'1':'any';if(Number.isFinite(property.minimum))control.min=String(property.minimum);if(Number.isFinite(property.maximum))control.max=String(property.maximum);
      }else if(property.type==='string'||!property.type){control=node('input');control.type='text';if(Number.isInteger(property.maxLength))control.maxLength=property.maxLength;}
      else if(property.type==='array'&&['string','integer','number','boolean'].includes(property.items?.type)){control=node('textarea');control.rows=2;control.placeholder='每行一项';}
      else{unsupported.push(property.title||name);continue;}
      control.disabled=disabled;control.setAttribute('aria-label',label+' · '+path.map(factLabel).join(' / '));control.required=required;
      if(fallback!==undefined){if(enumValues){const index=enumValues.findIndex(value=>value===fallback);if(index>=0)control.value=String(index);}else control.value=Array.isArray(fallback)?fallback.join('\n'):String(fallback);}
      field.append(control);if(property.description)field.append(node('small',property.description));container.append(field);fields.push({path,property,control,required,enumValues,choiceKey});
    }
  }
  addFields(topProperties,[],fixed,topRequired);
  addFields(descriptor.params_schema?.properties||{},['params'],fixed.params||{},new Set(descriptor.params_schema?.required||[]));
  const intent=node('p',undefined,'interaction-preview'),note=node('p',undefined,'small'),controls=node('div',undefined,'descriptor-controls'),submit=button(label,()=>{}, {disabled:true});submit.type='submit';
  controls.append(submit,button('取消选择',()=>{actionDrafts.delete(entity.id+':'+descriptor.action);form.reset();for(const field of fields)if(field.control.tagName==='SELECT')field.control.value='';update();},{disabled,secondary:true}));form.append(intent,note,controls);
  const liveDescriptor=()=>inspection?.entity?.id===entity.id&&!inspection.reading?actionDescriptors(inspection).find(option=>option.function_id==='grove.rule_action'&&option.action===descriptor.action):null;
  function readValues(live){
    const result={},errors=[];
    function set(path,value){let target=result;for(const key of path.slice(0,-1))target=target[key]||(target[key]=Object.create(null));target[path.at(-1)]=value;}
    for(const field of fields){const {control,property,path,enumValues,required,choiceKey}=field,raw=control.value.trim();if(!raw){if(required)errors.push('请选择或填写 '+(property.title||factLabel(path.at(-1))));continue;}
      let value=enumValues?enumValues[Number(raw)]:raw;
      if(choiceKey&&!ruleChoices(live,choiceKey).some(candidate=>candidate.id===raw)){errors.push('所选 '+factLabel(path.at(-1))+' 的条件已改变，请重新选择。');continue;}
      if(['integer','number'].includes(property.type)){value=Number(raw);if(!Number.isFinite(value)||property.type==='integer'&&!Number.isInteger(value)||Number.isFinite(property.minimum)&&value<property.minimum||Number.isFinite(property.maximum)&&value>property.maximum)errors.push((property.title||factLabel(path.at(-1)))+' 超出当前允许范围。');}
      if(property.type==='string'&&(Number.isInteger(property.minLength)&&raw.length<property.minLength||Number.isInteger(property.maxLength)&&raw.length>property.maxLength))errors.push((property.title||factLabel(path.at(-1)))+' 长度超出当前允许范围。');
      if(property.type==='array'){const parts=raw.split('\n').map(part=>part.trim()).filter(Boolean),type=property.items.type;value=parts.map(part=>type==='string'?part:type==='boolean'?part==='true'||part==='是'?true:part==='false'||part==='否'?false:null:Number(part));if(value.some(item=>item===null||typeof item==='number'&&(!Number.isFinite(item)||type==='integer'&&!Number.isInteger(item)))||Number.isInteger(property.minItems)&&value.length<property.minItems||Number.isInteger(property.maxItems)&&value.length>property.maxItems)errors.push((property.title||factLabel(path.at(-1)))+' 列表不符合当前规则。');}
      set(path,value);
    }
    return {result,errors};
  }
  function update(){
    const live=liveDescriptor(),reading=!live;
    for(const field of fields)if(field.choiceKey&&field.control.tagName==='SELECT'){
      const selected=field.control.value,candidates=ruleChoices(live||descriptor,field.choiceKey),empty=node('option','请选择');empty.value='';field.control.replaceChildren(empty);
      for(const candidate of candidates){const option=node('option',candidateLabel(readableCandidate(candidate),candidates.map(readableCandidate)));option.value=candidate.id;field.control.append(option);}
      if(selected&&!candidates.some(candidate=>candidate.id===selected)){const previous=node('option','原选择当前不可用，请重选');previous.value=selected;previous.disabled=true;field.control.append(previous);}
      field.control.value=selected;field.control.disabled=disabled||!candidates.length;
    }
    const values=live?readValues(live):{errors:[]};submit.disabled=disabled||actionBusy||reading||live?.available===false||unsupported.length>0||values.errors.length>0;
    const result=values.result||{},item=ruleChoices(live||descriptor,'items').find(candidate=>candidate.id===result.item_id);
    intent.textContent=operationIntentLabel(live||descriptor,entity,{item:item?readableCandidate(item):null,amount:result.amount??result.params?.amount,choice:result.material});
    note.textContent=reading?'正在核对当前行动条件。取得当前状态后再提交。':live.available===false?live.reason||'当前条件不满足。':unsupported.length?'此参数结构尚未接入网页：'+unsupported.join('、')+'。可明确委托 Agent 通过正式接口操作。':values.errors[0]||'普通操作直接由规则结算，不调用模型；提交前重新读取版本和资格。';
  }
  form.addEventListener('input',update);form.addEventListener('change',update);form.addEventListener('refresh-candidate-preview',update);
  form.addEventListener('submit',event=>{event.preventDefault();if(submit.disabled)return;const live=liveDescriptor(),values=readValues(live);if(values.errors.length)return;run(()=>ruleAction(descriptor.action,entity,values.result,live));});
  restoreActionDraft(form,update);update();$('selection-actions').append(form);
}
async function ruleAction(action,entity,values,chosenDescriptor){
  stopWalking();const epoch=session;
  async function readEntity(id){
    try{return await client.query('grove.inspect',{entity_id:id});}
    catch(error){if(epoch!==session||!playing)return null;if(![403,404].includes(error.status))throw error;const message='对象当前不可访问：'+(error.message||id)+' 本次行动尚未提交，请重新选择。';
      if(selectedId===entity.id){const previous=inspection?.entity;inspection=previous?{entity:previous,reading:true}:null;inspectionError=message;inspectionErrorStatus=error.status;}announce(message,true);$('movement-feedback').textContent=message;return null;}
  }
  const label=descriptorLabel(chosenDescriptor);
  return perform('grove.rule_action',async isCurrent=>{
    ++inspectEpoch;inspectionFlight=null;const latest=await readEntity(entity.id);if(!isCurrent()||!latest)return null;
    if(selectedId!==entity.id){$('movement-feedback').textContent='选择已改变，这次规则行动尚未提交。';return null;}
    inspection=latest;const live=actionDescriptors(latest).find(option=>option.function_id==='grove.rule_action'&&option.action===action);renderSelection();
    if(!live||live.available===false)throw new Error(live?.reason||'当前行动资格已改变，请重新选择。');
    if(chosenDescriptor.arguments?.terms_version!==live.arguments?.terms_version){announce('契约条款已改变，尚未提交。请读取当前条款，再选择是否接受。');$('movement-feedback').textContent='条款已改变，这次行动尚未提交。';return null;}
    const args={...values,action,target_id:entity.id,expected_target_version:latest.entity.version,expected_player_version:point().version};
    if(Object.hasOwn(live.arguments||{},'terms_version'))args.terms_version=chosenDescriptor.arguments.terms_version;
    for(const [argument,key,versionKey] of [['item_id','items','expected_item_version'],['other_id','others','expected_other_version']]){
      const id=values[argument];if(!id)continue;const candidate=ruleChoices(live,key).find(candidate=>candidate.id===id);if(!candidate)throw new Error('所选对象已不在当前候选中，尚未提交。');
      if(candidate.kind==='role'){if(Number.isInteger(candidate.version))args[versionKey]=candidate.version;}
      else{const item=await readEntity(id);if(!isCurrent()||!item)return null;args[versionKey]=item.entity.version;}
    }
    if(values.material&&!ruleChoices(live,'materials').some(candidate=>candidate.id===values.material))throw new Error('材料已经不符合当前规则，尚未提交。');
    return args;
  },label);
}
function renderDescriptorAction(descriptor,entity,disabled){
  if(descriptor.available===false)return;
  const generated=['action','target_id','expected_target_version','expected_player_version','expected_item_version','expected_other_version','params','delegation','terms_version'];
  const needsInput=!!descriptor.input||descriptor.function_id==='grove.rule_action'&&(Object.keys(descriptor.choices||{}).some(key=>(descriptor.choices[key]||[]).length)||Object.keys(descriptor.params_schema?.properties||{}).length||Object.keys(descriptor.input_schema?.properties||{}).some(key=>!generated.includes(key)));
  const choose=()=>{rememberActionDrafts();activeActionKey=actionKey(descriptor);lastSelectionSignature='';renderSelection();};
  if(needsInput){const option=button(descriptorLabel(descriptor)+'…',choose,{disabled,secondary:true});option.classList.add('action-option');$('selection-actions').append(option);return;}
  if(descriptor.function_id==='grove.rule_action'){$('selection-actions').append(button(descriptorLabel(descriptor),()=>run(()=>ruleAction(descriptor.action,entity,{},descriptor)),{disabled}));return;}
  renderDescriptorEditor(descriptor,entity,disabled);
}
function renderDescriptorEditor(descriptor,entity,disabled){
  if(descriptor.function_id==='grove.rule_action'){renderRuleAction(descriptor,entity,disabled);return;}
  const available=descriptor.available!==false&&actionIds(inspection.available_actions).includes(descriptor.action),label=descriptorLabel(descriptor);
  if(!available){if(descriptor.reason)$('selection-actions').append(node('p',label+'：'+descriptor.reason,'action-reason'));return;}
  const input=descriptor.input;
  if(!input){$('selection-actions').append(button(label,()=>interact(descriptor.action,entity),{disabled,secondary:descriptor.action==='close'}));return;}
  const form=node('form',undefined,'descriptor-action'),caption=node('p',label,'descriptor-label');form.dataset.action=descriptor.action;form.dataset.actionKey=actionKey(descriptor);form.dataset.entityId=entity.id;if(['store','attach'].includes(descriptor.action))form.dataset.combination='true';form.append(caption);
  if(descriptor.use)form.append(node('p',friendlyInstruction(descriptor.use),'small'));
  let itemSelect,lotSelect,choiceSelect,quantityInput;
  const candidates=descriptorCandidates(descriptor),requiresEntity=['entity','entity_quantity'].includes(input.kind);
  if(requiresEntity){const field=node('label',['store','attach'].includes(descriptor.action)?'选择整件物品':['transfer','pour','ignite','fuel','tend'].includes(descriptor.action)?'用哪件物品作用于它':'选择物品');itemSelect=node('select');itemSelect.setAttribute('aria-label',label+'的物件');const placeholder=node('option','请选择');placeholder.value='';itemSelect.append(placeholder);
    for(const candidate of candidates){const option=node('option',candidateLabel(readableCandidate(candidate),candidates.map(readableCandidate)));option.value=candidate.id;itemSelect.append(option);}if(candidates.length===1)itemSelect.value=candidates[0].id;itemSelect.disabled=disabled||!candidates.length;field.append(itemSelect);form.append(field);}
  const hasLots=Array.isArray(input.lots)||candidates.some(candidate=>Array.isArray(candidate.lots));
  if(hasLots){const field=node('label','取出其中哪种材料');lotSelect=node('select');lotSelect.setAttribute('aria-label',label+'的材料批次');field.append(lotSelect);form.append(field);}
  if(input.kind==='choice'){const field=node('label','选择方式');choiceSelect=node('select');choiceSelect.setAttribute('aria-label',label+'的方式');for(const choice of input.choices||[]){if(!choice||typeof choice.value!=='string')continue;const option=node('option',choice.label||choice.value);option.value=choice.value;choiceSelect.append(option);}choiceSelect.disabled=disabled;field.append(choiceSelect);form.append(field);}
  if(input.quantity||['quantity','entity_quantity'].includes(input.kind)){const field=node('label','数量（份）');quantityInput=node('input');quantityInput.type='number';quantityInput.step='1';quantityInput.inputMode='numeric';quantityInput.setAttribute('aria-label',label+'的数量');quantityInput.disabled=disabled;field.append(quantityInput);form.append(field);}
  const preview=node('div',undefined,'candidate-preview');preview.setAttribute('aria-live','polite');
  const intent=node('p',undefined,'interaction-preview'),note=node('p',undefined,'small'),controls=node('div',undefined,'descriptor-controls'),submit=button('尝试'+label,()=>{}, {disabled:true});submit.type='submit';
  const cancel=button('取消选择',()=>{actionDrafts.delete(entity.id+':'+descriptor.action);form.reset();if(itemSelect)itemSelect.value='';update(true);},{disabled,secondary:true});controls.append(submit,cancel);form.append(intent,preview,note,controls);
  const currentDescriptor=()=>inspection?.entity?.id===entity.id&&!inspection.reading?actionDescriptors(inspection).find(option=>option.action===descriptor.action&&option.function_id!=='grove.rule_action')||{...descriptor,available:false,reason:'当前行动条件已改变，请按新状态重新选择。',input:{...descriptor.input,candidates:[]}}:descriptor;
  const selectedLot=()=>descriptorLots(currentDescriptor(),itemSelect?.value).find(lot=>(lot.id||lot.lot_id)===lotSelect?.value);
  function update(rebuildLots=false){
    const live=currentDescriptor(),liveCandidates=descriptorCandidates(live),candidate=liveCandidates.find(item=>item.id===itemSelect?.value);
    if(itemSelect){const selected=itemSelect.value,empty=node('option','请选择');empty.value='';itemSelect.replaceChildren(empty);
      for(const value of liveCandidates){const option=node('option',candidateLabel(readableCandidate(value),liveCandidates.map(readableCandidate)));option.value=value.id;itemSelect.append(option);}
      if(selected&&!liveCandidates.some(value=>value.id===selected)){const previous=node('option','原选择当前不可用，请重选');previous.value=selected;previous.disabled=true;itemSelect.append(previous);}itemSelect.value=selected;itemSelect.disabled=disabled||!liveCandidates.length;}
    const awaitingInspection=inspection?.entity?.id!==entity.id||!!inspection?.reading,awaitingPreview=candidates.some(item=>item.preview)&&awaitingInspection;
    const previewLines=awaitingPreview?['组合预览正在重新读取。你的选择保留，取得当前状态后再提交。']:candidatePreviewLines(candidate);preview.hidden=!previewLines.length;preview.replaceChildren(...previewLines.map(line=>node('p',line,'small')));
    if(lotSelect){const selected=lotSelect.value,options=descriptorLots(live,itemSelect?.value),empty=node('option','请选择材料');empty.value='';lotSelect.replaceChildren(empty);
      for(const lot of options){const option=node('option',materialLabel(lot,candidate?readableCandidate(candidate):entity)+' · '+slotLabel(lot.slot)+' · '+lot.quantity+' 单位');option.value=lot.id||lot.lot_id;lotSelect.append(option);}
      if(selected&&!options.some(lot=>(lot.id||lot.lot_id)===selected)){const previous=node('option','原材料批次当前不可用，请重选');previous.value=selected;previous.disabled=true;lotSelect.append(previous);}lotSelect.value=selected||(!selected&&rebuildLots&&options.length?(options[0].id||options[0].lot_id):'');lotSelect.disabled=disabled||!options.length;}
    const bounds=quantityBounds(live,selectedLot());if(quantityInput){quantityInput.min=String(bounds.min);if(Number.isFinite(bounds.max))quantityInput.max=String(bounds.max);else quantityInput.removeAttribute('max');if(!quantityInput.value||rebuildLots)quantityInput.value=String(bounds.value);}
    const quantity=quantityInput?Number(quantityInput.value):null,invalidQuantity=quantityInput&&(!Number.isInteger(quantity)||quantity<bounds.min||quantity>bounds.max);
    intent.textContent=operationIntentLabel(live,entity,{item:candidate?readableCandidate(candidate):null,lot:selectedLot(),amount:quantity,choice:choiceSelect?.selectedOptions[0]?.textContent});
    const candidateUnavailable=requiresEntity&&itemSelect.value&&!candidate||candidate?.preview?.available===false,lotUnavailable=lotSelect&&lotSelect.value&&!selectedLot();
    submit.disabled=disabled||actionBusy||!!client.pending()||awaitingInspection||live.available===false||candidateUnavailable||(requiresEntity&&!itemSelect.value)||(lotSelect&&!selectedLot())||(choiceSelect&&!choiceSelect.value)||!!invalidQuantity;
    note.textContent=awaitingInspection?'正在重新读取操作资格；输入保留，取得当前状态后再提交。':live.available===false?live.reason||'当前操作条件不满足。':candidateUnavailable?candidate?.preview?.reason||'所选物件的条件已经改变，请重新选择。':lotUnavailable?'所选材料批次已耗尽或条件改变，请重新选择材料。':!liveCandidates.length&&requiresEntity?'当前没有可选择的合适物件。':invalidQuantity?'数量须为 '+bounds.min+' 至 '+(Number.isFinite(bounds.max)?bounds.max:'可用量')+' 的整数。':live.cost?'成本：'+formatCost(live.cost):'提交时会重新核对当前物件和数量；取消选择不改变世界。';
  }
  form.addEventListener('refresh-candidate-preview',()=>update());
  itemSelect?.addEventListener('change',()=>update(true));lotSelect?.addEventListener('change',()=>update(true));choiceSelect?.addEventListener('change',()=>update());quantityInput?.addEventListener('input',()=>update());
  form.addEventListener('submit',event=>{event.preventDefault();update();if(submit.disabled||lotSelect&&!selectedLot())return;const extra={};if(lotSelect)extra.lot_id=lotSelect.value;if(choiceSelect)extra.mode=choiceSelect.value;if(['contained','surface'].includes(descriptor.arguments?.target_slot))extra.target_slot=descriptor.arguments.target_slot;const combination=form.dataset.combination==='true'?{form,fingerprint:descriptorCandidates(currentDescriptor()).find(candidate=>candidate.id===itemSelect?.value)?.preview?.version_fingerprint}:undefined;run(()=>interact(descriptor.action,entity,itemSelect?.value,quantityInput?Number(quantityInput.value):undefined,extra,combination));});
  update(true);restoreActionDraft(form,update);$('selection-actions').append(form);
}
async function interact(action,entity,itemId,amount,extra={},combination){
  stopWalking();
  if(amount!==undefined&&(!Number.isInteger(amount)||amount<1)){announce('请输入大于零的整数数量。',true);return;}
  const interactionEpoch=session;
  async function readInteractionEntity(id,label){
    try{return await client.query('grove.inspect',{entity_id:id});}
    catch(error){
      if(interactionEpoch!==session||!playing)return null;
      if(![403,404].includes(error.status))throw error;
      const message=label+'当前不可访问：'+(error.message||'未取得对象的读取权限。')+' 本次行动尚未提交，请重新读取后选择。';
      if(selectedId===entity.id){
        const previous=inspection?.entity?.id===entity.id?inspection.entity:entityById(entity.id);
        inspection=previous?{entity:previous,reading:true}:null;inspectionError=message;inspectionErrorStatus=error.status;
        for(const form of $('selection-actions').querySelectorAll('.descriptor-action')){form.reset();for(const select of form.querySelectorAll('select'))select.value='';}
      }
      if(combination?.form){combination.form.reset();for(const select of combination.form.querySelectorAll('select'))select.value='';}
      $('movement-feedback').textContent=message;announce(message,true);return null;
    }
  }
  let checkedCombination;
  if(['store','attach'].includes(action)){
    if(!playing||!current()||actionBusy)return;
    if(client.pending()){announce('先核对上一个操作，再继续。');renderPending();return;}
    if(!combination?.form||!/^[0-9a-f]{64}$/.test(combination.fingerprint||'')){announce('组合预览尚未取得。请重新读取当前物件后选择。',true);return;}
    const epoch=session,form=combination.form,focus=document.activeElement,controls=[...form.querySelectorAll('input,select,button')].map(control=>[control,control.disabled]);
    actionBusy=true;++readEpoch;++inspectEpoch;inspectionFlight=null;combinationCheckingForm=form;form.tabIndex=-1;form.focus({preventScroll:true});for(const [control] of controls)control.disabled=true;$('movement-feedback').textContent='正在核对你选择的组合…';renderPending();
    try{
      const latest=await readInteractionEntity(entity.id,'目标物件');if(!latest)return;
      if(epoch!==session||!playing||!current()||selectedId!==entity.id){if(epoch===session&&playing&&current()){$('movement-feedback').textContent='选择已改变，这次组合尚未提交。';announce('选择已改变，这次组合尚未提交。');}return;}
      inspection=latest;const option=actionDescriptors(latest).find(option=>option.action===action&&option.function_id!=='grove.rule_action'),candidate=descriptorCandidates(option||{}).find(candidate=>candidate.id===itemId);
      if(candidate?.preview?.version_fingerprint!==combination.fingerprint){const message='组合已改变，尚未提交。'+(candidate?.preview?.reason||option?.reason||'')+' 请查看当前预览，再点一次。';announce(message);$('movement-feedback').textContent=message;return;}
      checkedCombination=latest;
    }finally{
      if(epoch===session){actionBusy=false;renderPending();const retainFocus=document.activeElement===form;for(const [control,disabled] of controls)control.disabled=disabled;if(retainFocus&&focus?.isConnected)focus.focus({preventScroll:true});if(playing&&selectedId===entity.id)renderSelection();}
      if(combinationCheckingForm===form)combinationCheckingForm=null;
    }
  }
  await perform('grove.interact',async(isCurrent)=>{
    // Processes may change an object while a person is choosing water/tool inputs.
    // Read immediately before publishing the operation, rather than retain a closure's old version.
    const latest=checkedCombination||await readInteractionEntity(entity.id,'目标物件');
    if(!isCurrent()||!latest)return null;
    if(selectedId===entity.id){inspection=latest;renderSelection();}
    const descriptor=actionDescriptors(latest).find(option=>option.action===action&&option.function_id!=='grove.rule_action'),label=descriptor?descriptorLabel(descriptor):actionNames[action]||action;
    if(!actionIds(latest.available_actions).includes(action)||descriptor?.available===false)throw new Error('这件物件现在不能'+label+'。'+(descriptor?.reason||'已读取最新状态，请重新选择。'));
    if(itemId&&descriptor?.input&&['entity','entity_quantity'].includes(descriptor.input.kind)){const candidate=descriptorCandidates(descriptor).find(candidate=>candidate.id===itemId);if(!candidate||candidate.preview?.available===false)throw new Error(candidate?.preview?.reason||'所选物件的条件已经改变。请按当前现场重新选择。');}
    let item;
    if(itemId){const inspectedItem=await readInteractionEntity(itemId,'所选物件');if(!isCurrent()||!inspectedItem)return null;item=inspectedItem.entity;}
    if(extra.lot_id&&descriptor){const lot=descriptorLots(descriptor,itemId).find(candidate=>(candidate.id||candidate.lot_id)===extra.lot_id);if(!lot)throw new Error('所选材料批次当前不可转移。请重新选择。');const bounds=quantityBounds(descriptor,lot);if(amount<bounds.min||amount>bounds.max)throw new Error('所选材料的当前可转移数量不足。请重新选择数量。');}
    else if(amount!==undefined&&descriptor?.input?.quantity){const bounds=quantityBounds(descriptor);if(amount<bounds.min||amount>bounds.max)throw new Error('当前可操作数量已改变，请重新选择数量。');}
    const args={action,expected_player_version:point().version,...extra};
    if(combination)args.expected_combination_fingerprint=combination.fingerprint;
    if(extra.target_slot!==undefined&&extra.target_slot!==descriptor?.arguments?.target_slot)throw new Error('所选材料的目标位置已改变，尚未提交。请按当前状态重新选择。');
    if(extra.target_slot===undefined&&['contained','surface'].includes(descriptor?.arguments?.target_slot))args.target_slot=descriptor.arguments.target_slot;
    if(action==='drop')args.item_id=entity.id;else{args.target_id=entity.id;args.expected_target_version=latest.entity.version;if(itemId)args.item_id=itemId;}
    if(item)args.expected_item_version=item.version;
    if(action==='drop')args.expected_item_version=latest.entity.version;
    if(amount!==undefined)args.amount=amount;
    return args;
  },actionNames[action]||action);
}
function authorLabel(author){if(typeof author==='string')return (world?.players||[]).find(player=>player.id===author)?.name||(author.startsWith('awr_')?'世界中的另一位玩家':author);if(author&&typeof author==='object')return String(author.name||'创作者');return '创作者';}
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
  for(const anchor of anchors){const entity=anchor.entity,row=node('div',undefined,'content-anchor');row.append(button(entity?.name||anchor.entity_id,()=>run(()=>select(anchor.entity_id)),{secondary:true}),node('p',entity?stateLine(entity):'当前状态需查看物件','small'),node('small','原对象 '+anchor.entity_id+' · 版本 '+(entity?.version??anchor.version),'fact-reference'));anchorSection.append(row);}
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
  if(e.x===null||e.x===undefined||e.y===undefined)return;const p=screen(e.x,e.y),s=tileSize*.64*safePresentation(e).size,state=e.state||{};
  ctx.save();ctx.translate(p.x,p.y);ctx.shadowColor='#24342755';ctx.shadowBlur=4;ctx.shadowOffsetY=3;
  if(state.destroyed){ctx.fillStyle='#5b6250';for(let i=0;i<4;i++){ctx.beginPath();ctx.ellipse((i-1.5)*s*.2,s*.2,s*.13,s*.06,i,0,Math.PI*2);ctx.fill();}}
  else if(state.kind==='composed'||e.presentation||state.program||e.creative){
    const appearance=safePresentation(e);ctx.shadowBlur=3;
    if(appearance.shape==='bowl'){ctx.fillStyle=appearance.color;ctx.strokeStyle='#536451';ctx.beginPath();ctx.ellipse(0,s*.04,s*.47,s*.32,0,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.fillStyle=state.open?'#394b3c':'#b4b69c';ctx.beginPath();ctx.ellipse(0,-s*.06,s*.36,s*.19,0,0,Math.PI*2);ctx.fill();}
    else if(appearance.shape==='beam'){ctx.rotate(-.1);roundRect(-s*.52,-s*.18,s*1.04,s*.36,4,appearance.color,'#536451');}
    else if(appearance.shape==='cloth'){ctx.fillStyle=appearance.color;ctx.strokeStyle='#536451';ctx.beginPath();ctx.moveTo(-s*.42,-s*.28);ctx.lineTo(s*.34,-s*.37);ctx.lineTo(s*.44,s*.29);ctx.lineTo(-s*.35,s*.36);ctx.closePath();ctx.fill();ctx.stroke();}
    else if(appearance.shape==='raft'){roundRect(-s*.48,-s*.31,s*.96,s*.62,4,appearance.color,'#536451');ctx.strokeStyle='#647054';for(const y of [-.15,0,.15]){ctx.beginPath();ctx.moveTo(-s*.45,y*s);ctx.lineTo(s*.45,y*s);ctx.stroke();}}
    else{roundRect(-s*.4,-s*.36,s*.8,s*.72,appearance.shape==='stone'?8:4,appearance.color,'#536451');}
    ctx.shadowBlur=0;ctx.fillStyle='#f7f2d8';ctx.font='600 '+Math.round(s*.38)+'px system-ui';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(appearance.glyph,0,0);ctx.textBaseline='alphabetic';
    if(e.relations?.parent?.relation==='attached'){ctx.fillStyle='#354f3c';ctx.font='600 12px system-ui';ctx.fillText('⌁',s*.4,s*.44);}
  }else if(state.recipe==='walkway'){
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
  if(e.lifecycle==='broken'||state.broken){ctx.strokeStyle='#513c31';ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(-s*.12,-s*.25);ctx.lineTo(s*.05,-s*.06);ctx.lineTo(-s*.03,s*.12);ctx.lineTo(s*.14,s*.3);ctx.stroke();}
  if(state.wetness>0&&!state.destroyed){ctx.fillStyle='#c3eff0cc';ctx.beginPath();ctx.moveTo(s*.29,-s*.35);ctx.bezierCurveTo(s*.05,-s*.05,s*.4,s*.08,s*.48,-s*.08);ctx.bezierCurveTo(s*.47,-s*.18,s*.35,-s*.29,s*.29,-s*.35);ctx.fill();}
  if((state.burning||state.recipe==='hearth'&&state.lit)&&!state.destroyed){const f=1+Math.sin(now/90+e.x)*.08;ctx.fillStyle='#f5a852';ctx.beginPath();ctx.moveTo(-s*.28,s*.1);ctx.quadraticCurveTo(-s*.36,-s*.27,-s*.05,-s*.66*f);ctx.quadraticCurveTo(s*.03,-s*.18,s*.22,-s*.42);ctx.quadraticCurveTo(s*.41,-s*.04,s*.2,s*.15);ctx.closePath();ctx.fill();ctx.fillStyle='#ffe6a1';ctx.beginPath();ctx.moveTo(-s*.12,s*.12);ctx.quadraticCurveTo(-s*.17,-s*.16,s*.03,-s*.35*f);ctx.quadraticCurveTo(s*.24,0,s*.1,s*.14);ctx.fill();}
  if(blocks(e)){ctx.strokeStyle='#624633';ctx.lineWidth=2;ctx.setLineDash([4,3]);ctx.strokeRect(-tileSize*.45,-tileSize*.42,tileSize*.9,tileSize*.84);ctx.setLineDash([]);}
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
$('refresh').addEventListener('click',()=>run(async()=>{stopWalking();const epoch=session;await refreshWorld(true);if(epoch===session&&playing)announce('已读取最新附近现场。');}));
$('backup').addEventListener('click',()=>{if(!key)return;const blob=new Blob([JSON.stringify(key,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=node('a');a.href=url;a.download='shared-grove-identity.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);announce('恢复文件包含你的身份密钥，请保存在自己的安全位置。');});
$('follow-player').addEventListener('click',()=>{if(point()){camera={x:point().x,y:point().y};canvas.focus({preventScroll:true});}});$('dismiss-onboarding').addEventListener('click',()=>{$('onboarding').hidden=true;});
$('selection-close').addEventListener('click',()=>{rememberActionDrafts();closePanels();canvas.focus({preventScroll:true});});
$('context-toggle').addEventListener('click',()=>{if(document.body.classList.contains('context-open'))closePanels();else openContext();});
$('inventory-toggle').addEventListener('click',()=>{const open=!$('inventory-panel').open;rememberActionDrafts();closePanels();$('inventory-panel').open=open;document.body.classList.toggle('inventory-open',open);$('inventory-toggle').setAttribute('aria-expanded',String(open));});
$('inventory-panel').addEventListener('toggle',()=>{document.body.classList.toggle('inventory-open',$('inventory-panel').open);$('inventory-toggle').setAttribute('aria-expanded',String($('inventory-panel').open));});
$('inventory-first').addEventListener('click',()=>run(()=>changeInventoryPage('first')));
$('inventory-previous').addEventListener('click',()=>run(()=>changeInventoryPage('previous')));
$('inventory-next').addEventListener('click',()=>run(()=>changeInventoryPage('next')));
$('agent-toggle').addEventListener('click',()=>{const open=!document.body.classList.contains('agent-open');rememberActionDrafts();if(open){openAgent();$('chat-text').focus({preventScroll:true});}else closePanels();});
$('agent-close').addEventListener('click',()=>{closePanels();canvas.focus({preventScroll:true});});
$('ask-selection').addEventListener('click',()=>run(async()=>{if(!world)return;const entity=selectedEntity(),tile=selectedTile,location=entityLocation(entity);const text=entity?'我正在看'+entity.name+'（'+(location.x??point().x)+'，'+(location.y??point().y)+'）。请读取当前现场，解释它能怎样用，以及不同办法的材料和环境取舍。':tile?'我正在看位置（'+tile.x+'，'+tile.y+'）。请读取当前现场，解释在这里走动或搭建设施有哪些取舍。':'请读取我身边的现场，解释我现在有哪些不同的办法。';openAgent();await hostCommand('prompt',{text,intent:$('host-intent').value.trim(),...(entity?{context_entity_id:entity.id}:{})});}));
$('recover-operation').addEventListener('click',()=>run(()=>recoverWorldOperation()));
$('release-operation').addEventListener('click',()=>run(()=>recoverWorldOperation(true)));
$('reconnect-world').addEventListener('click',()=>run(async()=>{const epoch=session;await authenticate(key);if(epoch!==session)return;if(await hostCommand('recover',{token:client.token,public_key:key.public_key,name:point()?.name||prefs().name},{refresh:true})){scheduleWorld();scheduleHost();}}));
$('chat-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const text=$('chat-text').value.trim();if(!text)return;const entity=selectedEntity();if(await hostCommand('prompt',{text,intent:$('host-intent').value.trim(),...(entity?{context_entity_id:entity.id}:{})})){$('chat-text').value='';client.saveDraft('grove-chat','');intentDirty=false;}});});
$('execution-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{
  const goal=$('execution-goal').value.trim(),maxActions=Number($('execution-max-actions').value),maxRequests=Number($('execution-max-requests').value),execution=host?.execution||{};
  if(!goal||hostCommandBusy||actionBusy||client.pending()||execution.delegation_active||['starting','running','unknown'].includes(execution.status)||(execution.receipts||[]).some(receipt=>receipt.status==='unknown'))return;
  if(goal.length>2000)throw new Error('委托目标最多 2000 字。');
  if(!Number.isInteger(maxActions)||maxActions<1||maxActions>64)throw new Error('目标动作上限须为 1 至 64 的整数。');
  if(!Number.isInteger(maxRequests)||maxRequests<1||maxRequests>8)throw new Error('目标模型请求上限须为 1 至 8 的整数。');
  stopWalking();const entity=selectedEntity();
  if(await hostCommand('execute',{goal,max_actions:maxActions,max_requests:maxRequests,compact_history:$('execution-compact-history').checked,...(entity?{context_entity_id:entity.id}:{})})){goalDirty=false;$('execution-compact-history').checked=false;client.saveDraft('grove-goal',goal);announce('目标已交给你的 Agent。它可在总额度内接续执行，你可以随时收回后续行动权。');}
});});
$('execution-goal').addEventListener('input',()=>{goalDirty=true;if(current())try{client.storage.setItem(client.draftKey('grove-goal'),$('execution-goal').value);}catch{}});
$('execution-resume').addEventListener('click',()=>run(async()=>{
  const execution=host?.execution||{};
  const additional=Number($('execution-additional-requests').value),canResume=execution.resume_allowed===true||(execution.extension_allowed===true&&additional>0);
  if(hostCommandBusy||executionReclaimBusy||actionBusy||client.pending()||execution.delegation_active||['starting','running','unknown'].includes(execution.status)||['connecting','thinking','disconnected'].includes(host?.status)||(execution.receipts||[]).some(receipt=>receipt.status==='unknown')||!canResume||!execution.task_id)return;
  if(!Number.isInteger(additional)||additional<0||(execution.max_requests||0)+additional>8)throw new Error('追加后的原目标模型请求总额度不能超过 8 次。');
  stopWalking();
  if(await hostCommand('execute-resume',{task_id:execution.task_id,compact_history:$('execution-compact-history').checked,additional_requests:additional})){$('execution-compact-history').checked=false;$('execution-additional-requests').value='0';announce('已再次授权接续原目标；保留原行动记录与已用额度，先读取最新现场。');}
}));
$('execution-additional-requests').addEventListener('input',renderExecution);
$('execution-reclaim').addEventListener('click',()=>run(reclaimExecution));$('execution-reclaim-quick').addEventListener('click',()=>run(reclaimExecution));
$('execution-recover').addEventListener('click',()=>run(()=>hostCommand('recover',{token:client.token,public_key:key.public_key,name:point()?.name||prefs().name},{refresh:true})));
$('rule-catalog-read').addEventListener('click',()=>run(async()=>{
  if(!playing||!current())return;const epoch=session,control=$('rule-catalog-read');control.disabled=true;$('rule-catalog-result').replaceChildren(node('p','正在读取当前规则能力…','small'));
  try{const result=await client.query('grove.rule_catalog',{});if(epoch!==session||!playing)return;$('rule-catalog-result').replaceChildren();appendFacts($('rule-catalog-result'),result);}
  catch(error){if(epoch===session&&playing)$('rule-catalog-result').replaceChildren(node('p',error.message||'尚未取得规则目录。','action-reason'));}
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
  stopWalking();++session;++readEpoch;++hostReadEpoch;++inspectEpoch;++hostCommandEpoch;++definitionReadEpoch;
  playing=false;entering=false;actionBusy=false;hostCommandBusy=false;executionReclaimBusy=false;
  chosenBuildMaterials.clear();touchedBuildChoices.clear();activeRainChunks.clear();factDisclosures.clear();actionDrafts.clear();activeActionKey=null;hostCommandKind=null;hostCommandFlight=null;
  clearTimeout(worldTimer);clearTimeout(hostTimer);client.cancel();client.token=null;identity=null;world=null;inspection=null;inspectionError=null;inspectionErrorStatus=null;inspectionFlight=null;selectedId=null;selectedTile=null;combinationCheckingForm=null;
  ledger.clear();streamCursor=null;lastSelectionSignature='';lastInventorySignature='';lastChatSignature='';lastGenerationSignature='';lastContentSignature='';contentHistoryId=null;contentHistoryPages=[];contentHistoryBusy=false;operationRecoveryBusy=false;inventoryPageBusy=false;inventoryPageCursors=[null];intentDirty=false;goalDirty=false;
  definitionReadBusy=false;definitionReadValue=null;definitionReadError=null;definitionReadRef=null;definitionReadOpen=false;$('rule-catalog-result').replaceChildren();$('rule-catalog-read').disabled=false;
  closePanels();document.body.classList.remove('playing');$('game').hidden=true;$('entry').hidden=false;$('world-status').textContent='已离开 · 可接续';$('entry-progress').replaceChildren();$('enter').disabled=false;
  announce('已离开网页。身份、目标进度与变化保留；已委托行动仍受这个目标的总额度约束。离开网页不等于收回行动权。');
});
window.addEventListener('resize',resize);window.addEventListener('pagehide',()=>{stopWalking();client.cancel();});document.addEventListener('visibilitychange',()=>{if(document.hidden)stopWalking();else if(playing){run(()=>refreshWorld());scheduleHost(250);}});
window.addEventListener('pageshow',event=>{if(event.persisted&&playing){run(()=>refreshWorld(true));scheduleWorld();scheduleHost(250);}});
new ResizeObserver(resize).observe(canvas);requestAnimationFrame(draw);readIdentity();
