import {createKey,validateKey,savedKey} from '/static/commons-client.js';
import {CampClient} from './camp-client.js';
import {pendingPresentation,CampReturnHistory,campaignPresentation,campaignActivity,intentStatus} from './camp-presentation.js';
const $=id=>document.getElementById(id), client=new CampClient();
let key=null, profile=null, snapshot=null, busy=false, generation=0, timer=null, agentClient=null, returnHistory=null;
const text=(tag,value,cls='')=>{const n=document.createElement(tag);n.textContent=value;if(cls)n.className=cls;return n;};
const status={pending:'待裁判回应',approved:'方案已获准，等待本人接受',declined:'未获准',executed:'已执行',active:'进行中',completed:'本人已声明完成',expired:'已到期'};
const passage={blocked:'阻断',foot:'步行',cargo:'货物'};
function download(name,value){const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)+'\n'],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function draft(){if(!profile)return;client.saveDraft('camp-intent',JSON.stringify({text:$('intent').value,note:$('private-note').value}));}
function restoreDraft(){let data={};try{data=JSON.parse(client.draft('camp-intent')||'{}');}catch{}$('intent').value=data.text||'';$('private-note').value=data.note||'';}
function clearCampaign(){$('campaign-card').hidden=true;for(const id of ['campaign-stage','campaign-result','campaign-goal','campaign-targets','personal-goal','personal-progress','campaign-next','campaign-actions','campaign-waiting','campaign-activity'])$(id).replaceChildren();}
function showError(error){if(error.cancelled||error.staleAuth)return;$('error').textContent=error.message||String(error);$('error').hidden=false;if(error.status===401){client.token=null;$('reconnect').hidden=false;$('connection').textContent='本页连接已失效；草稿和世界事实仍保留。请同钥重连。';for(const id of ['situation','intents','projects','clues','return-summary'])$(id).replaceChildren();$('return-card').hidden=true;clearCampaign();$('freshness').textContent='请重连后读取最新局面。';$('referee').hidden=true;snapshot=null;controls();}}
function pending(){const value=profile&&client.pending(),view=pendingPresentation(value);$('pending').hidden=!value;$('retry').hidden=view.rejected;$('restore').hidden=!view.rejected;$('restore').textContent=view.restoreLabel;$('pending-detail').textContent=view.detail;}
function controls(){const bridgeClosed=!!snapshot?.campaign&&snapshot.campaign.stage!=='bridge';$('cancel').hidden=!busy||$('entry').hidden;for(const id of ['intent','private-note'])$(id).disabled=bridgeClosed;document.querySelectorAll('button').forEach(n=>n.disabled=n.dataset.campaignEnabled==='false'||!!(bridgeClosed&&(n.closest('#proposal')||n.closest('#ruling')))||(busy&&!['leave','cancel'].includes(n.id))||!!(profile&&!client.token&&n.closest('#world')&&!['reconnect','backup','leave','restore'].includes(n.id)));}
async function run(fn){if(busy)return;busy=true;$('error').hidden=true;controls();try{await fn();}catch(e){showError(e);}finally{busy=false;controls();pending();}}
async function mutate(name,args,context={}){
  const session=generation;
  return client.mutate(name,args,context,()=>session===generation&&!!profile&&!!client.token);
}
async function enter(newKey,name=''){
  const session=++generation,active=()=>{if(session!==generation){const error=new Error('连接已取消。');error.cancelled=true;throw error;}};
  const candidate=await newKey;active();key=await validateKey(candidate);active();await savedKey(key);active();
  const result=await client.authenticate(key);active();profile=result.role_profile;
  returnHistory=new CampReturnHistory(client.storage,client.universe,key.public_key);
  if(name){profile=await client.request('/api/me',{display_name:name},'PATCH');active();}
  $('entry').hidden=true;$('world').hidden=false;$('reconnect').hidden=true;
  $('who').textContent=profile.display_name;
  $('connection').textContent='本页权限到期：'+new Date(client.expiresAt*1000).toLocaleString()+'。Agent 宿主是否运行由外部客户端决定。';
  await client.request('/v1/bootstrap');active();
  if(!client.pending())await mutate('camp.join',{});
  restoreDraft();await refresh();schedule();
}
function schedule(){clearTimeout(timer);timer=setTimeout(async()=>{if(profile&&client.token&&!document.hidden&&!busy){try{await refresh();}catch(e){showError(e);}}if(profile)schedule();},6000);}
async function refresh(){const session=generation;const value=await client.query('camp.inspect');if(session!==generation||!profile||!client.token)return;snapshot=value;render();}
function actionButton(label,fn){const button=text('button',label,'secondary');button.type='button';button.addEventListener('click',()=>run(fn));return button;}
function renderReturn(view){
  $('return-card').hidden=!view;$('return-summary').replaceChildren();if(!view)return;
  $('return-summary').append(text('p','本浏览器上次看到：'+new Date(view.previous.now*1000).toLocaleString()+'。这是本地观察记录，不是世界事件。','small'));
  if(!view.changes.length)$('return-summary').append(text('p','局面仍一致。'));
  else{const labels={passage:'通行',stability:'稳定度',exposure:'暴露程度',wood:'木材',rope:'绳子',stone:'石料'},list=text('ul','','return-changes');for(const change of view.changes)list.append(text('li',labels[change.key]+'：'+(change.key==='passage'?passage[change.before]:change.before)+' → '+(change.key==='passage'?passage[change.after]:change.after)));$('return-summary').append(list);}
  if(!view.saved)$('return-summary').append(text('p','此次局面未能保存到浏览器；本页仍可继续。','small'));
}
function participant(roleId){return roleId===profile.role_id?'你（'+profile.display_name+'）':roleId?'参与者 '+roleId.slice(-6):'世界规则';}
function renderCampaign(view){
  if(!view){clearCampaign();return;}$('campaign-card').hidden=false;
  $('campaign-stage').textContent=view.stage;$('campaign-result').textContent=view.goalReached?'共同目标当前达成':'共同目标尚未达成';$('campaign-goal').textContent=view.goal;
  $('campaign-targets').replaceChildren();for(const target of view.targets)$('campaign-targets').append(text('span',(target.done?'✓ ':'○ ')+target.label));
  $('personal-goal').textContent=view.personalLabel+(view.personal?.completed?' · 你的目标已达成':'');
  const contributions=view.personal?.contributions||{},progress=[];if(Number.isFinite(contributions.shipments))progress.push('已参与补给 '+contributions.shipments+' 趟');for(const [key,label] of [['built','建仓'],['observed','观测'],['repaired','修复']])if(contributions[key])progress.push('已参与'+label);$('personal-progress').textContent=progress.join(' · ');
  $('campaign-next').textContent=view.next;$('campaign-actions').replaceChildren();for(const action of view.actions){const row=text('div','','campaign-action'),button=actionButton(action.label,async()=>{await mutate(action.function,action.arguments);await refresh();});button.dataset.campaignEnabled=String(action.enabled===true);button.disabled=action.enabled!==true;row.append(button);if(action.enabled!==true&&action.blocked_reason)row.append(text('small',action.blocked_reason));$('campaign-actions').append(row);}
  $('campaign-waiting').replaceChildren();for(const message of view.waiting)$('campaign-waiting').append(text('p',message,'waiting-note'));
  $('campaign-activity').replaceChildren();for(const entry of view.activity){const item=text('article','','record'),label=campaignActivity(entry);item.append(text('small',participant(entry.actor_role_id)+' · '+(Number.isFinite(entry.at)?new Date(entry.at*1000).toLocaleString():'提交时间未提供')),text('p',label.label));if(label.detail)item.append(text('p',label.detail,'small'));$('campaign-activity').append(item);}if(!view.activity.length)$('campaign-activity').append(text('p','还没有已提交的营地行动。'));
}
function render(){
  const s=snapshot, route=s.route.value, stock=s.inventory.value,campaign=campaignPresentation(s,profile.role_id);
  renderReturn(returnHistory?.observe(s));
  renderCampaign(campaign);
  const routeReady=campaign?(s.route_goal_reached??(route.passage==='cargo'&&route.stability>=2&&route.exposure<=1)):s.goal_reached;
  $('situation').replaceChildren(text('p',campaign?(routeReady?'路线状态满足安全货运条件；共同目标见上方营地阶段。':'当前路线尚未满足安全货运条件。'):(s.goal_reached?'目标已达成：稳定且低暴露的货物通行。':'目标：恢复货物通行，稳定度至少 2，暴露程度至多 1。')));
  $('bridge-phase-note').hidden=!campaign?.bridgeLocked;
  const materials=text('div','','materials');for(const [name,label] of [['wood','木材'],['rope','绳子'],['stone','石料']])materials.append(text('span',label+' '+stock[name]));$('situation').append(materials);
  $('situation').append(text('p','通行：'+passage[route.passage]+' · 稳定度 '+route.stability+' · 暴露 '+route.exposure));
  $('freshness').textContent='最后核对 '+new Date(s.now*1000).toLocaleTimeString()+'；页面可见时每 6 秒读取。';
  $('sites').replaceChildren();for(const [site,label] of [['bank','河岸'],['bridge','旧桥'],['store','仓库']])$('sites').append(actionButton('探索'+label,async()=>{const result=await mutate('camp.explore',{site});$('clues').replaceChildren(text('p',result.clue));await refresh();}));
  $('intents').replaceChildren();const notes=new Map((s.private_notes||[]).map(n=>[n.key,n.value.content]));
  for(const record of [...s.intents].sort((a,b)=>b.value.created_at-a.value.created_at)){
    const v=record.value, item=text('article','','record');item.append(text('span',intentStatus(v.status,!!campaign?.bridgeLocked),'tag'),text('small',(v.owner===profile.role_id?'你的提案':'参与者 '+v.owner.slice(-6))+(Number.isFinite(record.updated_at)?' · 最后变更 '+new Date(record.updated_at*1000).toLocaleString():'')),text('p',v.text));
    if(notes.has(record.key))item.append(text('p','私密补充（你有读取权限）：'+notes.get(record.key),'small'));
    if(v.ruling){item.append(text('p','裁判理由：'+v.ruling.reason));if(v.ruling.effects)item.append(text('p',(v.status==='executed'?'已消耗：':'获准代价（尚未扣除）：')+'木材 '+v.ruling.cost.wood+' / 绳子 '+v.ruling.cost.rope+' / 石料 '+v.ruling.cost.stone+'；'+(v.status==='executed'?'已执行效果：':'获准效果：')+passage[v.ruling.effects.passage]+'，稳定 '+v.ruling.effects.stability+'，暴露 '+v.ruling.effects.exposure));}
    if(v.owner===profile.role_id&&v.status==='approved'){if(campaign?.bridgeLocked)item.append(text('p','补给已开始，旧桥方案已锁定；请按上方营地阶段继续。','small'));else item.append(actionButton('接受代价并执行',async()=>{await mutate('camp.accept',{intent_id:record.key,expected_intent_version:record.version});await refresh();}));}
    $('intents').append(item);
  }
  if(!s.intents.length)$('intents').append(text('p','营地还没有提案。探索后可以提出第一种办法。'));
  $('projects').replaceChildren();for(const record of s.projects){const v=record.value,item=text('article','','record');item.append(text('span',status[v.status],'tag'),text('p',v.title),text('small','真实期限：'+new Date(v.due_at*1000).toLocaleString()));if(v.owner===profile.role_id&&v.status==='active')item.append(actionButton('声明这个事项已完成',async()=>{await mutate('camp.complete_project',{project_id:record.key,expected_project_version:record.version});await refresh();}));$('projects').append(item);}
  if(!s.projects.length)$('projects').append(text('p','没有已提交的计时事项。'));
  const isJudge=s.authority.value.judges.includes(profile.role_id);
  $('referee').hidden=!isJudge||!!campaign?.bridgeLocked;
  if(isJudge&&!campaign?.bridgeLocked){const selected=$('ruling-intent').value;$('ruling-intent').replaceChildren();for(const record of s.intents.filter(n=>n.value.status!=='executed')){const option=text('option',record.value.text.slice(0,70));option.value=record.key;$('ruling-intent').append(option);}if([...$('ruling-intent').options].some(n=>n.value===selected))$('ruling-intent').value=selected;rulingCost();}
  pending();controls();
}
function rulingEffects(){return {passage:$('passage').value,stability:Number($('stability').value),exposure:Number($('exposure').value)};}
function declaredCost(effects){return {wood:effects.passage==='foot'?2:4,rope:effects.stability+2-effects.exposure,stone:effects.stability-1};}
function rulingCost(){const cost=declaredCost(rulingEffects());$('cost').textContent=$('decision').value==='approve'?'世界规则要求：木材 '+cost.wood+' / 绳子 '+cost.rope+' / 石料 '+cost.stone+'。库存和版本将在提交时重新检查。':'拒绝不会产生可执行效果或资源代价。';}
for(const id of ['passage','stability','exposure','decision'])$(id).addEventListener('input',rulingCost);
$('ruling').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const id=$('ruling-intent').value;if(!id)throw new Error('没有可以裁定的提案。');const s=snapshot,record=s.intents.find(n=>n.key===id);const args={intent_id:id,expected_intent_version:record.version,inventory_version:s.inventory.version,route_version:s.route.version,authority_epoch:s.authority.value.epoch,decision:$('decision').value,reason:$('reason').value.trim()};if(args.decision==='approve'){args.effects=rulingEffects();args.cost=declaredCost(args.effects);}await mutate('camp.judge',args);$('reason').value='';await refresh();});});
$('enter').addEventListener('click',()=>run(()=>enter(savedKey().then(value=>value||createKey()),$('name').value.trim())));
$('import-key').addEventListener('change',()=>run(async()=>{const file=$('import-key').files[0];if(!file)return;try{if(file.size>8192)throw new Error('恢复文件过大。');const candidate=file.text().then(value=>JSON.parse(value)).catch(error=>{if(error instanceof SyntaxError)throw new Error('恢复文件格式无法读取，请选择之前保存的营地恢复 JSON 文件；已有身份未被替换。');throw error;});await enter(candidate);}finally{$('import-key').value='';}}));
$('intent').addEventListener('input',()=>{try{draft();}catch(e){showError(e);}});$('private-note').addEventListener('input',()=>{try{draft();}catch(e){showError(e);}});
$('proposal').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const value=$('intent').value.trim();if(!value)return;draft();const original=client.draft('camp-intent');const args={intent_id:crypto.randomUUID(),text:value};if($('private-note').value.trim())args.private_note=$('private-note').value.trim();await mutate('camp.propose',args,{route:'camp-intent',draft_text:original});restoreDraft();await refresh();});});
$('refresh').addEventListener('click',()=>run(refresh));$('reconnect').addEventListener('click',()=>run(()=>enter(key)));
$('backup').addEventListener('click',()=>download('river-camp-recovery.json',key));
$('connect-agent').addEventListener('click',()=>run(async()=>{const session=generation,agent=new CampClient();agentClient=agent;try{await agent.authenticate(key);if(session!==generation)return;download('river-camp-opencode.json',{$schema:'https://opencode.ai/config.json',mcp:{agent_world:{type:'remote',url:location.origin+'/mcp',oauth:false,headers:{Authorization:'Bearer '+agent.token}}}});$('connection').textContent='连接文件已发出，到期 '+new Date(agent.expiresAt*1000).toLocaleString()+'。请在自己的客户端加载；世界尚未证明 Agent 已连接或持续运行。';}finally{if(agentClient===agent)agentClient=null;}}));
$('retry').addEventListener('click',()=>run(async()=>{await client.retry();restoreDraft();await refresh();}));
$('restore').addEventListener('click',()=>run(async()=>{await client.restoreRejected();restoreDraft();if(client.token)await refresh();}));
$('revoke').addEventListener('click',()=>run(async()=>{const result=await client.request('/api/me/revoke-other-credentials',{});$('connection').textContent='这个身份的其他连接权限已撤销。本页仍可用；身份根密钥仍可重新授权。';await refresh();}));
function leave(){try{draft();}catch(e){showError(e);}++generation;clearTimeout(timer);client.cancelRequests();agentClient?.cancelRequests();client.token=null;profile=null;snapshot=null;returnHistory=null;$('world').hidden=true;$('entry').hidden=false;$('situation').replaceChildren();$('intents').replaceChildren();$('projects').replaceChildren();$('return-summary').replaceChildren();$('return-card').hidden=true;clearCampaign();controls();}
$('leave').addEventListener('click',leave);$('cancel').addEventListener('click',leave);
