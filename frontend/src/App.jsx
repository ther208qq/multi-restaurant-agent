import React, { useState, useEffect } from 'react';
import {
  api,
  getStoredCustomer,
  getStoredStaff,
  removeCustomerToken,
  removeStaffToken,
} from './api';
import Navbar from './components/Navbar';
import BranchSelector from './components/customer/BranchSelector';
import MenuExplorer from './components/customer/MenuExplorer';
import CartPanel from './components/customer/CartPanel';
import CheckoutModal from './components/customer/CheckoutModal';
import ReservationModal from './components/customer/ReservationModal';
import ChatWidget from './components/customer/ChatWidget';
import CustomerAuthModal from './components/customer/CustomerAuthModal';
import MyHistoryModal from './components/customer/MyHistoryModal';
import AdminLoginModal from './components/admin/AdminLoginModal';
import AdminLayout from './components/admin/AdminLayout';
import { Shield, Sparkles, AlertCircle } from 'lucide-react';

export default function App() {
  const [currentView, setCurrentView] = useState('customer'); // 'customer' | 'admin'
  const [branches, setBranches] = useState([]);
  const [selectedBranchId, setSelectedBranchId] = useState(null);
  const [menuData, setMenuData] = useState(null);
  const [branchFaq, setBranchFaq] = useState(null);

  // Auth States
  const [currentCustomer, setCurrentCustomer] = useState(getStoredCustomer());
  const [currentStaff, setCurrentStaff] = useState(getStoredStaff());

  // Modal States
  const [isCustomerAuthOpen, setIsCustomerAuthOpen] = useState(false);
  const [isStaffAuthOpen, setIsStaffAuthOpen] = useState(false);
  const [isMyHistoryOpen, setIsMyHistoryOpen] = useState(false);
  const [isCheckoutOpen, setIsCheckoutOpen] = useState(false);
  const [isReservationOpen, setIsReservationOpen] = useState(false);

  // Self-service cart (sidebar). Lines are keyed by item + variant so the same
  // dish in two sizes stays two lines; cleared on branch switch and after an
  // order is placed.
  const [cart, setCart] = useState([]);

  // Chat prefill trigger
  const [prefilledPrompt, setPrefilledPrompt] = useState(null);

  // Initial Data Fetch
  const loadBranches = async () => {
    try {
      const data = await api.getBranches();
      setBranches(data);
      if (data.length > 0 && !selectedBranchId) {
        setSelectedBranchId(data[0].id);
      }
    } catch (err) {
      console.error('Failed to load branches:', err);
    }
  };

  useEffect(() => {
    loadBranches();
  }, []);

  const reloadMenu = () => {
    if (selectedBranchId) {
      api.getBranchMenu(selectedBranchId)
        .then(setMenuData)
        .catch((err) => console.error('Error fetching menu:', err));
    }
  };

  // Fetch branch menu & FAQ when selected branch changes. Switching branches
  // empties the cart -- a line priced for one branch's stock must not carry over.
  useEffect(() => {
    if (selectedBranchId) {
      setCart([]);
      api.getBranchMenu(selectedBranchId)
        .then(setMenuData)
        .catch((err) => console.error('Error fetching menu:', err));

      api.getBranchFaq(selectedBranchId)
        .then(setBranchFaq)
        .catch((err) => console.error('Error fetching branch FAQ:', err));
    }
  }, [selectedBranchId]);

  // Handle Logout
  const handleCustomerLogout = () => {
    removeCustomerToken();
    setCurrentCustomer(null);
  };

  const handleStaffLogout = () => {
    removeStaffToken();
    setCurrentStaff(null);
    setCurrentView('customer');
  };

  // Self-service cart operations. A line is keyed by item + variant so the same
  // dish in two sizes stays two separate lines.
  const addToCart = (item, variant, qty) => {
    const key = `${item.id}:${variant ? variant.id : 'none'}`;
    setCart((prev) => {
      const existing = prev.find((l) => l.key === key);
      if (existing) {
        return prev.map((l) => (l.key === key ? { ...l, quantity: l.quantity + qty } : l));
      }
      return [
        ...prev,
        {
          key,
          menu_item_id: item.id,
          name: item.name,
          base_price: Number(item.base_price),
          variant_id: variant ? variant.id : null,
          variant_name: variant ? variant.variant_name : null,
          price_delta: variant ? Number(variant.price_delta) : 0,
          quantity: qty,
        },
      ];
    });
  };

  const patchCart = (key, patch) =>
    setCart((prev) => prev.map((l) => (l.key === key ? { ...l, ...patch } : l)));

  const removeCart = (key) => setCart((prev) => prev.filter((l) => l.key !== key));

  // Booking and checkout both need an account; nudge to login first, like the
  // "ask the AI" flow does.
  const handleOpenReservation = () => {
    if (!currentCustomer) {
      setIsCustomerAuthOpen(true);
    } else {
      setIsReservationOpen(true);
    }
  };

  const handleOpenCheckout = () => {
    if (!currentCustomer) {
      setIsCustomerAuthOpen(true);
    } else {
      setIsCheckoutOpen(true);
    }
  };

  // After an order lands: drop the cart and re-pull the menu (stock changed).
  const handleOrderPlaced = () => {
    setIsCheckoutOpen(false);
    setCart([]);
    reloadMenu();
  };

  const selectedBranch = branches.find((b) => b.id === selectedBranchId) || branches[0];

  return (
    <div className="app-container">
      {/* Top Navigation */}
      <Navbar
        currentView={currentView}
        onSwitchView={(v) => {
          if (v === 'admin' && !currentStaff) {
            setIsStaffAuthOpen(true);
          } else {
            setCurrentView(v);
          }
        }}
        currentCustomer={currentCustomer}
        currentStaff={currentStaff}
        onOpenCustomerAuth={() => setIsCustomerAuthOpen(true)}
        onOpenStaffAuth={() => setIsStaffAuthOpen(true)}
        onOpenMyHistory={() => setIsMyHistoryOpen(true)}
        onOpenReservation={handleOpenReservation}
        onCustomerLogout={handleCustomerLogout}
        onStaffLogout={handleStaffLogout}
      />

      {/* Main Viewport */}
      {currentView === 'customer' ? (
        <main className="chat-page">
          {/* Left Sidebar: Branch Info & Live Menu with Stock */}
          <aside className="chat-sidebar">
            <BranchSelector
              branches={branches}
              selectedBranchId={selectedBranchId}
              onSelectBranch={setSelectedBranchId}
              branchFaq={branchFaq}
            />

            <CartPanel
              items={cart}
              onPatch={patchCart}
              onRemove={removeCart}
              onCheckout={handleOpenCheckout}
            />

            <MenuExplorer
              menuData={menuData}
              onAddToCart={addToCart}
              onAskAiToOrder={(prompt) => {
                if (!currentCustomer) {
                  setIsCustomerAuthOpen(true);
                } else {
                  setPrefilledPrompt(prompt);
                }
              }}
            />
          </aside>

          {/* Right Area: Interactive Conversational AI Agent */}
          <section style={{ flex: 1, display: 'flex', minHeight: 0, minWidth: 0 }}>
            <ChatWidget
              currentCustomer={currentCustomer}
              selectedBranch={selectedBranch}
              onRequireAuth={() => setIsCustomerAuthOpen(true)}
              prefilledPrompt={prefilledPrompt}
              onClearPrefill={() => setPrefilledPrompt(null)}
            />
          </section>
        </main>
      ) : (
        <main style={{ flex: 1, background: '#f8fafc' }}>
          {currentStaff ? (
            <AdminLayout
              currentStaff={currentStaff}
              branches={branches}
              onLogout={handleStaffLogout}
              onRefreshBranches={loadBranches}
            />
          ) : (
            <div style={{ textAlign: 'center', padding: '6rem 2rem' }}>
              <Shield size={48} style={{ color: 'var(--primary)', margin: '0 auto 1rem' }} />
              <h2 style={{ fontSize: '1.5rem', fontWeight: 800, color: 'var(--secondary)', marginBottom: '0.5rem' }}>
                员工专区
              </h2>
              <p style={{ color: 'var(--text-muted)', marginBottom: '1.5rem' }}>
                请使用员工或管理员账号登录，以管理餐厅运营。
              </p>
              <button onClick={() => setIsStaffAuthOpen(true)} className="btn-primary">
                员工登录
              </button>
            </div>
          )}
        </main>
      )}

      {/* Modals */}
      <CustomerAuthModal
        isOpen={isCustomerAuthOpen}
        onClose={() => setIsCustomerAuthOpen(false)}
        onAuthSuccess={(cust) => setCurrentCustomer(cust)}
      />

      <MyHistoryModal
        isOpen={isMyHistoryOpen}
        onClose={() => setIsMyHistoryOpen(false)}
      />

      <CheckoutModal
        isOpen={isCheckoutOpen}
        onClose={() => setIsCheckoutOpen(false)}
        branch={selectedBranch}
        items={cart}
        onOrderPlaced={handleOrderPlaced}
        onViewHistory={() => setIsMyHistoryOpen(true)}
      />

      <ReservationModal
        isOpen={isReservationOpen}
        onClose={() => setIsReservationOpen(false)}
        branch={selectedBranch}
        onBooked={() => setIsReservationOpen(false)}
        onViewHistory={() => setIsMyHistoryOpen(true)}
      />

      <AdminLoginModal
        isOpen={isStaffAuthOpen}
        onClose={() => setIsStaffAuthOpen(false)}
        onLoginSuccess={(staff) => {
          setCurrentStaff(staff);
          setCurrentView('admin');
        }}
      />
    </div>
  );
}
