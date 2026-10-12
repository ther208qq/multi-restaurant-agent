import React, { useState } from 'react';
import OrdersManager from './OrdersManager';
import ReservationsManager from './ReservationsManager';
import MenuManager from './MenuManager';
import BranchTableManager from './BranchTableManager';
import { ShoppingBag, Calendar, Utensils, MapPin, LogOut, Shield } from 'lucide-react';

export default function AdminLayout({ currentStaff, branches, onLogout, onRefreshBranches }) {
  const [activeTab, setActiveTab] = useState('orders'); // 'orders' | 'reservations' | 'menu' | 'branches'

  const ROLE_LABELS = { admin: '管理员', staff: '员工' };

  return (
    <div className="admin-container">
      {/* Staff Header */}
      <div className="admin-header">
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span style={{ fontSize: '1.4rem', fontWeight: 800, color: 'var(--secondary)' }}>
              员工管理后台
            </span>
            <span className="badge badge-info" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
              <Shield size={12} /> {ROLE_LABELS[currentStaff.role] || currentStaff.role}
            </span>
          </div>
          <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginTop: '2px' }}>
            登录身份：<strong>{currentStaff.username}</strong>
            {currentStaff.branch_id
              ? ` • 门店 #${currentStaff.branch_id}`
              : ' • 全部门店（超级管理员）'}
          </p>
        </div>

        <button
          onClick={onLogout}
          className="btn-outline btn-sm"
          style={{ display: 'flex', alignItems: 'center', gap: '6px', color: '#ef4444' }}
        >
          <LogOut size={14} /> 员工退出
        </button>
      </div>

      {/* Admin Tabs */}
      <div className="admin-tabs">
        <button
          className={`admin-tab ${activeTab === 'orders' ? 'active' : ''}`}
          onClick={() => setActiveTab('orders')}
          style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
        >
          <ShoppingBag size={16} /> 实时订单
        </button>
        <button
          className={`admin-tab ${activeTab === 'reservations' ? 'active' : ''}`}
          onClick={() => setActiveTab('reservations')}
          style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
        >
          <Calendar size={16} /> 餐桌预订
        </button>
        <button
          className={`admin-tab ${activeTab === 'menu' ? 'active' : ''}`}
          onClick={() => setActiveTab('menu')}
          style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
        >
          <Utensils size={16} /> 菜单与库存
        </button>
        <button
          className={`admin-tab ${activeTab === 'branches' ? 'active' : ''}`}
          onClick={() => setActiveTab('branches')}
          style={{ display: 'flex', alignItems: 'center', gap: '6px' }}
        >
          <MapPin size={16} /> 门店与餐桌
        </button>
      </div>

      {/* Subtab Content */}
      {activeTab === 'orders' && (
        <OrdersManager currentStaff={currentStaff} branches={branches} />
      )}

      {activeTab === 'reservations' && (
        <ReservationsManager currentStaff={currentStaff} branches={branches} />
      )}

      {activeTab === 'menu' && (
        <MenuManager currentStaff={currentStaff} branches={branches} />
      )}

      {activeTab === 'branches' && (
        <BranchTableManager
          currentStaff={currentStaff}
          branches={branches}
          onRefreshBranches={onRefreshBranches}
        />
      )}
    </div>
  );
}
