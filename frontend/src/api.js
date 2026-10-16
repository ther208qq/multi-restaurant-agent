/**
 * Multi-Branch Restaurant AI Agent API Client
 * Zero external client dependencies (native fetch).
 * Handles customer and staff authentication tokens, error handling, and API endpoints.
 */

const API_BASE = import.meta.env.VITE_API_BASE || '/api';

// Token Management
export const getCustomerToken = () => localStorage.getItem('customer_token');
export const setCustomerToken = (token) => localStorage.setItem('customer_token', token);
export const removeCustomerToken = () => {
  localStorage.removeItem('customer_token');
  localStorage.removeItem('customer_user');
};

export const getStaffToken = () => localStorage.getItem('staff_token');
export const setStaffToken = (token) => localStorage.setItem('staff_token', token);
export const removeStaffToken = () => {
  localStorage.removeItem('staff_token');
  localStorage.removeItem('staff_user');
};

/**
 * Tell the app that an authenticated call came back 401 (stale / expired token).
 * The app listens for ``auth:unauthorized`` and drops the dead session, then
 * opens the matching login prompt — instead of leaving the user stuck seeing
 * "无法验证凭据" on every screen.
 */
export function notifyUnauthorized(authType) {
  window.dispatchEvent(
    new CustomEvent('auth:unauthorized', { detail: { authType } })
  );
}

