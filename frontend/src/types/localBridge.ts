export interface LocalBridge {
  id:string;label:string;paired_at:string | null;last_seen_at:string | null;revoked_at:string | null;online:boolean;
  client_info:{hostname?:string;target_label?:string;capabilities?:string[]};
}
export interface BridgeDelivery {
  id:string;bridge_id:string;release_id:string;status:string;attempts:number;error_message:string | null;
  receipt:{directory:string;file_count:number;archive_sha256:string} | null;
  created_at:string;finished_at:string | null;
}
export interface BridgeState {bridges:LocalBridge[];deliveries:BridgeDelivery[]}
export interface BridgePairing {bridge_id:string;pairing_code:string;expires_at:string}
