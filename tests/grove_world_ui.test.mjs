import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../agent_world/grove_web/grove-world-ui.js',import.meta.url),'utf8');
const ui=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

{
  const menu={cost_u:{wood:3,cloth:2},action:{choices:{sources:[
    {id:'wood',version:4,lots:[{id:'wood-lot',material:'wood',quantity:8,slot:'body'}]},
    {id:'cloth',version:5,lots:[{id:'cloth-lot',material:'cloth',quantity:2,slot:'body'}]}]}}};
  const rows=[{entity_id:'wood',lot_id:'wood-lot',material_id:'wood',quantity_u:'3'},
    {entity_id:'cloth',lot_id:'cloth-lot',material_id:'cloth',quantity_u:'2'}];
  assert.equal(ui.fabricationPlan(menu,rows).errors.length,0);
  const changed=structuredClone(menu);changed.action.choices.sources.reverse();changed.action.choices.sources[1].version=6;
  assert.equal(ui.fabricationPlan(changed,rows).sources[0].entity_id,'wood');
  assert.equal(ui.fabricationPlan(changed,rows).sources[0].expected_entity_version,6);
  changed.action.choices.sources[1].lots=[];
  assert.ok(ui.fabricationPlan(changed,rows).errors.length>0);
  assert.ok(ui.fabricationPlan(menu,[...rows,rows[0]]).errors.some(value=>value.includes('一行')));
  const descriptor={action:'bind_port',choices:{others:[{id:'water',eligible_ports:['wet']},{id:'gear',eligible_ports:['drive']}]}};
  assert.deepEqual(ui.ruleChoices(descriptor,'others','wet').map(value=>value.id),['water']);
}

{
  const entity={simulation:{contact:{candidate_count:0,process:{status:'waiting',steps:1},last_actual_reactions:[{
    extent:1,inputs:{'program_original#x':1},outputs:{'program_original#y':2},
    material_names:{'program_original#x':'旧配料','program_original#y':'新产物'}}]}}};
  const lines=ui.contactObservationLines(entity);
  assert.ok(lines.some(line=>line.includes('旧配料 1u')&&line.includes('新产物 2u')));
  assert.ok(lines.some(line=>line.includes('历史生成量不等于现在库存')));
  assert.deepEqual(ui.contactObservationLines({}),[]);
  entity.simulation.contact.reactions=[{condition:{known:false,satisfied:null,error:'除零'}}];
  assert.ok(ui.contactObservationLines(entity).some(line=>line.includes('无法判断（除零）')));
}

// The same renderer describes quantities and relationships regardless of names.
const first={name:'器皿',matter:[{id:'body',material:'stone',quantity:40,slot:'body'},
  {id:'water',material:'water',quantity:4,slot:'contained'}],state:{wetness:0},
  relations:{parent:{id:'parent',relation:'attached'}},
  simulation:{local:128,total:144,supported:16,limit:48,overload:0,integrity:90,max_integrity:100,buoyancy:0,can_float:false}};
const renamed={...first,name:'另一位主持的称呼',composition:{construction:'another-name'}};
assert.equal(ui.entitySummary(first),ui.entitySummary(renamed));
assert.deepEqual(ui.simulationStats(first),ui.simulationStats(renamed));
assert.ok(ui.entitySummary(first).includes('容纳 水 4 单位'));
assert.ok(ui.entitySummary(first).includes('绑定在物件上'));
assert.ok(ui.simulationStats(first).some(([label,value])=>label==='承载上限'&&value===48));

// Values are displayed from the server; appearance text never loads an asset.
const partialReaction=ui.reactionObservationLines({reaction_observations:[{rule_id:'expand',
  blockers:[],inputs:[],capacity_limited:true,isolated_extent:2}]},{});
assert.ok(partialReaction.some(line=>line.includes('可进行 2 次')));
assert.ok(!partialReaction.some(line=>line.includes('容量不满足')));
assert.deepEqual(ui.safePresentation({presentation:{shape:'<script>',color:'url(secret)',glyph:'<img src=x>'}}),
  {shape:'part',color:'#aaac8c',glyph:'◆',size:1});
