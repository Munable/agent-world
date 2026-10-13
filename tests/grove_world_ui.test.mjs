import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(new URL('../agent_world/grove_web/grove-world-ui.js',import.meta.url),'utf8');
const ui=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const pageSource=readFileSync(new URL('../agent_world/grove_web/grove.js',import.meta.url),'utf8');
const extract=(start,end)=>{const from=pageSource.indexOf(start),to=pageSource.indexOf(end,from+start.length);assert(from>=0&&to>from, 'The actual browser function must exist');return pageSource.slice(from,to);};
const row=(id,label,value,extra={})=>({id,label,value,type:typeof value==='boolean'?'boolean':typeof value==='number'?'number':'text',availability:value===null?'unknown':'known',temporal_scope:'current',source:'world',...extra});
const entity=(id,name)=>({id,name,version:3,x:0,y:0,appearance:{shape:'cloth',color:'#112233',glyph:'■',size:1,overlays:{}},materials:[],summary:'服务器摘要',summary_facts:[],fact_sections:[],relations:{parent:null,children:[]},custody:{scope:'public',holder_id:null,container_id:null},actions:[]});
const action=(extra={})=>({id:'world.opaque:do',action:'opaque-do',function_id:'world.opaque',label:'服务器操作名称',description:'服务器操作说明',available:true,expanded:true,fixed_arguments:{action:'opaque-do',target_id:'target',expected_target_version:3,expected_target_interaction_revision:2,expected_player_version:1},confirmation_keys:['target_id','expected_target_interaction_revision','terms_version','expected_combination_fingerprint','expected_item_version'],controls:[],read_request:{function_id:'grove.client_inspect',arguments:{entity_id:'target',action:'opaque-do'}},preconditions:[],...extra});
const replyContext={factLines:ui.factLines,entityById:()=>null};
vm.createContext(replyContext);vm.runInContext(extract('function currentAssistantReply(','function button('),replyContext);
{
  const execution={task_id:'same-task',action_summary:{task_id:'same-task',scope:'current_task_retained_confirmed_actions',confirmed_count:3,
    material_costs:[{material:'wood',quantity_u:30}],coverage:{receipt_history_complete:true}}};
  const lines=ui.executionTaskSummaryLines(execution);
  assert(lines.some(line=>line.includes('3 次行动')&&line.includes('各段执行')));
  assert(lines.some(line=>line.includes('木 30 份')&&line.includes('不包含')));
  assert(!lines.some(line=>line.includes('零消耗')));
  execution.action_summary.coverage={receipt_history_complete:false,operations_without_structured_costs:1,unknown_count:1};
  const partial=ui.executionTaskSummaryLines(execution);
  assert(partial.some(line=>line.includes('仅统计仍保留')));
  assert(partial.some(line=>line.includes('不表示零消耗')));
  assert(partial.some(line=>line.includes('待核对')));
  execution.action_summary.task_id='different-task';
  assert.deepEqual(ui.executionTaskSummaryLines(execution),[]);
}

// An old native final remains readable history, but cannot become this request's reply.
{
  const message={role:'assistant',text:'原模型正文保持不变',task_id:'original-task',request_number:1,
    response_message_id:'native-final',session_id:'native-session',time:1791781456};
  const value={status:'ready',completed:true,message_id:'native-final',session_id:'native-session',
    execution:{task_id:'original-task',requests_used:1,status:'completed'},messages:[message]};
  assert.equal(replyContext.currentAssistantReply(value),message);
  assert.ok(replyContext.agentMessageLabel(message,value).includes('本次委托回复'));
  for(const changed of [
    {...value,status:'paused'}, {...value,reclaim_in_progress:true}, {...value,unknown_operations:1},
    {...value,completed:false}, {...value,message_id:'next-final'}, {...value,session_id:'other-session'},
    {...value,execution:{...value.execution,status:'starting'}},
    {...value,execution:{task_id:'next-task',requests_used:1}},
    {...value,execution:{task_id:'original-task',requests_used:2}}
  ])assert.equal(replyContext.currentAssistantReply(changed),null);
  assert.ok(replyContext.agentMessageLabel(message,{...value,execution:{task_id:'next-task'}}).includes('历史委托'));
  assert.ok(replyContext.agentMessageLabel(message,{...value,status:'paused'}).includes('本次委托历史'));
  const legacy={role:'assistant',text:'旧正文'};
  assert.equal(replyContext.currentAssistantReply({...value,messages:[legacy]}),null);
  assert.ok(replyContext.agentMessageLabel(legacy,value).includes('请求来源未记录'));
  const ordinary={...message,task_id:null,request_number:null};
  assert.equal(replyContext.currentAssistantReply({...value,messages:[ordinary]}),ordinary);
  assert.equal(replyContext.currentAssistantReply({...value,execution:{task_id:'next-task',status:'starting'},messages:[ordinary]}),null);
  assert.equal(message.text,'原模型正文保持不变');
}


