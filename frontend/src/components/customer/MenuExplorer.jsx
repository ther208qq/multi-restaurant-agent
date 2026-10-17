import React, { useState } from 'react';
import VariantPicker from './VariantPicker';
import MenuItemActions from './MenuItemActions';
import { Utensils } from 'lucide-react';

export default function MenuExplorer({ menuData, onAddToCart }) {
  const [activeCategory, setActiveCategory] = useState('ALL');
  // Per-item draft: how many to add and, for items that carry variants, which
  // single variant is picked. Keyed by menu item id.
  const [draft, setDraft] = useState({});

  const draftFor = (itemId) => draft[itemId] || { qty: 1, variantId: null };
  const patchDraft = (itemId, patch) =>
    setDraft((prev) => ({ ...prev, [itemId]: { ...draftFor(itemId), ...patch } }));

  const handleAdd = (item) => {
    const d = draftFor(item.id);
    const variant = (item.variants || []).find((v) => v.id === d.variantId) || null;
    onAddToCart(item, variant, d.qty);
    patchDraft(item.id, { qty: 1, variantId: null });
  };

  if (!menuData || !menuData.categories) {
    return (
      <div className="sidebar-card" style={{ textAlign: 'center', padding: '2rem 1rem', color: 'var(--text-muted)' }}>
        <Utensils size={32} style={{ margin: '0 auto 8px', opacity: 0.5 }} />
        <p>正在加载菜单…</p>
      </div>
    );
  }

  const categories = menuData.categories;
  const allItems = categories.flatMap((cat) =>
    (cat.items || []).map((item) => ({ ...item, category_name: cat.name }))
  );

  const displayedItems =
    activeCategory === 'ALL'
      ? allItems
      : (categories.find((c) => c.name === activeCategory)?.items || []).map((item) => ({
          ...item,
          category_name: activeCategory,
        }));

  return (
    <div className="sidebar-card" style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75rem' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Utensils size={18} style={{ color: 'var(--primary)' }} />
          <h3 style={{ fontSize: '1rem', fontWeight: 700, color: 'var(--secondary)' }}>实时菜单与库存</h3>
        </div>
        <span className="badge badge-neutral" style={{ fontSize: '0.7rem' }}>
          {allItems.length} 个菜品
        </span>
      </div>

      {/* Category Tabs */}
      <div
        style={{
          display: 'flex',
          gap: '6px',
          overflowX: 'auto',
          paddingBottom: '6px',
          marginBottom: '0.75rem',
          scrollbarWidth: 'none',
        }}
      >
        <button
          className={`chip-btn ${activeCategory === 'ALL' ? 'active' : ''}`}
          style={{
            background: activeCategory === 'ALL' ? 'var(--primary)' : '#f1f5f9',
            color: activeCategory === 'ALL' ? 'white' : 'var(--text-main)',
            fontWeight: 600,
          }}
          onClick={() => setActiveCategory('ALL')}
        >
          全部
        </button>
        {categories.map((c) => (
          <button
            key={c.id}
            className={`chip-btn ${activeCategory === c.name ? 'active' : ''}`}
            style={{
              background: activeCategory === c.name ? 'var(--primary)' : '#f1f5f9',
              color: activeCategory === c.name ? 'white' : 'var(--text-main)',
              fontWeight: 600,
            }}
            onClick={() => setActiveCategory(c.name)}
          >
            {c.name}
          </button>
        ))}
      </div>

      {/* Items Scrollable List */}
      <div style={{ flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '8px', paddingRight: '2px' }}>
        {displayedItems.length === 0 ? (
          <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', textAlign: 'center', margin: '1rem 0' }}>
            该分类暂无菜品。
          </p>
        ) : (
          displayedItems.map((item) => {
            const isAvailable = item.is_available && item.stock_quantity > 0;
            const d = draftFor(item.id);
            const variants = item.variants || [];
            return (
              <div
                key={item.id}
                style={{
                  border: '1px solid var(--surface-border)',
                  borderRadius: '8px',
                  padding: '10px 12px',
                  background: isAvailable ? 'white' : '#f8fafc',
                  opacity: isAvailable ? 1 : 0.65,
                  display: 'flex',
                  flexDirection: 'column',
                  gap: '4px',
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <span style={{ fontWeight: 600, fontSize: '0.9rem', color: 'var(--secondary)' }}>
                    {item.name}
                  </span>
                  <span style={{ fontWeight: 700, fontSize: '0.9rem', color: 'var(--primary)' }}>
                    ${Number(item.base_price).toFixed(2)}
                  </span>
                </div>

                {item.description && (
                  <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)', lineHeight: 1.3 }}>
                    {item.description}
                  </p>
                )}

                {variants.length > 0 && (
                  <VariantPicker
                    variants={variants}
                    selectedId={d.variantId}
                    onSelect={(variantId) => patchDraft(item.id, { variantId })}
                  />
                )}

                <MenuItemActions
                  item={item}
                  draft={d}
                  onPatch={patchDraft}
                  onAdd={handleAdd}
                />
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