assert.deepEqual(ui.matterLots({matter:{lots:first.matter}}),[]);

// A source's actual lots bound quantity; switching sources cannot retain a lot.
const descriptor={action:'transfer',label:'转移物质',input:{kind:'entity_quantity',argument:'item_id',
  candidates:[{id:'source-a',lots:[{id:'a-water',item_id:'source-a',material:'water',quantity:3,slot:'contained'}]},
    {id:'source-b',lots:[{id:'b-water',item_id:'source-b',material:'water',quantity:7,slot:'contained'}]}],
  quantity:{min:1,max:5,default:4}}};
assert.equal(ui.descriptorLots(descriptor,'source-a')[0].id,'a-water');
assert.equal(ui.descriptorLots(descriptor,'source-b')[0].id,'b-water');
assert.equal(ui.quantityBounds(descriptor,ui.descriptorLots(descriptor,'source-a')[0]).max,3);
assert.equal(ui.quantityBounds(descriptor,ui.descriptorLots(descriptor,'source-a')[0]).value,3);
assert.equal(ui.quantityBounds(descriptor,ui.descriptorLots(descriptor,'source-b')[0]).max,5);
assert.equal(ui.actionDescriptors({action_options:[descriptor,null,'transfer']}).length,1);

// A descendant can change a container's aggregate without changing its own
// record version. Refresh from visible aggregates, without exposing child IDs.
const preview={action:'store',target_id:'pouch',item_id:'closed-box',target_version:3,item_version:4,
  player_version:2,available:true,physics:{known:true,subject:{id:'pouch',name:'小袋'},
    before:{local:16,total:16,supported:0,limit:32,overload:0,buoyancy:0,can_float:false},
    after:{local:16,total:176,supported:160,limit:32,overload:128,buoyancy:0,can_float:false},
    support_margin:-128,overloaded:true,can_float:false,
    affected:[{id:'closed-box',version:4,before:{local:40,total:160,supported:120,limit:200,
      overload:0,integrity:100,max_integrity:100,buoyancy:300,can_float:true}}]}};
const combination={action_options:[{action:'store',input:{candidates:[{id:'closed-box',preview}]}}]};
const visible=[{id:'pouch',version:3,simulation:preview.physics.before},
  {id:'closed-box',version:4,simulation:{...preview.physics.affected[0].before}}];
assert.equal(ui.previewVersionsChanged(combination,visible,{version:2}),false);
assert.equal(ui.previewVersionsChanged(combination,[visible[0],{...visible[1],
  simulation:{...visible[1].simulation,total:168,supported:128}}],{version:2}),true);
assert.equal(ui.previewVersionsChanged(combination,visible,{version:3}),true);
assert.ok(ui.candidatePreviewLines({preview}).some(line=>line.includes('余量 -128')));
assert.ok(ui.candidatePreviewLines({preview}).some(line=>line.includes('危险实验仍由你选择')));
assert.ok(ui.candidatePreviewLines({preview}).some(line=>line.includes('不保证后续遇水')));

