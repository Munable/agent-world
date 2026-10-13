// World-owned client data only. Text is always rendered as text; this module
// never interprets a definition, storage state, expression or executable callback.
const materialLabels={wood:'木',stone:'石',cloth:'布',metal:'金属',water:'水',leather:'皮革'};
const slotLabels={body:'本体',contained:'容纳',surface:'表面',absorbed:'吸附'};
const parentLabels={ground:'在地面',held:'随身携带',contained:'在容器中',attached:'绑定在物件上'};
const referencePattern=/(?:\b(?:program(?:_ref)?|object|entity|player|role|lot|operation)[_:.-][a-z0-9_-]+(?:#[a-z0-9_-]+)?\b|\b[0-9a-f]{24,}\b)/gi;
const readableText=(value,fallback='未提供名称')=>typeof value==='string'&&value.trim()?value.trim().replace(referencePattern,'关联对象'):fallback;
export const factLabel=key=>readableText(String(key??'属性')).replace(/_/g,' ');
export function factValue(value){
  if(value===null||value===undefined)return '未公开或不可读';
  if(typeof value==='boolean')return value?'是':'否';
  if(typeof value==='number')return String(value);
  if(typeof value==='string')return readableText(value);
  return Array.isArray(value)?value.length+' 项':'详细事实';
}
export function factRowText(row){
  if(row?.availability!=='known')return '未公开或不可读';
  return factValue(row.display_value??row.value)+(row.unit?' '+row.unit:'');
}
export function factLines(rows,prefix=''){
  return (rows||[]).flatMap(row=>row?.type==='group'?factLines(row.rows,prefix+(row.label||'事实')+' · '):[prefix+(row.label||'事实')+'：'+factRowText(row)]);
}
export const summaryFact=(entity,id)=>(entity?.summary_facts||[]).find(row=>row.id===id);
export function materialLabel(value,entity){
  const material=value&&typeof value==='object'?value.material_id||value.material||value.id:value;
  const label=value&&typeof value==='object'?value.material_name||value.material_label||value.name||value.label:null;
  const projected=(entity?.materials||[]).find(row=>row.material_id===material);
  return readableText(label||projected?.material_name||materialLabels[material]||material,'未知材料');
}
export const slotLabel=value=>slotLabels[value]||String(value||'未标明');
export const matterLots=entity=>(entity?.materials||[]).filter(lot=>lot&&Number.isInteger(lot.quantity)&&lot.quantity>=0);
export const bodyQuantities=entity=>matterLots(entity).filter(lot=>lot.slot==='body').map(lot=>({material:lot.material_id,material_name:lot.material_name,quantity:lot.quantity}));
export function relationLabel(entity,backpackId){
  const parent=entity?.relations?.parent;if(!parent)return '';
  return parent.relation==='contained'&&parent.id===backpackId?'在自己的背包中':parentLabels[parent.relation]||'存在物理关联';
}
export const entitySummary=(entity,backpackId)=>[entity?.summary,relationLabel(entity,backpackId)].filter(Boolean).join(' · ')||'详细事实尚未读取';
export function lifecycleLabel(entity){return entity?.appearance?.overlays?.consumed?'材料已耗尽':entity?.appearance?.overlays?.broken?'结构已破损，残材仍在':'';}
export const simulationStats=entity=>(entity?.summary_facts||[]).map(row=>[row.label,factRowText(row)]);
export function entityGlyph(entity){
  const glyph=entity?.appearance?.glyph;
  return typeof glyph==='string'&&glyph.trim()&&[...glyph].length<=3&&!/\p{C}/u.test(glyph)?glyph:'◆';
}
export function safePresentation(entity){
  const appearance=entity?.appearance||{},shapes=new Set(['box','bowl','cloth','beam','stone','raft','part']);
  const result={shape:shapes.has(appearance.shape)?appearance.shape:'part',color:typeof appearance.color==='string'&&/^#[0-9a-f]{6}$/i.test(appearance.color)?appearance.color:'#aaac8c',glyph:entityGlyph(entity),size:appearance.size===2?2:1,container_open:appearance.container_open,overlays:appearance.overlays||{}};
  if(['normal','folded'].includes(appearance.pose))result.pose=appearance.pose;
  if(['resolved','unavailable','unmapped'].includes(appearance.binding_status))result.binding_status=appearance.binding_status;
  const badge=appearance.badge;
  if(badge&&typeof badge==='object'){
    const label=typeof badge.label==='string'&&[...badge.label].length<=12&&!/\p{C}/u.test(badge.label)?badge.label:'';
    const known=badge.known===true&&(typeof badge.value==='boolean'||Number.isSafeInteger(badge.value)&&Math.abs(badge.value)<=1000000);
    result.badge={label,known,value:known?badge.value:null};
  }
  return result;
}
export function presentationBadgeText(entity){const badge=safePresentation(entity).badge;return badge?(badge.label?badge.label+' ':'')+(badge.known?factValue(badge.value):'?'):'';}
export const actionIds=actions=>(actions||[]).map(action=>typeof action==='string'?action:action?.action).filter(value=>typeof value==='string');
export const actionDescriptors=inspection=>(inspection?.entity?.actions||inspection?.actions||[]).filter(row=>row&&typeof row.function_id==='string'&&typeof row.id==='string');
export const descriptorLabel=descriptor=>readableText(descriptor?.label,'使用');
export const ruleChoices=(descriptor,key)=>(descriptor?.choices?.[key]||[]).filter(row=>row&&typeof row.id==='string');
export const candidateLabel=candidate=>[readableText(candidate?.label||candidate?.name,'来源物件'),candidate?.summary].filter(Boolean).join(' · ');
export const installedInteractionDefinition=value=>value?.installable===true;
export const previewVersionsChanged=(inspection,entities,player)=>Number.isInteger(inspection?.player?.version)&&inspection.player.version!==player?.version;
export const ruleSnapshotChanged=(inspection,clock)=>!!inspection?.context&&inspection.context.observation_clock?.epoch===clock?.epoch&&inspection.context.observation_clock?.sequence!==clock?.sequence;
export function mechanicalRenderSample(entity){
  const motion=entity?.appearance?.motion;
  if(!motion||!['rotation','linear_actuator'].includes(motion.kind)||!Number.isFinite(motion.angle)||!Number.isFinite(motion.observed_tick)||!Number.isFinite(entity.x)||!Number.isFinite(entity.y))return null;
  return {id:entity.id,version:entity.version,tick:motion.observed_tick,role:motion.kind==='linear_actuator'?'actuator':'rotation',angle:motion.angle,position:motion.position,travel:motion.travel,replayAllowed:motion.replay_allowed===true};
}
export const visibleWaterQuantity=entity=>Number.isFinite(entity?.appearance?.overlays?.water_quantity)?entity.appearance.overlays.water_quantity:null;
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
    if(!Number.isInteger(row.expected_entity_version)||row.expected_entity_version!==source.version){errors.push('原来源的观察版本已改变或尚未确认，请按新事实重新确认同一来源。');continue;}
    if(!lot||row.material_id!==lot.material){errors.push('原材料批次已耗尽、改变或不可用，请重新选择。');continue;}
    const quantity=Number(row.quantity_u);
    if(!Number.isInteger(quantity)||quantity<1){errors.push('每行数量须为大于零的整数。');continue;}
    const key=JSON.stringify([source.id,lot.id]);
    if(used.has(key)){errors.push('同一实际来源批次只能填写一行，请合并数量。');continue;}
    const quantityUsed=quantity;used.set(key,quantityUsed);
    if(quantityUsed>lot.quantity)errors.push('同一来源批次的合计用量超过当前 '+lot.quantity+' 单位。');
    totals[lot.material]=(totals[lot.material]||0)+quantity;
    sources.push({entity_id:source.id,expected_entity_version:row.expected_entity_version,lot_id:lot.id,quantity_u:quantity});
  }
  for(const [material,quantity] of Object.entries(cost))if(totals[material]!==quantity)errors.push('材料 '+material+' 须恰好选择 '+quantity+' 单位，当前 '+(totals[material]||0)+' 单位。');
  return {sources,totals,errors};
}

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


export function publicWorkLocalTarget(work,world,fresh=false){
  if(!fresh||typeof work?.entity_id!=='string'||!world?.bounds)return null;
  const bounds=world.bounds;
  return (world.entities||[]).find(entity=>entity.id===work.entity_id&&entity.custody?.scope==='public'&&!entity.custody?.container_id&&
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
  const selected=facts.selected_inspection?.entity;
  if(selected?.summary_facts){lines.push((selected.name||'所选物件')+'最近读回：'+entitySummary(selected));lines.push(...factLines(selected.summary_facts).slice(0,8));}
  const clock=facts.selected_inspection?.context?.observation_clock;
  if(selected)lines.push(Number.isInteger(clock?.sequence)?'物件详情读取步 '+clock.sequence:'物件详情来自独立读取。');
  if(Number.isInteger(facts.clock?.sequence))lines.push('地图读取步 '+facts.clock.sequence+'；物件详情另行读取，后续变化请看地图。');
  return lines;
}
