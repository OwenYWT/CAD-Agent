export interface DFMRuleData {
  id: string;
  process: string;
  category: string;
  check_type: string;
  threshold_min: number | null;
  threshold_max: number | null;
  unit: string;
  severity: string;
  description: string;
  suggestion_template: string;
  enabled: boolean;
}