// Exercise the actual action preparation code: a formerly public object can
// become private between selection and submission without revoking the user.
const pageSource=readFileSync(new URL('../agent_world/grove_web/grove.js',import.meta.url),'utf8');
const extract=(start,end)=>pageSource.slice(pageSource.indexOf(start),pageSource.indexOf(end));
// Keep selected IDs while the actual editors reconcile a fresh server projection.
function formNode(tag,text,className){
  const listeners=new Map();
  return {tagName:tag.toUpperCase(),textContent:text||'',className:className||'',dataset:{},children:[],disabled:false,_value:null,
    get options(){return this.children.filter(child=>child.tagName==='OPTION');},
    get value(){return this._value??(this.tagName==='SELECT'?this.options[0]?.value||'':'');},
    set value(value){const raw=String(value);this._value=this.tagName==='SELECT'&&!this.options.some(option=>option.value===raw)?'':raw;},
    get selectedOptions(){return this.options.filter(option=>option.value===this.value);},
    append(...children){this.children.push(...children);},
    replaceChildren(...children){this.children=[];this._value=null;this.append(...children);},
    setAttribute(name,value){this[name]=String(value);},removeAttribute(name){delete this[name];},
    addEventListener(type,handler){listeners.set(type,[...(listeners.get(type)||[]),handler]);},
    dispatchEvent(event){for(const handler of listeners.get(event.type)||[])handler(event);},
    querySelectorAll(selector){const tags=selector.toUpperCase().split(',');return this.children.flatMap(child=>[...(tags.includes(child.tagName)?[child]:[]),...child.querySelectorAll(selector)]);}};
}
{
  const target={id:'target',name:'器皿'},actions=formNode('section'),writes=[];
  const initial={...descriptor,available:true},context={inspection:{entity:target,available_actions:['transfer'],action_options:[initial]},
    actionBusy:false,client:{pending:()=>null},node:formNode,$:()=>actions,actionKey:option=>option.action,
    actionDescriptors:ui.actionDescriptors,actionIds:ui.actionIds,descriptorLabel:ui.descriptorLabel,
    descriptorCandidates:ui.descriptorCandidates,descriptorLots:ui.descriptorLots,quantityBounds:ui.quantityBounds,
    candidateLabel:ui.candidateLabel,candidatePreviewLines:ui.candidatePreviewLines,readableCandidate:value=>value,
    materialLabel:ui.materialLabel,slotLabel:ui.slotLabel,operationIntentLabel:ui.operationIntentLabel,
    restoreActionDraft:()=>{},run:fn=>fn(),interact:(...args)=>writes.push(args)};
  vm.createContext(context);vm.runInContext(extract('function button(','function announce(')+extract('function renderDescriptorEditor(','async function interact('),context);
  context.renderDescriptorEditor(initial,target,false);
  const form=actions.children[0],[items,lots]=form.querySelectorAll('select'),amount=form.querySelectorAll('input')[0],submit=form.querySelectorAll('button').find(button=>button.type==='submit');
  items.value='source-a';items.dispatchEvent({type:'change'});amount.value='2';amount.dispatchEvent({type:'input'});
  const refresh=candidates=>{context.inspection={entity:target,available_actions:['transfer'],action_options:[{...initial,input:{...initial.input,candidates}}]};form.dispatchEvent({type:'refresh-candidate-preview'});};
  refresh([{id:'source-a',lots:[{id:'a-wood',material:'wood',quantity:6,slot:'contained'}]},
    {id:'source-c',lots:[{id:'c-water',material:'water',quantity:5,slot:'contained'}]}]);
  assert.equal(items.value,'source-a');assert.equal(lots.value,'a-water');assert.equal(amount.value,'2');
  assert.equal(lots.options.find(option=>option.value==='a-water').disabled,true);
  assert.ok(lots.options.some(option=>option.value==='a-wood'));
  assert.ok(items.options.some(option=>option.value==='source-c'));
  assert.equal(submit.disabled,true);assert.ok(form.querySelectorAll('p').some(note=>note.textContent.includes('请重新选择材料')));
  form.dispatchEvent({type:'submit',preventDefault(){}});assert.equal(writes.length,0);
  lots.value='a-wood';lots.dispatchEvent({type:'change'});amount.value='2';amount.dispatchEvent({type:'input'});
  form.dispatchEvent({type:'submit',preventDefault(){}});assert.equal(writes[0][4].lot_id,'a-wood');
  refresh([]);assert.equal(items.value,'source-a');assert.equal(items.options.find(option=>option.value==='source-a').disabled,true);assert.equal(items.disabled,true);
  refresh([{id:'source-c',lots:[{id:'c-water',material:'water',quantity:5,slot:'contained'}]}]);
  assert.equal(items.disabled,false);assert.equal(items.value,'source-a');assert.equal(amount.value,'2');assert.equal(submit.disabled,true);
}
{
  const target={id:'rule-target',name:'做法'},actions=formNode('section'),writes=[];
  const initial={function_id:'grove.rule_action',action:'process',available:true,choices:{items:[{id:'old-item',name:'原料'}]},
    params_schema:{properties:{amount:{type:'integer',minimum:1,default:1}},required:['amount']}};
  const context={inspection:{entity:target,action_options:[initial]},actionBusy:false,client:{pending:()=>null},node:formNode,$:()=>actions,
    actionKey:option=>option.action,descriptorLabel:ui.descriptorLabel,actionDescriptors:ui.actionDescriptors,
    conditionObservationLines:()=>[],ruleChoices:ui.ruleChoices,candidateLabel:ui.candidateLabel,
    readableCandidate:value=>value,factLabel:ui.factLabel,operationIntentLabel:ui.operationIntentLabel,
    restoreActionDraft:()=>{},run:fn=>fn(),ruleAction:(...args)=>writes.push(args)};
  vm.createContext(context);vm.runInContext(extract('function button(','function announce(')+extract('function renderRuleAction(','async function ruleAction('),context);
  context.renderRuleAction(initial,target,false);
  const form=actions.children[0],items=form.querySelectorAll('select')[0],amount=form.querySelectorAll('input')[0],submit=form.querySelectorAll('button').find(button=>button.type==='submit');
  items.value='old-item';amount.value='2';form.dispatchEvent({type:'change'});
  context.inspection={entity:target,action_options:[{...initial,choices:{items:[{id:'new-item',name:'新原料'}]}}]};
  form.dispatchEvent({type:'refresh-candidate-preview'});
  assert.equal(items.value,'old-item');assert.equal(amount.value,'2');assert.equal(items.options.find(option=>option.value==='old-item').disabled,true);
  assert.ok(items.options.some(option=>option.value==='new-item'));assert.equal(submit.disabled,true);
  items.value='new-item';form.dispatchEvent({type:'change'});form.dispatchEvent({type:'submit',preventDefault(){}});
  assert.equal(writes[0][2].item_id,'new-item');assert.equal(writes[0][2].params.amount,2);
}

