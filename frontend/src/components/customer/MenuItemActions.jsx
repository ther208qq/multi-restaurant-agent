import React from 'react';
import { AlertTriangle, ShoppingCart, Minus, Plus, MessageSquarePlus } from 'lucide-react';

/**
 * The per-item action row of the menu explorer: the stock badge, the quantity
 * stepper and the "add to cart" / "ask the AI" buttons. Kept in its own small
 * component so the explorer stays readable.
 */
export default function MenuItemActions({ item, draft, onPatch, onAdd, onAskAi }) {
  const isAvailable = item.is_available && item.stock_quantity > 0;
  return (
    <>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginTop: '4px',
          paddingTop: '4px',
          borderTop: '1px dashed #f1f5f9',
        }}
      >
        {isAvailable ? (
          <span style={{ fontSize: '0.75rem', color: '#15803d', fontWeight: 600 }}>
            ✓ 有货 · {item.stock_quantity}
          </span>
        ) : (
          <span
            style={{
              fontSize: '0.75rem',
              color: '#b91c1c',
              fontWeight: 600,
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
            }}
          >
            <AlertTriangle size={12} /> 已售罄
          </span>
        )}

        {isAvailable && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <div style={{ display: 'flex', alignItems: 'center', border: '1px solid var(--surface-border)', borderRadius: '6px' }}>
              <button
                type="button"
                className="btn-sm"
                style={{ padding: '2px 8px' }}
                onClick={() => onPatch(item.id, { qty: Math.max(1, draft.qty - 1) })}
              >
                <Minus size={12} />
              </button>
              <span style={{ minWidth: '20px', textAlign: 'center', fontSize: '0.8rem', fontWeight: 600 }}>{draft.qty}</span>
              <button
                type="button"
                className="btn-sm"
                style={{ padding: '2px 8px' }}
                onClick={() => onPatch(item.id, { qty: draft.qty + 1 })}
              >
                <Plus size={12} />
              </button>
            </div>
            <button
              type="button"
              className="btn-sm"
              style={{ background: 'var(--primary)', color: 'white', display: 'flex', alignItems: 'center', gap: '4px', fontWeight: 600 }}
              onClick={() => onAdd(item)}
            >
              <ShoppingCart size={12} /> 加入
            </button>
          </div>
        )}
      </div>

      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '2px' }}>
        <button
          type="button"
          className="btn-sm"
          style={{ background: 'var(--primary-light)', color: 'var(--primary)', display: 'flex', alignItems: 'center', gap: '4px', fontWeight: 600 }}
          onClick={() => onAskAi(`我想点 1 份 ${item.name}`)}
        >
          <MessageSquarePlus size={12} /> 让 AI 下单
        </button>
      </div>
    </>
  );
}
