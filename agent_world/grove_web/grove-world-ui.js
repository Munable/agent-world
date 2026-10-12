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

export function installedInteractionDefinition(value){
  const definition=value?.definition;
  if(!definition||!Array.isArray(definition.contact_reactions))return false;
  return definition.contact_reactions.some(rule=>rule?.activation==='installed');
}
export function interactionChoices(descriptor){
  return (Array.isArray(descriptor?.choices?.interactions)?descriptor.choices.interactions:[])
    .filter(value=>value&&typeof value.definition_ref==='string');
}

export function contactObservationLines(entity){
  const contact=entity?.simulation?.contact;if(!contact)return [];
  const process=contact.process||{},lines=['材料接触 · '+({running:'已发生反应',waiting:'等待材料或条件',error:'上次反应未成立'}[process.status]||'等待材料或条件')+' · 已成立 '+(process.steps||0)+' 步'];
  if(process.error)lines.push('上次失败：'+process.error);
  for(const interaction of contact.installed_interactions||[])lines.push('本容器已安装：'+readableText(interaction.name,'局部交互')+'；按固定版本在本容器内生效。');
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
  return [quantities,lifecycleLabel(entity),burning?'有残余材料正在燃烧':null,Number.isFinite(wetness)&&wetness>0?'吸水饱和度 '+wetness+'%':null,relationLabel(entity,backpackId)].filter(Boolean).join(' · ');
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
  if(Number.isFinite(sim.wetness))rows.push(['吸水饱和度',sim.wetness+'%']);
  return rows;
}
export function entityGlyph(entity){
  const glyph=entity?.presentation?.glyph;
  // A small text glyph cannot introduce HTML, a URL, or arbitrary scale.
  if(typeof glyph==='string'&&glyph.trim().length>0&&[...glyph].length<=3&&!/\p{C}/u.test(glyph))return glyph;
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
  const result={shape:shapes.has(presentation.shape)?presentation.shape:fallback,
    color:typeof presentation.color==='string'&&/^#[0-9a-f]{6}$/i.test(presentation.color)?presentation.color:'#aaac8c',
    glyph:entityGlyph(entity),size:(entity?.composition?.size??presentation.size)===2?2:1};
  if(['normal','folded'].includes(presentation.pose))result.pose=presentation.pose;
  if(['resolved','unavailable','unmapped'].includes(presentation.binding_status))result.binding_status=presentation.binding_status;
  const badge=presentation.badge;
  if(badge&&typeof badge==='object'&&!Array.isArray(badge)){
    const label=typeof badge.label==='string'&&[...badge.label].length<=12&&!/\p{C}/u.test(badge.label)?badge.label:'';
    const known=badge.known===true&&(typeof badge.value==='boolean'||Number.isSafeInteger(badge.value)&&Math.abs(badge.value)<=1000000);
    result.badge={label,value:known?badge.value:null,known};
  }
  return result;
}
export function presentationBadgeText(entity){
  const badge=safePresentation(entity).badge;if(!badge)return '';
  return (badge.label?badge.label+' ':'')+(badge.known?factValue(badge.value):'?');
}
export function presentationObservationLines(entity){
  const presentation=entity?.presentation,observation=presentation?.observation;
  if(!presentation||typeof presentation!=='object')return [];
  const lines=[];
  if(presentation.binding_status==='unavailable')lines.push('部分外观字段当前不可读，地图标为 ?；基底外观不代表默认字段或上一结果。');
  else if(presentation.binding_status==='unmapped')lines.push('当前字段值未配置完整外观映射，地图标为 ? 并保留基底外观。');
  if(observation&&Array.isArray(observation.bindings)){
    const at=Number.isInteger(observation.tick)?'世界时刻 '+observation.tick+' · ':'';
    const targets={color:'颜色',glyph:'符号',pose:'轮廓',badge:'数字／真假牌'};
    for(const row of observation.bindings.slice(0,4)){
      if(!row||!targets[row.target]||typeof row.field!=='string')continue;
      const actual=row.known===true?factValue(row.value):'当前不可读';
      const result=row.status==='resolved'?'已按声明显示':row.status==='unmapped'?'没有对应显示':'显示未知';
      lines.push(at+targets[row.target]+' ← 声明字段 '+factLabel(row.field)+' '+actual+'；'+result+'。');
    }
    if(observation.bindings.length)lines.push('外观只对应已声明字段，不证明吸水、加热、运动或其他机制已发生。');
  }
  return lines;
}
const factLabels={definition_ref:'规则定义',type_id:'对象类型',fields:'当前属性',materials:'材料定义',material_definitions:'材料定义',mechanical:'机械状态',recorder:'记录状态',contract:'双方契约',connections:'当前连接',process:'过程状态与历史',last_reaction:'保存的上次反应',source_last_tick:'保存的过程逻辑步',observed_tick:'本次观察逻辑步',last_tick:'过程保存的逻辑步',steps:'累计反应步数',steps_meaning:'步数含义',historical:'历史记录',current_production:'是否本次生产',meaning:'含义',history:'已发生的记录',position:'位置',angle:'角度',speed:'速度',status:'状态',energy:'能量',available_torque:'可用扭矩',active:'正在运行',started_tick:'开始逻辑步',until_tick:'截止逻辑步',sequence:'事件序号',tick:'逻辑步',occurred_at:'发生时间',recorded_tick:'记录逻辑步',action:'行动',entity_id:'对象',actor:'行动者',read_tick:'读取逻辑步',reads:'实际读取',parties:'双方身份',terms_version:'条款版本',accepted:'已接受的身份',deadline_tick:'截止逻辑步',delivered:'已交付',quantity:'数量',material:'材料',beneficiary:'受益方',id:'标识',name:'名称',version:'版本',unit:'单位',temperature:'温度',pressure:'压力',progress:'进展',running:'运行中',remaining:'剩余量',capacity:'容量',amount:'数量',item_id:'投入物件',other_id:'另一个对象',connected_to:'连接至',direction:'方向',mode:'方式',output:'产物',inputs:'投入',outputs:'产出',rules:'规则',types:'类型',actions:'行动',conditions:'条件',effects:'效果',description:'说明',label:'名称',duration:'持续逻辑步',fuel:'燃料',work:'做功',torque:'扭矩',reaction:'反应',state:'当前状态',owner:'身份',scope:'范围'};
Object.assign(factLabels,{definition_ref:'使用的规则',type_id:'物件种类',material_aliases:'材料称呼',material_imports:'引用的材料',source_last_tick:'上次反应的世界时刻',observed_tick:'本次观察的世界时刻',last_tick:'上次运行的世界时刻',started_tick:'开始的世界时刻',until_tick:'截止的世界时刻',tick:'世界时刻',recorded_tick:'记录的世界时刻',read_tick:'读取的世界时刻',deadline_tick:'截止的世界时刻',duration:'持续时刻数',terms_version:'当前条款记录',version:'更新记录',id:'引用的对象',entity_id:'关联物件',item_id:'所用物件',other_id:'关联对象',lot_id:'所选材料',target_id:'操作对象',material_id:'材料',symbol:'材料称呼',load_per_u:'每单位u载荷',per_extent:'每次所需',missing_for_one_extent:'还缺多少',available:'当前可用',slot:'材料位置',scope:'可见范围',snapshot_only:'当前观察',parameter_defaults:'默认参数',known:'是否读到',satisfied:'是否满足',intact:'结构完整',passable:'是否可通过',opened_sequence:'最近开放记录',started_event_sequence:'开始时的事件记录','barrier.active':'通道是否阻挡','barrier.opened_sequence':'通道最近开放记录','mechanical.position':'机械位置','mechanical.speed':'机械速度','mechanical.angle':'机械角度','mechanical.energy':'机械能量','mechanical.status':'机械状态'});
Object.assign(factLabels,{port:'绑定槽位',ports:'关联槽位',port_bindings:'当前绑定',eligible_ports:'可绑定槽位',cost:'区域原料（份）',cost_u:'成型所需（u）',sources:'材料来源',quantity_u:'数量（u）',expected_entity_version:'来源更新记录'});
Object.assign(factLabels,{physical:'真实物理',wetness:'湿度',burning:'是否燃烧','structure.integrity':'结构完整度','load.local':'本件载荷','load.total':'总载荷','load.supported':'承受载荷','load.limit':'承载上限','load.overload':'超出载荷',buoyancy:'浮力',can_float:'是否可浮',mechanical:'保存机械状态',automatic_rule_count:'自动声明数量',explicit_action_count:'显式行动数量',automatic_rules_projection:'自动声明读取范围',automatic_rules_truncated:'自动声明展示未完整'});
Object.assign(factLabels,{error:'上次运行错误',blocked_reason:'等待原因',blocked_rules:'等待可读事实的声明',rule_id:'声明',phase:'阶段',reason:'原因'});
Object.assign(factLabels,{status_meaning:'过程标记含义',last_tick_meaning:'过程记录时刻含义'});
Object.assign(factLabels,{presentation:'声明外观',bindings:'外观绑定',binding_status:'外观读取状态',shape:'轮廓',color:'颜色',glyph:'符号',pose:'展开／折叠轮廓',badge:'数字／真假牌',cases:'精确值映射',display:'显示值',matched:'是否匹配显示',observation:'本次外观读取'});
export const factLabel=key=>key===null||key===undefined?'属性':factLabels[key]||readableSymbol(String(key))||'属性';
const valueLabels={...materialLabels,body:'本体',contained:'容纳的材料',surface:'表面',absorbed:'吸附',ground:'地面',held:'随身',attached:'绑定',private:'私人',public:'公共',intact:'完整',broken:'破损',consumed:'材料耗尽',idle:'闲置',running:'运行中',stopped:'已停止',active:'进行中',complete:'已完成',failed:'已失败',error:'已暂停',pending:'等待中',waiting:'等待材料或条件',awaiting_acceptance:'等待双方接受',unoffered:'尚未发起约定',no_source:'未接入动力',program_error:'规则已暂停',accepted:'已接受',released:'已领出',independent:'分别反应',exclusive:'择一反应',reaction:'材料反应',after:'反应之后',logical_matter:'材料份数',whole_entity:'整件物件',material_lot:'材料',role:'人物',entity:'物件',resource_node:'资源点',water_source:'水源',ignition:'火种',container:'容器',structure:'结构'};
Object.assign(valueLabels,{unavailable_fact_or_port:'事实或绑定槽位当前不可读'});
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
function conditionReferenceLabel(reference,entity){
  const kind=Object.keys(reference)[0],key=reference[kind];
  if(kind==='quantity')return '容纳 '+materialLabel(key,entity);
  if(kind==='concentration')return materialLabel(key,entity)+'容纳份额';
  if(kind==='object')return (key?.port?'槽位 '+readableSymbol(key.port):objectName(key,'关联物件'))+' · '+factLabel(typeof key?.field==='string'?key.field.replace(/^physical\./,''):key?.field);
  if(['field','mechanical','contract','physical'].includes(kind))return factLabel(key);
  return kind?factLabel(kind)+' · '+factLabel(key):'子条件';
}
export function conditionObservationLines(observation,entity){
  if(!observation||typeof observation!=='object')return [];
  if(observation.known!==true)return [observation.reason==='event_required'?'条件未知：须有实际事件输入，当前快照不能判断。':'条件未知：当前未取得所需的可观察事实。'];
  const lines=[observation.satisfied?'本次读取时条件成立。':'本次读取时条件未成立。'];
  const symbols={eq:'=',ne:'≠',lt:'<',lte:'≤',gt:'>',gte:'≥'};
  if(['all','any','not'].includes(observation.expression?.op))lines.push('以下是子比较的快照；整体条件按原规则组合判断。');
  for(const check of observation.checks||[]){const unit=check.left_semantics?.unit,label=conditionReferenceLabel(check.left||{},entity)+(unit?'（'+unit+'）':'');lines.push(label+'：'+(check.known===true?factValue(check.actual_left)+' '+(symbols[check.operator]||check.operator)+' '+factValue(check.actual_right)+'（'+(check.satisfied?'成立':'未成立')+'）':'当前无法核对'));}
  for(const fact of observation.facts||[]){const unit=fact.semantics?.unit,label=conditionReferenceLabel(fact.reference||{},entity)+(unit?'（'+unit+'）':'');lines.push(label+'：'+(fact.known===true?factValue(fact.actual):'当前未公开或不可观察'));if(fact.semantics?.meaning)lines.push(fact.semantics.meaning);}
  if(observation.input_scope==='parameter_defaults')lines.push('以上条件使用当前默认参数；实际填写的参数仍在提交时核验。');
  return lines;
}
export function ruleRuntimeObservationLines(creative,entity){
  if(!creative||creative.hidden===true)return [];
  const lines=[],deployment=creative.deployment,mechanical=creative.mechanical_observation;
  if(creative.process?.status==='error'){
    lines.push('自动规则已暂停：'+readableText(creative.process.error,'需要处理当前规则错误')+'。');
    const diagnostic=creative.process.error_diagnostic;
    if(diagnostic?.code==='field_range'&&Number.isFinite(diagnostic.attempted_value))lines.push('字段 '+readableSymbol(diagnostic.field)+' 的实际赋值 '+diagnostic.attempted_value+' 超出允许范围 '+diagnostic.min+' 至 '+diagnostic.max+'。');
    if(creative.process.error_diagnostic_omitted)lines.push('精确错误位置未保存；现有错误不能当作正常运行结果。');
  }
  for(const [name,semantics] of Object.entries(creative.fact_semantics||{}))if(semantics.meaning)lines.push(factLabel(name.replace(/^physical\./,''))+'：'+semantics.meaning);
  const transition=creative.automatic_field_change;
  if(transition&&Number.isInteger(transition.step)){
    for(const [name,value] of Object.entries(transition.after||{}))lines.push('最近已记录的自动变化（逻辑步 '+transition.step+'）：'+factLabel(name)+' '+factValue(transition.before?.[name])+' → '+factValue(value)+'。这是历史后果，记录可能省略其他变化，不是当前状态或下一步预测。');
  }
  if(creative.automatic_field_change_status?.available===false)lines.push('暂无已记录的自动字段变化；不代表此前没有变化。');
  if(Number.isInteger(creative.automatic_rule_count)||Number.isInteger(creative.explicit_action_count)){
    const counts=[];
    if(Number.isInteger(creative.automatic_rule_count))counts.push('自动声明 '+creative.automatic_rule_count+' 项');
    if(Number.isInteger(creative.explicit_action_count))counts.push('显式行动 '+creative.explicit_action_count+' 项');
    lines.push(counts.join('，')+'。自动声明按世界逻辑步或实际公开事件评估；显式行动由人或获准 Agent 提交。');
  }
  if(deployment&&typeof deployment==='object'){
    if(deployment.activity_state==='sleeping')lines.push('规则对象已保存，当前休眠；仍可操作，相关输入变化会重新进入评估。休眠不是未获准运行。');
    else if(deployment.activity_state==='active')lines.push('规则对象当前列入活动执行；仍以已保存后果判断实际发生了什么。');
    if(typeof deployment.publicly_placed==='boolean')lines.push('这次观察的公开部署：'+(deployment.publicly_placed?'有公开位置。':'没有公开位置。'));
    if(typeof deployment.ordinary_rules_enabled==='boolean')lines.push(deployment.ordinary_rules_enabled?'普通自身声明当前具备评估资格，随身携带也可评估；资格不证明已执行。':'当前没有可评估的普通自身声明；这不判断显式行动是否可用。');
    if(typeof deployment.public_event_input_available==='boolean')lines.push(deployment.public_event_input_available?'这次观察具备公开事件读取资格；仍须有实际日志输入，资格不证明本次发生事件。':'这次观察不具备公开事件读取资格；条件不能以虚构事件补足。');
  }
  const typeLabels={integer:'整数',boolean:'布尔值',string:'文字'};
  for(const [name,source] of Object.entries(creative.published_exports||{})){
    lines.push('公开属性 '+readableSymbol(name.replace(/^authored\./,''))+'（'+(typeLabels[source.type]||factValue(source.type))+'）：'+(source.known===true?factValue(source.actual):'当前不可观察')+'。作者字段不自动代表物理量。');
  }
  for(const port of creative.port_sources||[]){
    const label=readableText(port.label||port.port,'来源槽位');
    if(port.source)lines.push('来源槽位 '+label+'：固定引用所选定义的 '+readableSymbol(port.source.type_id)+' 类型，版本不能被同名对象替换。');
    if(port.bound_entity_name)lines.push('当前关联：'+readableText(port.bound_entity_name,'关联物件')+'。');
    else if(!port.bound_entity_id)lines.push('来源槽位 '+label+' 尚未绑定实际物件。');
    if(port.source_matches===false)lines.push('当前来源与固定类型不符，不能读取为有效输入。');
    for(const [name,value] of Object.entries(port.facts||{}))lines.push(label+' · '+readableSymbol(name.replace(/^authored\./,''))+'：'+(value.known===true?factValue(value.actual):'当前未公开或不可观察')+'。');
  }
  const consumer=creative.event_consumer;
  if(consumer&&typeof consumer==='object'){
    if(Number.isInteger(consumer.pending_count)&&consumer.pending_count>0)lines.push('仍有 '+consumer.pending_count+' 条实际事件未消费；这些事件尚未形成规则后果。');
    if(consumer.status==='error'||consumer.paused===true)lines.push('事件消费已暂停：'+readableText(consumer.error,'需要处理当前消费问题')+'。保留的队列与游标不表示成功完成。');
    if(consumer.gap)lines.push('事件输入存在明确断档；缺少的事件不能补写为已发生或已消费。');
  }
  const rules=Array.isArray(creative.automatic_rules)?creative.automatic_rules:[];
  const phaseLabel=phase=>phase==='reaction'?'材料反应阶段':phase==='after'?'反应与机械之后':factValue(phase);
  const effectLabels={set:'自身字段变更',react:'材料反应',drive:'有限动力启停',emit:'声明信号'};
  for(const rule of rules){
    const event=rule.input_kind==='public_event',name=readableSymbol(rule.rule_id)||'自动声明';
    lines.push('自动声明 '+name+' · '+phaseLabel(rule.phase)+' · '+(event?'实际公开事件输入':'世界逻辑步输入')+'。');
    lines.push(...conditionObservationLines(rule.condition,entity));
    if(rule.condition?.known===true&&rule.condition.satisfied===false)lines.push('声明条件不成立时跳过该声明；不据此改写上次动力请求。');
    else if(rule.condition?.known===false)lines.push('声明条件未知时等待可读事实，不把未知当作否，也不据此改写上次动力请求。');
    if(event)lines.push('事件声明须读取实际事件；当前条件快照不能代替事件输入。');
    if(Array.isArray(rule.effect_ops)&&rule.effect_ops.length)lines.push('声明效果：'+rule.effect_ops.map(op=>effectLabels[op]||factValue(op)).join('、')+'；只有实际执行后才形成后果。');
    for(const input of rule.drive_inputs||[]){
      const observation=input.observation;
      lines.push(observation?.known===true?'启停表达式当前读数：'+(observation.satisfied?'启用':'关闭')+'；仅在声明实际执行时用于控制，该读数不证明已发出请求。':'启停表达式当前未知；不能据此推定关闭或改写上次控制请求。');
      if(observation?.known===true)lines.push(...conditionObservationLines(observation,entity).slice(1));
    }
  }
  if(rules.length)lines.push('以上是该次读取的条件快照，不证明自动声明已经执行，也不预测下一步结果。');
  if(creative.automatic_rules_truncated)lines.push('这里最多展示 8 项自动声明；其余声明须读取完整规则。');
  if(mechanical&&typeof mechanical==='object'){
    if(typeof mechanical.role==='string')lines.push('机械用途：'+({source:'动力源',gear:'齿轮传递',actuator:'执行器'}[mechanical.role]||factValue(mechanical.role))+'。');
    if(mechanical.role_meaning)lines.push(mechanical.role_meaning);
    if(mechanical.angle_unit==='logical_turns')lines.push('角度按累计圈数记录；速度是每个世界逻辑步的圈数。地图只播放两次确认读取之间的变化。');
    const inputs=mechanical.current_inputs,stored=mechanical.saved_outputs?.values||mechanical.stored_state||{},values=[];
    if(inputs&&typeof inputs==='object'){
      const actual=[];
      if(Number.isFinite(inputs.energy))actual.push('剩余能源 '+inputs.energy);
      if(typeof inputs.enabled==='boolean')actual.push('启用值 '+factValue(inputs.enabled));
      if(actual.length)lines.push('当前机械输入：'+actual.join('，')+'；添料后的输入不代表已经完成求解。');
    }
    for(const key of ['position','angle','speed','available_torque'])if(Number.isFinite(stored[key]))values.push(factLabel(key)+' '+stored[key]);
    if(!inputs&&Number.isFinite(stored.energy))values.push('保存能源 '+stored.energy);
    if(!inputs&&typeof stored.enabled==='boolean')values.push('保存启用值 '+factValue(stored.enabled));
    if(typeof stored.status==='string')values.push('保存状态 '+factValue(stored.status));
    if(values.length)lines.push('机械保存输出：'+values.join('，')+'；这是历史求解输出或初始值，不能断言当前正在运动。');
    if(typeof mechanical.solver_eligible==='boolean')lines.push(mechanical.solver_eligible?'这次观察具备机械求解资格；能源、连接和实际运动仍须核对求解结果。':'这次观察不具备机械求解资格；保存的输出不代表当前参与求解。');
    if(Array.isArray(deployment?.mechanical_blockers)&&deployment.mechanical_blockers.length){
      const blockers={no_mechanical_definition:'未声明机械用途',not_admitted:'未获实例准入',not_intact:'物件不完整或已退役',program_error:'规则已暂停',no_public_location:'没有公开位置'};
      lines.push('机械求解阻塞：'+deployment.mechanical_blockers.map(reason=>blockers[reason]||factValue(reason)).join('、')+'。');
    }
    const solved=mechanical.last_solved;
    lines.push(Number.isFinite(solved?.step)?'最近输出变化的求解步：'+solved.step+'；这不是每步参与求解的记录。':'未读到保存的求解标记；不能据此判断从未求解。');
    const request=mechanical.drive_request;
    if(request&&typeof request==='object'){
      const source=request.source,origin=source?.kind==='automatic_rule'?'自动声明 '+(readableSymbol(source.rule_id)||'未提供名称')+'（'+phaseLabel(source.phase)+'）':source?.kind==='explicit_action'?'显式行动 '+(readableSymbol(source.action_id)||'未提供名称')+(source.actor_role_id?'，行动者 '+objectName(source.actor_role_id,'关联人物'):''):'未读到来源';
      lines.push('保存的动力请求：'+(typeof request.enabled==='boolean'?(request.enabled?'启用':'关闭'):'未读到启停值')+'；发出于逻辑步 '+previewNumber(request.requested_step)+'，最早用于逻辑步 '+previewNumber(request.applies_from_step)+'；来源 '+origin+'。');
      if(mechanical.pending_next_step===true)lines.push('该请求尚未到最早生效逻辑步。');
      else if(mechanical.pending_next_step===false)lines.push('该请求已到最早生效逻辑步；这不证明已被求解或已经运动。');
    }else if(request===null)lines.push('这次观察没有保存的动力控制请求；启停表达式读数不替代请求记录。');
  }
  if(Number.isFinite(creative.process?.steps))lines.push('过程累计 '+creative.process.steps+' 个反应逻辑步；不是机械推进次数或目标完成进度。');
  return lines;
}

