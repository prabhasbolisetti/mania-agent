let currentUserRequest = null;
let leadsRequest = null;

const apiRequest = async (url, options = {}) => {
  const response = await fetch(url, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });

  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }

  if (!response.ok) {
    const error = new Error(data?.detail || data?.message || "Request failed");
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
};

export const login = (username, password) =>
  apiRequest("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });

export const getCurrentUser = () => {
  if (!currentUserRequest) {
    currentUserRequest = apiRequest("/api/auth/me").finally(() => {
      currentUserRequest = null;
    });
  }
  return currentUserRequest;
};

export const logout = () => apiRequest("/api/auth/logout", { method: "POST" });

export const getLeads = () => {
  if (!leadsRequest) {
    leadsRequest = apiRequest("/api/leads").finally(() => {
      leadsRequest = null;
    });
  }
  return leadsRequest;
};
export const getLead = (leadId) =>
  apiRequest(`/api/leads/${encodeURIComponent(leadId)}`);

export const updateLeadCallStatus = (leadId, callCompleted) =>
  apiRequest(`/api/leads/${encodeURIComponent(leadId)}/call-status`, {
    method: "PATCH",
    body: JSON.stringify({ call_completed: Boolean(callCompleted) }),
  });
