import {CommonsClient} from '/static/commons-client.js';

// Keep the original operation through timeout, cancellation and a changed session.
export class DndClient extends CommonsClient {
  constructor(fetcher,storage,locks,{timeoutMs=20000}={}){super(fetcher,storage,locks);this.timeoutMs=timeoutMs;this.requests=new Set();this.cancelEpoch=0;}
  async authenticate(key,options={}){const epoch=this.cancelEpoch;const callerCurrent=options.isCurrent||(()=>true);return super.authenticate(key,{...options,isCurrent:()=>epoch===this.cancelEpoch&&callerCurrent()});}
  async request(path,body,method){
    const controller=new AbortController();this.requests.add(controller);const deadline=setTimeout(()=>controller.abort(),this.timeoutMs);
    try{return await super.request(path,body,method,{signal:controller.signal});}
    catch(error){if(controller.signal.aborted){const stopped=new Error(controller.cancelled?'本页连接已取消。':'世界回应超时。原操作与草稿已保留，请核对后重试。');stopped.cancelled=!!controller.cancelled;throw stopped;}throw error;}
    finally{clearTimeout(deadline);this.requests.delete(controller);}
  }
  cancelRequests(){++this.cancelEpoch;for(const controller of this.requests){controller.cancelled=true;controller.abort();}}
  async retry(){
    const pendingKey=this.pendingKey,original=this.pending();
    try{return await super.retry();}catch(error){
      if(original&&error.status>=400&&error.status<500&&['RuleViolation','StateConflict','PermissionDenied'].includes(error.code))await this.withPendingLock(pendingKey,()=>{if(this.isCurrentPending(pendingKey,original))this.storage.setItem(pendingKey,JSON.stringify({...original,rejection:{code:error.code,message:error.message}}));});
      throw error;
    }
  }
}
