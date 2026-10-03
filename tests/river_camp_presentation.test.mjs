import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../examples/river_camp_web/camp-presentation.js',import.meta.url),'utf8');
const {pendingPresentation,CampReturnHistory,campaignPresentation,campaignActivity,intentStatus}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

const unknown=pendingPresentation({name:'camp.propose',route:'camp-intent'});
assert.equal(unknown.rejected,false);assert.equal(unknown.hasDraft,false);assert.match(unknown.detail,/复用同一个操作/);
const proposal=pendingPresentation({name:'camp.propose',route:'camp-intent',rejection:{message:'库存变化'}});
assert.equal(proposal.hasDraft,true);assert.match(proposal.restoreLabel,/恢复提案/);assert.match(proposal.detail,/私密补充/);
for(const name of ['camp.accept','camp.judge','camp.explore','campaign.advance']){
  const action=pendingPresentation({name,rejection:{message:'版本已变化'}});
  assert.equal(action.rejected,true);assert.equal(action.hasDraft,false);assert.match(action.restoreLabel,/清除已拒绝操作/);assert.match(action.detail,/不撤销任何已成功行动/);assert.doesNotMatch(action.detail,/恢复.*草稿/);
}
console.log('Camp presentation: unknown outcomes retain same-operation checking; only proposal drafts offer text recovery');

const records=new Map(),storage={getItem:key=>records.get(key)||null,setItem:(key,value)=>records.set(key,value)};
const snapshot=(now,wood,passage='blocked')=>({now,inventory:{value:{wood,rope:4,stone:3}},route:{value:{passage,stability:0,exposure:2}},private_notes:[{content:'private secret'}],identity:{token:'Bearer secret'}});
const firstVisit=new CampReturnHistory(storage,'camp','public-key-a');assert.equal(firstVisit.observe(snapshot(10,6)),null);
const returned=new CampReturnHistory(storage,'camp','public-key-a');
const firstChange=returned.observe(snapshot(20,4,'foot'));assert.equal(firstChange.previous.now,10);assert.deepEqual(firstChange.changes.map(change=>change.key),['passage','wood']);
const poll=returned.observe(snapshot(30,0,'cargo'));assert.equal(poll.previous.now,10);assert.equal(poll.previous.inventory.wood,6);assert.equal(poll.current.inventory.wood,0);
const nextVisit=new CampReturnHistory(storage,'camp','public-key-a');const unchanged=nextVisit.observe(snapshot(40,0,'cargo'));assert.equal(unchanged.previous.now,30);assert.deepEqual(unchanged.changes,[]);
assert.equal(new CampReturnHistory(storage,'other-camp','public-key-a').previous,null);assert.equal(new CampReturnHistory(storage,'camp','public-key-b').previous,null);
assert.doesNotMatch(JSON.stringify([...records]),/private|Bearer|secret|identity/);assert.deepEqual(Object.keys(JSON.parse([...records.values()][0])).sort(),['inventory','now','route']);
const invalid=new CampReturnHistory(storage,'camp','public-key-a');assert.equal(invalid.observe({now:50}),null);assert.equal(invalid.previous.now,40);
console.log('Camp return: session baseline stays frozen across polls; checkpoints isolate world/key and persist only public route, stock and server time');

