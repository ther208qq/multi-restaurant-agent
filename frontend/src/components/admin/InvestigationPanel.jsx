import React, { useEffect, useMemo, useState } from 'react';
import { api } from '../../api';
import { describeEvidenceRef, evidenceAnchorId } from './evidenceRefs';
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Compass,
  HelpCircle,
  Info,
  Loader2,
  MessageSquarePlus,
  Send,
  Wrench,
} from 'lucide-react';

const EXAMPLE_QUESTIONS = [
  '最近生意为什么下降？',
  '那周末呢？',
  '上个月哪些菜品卖得不好？',
  '和别的门店比，我们是普遍下滑吗？',
];

const TOOL_LABELS = {
  resolve_time_range: '确定时间区间',
  get_diagnosis: '运行经营诊断',
  get_daily_series: '查看逐日走势',
  get_business_profile: '查看预订与时段结构',
  get_dish_daily: '查看单品逐日销量',
  compare_peers: '对比其他门店',
  get_operational_changes: '核对运营变更',
};

const STATUS_META = {
  ok: { label: '调查完成', color: '#15803d', background: '#f0fdf4', border: '#86efac' },
  insufficient_data: {
    label: '数据不足',
    color: '#b45309',
    background: '#fffbeb',
    border: '#fcd34d',
  },
  budget_exhausted: {
    label: '达到调查上限',
    color: '#b45309',
    background: '#fffbeb',
    border: '#fcd34d',
  },
  llm_unavailable: {
    label: '模型不可用 · 确定性结果',
    color: '#b91c1c',
    background: '#fef2f2',
    border: '#fecaca',
  },
};

const WINDOW_SOURCE_LABELS = {
  question: '来自问题',
  agent: 'Agent 选择',
  explicit: '请求指定',
  default: '默认最近 7 天',
  coverage: '按最近可用数据',
};

const REPORT_SECTIONS = [
  {
    key: 'facts',
    title: '已确认事实',
    icon: <CheckCircle2 size={14} />,
    emptyText: '本次没有已确认的事实。',
  },
  {
    key: 'inferences',
    title: '有证据的推断',
    icon: <Info size={14} />,
    emptyText: '现有证据不足以给出推断，不做推测。',
  },
  {
    key: 'hypotheses',
    title: '未经证实的假设',
    icon: <HelpCircle size={14} />,
    emptyText: '本次没有提出未经证实的假设。',
  },
  {
    key: 'checks',
    title: '待核查事项',
    icon: <AlertTriangle size={14} />,
    emptyText: '没有需要额外核实的事项。',
  },
];

/**
 * The investigation flattens every tool's evidence into one list. Turn the
 * server-provided labels into the same ``Map`` shape ``describeEvidenceRef``
 * expects, so a citation reads "本店营业额变化" instead of ``peers1.e1``.
 */
function buildEvidenceIndex(evidence) {
  const index = new Map();
  (evidence || []).forEach((item) => {
    if (item.ref) index.set(item.ref, { text: item.label || item.ref, evidence: item });
  });
  return index;
}

function StatusBadge({ status }) {
  const meta = STATUS_META[status] || {
    label: status,
    color: '#475569',
    background: '#f1f5f9',
    border: '#e2e8f0',
  };
  return (
    <span
      className="badge"
      style={{
        color: meta.color,
        background: meta.background,
        border: `1px solid ${meta.border}`,
        fontWeight: 500,
      }}
    >
      {meta.label}
    </span>
  );
}

function RefChips({ refs, refIndex }) {
  const unique = [...new Set(refs || [])];
  if (unique.length === 0) return null;
  return (
    <div style={{ marginTop: '2px', fontSize: '0.75rem', color: 'var(--text-muted)' }}>
      依据：
      {unique.map((ref) => (
        <a
          key={ref}
          href={`#${evidenceAnchorId(ref)}`}
          title={`证据 ${ref}（点击跳到对应证据）`}
          style={{
            display: 'inline-block',
            marginRight: '4px',
            padding: '1px 7px',
            borderRadius: '999px',
            background: '#f1f5f9',
            color: '#475569',
            textDecoration: 'none',
          }}
        >
          {describeEvidenceRef(ref, refIndex)}
        </a>
      ))}
    </div>
  );
}

