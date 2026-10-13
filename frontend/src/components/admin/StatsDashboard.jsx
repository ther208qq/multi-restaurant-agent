import React, { useState, useEffect } from 'react';
import { api } from '../../api';
import { BarChart3, RefreshCw, AlertCircle, TrendingUp } from 'lucide-react';

const RANGE_OPTIONS = [
  { value: 7, label: '最近 7 天' },
  { value: 30, label: '最近 30 天' },
  { value: 90, label: '最近 90 天' },
];

function isoDate(date) {
  return date.toISOString().slice(0, 10);
}

function rangeStart(days) {
  const end = new Date();
  const start = new Date(end);
  start.setDate(start.getDate() - (days - 1));
  return { start: isoDate(start), end: isoDate(end) };
}

function money(value) {
  return `¥${Number(value || 0).toFixed(2)}`;
}

function KpiCard({ label, value, hint }) {
  return (
    <div
      style={{
        background: 'white',
        border: '1px solid var(--surface-border)',
        borderRadius: 'var(--radius-lg)',
        padding: '1rem 1.25rem',
        minWidth: '150px',
        flex: '1 1 150px',
      }}
    >
      <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)', fontWeight: 600 }}>{label}</div>
      <div style={{ fontSize: '1.5rem', fontWeight: 700, marginTop: '4px' }}>{value}</div>
      {hint && <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>{hint}</div>}
    </div>
  );
}

export default function StatsDashboard({ currentStaff, branches }) {
  const initial = rangeStart(7);
  const [selectedBranchId, setSelectedBranchId] = useState(
    currentStaff.branch_id || (branches[0] ? branches[0].id : 1)
  );
  const [startDate, setStartDate] = useState(initial.start);
  const [endDate, setEndDate] = useState(initial.end);
  const [top, setTop] = useState(10);
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const fetchStats = async () => {
    setLoading(true);
    setError('');
    try {
      const bId = currentStaff.branch_id || selectedBranchId;
      const data = await api.getStats(bId, startDate, endDate, top);
      setStats(data);
    } catch (err) {
      setError(err.message || '加载经营统计失败。');
      setStats(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchStats();
  }, [selectedBranchId, startDate, endDate, top]);

  const applyRange = (days) => {
    const { start, end } = rangeStart(days);
    setStartDate(start);
    setEndDate(end);
  };

  const topItems = stats ? stats.top_items || [] : [];
  const peakQuantity = topItems.reduce((max, row) => Math.max(max, row.quantity), 0);
  const daily = stats ? [...(stats.daily || [])].reverse() : [];

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
              <label style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-muted)' }}>门店：</label>
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
            <label style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-muted)' }}>日期：</label>
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

          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <label style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-muted)' }}>排行：</label>
            <select
              value={top}
              onChange={(e) => setTop(Number(e.target.value))}
              style={{ padding: '6px 12px', borderRadius: '6px', border: '1px solid var(--surface-border)' }}
            >
              <option value={5}>前 5 名</option>
              <option value={10}>前 10 名</option>
              <option value={20}>前 20 名</option>
            </select>
          </div>
        </div>

        <button
          onClick={fetchStats}
          disabled={loading}
          className="btn-outline btn-sm"
          style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
        >
          <RefreshCw size={14} className={loading ? 'spin' : ''} /> 刷新
        </button>
      </div>

      {error && (
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center', background: '#fee2e2', color: '#b91c1c', padding: '10px 14px', borderRadius: '8px', fontSize: '0.875rem' }}>
          <AlertCircle size={16} />
          <span>{error}</span>
        </div>
      )}

      {!stats ? (
        <div style={{ textAlign: 'center', padding: '3rem', color: 'var(--text-muted)' }}>
          {loading ? '正在统计…' : '暂无统计数据。'}
        </div>
      ) : (
        <>
          <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap' }}>
            <KpiCard label="订单量" value={stats.order_count} hint="不含已取消" />
            <KpiCard label="销售额" value={money(stats.revenue)} hint="不含已取消" />
            <KpiCard label="客单价" value={money(stats.average_order_value)} />
            <KpiCard label="销量（份）" value={stats.units_sold} />
            <KpiCard label="取消订单" value={stats.cancelled_count} hint="不计入销售额" />
          </div>

          <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
            {stats.branch_name}　{stats.start_date} 至 {stats.end_date}（按 UTC 日期统计）
          </div>

          <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap', alignItems: 'flex-start' }}>
            <div style={{ flex: '1 1 420px', background: 'white', borderRadius: 'var(--radius-lg)', border: '1px solid var(--surface-border)' }}>
              <div style={{ padding: '0.9rem 1.25rem', borderBottom: '1px solid var(--surface-border)', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '8px' }}>
                <TrendingUp size={16} /> 菜品销量排行
              </div>
              {topItems.length === 0 ? (
                <div style={{ padding: '2rem', textAlign: 'center', color: 'var(--text-muted)' }}>
                  该区间没有销售记录。
                </div>
              ) : (
                <div style={{ padding: '0.75rem 1.25rem', display: 'flex', flexDirection: 'column', gap: '0.6rem' }}>
                  {topItems.map((item, index) => (
                    <div key={item.menu_item_id}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.85rem' }}>
                        <span>
                          {index + 1}. {item.name}
                        </span>
                        <span style={{ color: 'var(--text-muted)' }}>
                          {item.quantity} 份 · {money(item.revenue)} · {item.order_count} 单
                        </span>
                      </div>
                      <div style={{ height: '6px', background: 'var(--surface-border)', borderRadius: '3px', marginTop: '4px' }}>
                        <div
                          style={{
                            width: `${peakQuantity ? Math.round((item.quantity / peakQuantity) * 100) : 0}%`,
                            height: '100%',
                            background: 'var(--primary)',
                            borderRadius: '3px',
                          }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div style={{ flex: '1 1 380px', overflowX: 'auto', background: 'white', borderRadius: 'var(--radius-lg)', border: '1px solid var(--surface-border)' }}>
              <div style={{ padding: '0.9rem 1.25rem', borderBottom: '1px solid var(--surface-border)', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '8px' }}>
                <BarChart3 size={16} /> 每日明细
              </div>
              {daily.length === 0 ? (
                <div style={{ padding: '2rem', textAlign: 'center', color: 'var(--text-muted)' }}>
                  该区间没有订单。
                </div>
              ) : (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>日期</th>
                      <th>订单量</th>
                      <th>销售额</th>
                      <th>取消</th>
                    </tr>
                  </thead>
                  <tbody>
                    {daily.map((point) => (
                      <tr key={point.date}>
                        <td style={{ whiteSpace: 'nowrap' }}>{point.date}</td>
                        <td>{point.order_count}</td>
                        <td>{money(point.revenue)}</td>
                        <td>{point.cancelled_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
