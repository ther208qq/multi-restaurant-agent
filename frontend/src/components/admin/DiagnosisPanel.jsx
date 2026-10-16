import React, { useState, useEffect, useRef } from 'react';
import { api } from '../../api';
import {
  DEBOUNCE_MS,
  cacheKey,
  cacheTtlMs,
  createDebouncedScheduler,
  createDiagnosisCache,
  createRequestGuard,
  isCacheable,
  staffScopeId,
} from './diagnosisQuery';
import {
  Stethoscope,
  RefreshCw,
  AlertCircle,
  AlertTriangle,
  Info,
  CheckCircle2,
} from 'lucide-react';

const RANGE_OPTIONS = [
  { value: 7, label: '最近 7 天' },
  { value: 30, label: '最近 30 天' },
  { value: 90, label: '最近 90 天' },
];

const SEVERITY_LABELS = { warning: '需关注', critical: '严重' };
const DIRECTION_LABELS = { down: '下降', up: '上升' };
const SOURCE_LABELS = {
  llm: '模型分析',
  rule: '规则式归因（模型不可用时使用）',
};

function isoDate(date) {
  return date.toISOString().slice(0, 10);
}

function rangeStart(days) {
  const end = new Date();
  const start = new Date(end);
  start.setDate(start.getDate() - (days - 1));
  return { start: isoDate(start), end: isoDate(end) };
}

/** The unit a change is measured in -- percentage points are not percents. */
function changeUnit(metric) {
  return metric === 'cancelled_rate' ? '个百分点' : '%';
}

function formatAmount(metric, value) {
  const number = Number(value || 0);
  if (metric === 'revenue' || metric === 'average_order_value') {
    return `¥${number.toFixed(2)}`;
  }
  return String(number);
}

/**
 * Dish findings carry no name field: the Service labels every one of them
 * "菜品销量" and puts the dish name inside the evidence labels, so two dish cards
 * would otherwise be headed with identical words. Lift the name back out.
 */
function findingTitle(finding) {
  if (finding.menu_item_id === null || finding.menu_item_id === undefined) {
    return finding.label;
  }
  const named = finding.evidence.find((item) => /^「.+?」/.test(item.label || ''));
  const match = named && named.label.match(/^「(.+?)」/);
  return match ? `${finding.label}：${match[1]}` : finding.label;
}

function signed(value, unit) {
  const number = Number(value || 0);
  const sign = number > 0 ? '+' : '';
  return `${sign}${number.toFixed(2)}${unit}`;
}

// The only object-shaped evidence value today is the cancellation split, whose
// keys come from the Service in English. Anything unknown prints as-is.
const VALUE_KEY_LABELS = {
  customer: '顾客',
  staff: '员工',
  unattributed: '未归属',
  total: '合计',
};

/**
 * Evidence values arrive as numbers, strings, lists (a daily series) or objects
 * (a cancellation split). ``null`` is not an error: the Service uses it to say
 * "this fact is undefined here", and it must read as that rather than as a zero.
 */
function EvidenceValue({ value }) {
  if (value === null || value === undefined) {
    return <span style={{ color: 'var(--text-muted)', fontStyle: 'italic' }}>无数据</span>;
  }
  if (Array.isArray(value)) {
    return <span>{value.map((v) => (v === null ? '—' : v)).join('、')}</span>;
  }
  if (typeof value === 'object') {
    return (
      <span>
        {Object.entries(value)
          .map(([key, v]) => `${VALUE_KEY_LABELS[key] || key}: ${v}`)
          .join('　')}
      </span>
    );
  }
  return <span>{String(value)}</span>;
}

function SeverityBadge({ severity }) {
  const critical = severity === 'critical';
  return (
    <span
      className={`badge ${critical ? 'badge-danger' : 'badge-warning'}`}
      style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}
    >
      <AlertTriangle size={12} /> {SEVERITY_LABELS[severity] || severity}
    </span>
  );
}

