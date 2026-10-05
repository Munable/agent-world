// Public story records are immutable. Recent observation and historical paging
// have different positions: a tail refresh never advances the older-page cursor.
export class FrontierHistory {
  constructor(){this.items=new Map();this.latestSeq=0;this.olderAfter=0;this.latestLoaded=false;}
  observeLatest(value){
    if(!Number.isInteger(value)||value<0)throw new Error('世界记录的最新序号不完整。');
    this.latestSeq=Math.max(this.latestSeq,value);
  }
  merge(page,after){
    if(!page||!Array.isArray(page.items)||!Number.isInteger(after)||after<0||!Number.isInteger(page.latest_seq)||page.latest_seq<0||!Number.isInteger(page.next_after)||page.next_after<after||typeof page.has_more!=='boolean')throw new Error('世界记录分页没有正确接续。');
    let previous=after;
    for(const item of page.items){if(!item||!Number.isInteger(item.seq)||item.seq!==previous+1||item.seq>page.latest_seq)throw new Error('世界记录页有遗漏或序号不一致。');previous=item.seq;}
    if(page.next_after!==previous||(page.has_more&&(!page.items.length||page.next_after>=page.latest_seq))||(!page.has_more&&page.next_after<page.latest_seq))throw new Error('世界记录分页位置不完整。');
    this.observeLatest(page.latest_seq);
    for(const item of page.items)this.items.set(item.seq,item);
    // Only an actually observed uninterrupted prefix advances historical paging.
    while(this.items.has(this.olderAfter+1))++this.olderAfter;
  }
  get hasMore(){return this.olderAfter<this.latestSeq;}
  get needsLatest(){return !this.latestLoaded||this.latestSeq>0&&!this.items.has(this.latestSeq);}
  get rows(){return [...this.items.values()].sort((a,b)=>b.seq-a.seq);}
  get highestLoaded(){return Math.max(0,...this.items.keys());}
}
