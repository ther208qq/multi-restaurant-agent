import React, { useState } from 'react';
import { api } from '../../api';
import { ChefHat, Send, PauseCircle } from 'lucide-react';

/**
 * "Contact the kitchen" panel for one order (AgentFeatures feature 1).
 *
 * The customer types a message; the backend classifies it (hold / resume /
 * status) and drives the order's simulated KDS, answering with the agent's
 * reply. Self-contained: it owns its open/closed and draft state, and asks the
 * parent to refresh (``onSent``) once the order's KDS state may have changed.
 */

const KDS_LABELS = {
  queued: '后厨排队中',
  cooking: '后厨制作中',
  held: '已通知后厨暂缓',
  done: '后厨已出餐',
};

// Styles as plain objects so no JSX attribute ends with ``}}>``.
const S = {
  wrap: { marginTop: '8px' },
  held: {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    fontSize: '0.8rem',
    color: '#b45309',
    fontWeight: 600,
    marginBottom: '6px',
  },
  panel: {
    marginTop: '10px',
    paddingTop: '10px',
    borderTop: '1px dashed var(--surface-border)',
  },
  textarea: {
    width: '100%',
    padding: '8px 10px',
    borderRadius: '8px',
    border: '1px solid var(--surface-border)',
    outline: 'none',
    resize: 'vertical',
    fontSize: '0.85rem',
  },
  error: { color: '#b91c1c', fontSize: '0.8rem', marginTop: '6px' },
  reply: {
    display: 'flex',
    gap: '6px',
    alignItems: 'flex-start',
    background: '#ecfdf5',
    color: '#065f46',
    padding: '8px 10px',
    borderRadius: '8px',
    fontSize: '0.85rem',
    marginTop: '6px',
  },
  icon: { flexShrink: 0, marginTop: '2px' },
  row: { display: 'flex', gap: '8px', marginTop: '8px' },
  btn: { fontSize: '0.85rem' },
  btnBusy: { fontSize: '0.85rem', opacity: 0.7 },
};

export default function KdsRequestPanel({ orderId, kdsStatus, kdsHeldReason, onSent }) {
  const [open, setOpen] = useState(false);
  const [message, setMessage] = useState('');
  const [reply, setReply] = useState('');
  const [error, setError] = useState('');
  const [sending, setSending] = useState(false);

  const send = async () => {
    const msg = message.trim();
    if (!msg) {
      setError('请先填写要传达给后厨的内容。');
      return;
    }
    setSending(true);
    setError('');
    setReply('');
    try {
      const res = await api.requestOrderKds(orderId, msg);
      setReply(res.reply || '已通知后厨。');
      setMessage('');
      if (onSent) await onSent();
    } catch (err) {
      setError(err.message || '发送失败，请稍后重试。');
    } finally {
      setSending(false);
    }
  };

  return (
    <div style={S.wrap}>
      {kdsStatus === 'held' && (
        <div style={S.held}>
          <PauseCircle size={14} />
          {KDS_LABELS.held}
          {kdsHeldReason ? `：${kdsHeldReason}` : ''}
        </div>
      )}

      {open ? (
        <div style={S.panel}>
          <textarea
            rows={2}
            placeholder='例如：先别上热菜，等人齐了再上 / 可以上菜了 / 我的菜好了吗'
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            style={S.textarea}
          />
          {error && <div style={S.error}>{error}</div>}
          {reply && (
            <div style={S.reply}>
              <ChefHat size={15} style={S.icon} />
              <span>{reply}</span>
            </div>
          )}
          <div style={S.row}>
            <button
              className='btn-primary'
              style={sending ? S.btnBusy : S.btn}
              disabled={sending}
              onClick={send}
            >
              <Send size={14} /> {sending ? '正在通知…' : '发送'}
            </button>
            <button className='btn-outline' style={S.btn} onClick={() => setOpen(false)}>
              收起
            </button>
          </div>
        </div>
      ) : (
        <button className='btn-outline' style={S.btn} onClick={() => setOpen(true)}>
          <ChefHat size={15} /> 联系后厨
        </button>
      )}
    </div>
  );
}