// Rule fuel/charge quantities live at the top level. The preview must describe
// the actual edited values, including after the server refreshes the choices.
for(const action of ['fuel','charge']){
  const target={id:'source',name:'十二齿有限动力'},actions=formNode('section'),writes=[];
  const choices={items:[{id:'wood',name:'木料',lots:[{id:'wood-lot',material:'wood',quantity:6,slot:'body'}]},
    {id:'cloth',name:'布料',lots:[{id:'cloth-lot',material:'cloth',quantity:6,slot:'body'}]}]};
  if(action==='charge')choices.materials=[{id:'resin',name:'木合料',feedstock:'wood'},{id:'binder',name:'布合料',feedstock:'cloth'}];
  const initial={function_id:'grove.rule_action',action,label:action==='fuel'?'消耗木料储能':'制备已声明材料',available:true,
    arguments:{amount:1},choices,input_schema:{properties:{item_id:{type:'string'},amount:{type:'integer',minimum:1,maximum:10},
      ...(action==='fuel'?{lot_id:{type:'string',minLength:1}}:{})},required:action==='fuel'?['amount','lot_id']:['amount']}};
  const context={inspection:{entity:target,action_options:[initial]},actionBusy:false,client:{pending:()=>null},node:formNode,$:()=>actions,
    actionKey:option=>option.action,descriptorLabel:ui.descriptorLabel,actionDescriptors:ui.actionDescriptors,
    conditionObservationLines:()=>[],ruleChoices:ui.ruleChoices,candidateLabel:ui.candidateLabel,
    descriptorLots:ui.descriptorLots,materialLabel:ui.materialLabel,slotLabel:ui.slotLabel,
    readableCandidate:value=>value,factLabel:ui.factLabel,operationIntentLabel:ui.operationIntentLabel,
    restoreActionDraft:()=>{},run:fn=>fn(),ruleAction:(...args)=>writes.push(args)};
  vm.createContext(context);vm.runInContext(extract('function button(','function announce(')+extract('function renderRuleAction(','async function ruleAction('),context);
  context.renderRuleAction(initial,target,false);
  const form=actions.children[0],[items,materials]=form.querySelectorAll('select'),amount=form.querySelectorAll('input')[0];
  const intent=form.querySelectorAll('p').find(element=>element.className==='interaction-preview');
  items.value='wood';form.dispatchEvent({type:'change',target:items});if(materials)materials.value=action==='fuel'?'wood-lot':'resin';amount.value='2';form.dispatchEvent({type:'input'});
  assert.ok(intent.textContent.includes(action==='fuel'?'木料 添入 2 份木':'木料 的 2 份木'));assert.ok(!intent.textContent.includes('1 份'));
  context.inspection={entity:target,action_options:[{...initial,arguments:{amount:1},choices:{...choices,
    items:[{...choices.items[0],name:'留下的木料'},choices.items[1]]}}]};
  form.dispatchEvent({type:'refresh-candidate-preview'});
  assert.equal(amount.value,'2');assert.ok(intent.textContent.includes(action==='fuel'?'留下的木料 添入 2 份木':'留下的木料 的 2 份木'));
  items.value='cloth';form.dispatchEvent({type:'change',target:items});if(materials)materials.value=action==='fuel'?'cloth-lot':'binder';amount.value='3';form.dispatchEvent({type:'change'});
  assert.ok(intent.textContent.includes(action==='fuel'?'布料 添入 3 份布':'布料 的 3 份布'));
  if(action==='charge')assert.ok(intent.textContent.includes('3 份布合料'));
  form.dispatchEvent({type:'submit',preventDefault(){}});
  assert.equal(writes[0][2].item_id,'cloth');assert.equal(writes[0][2].amount,3);
  if(action==='charge')assert.equal(writes[0][2].material,'binder');
  if(action==='fuel')assert.equal(writes[0][2].lot_id,'cloth-lot');
}

