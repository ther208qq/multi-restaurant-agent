import React, { useState, useEffect, useRef } from 'react';
import { api } from '../../api';
import { Send, Bot, User, Sparkles, AlertCircle, RefreshCw } from 'lucide-react';

export default function ChatWidget({
  currentCustomer,
  selectedBranch,
  onRequireAuth,
  prefilledPrompt,
  onClearPrefill,
}) {
  const [messages, setMessages] = useState([
    {
      id: 1,
      sender: 'assistant',
      text: "您好！欢迎光临 GourmetBistro。我是由 DeepSeek R1 驱动的 AI 点餐助手。今天有什么可以帮您？您可以查看菜单、点餐，或预订座位（自动即时确认）！",
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    },
  ]);
  const [inputText, setInputText] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const chatMessagesRef = useRef(null);

  const quickChips = [
    "菜单上有什么？",
    "今晚 7 点订一张 4 人桌",
    "点 2 份玛格丽特披萨",
    "你们在哪里？营业时间是？",
    "查一下我最近订单的状态",
  ];

  // Auto-scroll messages container to bottom without scrolling window
  const scrollToBottom = () => {
    if (chatMessagesRef.current) {
      chatMessagesRef.current.scrollTop = chatMessagesRef.current.scrollHeight;
    }
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, loading]);

  // Handle prefilled prompt from MenuExplorer
  useEffect(() => {
    if (prefilledPrompt) {
      handleSendMessage(prefilledPrompt);
      if (onClearPrefill) onClearPrefill();
    }
  }, [prefilledPrompt]);

  const handleSendMessage = async (textToSend = null) => {
    const text = (textToSend || inputText).trim();
    if (!text) return;

    // Guard: Require customer authentication
    if (!currentCustomer) {
      onRequireAuth();
      return;
    }

    const userMessage = {
      id: Date.now(),
      sender: 'user',
      text,
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    };

    setMessages((prev) => [...prev, userMessage]);
    if (!textToSend) setInputText('');
    setLoading(true);
    setError('');

    try {
      const branchId = selectedBranch ? selectedBranch.id : null;
      const response = await api.sendMessage(text, branchId);

      const botMessage = {
        id: Date.now() + 1,
        sender: 'assistant',
        text: response.reply,
        active_flow: response.active_flow,
        order_id: response.order_id,
        reservation_id: response.reservation_id,
        timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      };

      setMessages((prev) => [...prev, botMessage]);
    } catch (err) {
      setError(err.message || '与 AI 助手通信失败。');
      setMessages((prev) => [
        ...prev,
        {
          id: Date.now() + 1,
          sender: 'assistant',
          text: `⚠️ 出错：${err.message || '无法连接 AI 助手，请重试。'}`,
          isError: true,
          timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        },
      ]);
    } finally {
      setLoading(false);
    }
  };

  const handleResetChat = () => {
    setMessages([
      {
        id: 1,
        sender: 'assistant',
        text: `欢迎！我是 ${selectedBranch?.name || 'GourmetBistro'} 的 AI 助手，可以协助您点餐、查看菜品或预订座位。`,
        timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      },
    ]);
  };

  return (
    <div className="chat-main">
      {/* Chat Header */}
      <div className="chat-header">
        <div className="chat-agent-info">
          <div
            style={{
              width: '38px',
              height: '38px',
              borderRadius: '50%',
              background: 'linear-gradient(135deg, #f97316 0%, #ea580c 100%)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'white',
            }}
          >
            <Bot size={20} />
          </div>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <span style={{ fontWeight: 700, fontSize: '0.95rem', color: 'var(--secondary)' }}>
                BistroAI 助手
              </span>
              <span className="status-dot" title="已连接 Ollama DeepSeek R1" />
            </div>
            <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
              {selectedBranch ? `${selectedBranch.name} • DeepSeek R1 8B` : '请选择门店'}
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <button
            onClick={handleResetChat}
            className="btn-outline btn-sm"
            style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '0.75rem' }}
            title="开始新对话"
          >
            <RefreshCw size={13} /> 重置
          </button>
        </div>
      </div>

      {/* Messages List */}
      <div className="chat-messages" ref={chatMessagesRef}>
        {messages.map((m) => (
          <div
            key={m.id}
            className={`message-bubble ${m.sender}`}
            style={{
              background: m.isError ? '#fee2e2' : undefined,
              color: m.isError ? '#991b1b' : undefined,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px' }}>
              {m.sender === 'assistant' ? (
                <Bot size={14} style={{ color: 'var(--primary)' }} />
              ) : (
                <User size={14} style={{ color: 'white' }} />
              )}
              <span style={{ fontSize: '0.75rem', fontWeight: 600, opacity: 0.8 }}>
                {m.sender === 'assistant' ? 'AI 助手' : currentCustomer?.name || '你'}
              </span>
              <span style={{ fontSize: '0.7rem', opacity: 0.6, marginLeft: 'auto' }}>
                {m.timestamp}
              </span>
            </div>

            <div>{m.text}</div>

            {m.order_id && (
              <div
                style={{
                  marginTop: '6px',
                  background: 'white',
                  color: 'var(--text-main)',
                  padding: '4px 8px',
                  borderRadius: '6px',
                  fontSize: '0.75rem',
                  fontWeight: 600,
                  display: 'inline-block',
                }}
              >
                🎉 Order #{m.order_id} 已确认
              </div>
            )}

            {m.reservation_id && (
              <div
                style={{
                  marginTop: '6px',
                  background: 'white',
                  color: 'var(--text-main)',
                  padding: '4px 8px',
                  borderRadius: '6px',
                  fontSize: '0.75rem',
                  fontWeight: 600,
                  display: 'inline-block',
                }}
              >
                ✅ Reservation #{m.reservation_id} 已自动确认
              </div>
            )}
          </div>
        ))}

        {loading && (
          <div className="message-bubble assistant" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Sparkles size={16} className="spin" style={{ color: 'var(--primary)' }} />
            <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
              DeepSeek R1 正在推理并检查餐厅可订情况…
            </span>
          </div>
        )}
      </div>

      {/* Suggested Quick Action Chips */}
      <div className="chat-chips">
        {quickChips.map((chip, idx) => (
          <button
            key={idx}
            className="chip-btn"
            onClick={() => handleSendMessage(chip)}
            disabled={loading}
          >
            {chip}
          </button>
        ))}
      </div>

      {/* Input Area */}
      <div className="chat-input-area">
        {!currentCustomer ? (
          <div
            style={{
              width: '100%',
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '8px 12px',
              background: 'var(--primary-light)',
              borderRadius: '8px',
              fontSize: '0.85rem',
            }}
          >
            <span style={{ color: '#9a3412', fontWeight: 500 }}>
              请先登录，即可与 AI 助手对话、下单和预订座位。
            </span>
            <button onClick={onRequireAuth} className="btn-primary btn-sm">
              登录 / 注册
            </button>
          </div>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              handleSendMessage();
            }}
            style={{ display: 'flex', width: '100%', gap: '8px' }}
          >
            <input
              type="text"
              placeholder={`咨询 ${selectedBranch?.name || '本餐厅'}、点餐，或预订座位…`}
              value={inputText}
              onChange={(e) => setInputText(e.target.value)}
              disabled={loading}
            />
            <button
              type="submit"
              className="btn-primary"
              disabled={loading || !inputText.trim()}
              style={{ padding: '0 18px' }}
            >
              <Send size={16} />
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
