import React, { useEffect, useRef, useState } from 'react';
import { api } from '../../api';
import { AlertTriangle, RefreshCw, Users } from 'lucide-react';

/**
 * Same-window peer comparison, as its own entry (Task28).
 *
 * Deliberately *not* part of the diagnosis payload: the diagnosis is one branch
 * over time, while this asks "how do the other branches I can see compare over
 * the very same window?". Keeping it separate means the diagnosis contract does
 * not change, and the cost is explicit -- one request here fans out to two
 * statistics reads per visible branch, so it runs on a click, not on mount.
 *
 * A branch-bound staff member gets `not_comparable` from the server, and this
 * panel says so in the same words. An empty peer list is never rendered as
 * "nothing else moved".
 */

const PEER_STATUS_LABELS = {
  ok: '可比',
  insufficient_baseline: '基线不足',
  no_data: '无数据',
  zero_baseline: '零基线',
  not_comparable: '不可比',
};

const PEER_STATUS_COLORS = {
  ok: '#15803d',
  insufficient_baseline: '#b45309',
  no_data: '#6b7280',
  zero_baseline: '#b45309',
  not_comparable: '#b45309',
};

const formatChange = (value) => {
  if (value === null || value === undefined) return '—';
  const sign = value > 0 ? '+' : '';
  return `${sign}${value}%`;
};

export default function PeerComparison({ branchId, startDate, endDate }) {
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const requestRef = useRef(0);

  // A comparison belongs to exactly one branch and window. Changing either
  // retires whatever is in flight, so an old answer cannot be painted under the
  // new selection.
  useEffect(() => {
    requestRef.current += 1;
    setResult(null);
    setError('');
    setLoading(false);
  }, [branchId, startDate, endDate]);

  const run = async () => {
    const ticket = ++requestRef.current;
    setLoading(true);
    setError('');
    try {
      const data = await api.getBranchPeers(branchId, startDate, endDate);
      if (ticket === requestRef.current) setResult(data);
    } catch (err) {
      if (ticket === requestRef.current) {
        setError(err.message || '加载同业对比失败。');
        setResult(null);
      }
    } finally {
      if (ticket === requestRef.current) setLoading(false);
    }
  };

  const notComparable = result && result.status === 'not_comparable';

  return (
    <div
      style={{
        background: 'white',
        border: '1px solid var(--surface-border)',
        borderRadius: 'var(--radius-lg)',
        padding: '1.25rem',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: '1rem',
          flexWrap: 'wrap',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 700 }}>
          <Users size={18} /> 同期同业对比
        </div>
        <button
          onClick={run}
          disabled={loading}
          className="btn-outline btn-sm"
          style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
        >
          <RefreshCw size={14} className={loading ? 'spin' : ''} />
          {result ? '重新对比' : '查询同业对比'}
        </button>
      </div>

      {!result && !loading && !error && (
        <div style={{ marginTop: '0.75rem', fontSize: '0.85rem', color: 'var(--text-muted)' }}>
          用与本次诊断相同的时间窗口，比较当前可见门店的可比性。该查询会逐店读取统计，
          按需点击即可；不会修改任何经营数据。
        </div>
      )}

      {loading && (
        <div style={{ marginTop: '0.75rem', fontSize: '0.85rem', color: 'var(--text-muted)' }}>
          正在对比同期同业…
        </div>
      )}

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
            marginTop: '0.75rem',
          }}
        >
          <AlertTriangle size={16} />
          <span>{error}</span>
        </div>
      )}

      {result && (
        <div style={{ marginTop: '0.75rem' }}>
          <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
            {result.target.branch_name}　本期 {result.window.start_date} 至{' '}
            {result.window.end_date}　对比上期 {result.window.baseline_start_date} 至{' '}
            {result.window.baseline_end_date}（按 UTC 日期统计）
          </div>

          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginTop: '0.6rem' }}>
            <span className="badge badge-info" style={{ fontWeight: 500 }}>
              可比 {result.comparable_count} 家
            </span>
            <span className="badge badge-warning" style={{ fontWeight: 500 }}>
              不可比 {result.incomparable_count} 家
            </span>
          </div>

          {notComparable ? (
            <div
              style={{
                display: 'flex',
                gap: '8px',
                alignItems: 'flex-start',
                background: '#fffbeb',
                border: '1px solid #fcd34d',
                borderRadius: '8px',
                padding: '0.9rem 1.1rem',
                fontSize: '0.875rem',
                marginTop: '0.75rem',
              }}
            >
              <AlertTriangle size={16} color="#b45309" style={{ flexShrink: 0, marginTop: '2px' }} />
              <span>{result.summary}</span>
            </div>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table
                style={{
                  width: '100%',
                  borderCollapse: 'collapse',
                  fontSize: '0.85rem',
                  marginTop: '0.75rem',
                }}
              >
                <thead>
                  <tr style={{ textAlign: 'left', color: 'var(--text-muted)' }}>
                    <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--surface-border)' }}>门店</th>
                    <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--surface-border)' }}>判定</th>
                    <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--surface-border)' }}>本期有效天</th>
                    <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--surface-border)' }}>上期有效天</th>
                    <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--surface-border)' }}>订单量变化</th>
                    <th style={{ padding: '6px 8px', borderBottom: '1px solid var(--surface-border)' }}>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {result.peers.map((peer) => (
                    <tr key={peer.branch_id}>
                      <td style={{ padding: '6px 8px', borderBottom: '1px solid #f1f5f9' }}>
                        {peer.branch_name}
                      </td>
                      <td
                        style={{
                          padding: '6px 8px',
                          borderBottom: '1px solid #f1f5f9',
                          fontWeight: 600,
                          color: PEER_STATUS_COLORS[peer.status] || 'inherit',
                        }}
                      >
                        {PEER_STATUS_LABELS[peer.status] || peer.status}
                      </td>
                      <td style={{ padding: '6px 8px', borderBottom: '1px solid #f1f5f9' }}>
                        {peer.current_active_days}
                      </td>
                      <td style={{ padding: '6px 8px', borderBottom: '1px solid #f1f5f9' }}>
                        {peer.baseline_active_days}
                      </td>
                      {/* A dash is the whole point: null means "not comparable",
                          not zero and never -100%. */}
                      <td style={{ padding: '6px 8px', borderBottom: '1px solid #f1f5f9' }}>
                        {formatChange(peer.change_pct)}
                      </td>
                      <td
                        style={{
                          padding: '6px 8px',
                          borderBottom: '1px solid #f1f5f9',
                          color: 'var(--text-muted)',
                        }}
                      >
                        {peer.verdict}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {!notComparable && (
            <div style={{ marginTop: '0.6rem', fontSize: '0.8rem', color: 'var(--text-muted)' }}>
              {result.summary}
            </div>
          )}

          {result.notes.length > 0 && (
            <div style={{ marginTop: '0.5rem', fontSize: '0.8rem', color: 'var(--text-muted)' }}>
              {result.notes.map((note, index) => (
                <div key={index}>· {note}</div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
