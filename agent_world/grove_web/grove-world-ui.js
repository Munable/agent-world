// All presentation is data. This module never interprets host text as markup,
// loads a model asset, or turns a descriptor into an executable callback.
const materialLabels={wood:'木',stone:'石',cloth:'布',metal:'金属',water:'水',leather:'皮革'};
const slotLabels={body:'本体',contained:'容纳',surface:'表面',absorbed:'吸附'};
const parentLabels={ground:'在地面',held:'随身携带',contained:'在容器中',attached:'绑定在物件上'};
const finite=value=>Number.isFinite(value)?value:null;
const referencePattern=/(?:\bprogram_ref(?:#[a-z0-9_-]+)?\b|\b(?:program(?:_ref)?|object|entity|player|role|lot|operation)[_:.-][a-z0-9_-]+(?:#[a-z0-9_-]+)?\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b[0-9a-f]{24,}\b)/gi;
const readableText=(value,fallback='未提供名称')=>{
  if(typeof value!=='string'||!value.trim())return fallback;
  return value.trim().replace(referencePattern,'关联对象');
};
const readableSymbol=value=>{
  if(typeof value!=='string'||!value.trim())return '';
  const symbol=value.includes('#')?value.slice(value.lastIndexOf('#')+1):value;
  return readableText(symbol,'').replace(/_/g,' ');
};
const snapshotName=value=>value&&typeof value==='object'?value.label||value.name||value.material_label||value.material_name:null;
export function materialLabel(value,entity){
  const snapshot=value&&typeof value==='object'?value:null,material=snapshot?.material||snapshot?.material_id||snapshot?.symbol||snapshot?.id||value;
  if(snapshotName(snapshot))return readableText(snapshotName(snapshot),'未知材料');
  const program={...(entity?.state?.program||{}),...(entity?.creative||{}),...(!entity?.creative&&!entity?.state?.program?entity||{}:{})},aliases=program.material_aliases||{},id=aliases[material]||material;
  const localId=typeof material==='string'&&typeof program.definition_ref==='string'?program.definition_ref+'#'+material:null;
  const definition=program.material_definitions?.[id]||program.material_definitions?.[material]||program.material_definitions?.[localId]||program.materials?.[id]||program.materials?.[material]||program.materials?.[localId];
  if(snapshotName(definition))return readableText(snapshotName(definition),'未知材料');
  if(materialLabels[material]||materialLabels[id])return materialLabels[material]||materialLabels[id];
  const symbol=readableSymbol(typeof material==='string'?material:typeof id==='string'?id:'');
  return symbol&&symbol!=='关联对象'?symbol:'未知材料';
}
export const slotLabel=value=>slotLabels[value]||String(value||'未标明');

export function contactObservationLines(entity){
  const contact=entity?.simulation?.contact;if(!contact)return [];
  const process=contact.process||{},lines=['材料接触 · '+({running:'已发生反应',waiting:'等待材料或条件',error:'上次反应未成立'}[process.status]||'等待材料或条件')+' · 已成立 '+(process.steps||0)+' 步'];
  if(process.error)lines.push('上次失败：'+process.error);
  for(const reaction of contact.reactions||[]){
    const names=Object.fromEntries(Object.values(reaction.material_bindings||{}).map(binding=>[binding.material_id,binding.name]));
    const label=id=>names[id]||materialLabel(id,entity);
    lines.push('当前配比：'+Object.entries(reaction.inputs_per_extent||{}).map(([id,n])=>label(id)+' '+n+'u').join(' + ')+' → '+Object.entries(reaction.outputs_per_extent||{}).map(([id,n])=>label(id)+' '+n+'u').join(' + '));
    const condition=reaction.condition||{},conditionText=condition.known===false?'无法判断'+(condition.error?'（'+condition.error+'）':''):condition.satisfied?'满足':'未满足';
    lines.push('当前条件：'+conditionText+'；可用 '+Object.entries(reaction.available||{}).map(([id,n])=>label(id)+' '+n+'u').join('，'));
    const missing=Object.entries(reaction.missing_for_one_extent||{}).filter(([,n])=>n>0);if(missing.length)lines.push('一次反应还缺：'+missing.map(([id,n])=>label(id)+' '+n+'u').join('，'));
  }
  for(const actual of contact.last_actual_reactions||[]){
    const label=id=>actual.material_names?.[id]||materialLabel(id,entity);
    lines.push('上次真实消耗：'+Object.entries(actual.inputs||{}).map(([id,n])=>label(id)+' '+n+'u').join('，')+'；生成 '+Object.entries(actual.outputs||{}).map(([id,n])=>label(id)+' '+n+'u').join('，'));
  }
  lines.push('只处理本容器自身配料；整件子物件不混入。产物下一逻辑步可继续反应，历史生成量不等于现在库存。');return lines;
}

export function matterLots(entity){
  const lots=Array.isArray(entity?.matter)?entity.matter:[];
  return lots.filter(lot=>lot&&typeof lot==='object'&&Number.isInteger(lot.quantity)&&lot.quantity>=0);
}
export function bodyQuantities(entity){
  if(entity?.material_quantity?.by_material)return Object.entries(entity.material_quantity.by_material).filter(([,slots])=>Number.isInteger(slots.body)&&slots.body>=0).map(([material,slots])=>({material,quantity:slots.body}));
  if(Array.isArray(entity?.simulation?.body))return entity.simulation.body.filter(row=>row&&Number.isInteger(row.quantity)&&row.quantity>=0);
  return matterLots(entity).filter(lot=>lot.slot==='body').map(lot=>({material:lot.material,quantity:lot.quantity}));
}
export function relationLabel(entity,backpackId){
  const parent=entity?.relations?.parent;
  if(!parent||typeof parent!=='object')return '';
  const kind=parent.relation;
  if(kind==='contained'&&backpackId&&parent.id===backpackId)return '在自己的背包中';
  return parentLabels[kind]||'存在物理关联';
}
export function entitySummary(entity,backpackId){
  const state=entity?.state||{},lots=matterLots(entity);
  const quantities=lots.length?lots.map(lot=>slotLabel(lot.slot)+' '+materialLabel(lot.material,entity)+' '+lot.quantity+' 单位').join('，'):materialLabel(entity?.material,entity);
  const wetness=entity?.simulation?.wetness??state.wetness,burning=(entity?.simulation?.burning_lots||[]).length>0||lots.some(lot=>lot.burning);
  return [quantities,lifecycleLabel(entity),burning?'有残余材料正在燃烧':null,Number.isFinite(wetness)&&wetness>0?'湿度 '+wetness:null,relationLabel(entity,backpackId)].filter(Boolean).join(' · ');
}
export function lifecycleLabel(entity){
  if(entity?.lifecycle==='consumed'||entity?.state?.destroyed)return '材料已耗尽';
  if(entity?.lifecycle==='broken'||entity?.state?.broken)return '结构已破损，残材仍在';
  return '';
}
export function simulationStats(entity,backpackId){
  const sim=entity?.simulation||{},rows=[];
  for(const [key,label] of [['local','本件载荷'],['total','总载荷'],['supported','承受载荷'],['limit','承载上限'],['buoyancy','浮力']])if(finite(sim[key])!==null)rows.push([label,sim[key]]);
  if(finite(sim.integrity)!==null)rows.push(['完整度',sim.integrity+(finite(sim.max_integrity)!==null?' / '+sim.max_integrity:'')]);
  if(finite(sim.overload)!==null)rows.push(['过载量',sim.overload]);
  if(typeof sim.can_float==='boolean')rows.push(['下水资格',sim.can_float?'满足浮力条件':'未满足浮力条件']);
  if(entity?.relations)rows.push(['物理关系',entity.relations.parent?relationLabel(entity,backpackId):Number.isFinite(entity.x)&&Number.isFinite(entity.y)?'地面 '+entity.x+'，'+entity.y:'无父物件']);
  if(entity?.custody)rows.push(['保管边界',entity.custody.scope==='private'?'私人':entity.custody.scope==='public'?'公共':String(entity.custody.scope||entity.custody)]);
  if(Number.isFinite(sim.wetness))rows.push(['派生湿度',sim.wetness]);
  return rows;
}
export function entityGlyph(entity){
  const glyph=entity?.presentation?.glyph;
  // A small text glyph cannot introduce HTML, a URL, or arbitrary scale.
  if(typeof glyph==='string'&&glyph.length>0&&[...glyph].length<=3&&!/[\u0000-\u001f\u007f]/.test(glyph))return glyph;
  const capabilities=entity?.capabilities||[];
  if(capabilities.includes('container'))return '▣';
  if(capabilities.includes('water_source'))return '≈';
  if(capabilities.includes('ignition'))return '✧';
  return '◆';
}
export function safePresentation(entity){
  const presentation=entity?.presentation||{};
  const shapes=new Set(['box','bowl','cloth','beam','stone','raft','part']);
  const fallback=(entity?.capabilities||[]).includes('container')?'box':'part';
  return {shape:shapes.has(presentation.shape)?presentation.shape:fallback,
    color:typeof presentation.color==='string'&&/^#[0-9a-f]{6}$/i.test(presentation.color)?presentation.color:'#aaac8c',
    glyph:entityGlyph(entity),size:(entity?.composition?.size??presentation.size)===2?2:1};
}
const factLabels={definition_ref:'规则定义',type_id:'对象类型',fields:'当前属性',materials:'材料定义',material_definitions:'材料定义',mechanical:'机械状态',recorder:'记录状态',contract:'双方契约',connections:'当前连接',process:'过程状态与历史',last_reaction:'保存的上次反应',source_last_tick:'保存的过程逻辑步',observed_tick:'本次观察逻辑步',last_tick:'过程保存的逻辑步',steps:'累计反应步数',steps_meaning:'步数含义',historical:'历史记录',current_production:'是否本次生产',meaning:'含义',history:'已发生的记录',position:'位置',angle:'角度',speed:'速度',status:'状态',energy:'能量',available_torque:'可用扭矩',active:'正在运行',started_tick:'开始逻辑步',until_tick:'截止逻辑步',sequence:'事件序号',tick:'逻辑步',occurred_at:'发生时间',recorded_tick:'记录逻辑步',action:'行动',entity_id:'对象',actor:'行动者',read_tick:'读取逻辑步',reads:'实际读取',parties:'双方身份',terms_version:'条款版本',accepted:'已接受的身份',deadline_tick:'截止逻辑步',delivered:'已交付',quantity:'数量',material:'材料',beneficiary:'受益方',id:'标识',name:'名称',version:'版本',unit:'单位',temperature:'温度',pressure:'压力',progress:'进展',running:'运行中',remaining:'剩余量',capacity:'容量',amount:'数量',item_id:'投入物件',other_id:'另一个对象',connected_to:'连接至',direction:'方向',mode:'方式',output:'产物',inputs:'投入',outputs:'产出',rules:'规则',types:'类型',actions:'行动',conditions:'条件',effects:'效果',description:'说明',label:'名称',duration:'持续逻辑步',fuel:'燃料',work:'做功',torque:'扭矩',reaction:'反应',state:'当前状态',owner:'身份',scope:'范围'};
Object.assign(factLabels,{definition_ref:'使用的规则',type_id:'物件种类',material_aliases:'材料称呼',material_imports:'引用的材料',source_last_tick:'上次反应的世界时刻',observed_tick:'本次观察的世界时刻',last_tick:'上次运行的世界时刻',started_tick:'开始的世界时刻',until_tick:'截止的世界时刻',tick:'世界时刻',recorded_tick:'记录的世界时刻',read_tick:'读取的世界时刻',deadline_tick:'截止的世界时刻',duration:'持续时刻数',terms_version:'当前条款记录',version:'更新记录',id:'引用的对象',entity_id:'关联物件',item_id:'所用物件',other_id:'关联对象',lot_id:'所选材料',target_id:'操作对象',material_id:'材料',symbol:'材料称呼',load_per_u:'每份载荷',per_extent:'每次所需',missing_for_one_extent:'还缺多少',available:'当前可用',slot:'材料位置',scope:'可见范围',snapshot_only:'当前观察',parameter_defaults:'默认参数',known:'是否读到',satisfied:'是否满足',intact:'结构完整',passable:'是否可通过',opened_sequence:'最近开放记录',started_event_sequence:'开始时的事件记录','barrier.active':'通道是否阻挡','barrier.opened_sequence':'通道最近开放记录','mechanical.position':'机械位置','mechanical.speed':'机械速度','mechanical.angle':'机械角度','mechanical.energy':'机械能量','mechanical.status':'机械状态'});
export const factLabel=key=>key===null||key===undefined?'属性':factLabels[key]||readableSymbol(String(key))||'属性';
const valueLabels={...materialLabels,body:'本体',contained:'容纳的材料',surface:'表面',absorbed:'吸附',ground:'地面',held:'随身',attached:'绑定',private:'私人',public:'公共',intact:'完整',broken:'破损',consumed:'材料耗尽',idle:'闲置',running:'运行中',stopped:'已停止',active:'进行中',complete:'已完成',failed:'已失败',error:'已暂停',pending:'等待中',waiting:'等待材料或条件',awaiting_acceptance:'等待双方接受',unoffered:'尚未发起约定',no_source:'未接入动力',program_error:'规则已暂停',accepted:'已接受',released:'已领出',independent:'分别反应',exclusive:'择一反应',reaction:'材料反应',after:'反应之后',logical_matter:'材料份数',whole_entity:'整件物件',material_lot:'材料',role:'人物',entity:'物件',resource_node:'资源点',water_source:'水源',ignition:'火种',container:'容器',structure:'结构'};
export function factValue(value){
  if(value===null||value===undefined)return '未公开或未设定';
  if(typeof value==='boolean')return value?'是':'否';
  if(typeof value==='number')return String(value);
  if(typeof value==='string')return valueLabels[value]||readableText(value,'未提供名称');
  if(snapshotName(value))return readableText(snapshotName(value));
  return Array.isArray(value)?value.length+' 项':'详细状态';
}
export function creativeState(inspection,entity){
  const value=inspection?.creative||entity?.creative||entity?.state?.program;
  return value&&typeof value==='object'&&!Array.isArray(value)?value:null;
}
export function conditionObservationLines(observation,entity){
  if(!observation||typeof observation!=='object')return [];
  if(observation.known!==true)return [observation.reason==='event_required'?'条件未知：须有实际事件输入，当前快照不能判断。':'条件未知：当前未取得所需的可观察事实。'];
  const lines=[observation.satisfied?'声明条件当前成立。':'声明条件当前未成立。'];
  const symbols={eq:'=',ne:'≠',lt:'<',lte:'≤',gt:'>',gte:'≥'};
  if(['all','any','not'].includes(observation.expression?.op))lines.push('以下是子比较的快照；整体条件按原规则组合判断。');
  for(const check of observation.checks||[]){const reference=check.left||{},kind=Object.keys(reference)[0],key=reference[kind],label=kind==='quantity'?'容纳 '+materialLabel(key,entity):kind==='concentration'?materialLabel(key,entity)+'容纳份额':kind==='object'?objectName(key,'关联物件')+' · '+factLabel(key?.field):kind==='field'||kind==='mechanical'||kind==='contract'?factLabel(key):'子条件';lines.push(label+'：'+(check.known===true?factValue(check.actual_left)+' '+(symbols[check.operator]||check.operator)+' '+factValue(check.actual_right)+'（'+(check.satisfied?'成立':'未成立')+'）':'当前无法核对'));}
  for(const fact of observation.facts||[]){const reference=fact.reference||{},kind=Object.keys(reference)[0],key=reference[kind],label=kind==='quantity'?'容纳材料 '+materialLabel(key,entity):kind==='concentration'?'容纳份额 '+materialLabel(key,entity):kind==='object'?objectName(key,'关联物件')+' · '+factLabel(key?.field):factLabel(kind)+' · '+factLabel(key);lines.push(label+'：'+(fact.known===true?factValue(fact.actual):'当前未公开或不可观察'));}
  if(observation.input_scope==='parameter_defaults')lines.push('以上条件使用当前默认参数；实际填写的参数仍在提交时核验。');
  return lines;
}
export function reactionObservationLines(creative,entity){
  const lines=[],inputs=creative?.reaction_inputs,materials={...entity,creative:{...(entity?.creative||entity?.state?.program||{}),...creative}};
  if(inputs){const actual=Object.entries(inputs.quantities||{}).map(([material,quantity])=>materialLabel(material,materials)+' '+quantity+' 份').join('，');lines.push('已装入的反应材料：'+(actual||'当前没有材料')+'。物件本体和整件放入的物品不参与这份配料。');if(Number.isFinite(inputs.contained_quantity)&&Number.isFinite(inputs.capacity))lines.push('已装材料 '+inputs.contained_quantity+' / '+inputs.capacity+' 份。');}
  const blockerLabels={condition_unknown:'所需条件还未读到',condition_unmet:'所需条件未满足',insufficient_inputs:'材料不足',capacity:'剩余容量不足',program_error:'规则已暂停',not_intact:'物件不完整'};
  const reactionName=reaction=>{
    if(snapshotName(reaction))return readableText(snapshotName(reaction),'材料反应');
    const from=(reaction.inputs||[]).map(input=>materialLabel(input.material_id||input.symbol,materials)).join(' + '),to=Object.keys(reaction.outputs_per_extent||{}).map(material=>materialLabel(material,materials)).join(' + ');
    return from&&to?from+' → '+to:readableSymbol(reaction.rule_id)||'材料反应';
  };
  for(const reaction of creative?.reaction_observations||[]){lines.push(reactionName(reaction)+'：'+((reaction.blockers||[]).length?reaction.blockers.map(value=>blockerLabels[value]||factValue(value)).join('、'):'这项反应所需的材料和条件当前满足')+'。');for(const input of reaction.inputs||[])lines.push(materialLabel(input.material_id||input.symbol,materials)+'：当前可用 '+input.available+' 份，每次需 '+input.per_extent+' 份'+(input.missing_for_one_extent?'，还缺 '+input.missing_for_one_extent+' 份':'')+'。');if(Object.keys(reaction.outputs_per_extent||{}).length)lines.push('每次反应产出：'+Object.entries(reaction.outputs_per_extent).map(([material,quantity])=>materialLabel(material,materials)+' '+quantity+' 份').join('，')+'。');lines.push(...conditionObservationLines(reaction.condition,materials));}
  for(const reaction of creative?.reaction_observations||[])if(reaction.capacity_limited&&reaction.isolated_extent>0)lines.push(reactionName(reaction)+' 受容量限制；仅考虑当前这项反应的材料和条件，可进行 '+reaction.isolated_extent+' 次。');
  if((creative?.reaction_observations||[]).length)lines.push('以上是当前读到的条件和材料；多项反应的材料分配与实际产出，随世界运行结算。');
  if(creative?.reaction_observations_truncated)lines.push('当前只展示前 8 项反应；可读取这件物品的完整规则查看其余反应。');
  if(creative?.process?.last_reaction)lines.push('上次反应是历史记录（世界时刻 '+previewNumber(creative.process.last_reaction.source_last_tick)+'），当前产物数量以物品内的实际材料为准。');
  return lines;
}
export function ruleChoices(descriptor,key){
  return (Array.isArray(descriptor?.choices?.[key])?descriptor.choices[key]:[]).filter(value=>value&&typeof value==='object'&&typeof value.id==='string');
}
export function actionIds(actions){return (actions||[]).map(action=>typeof action==='string'?action:action?.action).filter(value=>typeof value==='string');}
export function actionDescriptors(inspection){
  return (inspection?.action_options||[]).filter(value=>value&&typeof value==='object'&&typeof value.action==='string');
}
const operationLabels={pickup:'拿进背包',drop:'放到脚下',open:'打开',close:'合上',store:'整件放入',retrieve:'取出整件物品',fill:'从这里装水',pour:'向这里倒水',ignite:'用火种点燃',harvest:'采集',tend:'用水养护',fuel:'添入木料',dismantle:'拆回材料',switch_mode:'切换用途',attach:'绑定部件',detach:'解绑到地面',launch:'放入水中',transfer:'转入材料'};
export function descriptorLabel(descriptor){
  if(descriptor?.function_id!=='grove.rule_action'&&operationLabels[descriptor?.action])return operationLabels[descriptor.action];
  return readableText(descriptor?.label||descriptor?.use||readableSymbol(descriptor?.action),'使用');
}
export function descriptorCandidates(descriptor){
  return (descriptor?.input?.candidates||[]).filter(item=>item&&typeof item==='object'&&typeof item.id==='string');
}
function previewNumber(value){return Number.isFinite(value)?String(value):'未读取';}
function previewLoad(summary){
  const applicable=summary?.support_limit_applicable!==false;
  return '操作后承受载荷 '+previewNumber(summary?.after?.supported)+(applicable?' / '+previewNumber(summary?.after?.limit):'')+' 载荷单位'+
    (!applicable?' · 未设结构承载上限':summary?.overloaded===true?' · 会过载 '+previewNumber(summary.after?.overload):Number.isFinite(summary?.support_margin)?' · 余量 '+summary.support_margin:'');
}
export function candidatePreviewSummary(candidate){
  const preview=candidate?.preview;if(!preview||typeof preview!=='object')return '';
  const physics=preview.physics||{};
  if(physics.known!==true)return '组合载荷预估未知';
  const affected=Array.isArray(physics.affected)?physics.affected:[],otherOverloads=affected.filter(row=>row.id!==(physics.subject?.id||preview.target_id)&&row.overloaded===true).length;
  return previewLoad(physics)+(otherOverloads?' · 另有 '+otherOverloads+' 个受影响结构会过载':'');
}
function objectName(value,fallback='物件'){
  return readableText(typeof value==='string'?value:value?.name||value?.label,fallback);
}
function candidateContext(candidate){
  const position=Number.isFinite(candidate?.x)&&Number.isFinite(candidate?.y)?candidate:candidate?.position||candidate?.location;
  if(Number.isFinite(position?.x)&&Number.isFinite(position?.y))return '位置 '+position.x+'，'+position.y;
  const kind=candidate?.type_name||candidate?.creative?.type_name||candidate?.state?.kind||candidate?.creative?.type_id||candidate?.type_id||candidate?.selection_kind||candidate?.kind;
  return kind?factValue(kind):'';
}
function readableCandidateSummary(candidate){
  const lots=matterLots(candidate);
  if(lots.length)return lots.map(lot=>slotLabel(lot.slot)+' '+materialLabel(lot,candidate)+' '+lot.quantity+' 份').join('，');
  if(!candidate?.summary)return '';
  return readableText(candidate.summary,'').replace(/\b(wood|stone|cloth|metal|water|leather)(?=\d|\b)/g,value=>materialLabels[value]+' ').replace(/(\d+)u\b/g,'$1 份');
}
export function candidateLabel(candidate,candidates=[]){
  const name=objectName(candidate),sameName=Array.isArray(candidates)&&candidates.filter(value=>objectName(value)===name).length>1;
  const transferable=Array.isArray(candidate?.lots)?candidate.lots.filter(lot=>Number.isInteger(lot?.quantity)&&lot.quantity>0).map(lot=>materialLabel(lot,candidate)+' '+lot.quantity+' 份').join('，'):'';
  return [name,sameName?candidateContext(candidate):null,candidate?.feedstock?'每制备 1 份，消耗 1 份'+materialLabel(candidate.feedstock,candidate):null,readableCandidateSummary(candidate),transferable?'可转入 '+transferable:null,candidatePreviewSummary(candidate)].filter(Boolean).join(' · ');
}
export function operationIntentLabel(descriptor,target,{item,lot,amount,choice}={}){
  const candidates=[...descriptorCandidates(descriptor),...ruleChoices(descriptor,'items'),...ruleChoices(descriptor,'others')];
  const source=typeof item==='string'?candidates.find(candidate=>candidate.id===item)||item:item;
  const selectedLot=typeof lot==='string'?descriptorLots(descriptor,source?.id).find(value=>(value.id||value.lot_id)===lot):lot;
  const selectedChoice=typeof choice==='string'?[...(descriptor?.input?.choices||[]),...ruleChoices(descriptor,'materials')].find(value=>(value.id||value.value)===choice)||choice:choice;
  const targetName=objectName(target,'目标物件'),sourceName=objectName(source,'所选物件');
  const selectedAmount=amount??descriptor?.input?.quantity?.default??descriptor?.arguments?.amount;
  const quantity=material=>(Number.isFinite(selectedAmount)&&selectedAmount>0?selectedAmount+' 份':'所选数量的')+material;
  switch(descriptor?.action){
    case 'store':return '把 '+sourceName+' 整件放进 '+targetName;
    case 'transfer':{
      const sourceProgram={...(source?.state?.program||{}),...(source?.creative||{})},targetProgram={...(target?.state?.program||{}),...(target?.creative||{})};
      const materials={creative:{...targetProgram,...sourceProgram,material_definitions:{...(targetProgram.material_definitions||targetProgram.materials||{}),...(sourceProgram.material_definitions||sourceProgram.materials||{})},material_aliases:{...(targetProgram.material_aliases||{}),...(sourceProgram.material_aliases||{})}}};
      return '从 '+sourceName+' 转入 '+quantity(materialLabel(selectedLot,materials))+' → '+targetName;
    }
    case 'fill':return '从 '+targetName+' 装 '+quantity('水')+' → '+sourceName;
    case 'pour':return '从 '+sourceName+' 倒出 '+quantity('水')+' → '+targetName;
    case 'ignite':return '用 '+sourceName+' → '+targetName;
    case 'retrieve':return '从 '+targetName+' 取出整件 '+sourceName;
    case 'attach':return '把 '+sourceName+' 绑定到 '+targetName;
    case 'pickup':return '把 '+targetName+' 拿进背包';
    case 'drop':return '把 '+targetName+' 放到脚下';
    case 'open':return '打开 '+targetName;
    case 'close':return '合上 '+targetName;
    case 'detach':return '把 '+targetName+' 解绑到地面';
    case 'launch':return '把 '+targetName+' 放入水中';
    case 'tend':return '从 '+sourceName+' 用 '+quantity('水')+'养护 → '+targetName;
    case 'charge':{
      const prepared=materialLabel(selectedChoice,target),feedstock=selectedChoice?.feedstock;
      return '用 '+sourceName+(feedstock?' 的 '+quantity(materialLabel(feedstock,target)):'')+' → 在 '+targetName+' '+descriptorLabel(descriptor)+' '+quantity(prepared);
    }
    case 'fuel':return descriptor?.function_id==='grove.rule_action'?'用 '+sourceName+' 的 '+quantity('木')+' → '+targetName+' · '+descriptorLabel(descriptor):'从 '+sourceName+' 添入 '+quantity('木')+' → '+targetName;
    case 'switch_mode':return '把 '+targetName+' 切换为 '+objectName(selectedChoice,'所选用途');
    case 'connect':return '把 '+targetName+' 连接到 '+sourceName;
    case 'disconnect':return '断开 '+targetName+' 与 '+sourceName+' 的连接';
    default:return descriptorLabel(descriptor)+(source?' · 用 '+sourceName+' → '+targetName:' · '+targetName)+(selectedChoice?' · '+objectName(selectedChoice,'所选方式'):'');
  }
}
export function candidatePreviewLines(candidate){
  const preview=candidate?.preview;if(!preview||typeof preview!=='object')return [];
  const physics=preview.physics||{},before=physics.before||{},after=physics.after||{},lines=[];
  lines.push(preview.available===false?'当前不能操作：'+readableText(preview.reason,'操作条件不满足。'):preview.available===true?'当前操作条件满足；过载风险见下方。':'当前操作条件还未读到。');
  if(physics.known!==true)lines.push('组合载荷预估未知：'+readableText(physics.reason,'没有取得足够的可见物理状态。'));
  else{
    lines.push('目标 '+objectName(physics.subject,'所选物件')+'：本件载荷 '+previewNumber(before.local)+' → '+previewNumber(after.local)+'，总载荷 '+previewNumber(before.total)+' → '+previewNumber(after.total)+' 载荷单位。');
    lines.push('承受载荷 '+previewNumber(before.supported)+' → '+previewNumber(after.supported)+' 载荷单位；'+(physics.support_limit_applicable===false?'未设结构承载上限。':'承载上限 '+previewNumber(before.limit)+' → '+previewNumber(after.limit)+'；操作后余量 '+previewNumber(physics.support_margin)+' 载荷单位。'));
    if(physics.support_limit_applicable!==false)lines.push('过载量 '+previewNumber(before.overload)+' → '+previewNumber(after.overload)+'；'+(physics.overloaded===true?'操作后会过载，结构可能继续受损。':physics.overloaded===false?'操作后当前目标不过载。':'是否过载未读取。'));
    lines.push('浮力 '+previewNumber(before.buoyancy)+' → '+previewNumber(after.buoyancy)+'；'+(physics.can_float===true?'操作后满足当前浮力条件。':physics.can_float===false?'操作后不满足当前浮力条件。':'当前浮力条件未读取。'));
    for(const row of Array.isArray(physics.affected)?physics.affected:[]){
      if(!row||row.id===(physics.subject?.id||preview.target_id))continue;
      lines.push('受影响结构 '+objectName(row,'附近结构')+'：'+previewLoad(row)+(row.can_float===true?' · 满足当前浮力条件':row.can_float===false?' · 不满足当前浮力条件':''));
    }
  }
  lines.push('只预估当前关系改变，不保证后续遇水、火或世界过程的结果。提交前仍会读取当前状态；取消选择不改变世界。');
  if(physics.overloaded===true)lines.push('危险实验仍由你选择；取消选择不改变世界。');
  lines.push('预览来自当前已读取的物件和角色状态；提交前会重新核对是否发生变化。');
  return lines;
}
export function previewVersionsChanged(inspection,entities,player){
  const current=new Map((entities||[]).filter(entity=>entity&&typeof entity.id==='string').map(entity=>[entity.id,entity]));
  const changed=(id,version)=>Number.isFinite(version)&&current.has(id)&&current.get(id).version!==version;
  const physicsChanged=(id,before)=>{
    const actual=current.get(id)?.simulation;if(!before||!actual)return false;
    // Descendants can gain water or lose fuel without changing their root's
    // version. Compare only already-visible authoritative aggregate fields.
    return ['local','total','supported','limit','overload','integrity','max_integrity','buoyancy','can_float'].some(key=>{
      const expected=before[key],value=actual[key];
      return (Number.isFinite(expected)&&Number.isFinite(value)||typeof expected==='boolean'&&typeof value==='boolean')&&expected!==value;
    });
  };
  return actionDescriptors(inspection).some(descriptor=>descriptorCandidates(descriptor).some(candidate=>{
    const preview=candidate.preview;if(!preview||typeof preview!=='object')return false;
    return Number.isFinite(preview.player_version)&&Number.isFinite(player?.version)&&preview.player_version!==player.version||
      changed(preview.target_id,preview.target_version)||changed(preview.item_id,preview.item_version)||
      physicsChanged(preview.target_id,preview.physics?.before)||
      (Array.isArray(preview.physics?.affected)?preview.physics.affected:[]).some(row=>row&&(changed(row.id,row.version)||physicsChanged(row.id,row.before)));
  }));
}
export function descriptorLots(descriptor,itemId){
  const item=descriptorCandidates(descriptor).find(candidate=>candidate.id===itemId);
  const values=Array.isArray(item?.lots)?item.lots:descriptor.input?.lots||[];
  return values.filter(lot=>lot&&typeof lot==='object'&&typeof (lot.id||lot.lot_id)==='string'&&(!lot.item_id||lot.item_id===itemId));
}
export function quantityBounds(descriptor,lot){
  const quantity=descriptor.input?.quantity||{};
  const min=Number.isInteger(quantity.min)&&quantity.min>0?quantity.min:1;
  const declaredMax=Number.isInteger(quantity.max)?quantity.max:Infinity;
  const max=Math.min(declaredMax,Number.isInteger(lot?.quantity)?lot.quantity:Infinity);
  const proposed=Number.isInteger(quantity.default)?quantity.default:min;
  return {min,max,value:Math.max(min,Math.min(proposed,max)),label:String(quantity.label||'数量')};
}
