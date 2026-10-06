// A browser checkpoint records only facts this identity has actually displayed.
// It is local presentation state, never a goal or a world write.
export function parseCheckpoint(raw,{worldId,roleId}){
  if(!raw)return null;
  const value=JSON.parse(raw);
  if(value?.version!==1||value.world_id!==worldId||value.role_id!==roleId||!Number.isInteger(value.seen_seq)||value.seen_seq<0||!Number.isFinite(value.saved_at)||typeof value.character?.scene_id!=='string'||!Array.isArray(value.character.inventory))throw new Error('本机接续记录格式不完整。');
  for(const item of value.character.inventory)if(typeof item?.id!=='string'||typeof item.name!=='string')throw new Error('本机物件记录格式不完整。');
  return value;
}

export function makeCheckpoint(snapshot,seenSeq,savedAt=Date.now()){
  const character=snapshot.character;
  if(!character||!Number.isInteger(seenSeq)||seenSeq<0)throw new Error('当前人物或已读记录不完整。');
  const objects=new Map((snapshot.inventory_objects||[]).map(item=>[item.id,item]));
  return {version:1,world_id:snapshot.world_id,role_id:character.role_id,seen_seq:seenSeq,saved_at:savedAt,character:{scene_id:character.scene_id,scene_title:snapshot.scene?.title||character.scene_id,hp:character.hp,max_hp:character.max_hp,inventory:character.inventory.map(id=>({id,name:objects.get(id)?.name||'物件 '+id.slice(-8)}))}};
}

export function characterChanges(checkpoint,snapshot){
  if(!checkpoint||!snapshot.character)return [];
  const before=checkpoint.character,now=snapshot.character,changes=[];
  if(before.scene_id!==now.scene_id)changes.push('位置从“'+(before.scene_title||before.scene_id)+'”变为“'+(snapshot.scene?.title||now.scene_id)+'”。');
  if(Number.isFinite(before.hp)&&before.hp!==now.hp)changes.push('生命从 '+before.hp+' 变为 '+now.hp+' / '+now.max_hp+'。');
  const previous=new Map(before.inventory.map(item=>[item.id,item.name])),current=new Map((snapshot.inventory_objects||[]).map(item=>[item.id,item.name]));
  for(const id of now.inventory)if(!previous.has(id))changes.push('现在持有“'+(current.get(id)||'物件 '+id.slice(-8))+'”。');
  const held=new Set(now.inventory);for(const [id,name]of previous)if(!held.has(id))changes.push('“'+name+'”已不在你的背包里；具体去向以已读记录和当前物件状态为准。');
  return changes;
}

// No filter is applied until coverage is known. Empty local activity therefore
// cannot be confused with a complete absence of activity in the world.
export function observedRange(items,after,through){
  if(!Number.isInteger(after)||after<0||!Number.isInteger(through)||through<0)throw new Error('接续记录序号不完整。');
  if(after>through)return {rows:[],gaps:[],complete:false,rolledBack:true};
  const rows=[...items.values()].filter(item=>item.seq>after&&item.seq<=through).sort((a,b)=>a.seq-b.seq),gaps=[];
  let next=after+1;for(const row of rows){if(row.seq>next)gaps.push({from:next,to:row.seq-1});next=row.seq+1;}
  if(next<=through)gaps.push({from:next,to:through});
  return {rows,gaps,complete:gaps.length===0,rolledBack:false};
}
