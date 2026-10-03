export function pendingPresentation(pending){
  const rejected=!!pending?.rejection,hasDraft=rejected&&pending.route==='camp-intent';
  return {
    rejected,hasDraft,
    restoreLabel:hasDraft?'恢复提案草稿':'清除已拒绝操作并查看新局面',
    detail:rejected
      ? (pending.rejection.message||'这次行动被世界拒绝。')+' 本次明确未提交。'+(hasDraft?'恢复提案和私密补充草稿后可以调整；这不会撤销任何已成功行动。':'清除只移除这条拒绝记录，不撤销任何已成功行动；随后读取最新局面。')
      : '结果尚未确定。核对会复用同一个操作，不会创建第二次行动。',
  };
}

function publicSnapshot(snapshot){
  const route=snapshot?.route?.value,inventory=snapshot?.inventory?.value;
  if(!Number.isFinite(snapshot?.now) || !route || !inventory || !['blocked','foot','cargo'].includes(route.passage))return null;
  if(!['stability','exposure'].every(key=>Number.isFinite(route[key])) || !['wood','rope','stone'].every(key=>Number.isFinite(inventory[key])))return null;
  return {now:snapshot.now,route:{passage:route.passage,stability:route.stability,exposure:route.exposure},inventory:{wood:inventory.wood,rope:inventory.rope,stone:inventory.stone}};
}

// This is a browser observation checkpoint, never a world event or input to a
// world action. Only these public fields may enter the local return record.
export class CampReturnHistory {
  constructor(storage,world,publicKey){
    this.storage=storage;this.key='river-camp-last-seen:'+encodeURIComponent(world)+':'+publicKey;this.previous=null;
    try{const raw=JSON.parse(storage.getItem(this.key)||'null');if(raw)this.previous=publicSnapshot({now:raw.now,route:{value:raw.route},inventory:{value:raw.inventory}});}catch{}
  }
  observe(snapshot){
    const current=publicSnapshot(snapshot);if(!current)return null;
    let saved=true;try{this.storage.setItem(this.key,JSON.stringify(current));}catch{saved=false;}
    if(!this.previous)return null;
    const changes=[];
    for(const [scope,fields] of [['route',['passage','stability','exposure']],['inventory',['wood','rope','stone']]])for(const key of fields){const before=this.previous[scope][key],after=current[scope][key];if(before!==after)changes.push({key,before,after});}
    return {previous:this.previous,current,changes,saved};
  }
}

