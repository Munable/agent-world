// Presentation data is untrusted text. This module never executes or renders HTML.
export class EventLedger {
  constructor(limit=600) { this.limit=limit; this.byId=new Map(); }
  ingest(events, {historical=false}={}) {
    const added=[];
    for(const event of events||[]) {
      if(!event || typeof event.event_id!=='string' || !Number.isFinite(event.occurred_at)) continue;
      if(this.byId.has(event.event_id)) continue;
      const item={...structuredClone(event),historical};
      this.byId.set(item.event_id,item); added.push(item);
    }
    if(this.byId.size>this.limit) {
      const ordered=this.items();
      for(const item of ordered.slice(0,this.byId.size-this.limit)) this.byId.delete(item.event_id);
    }
    return added;
  }
  items() { return [...this.byId.values()].sort((a,b)=>a.occurred_at-b.occurred_at || a.seq-b.seq || a.event_id.localeCompare(b.event_id)); }
  clear() { this.byId.clear(); }
}

export class BubbleQueue {
  constructor({maxVisible=4,maxPending=80,perSubject=12}={}) {
    this.maxVisible=maxVisible;this.maxPending=maxPending;this.perSubject=perSubject;
    this.pending=[];this.showing=new Map();this.seen=new Set();this.dropped=0;
  }
  push(message, now=Date.now()/1000) {
    if(!message?.id || this.seen.has(message.id)) return false;
    this.seen.add(message.id);
    if(this.seen.size>1200) this.seen.delete(this.seen.values().next().value);
    if(this.pending.length>=this.maxPending || this.pending.filter(x=>x.subject===message.subject).length>=this.perSubject) {
      ++this.dropped;return false; // The retained ledger remains the source for missed speech.
    }
    if(message.expires_at!==undefined && (!Number.isFinite(message.expires_at) || message.expires_at<=now)) {
      ++this.dropped;return false;
    }
    this.pending.push(structuredClone(message));return true;
  }
  active(now) {
    for(const [subject,item] of this.showing) if(item.until<=now)this.showing.delete(subject);
    // Expire even behind occupied slots; retained history is unaffected.
    this.pending=this.pending.filter(message=>{
      if(message.expires_at!==undefined && message.expires_at<=now){++this.dropped;return false;}
      return true;
    });
    for(let i=0;i<this.pending.length && this.showing.size<this.maxVisible;) {
      const message=this.pending[i];
      if(this.showing.has(message.subject)){++i;continue;}
      this.pending.splice(i,1);
      const until=Math.min(message.expires_at??Infinity,now+Math.min(8,Math.max(4,[...(message.text||'')].length/25)));
      this.showing.set(message.subject,{...message,started:now,until});
    }
    return [...this.showing.values()];
  }
  clear(){this.pending=[];this.showing.clear();this.seen.clear();this.dropped=0;}
}