// Typed unknown, zero and false facts stay different; author names cannot
// create a material, a mechanism, an operation or a previous numeric value.
{
  const sample=entity('object','任意名字');
  sample.summary_facts=[row('zero','实际零',0,{unit:'u'}),row('false','实际否',false),row('unknown','未读',null)];
  sample.materials=[{id:'lot',material_id:'author#material',material_name:'作者材料',quantity:3,slot:'body'}];
  sample.relations.parent={id:'parent',relation:'attached'};
  for(const key of ['state','program','capabilities','components','creative'])Object.defineProperty(sample,key,{get(){throw new Error('Storage must not be read');}});
  assert.deepEqual(ui.simulationStats(sample),[['实际零','0 u'],['实际否','否'],['未读','未公开或不可读']]);
  assert.equal(ui.materialLabel('author#material',sample),'作者材料');
  assert.equal(ui.bodyQuantities(sample)[0].quantity,3);
  assert.deepEqual(ui.matterLots({}),[]);
  assert.equal(ui.entitySummary(sample),'服务器摘要 · 绑定在物件上');
  assert.equal(ui.presentationBadgeText({...sample,appearance:{badge:{label:'次数',value:99,known:false}}}),'次数 ?');
  assert.equal(ui.installedInteractionDefinition({installable:true}),true);
  assert.equal(ui.installedInteractionDefinition({installable:false}),false);
  const groups=[{id:'saved',label:'历史输出',type:'group',rows:[row('energy','保存能量',0,{temporal_scope:'historical'})]}];
  assert.deepEqual(ui.factLines(groups),['历史输出 · 保存能量：0']);
}
{
  const unsafe={appearance:{shape:'<script>',color:'url(secret)',glyph:'<img src=x>',size:99,pose:'teleport',badge:{label:'bad\u202e',value:Infinity,known:true}}};
  const safe=ui.safePresentation(unsafe);
  assert.equal(safe.shape,'part');assert.equal(safe.color,'#aaac8c');assert.equal(safe.glyph,'◆');assert.equal(safe.size,1);assert.equal(safe.pose,undefined);
  assert.deepEqual(safe.badge,{label:'',known:false,value:null});
}

// A creation binds exact source, batch, quantity and the person's observed
// source version. Reordering candidates cannot substitute or renew a source.
{
  const menu={cost_u:{wood:3,cloth:2},action:{choices:{sources:[
    {id:'wood',version:4,lots:[{id:'wood-lot',material:'wood',quantity:8,slot:'body'}]},
    {id:'cloth',version:5,lots:[{id:'cloth-lot',material:'cloth',quantity:2,slot:'body'}]}]}}};
  const rows=[{entity_id:'wood',expected_entity_version:4,lot_id:'wood-lot',material_id:'wood',quantity_u:'3'},
    {entity_id:'cloth',expected_entity_version:5,lot_id:'cloth-lot',material_id:'cloth',quantity_u:'2'}];
  assert.equal(ui.fabricationPlan(menu,rows).errors.length,0);
  const changed=structuredClone(menu);changed.action.choices.sources.reverse();
  assert.equal(ui.fabricationPlan(changed,rows).sources[0].expected_entity_version,4);
  changed.action.choices.sources[1].version=6;
  assert.ok(ui.fabricationPlan(changed,rows).errors.some(value=>value.includes('观察版本')));
  changed.action.choices.sources[1].lots=[];assert.ok(ui.fabricationPlan(changed,rows).errors.length>0);
  assert.ok(ui.fabricationPlan(menu,[...rows,rows[0]]).errors.some(value=>value.includes('一行')));
  assert.ok(ui.fabricationPlan(menu,[{...rows[0],quantity_u:'2'},rows[1]]).errors.length>0);
}