export function domainObservationLines(entity){
  const domain=entity?.execution_domain;
  if(!domain||typeof domain!=='object')return [];
  const lines=[];
  if(domain.state==='calculating')lines.push('相关物件正在结算世界时刻 '+domain.pending_target_tick+'；当前显示已保存状态，相关操作需稍候。');
  if(domain.state==='pressure')lines.push('相关作用域超过运行容量，自动变化已暂停。可以解除连接、停用规则或减少内容后恢复；其他地方继续运行。');
  if(domain.state==='error')lines.push('相关作用域自动运行受阻，原物件和材料保留；当前状态不是执行成功。');
  if(Number.isInteger(domain.last_applied_tick))lines.push('最近完成自动结算：世界时刻 '+domain.last_applied_tick+'。休眠期间没有虚构计算，恢复不补扣停顿期间的材料。');
  return lines;
}

export function executionTaskSummaryLines(execution){
  const summary=execution?.action_summary;
  if(!summary||summary.task_id!==execution.task_id||summary.scope!=='current_task_retained_confirmed_actions')return [];
  const coverage=summary.coverage||{},lines=[];
  if(Number.isInteger(summary.confirmed_count))lines.push('本目标已确认 '+summary.confirmed_count+' 次行动，包含同一目标的各段执行；'+(coverage.receipt_history_complete===true?'回执历史完整。':'仅统计仍保留的回执，不能当作全部历史。'));
  const costs=(Array.isArray(summary.material_costs)?summary.material_costs:[]).filter(row=>typeof row?.material==='string'&&Number.isFinite(row.quantity_u)&&row.quantity_u>=0);
  if(costs.length)lines.push('本目标回执明确记录的消耗：'+costs.map(row=>materialLabel(row.material)+' '+row.quantity_u+' 份').join('，')+'；不包含未报告的成本、材料转移或自动消耗。');
  if(coverage.operations_without_structured_costs>0||coverage.invalid_cost_operations>0||coverage.omitted_material_cost_rows>0)lines.push('部分行动没有完整成本明细；未列出不表示零消耗。');
  if(coverage.unknown_count>0||coverage.committed_unconfirmed_count>0)lines.push('仍有行动结果待核对，未计入已确认汇总。');
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
export function ruleChoices(descriptor,key,port){
  const candidates=(Array.isArray(descriptor?.choices?.[key])?descriptor.choices[key]:[]).filter(value=>value&&typeof value==='object'&&typeof value.id==='string');
  if(descriptor?.action==='bind_port'&&key==='others')return candidates.filter(value=>Array.isArray(value.eligible_ports)&&value.eligible_ports.includes(port));
  return candidates;
}
export function actionIds(actions){return (actions||[]).map(action=>typeof action==='string'?action:action?.action).filter(value=>typeof value==='string');}
export function actionDescriptors(inspection){
  return (inspection?.action_options||[]).filter(value=>value&&typeof value==='object'&&typeof value.action==='string');
}
const operationLabels={pickup:'拿进背包',drop:'放到脚下',open:'打开',close:'合上',store:'整件放入',retrieve:'取出整件物品',fill:'从这里装水',pour:'向这里倒水',ignite:'用火种点燃',harvest:'采集',tend:'用水养护',fuel:'添入燃料',dismantle:'拆回材料',switch_mode:'切换用途',attach:'绑定部件',detach:'解绑到地面',launch:'放入水中',transfer:'转入材料',install_interaction:'安装局部交互',uninstall_interaction:'卸载局部交互'};
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
export function candidateLabel(candidate,candidates=[],quantityUnit='份'){
  const name=objectName(candidate),sameName=Array.isArray(candidates)&&candidates.filter(value=>objectName(value)===name).length>1;
  const transferable=Array.isArray(candidate?.lots)?candidate.lots.filter(lot=>Number.isInteger(lot?.quantity)&&lot.quantity>0).map(lot=>materialLabel(lot,candidate)+' '+lot.quantity+' '+quantityUnit).join('，'):'';
  return [name,sameName?candidateContext(candidate):null,candidate?.feedstock?'每制备 1 份，消耗 1 份'+materialLabel(candidate.feedstock,candidate):null,readableCandidateSummary(candidate),transferable?'可转入 '+transferable:null,candidatePreviewSummary(candidate)].filter(Boolean).join(' · ');
}
export function operationIntentLabel(descriptor,target,{item,lot,amount,choice,port}={}){
  const candidates=[...descriptorCandidates(descriptor),...ruleChoices(descriptor,'items'),...ruleChoices(descriptor,'others',port)];
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
    case 'fuel':return '从 '+sourceName+' 添入 '+quantity(selectedLot?materialLabel(selectedLot,source):'所选燃料')+' → '+targetName;
    case 'bind_port':return '把 '+targetName+' 的 '+(port||'待选槽位')+' 绑定到 '+sourceName;
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
export function ruleSnapshotChanged(inspection,clock){
  const creative=inspection?.entity?.creative;
  if(!creative||creative.hidden||inspection.reading)return false;
  // Referenced objects and logical-step conditions can change without changing
  // the selected object's version. Re-read its complete authorised snapshot.
  return Number.isFinite(creative.world_tick)&&Number.isFinite(clock?.sequence)&&creative.world_tick!==clock.sequence;
}
export function descriptorLots(descriptor,itemId){
  const item=(descriptor?.function_id==='grove.rule_action'?ruleChoices(descriptor,'items'):descriptorCandidates(descriptor)).find(candidate=>candidate.id===itemId);
  const values=Array.isArray(item?.lots)?item.lots:descriptor?.input?.lots||[];
  return values.filter(lot=>lot&&typeof lot==='object'&&typeof (lot.id||lot.lot_id)==='string'&&(!lot.item_id||lot.item_id===itemId));
}
export function fabricationSources(menu){
  const cost=menu?.cost_u||{};
  return ruleChoices(menu?.action,'sources').map(source=>({...source,lots:(Array.isArray(source.lots)?source.lots:[]).filter(lot=>lot&&typeof lot.id==='string'&&Object.hasOwn(cost,lot.material)&&Number.isInteger(lot.quantity)&&lot.quantity>0)})).filter(source=>source.lots.length);
}
export function fabricationPlan(menu,rows){
  const errors=[],sources=[],totals=Object.create(null),used=new Map(),choices=fabricationSources(menu),cost=menu?.cost_u||{};
  if(!Array.isArray(rows)||!rows.length||rows.length>8)return {sources,totals,errors:['请选择一至八行真实来源材料。']};
  for(const row of rows){
    const source=choices.find(value=>value.id===row.entity_id),lot=source?.lots.find(value=>value.id===row.lot_id);
    if(!source||!Number.isInteger(source.version)){errors.push('原来源当前不可用，请重新选择；不会自动换料。');continue;}
    if(!lot||row.material_id!==lot.material){errors.push('原材料批次已耗尽、改变或不可用，请重新选择。');continue;}
    const quantity=Number(row.quantity_u);
    if(!Number.isInteger(quantity)||quantity<1){errors.push('每行数量须为大于零的整数。');continue;}
    const key=JSON.stringify([source.id,lot.id]);
    if(used.has(key)){errors.push('同一实际来源批次只能填写一行，请合并数量。');continue;}
    const quantityUsed=quantity;used.set(key,quantityUsed);
    if(quantityUsed>lot.quantity)errors.push('同一来源批次的合计用量超过当前 '+lot.quantity+' 单位。');
    totals[lot.material]=(totals[lot.material]||0)+quantity;
    sources.push({entity_id:source.id,expected_entity_version:source.version,lot_id:lot.id,quantity_u:quantity});
  }
  for(const [material,quantity] of Object.entries(cost))if(totals[material]!==quantity)errors.push('材料 '+material+' 须恰好选择 '+quantity+' 单位，当前 '+(totals[material]||0)+' 单位。');
  return {sources,totals,errors};
}
export function quantityBounds(descriptor,lot){
  const quantity=descriptor.input?.quantity||{};
  const min=Number.isInteger(quantity.min)&&quantity.min>0?quantity.min:1;
  const declaredMax=Number.isInteger(quantity.max)?quantity.max:Infinity;
  const max=Math.min(declaredMax,Number.isInteger(lot?.quantity)?lot.quantity:Infinity);
  const proposed=Number.isInteger(quantity.default)?quantity.default:min;
  return {min,max,value:Math.max(min,Math.min(proposed,max)),label:String(quantity.label||'数量')};
}

export function mechanicalRenderSample(entity){
  const observation=entity?.creative?.mechanical_observation,state=observation?.stored_state;
  if(!state||observation.angle_unit!=='logical_turns'||!observation.solver_eligible||entity.lifecycle!=='intact'||
     !Number.isFinite(entity.x)||!Number.isFinite(entity.y)||!Number.isFinite(state.angle))return null;
  const role=observation.role;if(!['source','gear','actuator'].includes(role))return null;
  return {id:entity.id,version:entity.version,tick:observation.observed_tick,role,angle:state.angle,
    position:state.position,travel:observation.travel,status:state.status,
    replayAllowed:['running','travel_limit','insufficient_energy'].includes(state.status)};
}

// Replay only the bounded difference between two received snapshots. Neither
// speed nor wall time can create another turn after the confirmed destination.
export function createMechanicalMotion(){
  const samples=new Map();let epoch=null;
  return {
    reset(){samples.clear();epoch=null;},
    accept(entities,clock,now){
      if(epoch!==clock?.epoch){samples.clear();epoch=clock?.epoch;}
      const seen=new Set();
      for(const entity of entities||[]){const value=mechanicalRenderSample(entity);if(!value)continue;seen.add(value.id);
        const previous=samples.get(value.id);
        if(previous&&(value.version<previous.value.version||value.tick<previous.value.tick))continue;
        if(previous&&value.version===previous.value.version&&value.angle===previous.value.angle&&value.status===previous.value.status&&value.position===previous.value.position)continue;
        const delta=previous?value.angle-previous.value.angle:0;
        const animate=!!previous&&value.replayAllowed&&Number.isFinite(delta)&&delta!==0;
        samples.set(value.id,{value,from:animate?previous.value.angle:value.angle,start:now,
          duration:animate?Math.min(900,Math.max(240,Math.abs(delta)*320)):0});
      }
      for(const id of samples.keys())if(!seen.has(id))samples.delete(id);
    },
    read(entity,now,fresh=true){
      const value=mechanicalRenderSample(entity);if(!value)return null;
      const sample=samples.get(value.id);
      if(!fresh||!value.replayAllowed||!sample||sample.value.version!==value.version)return {...value,angle:value.angle,replaying:false};
      const fraction=sample.duration?Math.min(1,Math.max(0,(now-sample.start)/sample.duration)):1;
      return {...value,angle:sample.from+(value.angle-sample.from)*fraction,replaying:sample.duration>0&&fraction<1};
    }
  };
}

export function visibleWaterQuantity(entity){
  const summary=entity?.content_summary;
  if(summary?.visible!==true)return 0;
  return (summary.materials||[]).filter(row=>row.material==='water').reduce((total,row)=>
    total+(Number.isFinite(row.quantity)?Math.max(0,row.quantity):0),0);
}

// Directory references are discovery hints. Only the current nearby map can
// supply an inspect target; directory.nearby and old detail caches cannot.
export function publicWorkLocalTarget(work,world,fresh=false){
  if(!fresh||typeof work?.entity_id!=='string'||!world?.bounds)return null;
  const bounds=world.bounds;
  return (world.entities||[]).find(entity=>entity.id===work.entity_id&&!entity.owner&&!entity.container&&
    Number.isInteger(entity.x)&&Number.isInteger(entity.y)&&
    entity.x>=bounds.min_x&&entity.x<=bounds.max_x&&entity.y>=bounds.min_y&&entity.y<=bounds.max_y)||null;
}

export function publicWorkFactLines(work){
  const lines=[];
  if(work?.contents_visibility==='hidden')lines.push('容器已关闭；内部字段未公开。');
  else{
    const fields=work?.fields&&typeof work.fields==='object'&&!Array.isArray(work.fields)?Object.entries(work.fields):[];
    const visible=fields.filter(([,value])=>Number.isInteger(value)||typeof value==='boolean').slice(0,8);
    if(visible.length)lines.push('保存字段：'+visible.map(([name,value])=>factLabel(name)+' '+factValue(value)).join('，'));
    if(Number.isInteger(work?.fields_omitted_count)&&work.fields_omitted_count>0)lines.push('另有 '+work.fields_omitted_count+' 个字段，到附近读取详情。');
  }
  if(typeof work?.lifecycle==='string')lines.push('对象状态：'+factValue(work.lifecycle));
  return lines;
}

export function publicWorkDirectoryLines(page){
  if(!page)return ['展开后读取最近公开作品。'];
  const filters=page.filters||{},lines=[];
  if(filters.author)lines.push('按作者身份查找：'+filters.author);
  else if(filters.author_name)lines.push('作者姓名：'+filters.author_name);
  if(filters.name)lines.push('作品名称：'+filters.name);
  if(!filters.author&&!filters.author_name&&!filters.name)lines.push('最近公开的顶层作品。');
  if(page.index_build_complete!==true)lines.push('旧作品目录仍在建立；当前结果只含已索引作品，稍后可刷新。');
  if(page.author_identity_required)lines.push('姓名不能区分作者，请按独立身份查看；同名作者的作品不会合并。');
  if(!(page.works||[]).length&&!(page.author_matches||[]).length)lines.push('这一页没有符合条件的公开作品；空页不代表作品不存在。');
  if(page.has_more)lines.push('还有后续目录页；这不是全部结果。');
  return lines;
}

// Clip a directory marker to the view edge without moving the camera or
// inventing remote terrain, an object sprite, or a walkable route.
export function publicWorkMarkerGeometry(at,width,height,padding=56){
  if(!Number.isFinite(at?.x)||!Number.isFinite(at?.y)||!(width>0)||!(height>0))return null;
  const inset=Math.min(padding,width/4,height/4),cx=width/2,cy=height/2,dx=at.x-cx,dy=at.y-cy;
  const offscreen=at.x<inset||at.x>width-inset||at.y<inset||at.y>height-inset;
  const factor=offscreen?Math.min(dx?((width/2-inset)/Math.abs(dx)):Infinity,dy?((height/2-inset)/Math.abs(dy)):Infinity):1;
  return {x:cx+dx*factor,y:cy+dy*factor,offscreen,angle:Math.atan2(dy,dx)};
}

export function receiptOutcomeLines(receipt){
  const facts=receipt?.confirmed_outcome;
  if(!['committed','confirmed'].includes(receipt?.status)||facts?.kind!=='server_receipt')return [];
  const lines=(facts.changes||[]).filter(value=>typeof value==='string').slice(0,4);
  const action=facts.action_result||{},transfer=action.transfer;
  if(Number.isInteger(transfer?.transferred))lines.push((transfer.source_name||'来源物件')+' → '+(transfer.target_name||'目的物件')+'：'+transfer.transferred+'u '+materialLabel(transfer.material));
  for(const cost of (action.costs||[]).slice(0,8))if(Number.isInteger(cost.quantity_u))lines.push((cost.entity_name||'来源物件')+'实际消耗 '+cost.quantity_u+'u '+materialLabel(cost.material));
  if(Number.isFinite(action.stored_energy_after))lines.push((action.target_name||'动力源')+'提交后储能 '+action.stored_energy_after);
  for(const entity of (action.related_entities||[]).slice(0,8)){const relation=Number.isFinite(entity.x)&&Number.isFinite(entity.y)?'在地面 '+entity.x+'，'+entity.y:entity.owner===facts.player?.id?'由本人持有':entity.container?'在容器中':'位置未读回';lines.push((entity.name||'关联物件')+'行动后：'+relation);}
  const player=facts.player;
  if(Number.isFinite(player?.x)&&Number.isFinite(player?.y))lines.push('行动后本人位置 '+player.x+'，'+player.y);
  const entity=facts.entity;
  if(entity){
    const relation=Number.isFinite(entity.x)&&Number.isFinite(entity.y)?'在地面 '+entity.x+'，'+entity.y:entity.owner===player?.id?'由本人持有':entity.container?'在容器中':'位置未读回';
    lines.push((entity.name||'所操作物件')+'：'+relation);
  }
  if((facts.same_position_public_objects||[]).length)lines.push('行动后同格公开物件：'+facts.same_position_public_objects.map(item=>item.name||'未命名物件').join('、'));
  return lines;
}

export function executionReadbackLines(execution){
  if(execution?.readback_actual!==true)return ['当前现场尚未读回。'];
  const facts=execution.readback||{},player=facts.player,lines=[];
  if(Number.isFinite(player?.x)&&Number.isFinite(player?.y))lines.push('最近读回本人位置 '+player.x+'，'+player.y);
  const selected=facts.selected_inspection?.entity,mechanical=creativeState(null,selected)?.mechanical;
  if(selected){
    const lots=matterLots(selected);
    lines.push((selected.name||'所选物件')+'最近读回材料：'+(!Array.isArray(selected.matter)?'材料尚未读取':lots.length?lots.map(lot=>materialLabel(lot,selected)+' '+lot.quantity+'u').join('、'):'没有可见材料'));
    if(mechanical){const role=creativeState(null,selected)?.mechanical_observation?.role;lines.push('储能 '+(Number.isFinite(mechanical.energy)?mechanical.energy:'未读取')+' · 角度 '+(Number.isFinite(mechanical.angle)?mechanical.angle:'未读取')+' · '+(role==='actuator'?'行程':'机械位置')+' '+(Number.isFinite(mechanical.position)?mechanical.position:'未读取')+' · 速度 '+(Number.isFinite(mechanical.speed)?mechanical.speed:'未读取'));}
  }
  if(selected){const detailTick=creativeState(null,selected)?.world_tick;lines.push(Number.isInteger(detailTick)?'物件详情读取步 '+detailTick:'物件详情来自独立读取，未提供读取步。');}
  if(Number.isInteger(facts.clock?.sequence))lines.push('地图读取步 '+facts.clock.sequence+'；物件详情另行读取，后续变化请看地图。');
  return lines;
}