assert.equal(campaignPresentation({goal_reached:true},'me'),null);
const action={function:'campaign.supply',label:'带回木材',arguments:{shipment_id:'timber',expected_campaign_version:4,inventory_version:2,route_version:2},enabled:false,blocked_reason:'先恢复通行'};
const base={goal_reached:true,authority:{value:{judges:['gm']}},intents:[{value:{owner:'me',status:'approved'}},{value:{owner:'other',status:'pending'}},{value:{owner:'other',status:'approved'}}],campaign:{stage:'bridge',version:4,goal:{text:'完成补给和营地准备',reached:false,targets:[{id:'supply',label:'两趟补给',done:false}]},personals:[{role_id:'other',choice:'build',label:'他人的目标',completed:true},{role_id:'me',choice:'watch',label:'观察水情',completed:false,contributions:{observed:false}}],available_actions:[action],shipments:{timber:{claimed:false},quarry:{claimed:false}},next:'你可以选择自己的目标。',activity:[{actor_role_id:'other',action:'supply',at:1,details:{}},{actor_role_id:'me',action:'choose_goal',at:2,details:{choice:'watch'}}]}};
const presentation=campaignPresentation(base,'me');
assert.equal(presentation.goalReached,false,'base route goal must not complete the campaign');assert.equal(presentation.personalLabel,'观察水情');assert.equal(presentation.personal.completed,false);assert.equal(presentation.actions[0],action);assert.equal(presentation.actions[0].enabled,false);assert.equal(presentation.actions[0].arguments.expected_campaign_version,4);
const readyBridge=campaignPresentation({...base,route_goal_reached:true},'me');assert.equal(readyBridge.stage,'货运桥已通，准备补给');assert.match(readyBridge.next,/选择个人目标并领取第一趟补给/);assert.match(readyBridge.next,/第二趟之前建仓可防损失/);assert.equal(readyBridge.goalReached,false);assert.equal(readyBridge.personal.completed,false);assert.equal(readyBridge.actions[0],action);
const cargoRoute={value:{passage:'cargo',stability:2,exposure:1}};
assert.equal(campaignPresentation({...base,route:cargoRoute},'me').stage,'货运桥已通，准备补给','older snapshots derive route readiness only when the authoritative milestone is absent');
const unfinishedBridge=campaignPresentation({...base,route:cargoRoute,route_goal_reached:false},'me');assert.equal(unfinishedBridge.stage,'恢复旧桥通行');assert.equal(unfinishedBridge.next,base.campaign.next);assert.doesNotMatch(unfinishedBridge.next,/货运桥已通/);
assert.equal(campaignPresentation({...base,route:{value:{passage:'cargo',stability:1,exposure:1}}},'me').stage,'恢复旧桥通行');
const terminal=campaignPresentation({...base,campaign:{...base.campaign,stage:'repaired',goal:{...base.campaign.goal,reached:true},next:'共同目标已达成。'}},'me');assert.equal(terminal.goalReached,true);assert.match(terminal.next,/共同目标已达成。 这一有限章节已结束；可看历史和继续探索，后续章节尚未开放。/);assert.equal(terminal.personal.completed,false,'chapter completion must not complete another personal contribution');assert.equal(terminal.actions[0],action);
assert.match(presentation.waiting.join('\n'),/由你本人接受/);assert.match(presentation.waiting.join('\n'),/等待当前获准裁判/);assert.equal(presentation.activity[0].at,2);
const locked=campaignPresentation({...base,campaign:{...base.campaign,stage:'supply',shipments:{timber:{claimed:true},quarry:{claimed:false}}}},'me');assert.equal(locked.bridgeLocked,true);assert.match(locked.waiting.join('\n'),/旧桥方案当前不能再执行/);assert.doesNotMatch(locked.waiting.join('\n'),/等待当前获准裁判|等待原提案者/);
const noJudge=campaignPresentation({...base,authority:{value:{judges:[]}}},'me');assert.match(noJudge.waiting.join('\n'),/当前没有获准裁判/);
const flood=campaignActivity({action:'flood',details:{loss:{wood:2,rope:1},private_notes:'do not display secret',token:'Bearer secret'}});assert.match(flood.label,/后果已落地/);assert.match(flood.detail,/木材 2/);assert.doesNotMatch(JSON.stringify(flood),/secret|Bearer|private/);
const supply=campaignActivity({action:'campaign.supply',details:{shipment_id:'quarry',received:{wood:2,rope:1,stone:2},loss:{wood:2,rope:1,stone:0},route:{passage:'blocked',stability:1,exposure:2},story_event:'second-shipment high water'}});assert.match(supply.detail,/收到材料：木材 2/);assert.match(supply.detail,/损失：木材 2/);assert.match(supply.detail,/已落地路线：阻断/);assert.match(supply.detail,/不是现实天气/);
assert.match(campaignActivity({action:'campaign.salvage_crate',details:{received:{wood:2,rope:1,stone:0}}}).detail,/不退回旧桥材料/);
const knownFinding='The second fixed shipment triggers story high water: it blocks the route and loses 2 wood/1 rope unless a storehouse already exists. This is not real weather or a day passing.';
const observed=campaignActivity({action:'campaign.observe_water',details:{finding:knownFinding}}).detail;
assert.match(observed,/第二趟固定补给会触发剧情涨水并阻断路线/);assert.match(observed,/已有仓库可避免材料损失/);assert.match(observed,/木材 2 \/ 绳子 1/);assert.match(observed,/不是现实天气或一天过去/);assert.doesNotMatch(observed,/The second fixed shipment/);
const unknownFinding=knownFinding+' Different world rule.';assert.equal(campaignActivity({action:'campaign.observe_water',details:{finding:unknownFinding}}).detail,'观测线索：'+unknownFinding);assert.equal(campaignActivity({action:'custom.observe',details:{finding:knownFinding}}).detail,'观测线索：'+knownFinding);
assert.doesNotMatch(intentStatus('pending',true),/待裁判回应/);assert.match(intentStatus('approved',true),/已锁定/);assert.equal(intentStatus('executed',true),'已执行');assert.match(intentStatus('approved',false),/等待本人接受/);
console.log('Campaign presentation: base/campaign goals stay distinct, only own goal is shown, current action versions and blockers are preserved, and waits follow committed state');