// Real geometry consumes only appearance. Values and labels belong to the
// final measured layout pass, so badges do not overlap a player name.
{
  const calls=[],canvasContext=new Proxy({}, {get:(_,name)=>(...args)=>{calls.push([name,...args]);},set:()=>true});
  const context={ctx:canvasContext,tileSize:40,screen:()=>({x:0,y:0}),safePresentation:ui.safePresentation,mechanicalMotion:{read:()=>null},worldFresh:true,selectedId:null};
  vm.createContext(context);vm.runInContext(extract('function drawEntity(','function entityMapLabel('),context);
  const actual=entity('actual','任意名字');actual.appearance.pose='folded';actual.appearance.overlays.burning=true;actual.appearance.badge={label:'次数',value:2,known:true};
  context.drawEntity(actual,1000);
  assert.ok(calls.some(([name,x,y])=>name==='scale'&&x===1&&y===.4));
  assert.ok(calls.some(([name])=>name==='quadraticCurveTo'));
  assert.ok(!calls.some(([name,text])=>name==='fillText'&&text==='次数 2'));
  assert.equal(actual.x,0);assert.equal(actual.y,0);
}
{
  const objects=['甲','乙','丙','丁'].map((name,index)=>({...entity(String.fromCharCode(97+index),name),appearance:{shape:'cloth',glyph:'■',badge:{label:'数',known:true,value:[2,3,7,9][index]}}}));
  const labels=[],rectangles=[],context={canvasWidth:600,canvasHeight:400,tileSize:40,selectedId:'a',hoverTile:{x:0,y:0},world:{players:[]},publicWorkMarker:null,mapLabelSlots:new Map(),
    ground:()=>objects,point:()=>({id:'self',x:3,y:0}),screen:(x,y)=>({x:300+x*40,y:200+y*40}),client:{roleId:'self'},
    safePresentation:ui.safePresentation,entityGlyph:ui.entityGlyph,presentationBadgeText:ui.presentationBadgeText,visibleWaterQuantity:ui.visibleWaterQuantity,
    roundRect:(x,y,w,h)=>rectangles.push({x,y,w,h}),ctx:{save(){},restore(){},measureText:text=>({width:String(text).length*6}),fillText:(text,x,y)=>labels.push({text,x,y})}};
  vm.createContext(context);vm.runInContext(extract('function entityMapLabel(','function drawPlayer('),context);context.drawMapLabels();
  assert.ok(labels.some(row=>row.text.includes('甲')&&row.text.includes('数 2')));
  assert.ok(labels.some(row=>row.text.includes('乙')&&row.text.includes('数 3')));
  assert.ok(labels.some(row=>row.text.includes('＋1')));
  assert.ok(!labels.some(row=>row.text.includes('数 21')));
  for(const rect of rectangles)assert(rect.x>=0&&rect.y>=0&&rect.x+rect.w<=600&&rect.y+rect.h<=400);
  for(let i=0;i<rectangles.length;i++)for(let j=i+1;j<rectangles.length;j++){const a=rectangles[i],b=rectangles[j];assert(!(a.x<b.x+b.w&&a.x+a.w>b.x&&a.y<b.y+b.h&&a.y+a.h>b.y));}
  const oldSlot=[...context.mapLabelSlots.values()][0].slot;objects[0].appearance.badge.value=1;context.drawMapLabels();assert.equal([...context.mapLabelSlots.values()][0].slot,oldSlot);
}

// Only the finite server dependency data filters a candidate. Opaque action
// names are irrelevant to form shape, eligibility and argument placement.
{
  const context={};vm.createContext(context);vm.runInContext(extract('const sameValue=','function renderDescriptorEditor('),context);
  const control={choices:[{value:'one',when:{port:['wet']}},{value:'two',when:{port:['drive']}}]};
  assert.equal(context.controlChoices(control,{port:'wet'})[0].value,'one');
  assert.equal(context.controlChoices(control,{port:'missing'}).length,0);
}

