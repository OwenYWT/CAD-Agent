import { useReducer, type SetStateAction } from 'react';
import { reduceDraft } from '../adapters/draftHistory';
export function useDraftHistory<T>(initial:T) {
  const [state,dispatch]=useReducer(reduceDraft<T>,{past:[],present:initial,future:[]});
  return {value:state.present,set:(value:SetStateAction<T>)=>dispatch({kind:'set',value}),
    reset:(value:T)=>dispatch({kind:'reset',value}),canUndo:state.past.length>0,canRedo:state.future.length>0,
    undo:()=>dispatch({kind:'undo'}),redo:()=>dispatch({kind:'redo'})};
}
