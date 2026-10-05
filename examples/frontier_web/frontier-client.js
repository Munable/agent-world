import {CommonsClient} from '/static/commons-client.js';

// Authentication and durable operations use the shared, formally authenticated client.
// Cancelling a fetch never clears an operation whose result may have committed.
export class FrontierClient extends CommonsClient {
  constructor(fetcher, storage, locks, {timeoutMs=25000}={}) {
    super(fetcher,storage,locks);
    this.timeoutMs=timeoutMs;
    this.requests=new Set();
    this.cancelEpoch=0;
  }
  async authenticate(key, options={}) {
    const epoch=this.cancelEpoch;
    const isCurrent=options.isCurrent||(()=>true);
    return super.authenticate(key,{...options,isCurrent:()=>epoch===this.cancelEpoch&&isCurrent()});
  }
  async request(path, body, method, {signal}={}) {
    const controller=new AbortController();
    const abort=()=>controller.abort();
    if(signal?.aborted)controller.abort();
    else signal?.addEventListener('abort',abort,{once:true});
    this.requests.add(controller);
    const deadline=setTimeout(()=>controller.abort(),this.timeoutMs);
    try {return await super.request(path,body,method,{signal:controller.signal});}
    catch(error) {
      if(controller.signal.aborted) {
        const stopped=new Error(controller.cancelled?'本页连接已取消。':'世界回应超时。草稿和原操作仍已保留，请核对后再行动。');
        stopped.cancelled=!!controller.cancelled||!!signal?.aborted;
        throw stopped;
      }
      throw error;
    } finally {
      clearTimeout(deadline);
      signal?.removeEventListener('abort',abort);
      this.requests.delete(controller);
    }
  }
  cancelRequests() {
    ++this.cancelEpoch;
    for(const controller of this.requests){controller.cancelled=true;controller.abort();}
  }
  async retry() {
    const key=this.pendingKey, original=this.pending();
    try {return await super.retry();}
    catch(error) {
      if(original&&error.status>=400&&error.status<500&&['RuleViolation','StateConflict','PermissionDenied'].includes(error.code)) {
        await this.withPendingLock(key,()=>{
          if(this.isCurrentPending(key,original))this.storage.setItem(key,JSON.stringify({...original,rejection:{code:error.code,message:error.message}}));
        });
      }
      throw error;
    }
  }
  async restoreRejected() {
    const pending=this.pending();
    if(!pending?.route?.startsWith('frontier-create:'))return super.restoreRejected();
    // A structured creation draft cannot be merged by joining two JSON strings.
    // Keep a newer edited draft; otherwise restore the original complete draft.
    return this.withPendingLock(this.pendingKey,()=>{
      const original=this.pending();
      if(!original?.rejection)throw new Error('提交结果仍不明确，请先核对原操作。');
      if(!original.route?.startsWith('frontier-create:'))throw new Error('另一个页面已经改变当前提交，请重新核对。');
      const route=original.route, current=this.draft(route), value=current||original.draft_text||'';
      if(value)this.saveDraft(route,value);
      this.storage.removeItem(this.pendingKey);
      return {route,text:value};
    });
  }
}
