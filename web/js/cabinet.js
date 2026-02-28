// Cabinet common JS - auth, API helpers, utils

const API_BASE = "";

function getToken() {
  return localStorage.getItem("arkentiy_token");
}

function setToken(token) {
  localStorage.setItem("arkentiy_token", token);
}

function clearToken() {
  localStorage.removeItem("arkentiy_token");
}

function checkAuth() {
  const token = getToken();
  if (!token) {
    window.location.href = "/login.html?redirect=" + encodeURIComponent(window.location.pathname);
    return false;
  }
  return true;
}

function logout() {
  clearToken();
  window.location.href = "/login.html";
}

async function apiGet(url) {
  const token = getToken();
  const resp = await fetch(API_BASE + url, {
    headers: { "Authorization": "Bearer " + token }
  });
  if (resp.status === 401) {
    logout();
    throw new Error("Unauthorized");
  }
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function apiPost(url, body) {
  const token = getToken();
  const resp = await fetch(API_BASE + url, {
    method: "POST",
    headers: {
      "Authorization": "Bearer " + token,
      "Content-Type": "application/json"
    },
    body: JSON.stringify(body)
  });
  if (resp.status === 401) {
    logout();
    throw new Error("Unauthorized");
  }
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function apiPut(url, body) {
  const token = getToken();
  const resp = await fetch(API_BASE + url, {
    method: "PUT",
    headers: {
      "Authorization": "Bearer " + token,
      "Content-Type": "application/json"
    },
    body: JSON.stringify(body)
  });
  if (resp.status === 401) {
    logout();
    throw new Error("Unauthorized");
  }
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function apiDelete(url) {
  const token = getToken();
  const resp = await fetch(API_BASE + url, {
    method: "DELETE",
    headers: { "Authorization": "Bearer " + token }
  });
  if (resp.status === 401) {
    logout();
    throw new Error("Unauthorized");
  }
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

function formatDate(dateStr) {
  if (!dateStr) return "—";
  const d = new Date(dateStr);
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" });
}

function formatDateTime(dateStr) {
  if (!dateStr) return "—";
  const d = new Date(dateStr);
  return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }) + " " + 
         d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function formatMoney(amount) {
  if (amount == null) return "—";
  return new Intl.NumberFormat("ru-RU").format(amount) + " ₽";
}

function showToast(message, type = "info") {
  const colors = {
    info: "bg-gray-800",
    success: "bg-green-600",
    error: "bg-red-600"
  };
  const toast = document.createElement("div");
  toast.className = `fixed bottom-4 right-4 ${colors[type]} text-white px-4 py-2 rounded-lg shadow-lg z-50`;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3000);
}

// Highlight active sidebar link
document.addEventListener("DOMContentLoaded", () => {
  const path = window.location.pathname;
  document.querySelectorAll(".sidebar-link").forEach(link => {
    link.classList.remove("active");
    const href = link.getAttribute("href");
    if (path === href || (path === "/cabinet/" && href === "/cabinet/") || 
        (path.startsWith("/cabinet/") && href !== "/cabinet/" && path.startsWith(href.replace(".html", "")))) {
      link.classList.add("active");
    }
  });
});
