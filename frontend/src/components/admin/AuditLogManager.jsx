import React, { useState, useEffect } from 'react';
import { api } from '../../api';
import { History, RefreshCw, AlertCircle } from 'lucide-react';

const ACTION_OPTIONS = [
  { value: '', label: '全部操作' },
  { value: 'order.created', label: '下单' },
  { value: 'order.status_changed', label: '订单状态变更' },
  { value: 'reservation.created', label: '新建预订' },
  { value: 'reservation.status_changed', label: '预订状态变更' },
  { value: 'reservation.rescheduled', label: '预改约' },
  { value: 'reservation.table_reassigned', label: '改派餐桌' },
  { value: 'stock.updated', label: '库存调整' },
  { value: 'menu_item.created', label: '新建菜品' },
  { value: 'menu_item.updated', label: '修改菜品' },
  { value: 'menu_category.created', label: '新建菜品分类' },
  { value: 'branch.created', label: '新建门店' },
  { value: 'branch.updated', label: '修改门店' },
  { value: 'table.created', label: '新建餐桌' },
  { value: 'table.updated', label: '修改餐桌' },
];

const ACTOR_LABELS = { staff: '员工', customer: '顾客', system: '系统' };

const ROLE_LABELS = { admin: '管理员', staff: '员工' };

// "from → to" for status/stock changes, plain values otherwise.
function formatDetailValue(value) {
  if (Array.isArray(value)) {
    return value.map((v) => (v === null ? '—' : String(v))).join(' → ');
  }
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN');
}

export default function AuditLogManager({ currentStaff, branches }) {
  const [logs, setLogs] = useState([]);
  const [selectedBranchId, setSelectedBranchId] = useState(
    currentStaff.branch_id || (branches[0] ? branches[0].id : 1)
  );
  const [actionFilter, setActionFilter] = useState('');
  const [limit, setLimit] = useState(50);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const fetchLogs = async () => {
    setLoading(true);
    setError('');
    try {
      const bId = currentStaff.branch_id || selectedBranchId;
      const data = await api.getAuditLogs(bId, actionFilter || null, null, limit);
      setLogs(data);
    } catch (err) {
      setError(err.message || '加载操作日志失败。');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchLogs();
  }, [selectedBranchId, actionFilter, limit]);

  return (
    <div className="admin-panel" style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: '1rem',
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
            <label style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-muted)' }}>操作：</label>
            <select
              value={actionFilter}
              onChange={(e) => setActionFilter(e.target.value)}
              style={{ padding: '6px 12px', borderRadius: '6px', border: '1px solid var(--surface-border)' }}
            >
              {ACTION_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <label style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-muted)' }}>条数：</label>
            <select
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
              style={{ padding: '6px 12px', borderRadius: '6px', border: '1px solid var(--surface-border)' }}
            >
              <option value={50}>最近 50 条</option>
              <option value={100}>最近 100 条</option>
              <option value={200}>最近 200 条</option>
            </select>
          </div>
        </div>

        <button
          onClick={fetchLogs}
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

      {loading && logs.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '3rem', color: 'var(--text-muted)' }}>
          正在加载操作日志…
        </div>
      ) : logs.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '3rem', background: 'white', borderRadius: 'var(--radius-lg)', border: '1px solid var(--surface-border)', color: 'var(--text-muted)' }}>
          <History size={40} style={{ margin: '0 auto 8px', opacity: 0.4 }} />
          <p>没有符合筛选条件的操作记录。</p>
        </div>
      ) : (
        <div style={{ overflowX: 'auto', background: 'white', borderRadius: 'var(--radius-lg)', border: '1px solid var(--surface-border)' }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>操作者</th>
                <th>操作</th>
                <th>对象</th>
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              {logs.map((log) => (
                <tr key={log.id}>
                  <td style={{ whiteSpace: 'nowrap', fontSize: '0.8rem' }}>{formatTime(log.created_at)}</td>
                  <td>
                    <div style={{ fontWeight: 600 }}>{log.actor_name || '—'}</div>
                    <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                      {ACTOR_LABELS[log.actor_type] || log.actor_type}
                      {log.actor_role ? `（${ROLE_LABELS[log.actor_role] || log.actor_role}）` : ''}
                      {log.actor_id ? ` #${log.actor_id}` : ''}
                    </div>
                  </td>
                  <td>
                    <span className="badge badge-info">{log.action_label}</span>
                  </td>
                  <td style={{ whiteSpace: 'nowrap', fontSize: '0.85rem' }}>
                    {log.entity_label}
                    {log.entity_id ? ` #${log.entity_id}` : ''}
                  </td>
                  <td style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                    {Object.entries(log.details || {}).length === 0
                      ? '—'
                      : Object.entries(log.details).map(([key, value]) => (
                          <div key={key}>
                            <code style={{ color: 'var(--text-muted)' }}>{key}</code>:{' '}
                            {formatDetailValue(value)}
                          </div>
                        ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
