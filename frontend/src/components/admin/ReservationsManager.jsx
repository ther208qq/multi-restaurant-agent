import React, { useState, useEffect } from 'react';
import { api } from '../../api';
import { Calendar, RefreshCw, CheckCircle2, UserCheck, XCircle, Edit3, AlertCircle, X } from 'lucide-react';

export default function ReservationsManager({ currentStaff, branches }) {
  const [reservations, setReservations] = useState([]);
  const [selectedBranchId, setSelectedBranchId] = useState(
    currentStaff.branch_id || (branches[0] ? branches[0].id : 1)
  );
  const [statusFilter, setStatusFilter] = useState('');
  const [dateFilter, setDateFilter] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // Table Reassignment Modal State
  const [reassignModalRes, setReassignModalRes] = useState(null);
  const [branchTables, setBranchTables] = useState([]);
  const [selectedNewTableId, setSelectedNewTableId] = useState('');
  const [reassignLoading, setReassignLoading] = useState(false);

  const fetchReservations = async () => {
    setLoading(true);
    setError('');
    try {
      const bId = currentStaff.branch_id || selectedBranchId;
      const data = await api.getStaffReservations(bId, statusFilter || null, dateFilter || null);
      setReservations(data);
    } catch (err) {
      setError(err.message || '加载预订失败。');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchReservations();
  }, [selectedBranchId, statusFilter, dateFilter]);

  const handleUpdateStatus = async (resId, newStatus) => {
    try {
      await api.updateReservation(resId, { status: newStatus });
      await fetchReservations();
    } catch (err) {
      alert(`更新预订失败：${err.message}`);
    }
  };

  const openReassignModal = async (reservation) => {
    setReassignModalRes(reservation);
    setSelectedNewTableId(reservation.table_id || '');
    try {
      const tables = await api.getStaffTables(reservation.branch_id);
      setBranchTables(tables);
    } catch (err) {
      alert('加载门店餐桌失败，无法改派。');
    }
  };

  const handleConfirmReassign = async () => {
    if (!reassignModalRes || !selectedNewTableId) return;
    setReassignLoading(true);
    try {
      await api.updateReservation(reassignModalRes.id, {
        table_id: Number(selectedNewTableId),
      });
      setReassignModalRes(null);
      await fetchReservations();
    } catch (err) {
      alert(`改派餐桌失败：${err.message}`);
    } finally {
      setReassignLoading(false);
    }
  };

  const STATUS_LABELS = {
    pending: '待确认',
    confirmed: '已确认',
    seated: '已入座',
    completed: '已完成',
    cancelled: '已取消',
  };

  const getStatusBadge = (status) => {
    const label = STATUS_LABELS[status] || status;
    switch (status) {
      case 'pending':
        return <span className="badge badge-warning">{label}</span>;
      case 'confirmed':
        return <span className="badge badge-success">{label}</span>;
      case 'seated':
        return <span className="badge badge-info">{label}</span>;
      case 'completed':
        return <span className="badge badge-neutral">{label}</span>;
      case 'cancelled':
        return <span className="badge badge-danger">{label}</span>;
      default:
        return <span className="badge badge-warning">{label}</span>;
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
      {/* Controls Bar */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
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
            <label style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-muted)' }}>状态：</label>
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              style={{ padding: '6px 12px', borderRadius: '6px', border: '1px solid var(--surface-border)' }}
            >
              <option value="">全部状态</option>
              <option value="pending">待确认</option>
              <option value="confirmed">已确认</option>
              <option value="seated">已入座</option>
              <option value="completed">已完成</option>
              <option value="cancelled">已取消</option>
            </select>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <label style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-muted)' }}>日期：</label>
            <input
              type="date"
              value={dateFilter}
              onChange={(e) => setDateFilter(e.target.value)}
              style={{ padding: '5px 10px', borderRadius: '6px', border: '1px solid var(--surface-border)' }}
            />
          </div>
        </div>

        <button
          onClick={fetchReservations}
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

      {/* Reservations Table */}
      {loading && reservations.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '3rem', color: 'var(--text-muted)' }}>
          正在加载预订…
        </div>
      ) : reservations.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '3rem', background: 'white', borderRadius: 'var(--radius-lg)', border: '1px solid var(--surface-border)', color: 'var(--text-muted)' }}>
          <Calendar size={40} style={{ margin: '0 auto 8px', opacity: 0.4 }} />
          <p>没有符合筛选条件的预订。</p>
        </div>
      ) : (
        <div style={{ overflowX: 'auto', background: 'white', borderRadius: 'var(--radius-lg)', border: '1px solid var(--surface-border)' }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>编号</th>
                <th>顾客</th>
                <th>人数</th>
                <th>时间与日期</th>
                <th>区域</th>
                <th>已分配餐桌</th>
                <th>状态</th>
                <th>员工操作</th>
              </tr>
            </thead>
            <tbody>
              {reservations.map((r) => (
                <tr key={r.id}>
                  <td style={{ fontWeight: 700 }}>#{r.id}</td>
                  <td>
                    <strong>{r.customer_name || '访客'}</strong>
                  </td>
                  <td>{r.party_size} 人</td>
                  <td>
                    <div>{new Date(r.start_time).toLocaleDateString()}</div>
                    <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                      {new Date(r.start_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} （{r.duration_minutes} 分钟）
                    </div>
                  </td>
                  <td>
                    <span style={{ textTransform: 'capitalize', fontSize: '0.8rem', background: '#f1f5f9', padding: '2px 8px', borderRadius: '4px' }}>
                      {r.seating_area_preference || '标准区'}
                    </span>
                  </td>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                      <span style={{ fontWeight: 600, color: 'var(--secondary)' }}>
                        {r.table_label || (r.table_id ? `餐桌 #${r.table_id}` : '未分配')}
                      </span>
                      <button
                        onClick={() => openReassignModal(r)}
                        className="btn-outline btn-sm"
                        style={{ padding: '2px 6px', fontSize: '0.7rem' }}
                        title="手动改派餐桌"
                      >
                        <Edit3 size={11} /> 改派
                      </button>
                    </div>
                  </td>
                  <td>{getStatusBadge(r.status)}</td>
                  <td>
                    <div style={{ display: 'flex', gap: '4px' }}>
                      {r.status === 'pending' && (
                        <button
                          onClick={() => handleUpdateStatus(r.id, 'confirmed')}
                          className="btn-primary btn-sm"
                          style={{ padding: '4px 8px', fontSize: '0.75rem' }}
                          title="确认预订"
                        >
                          <CheckCircle2 size={13} /> 确认
                        </button>
                      )}

                      {r.status === 'confirmed' && (
                        <button
                          onClick={() => handleUpdateStatus(r.id, 'seated')}
                          className="btn-primary btn-sm"
                          style={{ padding: '4px 8px', fontSize: '0.75rem' }}
                          title="安排入座"
                        >
                          <UserCheck size={13} /> 入座
                        </button>
                      )}

                      {r.status === 'seated' && (
                        <button
                          onClick={() => handleUpdateStatus(r.id, 'completed')}
                          className="btn-secondary btn-sm"
                          style={{ padding: '4px 8px', fontSize: '0.75rem' }}
                          title="标记完成"
                        >
                          <CheckCircle2 size={13} /> 完成
                        </button>
                      )}

                      {r.status !== 'cancelled' && r.status !== 'completed' && (
                        <button
                          onClick={() => {
                            if (confirm(`确定取消预订 #${r.id}？`)) {
                              handleUpdateStatus(r.id, 'cancelled');
                            }
                          }}
                          className="btn-outline btn-sm"
                          style={{ padding: '4px 8px', color: '#ef4444' }}
                          title="取消预订"
                        >
                          <XCircle size={13} />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Manual Override Reassignment Modal */}
      {reassignModalRes && (
        <div className="modal-overlay" onClick={() => setReassignModalRes(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
              <h3 style={{ fontSize: '1.1rem', fontWeight: 700 }}>
                手动改派餐桌 — 预订 #{reassignModalRes.id}
              </h3>
              <button onClick={() => setReassignModalRes(null)}>
                <X size={18} />
              </button>
            </div>

            <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginBottom: '1rem' }}>
              顾客：<strong>{reassignModalRes.customer_name}</strong> ｜ 人数：{' '}
              <strong>{reassignModalRes.party_size}</strong> ｜ 当前：{' '}
              <strong>{reassignModalRes.table_label || '无'}</strong>
            </p>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginBottom: '1.25rem' }}>
              <label style={{ fontSize: '0.85rem', fontWeight: 600 }}>选择目标餐桌：</label>
              <select
                value={selectedNewTableId}
                onChange={(e) => setSelectedNewTableId(e.target.value)}
                style={{ width: '100%', padding: '8px 10px', borderRadius: '6px', border: '1px solid var(--surface-border)' }}
              >
                <option value="">请选择餐桌…</option>
                {branchTables.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.label} （容纳 {t.capacity} 人，{t.seating_area}）
                  </option>
                ))}
              </select>
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
              <button onClick={() => setReassignModalRes(null)} className="btn-secondary btn-sm">
                取消
              </button>
              <button
                onClick={handleConfirmReassign}
                disabled={reassignLoading || !selectedNewTableId}
                className="btn-primary btn-sm"
              >
                {reassignLoading ? '保存中…' : '确认改派'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
