import React, { useState, useEffect } from 'react';
import { api } from '../../api';
import { formatWallClockDateTime } from '../../utils/datetime';
import { X, ShoppingBag, Calendar, RefreshCw, CheckCircle2, Clock, AlertCircle } from 'lucide-react';
import KdsRequestPanel from './KdsRequestPanel';

export default function MyHistoryModal({ isOpen, onClose }) {
  const [tab, setTab] = useState('orders'); // 'orders' | 'reservations'
  const [orders, setOrders] = useState([]);
  const [reservations, setReservations] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [cancellingId, setCancellingId] = useState(null);

  const fetchData = async () => {
    setLoading(true);
    setError('');
    try {
      if (tab === 'orders') {
        const data = await api.getMyOrders();
        setOrders(data);
      } else {
        const data = await api.getMyReservations();
        setReservations(data);
      }
    } catch (err) {
      setError(err.message || '获取历史记录失败。');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (isOpen) {
      fetchData();
    }
  }, [isOpen, tab]);

  const handleCancel = async (orderId) => {
    if (!confirm('确定取消该订单？取消后相关库存将释放。')) return;
    setCancellingId(orderId);
    setError('');
    try {
      await api.cancelOrder(orderId);
      await fetchData();
    } catch (err) {
      setError(err.message || '取消订单失败。');
    } finally {
      setCancellingId(null);
    }
  };

  if (!isOpen) return null;

  // Shared by the orders tab and the reservations tab; a key present in either
  // lifecycle must be listed so no status ever leaks its raw English code.
  const STATUS_LABELS = {
    placed: '已下单',
    pending_approval: '待审批',
    confirmed: '已确认',
    preparing: '制作中',
    ready: '已就绪',
    delivered: '已送达',
    seated: '已入座',
    completed: '已完成',
    pending: '待处理',
    cancelled: '已取消',
  };

  const ORDER_TYPE_LABELS = {
    dine_in: '堂食',
    takeaway: '外带',
    delivery: '外卖',
  };

  const getStatusBadge = (status) => {
    const label = STATUS_LABELS[status] || status;
    switch (status) {
      case 'confirmed':
      case 'ready':
      case 'delivered':
      case 'seated':
      case 'completed':
        return <span className="badge badge-success">{label}</span>;
      case 'preparing':
      case 'pending':
        return <span className="badge badge-warning">{label}</span>;
      case 'cancelled':
        return <span className="badge badge-danger">{label}</span>;
      default:
        return <span className="badge badge-info">{label}</span>;
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-content"
        style={{ maxWidth: '650px', maxHeight: '85vh', display: 'flex', flexDirection: 'column' }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              className={`nav-tab-btn ${tab === 'orders' ? 'active' : ''}`}
              onClick={() => setTab('orders')}
              style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
            >
              <ShoppingBag size={16} /> 我的订单
            </button>
            <button
              className={`nav-tab-btn ${tab === 'reservations' ? 'active' : ''}`}
              onClick={() => setTab('reservations')}
              style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
            >
              <Calendar size={16} /> 我的预订
            </button>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <button
              onClick={fetchData}
              disabled={loading}
              title="刷新"
              style={{ color: 'var(--text-muted)' }}
            >
              <RefreshCw size={16} className={loading ? 'spin' : ''} />
            </button>
            <button onClick={onClose} style={{ color: 'var(--text-muted)' }}>
              <X size={20} />
            </button>
          </div>
        </div>

        {error && (
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', background: '#fee2e2', color: '#b91c1c', padding: '8px 12px', borderRadius: '8px', fontSize: '0.85rem', marginBottom: '1rem' }}>
            <AlertCircle size={16} />
            <span>{error}</span>
          </div>
        )}

        {/* Content Body */}
        <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '10px' }}>
          {loading ? (
            <div style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>
              正在加载{tab === 'orders' ? '订单' : '预订'}…
            </div>
          ) : tab === 'orders' ? (
            orders.length === 0 ? (
              <div style={{ textAlign: 'center', padding: '2.5rem', color: 'var(--text-muted)' }}>
                <ShoppingBag size={36} style={{ margin: '0 auto 8px', opacity: 0.5 }} />
                <p>还没有订单。快去菜单选一道喜欢的菜，下第一单吧！</p>
              </div>
            ) : (
              orders.map((o) => (
                <div
                  key={o.id}
                  style={{
                    border: '1px solid var(--surface-border)',
                    borderRadius: '10px',
                    padding: '12px 14px',
                    background: 'white',
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <span style={{ fontWeight: 700, fontSize: '0.95rem', color: 'var(--secondary)' }}>
                        Order #{o.id}
                      </span>
                      <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                        • {o.branch_name || `门店 #${o.branch_id}`}
                      </span>
                      <span style={{ fontSize: '0.75rem', background: '#f1f5f9', padding: '2px 6px', borderRadius: '4px' }}>
                        {ORDER_TYPE_LABELS[o.order_type] || o.order_type}
                      </span>
                    </div>
                    {getStatusBadge(o.status)}
                  </div>

                  <div style={{ fontSize: '0.85rem', color: 'var(--text-main)', margin: '6px 0' }}>
                    {o.items && o.items.map((item, idx) => (
                      <div key={idx} style={{ display: 'flex', justifyContent: 'space-between', padding: '2px 0' }}>
                        <span>
                          {item.quantity}× {item.menu_item_name || `菜品 #${item.menu_item_id}`}
                          {item.item_notes ? ` (${item.item_notes})` : ''}
                        </span>
                        <span style={{ fontWeight: 500 }}>
                          ${(Number(item.unit_price) * item.quantity).toFixed(2)}
                        </span>
                      </div>
                    ))}
                  </div>

                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      marginTop: '6px',
                      paddingTop: '6px',
                      borderTop: '1px solid #f1f5f9',
                      fontSize: '0.85rem',
                    }}
                  >
                    <span style={{ color: 'var(--text-muted)', fontSize: '0.75rem' }}>
                      {formatWallClockDateTime(o.created_at)}
                    </span>
                    <span style={{ fontWeight: 700, color: 'var(--primary)', fontSize: '0.95rem' }}>
                      合计：${Number(o.total_amount).toFixed(2)}
                    </span>
                  </div>
                  <KdsRequestPanel
                    orderId={o.id}
                    kdsStatus={o.kds_status}
                    kdsHeldReason={o.kds_held_reason}
                    onSent={fetchData}
                  />
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '8px', gap: '8px' }}>
                    {o.approval_status === 'pending' && (
                      <span style={{ fontSize: '0.8rem', color: '#b45309' }}>⏳ 赠品待店长审批</span>
                    )}
                    {o.approval_status === 'rejected' && (
                      <span style={{ fontSize: '0.8rem', color: '#9a3412' }}>赠品请求未通过</span>
                    )}
                    {o.status !== 'cancelled' && o.status !== 'delivered' && (
                      <button
                        className="btn-outline btn-sm"
                        style={{ color: '#ef4444', marginLeft: 'auto' }}
                        disabled={cancellingId === o.id}
                        onClick={() => handleCancel(o.id)}
                      >
                        {cancellingId === o.id ? '正在取消…' : '取消订单'}
                      </button>
                    )}
                  </div>
                </div>
              ))
            )
          ) : (
            reservations.length === 0 ? (
              <div style={{ textAlign: 'center', padding: '2.5rem', color: 'var(--text-muted)' }}>
                <Calendar size={36} style={{ margin: '0 auto 8px', opacity: 0.5 }} />
                <p>还没有预订。与 AI 聊聊即可预订座位！</p>
              </div>
            ) : (
              reservations.map((r) => (
                <div
                  key={r.id}
                  style={{
                    border: '1px solid var(--surface-border)',
                    borderRadius: '10px',
                    padding: '12px 14px',
                    background: 'white',
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <span style={{ fontWeight: 700, fontSize: '0.95rem', color: 'var(--secondary)' }}>
                        预订 #{r.id}
                      </span>
                      <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                        • {r.branch_name || `门店 #${r.branch_id}`}
                      </span>
                    </div>
                    {getStatusBadge(r.status)}
                  </div>

                  <div style={{ fontSize: '0.85rem', color: 'var(--text-main)', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px', margin: '6px 0' }}>
                    <div>
                      <strong>用餐人数：</strong> {r.party_size} 人
                    </div>
                    <div>
                      <strong>区域：</strong> {r.seating_area_preference || '标准区'}
                    </div>
                    <div>
                      <strong>桌位：</strong> {r.table_label || (r.table_id ? `餐桌 #${r.table_id}` : '待分配')}
                    </div>
                    <div>
                      <strong>时长：</strong> {r.duration_minutes} 分钟
                    </div>
                  </div>

                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      marginTop: '6px',
                      paddingTop: '6px',
                      borderTop: '1px solid #f1f5f9',
                      fontSize: '0.8rem',
                      color: 'var(--text-muted)',
                    }}
                  >
                    <span>
                      📅 {formatWallClockDateTime(r.start_time)}
                    </span>
                    {r.status === 'pending' && (
                      <span style={{ color: '#b45309', display: 'flex', alignItems: 'center', gap: '4px', fontWeight: 600 }}>
                        <Clock size={13} /> 等待门店确认
                      </span>
                    )}
                    {r.status === 'confirmed' && (
                      <span style={{ color: '#15803d', display: 'flex', alignItems: 'center', gap: '4px', fontWeight: 600 }}>
                        <CheckCircle2 size={13} /> 已确认
                      </span>
                    )}
                  </div>
                </div>
              ))
            )
          )}
        </div>
      </div>
    </div>
  );
}