function ClaimList({ icon, title, claims, emptyText }) {
  return (
    <div style={{ marginTop: '0.75rem' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '6px',
          fontSize: '0.85rem',
          fontWeight: 600,
          color: 'var(--text-muted)',
        }}
      >
        {icon} {title}
      </div>
      {claims.length === 0 ? (
        <div style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginTop: '4px' }}>
          {emptyText}
        </div>
      ) : (
        <ul style={{ margin: '6px 0 0 0', paddingLeft: '1.1rem', fontSize: '0.875rem' }}>
          {claims.map((claim, index) => (
            <li key={index} style={{ marginBottom: '4px' }}>
              {claim.text}
              {claim.evidence_refs.length > 0 && (
                <span
                  style={{
                    marginLeft: '6px',
                    fontSize: '0.75rem',
                    color: 'var(--text-muted)',
                    fontFamily: 'monospace',
                  }}
                >
                  [{claim.evidence_refs.join(' ')}]
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FindingCard({ finding }) {
  const unit = changeUnit(finding.metric);
  const down = finding.direction === 'down';
  return (
    <div
      style={{
        background: 'white',
        border: '1px solid var(--surface-border)',
        borderLeft: `4px solid ${finding.severity === 'critical' ? '#ef4444' : '#f59e0b'}`,
        borderRadius: 'var(--radius-lg)',
        padding: '1rem 1.25rem',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
          <strong style={{ fontSize: '1rem' }}>{findingTitle(finding)}</strong>
          <SeverityBadge severity={finding.severity} />
        </div>
        <div
          style={{
            fontSize: '1.25rem',
            fontWeight: 700,
            color: down ? '#ef4444' : '#f59e0b',
          }}
        >
          {signed(finding.change_pct, unit)}
        </div>
      </div>

      <div style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginTop: '4px' }}>
        {DIRECTION_LABELS[finding.direction] || finding.direction}　本期{' '}
        {formatAmount(finding.metric, finding.current)} 上期{' '}
        {formatAmount(finding.metric, finding.previous)}
      </div>

      <div style={{ marginTop: '0.75rem' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
            fontSize: '0.85rem',
            fontWeight: 600,
            color: 'var(--text-muted)',
          }}
        >
          <Info size={14} /> 证据
        </div>
        {/* A daily-series evidence value is one number per day in the window, so
            a 90-day range produces a very wide row -- scroll rather than clip. */}
        <div style={{ overflowX: 'auto' }}>
          <table className="data-table" style={{ marginTop: '4px' }}>
            <tbody>
              {finding.evidence.map((item) => (
                <tr key={item.ref}>
                  <td style={{ width: '90px', fontFamily: 'monospace', fontSize: '0.75rem' }}>
                    {item.ref}
                  </td>
                  <td style={{ whiteSpace: 'nowrap' }}>{item.label}</td>
                  <td>
                    <EvidenceValue value={item.value} />
                  </td>
                  <td style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>{item.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

/**
 * Deliberately at module scope: the panel is unmounted whenever the staff member
 * switches tabs, and a cache that died with it would only ever help within one
 * visit. Entries are keyed by caller as well as by branch and window, so the
 * map surviving a logout cannot hand one staff member another's answer -- see
 * ``staffScopeId``.
 */
const diagnosisCache = createDiagnosisCache();

/**
 * Store-anomaly diagnosis tab.
 *
 * Three outcomes are rendered as three different things, and that separation is
 * the point of the panel: a period that was judged clean ("本期未发现异常"), a
 * period that could not be judged at all ("数据不足，暂不判定"), and a period
 * with findings. Showing a blank page or a fabricated -100% for either of the
 * first two is exactly the failure this feature exists to avoid.
 *
 * Asking the server is not free -- a branch with findings runs the Agent, one
 * model call per request -- so how often this panel asks is a cost decision, not
 * a style one. The rules for that live in ``diagnosisQuery``: a debounce so the
 * two date inputs are one request, a ticket so a late answer cannot overwrite a
 * newer one, and a cache so re-reading a branch does not ask again.
 */
export default function DiagnosisPanel({ currentStaff, branches }) {
  const initial = rangeStart(7);
  const [selectedBranchId, setSelectedBranchId] = useState(
    currentStaff.branch_id || (branches[0] ? branches[0].id : 1)
  );
  const [startDate, setStartDate] = useState(initial.start);
  const [endDate, setEndDate] = useState(initial.end);
  const [result, setResult] = useState(null);
  // True from the first paint: the debounced query has not gone out yet, and
  // "暂无诊断结果" would be a lie for the 400ms before it does.
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const guardRef = useRef();
  const schedulerRef = useRef();
  if (!guardRef.current) guardRef.current = createRequestGuard();
  if (!schedulerRef.current) schedulerRef.current = createDebouncedScheduler(DEBOUNCE_MS);
  const guard = guardRef.current;
  const scheduler = schedulerRef.current;

  const branchId = currentStaff.branch_id || selectedBranchId;

  const runQuery = async ({ force = false } = {}) => {
    // Every query takes a ticket, whether or not it ends up asking the server.
    // Answering from the cache has to retire what is still in flight, or a slow
    // earlier request would land on top of the answer just shown.
    const ticket = guard.begin();
    const key = cacheKey({
      scopeId: staffScopeId(currentStaff),
      branchId,
      startDate,
      endDate,
    });

    if (!force) {
      const cached = diagnosisCache.read(key);
      if (cached !== undefined) {
        setResult(cached);
        setError('');
        setLoading(false);
        return;
      }
    }

    setLoading(true);
    setError('');
    try {
      const data = await api.getDiagnosis(branchId, startDate, endDate);
      if (!guard.isCurrent(ticket)) return;
      if (isCacheable(data)) {
        diagnosisCache.write(key, data, cacheTtlMs(endDate, isoDate(new Date())));
      }
      setResult(data);
    } catch (err) {
      // A superseded failure is not this selection's failure.
      if (!guard.isCurrent(ticket)) return;
      setError(err.message || '加载经营诊断失败。');
      setResult(null);
    } finally {
      if (guard.isCurrent(ticket)) setLoading(false);
    }
  };

  // Debounced, because the start and end date are two separate inputs: adjusting
  // one range by hand would otherwise be two model runs. The cleanup restarts
  // the wait on every change and cancels it on unmount -- the latter is also
  // what stops React StrictMode's double mount from sending the request twice.
  //
  // The dependencies are the three primitives and nothing else: anything that
  // changes identity on each parent render would restart the timer forever and
  // the query would never fire.
  useEffect(() => {
    // Retire in-flight work the moment the selection changes, so a response for
    // the previous selection cannot be painted under the new one during the
    // wait.
    guard.invalidate();
    scheduler.schedule(() => runQuery());
    return () => scheduler.cancel();
  }, [selectedBranchId, startDate, endDate]);

  // Nothing may write to the state of a panel that is gone.
  useEffect(() => () => guard.invalidate(), []);

  const refresh = () => {
    scheduler.cancel(); // a pending debounced query would only duplicate this
    runQuery({ force: true });
  };

  const applyRange = (days) => {
    const { start, end } = rangeStart(days);
    setStartDate(start);
    setEndDate(end);
  };

  const analysis = result ? result.analysis : null;
  const findings = result ? result.findings || [] : [];
  const insufficient = result && result.status === 'insufficient_data';

  return (
    <div className="admin-panel" style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: '1rem',
          flexWrap: 'wrap',
          background: 'white',
          padding: '1rem 1.25rem',
          borderRadius: 'var(--radius-lg)',
          border: '1px solid var(--surface-border)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '1rem', flexWrap: 'wrap' }}>
          {!currentStaff.branch_id && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <label style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-muted)' }}>
                门店：
              </label>
              <select
                value={selectedBranchId}
                onChange={(e) => setSelectedBranchId(Number(e.target.value))}
                style={{ padding: '6px 12px', borderRadius: '6px', border: '1px solid var(--surface-border)' }}
              >
                {branches.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <label style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-muted)' }}>
              区间：
            </label>
            <input
              type="date"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
              style={{ padding: '6px 10px', borderRadius: '6px', border: '1px solid var(--surface-border)' }}
            />
            <span style={{ color: 'var(--text-muted)' }}>至</span>
            <input
              type="date"
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
              style={{ padding: '6px 10px', borderRadius: '6px', border: '1px solid var(--surface-border)' }}
            />
          </div>

          <div style={{ display: 'flex', gap: '6px' }}>
            {RANGE_OPTIONS.map((option) => (
              <button
                key={option.value}
                onClick={() => applyRange(option.value)}
                className="btn-outline btn-sm"
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>

        <button
          onClick={refresh}
          disabled={loading}
          className="btn-outline btn-sm"
          style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
        >
          <RefreshCw size={14} className={loading ? 'spin' : ''} /> 刷新
        </button>
      </div>

      {error && (
        <div
          style={{
            display: 'flex',
            gap: '8px',
            alignItems: 'center',
            background: '#fee2e2',
            color: '#b91c1c',
            padding: '10px 14px',
            borderRadius: '8px',
            fontSize: '0.875rem',
          }}
        >
          <AlertCircle size={16} />
          <span>{error}</span>
        </div>
      )}

      {!result ? (
        <div style={{ textAlign: 'center', padding: '3rem', color: 'var(--text-muted)' }}>
          {loading ? '正在诊断…' : '暂无诊断结果。'}
        </div>
      ) : (
        <>
          <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
            {result.branch_name}　本期 {result.current_period.start_date} 至{' '}
            {result.current_period.end_date}　对比上期 {result.baseline_period.start_date} 至{' '}
            {result.baseline_period.end_date}（按 UTC 日期统计）
          </div>

          {insufficient && (
            <div
              style={{
                background: '#fffbeb',
                border: '1px solid #fcd34d',
                borderRadius: 'var(--radius-lg)',
                padding: '1.25rem',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 700 }}>
                <AlertTriangle size={18} color="#b45309" /> 数据不足，暂不判定
              </div>
              <ul style={{ margin: '0.75rem 0 0 0', paddingLeft: '1.2rem', fontSize: '0.875rem' }}>
                {result.notes.map((note, index) => (
                  <li key={index} style={{ marginBottom: '4px' }}>
                    {note}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {!insufficient && findings.length === 0 && (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
                background: '#f0fdf4',
                border: '1px solid #86efac',
                borderRadius: 'var(--radius-lg)',
                padding: '1.25rem',
                fontWeight: 600,
              }}
            >
              <CheckCircle2 size={18} color="#15803d" />
              本期未发现异常（已与上期对比，各项指标变化均在阈值内）。
            </div>
          )}

          {/* With no findings the analysis block below already lists these same
              notes as 待核查事项 (the Agent copies them verbatim), so showing the
              box too would print every line twice. */}
          {result.notes.length > 0 && !insufficient && findings.length > 0 && (
            <div
              style={{
                background: '#f8fafc',
                border: '1px solid var(--surface-border)',
                borderRadius: 'var(--radius-lg)',
                padding: '0.9rem 1.25rem',
                fontSize: '0.85rem',
                color: 'var(--text-muted)',
              }}
            >
              {result.notes.map((note, index) => (
                <div key={index}>· {note}</div>
              ))}
            </div>
          )}

          {findings.map((finding) => (
            <FindingCard
              key={`${finding.metric}-${finding.menu_item_id ?? 'all'}`}
              finding={finding}
            />
          ))}

          {analysis && analysis.status !== 'insufficient_data' && (
            <div
              style={{
                background: 'white',
                border: '1px solid var(--surface-border)',
                borderRadius: 'var(--radius-lg)',
                padding: '1.25rem',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 700 }}>
                <Stethoscope size={18} /> 原因分析与建议
                <span className="badge badge-info" style={{ fontWeight: 500 }}>
                  {SOURCE_LABELS[analysis.source] || analysis.source}
                </span>
                {analysis.dropped > 0 && (
                  <span className="badge badge-warning" style={{ fontWeight: 500 }}>
                    已丢弃 {analysis.dropped} 条无证据支撑的表述
                  </span>
                )}
              </div>

              <ClaimList
                icon={<CheckCircle2 size={14} />}
                title="已确认事实"
                claims={analysis.facts}
                emptyText="本期没有已确认的异常事实。"
              />
              <ClaimList
                icon={<Info size={14} />}
                title="可能原因"
                claims={analysis.causes}
                emptyText="现有证据不足以给出原因，不做推测。"
              />
              <ClaimList
                icon={<AlertTriangle size={14} />}
                title="待核查事项"
                claims={analysis.checks}
                emptyText="没有需要额外核实的事项。"
              />

              <div
                style={{
                  marginTop: '0.9rem',
                  paddingTop: '0.75rem',
                  borderTop: '1px solid var(--surface-border)',
                  fontSize: '0.75rem',
                  color: 'var(--text-muted)',
                }}
              >
                本页只做诊断与建议，不会自动修改价格、促销、库存或订单状态。
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
