import React from 'react';

/**
 * Inline single-select for an item's variants (size / spice / topping ...).
 * Only one variant per line is picked, matching how the order line carries its
 * resolved variants. Clicking the picked chip clears the choice.
 */
export default function VariantPicker({ variants, selectedId, onSelect }) {
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', marginTop: '2px' }}>
      {variants.map((v) => {
        const picked = selectedId === v.id;
        return (
          <button
            key={v.id}
            type="button"
            onClick={() => onSelect(picked ? null : v.id)}
            style={{
              fontSize: '0.7rem',
              background: picked ? 'var(--primary)' : '#f1f5f9',
              color: picked ? 'white' : '#475569',
              padding: '1px 6px',
              borderRadius: '4px',
              border: 'none',
              cursor: 'pointer',
            }}
          >
            {v.variant_name}
            {v.price_delta > 0 ? ' (+$' + v.price_delta + ')' : ''}
          </button>
        );
      })}
    </div>
  );
}