for(const status of [403,404,401])for(const kind of ['combination-target','ordinary-target','item']){
  const target={id:'target',version:1},feedback={textContent:''},errorBox={hidden:true};
  const controls=[{disabled:false}],form={isConnected:true,reset(){this.resets=(this.resets||0)+1;},
    querySelectorAll(selector){return selector==='input,select,button'?controls:[];},
    focus(){context.document.activeElement=this;}};
  let writes=0;
  const context={session:1,playing:true,identity:{},actionBusy:false,readEpoch:0,inspectEpoch:0,
    selectedId:target.id,selectedFromBag:false,inspection:{entity:target},inspectionError:null,
    inspectionErrorStatus:null,inspectionFlight:null,combinationCheckingForm:null,
    held:null,walkPath:[],pathTarget:null,lastAction:'',executionReclaimBusy:false,document:{activeElement:null},
    client:{token:'still-valid',pending:()=>null,query:async(name,args)=>{
      if(kind==='item'&&args.entity_id===target.id)return {entity:target,available_actions:['pour'],
        action_options:[{action:'pour',available:true,input:{kind:'entity',candidates:[{id:'item'}]}}]};
      throw Object.assign(new Error('Object access changed'),{status});
    },mutate:async()=>{writes++;}},
    $:id=>id==='movement-feedback'?feedback:id==='selection-actions'?{querySelectorAll:()=>[form]}:
      id==='error'?errorBox:{hidden:true,textContent:''},
    current:()=>context.client.token,point:()=>({version:1}),entityById:()=>target,takeManualControl:async()=>true,
    announce:()=>{},stopWalking:()=>{},renderSelection:()=>{},renderPending:()=>{},
    actionNames:{pour:'倒水',store:'存入'},actionDescriptors:ui.actionDescriptors,
    actionIds:ui.actionIds,descriptorCandidates:ui.descriptorCandidates,descriptorLabel:ui.descriptorLabel};
  vm.createContext(context);
  vm.runInContext(extract('function fail(','const current=')+
    extract('async function perform(','async function move(')+
    extract('async function interact(','function authorLabel('),context);
  await context.run(()=>context.interact(kind==='combination-target'?'store':'pour',target,'item',undefined,{},
    kind==='combination-target'?{form,fingerprint:'a'.repeat(64)}:undefined));
  assert.equal(writes,0,`${kind}/${status}: no command may be sent`);
  assert.equal(context.actionBusy,false);
  assert.equal(context.client.token,status===401?null:'still-valid',`${kind}/${status}: credential boundary`);
  if(status!==401){
    assert.ok(feedback.textContent.includes('尚未提交'));
    assert.equal(context.inspection.reading,true);
    assert.equal(context.inspectionErrorStatus,status);
    assert.ok(form.resets>0);
  }
}

