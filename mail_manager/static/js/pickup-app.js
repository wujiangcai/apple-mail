import { ApiClient } from "./api.js";
import { renderMailDetail, renderMailList, setStatus } from "./render.js";


export function bootstrapPickup(doc = document, api = new ApiClient()) {
  const root = doc.querySelector("[data-pickup-root]");
  if (!root) return null;

  const login = doc.querySelector("#pickup-login");
  const dashboard = doc.querySelector("#pickup-dashboard");
  const form = doc.querySelector("#pickup-login-form");
  const codeInput = doc.querySelector("#pickup-code");
  const email = doc.querySelector("#pickup-email");
  const expiry = doc.querySelector("#pickup-expiry");
  const status = doc.querySelector("#pickup-status");
  const list = doc.querySelector("#pickup-mail-list");
  const detail = doc.querySelector("#pickup-mail-detail");
  const keyword = doc.querySelector("#pickup-keyword");
  let messages = [];
  let folder = "inbox";
  let expiresAt = null;
  let rentalExpiresAt = null;
  let permanent = false;
  let countdown = null;

  function clearMailbox() {
    messages = [];
    list.replaceChildren();
    detail.replaceChildren();
    keyword.value = "";
    email.textContent = "邮箱取件";
    expiry.textContent = "";
    expiresAt = null;
    rentalExpiresAt = null;
    permanent = false;
    if (countdown !== null) clearInterval(countdown);
    countdown = null;
  }

  function showAuthenticated(value) {
    login.hidden = value;
    dashboard.hidden = !value;
    if (!value) clearMailbox();
  }

  function startCountdown() {
    if (countdown !== null) clearInterval(countdown);
    const update = () => {
      const remaining = expiresAt ? expiresAt.getTime() - Date.now() : 0;
      if (remaining <= 0) {
        api.clearCsrfToken();
        showAuthenticated(false);
        setStatus(status, "租约已到期", "error");
        return;
      }
      const sessionMinutes = Math.ceil(remaining / 60000);
      const rentalText = permanent
        ? "租约永不过期"
        : `租约到期：${rentalExpiresAt.toLocaleString()}`;
      expiry.textContent = `${rentalText}；当前会话剩余 ${sessionMinutes} 分钟`;
    };
    update();
    countdown = setInterval(update, 30000);
  }

  function acceptAuth(auth) {
    api.setCsrfToken(auth.csrfToken);
    email.textContent = auth.email;
    expiresAt = new Date(auth.expiresAt);
    permanent = auth.permanent === true || auth.rentalExpiresAt == null;
    rentalExpiresAt = auth.rentalExpiresAt ? new Date(auth.rentalExpiresAt) : null;
    showAuthenticated(true);
    startCountdown();
  }

  async function loadMail() {
    messages = await api.post("/api/pickup/mail", { folder, limit: 100 });
    renderMailList(list, messages);
    renderMailDetail(detail, null);
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const auth = await api.post(
        "/api/pickup/login",
        { code: codeInput.value },
        { notifyUnauthorized: false },
      );
      codeInput.value = "";
      acceptAuth(auth);
      await loadMail();
      setStatus(status, "邮箱已载入", "success");
    } catch (error) {
      setStatus(status, error.message, "error");
    }
  });

  doc.querySelectorAll("[data-pickup-folder]").forEach((button) => {
    button.addEventListener("click", async () => {
      folder = button.dataset.pickupFolder;
      try {
        await loadMail();
        setStatus(status, "邮件已载入", "success");
      } catch (error) {
        setStatus(status, error.message, "error");
      }
    });
  });

  list.addEventListener("click", (event) => {
    const item = event.target.closest("[data-mail-index]");
    if (!item) return;
    const message = messages[Number(item.dataset.mailIndex)];
    renderMailDetail(detail, {
      ...message,
      receivedAt: message.date,
      body: message.html
        ? { contentType: "html", content: message.html }
        : { contentType: "text", content: message.bodyPreview || "" },
    });
  });

  doc.querySelector("#pickup-verify").addEventListener("click", async () => {
    const value = keyword.value.trim();
    const body = value ? { keyword: value, maxResults: 100 } : { maxResults: 100 };
    try {
      const result = await api.post("/api/pickup/verify-code", body);
      setStatus(status, `验证码：${result.code}`, "success");
      await navigator.clipboard?.writeText(result.code);
    } catch (error) {
      setStatus(status, error.message, "error");
    }
  });

  doc.querySelector("#pickup-logout").addEventListener("click", async () => {
    try {
      await api.post("/api/pickup/logout", {});
    } finally {
      api.clearCsrfToken();
      showAuthenticated(false);
    }
  });

  api.addEventListener("unauthorized", () => {
    api.clearCsrfToken();
    showAuthenticated(false);
    setStatus(status, "取件授权已失效", "error");
  });

  (async () => {
    try {
      const auth = await api.get("/api/pickup/check-auth");
      if (!auth.authenticated) {
        showAuthenticated(false);
        return;
      }
      acceptAuth(auth);
      await loadMail();
    } catch (error) {
      showAuthenticated(false);
      setStatus(status, error.message, "error");
    }
  })();

  return { api, loadMail };
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => bootstrapPickup());
  } else {
    bootstrapPickup();
  }
}