function ClaimSection({ section, claims, refIndex }) {
  return (
    <div style={{ marginTop: '0.75rem' }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '6px',
          fontSize: '0.85rem',
          fontWeight: 600,
          color: 'var(--text-muted)',
        }}
      >
        {section.icon} {section.title}
      </div>
      {(!claims || claims.length === 0) ? (
        <div style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginTop: '4px' }}>
          {section.emptyText}
        </div>
      ) : (
        <ul style={{ margin: '6px 0 0 0', paddingLeft: '1.1rem', fontSize: '0.875rem' }}>
          {claims.map((claim, index) => (
            <li key={index} style={{ marginBottom: '6px' }}>
              {claim.text}
              {claim.status === 'unverified' && section.key === 'hypotheses' && (
                <span
                  className="badge badge-warning"
                  style={{ marginLeft: '6px', fontWeight: 500, fontSize: '0.7rem' }}
                >
                  未经证实
                </span>
              )}
              <RefChips refs={claim.evidence_refs} refIndex={refIndex} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function WindowLine({ window: windowInfo }) {
  if (!windowInfo) return null;
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        flexWrap: 'wrap',
        fontSize: '0.8rem',
        color: 'var(--text-muted)',
      }}
    >
      <Compass size={14} />
      <span>
        Agent 使用区间 <strong>{windowInfo.start_date}</strong> 至{' '}
        <strong>{windowInfo.end_date}</strong>，对比上期{' '}
        {windowInfo.baseline_start_date} 至 {windowInfo.baseline_end_date}
      </span>
      <span className="badge badge-info" style={{ fontWeight: 500 }}>
        {WINDOW_SOURCE_LABELS[windowInfo.source] || windowInfo.source}
      </span>
      {windowInfo.partial && (
        <span className="badge badge-warning" style={{ fontWeight: 500 }}>
          区间未结束
        </span>
      )}
      {windowInfo.expression && (
        <span style={{ fontSize: '0.75rem' }}>识别表达：{windowInfo.expression}</span>
      )}
    </div>
  );
}

function StepTimeline({ steps }) {
  if (!steps || steps.length === 0) {
    return (
      <div style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
        本轮没有调用工具，回答来自模型直接回复。
      </div>
    );
  }
  return (
    <ol
      style={{
        listStyle: 'none',
        margin: '8px 0 0 0',
        padding: '0 0 0 1rem',
        borderLeft: '2px solid var(--surface-border)',
      }}
    >
      {steps.map((step) => (
        <li key={step.index} style={{ position: 'relative', marginBottom: '10px' }}>
          <span
            style={{
              position: 'absolute',
              left: '-1.35rem',
              top: '4px',
              width: '10px',
              height: '10px',
              borderRadius: '50%',
              background: step.status === 'error' ? 'var(--danger)' : 'var(--accent)',
            }}
          />
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
            <span style={{ fontWeight: 600, fontSize: '0.85rem' }}>
              {TOOL_LABELS[step.tool] || step.tool}
            </span>
            <span
              className="badge"
              style={{
                fontWeight: 500,
                fontSize: '0.7rem',
                color: step.status === 'error' ? '#b91c1c' : '#475569',
                background: step.status === 'error' ? '#fee2e2' : '#f1f5f9',
              }}
            >
              {step.status === 'error' ? '失败' : '完成'}
            </span>
          </div>
          <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>{step.summary}</div>
        </li>
      ))}
    </ol>
  );
}

function InvestigationResult({ result }) {
  const refIndex = useMemo(() => buildEvidenceIndex(result.evidence), [result]);
  const report = result.report || {};
  const budget = result.budget || {};

  return (
    <div
      style={{
        background: 'white',
        border: '1px solid var(--surface-border)',
        borderRadius: 'var(--radius-lg)',
        padding: '1.25rem',
        display: 'flex',
        flexDirection: 'column',
        gap: '0.9rem',
      }}
    >
      <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
        <strong style={{ fontSize: '0.95rem' }}>{result.branch_name} · 调查结论</strong>
        <StatusBadge status={result.status} />
        <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
          工具调用 {budget.tool_calls_used || 0}/{budget.tool_calls_limit || 0}　循环上限{' '}
          {budget.recursion_limit || 0}
        </span>
        {report.dropped > 0 && (
          <span className="badge badge-warning" style={{ fontWeight: 500 }}>
            已丢弃 {report.dropped} 条无法验证的结论
          </span>
        )}
      </div>

      <WindowLine window={result.resolved_window} />

      {result.status === 'llm_unavailable' && (
        <div style={{ fontSize: '0.85rem', color: '#b91c1c' }}>
          对话模型当前不可用，本次使用确定性诊断与规则分析，未进行多步调查。
        </div>
      )}
      {result.status === 'budget_exhausted' && (
        <div style={{ fontSize: '0.85rem', color: '#b45309' }}>
          调查已达到工具调用或循环上限，结论只覆盖已检查的部分；未查项见“待核查事项”。
        </div>
      )}

      <div
        style={{
          whiteSpace: 'pre-wrap',
          fontSize: '0.925rem',
          lineHeight: 1.7,
          background: '#f8fafc',
          borderRadius: 'var(--radius-md)',
          padding: '0.9rem 1rem',
        }}
      >
        {result.answer}
      </div>

      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontWeight: 600 }}>
          <Wrench size={15} /> 调查步骤
        </div>
        <StepTimeline steps={result.steps} />
      </div>

      <div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontWeight: 600 }}>
          <Info size={15} /> 分组结论
        </div>
        {REPORT_SECTIONS.map((section) => (
          <ClaimSection
            key={section.key}
            section={section}
            claims={report[section.key]}
            refIndex={refIndex}
          />
        ))}
      </div>

      {result.evidence && result.evidence.length > 0 && (
        <details>
          <summary style={{ cursor: 'pointer', fontSize: '0.85rem', color: 'var(--text-muted)' }}>
            本次调查引用的证据（{result.evidence.length} 条）
          </summary>
          <div style={{ overflowX: 'auto' }}>
            <table className="data-table" style={{ marginTop: '6px' }}>
              <tbody>
                {result.evidence.map((item) => (
                  <tr
                    key={item.ref}
                    id={evidenceAnchorId(item.ref)}
                    style={{ scrollMarginTop: '96px' }}
                  >
                    <td style={{ whiteSpace: 'nowrap' }}>{item.label}</td>
                    <td>{item.value === null || item.value === undefined ? '无数据' : String(item.value)}</td>
                    <td style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>
                      {item.detail}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}

      {result.notes && result.notes.length > 0 && (
        <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>
          {result.notes.map((note, index) => (
            <div key={index}>· {note}</div>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Agentic store investigation, next to the one-click diagnosis.
 *
 * The difference the user sees is the question box: they no longer choose the
 * window by hand -- the Agent reads “上周”“周末”“上个月” from the question and
 * resolves the baseline itself. ``thread_id`` keeps follow-ups on the same
 * LangGraph checkpoint, so “那周末呢？” is a continuation, not a new topic.
 *
 * The one-click panel below remains untouched, so a manager who only wants the
 * deterministic report never pays for a second model call.
 */
export default function InvestigationPanel({ branchId }) {
  const [question, setQuestion] = useState('');
  const [threadId, setThreadId] = useState(null);
  const [turns, setTurns] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // A thread is bound to one branch on the server. Carrying it across a branch
  // switch would be rejected with 403, so start a fresh conversation instead.
  useEffect(() => {
    setThreadId(null);
    setTurns([]);
    setError('');
  }, [branchId]);

  const submit = async (event) => {
    event.preventDefault();
    const text = question.trim();
    if (!text || loading) return;
    setLoading(true);
    setError('');
    try {
      const payload = { question: text };
      if (branchId) payload.branch_id = branchId;
      if (threadId) payload.thread_id = threadId;
      const result = await api.investigateDiagnosis(payload);
      setThreadId(result.thread_id);
      setTurns((previous) => [...previous, { question: text, result }]);
      setQuestion('');
    } catch (err) {
      setError(err.message || '调查失败，请稍后重试。');
    } finally {
      setLoading(false);
    }
  };

  const startOver = () => {
    setThreadId(null);
    setTurns([]);
    setError('');
    setQuestion('');
  };

  return (
    <div
      style={{
        background: 'white',
        border: '1px solid var(--surface-border)',
        borderRadius: 'var(--radius-lg)',
        padding: '1.25rem',
        display: 'flex',
        flexDirection: 'column',
        gap: '0.9rem',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', flexWrap: 'wrap' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 700 }}>
            <MessageSquarePlus size={18} /> 经营调查 Agent
          </div>
          <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginTop: '2px' }}>
            直接提问，Agent 会自己选择时间区间、调用只读工具并给出带证据的结论。
          </div>
        </div>
        {threadId && (
          <button onClick={startOver} className="btn-outline btn-sm" type="button">
            新对话
          </button>
        )}
      </div>

      <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
        {EXAMPLE_QUESTIONS.map((example) => (
          <button
            key={example}
            type="button"
            className="btn-outline btn-sm"
            onClick={() => setQuestion(example)}
            disabled={loading}
          >
            {example}
          </button>
        ))}
      </div>

      <form onSubmit={submit} style={{ display: 'flex', gap: '8px' }}>
        <input
          type="text"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder={threadId ? '继续追问，例如“那周末呢？”' : '例如：最近生意为什么下降？'}
          maxLength={500}
          disabled={loading}
          style={{
            flex: 1,
            minWidth: 0,
            padding: '9px 12px',
            borderRadius: '8px',
            border: '1px solid var(--surface-border)',
          }}
        />
        <button
          type="submit"
          className="btn-primary btn-sm"
          disabled={loading || !question.trim()}
          style={{ display: 'flex', alignItems: 'center', gap: '6px', whiteSpace: 'nowrap' }}
        >
          {loading ? <Loader2 size={15} className="spin" /> : <Send size={15} />}
          {loading ? '调查中…' : '提问'}
        </button>
      </form>

      {error && (
        <div
          style={{
            display: 'flex',
            gap: '8px',
            alignItems: 'center',
            background: '#fee2e2',
            color: '#b91c1c',
            padding: '10px 14px',
            borderRadius: '8px',
            fontSize: '0.875rem',
          }}
        >
          <AlertCircle size={16} />
          <span>{error}</span>
        </div>
      )}

      {turns.map((turn, index) => {
        const last = index === turns.length - 1;
        if (!last) {
          return (
            <details key={index} style={{ fontSize: '0.85rem' }}>
              <summary style={{ cursor: 'pointer', color: 'var(--text-muted)' }}>
                追问 {index + 1}：{turn.question}
              </summary>
              <div style={{ marginTop: '6px', whiteSpace: 'pre-wrap' }}>{turn.result.answer}</div>
            </details>
          );
        }
        return (
          <div key={index} style={{ display: 'flex', flexDirection: 'column', gap: '0.6rem' }}>
            <div
              style={{
                alignSelf: 'flex-end',
                maxWidth: '85%',
                background: 'var(--primary-light)',
                border: '1px solid #fed7aa',
                borderRadius: '12px 12px 2px 12px',
                padding: '8px 12px',
                fontSize: '0.9rem',
              }}
            >
              {turn.question}
            </div>
            <InvestigationResult result={turn.result} />
          </div>
        );
      })}
    </div>
  );
}