// A preflight refresh may change the receiving slot; preserve the chosen slot
// and refuse the changed intent rather than silently pouring somewhere else.
for(const targetSlot of ['surface','contained']){
  const target={id:'target',version:7},item={id:'item',version:4},writes=[];
  const context={session:1,playing:true,selectedId:target.id,inspection:{entity:target},
    stopWalking:()=>{},renderSelection:()=>{},point:()=>({version:3}),actionNames:{pour:'倒水'},
    actionIds:ui.actionIds,actionDescriptors:ui.actionDescriptors,descriptorLabel:ui.descriptorLabel,
    descriptorCandidates:ui.descriptorCandidates,quantityBounds:ui.quantityBounds,
    client:{query:async(name,args)=>args.entity_id===item.id?{entity:item}:{entity:target,available_actions:['pour'],
      action_options:[{action:'pour',available:true,arguments:{target_slot:targetSlot},input:{kind:'entity_quantity',candidates:[item],quantity:{min:1,max:3}}}]}},
    perform:async(name,prepare)=>writes.push(await prepare(()=>true))};
  vm.createContext(context);vm.runInContext(extract('async function interact(','function authorLabel('),context);
  if(targetSlot==='contained'){await assert.rejects(context.interact('pour',target,item.id,2,{target_slot:'surface'}),/目标位置已改变/);assert.equal(writes.length,0);}
  else{await context.interact('pour',target,item.id,2,{target_slot:'surface'});assert.equal(writes[0].target_slot,'surface');assert.equal(writes[0].expected_target_version,7);assert.equal(writes[0].expected_item_version,4);}
}

// The same source and target remain visible; storing an object does not count
// its body as a transferred batch or claim that a preview has already happened.
const bowl={name:'石碗'},flask={name:'水壶'},box={name:'木箱'};
assert.equal(ui.operationIntentLabel({action:'store'},bowl,{item:box}),'把 木箱 整件放进 石碗');
assert.equal(ui.operationIntentLabel({action:'transfer'},bowl,{item:box,lot:{material:'wood'},amount:3}),'从 木箱 转入 3 份木 → 石碗');
assert.equal(ui.operationIntentLabel({action:'pour'},box,{item:flask,amount:2}),'从 水壶 倒出 2 份水 → 木箱');
assert.equal(ui.operationIntentLabel({action:'fill'},bowl,{item:flask,amount:2}),'从 石碗 装 2 份水 → 水壶');
assert.equal(ui.operationIntentLabel({action:'ignite'},box,{item:{name:'打火石'}}),'用 打火石 → 木箱');
const sameNamed=[{id:'one',name:'器皿',x:1,y:2},{id:'two',name:'器皿',x:3,y:4}];
assert.ok(ui.candidateLabel(sameNamed[0],sameNamed).includes('1，2'));
assert.ok(!ui.candidateLabel(sameNamed[0],sameNamed).includes('one'));

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
    node:()=>({append(){}})};
  vm.createContext(context);
  vm.runInContext(extract('function agentAdmission(','const point='),context);
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

