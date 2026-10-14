import React from 'react';
import { ShoppingCart, Minus, Plus, Trash2 } from 'lucide-react';

/**
 * The persistent sidebar cart, shown above the menu explorer. Renders nothing
 * while empty so the sidebar keeps its natural height.
 *
 * Prices here are a *preview* (snapshotted base price plus the variant delta);
 * the server reprices authoritatively at checkout against the current DB rows.
 */
export default function CartPanel({ items, onPatch, onRemove, onCheckout }) {
  if (!items || items.length === 0) return null;

  const lineTotal = (it) => (it.base_price + (it.price_delta || 0)) * it.quantity;
  const total = items.reduce((sum, it) => sum + lineTotal(it), 0);
  const count = items.reduce((sum, it) => sum + it.quantity, 0);

  return (
    <div className="sidebar-card" style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <ShoppingCart size={18} style={{ color: 'var(--primary)' }} />
          <h3 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--secondary)' }}>购物车</h3>
        </div>
        <span className="badge badge-info" style={{ fontSize: '0.7rem' }}>
          {count} 件
        </span>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', maxHeight: '220px', overflowY: 'auto' }}>
        {items.map((it) => (
          <div key={it.key} style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.85rem' }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600, color: 'var(--secondary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {it.name}
                {it.variant_name && (
                  <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}> · {it.variant_name}</span>
                )}
              </div>
              <div style={{ color: 'var(--text-muted)', fontSize: '0.75rem' }}>
                ${(it.base_price + (it.price_delta || 0)).toFixed(2)} × {it.quantity} = ${lineTotal(it).toFixed(2)}
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', border: '1px solid var(--surface-border)', borderRadius: '6px' }}>
              <button
                type="button"
                className="btn-sm"
                style={{ padding: '2px 6px' }}
                onClick={() => onPatch(it.key, { quantity: Math.max(1, it.quantity - 1) })}
              >
                <Minus size={12} />
              </button>
              <span style={{ minWidth: '18px', textAlign: 'center', fontSize: '0.8rem', fontWeight: 600 }}>
                {it.quantity}
              </span>
              <button
                type="button"
                className="btn-sm"
                style={{ padding: '2px 6px' }}
                onClick={() => onPatch(it.key, { quantity: it.quantity + 1 })}
              >
                <Plus size={12} />
              </button>
            </div>
            <button type="button" onClick={() => onRemove(it.key)} title="移除" style={{ color: '#b91c1c' }}>
              <Trash2 size={14} />
            </button>
          </div>
        ))}
      </div>

      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          borderTop: '1px solid var(--surface-border)',
          paddingTop: '8px',
        }}
      >
        <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>合计</span>
        <span style={{ fontWeight: 700, color: 'var(--primary)', fontSize: '1.05rem' }}>${total.toFixed(2)}</span>
      </div>
      <button type="button" className="btn-primary" style={{ justifyContent: 'center' }} onClick={onCheckout}>
        去结算
      </button>
    </div>
  );
}
