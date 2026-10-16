import React, { useState, useEffect } from 'react';
import { api } from '../../api';
import { Pencil, Plus, RefreshCw, Trash2 } from 'lucide-react';

export default function BranchTableManager({ currentStaff, branches, onRefreshBranches }) {
  const [selectedBranchId, setSelectedBranchId] = useState(
    currentStaff.branch_id || (branches[0] ? branches[0].id : 1)
  );
  const [tables, setTables] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // Table form state. One modal serves both jobs: ``editingTable`` is the row
  // being renamed (null means we are adding a new one).
  const [isFormOpen, setIsFormOpen] = useState(false);
  const [editingTable, setEditingTable] = useState(null);
  const [formLabel, setFormLabel] = useState('');
  const [formArea, setFormArea] = useState('indoor');
  const [formCap, setFormCap] = useState('4');
  const [saving, setSaving] = useState(false);

  const fetchTables = async () => {
    setLoading(true);
    setError('');
    try {
      const bId = currentStaff.branch_id || selectedBranchId;
      const data = await api.getStaffTables(bId);
      setTables(data);
    } catch (err) {
      setError(err.message || '加载门店餐桌失败。');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTables();
  }, [selectedBranchId]);

  const handleToggleTable = async (table) => {
    try {
      await api.updateTable(table.id, {
        is_available: !table.is_available,
      });
      await fetchTables();
    } catch (err) {
      alert(`更新餐桌失败：${err.message}`);
    }
  };

  const openAddTable = () => {
    setEditingTable(null);
    setFormLabel('');
    setFormArea('indoor');
    setFormCap('4');
    setError('');
    setIsFormOpen(true);
  };

  const openEditTable = (table) => {
    setEditingTable(table);
    setFormLabel(table.label);
    setFormArea(table.seating_area);
    setFormCap(String(table.capacity));
    setError('');
    setIsFormOpen(true);
  };

  const handleSubmitTable = async (e) => {
    e.preventDefault();
    if (!formLabel.trim()) return;
    setSaving(true);
    try {
      const payload = {
        label: formLabel.trim(),
        seating_area: formArea,
        capacity: Number(formCap),
      };
      if (editingTable) {
        await api.updateTable(editingTable.id, payload);
      } else {
        const bId = currentStaff.branch_id || selectedBranchId;
        await api.createTable({ ...payload, branch_id: bId, is_available: true });
      }
      setIsFormOpen(false);
      await fetchTables();
      if (onRefreshBranches) onRefreshBranches();
    } catch (err) {
      alert(`${editingTable ? '保存' : '创建'}餐桌失败：${err.message}`);
    } finally {
      setSaving(false);
    }
  };

  const handleDeleteTable = async (table) => {
    if (!confirm(`确定删除餐桌 ${table.label}？删除后无法恢复。`)) return;
    setError('');
    try {
      await api.deleteTable(table.id);
      await fetchTables();
    } catch (err) {
      // A table that has ever been booked cannot be deleted (the server answers
      // 409 and suggests disabling it instead) -- surface that here rather than
      // in an alert, so the reason stays on screen.
      setError(err.message);
    }
  };

  const AREA_LABELS = {
    indoor: '室内',
    outdoor: '户外露台',
    rooftop: '天台花园',
  };

  const selectedBranch = branches.find((b) => b.id === selectedBranchId);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
      {/* Top Bar */}
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
        <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
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

          {selectedBranch && (
            <div style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
              📍 {selectedBranch.address} • ⏰ {selectedBranch.opening_hours}
            </div>
          )}
        </div>

        <div style={{ display: 'flex', gap: '8px' }}>
          <button onClick={openAddTable} className="btn-primary btn-sm">
            <Plus size={14} /> 添加餐桌
          </button>
          <button onClick={fetchTables} className="btn-outline btn-sm">
            <RefreshCw size={14} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </div>

      {error && (
        <div style={{ background: '#fee2e2', color: '#b91c1c', padding: '10px 14px', borderRadius: '8px', fontSize: '0.875rem' }}>
          {error}
        </div>
      )}

      {/* Tables Grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(250px, 1fr))', gap: '1rem' }}>
        {tables.map((t) => (
          <div
            key={t.id}
            style={{
              background: 'white',
              border: '1px solid var(--surface-border)',
              borderRadius: 'var(--radius-lg)',
              padding: '1.25rem',
              display: 'flex',
              flexDirection: 'column',
              gap: '0.5rem',
              opacity: t.is_available ? 1 : 0.6,
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ fontWeight: 800, fontSize: '1.15rem', color: 'var(--secondary)' }}>
                {t.label}
              </span>
              <button
                onClick={() => handleToggleTable(t)}
                className={`badge ${t.is_available ? 'badge-success' : 'badge-danger'}`}
                style={{ cursor: 'pointer' }}
                title="点击切换餐桌可用状态"
              >
                {t.is_available ? '可用' : '停用'}
              </button>
            </div>

            <div style={{ fontSize: '0.85rem', color: 'var(--text-muted)', display: 'flex', flexDirection: 'column', gap: '2px' }}>
              <div>
                <strong>用餐区：</strong>{' '}
                <span>{AREA_LABELS[t.seating_area] || t.seating_area}</span>
              </div>
              <div>
                <strong>容纳人数：</strong> {t.capacity} 人
              </div>
            </div>

            <div
              style={{
                marginTop: 'auto',
                paddingTop: '10px',
                borderTop: '1px solid #f1f5f9',
                display: 'flex',
                justifyContent: 'flex-end',
                gap: '8px',
              }}
            >
              <button
                onClick={() => openEditTable(t)}
                className="btn-outline btn-sm"
                style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}
              >
                <Pencil size={12} /> 编辑
              </button>
              <button
                onClick={() => handleDeleteTable(t)}
                className="btn-danger btn-sm"
                style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}
              >
                <Trash2 size={12} /> 删除
              </button>
            </div>
          </div>
        ))}
      </div>

      {/* Add / Edit Table Modal */}
      {isFormOpen && (
        <div className="modal-overlay" onClick={() => setIsFormOpen(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <h3 style={{ fontSize: '1.1rem', fontWeight: 700, marginBottom: '1rem' }}>
              {editingTable ? `编辑餐桌 ${editingTable.label}` : '为门店添加餐桌'}
            </h3>
            <form onSubmit={handleSubmitTable} style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              <div>
                <label style={{ fontSize: '0.8rem', fontWeight: 600 }}>餐桌编号</label>
                <input
                  type="text"
                  required
                  placeholder="例如：T12 或 卡座-4"
                  value={formLabel}
                  onChange={(e) => setFormLabel(e.target.value)}
                  style={{ width: '100%', padding: '8px 10px', borderRadius: '6px', border: '1px solid var(--surface-border)' }}
                />
              </div>

              <div>
                <label style={{ fontSize: '0.8rem', fontWeight: 600 }}>用餐区</label>
                <select
                  value={formArea}
                  onChange={(e) => setFormArea(e.target.value)}
                  style={{ width: '100%', padding: '8px 10px', borderRadius: '6px', border: '1px solid var(--surface-border)' }}
                >
                  <option value="indoor">室内</option>
                  <option value="outdoor">户外露台</option>
                  <option value="rooftop">天台花园</option>
                </select>
              </div>

              <div>
                <label style={{ fontSize: '0.8rem', fontWeight: 600 }}>容纳人数</label>
                <input
                  type="number"
                  min="1"
                  required
                  value={formCap}
                  onChange={(e) => setFormCap(e.target.value)}
                  style={{ width: '100%', padding: '8px 10px', borderRadius: '6px', border: '1px solid var(--surface-border)' }}
                />
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', marginTop: '8px' }}>
                <button type="button" onClick={() => setIsFormOpen(false)} className="btn-secondary btn-sm">
                  取消
                </button>
                <button type="submit" disabled={saving} className="btn-primary btn-sm">
                  {editingTable ? '保存' : '添加餐桌'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
