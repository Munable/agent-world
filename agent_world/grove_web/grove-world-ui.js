// All presentation is data. This module never interprets host text as markup,
// loads a model asset, or turns a descriptor into an executable callback.
const materialLabels={wood:'木',stone:'石',cloth:'布',metal:'金属',water:'水',leather:'皮革'};
const slotLabels={body:'本体',contained:'容纳',surface:'表面',absorbed:'吸附'};
const parentLabels={ground:'地面',held:'持有',contained:'容纳于',attached:'绑定于'};
const finite=value=>Number.isFinite(value)?value:null;
export const materialLabel=value=>materialLabels[value]||String(value||'未标明');
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
export function relationLabel(entity){
  const parent=entity?.relations?.parent;
  if(!parent||typeof parent!=='object')return '';
  const kind=parent.relation;
  const target=parent.id;
  return (parentLabels[kind]||kind||'物理关系')+(target?' '+target:'');
}
export function entitySummary(entity){
  const state=entity?.state||{},lots=matterLots(entity);
  const quantities=lots.length?lots.map(lot=>slotLabel(lot.slot)+' '+materialLabel(lot.material)+' '+lot.quantity+' 单位').join('，'):materialLabel(entity?.material);
  const wetness=entity?.simulation?.wetness??state.wetness,burning=(entity?.simulation?.burning_lots||[]).length>0||lots.some(lot=>lot.burning);
  return [quantities,lifecycleLabel(entity),burning?'有残余材料正在燃烧':null,Number.isFinite(wetness)&&wetness>0?'湿度 '+wetness:null,relationLabel(entity)].filter(Boolean).join(' · ');
}
export function lifecycleLabel(entity){
  if(entity?.lifecycle==='consumed'||entity?.state?.destroyed)return '材料已耗尽';
  if(entity?.lifecycle==='broken'||entity?.state?.broken)return '结构已破损，残材仍在';
  return '';
}
export function simulationStats(entity){
  const sim=entity?.simulation||{},rows=[];
  for(const [key,label] of [['local','本件载荷'],['total','总载荷'],['supported','承载量'],['limit','承载上限'],['buoyancy','浮力']])if(finite(sim[key])!==null)rows.push([label,sim[key]]);
  if(finite(sim.integrity)!==null)rows.push(['完整度',sim.integrity+(finite(sim.max_integrity)!==null?' / '+sim.max_integrity:'')]);
  if(finite(sim.overload)!==null)rows.push(['过载量',sim.overload]);
  if(typeof sim.can_float==='boolean')rows.push(['下水资格',sim.can_float?'满足浮力条件':'未满足浮力条件']);
  if(entity?.relations)rows.push(['物理关系',entity.relations.parent?relationLabel(entity):Number.isFinite(entity.x)&&Number.isFinite(entity.y)?'地面 '+entity.x+'，'+entity.y:'无父物件']);
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
    glyph:entityGlyph(entity)};
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
  return '预计承载 '+previewNumber(summary?.after?.supported)+(applicable?' / '+previewNumber(summary?.after?.limit):'')+' 载荷单位'+
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
  return [candidate?.name||candidate?.id||'物件',candidate?.summary,candidatePreviewSummary(candidate)].filter(Boolean).join(' · ');
}
export function candidatePreviewLines(candidate){
  const preview=candidate?.preview;if(!preview||typeof preview!=='object')return [];
  const physics=preview.physics||{},before=physics.before||{},after=physics.after||{},lines=[];
  lines.push(preview.available===false?'当前不能提交：'+(preview.reason||'操作条件不满足。'):preview.available===true?'当前操作资格允许提交；是否过载单独显示，危险实验仍由你选择。':'当前操作资格尚未读取。');
  if(physics.known!==true)lines.push('组合载荷预估未知：'+(physics.reason||'没有取得足够的可见物理状态。'));
  else{
    lines.push('目标 '+(physics.subject?.name||physics.subject?.id||preview.target_id||'物件')+'：本件载荷 '+previewNumber(before.local)+' → '+previewNumber(after.local)+'，总载荷 '+previewNumber(before.total)+' → '+previewNumber(after.total)+' 载荷单位。');
    lines.push('承载量 '+previewNumber(before.supported)+' → '+previewNumber(after.supported)+' 载荷单位；'+(physics.support_limit_applicable===false?'未设结构承载上限。':'承载上限 '+previewNumber(before.limit)+' → '+previewNumber(after.limit)+'；操作后余量 '+previewNumber(physics.support_margin)+' 载荷单位。'));
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