// Automatic input snapshots, past control and saved mechanical output are
// distinct from actual execution, including an old running speed in a bag.
{
  const creative={automatic_rule_count:1,explicit_action_count:0,
    deployment:{publicly_placed:false,ordinary_rules_enabled:true,
      public_event_input_available:false,mechanical_solver_eligible:false,
      mechanical_blockers:['no_public_location']},
    automatic_rules:[{rule_id:'sense',phase:'reaction',input_kind:'logical_step',
      condition:{known:true,satisfied:true},effect_ops:['drive','emit'],snapshot_only:true,
      drive_inputs:[{observation:{known:true,satisfied:false},snapshot_only:true}]}],
    mechanical_observation:{role:'source',stored_state:{speed:5,position:7,status:'running'},
      last_solved:{step:12,historical:true},solver_eligible:false,stored_output_is_history:true,
      drive_request:{enabled:true,requested_step:13,applies_from_step:14,
        source:{kind:'automatic_rule',rule_id:'sense',phase:'reaction'}},
      pending_next_step:false,observed_tick:20},process:{steps:2}};
  const lines=ui.ruleRuntimeObservationLines(creative,{});
  assert.ok(lines.some(line=>line.includes('随身携带也可评估')));
  assert.ok(lines.some(line=>line.includes('不具备机械求解资格')));
  assert.ok(lines.some(line=>line.includes('速度 5')&&line.includes('历史求解输出或初始值')));
  assert.ok(lines.some(line=>line.includes('启停表达式当前读数：关闭')&&line.includes('不证明已发出请求')));
  assert.ok(lines.some(line=>line.includes('自动声明 sense')&&line.includes('来源')));
  assert.ok(lines.some(line=>line.includes('已到最早生效逻辑步')&&line.includes('不证明已被求解')));
  assert.ok(lines.some(line=>line.includes('最近输出变化的求解步：12')&&line.includes('不是每步参与')));
  assert.ok(lines.some(line=>line.includes('不是机械推进次数或目标完成进度')));
  assert.ok(lines.some(line=>line.includes('不证明自动声明已经执行')));
  assert.ok(lines.some(line=>line.includes('声明效果：有限动力启停、声明信号')));
  assert.deepEqual(ui.ruleRuntimeObservationLines({...creative,hidden:true},{}),[]);
  creative.automatic_rules[0].input_kind='public_event';
  creative.automatic_rules[0].condition={known:false,reason:'event_required'};
  creative.automatic_rules[0].drive_inputs[0].observation={known:false};
  creative.mechanical_observation.last_solved=null;
  creative.mechanical_observation.pending_next_step=true;
  const unknown=ui.ruleRuntimeObservationLines(creative,{});
  assert.ok(unknown.some(line=>line.includes('须有实际事件输入')));
  assert.ok(unknown.some(line=>line.includes('启停表达式当前未知')&&line.includes('不能据此推定关闭')));
  assert.ok(unknown.some(line=>line.includes('不把未知当作否')));
  assert.ok(unknown.some(line=>line.includes('不能据此判断从未求解')));
  assert.ok(unknown.some(line=>line.includes('尚未到最早生效逻辑步')));
  const physical=ui.conditionObservationLines({known:true,satisfied:false,
    checks:[{left:{object:{port:'cup',field:'physical.wetness'}},operator:'gte',
      known:true,actual_left:0,actual_right:1,satisfied:false}]},{});
  assert.ok(physical.some(line=>line.includes('槽位 cup · 湿度')));
}

