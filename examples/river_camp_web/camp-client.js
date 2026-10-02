import {CommonsClient} from '/static/commons-client.js';

// The reference world adds definitive domain rejections to the shared receipt flow.
export class CampClient extends CommonsClient {
  constructor(fetcher,storage,locks,{timeoutMs=20000}={}){super(fetcher,storage,locks);this.timeoutMs=timeoutMs;this.requests=new Set();this.cancelEpoch=0;}
  async authenticate(key){const epoch=this.cancelEpoch;return super.authenticate(key,{isCurrent:()=>epoch===this.cancelEpoch});}
  async request(path,body,method){
    const controller=new AbortController();this.requests.add(controller);
    const deadline=setTimeout(()=>controller.abort(),this.timeoutMs);
    try{return await super.request(path,body,method,{signal:controller.signal});}
    catch(error){if(controller.signal.aborted){const stopped=new Error(controller.cancelled?'已离开本页。':'世界回应超时。草稿与原操作已保留，请核对后重试。');stopped.cancelled=!!controller.cancelled;throw stopped;}throw error;}
    finally{clearTimeout(deadline);this.requests.delete(controller);}
  }
  cancelRequests(){++this.cancelEpoch;for(const controller of this.requests){controller.cancelled=true;controller.abort();}}
  async retry(){
    const pendingKey=this.pendingKey, original=this.pending();
    try{return await super.retry();}catch(error){
      if(original&&error.status>=400&&error.status<500&&['RuleViolation','StateConflict','PermissionDenied'].includes(error.code)){
        await this.withPendingLock(pendingKey,()=>{if(this.isCurrentPending(pendingKey,original))this.storage.setItem(pendingKey,JSON.stringify({...original,rejection:{code:error.code,message:error.message}}));});
      }
      throw error;
    }
  }
  async restoreRejected(){
    const pending=this.pending();
    if(pending?.route!=='camp-intent')return super.restoreRejected();
    return this.withPendingLock(this.pendingKey,()=>{
      const currentPending=this.pending();
      if(!currentPending?.rejection)throw new Error('The outcome is still unknown. Check the original operation before clearing it.');
      if(!this.isCurrentPending(this.pendingKey,pending))throw new Error('The pending action changed. Check it before restoring.');
      const original=JSON.parse(pending.draft_text), current=JSON.parse(this.draft('camp-intent')||'{}');
      const merged={};
      for(const field of ['text','note']){
        const old=original[field]||'', newer=current[field]||'';
        merged[field]=newer&&newer!==old&&old?newer+'\n\n'+old:newer||old;
      }
      this.saveDraft('camp-intent',JSON.stringify(merged));
      this.storage.removeItem(this.pendingKey);
      return {route:'camp-intent',text:JSON.stringify(merged)};
    });
  }
}