const stages={bridge:'恢复旧桥通行',supply:'补给与营地准备',flood:'涨水后的应对',repaired:'修复后的营地'};
const goalLabels={supply:'参与补给',build:'建设仓库',watch:'观察水情'};
const actionLabels={choose_goal:'选择个人目标',supply:'带回一趟补给',build_storehouse:'建设仓库',observe_water:'观察水情',salvage_crate:'领取应急箱',repair:'修复路线',propose:'提交提案',judge:'裁定提案',accept:'接受方案并执行',explore:'探索线索'};
export function intentStatus(status,bridgeClosed=false){
  if(bridgeClosed&&status==='pending')return '待决记录（桥梁阶段已关闭）';
  if(bridgeClosed&&status==='approved')return '获准旧方案（已锁定）';
  return {pending:'待裁判回应',approved:'方案已获准，等待本人接受',declined:'未获准',executed:'已执行'}[status]||'状态未提供';
}
export function campaignPresentation(snapshot,roleId){
  const campaign=snapshot?.campaign;if(!campaign)return null;
  const personal=(campaign.personals||[]).find(item=>item.role_id===roleId);
  const bridgeLocked=campaign.stage!=='bridge';
  const route=snapshot.route?.value,routeReady=(snapshot.route_goal_reached??(route?.passage==='cargo'&&route.stability>=2&&route.exposure<=1))===true;
  const bridgeReady=campaign.stage==='bridge'&&routeReady,goalReached=campaign.goal?.reached===true;
  let next=bridgeReady?'货运桥已通；选择个人目标并领取第一趟补给，第二趟之前建仓可防损失。':campaign.next||'';
  if(goalReached)next+=(next?' ':'')+'这一有限章节已结束；可看历史和继续探索，后续章节尚未开放。';
  const ownApproved=(snapshot.intents||[]).filter(item=>item.value.owner===roleId&&item.value.status==='approved');
  const waiting=[];
  const pending=(snapshot.intents||[]).filter(item=>item.value.status==='pending');
  if(pending.length)waiting.push(bridgeLocked?'桥梁提案阶段已关闭；'+pending.length+' 份旧待决记录保留为历史，不再等待新裁定。':(snapshot.authority?.value?.judges?.length?'等待当前获准裁判回应':'当前没有获准裁判，需要受权主持者安排裁判')+'：'+pending.length+' 份提案。系统不自动运行裁判模型。');
  if(ownApproved.length)waiting.push(bridgeLocked?'你的旧桥方案尚未接受；补给已开始，旧桥方案当前不能再执行。':'你有 '+ownApproved.length+' 份获准方案，下一步由你本人接受代价并执行。');
  const otherApproved=(snapshot.intents||[]).filter(item=>item.value.owner!==roleId&&item.value.status==='approved');
  if(otherApproved.length)waiting.push(bridgeLocked?otherApproved.length+' 份其他获准旧桥方案尚未接受；阶段已关闭，不能再执行。':otherApproved.length+' 份获准方案等待原提案者本人接受。');
  return {
    stage:bridgeReady?'货运桥已通，准备补给':stages[campaign.stage]||'营地当前阶段',goal:campaign.goal?.text||'',goalReached,
    targets:campaign.goal?.targets||[],personal,personalLabel:personal?.label||goalLabels[personal?.choice]||'尚未选择个人目标',
    bridgeLocked,waiting,next,actions:campaign.available_actions||[],
    activity:[...(campaign.activity||[])].sort((a,b)=>b.at-a.at).slice(0,8),
  };
}

export function campaignActivity(entry){
  const action=String(entry.action||'').split('.').at(-1),details=entry.details,parts=[];
  if(typeof details==='string')parts.push(details);
  else if(details&&typeof details==='object'){
    if(goalLabels[details.choice])parts.push('目标：'+goalLabels[details.choice]);
    const shipment=details.shipment_id||details.shipment;if(shipment==='timber')parts.push('木材补给');if(shipment==='quarry')parts.push('石料补给');
    if(details.plan==='quick')parts.push('临时修复');if(details.plan==='anchored')parts.push('锚固修复');
    for(const [key,label] of [['cost','消耗'],['loss','损失'],['received','收到材料']])if(details[key]&&typeof details[key]==='object'){const values=Object.entries({wood:'木材',rope:'绳子',stone:'石料'}).filter(([name])=>Number.isFinite(details[key][name])).map(([name,label])=>label+' '+details[key][name]);if(values.length)parts.push(label+'：'+values.join(' / '));}
    const route=details.route||details.effects;if(route&&['blocked','foot','cargo'].includes(route.passage)){const labels={blocked:'阻断',foot:'步行',cargo:'货物'};parts.push('已落地路线：'+labels[route.passage]+' / 稳定 '+route.stability+' / 暴露 '+route.exposure);}
    if(details.story_event==='second-shipment high water')parts.push('第二趟补给触发剧情涨水；不是现实天气或时间流逝');
    if(typeof details.finding==='string'){
      const knownFinding='The second fixed shipment triggers story high water: it blocks the route and loses 2 wood/1 rope unless a storehouse already exists. This is not real weather or a day passing.';
      const finding=entry.action==='campaign.observe_water'&&details.finding===knownFinding?'第二趟固定补给会触发剧情涨水并阻断路线；已有仓库可避免材料损失，否则会损失木材 2 / 绳子 1。这不是现实天气或一天过去。':details.finding;
      parts.push('观测线索：'+finding);
    }
    if(action==='salvage_crate')parts.push('一次固定外部应急补给，不退回旧桥材料');
  }
  return {label:actionLabels[action]||(action==='flood'?'涨水后果已落地':'已提交世界行动'),detail:parts.join('；')};
}