function formNode(tag,text,className){
  const listeners=new Map();
  const value={tagName:tag.toUpperCase(),textContent:text||'',className:className||'',dataset:{},children:[],disabled:false,isConnected:true,_value:'',selected:false,multiple:false,
    get options(){return this.children.filter(child=>child.tagName==='OPTION');},
    get value(){if(this.tagName==='SELECT')return this.options.find(option=>option.selected)?.value||this.options[0]?.value||'';return this._value;},
    set value(raw){this._value=String(raw);if(this.tagName==='SELECT')for(const option of this.options)option.selected=option.value===this._value;},
    get selectedOptions(){return this.options.filter(option=>option.selected);},
    append(...children){for(const child of children){child.parent=this;child.isConnected=this.isConnected;this.children.push(child);}},
    replaceChildren(...children){this.children=[];this.append(...children);},
    remove(){if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);this.isConnected=false;},
    setAttribute(name,data){this[name]=String(data);},removeAttribute(name){delete this[name];},
    checkValidity(){if(this.disabled)return true;if(this.required&&!this.value&&this.type!=='checkbox')return false;if(this.type==='number'&&this.value!==''){const number=Number(this.value);return Number.isFinite(number)&&(this.min===undefined||number>=Number(this.min))&&(this.max===undefined||number<=Number(this.max))&&(this.step!=='1'||Number.isInteger(number));}return true;},
    addEventListener(type,handler){listeners.set(type,[...(listeners.get(type)||[]),handler]);},
    dispatchEvent(event){for(const handler of listeners.get(event.type)||[])handler(event);},
    querySelectorAll(selector){const tags=selector.toUpperCase().split(','),matches=child=>selector==='[data-control-id]'?!!child.dataset.controlId:tags.includes(child.tagName);return this.children.flatMap(child=>[...(matches(child)?[child]:[]),...child.querySelectorAll(selector)]);},
    querySelector(selector){return this.querySelectorAll(selector)[0]||null;}};
  return value;
}
{
  const target=entity('target','同一目标'),actions=formNode('section'),writes=[],descriptor=action({controls:[
    {id:'item_id',path:['item_id'],label:'来源',kind:'choice',required:true,schema:{},choices:[{value:'a',label:'来源甲',arguments_patch:{expected_item_version:4}},{value:'b',label:'来源乙',arguments_patch:{expected_item_version:7}}]},
    {id:'lot_id',path:['lot_id'],label:'批次',kind:'choice',required:true,schema:{},choices:[{value:'a-lot',label:'甲批次',when:{item_id:['a']},maximum:3,arguments_patch:{}},{value:'b-lot',label:'乙批次',when:{item_id:['b']},maximum:7,arguments_patch:{}}]},
    {id:'params.amount',path:['params','amount'],label:'投入',kind:'integer',required:true,default:2,schema:{minimum:1,maximum:5},maximum_from_control_id:'lot_id'},
    {id:'params.enabled',path:['params','enabled'],label:'真假输入',kind:'boolean',required:true,default:false,schema:{}}]});target.actions=[descriptor];
  const context={inspection:{entity:target,context:{snapshot_seq:1}},actionBusy:false,actionDrafts:new Map(),interactionSelections:new Map(),node:formNode,$:()=>actions,
    actionKey:value=>value.id,actionDescriptors:ui.actionDescriptors,descriptorLabel:ui.descriptorLabel,appendFactRows(){},restoreActionDraft(){},run:fn=>fn(),client:{pending:()=>null}};
  vm.createContext(context);vm.runInContext(extract('function button(','function announce(')+extract('const setArgument=','function authorLabel('),context);
  context.submitDescriptor=(...args)=>writes.push(args);context.renderDescriptorEditor(descriptor,target,false);
  const form=actions.children[0],[item,lot]=form.querySelectorAll('select'),[amount]=form.querySelectorAll('input'),submit=form.querySelectorAll('button').find(value=>value.type==='submit');
  item.value=JSON.stringify('a');item.dispatchEvent({type:'change'});lot.value=JSON.stringify('a-lot');lot.dispatchEvent({type:'change'});
  assert.equal(amount.max,'3');assert.equal(submit.disabled,false);
  amount.value='4';amount.dispatchEvent({type:'input'});assert.equal(submit.disabled,true);
  amount.value='2';amount.dispatchEvent({type:'input'});item.value=JSON.stringify('b');item.dispatchEvent({type:'change'});
  assert.equal(lot.value,JSON.stringify('a-lot'));assert.equal(lot.options.find(option=>option.value===JSON.stringify('a-lot')).disabled,true);assert.equal(submit.disabled,true);
  lot.value=JSON.stringify('b-lot');lot.dispatchEvent({type:'change'});assert.equal(amount.max,'5');assert.equal(submit.disabled,false);
  form.dispatchEvent({type:'submit',preventDefault(){}});assert.equal(writes.length,1);
  assert.equal(writes[0][2].find(entry=>entry.control.id==='item_id').value,'b');assert.equal(writes[0][2].find(entry=>entry.control.id==='params.amount').value,2);
  assert.equal(writes[0][2].find(entry=>entry.control.id==='params.enabled').value,false);
  context.inspection={...context.inspection,reading:true};form.dispatchEvent({type:'refresh-candidate-preview'});assert.equal(amount.value,'2');assert.equal(submit.disabled,true);
}

