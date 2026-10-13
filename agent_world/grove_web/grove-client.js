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
    this.projectionContext=null;
    this.projectionBlocked=false;
  }
  async authenticate(key,options={}) {
    this.world.cancelReads();
    this.projectionContext=null;this.projectionBlocked=false;
    return super.authenticate(key,options);
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
  cancel(){for(const controller of this.controllers){this.cancelledControllers.add(controller);controller.abort();}this.world.cancelReads();this.world.view=null;this.inventoryAfter=null;this.inventoryWasReset=false;this.projectionContext=null;this.projectionBlocked=false;}
  async query(name,args={}) {
    if(!name.startsWith('grove.client_'))return super.query(name,args);
    const identity={token:this.token,tokenId:this.tokenId,roleId:this.roleId,universe:this.universe};
    let envelope;
    try{envelope=await this.world.query(name,args);}
    catch(error){
      if(Object.entries(identity).some(([field,expected])=>this[field]!==expected)){
        error.cancelled=true;
        if(error.status===401)error.staleAuth=true;
      }
      throw error;
    }
    const value=envelope?.result,context=value?.context;
    if(Object.entries(identity).some(([field,expected])=>this[field]!==expected)){
      const error=new Error('读取期间身份已改变；请重新读取。');error.cancelled=true;throw error;
    }
    if(context?.projection_version!==1){this.projectionBlocked=true;throw new Error('客户端接口版本尚未支持；重新发现接口后再操作。');}
    const functionVersion=name==='grove.client_definitions'?2:1;
    if(envelope.ok!==true||envelope.read_only!==true||envelope.function_id!==name||envelope.function_version!==functionVersion||
       context.world_id!=='shared-grove'||!Number.isInteger(context.world_version)||context.world_version<1||
       context.world_instance!==identity.universe||context.viewer_role_id!==identity.roleId||
       !Number.isSafeInteger(envelope.snapshot_seq)||envelope.snapshot_seq<0||context.snapshot_seq!==envelope.snapshot_seq){
      this.projectionBlocked=true;throw new Error('读取的世界、身份或快照水位未一致核对，未应用现场。');
    }
    const previous=this.projectionContext;
    if(previous?.world_instance===context.world_instance&&previous?.viewer_role_id===context.viewer_role_id&&previous.world_version!==context.world_version){
      this.projectionBlocked=true;throw new Error('世界规则版本已改变；重新发现并连接接口后再操作。');
    }
    if(previous?.world_instance===context.world_instance&&previous?.viewer_role_id===context.viewer_role_id&&context.snapshot_seq<previous.snapshot_seq){
      const error=new Error('较旧的读取已丢弃；保留最新已读现场。');error.cancelled=true;throw error;
    }
    this.projectionContext=context;this.projectionBlocked=false;
    return value;
  }
  async mutate(name,args,context={},isCurrent) {
    if(name.startsWith('grove.')&&name!=='grove.control_revoke'&&(this.projectionBlocked||this.projectionContext?.projection_version!==1||this.projectionContext.world_instance!==this.universe||this.projectionContext.viewer_role_id!==this.roleId))throw new Error('先取得本世界、本身份的受支持客户端接口，再操作。');
    return super.mutate(name,args,context,isCurrent);
  }
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
    try{return await this.query('grove.client_viewport',args);}
    catch(error){
      if(this.inventoryAfter&&error.code==='RuleViolation'&&error.message?.includes('背包分页位置')){
        this.inventoryAfter=null;this.inventoryWasReset=true;this.world.view=null;
        delete args.inventory_after;
        return this.query('grove.client_viewport',args);
      }
      throw error;
    }
  }
  unpackView(view) {
    return view?.context?.projection_version===1&&view?.player?view:null;
  }
  readChanges(cursor) {
    return this.request('/v1/streams/grove.changes/read',{limit:40,...(cursor?{cursor}:{})});
  }
}
