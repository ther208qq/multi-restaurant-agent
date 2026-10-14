import React, { useEffect, useState } from 'react';
import { api } from '../../api';
import { formatWallClockDateTime } from '../../utils/datetime';
import { X, AlertCircle, Clock, CheckCircle2 } from 'lucide-react';

const AREAS = [
{ value: 'any', label: '不限' },
{ value: 'indoor', label: '室内' },
{ value: 'outdoor', label: '户外露台' },
{ value: 'rooftop', label: '天台花园' },
];

const pad = (n) => String(n).padStart(2, '0');
const localDateStr = () => {
const d = new Date();
return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
};

/**
 * Table booking form. The customer chooses a date, time, party size and an
 * area; the system assigns the table (the customer never picks a table number).
 * Availability is refreshed as the slot changes so the guest sees how many
 * tables are still free before submitting. The booking lands as pending and
 * waits for staff confirmation.
 */
export default function ReservationModal({ isOpen, onClose, branch, onBooked, onViewHistory }) {
const [date, setDate] = useState(localDateStr());
const [time, setTime] = useState('19:00');
const [partySize, setPartySize] = useState(2);
const [area, setArea] = useState('any');
const [notes, setNotes] = useState('');
const [slots, setSlots] = useState(null);
const [availError, setAvailError] = useState('');
const [checking, setChecking] = useState(false);
const [error, setError] = useState('');
const [booking, setBooking] = useState(false);
const [booked, setBooked] = useState(null);

useEffect(() => {
if (isOpen) {
setDate(localDateStr());
setTime('19:00');
setPartySize(2);
setArea('any');
setNotes('');
setSlots(null);
setAvailError('');
setError('');
setBooked(null);
}
}, [isOpen]);

useEffect(() => {
if (!isOpen || !branch || !date || !time) return;
let cancelled = false;
setChecking(true);
api
.getReservationAvailability({ branch_id: branch.id, date: date, time: time, party_size: partySize, seating_area: area })
.then((res) => { if (!cancelled) { setSlots(res); setAvailError(''); } })
.catch((err) => { if (!cancelled) { setSlots(null); setAvailError(err.message || ''); } })
.finally(() => { if (!cancelled) setChecking(false); });
return () => { cancelled = true; };
}, [isOpen, branch, date, time, partySize, area]);

if (!isOpen) return null;

const handleClose = () => { if (booking) return; onClose(); };

const handleSubmit = async () => {
setError('');
if (!date || !time) { setError('请选择日期和时间。'); return; }
setBooking(true);
try {
const res = await api.createReservation({
branch_id: branch.id,
party_size: partySize,
requested_date: date,
requested_time: time,
seating_area_preference: area,
notes: notes.trim() || null,
});
setBooked(res);
} catch (err) {
setError(err.message || '预订失败，请重试。');
} finally {
setBooking(false);
}
};

return (
<div className='modal-overlay' onClick={handleClose}>
<div className='modal-content' style={{ maxWidth: '480px' }} onClick={(e) => e.stopPropagation()}>
<div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
<h2 style={{ fontSize: '1.25rem', fontWeight: 700, color: 'var(--secondary)' }}>{booked ? '预订成功' : '预订座位'}</h2>
<button onClick={handleClose} style={{ color: 'var(--text-muted)' }}>
<X size={20} />
</button>
</div>
{booked ? (
<div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', alignItems: 'center', textAlign: 'center', padding: '1rem 0' }}>
<CheckCircle2 size={48} style={{ color: '#15803d' }} />
<div>
<p style={{ fontWeight: 700, color: 'var(--secondary)', fontSize: '1.05rem' }}>预订已提交，等待门店确认</p>
<p style={{ color: 'var(--text-muted)', fontSize: '0.9rem', marginTop: '4px' }}>
{booked.table_label ? '已为您安排 ' + booked.table_label + ' 号桌 · ' : ''}{booked.party_size} 人 · {formatWallClockDateTime(booked.start_time)}
</p>
</div>
<div style={{ display: 'flex', gap: '8px', marginTop: '0.5rem' }}>
<button className='btn-outline' onClick={() => { onBooked(); onViewHistory(); }}>查看我的记录</button>
<button className='btn-primary' onClick={onBooked}>完成</button>
</div>
</div>
) : (
<div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
{error && (
<div style={{ display: 'flex', gap: '8px', alignItems: 'center', background: '#fee2e2', color: '#b91c1c', padding: '8px 12px', borderRadius: '8px', fontSize: '0.85rem' }}>
<AlertCircle size={16} />
<span>{error}</span>
</div>
)}
<div style={{ display: 'flex', gap: '10px' }}>
<div style={{ flex: 1 }}>
<label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: '6px' }}>用餐日期</label>
<input type='date' value={date} onChange={(e) => setDate(e.target.value)} style={{ width: '100%', padding: '9px 10px', borderRadius: '8px', border: '1px solid var(--surface-border)', outline: 'none' }} />
</div>
<div style={{ flex: 1 }}>
<label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: '6px' }}>用餐时间</label>
<input type='time' value={time} onChange={(e) => setTime(e.target.value)} style={{ width: '100%', padding: '9px 10px', borderRadius: '8px', border: '1px solid var(--surface-border)', outline: 'none' }} />
</div>
</div>
<div style={{ display: 'flex', gap: '10px' }}>
<div style={{ flex: 1 }}>
<label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: '6px' }}>用餐人数</label>
<input type='number' min={1} value={partySize} onChange={(e) => setPartySize(Math.max(1, Number(e.target.value) || 1))} style={{ width: '100%', padding: '9px 10px', borderRadius: '8px', border: '1px solid var(--surface-border)', outline: 'none' }} />
</div>
<div style={{ flex: 1 }}>
<label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: '6px' }}>区域偏好</label>
<select value={area} onChange={(e) => setArea(e.target.value)} style={{ width: '100%', padding: '9px 10px', borderRadius: '8px', border: '1px solid var(--surface-border)', outline: 'none', background: 'white' }}>
{AREAS.map((a) => (<option key={a.value} value={a.value}>{a.label}</option>))}
</select>
</div>
</div>
<div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.85rem', color: availError ? '#b91c1c' : checking ? 'var(--text-muted)' : (slots && slots.length > 0 ? '#15803d' : '#b45309') }}>
<Clock size={15} />
{checking ? '正在查询可用餐桌…' : availError ? availError : slots && slots.length > 0 ? '该时段还有 ' + slots.length + ' 张桌可用' : '该时段暂无可预订的餐桌，请换个时间或区域'}
</div>
<div>
<label style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: '6px' }}>备注（选填）</label>
<textarea rows={2} placeholder='例如：靠窗、生日聚会等' value={notes} onChange={(e) => setNotes(e.target.value)} style={{ width: '100%', padding: '8px 10px', borderRadius: '8px', border: '1px solid var(--surface-border)', outline: 'none', resize: 'vertical', fontSize: '0.85rem' }} />
</div>
<button className='btn-primary' style={{ justifyContent: 'center', width: '100%', opacity: booking ? 0.7 : 1 }} disabled={booking} onClick={handleSubmit}>
{booking ? '正在提交…' : '提交预订'}
</button>
</div>
)}
</div>
</div>
);
}
