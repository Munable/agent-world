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
