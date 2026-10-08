import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../agent_world/grove_web/grove-world-ui.js',import.meta.url),'utf8');
const ui=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

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
assert.ok(partialReaction.some(line=>line.includes('仍可支持 2 份反应')));
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
for(const status of [403,404,401])for(const kind of ['combination-target','ordinary-target','item']){
  const target={id:'target',version:1},feedback={textContent:''},errorBox={hidden:true};
  const controls=[{disabled:false}],form={isConnected:true,reset(){this.resets=(this.resets||0)+1;},
    querySelectorAll(selector){return selector==='input,select,button'?controls:[];},
    focus(){context.document.activeElement=this;}};
  let writes=0;
  const context={session:1,playing:true,identity:{},actionBusy:false,readEpoch:0,inspectEpoch:0,
    selectedId:target.id,selectedFromBag:false,inspection:{entity:target},inspectionError:null,
    inspectionErrorStatus:null,inspectionFlight:null,combinationCheckingForm:null,
    held:null,walkPath:[],pathTarget:null,lastAction:'',document:{activeElement:null},
    client:{token:'still-valid',pending:()=>null,query:async(name,args)=>{
      if(kind==='item'&&args.entity_id===target.id)return {entity:target,available_actions:['pour'],
        action_options:[{action:'pour',available:true,input:{kind:'entity',candidates:[{id:'item'}]}}]};
      throw Object.assign(new Error('Object access changed'),{status});
    },mutate:async()=>{writes++;}},
    $:id=>id==='movement-feedback'?feedback:id==='selection-actions'?{querySelectorAll:()=>[form]}:
      id==='error'?errorBox:{hidden:true,textContent:''},
    current:()=>context.client.token,point:()=>({version:1}),entityById:()=>target,
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
    if(!elements.has(id))elements.set(id,{textContent:'',dataset:{},hidden:false,disabled:false,replaceChildren(){},append(){}});
    return elements.get(id);
  };
  const context={host:{status:'ready',execution},goalDirty:true,hostCommandBusy:false,executionReclaimBusy:false,
    actionBusy:false,document:{activeElement:null},client:{pending:()=>null},$:element,
    node:()=>({append(){}})};
  vm.createContext(context);
  vm.runInContext(extract('function renderExecution(){','async function reclaimExecution(){'),context);
  context.renderExecution();
  assert.equal(element('execution-status').textContent,label);
  assert.ok(element('execution-detail').textContent.includes(detail));
  assert.equal(element('execution-resume').hidden,!resume);
}

console.log('Grove presentation: quantities, relationship/action boundaries and truthful goal/resume outcomes passed');
