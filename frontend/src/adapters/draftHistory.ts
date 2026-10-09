export interface DraftHistory<T> { past:T[]; present:T; future:T[] }
export type DraftAction<T> = {kind:'set';value:T | ((value:T)=>T)} | {kind:'undo'} | {kind:'redo'} | {kind:'reset';value:T};
export function reduceDraft<T>(state:DraftHistory<T>,action:DraftAction<T>):DraftHistory<T> {
  if (action.kind==='reset') return {past:[],present:action.value,future:[]};
  if (action.kind==='undo') return state.past.length ? {past:state.past.slice(0,-1),present:state.past.at(-1)!,future:[state.present,...state.future]} : state;
  if (action.kind==='redo') return state.future.length ? {past:[...state.past,state.present].slice(-50),present:state.future[0],future:state.future.slice(1)} : state;
  const present=typeof action.value==='function' ? (action.value as (value:T)=>T)(state.present) : action.value;
  if (JSON.stringify(present)===JSON.stringify(state.present)) return state;
  return {past:[...state.past,state.present].slice(-50),present,future:[]};
}
