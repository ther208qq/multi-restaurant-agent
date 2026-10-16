import React, { useEffect, useMemo, useState } from 'react';
import { api } from '../../api';
import { X, AlertCircle, AlertTriangle, CheckCircle2, Clock, XCircle } from 'lucide-react';

const ORDER_TYPES = [
  { value: 'dine_in', label: '堂食' },
  { value: 'takeaway', label: '外带' },
  { value: 'delivery', label: '外卖' },
];

/**
 * Cart checkout. The customer picks an order type (delivery needs an address),
 * may leave whole-order and per-line notes, and sees a live cost estimate from
 * ``/orders/preview`` before confirming. The preview is read-only; only
 * "确认下单" writes. On success the modal shows the created order and lets the
 * customer jump to their history or keep browsing (both clear the cart through
 * ``onOrderPlaced``).
 */
export default function CheckoutModal({ isOpen, onClose, branch, items, onOrderPlaced, onViewHistory }) {
  const [orderType, setOrderType] = useState('dine_in');
  const [deliveryAddress, setDeliveryAddress] = useState('');
  const [specialNotes, setSpecialNotes] = useState('');
  const [lineNotes, setLineNotes] = useState({});
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState('');
  const [previewing, setPreviewing] = useState(false);
  const [placing, setPlacing] = useState(false);
  const [placed, setPlaced] = useState(null);

  useEffect(() => {
    if (isOpen) {
      setOrderType('dine_in');
      setDeliveryAddress('');
      setSpecialNotes('');
      setLineNotes({});
      setPreview(null);
      setError('');
      setPlaced(null);
    }
  }, [isOpen]);

  // The exact item payload the server will price, rebuilt whenever the cart or
  // the per-line notes change.
  const payloadItems = useMemo(
    () =>
      (items || []).map((it) => ({
        menu_item_id: it.menu_item_id,
        quantity: it.quantity,
        selected_variants: it.variant_name ? [{ name: it.variant_name }] : [],
        item_notes: (lineNotes[it.key] || '').trim() || null,
      })),
    [items, lineNotes]
  );

  // Live estimate. Re-runs whenever the priced payload changes; a failed call
  // (e.g. a since-removed item) surfaces as an inline error, not a crash.
  const previewKey = JSON.stringify(payloadItems);
  useEffect(() => {
    if (!isOpen || !branch || payloadItems.length === 0) return;
    let cancelled = false;
    setPreviewing(true);
    api
      .previewOrder({ branch_id: branch.id, order_type: orderType, items: payloadItems })
      .then((res) => {
        if (!cancelled) setPreview(res);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message || '无法计算价格，请重试。');
      })
      .finally(() => {
        if (!cancelled) setPreviewing(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isOpen, previewKey, branch, orderType]);

  // While the order waits for a manager's approval (AgentFeatures §5), re-read it
  // until the decision lands: 'placed' means the gift was approved, 'cancelled'
  // means it was rejected. Polling stops the moment the status leaves pending.
  useEffect(() => {
    if (!isOpen || !placed || placed.status !== 'pending_approval') return;
    let cancelled = false;
    const timer = setInterval(async () => {
      try {
        const fresh = await api.getOrder(placed.id);
        if (!cancelled) setPlaced(fresh);
      } catch {
        // Transient error -- keep polling; the next tick may succeed.
      }
    }, 4000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [isOpen, placed]);

  if (!isOpen) return null;

  const handleClose = () => {
    if (placing) return;
    onClose();
  };

  const handleSubmit = async () => {
    setError('');
    if (orderType === 'delivery' && !deliveryAddress.trim()) {
      setError('外卖订单需要填写送货地址。');
      return;
    }
    if (payloadItems.length === 0) {
      setError('购物车是空的。');
      return;
    }
    setPlacing(true);
    try {
      const order = await api.createOrder({
        branch_id: branch.id,
        order_type: orderType,
        delivery_address: orderType === 'delivery' ? deliveryAddress.trim() : null,
        special_notes: specialNotes.trim() || null,
        items: payloadItems,
      });
      setPlaced(order);
    } catch (err) {
      setError(err.message || '下单失败，请重试。');
    } finally {
      setPlacing(false);
    }
  };


  return (
    <div className="modal-overlay" onClick={handleClose}>
      <div
        className="modal-content"
        style={{ maxWidth: '560px', maxHeight: '88vh', display: 'flex', flexDirection: 'column' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
          <h2 style={{ fontSize: '1.25rem', fontWeight: 700, color: 'var(--secondary)' }}>
            {placed ? '下单成功' : '结算'}
          </h2>
          <button onClick={handleClose} style={{ color: 'var(--text-muted)' }}>
            <X size={20} />
          </button>
        </div>

        {placed ? (
          placed.status === 'pending_approval' ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', alignItems: 'center', textAlign: 'center', padding: '1rem 0' }}>
              <Clock size={48} style={{ color: '#d97706' }} />
              <div>
                <p style={{ fontWeight: 700, color: 'var(--secondary)', fontSize: '1.05rem' }}>
                  订单 #{placed.id} 已提交，等待店长审批
                </p>
                <p style={{ color: 'var(--text-muted)', fontSize: '0.9rem', marginTop: '4px' }}>
                  您的备注中包含赠品请求，需店长确认后生效。批准后赠品会附在订单上，本页面会自动刷新。
                </p>
              </div>
              <div style={{ display: 'flex', gap: '8px', marginTop: '0.5rem' }}>
                <button className="btn-outline" onClick={() => { onOrderPlaced(); onViewHistory(); }}>
                  查看我的记录
                </button>
                <button className="btn-primary" onClick={onOrderPlaced}>
                  关闭
                </button>
              </div>
            </div>
          ) : placed.status === 'cancelled' ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', alignItems: 'center', textAlign: 'center', padding: '1rem 0' }}>
              <XCircle size={48} style={{ color: '#b91c1c' }} />
              <div>
                <p style={{ fontWeight: 700, color: 'var(--secondary)', fontSize: '1.05rem' }}>
                  订单 #{placed.id} 未通过审批
                </p>
                <p style={{ color: 'var(--text-muted)', fontSize: '0.9rem', marginTop: '4px' }}>
                  赠品请求未被批准，订单已取消、库存已释放。您可以调整备注后重新下单。
                </p>
              </div>
              <button className="btn-primary" onClick={onOrderPlaced}>
                知道了
              </button>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', alignItems: 'center', textAlign: 'center', padding: '1rem 0' }}>
              <CheckCircle2 size={48} style={{ color: '#15803d' }} />
              <div>
                <p style={{ fontWeight: 700, color: 'var(--secondary)', fontSize: '1.05rem' }}>
                  订单 #{placed.id} 已提交
                </p>
                <p style={{ color: 'var(--text-muted)', fontSize: '0.9rem', marginTop: '4px' }}>
                  门店确认后即开始准备，合计 ${Number(placed.total_amount).toFixed(2)}。
                </p>
              </div>
              <div style={{ display: 'flex', gap: '8px', marginTop: '0.5rem' }}>
                <button className="btn-outline" onClick={() => { onOrderPlaced(); onViewHistory(); }}>
                  查看我的记录
                </button>
                <button className="btn-primary" onClick={onOrderPlaced}>
                  继续浏览
                </button>
              </div>
            </div>
          )
        ) : (
          <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            {error && (
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center', background: '#fee2e2', color: '#b91c1c', padding: '8px 12px', borderRadius: '8px', fontSize: '0.85rem' }}>
                <AlertCircle size={16} />
                <span>{error}</span>
              </div>
            )}

            <div>
              <label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: '6px' }}>
                用餐方式
              </label>
              <div style={{ display: 'flex', gap: '8px' }}>
                {ORDER_TYPES.map((t) => {
                  const active = orderType === t.value;
                  return (
<button
key={t.value}
type='button'
className={active ? 'chip-btn active' : 'chip-btn'}
onClick={() => setOrderType(t.value)}
style={{
background: active ? 'var(--primary)' : '#f1f5f9',
color: active ? 'white' : 'var(--text-main)',
fontWeight: 600,
}}
>
{t.label}
</button>
);
})}
</div>
</div>

{orderType === 'delivery' && (
<div>
<label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: '6px' }}>送货地址</label>
<input
type='text'
placeholder='请输入送货地址'
value={deliveryAddress}
onChange={(e) => setDeliveryAddress(e.target.value)}
style={{ width: '100%', padding: '9px 10px', borderRadius: '8px', border: '1px solid var(--surface-border)', outline: 'none' }}
/>
</div>
)}

<div>
<label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: '6px' }}>订单内容</label>
<div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
{items.map((it) => {
const pl = (preview ? preview.items : []).find((l) => l.menu_item_id === it.menu_item_id);
const unit = pl ? pl.unit_price : it.base_price + (it.price_delta || 0);
const short = pl ? pl.shortfall : 0;
return (
<div key={it.key} style={{ border: '1px solid var(--surface-border)', borderRadius: '8px', padding: '8px 10px' }}>
<div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.85rem' }}>
<span style={{ fontWeight: 600, color: 'var(--secondary)' }}>{it.name}{it.variant_name ? ' · ' + it.variant_name : ''} × {it.quantity}</span>
<span style={{ fontWeight: 600 }}>${(unit * it.quantity).toFixed(2)}</span>
</div>
{short > 0 && (
<div style={{ display: 'flex', alignItems: 'center', gap: '4px', color: '#b91c1c', fontSize: '0.75rem', marginTop: '2px', fontWeight: 600 }}>
<AlertTriangle size={12} /> 库存不足，缺 {short} 份
</div>
)}
<input
type='text'
placeholder='该菜品备注（选填）'
value={lineNotes[it.key] || ''}
onChange={(e) => setLineNotes((prev) => ({ ...prev, [it.key]: e.target.value }))}
style={{ width: '100%', marginTop: '6px', padding: '6px 8px', borderRadius: '6px', border: '1px solid var(--surface-border)', outline: 'none', fontSize: '0.8rem' }}
/>
</div>
);
})}
</div>
</div>

<div>
<label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: '6px' }}>订单备注（选填）</label>
<textarea
rows={2}
placeholder='例如：少辣、打包餐具等'
value={specialNotes}
onChange={(e) => setSpecialNotes(e.target.value)}
style={{ width: '100%', padding: '8px 10px', borderRadius: '8px', border: '1px solid var(--surface-border)', outline: 'none', resize: 'vertical', fontSize: '0.85rem' }}
/>
</div>
<div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderTop: '1px solid var(--surface-border)', paddingTop: '10px' }}>
<div style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>{previewing ? '正在计算…' : '预估合计'}</div>
<span style={{ fontWeight: 700, color: 'var(--primary)', fontSize: '1.15rem' }}>${(preview ? preview.total_amount : items.reduce((sum, it) => sum + (it.base_price + (it.price_delta || 0)) * it.quantity, 0)).toFixed(2)}</span>
</div>
{preview && preview.has_shortfall && (
<div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: '#b45309', background: '#fef3c7', padding: '8px 12px', borderRadius: '8px', fontSize: '0.8rem' }}>
<AlertTriangle size={14} /> 部分菜品库存不足，提交后可能无法下单。
</div>
)}
<button
className='btn-primary'
style={{ justifyContent: 'center', width: '100%', opacity: placing ? 0.7 : 1 }}
disabled={placing}
onClick={handleSubmit}
>
{placing ? '正在下单…' : '确认下单'}
</button>
</div>
)}
</div>
</div>
);
}
