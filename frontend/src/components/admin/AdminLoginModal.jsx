import React, { useEffect, useState } from 'react';
import { api } from '../../api';
import { X, Lock, User, AlertCircle, Shield } from 'lucide-react';

// The accounts differ by database: the development DB is seeded by
// backend/app/seed.py, the isolated evaluation DB by the public-dataset
// import. The backend reports which one is live, so a hint is never shown for
// accounts that cannot log in.
const SEED_ACCOUNTS = {
  development: {
    title: '开发数据',
    placeholder: 'admin 或 downtown_staff',
    accounts: [
      { label: '管理员', username: 'admin', password: 'admin123' },
      { label: '市中心门店员工', username: 'downtown_staff', password: 'staff123' },
    ],
  },
  evaluation: {
    title: '评估数据（公开销售数据集）',
    placeholder: 'eval_admin 或 eval_staff',
    accounts: [
      { label: '评估管理员', username: 'eval_admin', password: 'admin123' },
      { label: '评估门店员工', username: 'eval_staff', password: 'staff123' },
    ],
  },
};

export default function AdminLoginModal({ isOpen, onClose, onLoginSuccess }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [dataset, setDataset] = useState(null);

  useEffect(() => {
    if (!isOpen) return undefined;
    let cancelled = false;
    setDataset(null);
    api.getLoginHints()
      .then((res) => {
        if (!cancelled) setDataset(res?.dataset || null);
      })
      .catch(() => {
        if (!cancelled) setDataset(null);
      });
    return () => {
      cancelled = true;
    };
  }, [isOpen]);

  if (!isOpen) return null;

  const hint = SEED_ACCOUNTS[dataset];

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      const res = await api.staffLogin(username.trim(), password);
      onLoginSuccess(res.user);
      onClose();
    } catch (err) {
      setError(err.message || '员工账号或密码错误。');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Shield size={20} style={{ color: 'var(--primary)' }} />
            <h2 style={{ fontSize: '1.25rem', fontWeight: 700, color: 'var(--secondary)' }}>
              员工与管理员登录
            </h2>
          </div>
          <button onClick={onClose} style={{ color: 'var(--text-muted)' }}>
            <X size={20} />
          </button>
        </div>

        {error && (
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', background: '#fee2e2', color: '#b91c1c', padding: '10px 12px', borderRadius: '8px', fontSize: '0.875rem', marginBottom: '1rem' }}>
            <AlertCircle size={16} />
            <span>{error}</span>
          </div>
        )}

        <div style={{ background: '#f8fafc', padding: '10px', borderRadius: '8px', fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: '1rem' }}>
          {hint ? (
            <>
              <strong>当前后端：{hint.title}</strong>
              {hint.accounts.map((account) => (
                <div key={account.username}>
                  • {account.label}：<code>{account.username}</code> / <code>{account.password}</code>
                </div>
              ))}
            </>
          ) : (
            <span>登录账号取决于后端当前连接的数据库（开发库或评估库）。</span>
          )}
        </div>

        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          <div>
            <label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: '4px' }}>用户名</label>
            <div style={{ position: 'relative', display: 'flex', alignItems: 'center' }}>
              <User size={16} style={{ position: 'absolute', left: 10, color: 'var(--text-muted)' }} />
              <input
                type="text"
                required
                placeholder={hint?.placeholder || '请输入员工用户名'}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                style={{ width: '100%', padding: '9px 10px 9px 34px', borderRadius: '8px', border: '1px solid var(--surface-border)', outline: 'none' }}
              />
            </div>
          </div>

          <div>
            <label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: '4px' }}>密码</label>
            <div style={{ position: 'relative', display: 'flex', alignItems: 'center' }}>
              <Lock size={16} style={{ position: 'absolute', left: 10, color: 'var(--text-muted)' }} />
              <input
                type="password"
                required
                placeholder="••••••••"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                style={{ width: '100%', padding: '9px 10px 9px 34px', borderRadius: '8px', border: '1px solid var(--surface-border)', outline: 'none' }}
              />
            </div>
          </div>

          <button
            type="submit"
            disabled={loading}
            className="btn-primary"
            style={{ justifyContent: 'center', marginTop: '0.5rem', width: '100%', opacity: loading ? 0.7 : 1 }}
          >
            {loading ? '正在验证…' : '登录后台'}
          </button>
        </form>
      </div>
    </div>
  );
}
