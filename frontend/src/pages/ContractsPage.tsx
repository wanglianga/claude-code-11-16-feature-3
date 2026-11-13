import { useEffect, useState } from "react";
import { api, errText } from "../api";
import { useAuth } from "../auth";
import type {
  Site, RevenueContract, ContractRules, ContractTrial, TrialLine,
  BillContractView, PayableStats,
} from "../types";

const CONTRACT_STATUS: Record<string, string> = {
  draft: "草稿", active: "现行", superseded: "已接替", archived: "停用",
};
const TRIAL_STATUS: Record<string, string> = {
  calculated: "可审批",
  stale: "草稿已变更",
  needs_clarification: "待澄清",
  invalidated: "已失效",
  approved: "已批准",
  rejected: "已驳回",
  blocked_overlap: "账期重叠",
  superseded: "已被替代",
};

function emptyRules(): ContractRules {
  return {
    fixed_fee: "0", minimum_fee: "0",
    revenue_tiers: [{ upper_revenue: null, rate: "0.2" }],
    deductions: [], promo_periods: [], priority: null,
  };
}

// ---------------- 结构化规则编辑器（只能填固定字段，无任何代码入口） ----------------
function RuleEditor({ rules, onChange }: {
  rules: ContractRules;
  onChange: (r: ContractRules) => void;
}) {
  const set = (patch: Partial<ContractRules>) => onChange({ ...rules, ...patch });
  return (
    <div className="rules-editor">
      <div className="row">
        <div>
          <label>月固定费用（元）</label>
          <input value={rules.fixed_fee} onChange={(e) => set({ fixed_fee: e.target.value })} />
        </div>
        <div>
          <label>月保底额（元）</label>
          <input value={rules.minimum_fee} onChange={(e) => set({ minimum_fee: e.target.value })} />
        </div>
        <div>
          <label>保底/优惠优先级</label>
          <select
            value={rules.priority ?? ""}
            onChange={(e) => set({ priority: e.target.value === "" ? null : e.target.value })}
          >
            <option value="">留空（重叠时停在待澄清）</option>
            <option value="minimum_first">保底优先（优惠不豁免保底）</option>
            <option value="promo_first">优惠优先（重叠期豁免保底）</option>
          </select>
        </div>
      </div>

      <label>分段（阶梯）比例：上限留空表示开放段，最后一段必须开放</label>
      <table>
        <thead><tr><th>分段上限（元，留空=开放）</th><th>分成比例 0~1</th><th></th></tr></thead>
        <tbody>
          {rules.revenue_tiers.map((t, i) => (
            <tr key={i}>
              <td>
                <input
                  value={t.upper_revenue ?? ""}
                  placeholder={i === rules.revenue_tiers.length - 1 ? "开放" : ""}
                  onChange={(e) => {
                    const tiers = rules.revenue_tiers.map((x, j) =>
                      j === i ? { ...x, upper_revenue: e.target.value === "" ? null : e.target.value } : x);
                    set({ revenue_tiers: tiers });
                  }}
                />
              </td>
              <td>
                <input value={t.rate} onChange={(e) => {
                  const tiers = rules.revenue_tiers.map((x, j) =>
                    j === i ? { ...x, rate: e.target.value } : x);
                  set({ revenue_tiers: tiers });
                }} />
              </td>
              <td>
                {rules.revenue_tiers.length > 1 && (
                  <button className="secondary" onClick={() =>
                    set({ revenue_tiers: rules.revenue_tiers.filter((_, j) => j !== i) })}>
                    删除
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button className="secondary" onClick={() =>
        set({ revenue_tiers: [...rules.revenue_tiers, { upper_revenue: null, rate: "0.1" }] })}>
        新增分段
      </button>

      <label style={{ marginTop: 12 }}>允许抵扣项（按月金额，按生效天数折算）</label>
      {rules.deductions.map((d, i) => (
        <div className="row" key={i} style={{ marginBottom: 6 }}>
          <input placeholder="名称" value={d.name ?? ""} onChange={(e) => {
            const deductions = rules.deductions.map((x, j) => j === i ? { ...x, name: e.target.value } : x);
            set({ deductions });
          }} />
          <input placeholder="月金额" value={d.monthly_amount} onChange={(e) => {
            const deductions = rules.deductions.map((x, j) => j === i ? { ...x, monthly_amount: e.target.value } : x);
            set({ deductions });
          }} />
          <input type="date" value={d.start_date} onChange={(e) => {
            const deductions = rules.deductions.map((x, j) => j === i ? { ...x, start_date: e.target.value } : x);
            set({ deductions });
          }} />
          <input type="date" value={d.end_date ?? ""} onChange={(e) => {
            const deductions = rules.deductions.map((x, j) => j === i ? { ...x, end_date: e.target.value || null } : x);
            set({ deductions });
          }} />
          <button className="secondary" onClick={() =>
            set({ deductions: rules.deductions.filter((_, j) => j !== i) })}>删除</button>
        </div>
      ))}
      <button className="secondary" onClick={() =>
        set({ deductions: [...rules.deductions, { name: "新抵扣", monthly_amount: "0", start_date: "2026-10-01", end_date: null }] })}>
        新增抵扣项
      </button>

      <label style={{ marginTop: 12 }}>优惠期（与保底重叠且未声明优先级时，试算停在待澄清）</label>
      {rules.promo_periods.map((p, i) => (
        <div className="row" key={i} style={{ marginBottom: 6 }}>
          <input type="date" value={p.start_date} onChange={(e) => {
            const promo_periods = rules.promo_periods.map((x, j) => j === i ? { ...x, start_date: e.target.value } : x);
            set({ promo_periods });
          }} />
          <input type="date" value={p.end_date} onChange={(e) => {
            const promo_periods = rules.promo_periods.map((x, j) => j === i ? { ...x, end_date: e.target.value } : x);
            set({ promo_periods });
          }} />
          <button className="secondary" onClick={() =>
            set({ promo_periods: rules.promo_periods.filter((_, j) => j !== i) })}>删除</button>
        </div>
      ))}
      <button className="secondary" onClick={() =>
        set({ promo_periods: [...rules.promo_periods, { start_date: "2026-10-01", end_date: "2026-10-15" }] })}>
        新增优惠期
      </button>
    </div>
  );
}

// ---------------- 试算逐月并排差异 ----------------
function TrialLineTable({ lines }: { lines: TrialLine[] }) {
  return (
    <table>
      <thead><tr>
        <th>账期</th>
        <th className="num">原-分段收益</th>
        <th className="num">新-分段收益</th>
        <th className="num">原-抵扣</th>
        <th className="num">新-抵扣</th>
        <th className="num">原-保底(启用)</th>
        <th className="num">新-保底(启用)</th>
        <th className="num">原应付</th>
        <th className="num">新应付</th>
        <th className="num">差额</th>
        <th className="num">新尾差</th>
        <th>状态/说明</th>
      </tr></thead>
      <tbody>
        {lines.map((ln) => (
          <tr key={ln.id}>
            <td className="mono">{ln.period_month}</td>
            <td className="num">{ln.base_revenue}</td>
            <td className="num">{ln.cand_revenue}</td>
            <td className="num">{ln.base_deduction}</td>
            <td className="num">{ln.cand_deduction}</td>
            <td className="num">{ln.base_minimum}{ln.base_minimum_applied ? " ✓" : ""}</td>
            <td className="num">{ln.cand_minimum}{ln.cand_minimum_applied ? " ✓" : ""}</td>
            <td className="num">{ln.base_payable}</td>
            <td className="num">{ln.cand_payable}</td>
            <td className="num" style={{ color: Number(ln.delta) >= 0 ? "#15803d" : "#b91c1c" }}>{ln.delta}</td>
            <td className="num">{ln.cand_rounding_diff}</td>
            <td>
              {ln.line_status === "needs_clarification" ? (
                <span className="badge suspended">待澄清</span>
              ) : <span className="badge measured">可结算</span>}
              {ln.clarify_reason && <div className="muted">{ln.clarify_reason}</div>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function ContractsPage() {
  const { role } = useAuth();
  const isFinance = role === "finance";
  const [sites, setSites] = useState<Site[]>([]);
  const [siteId, setSiteId] = useState<number | "">("");
  const [contracts, setContracts] = useState<RevenueContract[]>([]);
  const [trials, setTrials] = useState<ContractTrial[]>([]);
  const [openTrial, setOpenTrial] = useState<ContractTrial | null>(null);
  const [stats, setStats] = useState<PayableStats | null>(null);
  const [billViews, setBillViews] = useState<Record<number, BillContractView>>({});
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  // 新建/编辑草稿
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState({
    contract_no: "", name: "", effective_date: "2026-10-01", end_date: "",
    signed_at: "", base_contract_id: "" as number | "",
  });
  const [rules, setRules] = useState<ContractRules>(emptyRules());

  // 发起试算
  const [trialForm, setTrialForm] = useState({
    baseline_contract_id: "" as number | "", candidate_contract_id: "" as number | "",
    period_start: "2026-10-01", period_end: "2026-10-31",
  });

  const load = async () => {
    const { data: s } = await api.get<Site[]>("/api/sites");
    setSites(s);
    const sid = siteId === "" ? undefined : siteId;
    const { data: cs } = await api.get<RevenueContract[]>("/api/contracts", {
      params: sid === undefined ? {} : { site_id: sid },
    });
    setContracts(cs);
    const { data: ts } = await api.get<ContractTrial[]>("/api/contracts/trials", {
      params: sid === undefined ? {} : { site_id: sid },
    });
    setTrials(ts);
    if (sid !== undefined) {
      const { data: st } = await api.get<PayableStats>(`/api/contracts/sites/${sid}/payable-stats`);
      setStats(st);
    } else {
      setStats(null);
    }
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [siteId]);

  const parseRules = (c: RevenueContract): ContractRules =>
    JSON.parse(c.rules_json) as ContractRules;

  const resetForm = () => {
    setEditingId(null);
    setForm({ contract_no: "", name: "", effective_date: "2026-10-01", end_date: "",
      signed_at: "", base_contract_id: "" });
    setRules(emptyRules());
  };

  const startEdit = (c: RevenueContract) => {
    setEditingId(c.id);
    setForm({
      contract_no: c.contract_no, name: c.name,
      effective_date: c.effective_date ?? "", end_date: c.end_date ?? "",
      signed_at: c.signed_at ? c.signed_at.slice(0, 16) : "", base_contract_id: "",
    });
    setRules(parseRules(c));
  };

  const saveContract = async () => {
    setErr(""); setMsg("");
    try {
      const payload = {
        name: form.name,
        effective_date: form.effective_date,
        end_date: form.end_date || null,
        signed_at: form.signed_at ? form.signed_at.replace("T", " ") + ":00" : null,
        rules,
      };
      if (editingId !== null) {
        await api.patch(`/api/contracts/${editingId}`, payload);
        setMsg("草稿已更新；基于旧规则的试算将失去批准资格");
      } else {
        await api.post("/api/contracts", {
          site_id: siteId, ...payload,
          contract_no: form.contract_no,
          base_contract_id: form.base_contract_id === "" ? null : form.base_contract_id,
        });
        setMsg("已创建合同草稿，可反复编辑后再生效/试算");
      }
      resetForm();
      load();
    } catch (e) { setErr(errText(e)); }
  };

  const activate = async (c: RevenueContract) => {
    setErr("");
    try {
      await api.post(`/api/contracts/${c.id}/activate`);
      setMsg(`合同 ${c.contract_no} v${c.version_no} 已生效，旧版本标记为已接替`);
      load();
    } catch (e) { setErr(errText(e)); }
  };

  const createTrial = async () => {
    setErr(""); setMsg("");
    try {
      const { data } = await api.post<ContractTrial>("/api/contracts/trials", {
        baseline_contract_id: trialForm.baseline_contract_id,
        candidate_contract_id: trialForm.candidate_contract_id,
        period_start: trialForm.period_start, period_end: trialForm.period_end,
      });
      setMsg(`试算 ${data.trial_no} 已生成，状态：${TRIAL_STATUS[data.status] ?? data.status}`);
      load();
      setOpenTrial(data);
    } catch (e) { setErr(errText(e)); }
  };

  const refreshTrial = async (t: ContractTrial) => {
    const { data } = await api.post<ContractTrial>(`/api/contracts/trials/${t.id}/refresh`);
    setOpenTrial(data);
    load();
  };

  const openDetail = async (t: ContractTrial) => {
    const { data } = await api.get<ContractTrial>(`/api/contracts/trials/${t.id}`);
    setOpenTrial(data);
  };

  const loadBillView = async (billId: number) => {
    if (billViews[billId]) return;
    const { data } = await api.get<BillContractView>(`/api/contracts/bills/${billId}/view`);
    setBillViews((prev) => ({ ...prev, [billId]: data }));
  };

  const drafts = contracts.filter((c) => c.status === "draft");
  const activeOnes = contracts.filter((c) => c.status === "active");

  if (openTrial) {
    return (
      <TrialDetail
        trial={openTrial}
        contracts={contracts}
        trials={trials}
        onBack={() => { setOpenTrial(null); load(); }}
        onRefresh={refreshTrial}
        isFinance={isFinance}
        onChanged={load}
      />
    );
  }

  return (
    <div>
      <h2 className="page-title">分成合同变更与追溯试算</h2>
      <div className="filter-bar">
        <label>站点</label>
        <select value={siteId} onChange={(e) => setSiteId(e.target.value === "" ? "" : Number(e.target.value))}>
          <option value="">全部（按权限）</option>
          {sites.map((s) => <option key={s.id} value={s.id}>{s.code} {s.name}</option>)}
        </select>
      </div>
      {msg && <div className="ok-msg">{msg}</div>}
      {err && <div className="error">{err}</div>}

      {stats && (
        <div className="panel">
          <h2>站点应付统计（未批准试算不计入）</h2>
          <div className="stat-grid">
            <div className="stat"><div className="k">纳入合同的账单数</div><div className="v">{stats.bill_count_with_contract}</div></div>
            <div className="stat"><div className="k">原合同应付合计</div><div className="v">{stats.original_contract_payable}</div></div>
            <div className="stat"><div className="k">实际批准调整额</div><div className="v warn">{stats.approved_adjustment}</div></div>
            <div className="stat"><div className="k">生效应付合计</div><div className="v">{stats.effective_payable}</div></div>
          </div>
        </div>
      )}

      <div className="panel">
        <h2>合同版本（条款为结构化规则，不可写执行代码；签署时间与实际生效时间分列）</h2>
        <table>
          <thead><tr>
            <th>合同号</th><th>版本</th><th>名称</th><th>状态</th>
            <th>实际生效</th><th>生效结束</th><th>签署时间</th><th>创建人</th><th></th>
          </tr></thead>
          <tbody>
            {contracts.map((c) => (
              <tr key={c.id}>
                <td className="mono">{c.contract_no}</td>
                <td>v{c.version_no}</td>
                <td>{c.name}</td>
                <td><span className={`badge ${c.status === "active" ? "confirmed" : c.status}`}>
                  {CONTRACT_STATUS[c.status] ?? c.status}
                </span></td>
                <td>{c.effective_date ?? "—"}</td>
                <td>{c.end_date ?? "长期"}</td>
                <td>{c.signed_at ? c.signed_at.slice(0, 16).replace("T", " ") : "—"}</td>
                <td>{c.created_by}</td>
                <td>
                  {isFinance && c.status === "draft" && (
                    <span className="inline">
                      <button className="secondary" onClick={() => startEdit(c)}>编辑草稿</button>
                      <button onClick={() => activate(c)}>生效</button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {isFinance && (
        <div className="panel">
          <h2>{editingId !== null ? `编辑草稿 #${editingId}` : "新建合同/变更草稿"}</h2>
          <div className="row">
            <div>
              <label>合同号</label>
              <input value={form.contract_no} disabled={editingId !== null}
                onChange={(e) => setForm({ ...form, contract_no: e.target.value })} />
            </div>
            <div style={{ flex: 1, minWidth: 200 }}>
              <label>名称</label>
              <input style={{ width: "100%" }} value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </div>
            {editingId === null && (
              <div>
                <label>基于已生效版本（变更时选择）</label>
                <select value={form.base_contract_id}
                  onChange={(e) => setForm({ ...form, base_contract_id: e.target.value === "" ? "" : Number(e.target.value) })}>
                  <option value="">首份合同（无基线）</option>
                  {activeOnes.map((c) => <option key={c.id} value={c.id}>
                    {c.contract_no} v{c.version_no}
                  </option>)}
                </select>
              </div>
            )}
          </div>
          <div className="row">
            <div><label>实际生效日期</label><input type="date" value={form.effective_date}
              onChange={(e) => setForm({ ...form, effective_date: e.target.value })} /></div>
            <div><label>生效结束（留空=长期）</label><input type="date" value={form.end_date}
              onChange={(e) => setForm({ ...form, end_date: e.target.value })} /></div>
            <div><label>签署时间（仅留痕）</label><input type="datetime-local" value={form.signed_at}
              onChange={(e) => setForm({ ...form, signed_at: e.target.value })} /></div>
          </div>
          <RuleEditor rules={rules} onChange={setRules} />
          <div className="row" style={{ marginTop: 12 }}>
            <button onClick={saveContract}>{editingId !== null ? "保存草稿编辑" : "创建草稿"}</button>
            {editingId !== null && <button className="secondary" onClick={resetForm}>取消编辑</button>}
          </div>
        </div>
      )}

      {isFinance && (
        <div className="panel">
          <h2>发起追溯试算（固定输入：账单版本 + 新旧规则快照）</h2>
          <div className="row">
            <div>
              <label>原合同（基线，已生效）</label>
              <select value={trialForm.baseline_contract_id}
                onChange={(e) => setTrialForm({ ...trialForm, baseline_contract_id: e.target.value === "" ? "" : Number(e.target.value) })}>
                <option value="">请选择</option>
                {contracts.filter((c) => c.status !== "draft").map((c) => <option key={c.id} value={c.id}>
                  {c.contract_no} v{c.version_no}（{CONTRACT_STATUS[c.status]}）
                </option>)}
              </select>
            </div>
            <div>
              <label>新条款（草稿）</label>
              <select value={trialForm.candidate_contract_id}
                onChange={(e) => setTrialForm({ ...trialForm, candidate_contract_id: e.target.value === "" ? "" : Number(e.target.value) })}>
                <option value="">请选择</option>
                {drafts.map((c) => <option key={c.id} value={c.id}>
                  {c.contract_no} v{c.version_no}
                </option>)}
              </select>
            </div>
            <div><label>期间开始</label><input type="date" value={trialForm.period_start}
              onChange={(e) => setTrialForm({ ...trialForm, period_start: e.target.value })} /></div>
            <div><label>期间结束</label><input type="date" value={trialForm.period_end}
              onChange={(e) => setTrialForm({ ...trialForm, period_end: e.target.value })} /></div>
            <button onClick={createTrial}>发起试算</button>
          </div>
        </div>
      )}

      <div className="panel">
        <h2>试算记录（原试算始终可查看；失效/失配后失去批准资格）</h2>
        <table>
          <thead><tr>
            <th>试算号</th><th>账期</th><th>状态</th><th>原因</th><th>创建人</th><th></th>
          </tr></thead>
          <tbody>
            {trials.map((t) => (
              <tr key={t.id}>
                <td className="mono">{t.trial_no}</td>
                <td className="mono">{t.period_start} ~ {t.period_end}</td>
                <td><span className={`badge ${t.status === "calculated" ? "confirmed" : "suspended"}`}>
                  {TRIAL_STATUS[t.status] ?? t.status}
                </span></td>
                <td className="muted">{t.status_reason ?? ""}</td>
                <td>{t.created_by}</td>
                <td><button className="secondary" onClick={() => openDetail(t)}>查看并排差异</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <BillViewPanel siteId={siteId} views={billViews} onLoad={loadBillView} />
    </div>
  );
}

// ---------------- 试算详情与审批 ----------------
function TrialDetail({ trial, contracts, trials, onBack, onRefresh, isFinance, onChanged }: {
  trial: ContractTrial;
  contracts: RevenueContract[];
  trials: ContractTrial[];
  onBack: () => void;
  onRefresh: (t: ContractTrial) => void;
  isFinance: boolean;
  onChanged: () => void;
}) {
  const [approveStart, setApproveStart] = useState(trial.period_start);
  const [approveEnd, setApproveEnd] = useState(trial.period_end);
  const [note, setNote] = useState("");
  const [supersede, setSupersede] = useState<number | "">("");
  const [err, setErr] = useState("");
  const cname = (id: number) => {
    const c = contracts.find((x) => x.id === id);
    return c ? `${c.contract_no} v${c.version_no}` : `#${id}`;
  };
  const totalDelta = trial.lines.reduce((acc, l) => acc + Number(l.delta), 0);
  const overlapApproved = trials.filter((t) =>
    t.id !== trial.id && t.status === "approved");

  const doApprove = async () => {
    setErr("");
    try {
      await api.post(`/api/contracts/trials/${trial.id}/approve`, {
        period_start: approveStart, period_end: approveEnd,
        note: note || null,
        supersedes_trial_id: supersede === "" ? null : supersede,
      });
      onBack();
    } catch (e) { setErr(errText(e)); }
  };
  const doReject = async () => {
    setErr("");
    try {
      await api.post(`/api/contracts/trials/${trial.id}/reject`, { note: note || "驳回" });
      onBack();
    } catch (e) { setErr(errText(e)); }
  };

  return (
    <div>
      <button className="secondary" onClick={onBack}>← 返回试算列表</button>
      <h2 className="page-title">试算 {trial.trial_no}
        <span className="badge suspended" style={{ marginLeft: 10 }}>
          {TRIAL_STATUS[trial.status] ?? trial.status}
        </span>
      </h2>
      <div className="panel">
        <div className="row">
          <span>原合同：<b>{cname(trial.baseline_contract_id)}</b></span>
          <span>新条款：<b>{cname(trial.candidate_contract_id)}</b></span>
          <span>账期：<b className="mono">{trial.period_start} ~ {trial.period_end}</b></span>
          <span>差额合计：<b style={{ color: totalDelta >= 0 ? "#15803d" : "#b91c1c" }}>{totalDelta.toFixed(4)}</b></span>
        </div>
        {trial.status_reason && <div className="error" style={{ marginTop: 10 }}>{trial.status_reason}</div>}
        <div className="muted" style={{ marginTop: 8 }}>
          运算顺序：① 分段（阶梯）收益 → ② 扣允许抵扣 → ③ 与保底比较；日级金额月度统一舍入，尾差单独列出。
        </div>
        <button className="secondary" style={{ marginTop: 10 }} onClick={() => onRefresh(trial)}>
          重新校验固定输入（草稿变更/电量更正/账单版本）
        </button>
      </div>

      <div className="panel">
        <h2>逐月新旧并排差异</h2>
        <TrialLineTable lines={trial.lines} />
      </div>

      {isFinance && (
        <div className="panel">
          <h2>审批（仅与当前草稿一致、输入未变且无待澄清月份的试算可批准）</h2>
          {err && <div className="error">{err}</div>}
          <div className="row">
            <div><label>批准期间开始</label><input type="date" value={approveStart}
              onChange={(e) => setApproveStart(e.target.value)} /></div>
            <div><label>批准期间结束</label><input type="date" value={approveEnd}
              onChange={(e) => setApproveEnd(e.target.value)} /></div>
            <div style={{ flex: 1, minWidth: 220 }}>
              <label>审批意见</label>
              <input style={{ width: "100%" }} value={note} onChange={(e) => setNote(e.target.value)} />
            </div>
          </div>
          {overlapApproved.length > 0 && (
            <div className="field" style={{ marginTop: 10 }}>
              <label>相同账期已有批准方案，须确定采用关系（被替代的试算）</label>
              <select value={supersede} onChange={(e) => setSupersede(e.target.value === "" ? "" : Number(e.target.value))}>
                <option value="">请选择被替代试算</option>
                {overlapApproved.map((t) => <option key={t.id} value={t.id}>{t.trial_no}</option>)}
              </select>
            </div>
          )}
          <div className="row" style={{ marginTop: 12 }}>
            <button onClick={doApprove}
              disabled={trial.status !== "calculated"}>
              按指定期间批准差额
            </button>
            <button className="danger" onClick={doReject}
              disabled={["approved", "rejected", "superseded"].includes(trial.status)}>
              驳回
            </button>
            {trial.status !== "calculated" && (
              <span className="muted">当前状态不可批准：{trial.status_reason ?? TRIAL_STATUS[trial.status]}</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------- 账单三数视图 ----------------
function BillViewPanel({ siteId, views, onLoad }: {
  siteId: number | "";
  views: Record<number, BillContractView>;
  onLoad: (billId: number) => void;
}) {
  const [bills, setBills] = useState<Array<{ id: number; bill_no: string }>>([]);
  useEffect(() => {
    api.get("/api/bills", { params: siteId === "" ? {} : { site_id: siteId } })
      .then(({ data }) => setBills(data));
  }, [siteId]);
  return (
    <div className="panel">
      <h2>账单合同视图（原合同结果 / 新条款结果 / 实际批准调整额）</h2>
      <table>
        <thead><tr>
          <th>账单号</th><th className="num">原合同应付</th><th className="num">新条款应付</th>
          <th className="num">实际批准调整</th><th className="num">生效应付</th><th>最新试算状态</th><th></th>
        </tr></thead>
        <tbody>
          {bills.map((b) => {
            const v = views[b.id];
            return (
              <tr key={b.id}>
                <td className="mono">{b.bill_no}</td>
                <td className="num">{v?.original_contract_payable ?? "—"}</td>
                <td className="num">{v?.new_terms_payable ?? "—"}</td>
                <td className="num">{v?.approved_adjustment ?? "—"}</td>
                <td className="num">{v?.effective_payable ?? "—"}</td>
                <td>{v ? (TRIAL_STATUS[v.latest_trial_status ?? ""] ?? v.latest_trial_status) : "—"}</td>
                <td><button className="secondary" onClick={() => onLoad(b.id)}>查询</button></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
