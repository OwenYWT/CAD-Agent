import { events } from "@react-three/fiber";

// Canvas configuration is asynchronous. A fast document switch can detach its
// event source before onCreated runs; that obsolete canvas needs no listeners.
export function canvasEvents(state: Parameters<typeof events>[0]) {
  const manager = events(state);
  const connect = manager.connect;
  manager.connect = (target) => {
    if (target?.isConnected) connect?.(target);
  };
  return manager;
}
