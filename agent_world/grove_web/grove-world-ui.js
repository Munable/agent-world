// All presentation is data. This module never interprets host text as markup,
// loads a model asset, or turns a descriptor into an executable callback.
const materialLabels={wood:'木',stone:'石',cloth:'布',metal:'金属',water:'水',leather:'皮革'};
const slotLabels={body:'本体',contained:'容纳',surface:'表面',absorbed:'吸附'};
const parentLabels={ground:'在地面',held:'随身携带',contained:'在容器中',attached:'绑定在物件上'};
const finite=value=>Number.isFinite(value)?value:null;
export const materialLabel=(value,entity)=>{const program=entity?.creative||entity?.state?.program||{},id=program.material_aliases?.[value]||value;return program.material_definitions?.[id]?.name||program.materials?.[id]?.name||materialLabels[value]||String(value||'未标明');};
export const slotLabel=value=>slotLabels[value]||String(value||'未标明');

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
export const factLabel=key=>factLabels[key]||String(key);
export function factValue(value){
  if(value===null||value===undefined)return '未公开或未设定';
  if(typeof value==='boolean')return value?'是':'否';
  if(typeof value==='string'||typeof value==='number')return String(value);
  return Array.isArray(value)?value.length+' 项':'结构化状态';
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
  for(const check of observation.checks||[]){const reference=check.left||{},kind=Object.keys(reference)[0],key=reference[kind],label=kind==='quantity'?'容纳 '+materialLabel(key,entity):kind==='concentration'?materialLabel(key,entity)+'容纳份额':kind==='object'?String(key?.id||'对象')+' · '+factLabel(key?.field):kind==='field'||kind==='mechanical'||kind==='contract'?factLabel(key):'子条件';lines.push(label+'：'+(check.known===true?factValue(check.actual_left)+' '+(symbols[check.operator]||check.operator)+' '+factValue(check.actual_right)+'（'+(check.satisfied?'成立':'未成立')+'）':'当前无法核对'));}
  for(const fact of observation.facts||[]){const reference=fact.reference||{},kind=Object.keys(reference)[0],key=reference[kind],label=kind==='quantity'?'容纳材料 '+materialLabel(key,entity):kind==='concentration'?'容纳份额 '+materialLabel(key,entity):kind==='object'?String(key?.id||'对象')+' · '+factLabel(key?.field):factLabel(kind)+' · '+factLabel(key);lines.push(label+'：'+(fact.known===true?factValue(fact.actual):'当前未公开或不可观察'));}
  if(observation.input_scope==='parameter_defaults')lines.push('以上条件使用当前默认参数；实际填写的参数仍在提交时核验。');
  return lines;
}
export function reactionObservationLines(creative,entity){
  const lines=[],inputs=creative?.reaction_inputs;
  if(inputs){const actual=Object.entries(inputs.quantities||{}).map(([material,quantity])=>materialLabel(material,entity)+' '+quantity+' 单位').join('，');lines.push('本物件的容纳材料槽：'+(actual||'当前没有材料')+'。本体和完整子物件不计入反应投料。');if(Number.isFinite(inputs.contained_quantity)&&Number.isFinite(inputs.capacity))lines.push('实际容纳量 '+inputs.contained_quantity+' / '+inputs.capacity+' 单位。');}
  const blockerLabels={condition_unknown:'条件未知',condition_unmet:'声明条件未成立',insufficient_inputs:'反应原料不足',capacity:'容量不满足',program_error:'规则已暂停',not_intact:'物件不完整'};
  for(const reaction of creative?.reaction_observations||[]){lines.push('反应 '+reaction.rule_id+'：'+((reaction.blockers||[]).length?reaction.blockers.map(value=>blockerLabels[value]||value).join('、'):'当前单条材料与条件满足')+'。');for(const input of reaction.inputs||[])lines.push(materialLabel(input.material_id||input.symbol,entity)+'：当前可用 '+input.available+'，每份反应需 '+input.per_extent+(input.missing_for_one_extent?'，缺 '+input.missing_for_one_extent:'')+' 单位。');lines.push(...conditionObservationLines(reaction.condition,entity));}
  for(const reaction of creative?.reaction_observations||[])if(reaction.capacity_limited&&reaction.isolated_extent>0)lines.push('反应 '+reaction.rule_id+' 受容量限制，当前单条材料快照仍可支持 '+reaction.isolated_extent+' 份反应。');
  if((creative?.reaction_observations||[]).length)lines.push('这些是当前快照的条件和材料；通道竞争与实际产出由世界逻辑步结算。');
  if(creative?.reaction_observations_truncated)lines.push('当前只展示前 8 条反应；其余请读取本物件的完整规则定义。');
  if(creative?.process?.last_reaction)lines.push('保存的上次反应属于历史记录（过程逻辑步 '+creative.process.last_reaction.source_last_tick+'），不证明本次生产或当前产物数量。');
  return lines;
}
export function ruleChoices(descriptor,key){
  return (Array.isArray(descriptor?.choices?.[key])?descriptor.choices[key]:[]).filter(value=>value&&typeof value==='object'&&typeof value.id==='string');
}
export function actionIds(actions){return (actions||[]).map(action=>typeof action==='string'?action:action?.action).filter(value=>typeof value==='string');}
export function actionDescriptors(inspection){
  return (inspection?.action_options||[]).filter(value=>value&&typeof value==='object'&&typeof value.action==='string');
}
export function descriptorLabel(descriptor){return String(descriptor.label||descriptor.action||'行动');}
export function descriptorCandidates(descriptor){
  return (descriptor.input?.candidates||[]).filter(item=>item&&typeof item==='object'&&typeof item.id==='string');
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
export function candidateLabel(candidate){
  return [candidate?.name||candidate?.id||'物件',candidate?.feedstock?'每1u消耗1u'+materialLabel(candidate.feedstock):null,candidate?.summary,candidatePreviewSummary(candidate)].filter(Boolean).join(' · ');
}
export function candidatePreviewLines(candidate){
  const preview=candidate?.preview;if(!preview||typeof preview!=='object')return [];
  const physics=preview.physics||{},before=physics.before||{},after=physics.after||{},lines=[];
  lines.push(preview.available===false?'当前不能提交：'+(preview.reason||'操作条件不满足。'):preview.available===true?'当前操作资格允许提交；是否过载单独显示，危险实验仍由你选择。':'当前操作资格尚未读取。');
  if(physics.known!==true)lines.push('组合载荷预估未知：'+(physics.reason||'没有取得足够的可见物理状态。'));
  else{
    lines.push('目标 '+(physics.subject?.name||physics.subject?.id||preview.target_id||'物件')+'：本件载荷 '+previewNumber(before.local)+' → '+previewNumber(after.local)+'，总载荷 '+previewNumber(before.total)+' → '+previewNumber(after.total)+' 载荷单位。');
    lines.push('承受载荷 '+previewNumber(before.supported)+' → '+previewNumber(after.supported)+' 载荷单位；'+(physics.support_limit_applicable===false?'未设结构承载上限。':'承载上限 '+previewNumber(before.limit)+' → '+previewNumber(after.limit)+'；操作后余量 '+previewNumber(physics.support_margin)+' 载荷单位。'));
    if(physics.support_limit_applicable!==false)lines.push('过载量 '+previewNumber(before.overload)+' → '+previewNumber(after.overload)+'；'+(physics.overloaded===true?'操作后会过载，结构可能继续受损。':physics.overloaded===false?'操作后当前目标不过载。':'是否过载未读取。'));
    lines.push('浮力 '+previewNumber(before.buoyancy)+' → '+previewNumber(after.buoyancy)+'；'+(physics.can_float===true?'操作后满足当前浮力条件。':physics.can_float===false?'操作后不满足当前浮力条件。':'当前浮力条件未读取。'));
    for(const row of Array.isArray(physics.affected)?physics.affected:[]){
      if(!row||row.id===(physics.subject?.id||preview.target_id))continue;
      lines.push('受影响结构 '+(row.name||row.id)+'：'+previewLoad(row)+(row.can_float===true?' · 满足当前浮力条件':row.can_float===false?' · 不满足当前浮力条件':''));
    }
  }
  lines.push('只预估当前关系改变，不保证后续遇水、火或世界过程的结果。提交前仍会读取当前状态；取消选择不改变世界。');
  lines.push('预览依据：目标版本 '+previewNumber(preview.target_version)+' · 所选物件版本 '+previewNumber(preview.item_version)+' · 角色版本 '+previewNumber(preview.player_version));
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