// Prepare the actual operation: refresh may renew the player's observation,
// while the original target intent and selected source guards remain fixed.
for(const mode of ['valid','revision','fingerprint','source','access','target_changed']){
  const original=action({controls:[{id:'item_id',path:['item_id'],kind:'choice',required:true,schema:{},choices:[{value:'source',label:'原来源',arguments_patch:{expected_item_version:4,expected_combination_fingerprint:'same'}}]}]}),current=structuredClone(original),writes=[];
  current.fixed_arguments.expected_target_version=8;current.fixed_arguments.expected_player_version=9;
  if(mode==='revision')current.fixed_arguments.expected_target_interaction_revision=3;
  if(mode==='fingerprint')current.controls[0].choices[0].arguments_patch.expected_combination_fingerprint='changed';
  if(mode==='source')current.controls[0].choices=[];
  const selected=[{control:original.controls[0],value:'source',choices:original.controls[0].choices}],context={session:1,selectedId:'target',selectedTile:null,inspection:null,actionDrafts:new Map(),actionBusy:false,lastSelectionSignature:'',
    stopWalking(){},renderSelection(){},actionDescriptors:ui.actionDescriptors,descriptorLabel:ui.descriptorLabel,structuredClone,
    client:{query:async()=>{if(mode==='access')throw Object.assign(new Error('Object unavailable'),{status:403});if(mode==='target_changed')context.selectedId='another';return {player:{version:9},entity:{id:'target',actions:[current]}};}},
    perform:async(_name,prepare)=>{writes.push(await prepare(()=>true));return false;}};
  vm.createContext(context);vm.runInContext(extract('function actionReadTarget(','function authorLabel('),context);
  if(mode==='valid'){await context.submitDescriptor(original,{id:'target'},selected);assert.equal(writes[0].expected_target_version,3);assert.equal(writes[0].expected_target_interaction_revision,2);assert.equal(writes[0].expected_item_version,4);assert.equal(writes[0].expected_combination_fingerprint,'same');assert.equal(writes[0].expected_player_version,9);assert.equal(writes[0].item_id,'source');}
  else{await assert.rejects(context.submitDescriptor(original,{id:'target'},selected));assert.equal(writes.length,0);}
}

// The panel carries a task read reference. Fresh DTO facts are a separate
// observation and never inherit the task's old read time or completion claim.
{
  const execution={readback_actual:true,readback:{action_targets:[{entity_id:'target',name:'原对象',observed_at:1791781456,client_read_request:{function_id:'grove.client_inspect',arguments:{entity_id:'target'}}},{entity_id:'missing',unavailable:{message:'当前不可访问'}}]}};
  const lines=replyContext.actionTargetReadbackLines(execution);
  assert.ok(lines.some(line=>line.includes('原任务读取于')&&line.includes('当前详情需重新读取')));
  assert.ok(lines.some(line=>line.includes('当前不可访问')));
  assert.equal(replyContext.actionTargetReadbackLines({...execution,readback_actual:false}).length,0);
  const selected=entity('target','任意名字');selected.summary_facts=[row('known','实际值',0)];
  assert.deepEqual(replyContext.objectFactLines({entity:selected}),replyContext.objectFactLines({entity:{...selected,name:'另一个名字'}}));
}

