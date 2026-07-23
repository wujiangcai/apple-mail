import { ApiClient } from "./api.js";
import { renderMailDetail, renderMailList, setStatus } from "./render.js";


const MAIL_TIMEOUT_MS = 75_000;
const PASSWORD_REVEAL_TTL_MS = 60_000;


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
  const accountEmail = doc.querySelector("#pickup-account-email");
  const tokenStatus = doc.querySelector("#pickup-token-status");
  const tokenHelp = doc.querySelector("#pickup-token-help");
  const passwordValue = doc.querySelector("#pickup-password");
  const revealPassword = doc.querySelector("#pickup-reveal-password");
  const copyPassword = doc.querySelector("#pickup-copy-password");
  const mailControls = [...doc.querySelectorAll(
    "[data-pickup-folder], #pickup-refresh, #pickup-verify, #pickup-keyword",
  )];
  let messages = [];
  let folder = "inbox";
  let expiresAt = null;
  let rentalExpiresAt = null;
  let permanent = false;
  let countdown = null;
  let authEpoch = 0;
  let mailRequestVersion = 0;
  let detailRequestVersion = 0;
  let tokenValid = true;
  let revealedPassword = "";
  let passwordTimer = null;
  let passwordRequestVersion = 0;
  let authOperationVersion = 0;

  function friendlyError(error) {
    const message = String(error?.message || "");
    const translations = new Map([
      ["invalid or expired pickup code", "取件码无效、已过期或已被撤销"],
      ["pickup authorization required", "取件授权已失效，请重新输入取件码"],
      ["account token invalid", "邮箱授权已失效，请联系管理员重新授权"],
      ["mailbox authorization unavailable", "邮箱授权不可用，请联系管理员处理"],
      ["mail access authorization rejected", "Microsoft 已拒绝邮箱授权，请联系管理员重新授权"],
      ["mail access permission denied", "邮箱缺少 Mail.Read 权限，请联系管理员处理"],
      ["upstream rate limited", "Microsoft 服务请求过多，请稍后再试"],
      ["upstream unavailable", "Microsoft 邮箱服务暂时不可用，请稍后再试"],
      ["not found", "没有找到验证码或邮件"],
      ["request timed out", "请求超时，请稍后重试"],
      ["network request failed", "网络连接失败，请检查网络后重试"],
    ]);
    return translations.get(message) || "操作失败，请稍后重试或联系管理员";
  }

  function erasePassword() {
    passwordRequestVersion += 1;
    revealedPassword = "";
    passwordValue.textContent = "••••••••";
    revealPassword.textContent = "查看密码";
    copyPassword.disabled = true;
    if (passwordTimer !== null) clearTimeout(passwordTimer);
    passwordTimer = null;
  }

  function renderAuthorization(value) {
    tokenValid = value === true;
    tokenStatus.textContent = tokenValid ? "授权正常" : "授权需处理";
    tokenStatus.className = tokenValid
      ? "status-badge is-positive"
      : "status-badge is-muted";
    tokenHelp.textContent = tokenValid
      ? "站内邮件读取可用"
      : "可查看邮箱密码并登录 Microsoft 处理风控；站内邮件读取暂不可用";
    for (const control of mailControls) control.disabled = !tokenValid;
    doc.querySelector(".mail-layout").classList.toggle("pickup-mail-disabled", !tokenValid);
    if (!tokenValid) {
      messages = [];
      renderMailList(list, messages);
      renderMailDetail(detail, null);
    }
  }

  function markAuthorizationFromError(error) {
    if ([
      "account token invalid",
      "mail access authorization rejected",
      "mail access permission denied",
    ].includes(String(error?.message || ""))) {
      renderAuthorization(false);
    }
  }

  function clearMailbox() {
    authEpoch += 1;
    authOperationVersion += 1;
    mailRequestVersion += 1;
    detailRequestVersion += 1;
    api.abortAll?.();
    erasePassword();
    tokenValid = true;
    messages = [];
    list.replaceChildren();
    detail.replaceChildren();
    keyword.value = "";
    email.textContent = "邮箱取件";
    accountEmail.textContent = "";
    tokenStatus.textContent = "";
    tokenHelp.textContent = "";
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
      const sessionSeconds = Math.ceil(remaining / 1000);
      const sessionText = sessionSeconds < 300
        ? `${sessionSeconds} 秒`
        : `${Math.ceil(sessionSeconds / 60)} 分钟`;
      const rentalText = permanent
        ? "租约永不过期"
        : `租约到期：${rentalExpiresAt.toLocaleString()}`;
      expiry.textContent = `${rentalText}；当前会话剩余 ${sessionText}`;
      expiry.dataset.urgent = sessionSeconds < 300 ? "true" : "false";
    };
    update();
    countdown = setInterval(update, 1000);
  }

  function acceptAuth(auth) {
    api.setCsrfToken(auth.csrfToken);
    email.textContent = auth.email;
    accountEmail.textContent = auth.email;
    renderAuthorization(auth.tokenValid === true);
    expiresAt = new Date(auth.expiresAt);
    permanent = auth.permanent === true || auth.rentalExpiresAt == null;
    rentalExpiresAt = auth.rentalExpiresAt ? new Date(auth.rentalExpiresAt) : null;
    showAuthenticated(true);
    startCountdown();
  }

  async function loadMail() {
    const requestVersion = ++mailRequestVersion;
    const requestEpoch = authEpoch;
    const requestedFolder = folder;
    detailRequestVersion += 1;
    messages = [];
    renderMailList(list, messages);
    renderMailDetail(detail, null);
    const loaded = await api.post(
      "/api/pickup/mail",
      { folder: requestedFolder, limit: 100 },
      { timeoutMs: MAIL_TIMEOUT_MS },
    );
    if (requestVersion !== mailRequestVersion
        || requestEpoch !== authEpoch
        || requestedFolder !== folder) {
      return false;
    }
    messages = loaded;
    renderMailList(list, messages);
    renderMailDetail(detail, null);
    return true;
  }

  async function loadMessage(message) {
    const requestVersion = ++detailRequestVersion;
    const requestEpoch = authEpoch;
    const requestedFolder = folder;
    renderMailDetail(detail, null);
    const loaded = await api.post(
      "/api/pickup/mail/message",
      { folder: requestedFolder, messageId: message.id },
      { timeoutMs: MAIL_TIMEOUT_MS },
    );
    if (requestVersion !== detailRequestVersion
        || requestEpoch !== authEpoch
        || requestedFolder !== folder
        || dashboard.hidden) {
      return;
    }
    renderMailDetail(detail, {
      ...loaded,
      receivedAt: loaded.date,
      body: loaded.html
        ? { contentType: "html", content: loaded.html }
        : { contentType: "text", content: loaded.bodyPreview || "" },
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const operationVersion = ++authOperationVersion;
    try {
      const auth = await api.post(
        "/api/pickup/login",
        { code: codeInput.value },
        { notifyUnauthorized: false },
      );
      if (operationVersion !== authOperationVersion) return;
      codeInput.value = "";
      acceptAuth(auth);
      if (tokenValid) {
        await loadMail();
        setStatus(status, "邮箱已载入", "success");
      } else {
        setStatus(status, "邮箱授权需处理，可查看密码登录 Microsoft", "error");
      }
    } catch (error) {
      setStatus(status, friendlyError(error), "error");
    }
  });

  doc.querySelectorAll("[data-pickup-folder]").forEach((button) => {
    button.addEventListener("click", async () => {
      folder = button.dataset.pickupFolder;
      try {
        await loadMail();
        setStatus(status, "邮件已载入", "success");
      } catch (error) {
        markAuthorizationFromError(error);
        setStatus(status, friendlyError(error), "error");
      }
    });
  });

  list.addEventListener("click", async (event) => {
    const item = event.target.closest("[data-mail-index]");
    if (!item) return;
    const message = messages[Number(item.dataset.mailIndex)];
    try {
      await loadMessage(message);
    } catch (error) {
      renderMailDetail(detail, null);
      setStatus(status, friendlyError(error), "error");
    }
  });

  doc.querySelector("#pickup-refresh").addEventListener("click", async () => {
    try {
      await loadMail();
      setStatus(status, `邮件已刷新：${new Date().toLocaleTimeString()}`, "success");
    } catch (error) {
      setStatus(status, friendlyError(error), "error");
    }
  });

  doc.querySelector("#pickup-verify").addEventListener("click", async () => {
    const value = keyword.value.trim();
    const body = value
      ? { keyword: value, maxResults: 100, folder }
      : { maxResults: 100, folder };
    try {
      const result = await api.post(
        "/api/pickup/verify-code",
        body,
        { timeoutMs: MAIL_TIMEOUT_MS },
      );
      setStatus(status, `验证码：${result.code}`, "success");
      await navigator.clipboard?.writeText(result.code);
    } catch (error) {
      setStatus(status, friendlyError(error), "error");
    }
  });

  doc.querySelector("#pickup-copy-email").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(accountEmail.textContent);
      setStatus(status, "邮箱已复制", "success");
    } catch {
      setStatus(status, "复制失败，请手动选择邮箱", "error");
    }
  });

  revealPassword.addEventListener("click", async () => {
    if (revealedPassword) {
      erasePassword();
      return;
    }
    const requestEpoch = authEpoch;
    const requestVersion = ++passwordRequestVersion;
    try {
      const result = await api.post("/api/pickup/reveal-password", {});
      if (requestEpoch !== authEpoch
          || requestVersion !== passwordRequestVersion
          || dashboard.hidden) return;
      revealedPassword = result.password;
      passwordValue.textContent = revealedPassword;
      revealPassword.textContent = "隐藏密码";
      copyPassword.disabled = false;
      passwordTimer = setTimeout(erasePassword, PASSWORD_REVEAL_TTL_MS);
      setStatus(status, "密码将在 60 秒后从页面清除", "success");
    } catch (error) {
      setStatus(status, friendlyError(error), "error");
    }
  });

  copyPassword.addEventListener("click", async () => {
    if (!revealedPassword) return;
    try {
      await navigator.clipboard.writeText(revealedPassword);
      setStatus(status, "密码已复制；剪贴板历史可能继续保留", "success");
    } catch {
      setStatus(status, "复制失败，请手动选择密码", "error");
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
    const operationVersion = authOperationVersion;
    try {
      const auth = await api.get("/api/pickup/check-auth");
      if (operationVersion !== authOperationVersion) return;
      if (!auth.authenticated) {
        showAuthenticated(false);
        return;
      }
      acceptAuth(auth);
      if (tokenValid) await loadMail();
    } catch (error) {
      showAuthenticated(false);
      setStatus(status, friendlyError(error), "error");
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
