import {createKey,validateKey,savedKey} from '/static/commons-client.js';
import {DndClient} from './dnd-client.js';

const $=id=>document.getElementById(id),client=new DndClient();
let key=null,profile=null,snapshot=null,busy=false,generation=0,timer=null,agentClient=null,lastRead=null;
let historyItems=new Map(),historyCursor=0,historyLastSeq=0,historyHasMore=false;
let knownNames=new Map();
const abilityLabels={strength:'力量',dexterity:'敏捷',constitution:'体质',intelligence:'智力',wisdom:'感知',charisma:'魅力',str:'力量',dex:'敏捷',con:'体质',int:'智力',wis:'感知',cha:'魅力'};
const effectLabels={none:'不改变场景状态',open_bandit_door:'打开守卫门',secure_gap:'架设安全越隙路径',bandits_allow_passage:'守卫允许通过',reveal_store:'发现储藏室线索',damage_1d6:'受到 1d6 伤害'};
const statusLabels={pending:'等待裁决方案',offered:'等待本人接受风险',executed:'已结算',withdrawn:'已撤回',declined:'未获准',active:'行动中',alive:'可行动',unconscious:'倒地',dying:'倒地',dead:'死亡',stable:'已稳定',incapacitated:'无法行动'};
const actionLabels={move:'移动',search:'搜索',unlock:'开锁',force:'破门',set_rope:'架设绳索',jump:'跳跃',negotiate:'交涉',attack:'攻击',join_combat:'加入战斗',dodge:'闪避',help:'协助',end_turn:'结束回合',retreat:'撤退',heal:'恢复生命',rest:'休息',take:'拿取',transfer:'交付物品',deliver:'归还遗物',stabilize:'稳定伤势',death_save:'死亡豁免',say:'说话',propose:'提出行动',rule:'裁决方案',commit:'接受并结算',withdraw:'撤回行动',join:'进入世界'};
const text=(tag,value,cls='')=>{const node=document.createElement(tag);node.textContent=value??'';if(cls)node.className=cls;return node;};
const array=value=>Array.isArray(value)?value:value&&typeof value==='object'?Object.entries(value).map(([id,v])=>typeof v==='object'?{id,...v}:{id,name:id,value:v}):[];
const time=value=>Number.isFinite(value)?new Date(value*1000).toLocaleString():'时间未提供';
function announce(message){$('message').textContent=message;$('message').hidden=!message;}
function download(name,value){const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)+'\n'],{type:'application/json'}));const anchor=document.createElement('a');anchor.href=url;anchor.download=name;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function saveDrafts(){if(!profile)return;client.saveDraft('dnd-intent',$('intent').value);client.saveDraft('dnd-say',$('say-text').value);}
function restoreDrafts(){if(!profile)return;$('intent').value=client.draft('dnd-intent');$('say-text').value=client.draft('dnd-say');}
function current(){return !!profile&&!!client.token;}
function showError(error){
  if(error.cancelled||error.staleAuth)return;
  $('error').textContent=error.code==='StateConflict'?'局面已被另一项行动改变，这次行动没有提交。请解除被拒绝的提交，读取最新世界，再重新选择。':(error.message||String(error));$('error').hidden=false;
  if(error.status===401&&profile){client.token=null;$('reconnect').hidden=false;$('connection-state').textContent='连接已到期，请同钥重连。';$('freshness').textContent='以下是上次读取的局面，重连后才能继续行动。';}
  controls();
}
function pending(){
  let value=null;try{value=profile&&client.pending();}catch(error){showError(error);}
  $('pending').hidden=!value;if(!value)return;
  const rejected=!!value.rejection;$('retry').hidden=rejected;$('restore').hidden=!rejected;
  $('pending-detail').textContent=rejected?'这次请求被明确拒绝，没有提交。取回草稿或解除这次提交后，读取新局面再行动。':'尚未收到完整回执。可能已经提交，请用同一个操作核对；不要重新发一份相同行动。';
  $('restore').textContent=value.route?'取回草稿，重新选择':'解除被拒绝的提交';
}
function controls(){
  $('cancel').hidden=!busy||$('entry').hidden;
  document.querySelectorAll('button').forEach(button=>{
    const staysAvailable=['leave','cancel'].includes(button.id)||!!button.closest('#risk-dialog');
    const offline=profile&&!client.token&&button.closest('#world')&&!['reconnect','backup','leave','restore','copy-prompt'].includes(button.id);
    button.disabled=button.dataset.unavailable==='true'||(busy&&!staysAvailable)||!!offline;
  });
  $('enter').textContent=busy&&!$('entry').hidden?'正在连接…':'进入 / 接续冒险';
}
async function run(fn){if(busy)return;busy=true;$('error').hidden=true;controls();try{await fn();}catch(error){showError(error);}finally{busy=false;controls();pending();}}
async function mutate(name,args,context={}){const session=generation;return client.mutate(name,args,context,()=>session===generation&&current());}
async function enter(candidate,name='',classId='fighter'){
  announce('');
  const session=++generation,active=()=>{if(session!==generation){const error=new Error('连接已取消。');error.cancelled=true;throw error;}};
  key=await validateKey(await candidate);active();await savedKey(key);active();
  const result=await client.authenticate(key);active();profile=result.role_profile;
  $('entry').hidden=true;$('world').hidden=false;$('reconnect').hidden=true;
  $('connection-state').textContent='本页权限到期 '+time(client.expiresAt);$('who').textContent=profile.display_name;
  historyItems=new Map();knownNames=new Map();historyCursor=0;historyLastSeq=0;historyHasMore=false;
  const bootstrap=await client.request('/v1/bootstrap');active();
  const entryView=bootstrap.world_entry_state?.view;
  if(!entryView||typeof entryView.joined!=='boolean'||typeof entryView.can_rule!=='boolean')throw new Error('世界入口信息不完整，请读取最新状态后重连。');
  if(!client.pending()&&!entryView.joined&&!entryView.can_rule){
    await mutate('dnd.join',{name:name||profile.display_name||'灯塔访客',class_id:classId});active();
  }
  active();restoreDrafts();await refresh();active();await showPage();schedule();
}
function schedule(){clearTimeout(timer);timer=setTimeout(async()=>{if(current()&&!document.hidden&&!busy&&!$('risk-dialog').open){try{await refresh();}catch(error){showError(error);}}if(profile)schedule();},6000);}
async function refresh(){const session=generation,value=await client.query('dnd.look');if(session!==generation||!current())return;snapshot=value;lastRead=Date.now();render();}
function actionArgs(args={}){return {...args,...(snapshot?.character?.version!=null?{expected_character_version:snapshot.character.version}:{}),...(snapshot?.scene?.version!=null?{expected_scene_version:snapshot.scene.version}:{})};}
function button(label,fn,cls='secondary'){const node=text('button',label,cls);node.type='button';node.addEventListener('click',()=>run(fn));return node;}
function riskText(value){if(typeof value==='string')return value;if(!value)return '';if(Array.isArray(value))return value.map(riskText).join('\n');return Object.entries(value).map(([k,v])=>({success:'成功',failure:'失败',cost:'代价',check:'检定',dc:'难度',description:'说明'}[k]||k)+'：'+(typeof v==='object'?riskText(v):v)).join('\n');}
function serverTerms(ruling){
  const terms=ruling.terms,check=terms?.check,lines=[];
  if(check){lines.push((abilityLabels[check.ability]||check.ability)+'检定 · 修正 '+(check.modifier>=0?'+':'')+check.modifier+' · DC '+check.dc+(check.advantage?' · 优势':''));}
  else lines.push((abilityLabels[ruling.ability]||ruling.ability)+'检定 · DC '+ruling.dc+(ruling.advantage?' · 优势':''));
  if(terms?.success?.description)lines.push('成功：'+terms.success.description);
  if(Array.isArray(terms?.success?.cost_items)){const names={rope:'麻绳'};lines.push('成功时消耗或留置的物品：'+(terms.success.cost_items.length?terms.success.cost_items.map(item=>names[item]||item).join('、'):'无'));}
  if(terms?.failure?.description)lines.push('失败：'+terms.failure.description);
  if(terms?.failure?.damage)lines.push('失败伤害：'+terms.failure.damage+'（服务器掷骰）');
  if(!terms&&ruling.server_terms)lines.push(ruling.server_terms);
  lines.push('接受后才掷骰；结算结果不能事后撤回。');
  return lines.join('\n');
}
function rulingSummary(ruling){return '服务器固定条款\n'+serverTerms(ruling)+(ruling.risk?'\n\n裁决者的补充说明\n'+ruling.risk:'');}
async function acceptRisk(title,description){
  if(!$('risk-dialog').showModal)throw new Error('此浏览器无法显示风险确认，请使用新版浏览器。');
  $('risk-title').textContent=title;$('risk-description').replaceChildren(text('p',description));$('risk-description').style.whiteSpace='pre-wrap';$('risk-dialog').returnValue='';$('risk-dialog').showModal();
  return new Promise(resolve=>{$('risk-dialog').addEventListener('close',()=>resolve($('risk-dialog').returnValue==='confirm'),{once:true});});
}
async function performAction(action){
  const session=generation,args=actionArgs(action.args||{action:action.id});
  if(action.risk&&!await acceptRisk(action.label||'这次行动',riskText(action.risk))){announce('已取消，行动没有提交。');return;}
  if(session!==generation||!current())return;
  const result=await mutate('dnd.act',args);if(session!==generation)return;showResult(result);await refresh();
}
function appendRolls(parent,result){
  if(result?.check){appendRolls(parent,result.check);return;}
  if(result?.attack){const attack=result.attack,row=text('div','','dice-line');for(const roll of attack.rolls||[])row.append(text('span',roll,'die'));row.append(text('span','攻击总计 '+attack.total+' · '+(attack.hit?'命中，伤害 '+attack.damage:'未命中')));parent.append(row);if(attack.damage_rolls?.length)parent.append(text('p','伤害骰：'+attack.damage_rolls.join('、')+(attack.sneak_attack_rolls?.length?' · 偷袭骰：'+attack.sneak_attack_rolls.join('、'):''),'small'));return;}
  if(result&&typeof result==='object'&&result.natural!=null){const row=text('div','','dice-line');for(const roll of result.rolls||[result.natural])row.append(text('span',roll,'die'));row.append(text('span','修正 '+(result.modifier>=0?'+':'')+result.modifier+' · 总计 '+result.total+' / DC '+result.dc+' · '+(result.success?'成功':'失败')));parent.append(row);return;}
  const rolls=result?.rolls||result?.dice||result?.random_draws||result?.roll||result?.check;
  for(const roll of Array.isArray(rolls)?rolls:rolls?[rolls]:[]){
    const row=text('div','','dice-line');
    if(typeof roll==='number')row.append(text('span',roll,'die'));
    else if(roll&&typeof roll==='object'){
      const die=roll.d20??roll.roll??roll.value??roll.result??roll.total;
      if(typeof die==='number')row.append(text('span',die,'die'));
      const pieces=[];if(roll.ability)pieces.push(abilityLabels[roll.ability]||roll.ability);if(roll.label)pieces.push(roll.label);if(roll.modifier!=null)pieces.push('修正 '+(roll.modifier>=0?'+':'')+roll.modifier);if(roll.total!=null)pieces.push('总计 '+roll.total);if(roll.dc!=null)pieces.push('DC '+roll.dc);if(typeof roll.success==='boolean')pieces.push(roll.success?'成功':'失败');row.append(text('span',pieces.join(' · ')));
    }
    if(row.childNodes.length)parent.append(row);
  }
}
function showResult(result){
  $('last-result-card').hidden=false;const host=$('last-result');host.replaceChildren();
  const message=result.message||result.description||result.text||result.summary||result.outcome;
  host.append(text('p',typeof message==='string'?message:'这次行动已经提交，现场与记录已更新。'));appendRolls(host,result);
  if(result.damage!=null)host.append(text('p','伤害：'+result.damage));
  if(result.healed!=null)host.append(text('p','恢复生命：'+result.healed));
  if(result.ruling?.risk)host.append(text('p',result.ruling.risk,'small'));
  appendConsequences(host,result);
}
function appendConsequences(parent,result){
  if(result.death_save){const save=result.death_save,row=text('div','','dice-line');row.append(text('span',save.natural,'die'),text('span','死亡豁免 · '+(statusLabels[save.status]||save.status)+' · 成功 '+(save.death_saves?.successes||0)+' / 失败 '+(save.death_saves?.failures||0)));parent.append(row);}
  if(result.healing_rolls?.length){const row=text('div','','dice-line');row.append(text('small','治疗骰'));for(const roll of result.healing_rolls)row.append(text('span',roll,'die'));if(result.healing!=null)row.append(text('span','恢复 '+result.healing+' 点（不超过生命上限）'));parent.append(row);}
  for(const attack of result.npc_turns||[]){const row=text('div','','dice-line');row.append(text('span',participant(attack.attacker)+' → '+participant(attack.target)));for(const roll of attack.rolls||[])row.append(text('span',roll,'die'));row.append(text('span','攻击总计 '+attack.total+' · '+(attack.hit?'命中，伤害 '+attack.damage:'未命中')+' · 对方剩余 HP '+attack.target_hp));parent.append(row);}
  if(result.real_time_elapsed===false)parent.append(text('p','这里推进的是故事时间，现实中没有等候这些小时。','small'));
}
function renderCharacter(character){
  if(!character){const referee=snapshot?.authority?.can_rule;$('character-name').textContent=referee?'裁决观察者':'尚未创建人物';$('character-class').textContent=referee?'你的权限用于提供有限方案，不控制其他人物。':'先核对上次提交，再进入世界。';for(const id of ['character-stats','character-abilities','inventory','character-features'])$(id).replaceChildren();return;}
  $('who').textContent=character.name;$('character-name').textContent=character.name;
  $('character-class').textContent=(character.class_id==='rogue'?'游荡者':'战士')+' · '+(character.level||1)+' 级 · '+(statusLabels[character.status]||character.status||'可行动');
  $('character-stats').replaceChildren();for(const [value,label,cls] of [[character.hp+' / '+character.max_hp,'生命值','hp-stat'+(character.hp<=0?' danger':'')],[character.ac,'护甲等级 AC',''],[character.speed??30,'移动速度（尺）','']]){const stat=text('div','','stat '+cls);stat.append(text('strong',value),text('small',label));$('character-stats').append(stat);}
  $('character-abilities').replaceChildren();for(const [id,value] of Object.entries(character.abilities||{})){const node=text('span');node.append(text('span',abilityLabels[id]||id),text('strong',typeof value==='object'?value.score:value));$('character-abilities').append(node);}
  const inventory=text('ul','','inventory-list');for(const equipment of character.equipment||[])inventory.append(text('li',typeof equipment==='string'?equipment:equipment.name));for(const item of array(character.inventory)){const itemLabels={longsword:'长剑',shortsword:'短剑',shield:'盾牌',chain_mail:'链甲',leather_armor:'皮甲',rope:'麻绳',torch:'火把',thieves_tools:'盗贼工具',healing_potion:'治疗药水',relic:'灯塔遗物',beacon_seal:'航标印',ledger_page:'旧账册页',gold:'金币',rations:'口粮'};const label=typeof item==='string'?itemLabels[item]||item:item.name||item.label||itemLabels[item.id]||item.id;const count=item.quantity??item.count??(typeof item.value==='number'?item.value:null);if(count!=null&&count<=0)continue;inventory.append(text('li',label+(count!=null?' × '+count:'')));}if(!inventory.children.length)inventory.append(text('li','没有随身物品。'));$('inventory').replaceChildren(inventory);
  $('character-features').replaceChildren();for(const feature of array(character.features)){if(typeof feature==='string')$('character-features').append(text('p',feature,'small'));else $('character-features').append(text('p',feature.name||feature.description||feature.id,'small'));}
  if(character.hit_dice_remaining!=null)$('character-features').append(text('p','剩余生命骰：'+character.hit_dice_remaining+' 枚 d'+character.hit_die,'small'));
  if(character.class_id==='fighter')$('character-features').append(text('p','回气：'+(character.second_wind_available?'可使用':'已消耗，休息后恢复'),'small'));
  if(!character.features)$('character-features').append(text('p',character.class_id==='rogue'?'职业能力：潜行与盗贼工具专精。偷袭每回合最多一次，需满足优势或盟友近身等规则条件。':'职业能力：决斗战斗风格。回气消耗一次职业能力，短休后恢复。','small'));
  if(character.weapon)$('character-features').append(text('p',(character.weapon.name||'武器')+' · 攻击 '+(character.weapon.attack_bonus>=0?'+':'')+character.weapon.attack_bonus+' · 伤害 1d'+character.weapon.damage_die+'+'+character.weapon.damage_bonus,'small'));
  if(character.death_saves)$('character-features').append(text('p','死亡豁免：成功 '+(character.death_saves.successes||0)+' / 失败 '+(character.death_saves.failures||0),'small'));
}
function renderScene(){
  const scene=snapshot.scene||{};$('scene-title').textContent=scene.title||scene.name||'世界入口';$('scene-description').textContent=scene.description||'请读取最新现场。';$('scene-state').textContent=scene.version!=null?'场景已保存':'';
  $('scene-objects').replaceChildren();for(const object of array(scene.objects)){const chip=text('span',object.name||object.title||object.id,'object-chip');if(object.description)chip.append(text('small',object.description));$('scene-objects').append(chip);}
  $('scene-npcs').replaceChildren();for(const npc of array(scene.npcs)){const item=text('p',(npc.name||npc.id)+'：'+(npc.description||npc.status||''));if(npc.hp!=null)item.append(text('small',' · HP '+npc.hp));$('scene-npcs').append(item);}
  $('nearby').replaceChildren();const others=array(snapshot.characters).filter(item=>item.role_id!==profile.role_id&&(item.scene_id==null||item.scene_id===scene.id));if(others.length)$('nearby').append(text('p','同一房间：'+others.map(item=>(item.name||'另一位冒险者')+(item.status==='dead'?'（死亡）':item.hp<=0?'（倒地）':'')).join('、'),'small'));
  $('available-actions').replaceChildren();for(const action of snapshot.available_actions||[]){const tile=button(action.label||actionLabels[action.id]||action.id,()=>performAction(action),'action-tile'+(action.risk?' risky':''));if(action.risk)tile.append(text('small',riskText(action.risk)));if(action.disabled||action.available===false)tile.dataset.unavailable='true';$('available-actions').append(tile);}
  $('action-note').textContent=snapshot.available_actions?.length?'点选行动会使用现有规则结算。有风险的行动会先展示后果。':'当前没有可用动作。读取最新局面，或查看人物与战斗状态。';
  $('freshness').textContent='最后读取 '+new Date(lastRead||Date.now()).toLocaleTimeString()+' · 页面可见时每 6 秒读取已保存的世界';
}
function renderMap(){
  $('map').replaceChildren();for(const room of snapshot.map||[]){const node=text('li',room.visited||room.current?room.title||room.name||room.id:'未探索的房间',room.current?'current':room.visited?'':'unknown');if(room.current)node.append(text('span','你在这里','tag'));$('map').append(node);}
}
function renderProgress(){
  const host=$('progress');host.replaceChildren();const progress=snapshot.progress;
  if(typeof progress==='string')host.append(text('p',progress));
  else if(progress){host.append(text('p',progress.seal_returned?'航标印已归还，码头的守灯人收到了原物。':progress.seal_holder?'航标印现在由 '+participant(progress.seal_holder)+' 携带。':'航标印还没有归还。它只有一份，取走后不会刷新。'));if(progress.returned_by)host.append(text('p','归还者：'+participant(progress.returned_by),'small'));if(progress.returned_at)host.append(text('p','归还时间：'+time(progress.returned_at),'small'));}
  if(!host.childNodes.length)host.append(text('p','深入灯塔，了解遗物与这里的人。'));
}
function renderCombat(){
  const combat=snapshot.combat;$('combat-card').hidden=!combat?.active;if(!combat?.active)return;
  $('combat-round').textContent='第 '+(combat.round||1)+' 回合';const host=$('combat');host.replaceChildren();
  const turn=typeof combat.turn==='object'?combat.turn.role_id||combat.turn.id:combat.turn;
  const ownTurn=turn===profile.role_id;host.append(text('p',ownTurn?'轮到你行动。':turn?'当前轮到 '+participant(turn)+'。':'等待战斗顺序确定。'));
  const list=text('ul','','combatants');for(const entry of array(combat.order)){const id=typeof entry==='string'?entry:entry.role_id||entry.id;const row=text('li','',id===turn?'current':'');row.append(text('span',typeof entry==='object'?entry.name||participant(id):participant(id)));if(entry.initiative!=null)row.append(text('small','先攻 '+entry.initiative));list.append(row);}host.append(list);
}
function participant(id){if(id===profile?.role_id)return '你';const character=array(snapshot?.characters).find(item=>item.role_id===id);const npc=array(snapshot?.scene?.npcs).find(item=>item.id===id);return character?.name||npc?.name||knownNames.get(id)||(id?'参与者 '+String(id).slice(-6):'世界规则');}
function renderIntents(){
  const intents=snapshot.pending_intents||[];$('intents-card').hidden=!intents.length;$('intents').replaceChildren();
  for(const intent of intents){const item=text('article','','record'),own=intent.owner===profile.role_id;item.append(text('span',statusLabels[intent.status]||intent.status,'tag'),text('small',own?'你的意图':participant(intent.owner)),text('p',intent.text));
    if(intent.ruling){const ruling=intent.ruling,terms=text('div','','intent-offer');terms.append(text('strong','服务器固定条款'),text('p',serverTerms(ruling)));if(ruling.risk)terms.append(text('p','裁决者补充说明：'+ruling.risk,'small'));if(ruling.reason)terms.append(text('p','裁决理由：'+ruling.reason,'small'));item.append(terms);}
    if(own&&intent.status==='offered'){
      const hasTerms=!!(intent.ruling?.terms||intent.ruling?.server_terms);
      if(!hasTerms)item.append(text('p','这份方案缺少服务器固定条款，请请求裁决者重新提供方案。','small'));
      const commitButton=button('接受风险，掷骰结算',async()=>{const session=generation;if(!hasTerms)throw new Error('缺少服务器固定条款，无法接受这份方案。');if(!await acceptRisk('接受这一行动方案',rulingSummary(intent.ruling))){announce('已取消，行动方案没有执行。');return;}if(session!==generation)return;showResult(await mutate('dnd.commit',{intent_id:intent.intent_id}));await refresh();});commitButton.dataset.unavailable=String(!hasTerms);item.append(commitButton);
    }
    if(own&&['pending','offered'].includes(intent.status))item.append(button('撤回这个意图',async()=>{await mutate('dnd.withdraw',{intent_id:intent.intent_id});await refresh();},'text-button'));
    $('intents').append(item);
  }
  $('referee').hidden=!snapshot.authority?.can_rule;const selected=$('ruling-intent').value;$('ruling-intent').replaceChildren();for(const intent of intents.filter(item=>item.status==='pending')){const option=text('option',participant(intent.owner)+'：'+intent.text.slice(0,90));option.value=intent.intent_id;$('ruling-intent').append(option);}if([...$('ruling-intent').options].some(option=>option.value===selected))$('ruling-intent').value=selected;
  const gmOption=$('guide-chapter').querySelector('option[value=gm]');if(snapshot.authority?.can_rule&&!gmOption){const option=text('option','获准裁决者指南');option.value='gm';$('guide-chapter').append(option);}else if(!snapshot.authority?.can_rule&&gmOption)gmOption.remove();
  const submit=$('ruling').querySelector('button[type=submit]');submit.dataset.unavailable=String(!$('ruling-intent').options.length);
}
function render(){if(!snapshot)return;for(const character of snapshot.characters||[])knownNames.set(character.role_id,character.name);for(const npc of snapshot.scene?.npcs||[])knownNames.set(npc.id,npc.name);$('expression-card').hidden=!snapshot.character;renderCharacter(snapshot.character);renderScene();renderMap();renderProgress();renderCombat();renderIntents();if(Array.isArray(snapshot.history))mergeHistory(snapshot.history);pending();controls();}
function historyKey(entry){return String(entry.seq??entry.sequence??entry.event_id??entry.id??'');}
function mergeHistory(rows){for(const row of rows){const id=historyKey(row);if(id)historyItems.set(id,row);if(Number.isInteger(row.seq))historyLastSeq=Math.max(historyLastSeq,row.seq);}historyHasMore=historyCursor<historyLastSeq;renderHistory();}
function renderHistory(){
  const host=$('history');host.replaceChildren();const rows=[...historyItems.values()].sort((a,b)=>(b.seq??b.sequence??b.at??b.created_at??0)-(a.seq??a.sequence??a.at??a.created_at??0));
  for(const row of rows){const item=text('article','','record'),body=row.value||row.payload||row;const actor=row.actor_name||body.actor_name||participant(row.actor||body.actor_role_id||row.actor_role_id||body.owner);const action=body.action||row.action||row.kind;const meta=text('div','','record-meta');meta.append(text('strong',actor),text('span',actionLabels[action]||action||'世界记录'),text('span',time(row.occurred_at??row.at??body.at??row.created_at)));item.append(meta);const description=row.text||body.message||body.text||body.description||body.summary||row.message;item.append(text('p',description||'行动已提交，影响保存在世界中。'));appendRolls(item,body.result||body);appendConsequences(item,body.result||body);host.append(item);}
  if(!rows.length)host.append(text('p','还没有已提交的行动记录。'));$('history-more').hidden=!historyHasMore;$('history-status').textContent=rows.length?'已显示 '+rows.length+' 条记录'+(historyHasMore?'；完整历史还有未读取的部分，请继续补全。':'；完整历史已接续。'):'';
}
async function readHistory(){
  // The world provides an ascending sequence, not a reverse-page cursor.
  // Keep this contiguous watermark separate from the recent look projection.
  const session=generation,after=historyCursor,page=await client.query('dnd.history',{after,limit:40});if(session!==generation||!current())return;
  if(!Array.isArray(page.items)||!Number.isInteger(page.next_after)||typeof page.has_more!=='boolean'||page.items.some(row=>!Number.isInteger(row.seq)||row.seq<=after)||(page.has_more&&page.next_after<=after))throw new Error('行动记录未能正确接续，请重新读取。');
  historyCursor=Math.max(historyCursor,page.next_after);historyLastSeq=Math.max(historyLastSeq,page.last_seq||0);mergeHistory(page.items);historyHasMore=historyCursor<historyLastSeq;renderHistory();
}
async function readGuide(){const session=generation,chapter=$('guide-chapter').value,label=$('guide-chapter').selectedOptions[0]?.textContent;const page=await client.query('dnd.handbook',{chapter});if(session!==generation||!current()||chapter!==$('guide-chapter').value)return;const content=page.text||page.content||page.body||page.instructions||'';$('guide-content').replaceChildren(text('h3',page.title||label||'世界手册'),text('div',Array.isArray(content)?content.join('\n'):content));}
async function readCredentials(){
  const session=generation,page=await client.listCredentials({isCurrent:()=>session===generation&&current()});if(session!==generation)return;
  const host=$('credentials');host.replaceChildren();for(const row of page.credentials){const item=text('div','','credential'),detail=text('div');detail.append(text('strong',row.is_current?'当前网页连接':'连接 '+row.token_id.slice(-6)),text('small',(row.access_mode==='observe'?'只读观察':'读取与行动')+' · '+({active:'有效',expired:'已到期',revoked:'已撤销'}[row.status]||row.status)),text('small','签发 '+time(row.created_at)+' · 到期 '+(row.expires_at==null?'未设置':time(row.expires_at))));item.append(detail);if(!row.is_current&&row.status==='active')item.append(button('撤销此连接',async()=>{await client.revokeCredential(row.token_id,{isCurrent:()=>session===generation&&current()});announce('已撤销这一连接的访问权限。');await readCredentials();}));host.append(item);}
}
function selectedPage(){const page=location.hash.slice(1);return ['play','chronicle','guide','connections'].includes(page)?page:'play';}
async function showPage(){const page=selectedPage();for(const id of ['play','chronicle','guide','connections'])$(id).hidden=id!==page;document.querySelectorAll('.tabs a').forEach(link=>{if(link.dataset.page===page)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');});if(!current())return;if(page==='chronicle')await readHistory();if(page==='guide')await readGuide();if(page==='connections')await readCredentials();}
function leave(){
  try{saveDrafts();}catch(error){showError(error);}++generation;clearTimeout(timer);client.cancelRequests();agentClient?.cancelRequests();if($('risk-dialog').open)$('risk-dialog').close('cancel');client.token=null;profile=null;snapshot=null;historyItems.clear();knownNames.clear();
  historyCursor=0;historyLastSeq=0;$('world').hidden=true;$('entry').hidden=false;$('error').hidden=true;announce('已离开网页。人物与已提交行动仍在世界中，其他连接的权限没有被撤销。');$('last-result-card').hidden=true;controls();
}
$('enter').addEventListener('click',()=>run(()=>enter(savedKey().then(value=>value||createKey()),$('name').value.trim(),document.querySelector('input[name=class_id]:checked').value)));
$('import-key').addEventListener('change',()=>run(async()=>{const file=$('import-key').files[0];if(!file)return;try{if(file.size>8192)throw new Error('恢复文件过大，请选择之前保存的身份 JSON 文件。');let candidate;try{candidate=JSON.parse(await file.text());}catch{throw new Error('恢复文件格式不正确；已有身份没有被替换。');}await enter(candidate);}finally{$('import-key').value='';}}));
for(const id of ['intent','say-text'])$(id).addEventListener('input',()=>{try{saveDrafts();}catch(error){showError(error);}});
$('proposal').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const value=$('intent').value.trim();if(!value)return;saveDrafts();const original=client.draft('dnd-intent');showResult(await mutate('dnd.propose',{intent_id:crypto.randomUUID(),text:value,...actionArgs()},{route:'dnd-intent',draft_text:original}));restoreDrafts();await refresh();});});
$('say-form').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const value=$('say-text').value.trim();if(!value)return;saveDrafts();const original=client.draft('dnd-say');await mutate('dnd.say',{text:value},{route:'dnd-say',draft_text:original});restoreDrafts();await refresh();});});
$('ruling').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const intentId=$('ruling-intent').value;if(!intentId)throw new Error('没有等待裁决的行动意图。');await mutate('dnd.rule',{intent_id:intentId,ability:$('ruling-ability').value,dc:Number($('ruling-dc').value),advantage:false,success_effect:$('ruling-effect').value,failure_effect:$('ruling-failure').value,risk:$('ruling-risk').value.trim(),reason:$('ruling-reason').value.trim()});$('ruling-risk').value='';$('ruling-reason').value='';await refresh();});});
$('refresh').addEventListener('click',()=>run(refresh));$('reconnect').addEventListener('click',()=>run(()=>enter(key)));$('backup').addEventListener('click',()=>download('old-lighthouse-recovery.json',key));$('leave').addEventListener('click',leave);$('cancel').addEventListener('click',leave);
$('retry').addEventListener('click',()=>run(async()=>{const result=await client.retry();if(result)showResult(result);restoreDrafts();await refresh();}));$('restore').addEventListener('click',()=>run(async()=>{await client.restoreRejected();restoreDrafts();if(client.token)await refresh();}));
$('history-refresh').addEventListener('click',()=>run(async()=>{await refresh();await readHistory();}));$('history-more').addEventListener('click',()=>run(readHistory));$('guide-load').addEventListener('click',()=>run(readGuide));$('credentials-refresh').addEventListener('click',()=>run(readCredentials));
$('connect-agent').addEventListener('click',()=>run(async()=>{const session=generation,agent=new DndClient();agentClient=agent;try{await agent.authenticate(key,{accessMode:$('agent-access').value});if(session!==generation)return;download('old-lighthouse-opencode.json',{$schema:'https://opencode.ai/config.json',mcp:{agent_world:{type:'remote',url:location.origin+'/mcp',oauth:false,headers:{Authorization:'Bearer '+agent.token}}}});$('agent-status').textContent='已签发独立连接，到期 '+time(agent.expiresAt)+'。请在自己的工具中加载；下载没有启动 Agent。';await readCredentials();}finally{if(agentClient===agent)agentClient=null;}}));
$('copy-prompt').addEventListener('click',()=>run(async()=>{try{await navigator.clipboard.writeText($('agent-prompt').value);announce('已复制，可以粘贴给自己的 Agent。');}catch{$('agent-prompt').focus();$('agent-prompt').select();announce('浏览器没有允许自动复制，已选中文字，可手动复制。');}}));
window.addEventListener('hashchange',()=>{showPage().catch(showError);});window.addEventListener('pagehide',()=>{try{saveDrafts();}catch{}client.cancelRequests();agentClient?.cancelRequests();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&current()&&!busy)refresh().catch(showError);});
showPage().catch(showError);controls();