// Replay the bounded delta between two real motion samples, including a full
// turn with the same modulo endpoint. No wall-clock time creates another turn.
{
  const moving=(angle,version,replay_allowed=true,kind='rotation')=>({id:'actual',version,x:0,y:1,appearance:{motion:{kind,angle,observed_tick:version,position:5,travel:5,replay_allowed}}});
  const motion=ui.createMechanicalMotion();motion.accept([moving(0,1)],{epoch:'same'},0);motion.accept([moving(1,2)],{epoch:'same'},100);
  assert.equal(motion.read(moving(1,2),260).angle,.5);assert.equal(motion.read(moving(1,2),10000).angle,1);
  motion.accept([moving(2,3,true,'linear_actuator')],{epoch:'same'},10100);
  assert.equal(motion.read(moving(2,3,true,'linear_actuator'),10260).angle,1.5);assert.equal(motion.read(moving(2,3),10260,false).angle,2);
  motion.accept([moving(3,4,false)],{epoch:'same'},10300);assert.equal(motion.read(moving(3,4,false),10310).angle,3);
  motion.reset();motion.accept([moving(6,7)],{epoch:'same'},10400);assert.equal(motion.read(moving(6,7),10401).angle,6);
  motion.accept([moving(26,8)],{epoch:'same'},10500);assert.equal(motion.read(moving(26,8),10950).angle,16);assert.equal(motion.read(moving(26,8),11500).angle,26);
  assert.equal(motion.read({id:'private',appearance:{}},10402),null);
  assert.equal(ui.visibleWaterQuantity({appearance:{overlays:{water_quantity:null}}}),null);
  assert.equal(ui.visibleWaterQuantity({appearance:{overlays:{water_quantity:0}}}),0);
  assert.equal(ui.visibleWaterQuantity({appearance:{overlays:{water_quantity:9}}}),9);
}
{
  const receipt={status:'confirmed',commit_seq:7,confirmed_outcome:{kind:'server_receipt',changes:['转入完成'],action_result:{transfer:{source_name:'水壶',target_name:'杯',transferred:1,material:'water'}},player:{id:'me',x:2,y:3},entity:{name:'杯',x:2,y:3},same_position_public_objects:[{id:'cup',name:'杯'}]}};
  assert.ok(ui.receiptOutcomeLines(receipt).some(line=>line.includes('水壶 → 杯：1u 水')));
  assert.ok(ui.receiptOutcomeLines(receipt).some(line=>line.includes('同格公开物件：杯')));
  assert.deepEqual(ui.receiptOutcomeLines({...receipt,status:'unknown'}),[]);
  assert.ok(ui.executionReadbackLines({readback_actual:false}).some(line=>line.includes('尚未读回')));
}
console.log('Grove DTO UI: typed facts, fixed intent, bounded motion, labels and truthful Agent evidence sources passed');
// Exercise the page's actual control handoff. The quick acknowledgement may
// leave native cleanup pending, while counts and an offline receipt read block.
for(const mode of ['active','starting','unknown','fallback','starting-offline']){
  const original={task_id:'task',status:mode==='starting'||mode==='starting-offline'?'starting':'running',delegation_active:mode!=='starting'&&mode!=='starting-offline'};
  const events=[],context={host:{execution:original,unknown_operations:0},session:1,playing:true,
    hostCommandEpoch:0,hostReadEpoch:0,hostCommandKind:null,hostCommandFlight:null,hostCommandBusy:false,manualControlRecoveryPending:false,
    $:()=>({textContent:''}),announce:text=>events.push(text),renderHost:()=>{},scheduleHost:()=>{},
    refreshWorld:async()=>events.push('read'),client:{
      connector:async(path,body)=>{events.push('reclaim');assert.equal(body.background,true);if(['fallback','starting-offline'].includes(mode))throw new Error('offline');return {unknown_operations:mode==='unknown'?1:0,reclaim_in_progress:true,execution:{...original,delegation_active:false,status:mode==='unknown'?'unknown':'paused'}};},
      mutate:async function(name){events.push(name);}}
  };
  vm.createContext(context);vm.runInContext(extract('async function takeManualControl(','async function perform('),context);
  assert.equal(await context.takeManualControl(()=>true),['active','starting'].includes(mode));
  assert.equal(context.hostCommandBusy,false);
  assert.equal(events.filter(event=>event==='grove.control_revoke').length,['fallback','starting-offline'].includes(mode)?1:0);
  if(['active','starting'].includes(mode))assert.equal(context.host.reclaim_in_progress,true);
  if(['fallback','starting-offline'].includes(mode)){
    assert.equal(context.host.unknown_operations,0);assert.equal(context.host.execution.status,original.status);
    assert.equal(context.host.execution.delegation_active,false);assert.equal(context.manualControlRecoveryPending,true);
    assert.ok(events.some(event=>event.includes('连接恢复后先核对原回执')));
    assert.equal(await context.takeManualControl(()=>true),false,'a second click cannot bypass the unread receipts');
    context.host={execution:{...original,status:'paused',delegation_active:false},unknown_operations:0};
    context.client.connector=async()=>({execution:{...original,status:'paused',delegation_active:false},unknown_operations:0});
    assert.equal(await context.takeManualControl(()=>true),true);assert.equal(context.manualControlRecoveryPending,false);
  }
}