// A saved waiting declaration record can coexist with actual mechanical motion.
// Render it contextually without changing contact or mechanical status labels.
{
  const creative={process:{status:'waiting',steps:0,last_tick:2,observed_tick:9,
      status_meaning:'保存的过程标记，不证明整个装置当前运行或停止。',
      last_tick_meaning:'不是最近自动声明评估或机械求解步。'},
    mechanical_observation:{role:'source',stored_state:{speed:1,angle:7,energy:3,status:'running'},
      solver_eligible:true,last_solved:{step:9,historical:true},
      drive_request:{enabled:true,requested_step:5,applies_from_step:6,
        source:{kind:'automatic_rule',rule_id:'sense',phase:'after'}},
      pending_next_step:false,observed_tick:9}};
  const original=structuredClone(creative),selection=formNode('section');
  const context={inspection:{creative},node:formNode,$:()=>selection,factDisclosures:new Map(),
    factLabel:ui.factLabel,factValue:ui.factValue,creativeState:ui.creativeState,
    ruleRuntimeObservationLines:ui.ruleRuntimeObservationLines,reactionObservationLines:ui.reactionObservationLines};
  vm.createContext(context);vm.runInContext(extract('function appendFacts(','async function readRuleDefinition('),context);
  context.renderCreative({id:'moving-source',creative});
  const labels=selection.querySelectorAll('small,summary').map(node=>node.textContent);
  assert.ok(labels.includes('保存的过程标记'));
  assert.ok(labels.includes('过程记录更新时间'));
  assert.ok(labels.some(label=>label.startsWith('保存的过程记录')));
  assert.ok(!labels.includes('上次运行的世界时刻'));
  const values=selection.querySelectorAll('span').map(node=>node.textContent);
  assert.ok(values.includes('等待'));
  assert.ok(!values.includes('等待材料或条件'));
  const lines=selection.querySelectorAll('p').map(node=>node.textContent);
  assert.ok(lines.some(line=>line.includes('角度 7')&&line.includes('保存状态 运行中')));
  assert.ok(lines.some(line=>line.includes('自动声明 sense')&&line.includes('来源')));
  assert.ok(!labels.includes('等待原因'));
  assert.deepEqual(creative,original);
  creative.process.blocked_reason='unavailable_fact_or_port';
  selection.replaceChildren();context.renderCreative({id:'moving-source',creative});
  assert.ok(selection.querySelectorAll('small').some(node=>node.textContent==='等待原因'));
  assert.equal(ui.factValue('waiting'),'等待材料或条件');
}

console.log('Grove presentation: quantities, relationship/action boundaries and truthful goal/resume outcomes passed');

// A confirmed full turn must be visible between identical modulo endpoints.
{
  const entity=(angle,version,status='running',energy=2,role='source')=>({id:'actual',version,x:0,y:1,lifecycle:'intact',creative:{mechanical_observation:{
    role,angle_unit:'logical_turns',observed_tick:version,solver_eligible:true,travel:5,
    stored_state:{angle,status,energy,speed:status==='insufficient_energy'?0:1,position:5}}}});
  const motion=ui.createMechanicalMotion();
  motion.accept([entity(0,1)],{epoch:'same'},0);
  motion.accept([entity(1,2,'insufficient_energy',0)],{epoch:'same'},100);
  assert.equal(motion.read(entity(1,2,'insufficient_energy',0),260).angle,.5);
  assert.equal(motion.read(entity(1,2,'insufficient_energy',0),10000).angle,1);
  motion.accept([entity(2,3,'travel_limit',0,'actuator')],{epoch:'same'},10100);
  assert.equal(motion.read(entity(2,3,'travel_limit',0,'actuator'),10260).angle,1.5);
  assert.equal(motion.read(entity(2,3,'travel_limit',0,'actuator'),10260,false).angle,2);
  motion.accept([entity(3,4,'stopped',2)],{epoch:'same'},10300);
  assert.equal(motion.read(entity(3,4,'stopped',2),10310).angle,3);
  motion.reset();motion.accept([entity(6,7)],{epoch:'same'},10400);
  assert.equal(motion.read(entity(6,7),10401).angle,6);
  motion.accept([entity(26,8)],{epoch:'same'},10500);
  assert.equal(motion.read(entity(26,8),10950).angle,16);
  assert.equal(motion.read(entity(26,8),11500).angle,26);
  const privateEntity=entity(6,7);privateEntity.creative.mechanical_observation.solver_eligible=false;
  assert.equal(motion.read(privateEntity,10402),null);
  const hidden={content_summary:{visible:false,materials:[{material:'water',quantity:9}]},state:{water:99}};
  assert.equal(ui.visibleWaterQuantity(hidden),0);
  hidden.content_summary.visible=true;
  assert.equal(ui.visibleWaterQuantity(hidden),9);
}
