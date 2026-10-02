import {createKey,validateKey,savedKey} from '/static/commons-client.js';
import {CampClient} from './camp-client.js';
const $=id=>document.getElementById(id), client=new CampClient();
let key=null, profile=null, snapshot=null, busy=false, generation=0, timer=null, agentClient=null;
const text=(tag,value,cls='')=>{const n=document.createElement(tag);n.textContent=value;if(cls)n.className=cls;return n;};
const status={pending:'待裁判回应',approved:'方案已获准，等待本人接受',declined:'未获准',executed:'已执行',active:'进行中',completed:'本人已声明完成',expired:'已到期'};
function download(name,value){const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)+'\n'],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function draft(){if(!profile)return;client.saveDraft('camp-intent',JSON.stringify({text:$('intent').value,note:$('private-note').value}));}
function restoreDraft(){let data={};try{data=JSON.parse(client.draft('camp-intent')||'{}');}catch{}$('intent').value=data.text||'';$('private-note').value=data.note||'';}
function showError(error){if(error.cancelled||error.staleAuth)return;$('error').textContent=error.message||String(error);$('error').hidden=false;if(error.status===401){client.token=null;$('reconnect').hidden=false;$('connection').textContent='本页连接已失效；草稿和世界事实仍保留。请同钥重连。';for(const id of ['situation','intents','projects','clues'])$(id).replaceChildren();$('freshness').textContent='请重连后读取最新局面。';$('referee').hidden=true;snapshot=null;controls();}}
function pending(){const value=profile&&client.pending();$('pending').hidden=!value;$('retry').hidden=!!value?.rejection;$('restore').hidden=!value?.rejection;$('pending-detail').textContent=value?.rejection?value.rejection.message+' 本次没有提交。恢复草稿后可以调整。':'结果尚未确定。核对会复用同一个操作，不会创建第二次行动。';}
function controls(){$('cancel').hidden=!busy||$('entry').hidden;document.querySelectorAll('button').forEach(n=>n.disabled=(busy&&!['leave','cancel'].includes(n.id))||!!(profile&&!client.token&&n.closest('#world')&&!['reconnect','backup','leave','restore'].includes(n.id)));}
async function run(fn){if(busy)return;busy=true;$('error').hidden=true;controls();try{await fn();}catch(e){showError(e);}finally{busy=false;controls();pending();}}
async function mutate(name,args,context={}){
  const session=generation;
  return client.mutate(name,args,context,()=>session===generation&&!!profile&&!!client.token);
}
async function enter(newKey,name=''){
  const session=++generation,active=()=>{if(session!==generation){const error=new Error('连接已取消。');error.cancelled=true;throw error;}};
  const candidate=await newKey;active();key=await validateKey(candidate);active();await savedKey(key);active();
  const result=await client.authenticate(key);active();profile=result.role_profile;
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
function render(){
  const s=snapshot, route=s.route.value, stock=s.inventory.value;
  $('situation').replaceChildren(text('p',s.goal_reached?'目标已达成：稳定且低暴露的货物通行。':'目标：恢复货物通行，稳定度至少 2，暴露程度至多 1。'));
  const materials=text('div','','materials');for(const [name,label] of [['wood','木材'],['rope','绳子'],['stone','石料']])materials.append(text('span',label+' '+stock[name]));$('situation').append(materials);
  $('situation').append(text('p','通行：'+({blocked:'阻断',foot:'步行',cargo:'货物'}[route.passage])+' · 稳定度 '+route.stability+' · 暴露 '+route.exposure));
  $('freshness').textContent='最后核对 '+new Date(s.now*1000).toLocaleTimeString()+'；页面可见时每 6 秒读取。';
  $('sites').replaceChildren();for(const [site,label] of [['bank','河岸'],['bridge','旧桥'],['store','仓库']])$('sites').append(actionButton('探索'+label,async()=>{const result=await mutate('camp.explore',{site});$('clues').replaceChildren(text('p',result.clue));await refresh();}));
  $('intents').replaceChildren();const notes=new Map((s.private_notes||[]).map(n=>[n.key,n.value.content]));
  for(const record of [...s.intents].sort((a,b)=>b.value.created_at-a.value.created_at)){
    const v=record.value, item=text('article','','record');item.append(text('span',status[v.status],'tag'),text('small',v.owner===profile.role_id?'你的提案':'参与者 '+v.owner.slice(-6)),text('p',v.text));
    if(notes.has(record.key))item.append(text('p','私密补充（你有读取权限）：'+notes.get(record.key),'small'));
    if(v.ruling){item.append(text('p','裁判理由：'+v.ruling.reason));if(v.ruling.effects)item.append(text('p','代价：木材 '+v.ruling.cost.wood+' / 绳子 '+v.ruling.cost.rope+' / 石料 '+v.ruling.cost.stone+'；结果：'+v.ruling.effects.passage+'，稳定 '+v.ruling.effects.stability+'，暴露 '+v.ruling.effects.exposure));}
    if(v.owner===profile.role_id&&v.status==='approved')item.append(actionButton('接受代价并执行',async()=>{await mutate('camp.accept',{intent_id:record.key,expected_intent_version:record.version});await refresh();}));
    $('intents').append(item);
  }
  if(!s.intents.length)$('intents').append(text('p','营地还没有提案。探索后可以提出第一种办法。'));
  $('projects').replaceChildren();for(const record of s.projects){const v=record.value,item=text('article','','record');item.append(text('span',status[v.status],'tag'),text('p',v.title),text('small','真实期限：'+new Date(v.due_at*1000).toLocaleString()));if(v.owner===profile.role_id&&v.status==='active')item.append(actionButton('声明这个事项已完成',async()=>{await mutate('camp.complete_project',{project_id:record.key,expected_project_version:record.version});await refresh();}));$('projects').append(item);}
  if(!s.projects.length)$('projects').append(text('p','没有已提交的计时事项。'));
  const isJudge=s.authority.value.judges.includes(profile.role_id);
  $('referee').hidden=!isJudge;
  if(isJudge){const selected=$('ruling-intent').value;$('ruling-intent').replaceChildren();for(const record of s.intents.filter(n=>n.value.status!=='executed')){const option=text('option',record.value.text.slice(0,70));option.value=record.key;$('ruling-intent').append(option);}if([...$('ruling-intent').options].some(n=>n.value===selected))$('ruling-intent').value=selected;rulingCost();}
  pending();
}
function rulingEffects(){return {passage:$('passage').value,stability:Number($('stability').value),exposure:Number($('exposure').value)};}
function declaredCost(effects){return {wood:effects.passage==='foot'?2:4,rope:effects.stability+2-effects.exposure,stone:effects.stability-1};}
function rulingCost(){const cost=declaredCost(rulingEffects());$('cost').textContent=$('decision').value==='approve'?'世界规则要求：木材 '+cost.wood+' / 绳子 '+cost.rope+' / 石料 '+cost.stone+'。库存和版本将在提交时重新检查。':'拒绝不会产生可执行效果或资源代价。';}
for(const id of ['passage','stability','exposure','decision'])$(id).addEventListener('input',rulingCost);
$('ruling').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const id=$('ruling-intent').value;if(!id)throw new Error('没有可以裁定的提案。');const s=snapshot,record=s.intents.find(n=>n.key===id);const args={intent_id:id,expected_intent_version:record.version,inventory_version:s.inventory.version,route_version:s.route.version,authority_epoch:s.authority.value.epoch,decision:$('decision').value,reason:$('reason').value.trim()};if(args.decision==='approve'){args.effects=rulingEffects();args.cost=declaredCost(args.effects);}await mutate('camp.judge',args);$('reason').value='';await refresh();});});
$('enter').addEventListener('click',()=>run(()=>enter(savedKey().then(value=>value||createKey()),$('name').value.trim())));
$('import-key').addEventListener('change',()=>run(async()=>{const file=$('import-key').files[0];if(!file)return;try{if(file.size>8192)throw new Error('恢复文件过大。');await enter(file.text().then(value=>JSON.parse(value)));}finally{$('import-key').value='';}}));
$('intent').addEventListener('input',()=>{try{draft();}catch(e){showError(e);}});$('private-note').addEventListener('input',()=>{try{draft();}catch(e){showError(e);}});
$('proposal').addEventListener('submit',event=>{event.preventDefault();run(async()=>{const value=$('intent').value.trim();if(!value)return;draft();const original=client.draft('camp-intent');const args={intent_id:crypto.randomUUID(),text:value};if($('private-note').value.trim())args.private_note=$('private-note').value.trim();await mutate('camp.propose',args,{route:'camp-intent',draft_text:original});restoreDraft();await refresh();});});
$('refresh').addEventListener('click',()=>run(refresh));$('reconnect').addEventListener('click',()=>run(()=>enter(key)));
$('backup').addEventListener('click',()=>download('river-camp-recovery.json',key));
$('connect-agent').addEventListener('click',()=>run(async()=>{const session=generation,agent=new CampClient();agentClient=agent;try{await agent.authenticate(key);if(session!==generation)return;download('river-camp-opencode.json',{$schema:'https://opencode.ai/config.json',mcp:{agent_world:{type:'remote',url:location.origin+'/mcp',oauth:false,headers:{Authorization:'Bearer '+agent.token}}}});$('connection').textContent='连接文件已发出，到期 '+new Date(agent.expiresAt*1000).toLocaleString()+'。请在自己的客户端加载；世界尚未证明 Agent 已连接或持续运行。';}finally{if(agentClient===agent)agentClient=null;}}));
$('retry').addEventListener('click',()=>run(async()=>{await client.retry();restoreDraft();await refresh();}));
$('restore').addEventListener('click',()=>run(async()=>{await client.restoreRejected();restoreDraft();if(client.token)await refresh();}));
$('revoke').addEventListener('click',()=>run(async()=>{const result=await client.request('/api/me/revoke-other-credentials',{});$('connection').textContent='这个身份的其他连接权限已撤销。本页仍可用；身份根密钥仍可重新授权。';await refresh();}));
function leave(){try{draft();}catch(e){showError(e);}++generation;clearTimeout(timer);client.cancelRequests();agentClient?.cancelRequests();client.token=null;profile=null;snapshot=null;$('world').hidden=true;$('entry').hidden=false;$('situation').replaceChildren();$('intents').replaceChildren();$('projects').replaceChildren();controls();}
$('leave').addEventListener('click',leave);$('cancel').addEventListener('click',leave);
