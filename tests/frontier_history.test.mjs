import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../examples/frontier_web/frontier-history.js',import.meta.url),'utf8');
const {FrontierHistory}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const page=(start,end,latest)=>({items:Array.from({length:end-start},(_,i)=>({seq:start+i+1,text:`record ${start+i+1}`})),next_after:end,has_more:end<latest,latest_seq:latest});

// A recent observation cannot claim the intervening old history was read.
const history=new FrontierHistory();
history.observeLatest(105);
history.merge(page(104,105,105),104);
assert.equal(history.rows[0].seq,105);
assert.equal(history.olderAfter,0);
assert.equal(history.hasMore,true);
history.latestLoaded=true;
history.merge(page(65,85,105),65);
history.merge(page(85,105,105),85);
assert.equal(history.olderAfter,0);
history.merge(page(0,40,105),0);
assert.equal(history.olderAfter,40);
assert.equal(history.hasMore,true);

// New activity is visible immediately without losing the older prefix cursor.
history.observeLatest(108);
assert.equal(history.needsLatest,true);
history.merge(page(107,108,108),107);
assert.equal(history.rows[0].seq,108);
assert.equal(history.olderAfter,40);
history.merge(page(40,65,108),40);
assert.equal(history.olderAfter,105);
history.merge(page(105,108,108),105);
assert.equal(history.olderAfter,108);
assert.equal(history.hasMore,false);
assert.equal(history.rows.length,108);

// Short byte-bounded pages and overlap remain valid; missing records do not.
const bounded=new FrontierHistory();
bounded.merge(page(0,2,5),0);
assert.equal(bounded.olderAfter,2);
bounded.merge(page(1,4,5),1);
assert.equal(bounded.olderAfter,4);
assert.throws(()=>bounded.merge({items:[{seq:6}],next_after:6,has_more:false,latest_seq:6},4),/遗漏/);
assert.throws(()=>bounded.merge({items:[],next_after:4,has_more:true,latest_seq:6},4),/位置/);
assert.throws(()=>bounded.merge({items:[{seq:5}],next_after:6,has_more:false,latest_seq:6},4),/位置/);
assert.equal(bounded.olderAfter,4);
console.log('Frontier history: newest activity, independent old cursor, byte-short pages, overlap and gap rejection passed');

const observationSource=readFileSync(new URL('../examples/frontier_web/frontier-observation.js',import.meta.url),'utf8');
const {parseCheckpoint,makeCheckpoint,characterChanges,observedRange}=await import('data:text/javascript;base64,'+Buffer.from(observationSource).toString('base64'));
const before={world_id:'living-frontier',character:{role_id:'keeper',scene_id:'market',hp:10,max_hp:10,inventory:['keepsake']},scene:{title:'Market'},inventory_objects:[{id:'keepsake',name:'Old disc'}]};
const checkpoint=makeCheckpoint(before,40,1000);
assert.deepEqual(parseCheckpoint(JSON.stringify(checkpoint),{worldId:'living-frontier',roleId:'keeper'}),checkpoint);
assert.throws(()=>parseCheckpoint(JSON.stringify(checkpoint),{worldId:'living-frontier',roleId:'visitor'}),/格式/);
const returned={...before,character:{...before.character,scene_id:'forest',inventory:[]},scene:{title:'Forest'},inventory_objects:[]};
assert.deepEqual(characterChanges(checkpoint,returned),['位置从“Market”变为“Forest”。','“Old disc”已不在你的背包里；具体去向以已读记录和当前物件状态为准。']);
const seen=new Map([[43,{seq:43,scene_id:'forest'}],[44,{seq:44,scene_id:'market'}]]);
assert.deepEqual(observedRange(seen,40,44).gaps,[{from:41,to:42}]);
seen.set(41,{seq:41});seen.set(42,{seq:42});
assert.equal(observedRange(seen,40,44).complete,true);
assert.equal(observedRange(seen,40,39).rolledBack,true);
console.log('Frontier return observation: identity scope, actual possession changes, cross-scene gaps and restored snapshot detection passed');