export const getStoredCustomer = () => {
  try {
    const raw = localStorage.getItem('customer_user');
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
};
export const setStoredCustomer = (user) => localStorage.setItem('customer_user', JSON.stringify(user));

export const getStoredStaff = () => {
  try {
    const raw = localStorage.getItem('staff_user');
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
};
export const setStoredStaff = (user) => localStorage.setItem('staff_user', JSON.stringify(user));

/**
 * Generic Fetch Helper
 */
async function request(endpoint, options = {}, authType = null) {
  const url = `${API_BASE}${endpoint}`;
  const headers = {
    'Content-Type': 'application/json',
    ...(options.headers || {}),
  };

  if (authType === 'customer') {
    const token = getCustomerToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
  } else if (authType === 'staff') {
    const token = getStaffToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;
  }

  const response = await fetch(url, {
    ...options,
    headers,
  });

  let data = null;
  // 204/205 have no body by spec, but FastAPI still stamps them with
  // ``content-type: application/json`` (Starlette writes the media type and
  // skips content-length). Trusting the header alone would run JSON.parse("")
  // and throw "Unexpected end of JSON input" on an otherwise successful DELETE.
  const contentType = response.headers.get('content-type');
  if (response.status === 204 || response.status === 205) {
    data = null;
  } else if (contentType && contentType.includes('application/json')) {
    data = await response.json();
  } else {
    data = await response.text();
  }

  if (!response.ok) {
    // An invalid / expired token: hand control to the app's auth flow so the
    // user is sent to the login prompt rather than left on an error screen.
    if (response.status === 401 && authType) {
      notifyUnauthorized(authType);
    }
    const errMsg = (data && data.detail) || response.statusText || 'An error occurred';
    const error = new Error(errMsg);
    error.status = response.status;
    error.data = data;
    throw error;
  }

  return data;
}

// Public / Customer API
export const api = {
  // Customer Auth
  customerLogin: async (email, password) => {
    const res = await request('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });
    setCustomerToken(res.access_token);
    setStoredCustomer(res.user);
    return res;
  },

  customerRegister: async (payload) => {
    const res = await request('/auth/register', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    setCustomerToken(res.access_token);
    setStoredCustomer(res.user);
    return res;
  },

  customerMe: async () => {
    const user = await request('/auth/me', { method: 'GET' }, 'customer');
    setStoredCustomer(user);
    return user;
  },

  // Branches & Menu Explorer
  getBranches: () => request('/branches', { method: 'GET' }),
  getBranchMenu: (branchId) => request(`/branches/${branchId}/menu`, { method: 'GET' }),
  getBranchFaq: (branchId) => request(`/branches/${branchId}/faq`, { method: 'GET' }),

  // Customer Orders & Reservations
  getMyOrders: () => request('/orders/me', { method: 'GET' }, 'customer'),
  getMyReservations: () => request('/reservations/me', { method: 'GET' }, 'customer'),

  // One of my orders by id -- the checkout page polls this while an order waits
  // for manager approval (AgentFeatures feature 3), to see it become placed (or
  // cancelled) without reloading the whole history.
  getOrder: (orderId) => request(`/orders/${orderId}`, { method: 'GET' }, 'customer'),

  // Cancel one of my own orders (stock released). The customer decides this --
  // e.g. after a gift request was declined and they no longer want the order.
  cancelOrder: (orderId) =>
    request(`/orders/${orderId}/cancel`, { method: 'POST' }, 'customer'),

  // Customer self-service ordering & booking
  previewOrder: (payload) =>
    request(
      '/orders/preview',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
      'customer'
    ),

  createOrder: (payload) =>
    request(
      '/orders',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
      'customer'
    ),

  getReservationAvailability: (params) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== '')
    ).toString();
    return request(`/reservations/availability?${qs}`, { method: 'GET' }, 'customer');
  },

  createReservation: (payload) =>
    request(
      '/reservations',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
      'customer'
    ),

  // Accept the booking agent's counter-offer (a nearby time plus a gift) when
  // the requested slot is full.
  acceptReservationOffer: (payload) =>
    request(
      '/reservations/accept-offer',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
      'customer'
    ),

  // Contact the kitchen about one of my orders (AgentFeatures feature 1). The
  // message is classified server-side (hold / resume / status) and the agent
  // replies; the endpoint returns { reply }.
  requestOrderKds: (orderId, message) =>
    request(
      `/agent/order/${orderId}/request`,
      {
        method: 'POST',
        body: JSON.stringify({ message }),
      },
      'customer'
    ),

  // Staff Auth
  staffLogin: async (username, password) => {
    const res = await request('/staff/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    setStaffToken(res.access_token);
    setStoredStaff(res.user);
    return res;
  },

  staffMe: async () => {
    const user = await request('/staff/me', { method: 'GET' }, 'staff');
    setStoredStaff(user);
    return user;
  },

  // Staff Orders
  getStaffOrders: (branchId = null, status = null) => {
    const params = new URLSearchParams();
    if (branchId) params.append('branch_id', branchId);
    if (status) params.append('status', status);
    const qs = params.toString() ? `?${params.toString()}` : '';
    return request(`/staff/orders${qs}`, { method: 'GET' }, 'staff');
  },

  updateOrderStatus: (orderId, status) =>
    request(
      `/staff/orders/${orderId}/status`,
      {
        method: 'PATCH',
        body: JSON.stringify({ status }),
      },
      'staff'
    ),

  // Advance the order's simulated KDS state (kitchen side): queued -> cooking ->
  // done, or send a held task back to the queue.
  updateOrderKds: (orderId, status) =>
    request(
      `/staff/orders/${orderId}/kds`,
      {
        method: 'PATCH',
        body: JSON.stringify({ status }),
      },
      'staff'
    ),

  // Staff Approvals (AgentFeatures feature 3): orders gated behind a manager's
  // decision on a freebie request. Approve takes the manager's chosen gift_id;
  // the server re-validates it against live stock.
  getStaffApprovals: (branchId = null, status = null) => {
    const params = new URLSearchParams();
    if (branchId) params.append('branch_id', branchId);
    if (status) params.append('status', status);
    const qs = params.toString() ? `?${params.toString()}` : '';
    return request(`/staff/approvals${qs}`, { method: 'GET' }, 'staff');
  },

  approveApproval: (approvalId, giftId) =>
    request(
      `/staff/approvals/${approvalId}/approve`,
      {
        method: 'POST',
        body: JSON.stringify({ gift_id: giftId }),
      },
      'staff'
    ),

  rejectApproval: (approvalId) =>
    request(`/staff/approvals/${approvalId}/reject`, { method: 'POST' }, 'staff'),

  // Staff Reservations
  getStaffReservations: (branchId = null, status = null, date = null) => {
    const params = new URLSearchParams();
    if (branchId) params.append('branch_id', branchId);
    if (status) params.append('status', status);
    if (date) params.append('date', date);
    const qs = params.toString() ? `?${params.toString()}` : '';
    return request(`/staff/reservations${qs}`, { method: 'GET' }, 'staff');
  },

  updateReservation: (resId, payload) =>
    request(
      `/staff/reservations/${resId}`,
      {
        method: 'PATCH',
        body: JSON.stringify(payload),
      },
      'staff'
    ),

  // Staff Audit Log
  getAuditLogs: (branchId = null, action = null, entityType = null, limit = null) => {
    const params = new URLSearchParams();
    if (branchId) params.append('branch_id', branchId);
    if (action) params.append('action', action);
    if (entityType) params.append('entity_type', entityType);
    if (limit) params.append('limit', limit);
    const qs = params.toString() ? `?${params.toString()}` : '';
    return request(`/staff/audit-logs${qs}`, { method: 'GET' }, 'staff');
  },

  // Staff Statistics
  getStats: (branchId = null, startDate = null, endDate = null, top = null) => {
    const params = new URLSearchParams();
    if (branchId) params.append('branch_id', branchId);
    if (startDate) params.append('start_date', startDate);
    if (endDate) params.append('end_date', endDate);
    if (top) params.append('top', top);
    const qs = params.toString() ? `?${params.toString()}` : '';
    return request(`/staff/stats${qs}`, { method: 'GET' }, 'staff');
  },

  // Staff Store-Anomaly Diagnosis
  getDiagnosis: (branchId = null, startDate = null, endDate = null) => {
    const params = new URLSearchParams();
    if (branchId) params.append('branch_id', branchId);
    if (startDate) params.append('start_date', startDate);
    if (endDate) params.append('end_date', endDate);
    const qs = params.toString() ? `?${params.toString()}` : '';
    return request(`/staff/diagnosis${qs}`, { method: 'GET' }, 'staff');
  },

  // Staff Menu & Stock
  getStaffCategories: (branchId = null) => {    const qs = branchId ? `?branch_id=${branchId}` : '';
    return request(`/staff/menu/categories${qs}`, { method: 'GET' }, 'staff');
  },

  createCategory: (payload) =>
    request(
      '/staff/menu/categories',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
      'staff'
    ),

  createMenuItem: (payload) =>
    request(
      '/staff/menu/items',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
      'staff'
    ),

  updateMenuItem: (itemId, payload) =>
    request(
      `/staff/menu/items/${itemId}`,
      {
        method: 'PATCH',
        body: JSON.stringify(payload),
      },
      'staff'
    ),

  updateItemStock: (menuItemId, branchId, stockQuantity) =>
    request(
      '/staff/menu/stock',
      {
        method: 'PUT',
        body: JSON.stringify({
          menu_item_id: menuItemId,
          branch_id: branchId,
          stock_quantity: stockQuantity,
        }),
      },
      'staff'
    ),

  // Staff Branches & Tables
  getStaffBranches: () => request('/staff/branches', { method: 'GET' }, 'staff'),

  createBranch: (payload) =>
    request(
      '/staff/branches',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
      'staff'
    ),

  updateBranch: (branchId, payload) =>
    request(
      `/staff/branches/${branchId}`,
      {
        method: 'PATCH',
        body: JSON.stringify(payload),
      },
      'staff'
    ),

  getStaffTables: (branchId = null) => {
    const qs = branchId ? `?branch_id=${branchId}` : '';
    return request(`/staff/branches/tables${qs}`, { method: 'GET' }, 'staff');
  },

  createTable: (payload) =>
    request(
      '/staff/branches/tables',
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
      'staff'
    ),

  updateTable: (tableId, payload) =>
    request(
      `/staff/branches/tables/${tableId}`,
      {
        method: 'PATCH',
        body: JSON.stringify(payload),
      },
      'staff'
    ),

  deleteTable: (tableId) =>
    request(`/staff/branches/tables/${tableId}`, { method: 'DELETE' }, 'staff'),
};
