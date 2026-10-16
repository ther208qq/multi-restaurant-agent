import React, { useCallback, useEffect, useState } from 'react';
import { api } from '../../api';
import { ShieldCheck, RefreshCw, Check, XCircle } from 'lucide-react';

const INTENTS = { gift_request: '索要赠品' };
const STATUSES = { pending: '待审批', approved: '已通过', rejected: '已驳回' };
const TYPES = { dine_in: '堂食', takeaway: '外带', delivery: '外卖' };

const CARD = { background: 'white', border: '1px solid var(--surface-border)', borderRadius: 'var(--radius-lg)', padding: '1.25rem', display: 'flex', flexDirection: 'column', gap: '0.75rem' };
const BAR = { display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: '1rem', background: 'white', padding: '1rem', borderRadius: 'var(--radius-lg)', border: '1px solid var(--surface-border)' };
const SEL = { padding: '6px 12px', borderRadius: '6px', border: '1px solid var(--surface-border)' };
const ROW = { display: 'flex', justifyContent: 'space-between' };
const ACT = { display: 'flex', gap: '8px', flexWrap: 'wrap' };

// Manager approval queue (AgentFeatures feature 3). Polls /staff/approvals and
// lets a manager give one of the agent's suggested gifts, or decline.
export default function ApprovalsManager({ currentStaff, branches }) {
  const [rows, setRows] = useState([]);
  const [branchId, setBranchId] = useState(currentStaff.branch_id || (branches[0] ? branches[0].id : 1));
  const [statusFilter, setStatusFilter] = useState('pending');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [acting, setActing] = useState(null);
  const [choices, setChoices] = useState({});

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await api.getStaffApprovals(currentStaff.branch_id || branchId, statusFilter || null);
      setRows(data);
    } catch (e) {
      setError(e.message || '加载审批队列失败。');
    } finally {
      setLoading(false);
    }
  }, [currentStaff.branch_id, branchId, statusFilter]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { const t = setInterval(load, 10000); return () => clearInterval(t); }, [load]);

  const giftOf = (a) => choices[a.id] ?? (a.suggestion ? a.suggestion.recommended_item.item_id : '');

  const run = async (a, fn) => {
    setActing(a.id);
    try { await fn(); await load(); }
    catch (e) { alert('审批失败：' + e.message); }
    finally { setActing(null); }
  };
  const approve = (a) => {
    const g = giftOf(a);
    if (!g) { alert('请先选择要赠送的赠品。'); return; }
    run(a, () => api.approveApproval(a.id, Number(g)));
  };
  const reject = (a) => {
    if (confirm('确定拒绝该赠品请求？订单将照常下单（不附赠品），顾客可自行取消。')) run(a, () => api.rejectApproval(a.id));
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
      <div style={BAR}>
        <div>
          {!currentStaff.branch_id && (
            <select value={branchId} onChange={(e) => setBranchId(Number(e.target.value))} style={SEL}>
              {branches.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
          )}
          {' '}
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} style={SEL}>
            <option value="pending">待审批</option>
            <option value="approved">已通过</option>
            <option value="rejected">已驳回</option>
            <option value="">全部</option>
          </select>
        </div>
        <button onClick={load} disabled={loading} className="btn-outline btn-sm">
          <RefreshCw size={14} /> 刷新
        </button>
      </div>

      {error && <div className="badge badge-danger">{error}</div>}

      {rows.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '3rem', color: 'var(--text-muted)' }}>
          <ShieldCheck size={40} style={{ opacity: 0.4 }} />
          <p>没有符合条件的审批。</p>
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(380px, 1fr))', gap: '1rem' }}>
          {rows.map((a) => {
            const o = a.order || {};
            const cands = (a.suggestion && a.suggestion.candidates) || [];
            return (
              <div key={a.id} style={CARD}>
                <div style={ROW}>
                  <strong>#{o.id} · {TYPES[o.order_type] || o.order_type}</strong>
                  <span className="badge badge-info">{STATUSES[a.status] || a.status}</span>
                </div>
                <div>顾客诉求：{a.remark || '—'}</div>
                <div>识别意图：{INTENTS[a.intent] || a.intent}</div>

                <div>
                  {(o.items || []).map((it) =>
                    <div key={it.menu_item_id} style={ROW}>
                      <span>{it.quantity} x {it.menu_item_name || it.menu_item_id}</span>
                      <span>{Number(it.unit_price) * it.quantity}</span>
                    </div>
                  )}
                </div>

                {a.status === 'pending' && (
                  <div style={ACT}>
                    {cands.length > 0 ? (
                      <select value={giftOf(a)} onChange={(e) => setChoices((p) => ({ ...p, [a.id]: e.target.value }))} style={SEL}>
                        {cands.map((c) => <option key={c.item_id} value={c.item_id}>{c.name} 库存{c.stock_quantity}</option>)}
                      </select>
                    ) : (
                      <span style={{ color: '#b45309' }}>无可赠库存</span>
                    )}

                    <button className="btn-primary btn-sm" disabled={acting === a.id} onClick={() => approve(a)}>
                      <Check size={14} /> 同意并赠送
                    </button>
                    <button className="btn-outline btn-sm" disabled={acting === a.id} onClick={() => reject(a)}>
                      <XCircle size={14} /> 拒绝赠品
                    </button>
                  </div>
                )}
                {a.status === 'pending' && a.suggestion && (
                  <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>{a.suggestion.message}</div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
