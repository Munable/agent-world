import {CommonsClient} from '/static/commons-client.js';
import {WorldClient} from '/static/world-client.js';

// Browser signing, identity storage and pending-operation locking stay in Commons.
// A lost response is recovered by its Receipt, then by the same operation ID.
export class GroveClient extends CommonsClient {
  constructor(fetcher=globalThis.fetch.bind(globalThis), storage=localStorage, locks=navigator.locks) {
    super(fetcher,storage,locks);
    this.controllers=new Set();
    this.cancelledControllers=new WeakSet();
    this.world=new WorldClient('',()=>this.token,fetcher);
    this.inventoryAfter=null;
    this.inventoryLimit=16;
    this.inventoryWasReset=false;
  }
  async request(path,body,method,{signal,timeout=15000}={}) {
    const controller=new AbortController();
    const stop=()=>controller.abort();
    if(signal?.aborted)stop();else signal?.addEventListener('abort',stop,{once:true});
    this.controllers.add(controller);
    const deadline=setTimeout(stop,timeout);
    try{return await super.request(path,body,method,{signal:controller.signal});}
    catch(error){
      if(controller.signal.aborted){
        const interrupted=new Error('连接中断；原操作编号已保留。核对结果后再行动。');
        interrupted.cancelled=!!signal?.aborted||this.cancelledControllers.has(controller);
        throw interrupted;
      }
      throw error;
    }finally{clearTimeout(deadline);signal?.removeEventListener('abort',stop);this.controllers.delete(controller);}
  }
  cancel(){for(const controller of this.controllers){this.cancelledControllers.add(controller);controller.abort();}this.world.cancelReads();this.world.view=null;this.inventoryAfter=null;this.inventoryWasReset=false;}
  async retry() {
    const key=this.pendingKey, original=this.pending();
    try{return await super.retry();}
    catch(error){
      if(original && error.status>=400 && error.status<500 &&
        ['InvalidArguments','SchemaRejected','ResourceNotFound','RuleViolation','StateConflict','PermissionDenied','OperationConflict'].includes(error.code)) {
        await this.withPendingLock(key,()=>{if(this.isCurrentPending(key,original))this.storage.setItem(key,JSON.stringify({...original,rejection:{code:error.code,message:error.message}}));});
      }
      throw error;
    }
  }
  async recover() {
    const original=this.pending();
    if(!original)return null;
    if(original.rejection)return {rejected:original.rejection};
    try {
      const receipt=await this.request('/v1/receipts/'+encodeURIComponent(original.body.operation_id));
      if(receipt.ok!==true || receipt.operation_id!==original.body.operation_id || receipt.function_id!==original.name)throw new Error('回执不完整，原操作继续保留。');
      const key=this.pendingKey;
      await this.withPendingLock(key,()=>{if(this.isCurrentPending(key,original))this.storage.removeItem(key);});
      return receipt.result;
    }catch(error){
      if(error.code==='ReceiptNotFound')return this.retry();
      throw error;
    }
  }
  async releaseRejected() {
    const key=this.pendingKey;
    return this.withPendingLock(key,()=>{
      const original=this.pending();
      if(!original?.rejection)throw new Error('原操作结果仍不明确，请先核对回执。');
      this.storage.removeItem(key);
      return original.rejection;
    });
  }
  async connector(path,body){
    const token=this.token;
    try{return await this.request('/connector/'+path,body,undefined,{timeout:20000});}
    catch(error){
      // A disconnected local companion does not revoke the browser's world access.
      if(error.status===401 && !this.token)this.token=token;
      throw error;
    }
  }
  async viewport({reset=false,cx,cy,radius=8,inventory_after,inventory_limit}={}) {
    if(inventory_after!==undefined&&inventory_after!==this.inventoryAfter){this.inventoryAfter=inventory_after;reset=true;}
    if(inventory_limit!==undefined&&inventory_limit!==this.inventoryLimit){this.inventoryLimit=inventory_limit;reset=true;}
    const args={radius,inventory_limit:this.inventoryLimit,...(this.inventoryAfter?{inventory_after:this.inventoryAfter}:{}),...(cx===undefined?{}:{cx,cy})};
    // A map read needs no durable view cursor. The formal view/sync protocol
    // remains available to its consumers; browser polling uses the existing
    // authenticated read-only function, so a clock tick creates no view write.
    try{return await this.world.query('map.viewport',args);}
    catch(error){
      if(this.inventoryAfter&&error.code==='RuleViolation'&&error.message?.includes('背包分页位置')){
        this.inventoryAfter=null;this.inventoryWasReset=true;this.world.view=null;
        delete args.inventory_after;
        return this.world.query('map.viewport',args);
      }
      throw error;
    }
  }
  unpackView(view) {
    if(view?.result?.player)return view.result;
    if(!view?.snapshot)return null;
    return {...view.snapshot.meta,entities:Object.values(view.snapshot.entities),view_cursor:view.cursor};
  }
  readChanges(cursor) {
    return this.request('/v1/streams/grove.changes/read',{limit:40,...(cursor?{cursor}:{})});
  }
}