// A native response ending is not evidence that the delegated goal succeeded.
// Exercise the page's actual renderer with persisted request/task outcomes.
for(const [execution,label,detail,resume] of [
  [{status:'completed',goal:'运来补给'},'目标结果未确认','原执行记录已结束',false],
  [{status:'completed',task_id:'original-task',goal:'运来补给',goal_result:{status:'completed'}},'目标结果未确认','旧完成记录没有验证来源',false],
  [{status:'completed',task_id:'original-task',goal:'运来补给',goal_result:{status:'completed',completion_source:'agent_claim'}},'Agent报告完成','程序确认的具体世界结果另列',false],
  [{status:'completed',task_id:'original-task',goal:'运来补给',goal_result:{status:'completed'},receipts:[{status:'unknown',operation_id:'original-write'}]},'结果待核对','先核对原回执',false],
  [{status:'paused',task_id:'original-task',goal:'运来补给',resume_allowed:true},'目标已暂停','继续原目标与剩余额度',true]
]){
  const elements=new Map(),element=id=>{
    if(!elements.has(id))elements.set(id,{textContent:'',value:'0',dataset:{},hidden:false,disabled:false,replaceChildren(){},append(){},setAttribute(){},removeAttribute(){}});
    return elements.get(id);
  };
  const context={host:{status:'ready',execution,admission:{goal:{allowed:true},resume:{allowed:!!execution.resume_allowed},extend:{allowed:false,reason:'额度不可追加'}}},playing:true,current:()=>true,goalDirty:true,hostCommandBusy:false,executionReclaimBusy:false,
    actionBusy:false,manualControlRecoveryPending:false,document:{activeElement:null},client:{pending:()=>null},$:element,
    receiptOutcomeLines:ui.receiptOutcomeLines,executionReadbackLines:ui.executionReadbackLines,executionTaskSummaryLines:ui.executionTaskSummaryLines,
    node:()=>({append(){}})};
  vm.createContext(context);
  vm.runInContext(extract('function agentAdmission(','const point='),context);
  vm.runInContext(extract('function currentAssistantReply(','function button('),context);
  vm.runInContext(extract('function renderExecution(){','async function reclaimExecution(){'),context);
  context.renderExecution();
  assert.equal(element('execution-status').textContent,label);
  assert.ok(element('execution-detail').textContent.includes(detail));
  assert.equal(element('execution-resume').hidden,!resume);
}

// The actual page consumes the server's refusal even when older host/task
// fields look ready; a fresh server projection is required to re-enable it.
{
  const reasons=Object.fromEntries(['conversation','creation','goal','resume','extend'].map(kind=>
    [kind,{allowed:false,reason:'原请求尚未确认停止'}]));
  const context={host:{status:'paused',unknown_operations:0,reclaim_in_progress:false,
    execution:{status:'paused',request_status:'unsettled'},admission:reasons},playing:true,current:()=>true,
    hostCommandBusy:false,executionReclaimBusy:false,manualControlRecoveryPending:false,actionBusy:false,
    client:{pending:()=>null}};
  vm.createContext(context);vm.runInContext(extract('function agentAdmission(','function continuationAdmission('),context);
  for(const kind of Object.keys(reasons))assert.equal(context.agentAdmission(kind).allowed,false);
  context.host.admission.conversation={allowed:true,reason:''};
  assert.equal(context.agentAdmission('conversation').allowed,true);
  context.host.admission=null;
  assert.equal(context.agentAdmission('conversation').allowed,false);
}


// A domain's 403/404 refusal preserves world credentials and the original
// pending operation. Only the current credential's 401 asks for reconnect.
for(const [status,staleAuth] of [[403,false],[404,false],[401,true],[401,false]]){
  const pending={body:{operation_id:'original'},name:'world.opaque'},elements=new Map(),context={held:'w',walkPath:[{x:1,y:1}],client:{token:'still-valid',pending:()=>pending},announce(){},renderPending(){},$:id=>{if(!elements.has(id))elements.set(id,{hidden:true,textContent:''});return elements.get(id);}};
  vm.createContext(context);vm.runInContext(extract('function fail(','function run('),context);
  context.fail({status,staleAuth,message:'服务器拒绝本领域读取'});
  assert.equal(context.client.token,status===401&&!staleAuth?null:'still-valid');
  assert.equal(context.client.pending(),pending);
}
