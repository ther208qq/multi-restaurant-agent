import React, { useState } from 'react';
import { api } from '../../api';
import { X, Lock, Mail, User, Phone, AlertCircle } from 'lucide-react';

export default function CustomerAuthModal({ isOpen, onClose, onAuthSuccess }) {
  const [isRegister, setIsRegister] = useState(false);
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [phone, setPhone] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  React.useEffect(() => {
    if (isOpen) {
      setIsRegister(false);
      setName('');
      setEmail('');
      setPhone('');
      setPassword('');
      setError('');
    }
  }, [isOpen]);

  if (!isOpen) return null;

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      if (isRegister) {
        if (!name.trim()) throw new Error('请填写姓名');
        const res = await api.customerRegister({
          name: name.trim(),
          email: email.trim(),
          phone: phone.trim() || undefined,
          password,
        });
        onAuthSuccess(res.user);
      } else {
        const res = await api.customerLogin(email.trim(), password);
        onAuthSuccess(res.user);
      }
      onClose();
    } catch (err) {
      setError(err.message || '认证失败，请重试。');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem' }}>
          <h2 style={{ fontSize: '1.25rem', fontWeight: 700, color: 'var(--secondary)' }}>
            {isRegister ? '注册顾客账号' : '顾客登录'}
          </h2>
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

        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          {isRegister && (
            <>
              <div>
                <label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: '4px' }}>姓名</label>
                <div style={{ position: 'relative', display: 'flex', alignItems: 'center' }}>
                  <User size={16} style={{ position: 'absolute', left: 10, color: 'var(--text-muted)' }} />
                  <input
                    type="text"
                    required
                    placeholder="张三"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    style={{ width: '100%', padding: '9px 10px 9px 34px', borderRadius: '8px', border: '1px solid var(--surface-border)', outline: 'none' }}
                  />
                </div>
              </div>

              <div>
                <label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: '4px' }}>手机号（选填）</label>
                <div style={{ position: 'relative', display: 'flex', alignItems: 'center' }}>
                  <Phone size={16} style={{ position: 'absolute', left: 10, color: 'var(--text-muted)' }} />
                  <input
                    type="tel"
                    placeholder="138 0000 0000"
                    value={phone}
                    onChange={(e) => setPhone(e.target.value)}
                    style={{ width: '100%', padding: '9px 10px 9px 34px', borderRadius: '8px', border: '1px solid var(--surface-border)', outline: 'none' }}
                  />
                </div>
              </div>
            </>
          )}

          <div>
            <label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: '4px' }}>邮箱地址</label>
            <div style={{ position: 'relative', display: 'flex', alignItems: 'center' }}>
              <Mail size={16} style={{ position: 'absolute', left: 10, color: 'var(--text-muted)' }} />
              <input
                type="email"
                required
                placeholder="customer@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
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
            {loading ? '请稍候…' : isRegister ? '注册账号' : '登录'}
          </button>
        </form>

        <div style={{ textAlign: 'center', marginTop: '1.25rem', fontSize: '0.85rem', color: 'var(--text-muted)' }}>
          {isRegister ? '已有账号？' : '还没有账号？'}{' '}
          <button
            type="button"
            onClick={() => {
              setIsRegister(!isRegister);
              setError('');
            }}
            style={{ color: 'var(--primary)', fontWeight: 600 }}
          >
            {isRegister ? '去登录' : '去注册'}
          </button>
        </div>
      </div>
    </div>
  );
}
