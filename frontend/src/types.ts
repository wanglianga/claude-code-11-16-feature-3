export interface Site {
  id: number;
  code: string;
  name: string;
  timezone: string;
  diff_threshold_pct: string;
}

export interface Bill {
  id: number;
  bill_no: string;
  site_id: number;
  job_id: number;
  master_total_kwh: string;
  charging_kwh: string;
  estimated_kwh: string;
  pending_kwh: string;
  pending_order_count: number;
  public_kwh: string;
  unexplained_kwh: string;
  suspended_kwh: string;
  charging_amount: string;
  public_amount: string;
  status: string;
  confirmed_at: string | null;
  confirmed_by: string | null;
}

export interface Job {
  id: number;
  site_id: number;
  cycle_start_local: string;
  cycle_end_local: string;
  status: string;
  threshold_pct: string;
  rerun_of_job_id: number | null;
  message: string | null;
}

export interface Interval {
  id: number;
  start_ts_local: string;
  end_ts_local: string;
  master_reading_start: string | null;
  master_reading_end: string | null;
  master_energy_kwh: string | null;
  device_meter_kwh: string;
  charging_kwh: string;
  estimated_kwh: string;
  public_kwh: string;
  unexplained_kwh: string;
  order_meter_diff_kwh: string;
  basis_status: string;
  note: string | null;
}

export interface Split {
  id: number;
  order_id: number;
  start_ts_local: string;
  end_ts_local: string;
  segment_name: string;
  price_per_kwh: string;
  energy_kwh: string | null;
  amount: string | null;
  basis_status: string;
  note: string | null;
}

export interface ReviewTask {
  id: number;
  site_id: number;
  job_id: number | null;
  task_kind: string;
  title: string;
  detail: string;
  assignee: string;
  status: string;
  response: string | null;
  responded_by: string | null;
}

export interface Conflict {
  id: number;
  site_id: number;
  device_id: string;
  order_a_id: number;
  order_b_id: number;
  status: string;
  resolved_order_id: number | null;
  resolution_note: string | null;
}

export interface ImportFile {
  id: number;
  site_id: number;
  source_type: string;
  original_name: string;
  sha256: string;
  row_count: number;
  imported_by: string;
  imported_at: string;
}

export interface ImportRow {
  id: number;
  line_no: number;
  raw_line: string;
  outcome: string;
  source_record_no: string | null;
  note: string | null;
}

export interface Adjustment {
  id: number;
  component: string;
  original_value: string;
  corrected_value: string;
  delta: string;
  reason: string;
  created_by: string;
  created_at: string;
}

// ---------------- 分成合同与追溯试算 ----------------

export interface RevenueTier {
  upper_revenue: string | null;
  rate: string;
}

export interface Deduction {
  name?: string | null;
  monthly_amount: string;
  start_date: string;
  end_date?: string | null;
}

export interface PromoPeriod {
  start_date: string;
  end_date: string;
}

export interface ContractRules {
  fixed_fee: string;
  minimum_fee: string;
  revenue_tiers: RevenueTier[];
  deductions: Deduction[];
  promo_periods: PromoPeriod[];
  priority: string | null;
}

export interface RevenueContract {
  id: number;
  site_id: number;
  contract_no: string;
  version_no: number;
  name: string;
  status: string;
  rules_json: string;
  rules_hash: string;
  effective_date: string | null;
  end_date: string | null;
  signed_at: string | null;
  superseded_by_id: number | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface TrialLine {
  id: number;
  bill_id: number | null;
  period_month: string;
  line_status: string;
  clarify_reason: string | null;
  base_revenue: string;
  cand_revenue: string;
  base_deduction: string;
  cand_deduction: string;
  base_minimum: string;
  cand_minimum: string;
  base_minimum_applied: boolean;
  cand_minimum_applied: boolean;
  base_fixed_fee: string;
  cand_fixed_fee: string;
  base_in_promo: boolean;
  cand_in_promo: boolean;
  base_payable: string;
  cand_payable: string;
  delta: string;
  base_rounding_diff: string;
  cand_rounding_diff: string;
  detail_json: string;
}

export interface ContractTrial {
  id: number;
  site_id: number;
  trial_no: string;
  baseline_contract_id: number;
  candidate_contract_id: number;
  period_start: string;
  period_end: string;
  status: string;
  status_reason: string | null;
  candidate_rules_hash: string;
  created_by: string;
  created_at: string;
  lines: TrialLine[];
}

export interface ContractApproval {
  id: number;
  trial_id: number;
  site_id: number;
  decision: string;
  period_start: string;
  period_end: string;
  approved_delta: string;
  note: string | null;
  supersedes_trial_id: number | null;
  approved_by: string;
  created_at: string;
}

export interface BillContractView {
  bill_id: number;
  bill_no: string;
  original_contract_payable: string | null;
  new_terms_payable: string | null;
  approved_adjustment: string;
  effective_payable: string | null;
  latest_trial_id: number | null;
  latest_trial_status: string | null;
  approved_records: Array<{ trial_no: string; approval_id: number; delta: string; period: string }>;
}

export interface PayableStats {
  site_id: number;
  bill_count_with_contract: number;
  original_contract_payable: string;
  approved_adjustment: string;
  effective_payable: string;
}
