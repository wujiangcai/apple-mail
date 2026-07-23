import { ApiClient } from "./api.js";
import { paginate } from "./pagination.js";
import {
  renderAccountRows,
  renderMailDetail,
  renderMailList,
  setStatus,
} from "./render.js";
import { createState } from "./state.js";


const IMPORT_FIELDS = ["email", "password", "clientId", "refreshToken"];
const IMPORT_LIMIT = 1000;
const REVEAL_TTL_MS = 60_000;
const MAX_IMPORT_BYTES = 2 * 1024 * 1024;
const MAIL_TIMEOUT_MS = 75_000;
const STALE_RESULT = Symbol("stale-result");
const accountLoads = new WeakMap();


function validateImportRecord(record, lineNumber) {
  if (!record || typeof record !== "object" || Array.isArray(record)) {
    throw new Error(`第 ${lineNumber} 行不是账号对象`);
  }
  const normalized = {};
  for (const field of IMPORT_FIELDS) {
    if (typeof record[field] !== "string") {
      throw new Error(`第 ${lineNumber} 行缺少 ${field}`);
    }
    normalized[field] = record[field];
  }
  if (!normalized.email.trim() || !normalized.clientId.trim() || !normalized.refreshToken.trim()) {
    throw new Error(`第 ${lineNumber} 行包含空的必填字段`);
  }
  return normalized;
}


export function parseImportText(source) {
  const text = String(source || "");
  if (new TextEncoder().encode(text).byteLength > MAX_IMPORT_BYTES) {
    throw new Error("导入内容不能超过 2 MiB");
  }
  const lines = text
    .replace(/^\uFEFF/, "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (lines.length === 0) {
    throw new Error("没有可导入的账号");
  }
  if (lines.length > IMPORT_LIMIT) {
    throw new Error(`单次最多导入 ${IMPORT_LIMIT} 个账号`);
  }

  return lines.map((line, index) => {
    const lineNumber = index + 1;
    if (line.startsWith("{")) {
      let record;
      try {
        record = JSON.parse(line);
      } catch {
        throw new Error(`第 ${lineNumber} 行不是有效 JSON`);
      }
      return validateImportRecord(record, lineNumber);
    }
    const fields = line.split("----");
    if (fields.length !== 4) {
      throw new Error(`第 ${lineNumber} 行必须包含四个字段`);
    }
    return validateImportRecord({
      email: fields[0],
      password: fields[1],
      clientId: fields[2],
      refreshToken: fields[3],
    }, lineNumber);
  });
}


export async function loadAccounts({ api, state }) {
  const previous = accountLoads.get(state);
  previous?.controller.abort();
  const controller = new AbortController();
  const version = (previous?.version || 0) + 1;
  accountLoads.set(state, { controller, version });
  try {
    const accounts = await api.get("/api/accounts", { signal: controller.signal });
    const current = accountLoads.get(state);
    if (current?.version === version && !controller.signal.aborted) {
      state.setAccounts(accounts);
    }
    return state.accounts;
  } catch (error) {
    if (controller.signal.aborted) {
      return state.accounts;
    }
    throw error;
  } finally {
    if (accountLoads.get(state)?.version === version) {
      accountLoads.delete(state);
    }
  }
}


export function cancelAccountLoads(state) {
  accountLoads.get(state)?.controller.abort();
  accountLoads.delete(state);
}


export async function importAccounts({ api, state, accounts }) {
  const result = await api.post("/api/accounts/import", { accounts });
  await loadAccounts({ api, state });
  return result;
}


export async function deleteAccount({ api, state, id }) {
  const result = await api.delete(`/api/accounts/${id}`);
  await loadAccounts({ api, state });
  return result;
}


function validateIdBatch(ids, maximum, label) {
  if (!Array.isArray(ids)
      || ids.length === 0
      || ids.length > maximum
      || ids.some((id) => !Number.isSafeInteger(id) || id <= 0)
      || new Set(ids).size !== ids.length) {
    throw new Error(`${label}需包含 1–${maximum} 个不重复账号`);
  }
}


export async function deleteAccounts({ api, state, ids }) {
  validateIdBatch(ids, 1000, "批量删除");
  const result = await api.post("/api/accounts/delete-batch", { ids });
  await loadAccounts({ api, state });
  return result;
}


export async function updateAccount({ api, state, id, changes }) {
  const result = await api.patch(`/api/accounts/${id}`, changes);
  await loadAccounts({ api, state });
  return result;
}


export async function updateBound({ api, state, id, bound }) {
  return updateAccount({ api, state, id, changes: { bound } });
}


export async function checkBound({ api, state, ids }) {
  validateIdBatch(ids, 20, "绑定检测");
  const result = await api.post("/api/accounts/check-bound", { ids });
  await loadAccounts({ api, state });
  return result;
}


function accountById(state, id) {
  return state.accounts.find((account) => account.id === id);
}


function showDialog(dialog) {
  if (typeof dialog.showModal === "function") {
    dialog.showModal();
  } else {
    dialog.setAttribute("open", "");
  }
}


function closeDialog(dialog) {
  if (typeof dialog.close === "function") {
    dialog.close();
  } else {
    dialog.removeAttribute("open");
  }
}


function errorMessage(error) {
  return error instanceof Error ? error.message : "请求失败";
}


export function bootstrap(doc = document, api = new ApiClient()) {
  const root = doc.querySelector("[data-app-root]");
  if (!root) {
    return null;
  }

  const state = createState();
  const selectedIds = new Set();
  let page = 1;
  let pageSize = 20;
  let activeAccountId = null;
  let activeFolder = "inbox";
  let mailMessages = [];
  let mailPage = 1;
  let mailItemsPerPage = 20;
  let revealTimer = null;
  let mailRequestVersion = 0;
  let mailDetailRequestVersion = 0;
  let revealRequestVersion = 0;
  let verifyRequestVersion = 0;
  let authEpoch = 0;
  let authOperationVersion = 0;
  let logoutBarrier = Promise.resolve();
  let logoutPending = false;
  let loginPending = false;
  let rentals = [];
  let activePickupCode = "";
  let adminSettings = { rentalDefaultMinutes: 60, rentalMinMinutes: 5, rentalMaxMinutes: 10080 };

  const loginView = doc.querySelector("#login-view");
  const dashboard = doc.querySelector("#dashboard");
  const loginForm = doc.querySelector("#login-form");
  const loginPassword = doc.querySelector("#login-password");
  const loginSubmit = loginForm.querySelector('button[type="submit"]');
  const status = doc.querySelector("#app-status");
  const rows = doc.querySelector("#account-rows");
  const search = doc.querySelector("#search-input");
  const pageSizeSelect = doc.querySelector("#page-size");
  const pageLabel = doc.querySelector("#page-label");
  const previousPage = doc.querySelector("#previous-page");
  const nextPage = doc.querySelector("#next-page");
  const selectAll = doc.querySelector("#select-all");
  const totalStat = doc.querySelector("#total-stat");
  const boundStat = doc.querySelector("#bound-stat");
  const invalidStat = doc.querySelector("#invalid-stat");
  const importDialog = doc.querySelector("#import-dialog");
  const importForm = doc.querySelector("#import-form");
  const importText = doc.querySelector("#import-text");
  const importFile = doc.querySelector("#import-file");
  const revealDialog = doc.querySelector("#reveal-dialog");
  const mailDialog = doc.querySelector("#mail-dialog");
  const mailList = doc.querySelector("#mail-list");
  const mailDetail = doc.querySelector("#mail-detail");
  const mailPageLabel = doc.querySelector("#mail-page-label");
  const mailPrevious = doc.querySelector("#mail-previous");
  const mailNext = doc.querySelector("#mail-next");
  const mailKeyword = doc.querySelector("#mail-keyword");
  const rentalDialog = doc.querySelector("#rental-dialog");
  const rentalForm = doc.querySelector("#rental-form");
  const rentalRows = doc.querySelector("#rental-rows");
  const pickupCodeDialog = doc.querySelector("#pickup-code-dialog");
  const pickupCodeValue = doc.querySelector("#pickup-code-value");
  const rentalExpiry = doc.querySelector("#rental-expiry");
  const rentalExpiryLabel = doc.querySelector("#rental-expiry-label");
  const rentalAccountSelect = doc.querySelector("#rental-account");
  const rentalAccountHint = doc.querySelector("#rental-account-hint");
  const rentalLookupForm = doc.querySelector("#rental-lookup-form");
  const rentalLookupCode = doc.querySelector("#rental-lookup-code");
  const rentalLookupResult = doc.querySelector("#rental-lookup-result");
  const maintenanceStatus = doc.querySelector("#maintenance-status");
  const maintenanceSuccess = doc.querySelector("#maintenance-success");
  const maintenanceIssues = doc.querySelector("#maintenance-issues");

  function eraseReveal() {
    if (revealTimer !== null) {
      clearTimeout(revealTimer);
      revealTimer = null;
    }
    const id = Number(revealDialog.dataset.accountId);
    if (Number.isSafeInteger(id)) {
      state.clearReveal(id);
    }
    for (const field of IMPORT_FIELDS) {
      doc.querySelector(`#secret-${field}`).textContent = "";
    }
    delete revealDialog.dataset.accountId;
  }

  function closeAllDialogs() {
    for (const dialog of doc.querySelectorAll("dialog")) {
      closeDialog(dialog);
    }
  }

  function invalidateSession({ invalidateAuthOperation = true } = {}) {
    authEpoch += 1;
    if (invalidateAuthOperation) {
      authOperationVersion += 1;
    }
    api.abortAll();
    cancelAccountLoads(state);
    mailRequestVersion += 1;
    mailDetailRequestVersion += 1;
    revealRequestVersion += 1;
    verifyRequestVersion += 1;
    activeAccountId = null;
    mailMessages = [];
    selectedIds.clear();
    state.clearSecrets();
    eraseReveal();
    state.setAccounts([]);
    importText.value = "";
    importFile.value = "";
    mailKeyword.value = "";
    rentalLookupCode.value = "";
    rentalLookupResult.replaceChildren();
    rentalLookupResult.hidden = true;
    mailList.replaceChildren();
    mailDetail.replaceChildren();
    closeAllDialogs();
    showAuthenticated(false);
    renderAccounts();
    return authEpoch;
  }

  function showAuthenticated(authenticated) {
    loginView.hidden = authenticated;
    dashboard.hidden = !authenticated;
  }

  function renderAccounts() {
    const query = search.value.trim().toLocaleLowerCase();
    const accounts = state.accounts;
    const filtered = query
      ? accounts.filter((account) => account.email.toLocaleLowerCase().includes(query))
      : accounts;
    const pagination = paginate(filtered, page, pageSize);
    page = pagination.page;
    pageSize = pagination.pageSize;
    renderAccountRows(rows, pagination.items, selectedIds);
    pageLabel.textContent = `${pagination.page} / ${pagination.totalPages}`;
    previousPage.disabled = pagination.page <= 1;
    nextPage.disabled = pagination.page >= pagination.totalPages;
    totalStat.textContent = String(accounts.length);
    boundStat.textContent = String(accounts.filter((account) => account.bound).length);
    invalidStat.textContent = String(accounts.filter((account) => !account.tokenValid).length);
    const visibleIds = new Set(accounts.map((account) => account.id));
    for (const id of selectedIds) {
      if (!visibleIds.has(id)) {
        selectedIds.delete(id);
      }
    }
    selectAll.checked = pagination.items.length > 0
      && pagination.items.every((account) => selectedIds.has(account.id));
  }

  function refreshRentalAccountOptions(selectedId = "") {
    rentalAccountSelect.replaceChildren();
    const automatic = doc.createElement("option");
    automatic.value = "";
    automatic.textContent = "自动分配可用邮箱";
    rentalAccountSelect.append(automatic);
    const available = state.accounts.filter(
      (account) => account.rentable && account.tokenValid && !account.rented,
    );
    for (const account of available) {
      const option = doc.createElement("option");
      option.value = String(account.id);
      option.textContent = account.email;
      rentalAccountSelect.append(option);
    }
    rentalAccountSelect.value = available.some(
      (account) => String(account.id) === String(selectedId),
    ) ? String(selectedId) : "";
    rentalAccountHint.textContent = available.length
      ? `可指定 ${available.length} 个邮箱；也可由系统自动分配。`
      : "暂无可指定邮箱，自动分配也可能失败。";
  }

  async function refreshAdminMetadata() {
    const [settings, maintenance] = await Promise.all([
      api.get("/api/admin/settings"),
      api.get("/api/token-maintenance/latest"),
    ]);
    adminSettings = settings;
    const run = maintenance.run;
    maintenanceStatus.textContent = run
      ? `${run.status} · ${new Date(run.startedAt).toLocaleString()}`
      : "尚未运行";
    maintenanceSuccess.textContent = String(run?.success || 0);
    maintenanceIssues.textContent = String(
      (run?.invalid || 0) + (run?.busy || 0) + (run?.rateLimited || 0) + (run?.failed || 0),
    );
  }

  async function refreshAccounts() {
    await loadAccounts({ api, state });
    renderAccounts();
    refreshRentalAccountOptions();
  }

  function renderRentals() {
    rentalRows.replaceChildren();
    for (const rental of rentals) {
      const row = doc.createElement("tr");
      const values = [
        rental.email,
        rental.status,
        rental.expiresAt ? new Date(rental.expiresAt).toLocaleString() : "永不过期",
        rental.accessCount,
      ];
      for (const value of values) {
        const cell = doc.createElement("td");
        cell.textContent = String(value ?? "");
        row.append(cell);
      }
      const actions = doc.createElement("td");
      if (rental.status === "active") {
        if (rental.codeAvailable) {
          const reveal = doc.createElement("button");
          reveal.type = "button";
          reveal.className = "table-action";
          reveal.dataset.rentalAction = "reveal-code";
          reveal.dataset.rentalId = String(rental.id);
          reveal.textContent = "查看租用码";
          actions.append(reveal);
        }
        const reset = doc.createElement("button");
        reset.type = "button";
        reset.className = "table-action";
        reset.dataset.rentalAction = "reset-code";
        reset.dataset.rentalId = String(rental.id);
        reset.textContent = "重置租用码";
        actions.append(reset);
        const button = doc.createElement("button");
        button.type = "button";
        button.className = "table-action";
        button.dataset.rentalAction = "revoke";
        button.dataset.rentalId = String(rental.id);
        button.textContent = "撤销";
        actions.append(button);
      }
      row.append(actions);
      rentalRows.append(row);
    }
    if (rentals.length === 0) {
      const row = doc.createElement("tr");
      const cell = doc.createElement("td");
      cell.colSpan = 5;
      cell.className = "empty-cell";
      cell.textContent = "暂无租约";
      row.append(cell);
      rentalRows.append(row);
    }
  }

  async function refreshRentals() {
    rentals = await api.get("/api/rentals");
    renderRentals();
  }

  function renderMailPage() {
    const pagination = paginate(mailMessages, mailPage, mailItemsPerPage);
    mailPage = pagination.page;
    mailItemsPerPage = pagination.pageSize;
    renderMailList(mailList, pagination.items);
    renderMailDetail(mailDetail, null);
    mailPageLabel.textContent = `${pagination.page} / ${pagination.totalPages}`;
    mailPrevious.disabled = pagination.page <= 1;
    mailNext.disabled = pagination.page >= pagination.totalPages;
  }

  async function refreshMail() {
    const requestVersion = ++mailRequestVersion;
    const requestEpoch = authEpoch;
    const accountId = activeAccountId;
    const folder = activeFolder;
    const messages = await api.post(`/api/accounts/${accountId}/mail`, {
      folder,
      limit: 100,
    }, { timeoutMs: MAIL_TIMEOUT_MS });
    if (requestVersion !== mailRequestVersion || requestEpoch !== authEpoch) {
      return false;
    }
    mailMessages = messages;
    mailPage = 1;
    renderMailPage();
    return true;
  }

  async function loadMailDetail(message) {
    const requestVersion = ++mailDetailRequestVersion;
    const requestEpoch = authEpoch;
    const accountId = activeAccountId;
    const folder = activeFolder;
    const messageId = message.id;
    renderMailDetail(mailDetail, null);
    const detail = await api.post(
      `/api/accounts/${accountId}/mail/message`,
      { messageId },
      { timeoutMs: MAIL_TIMEOUT_MS },
    );
    if (requestVersion !== mailDetailRequestVersion
        || requestEpoch !== authEpoch
        || accountId !== activeAccountId
        || folder !== activeFolder
        || !mailDialog.hasAttribute("open")) {
      return;
    }
    renderMailDetail(mailDetail, {
      ...detail,
      receivedAt: detail.date,
      body: detail.html
        ? { contentType: "html", content: detail.html }
        : { contentType: "text", content: detail.bodyPreview || "" },
    });
  }

  async function run(action, successMessage, expectedEpoch = authEpoch) {
    try {
      setStatus(status, "正在处理…");
      const result = await action();
      if (expectedEpoch !== authEpoch) {
        return STALE_RESULT;
      }
      setStatus(status, successMessage, "success");
      return result;
    } catch (error) {
      if (expectedEpoch !== authEpoch) {
        return STALE_RESULT;
      }
      setStatus(status, errorMessage(error), "error");
      throw error;
    }
  }

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (loginPending) {
      return;
    }
    loginPending = true;
    loginSubmit.disabled = true;
    const loginVersion = ++authOperationVersion;
    const suppliedPassword = loginPassword.value;
    try {
      await logoutBarrier;
      if (loginVersion !== authOperationVersion) {
        return;
      }
      const loginEpoch = authEpoch;
      const result = await api.post(
        "/api/login",
        { password: suppliedPassword },
        { notifyUnauthorized: false },
      );
      if (loginEpoch !== authEpoch || loginVersion !== authOperationVersion) {
        return;
      }
      authEpoch += 1;
      api.setCsrfToken(result.csrfToken);
      loginPassword.value = "";
      showAuthenticated(true);
      await run(async () => {
        await Promise.all([refreshAccounts(), refreshRentals(), refreshAdminMetadata()]);
      }, "数据已载入");
    } catch (error) {
      if (loginVersion === authOperationVersion) {
        setStatus(status, errorMessage(error), "error");
      }
    } finally {
      loginPending = false;
      loginSubmit.disabled = false;
    }
  });

  doc.querySelector("#logout-button").addEventListener("click", async () => {
    invalidateSession();
    logoutPending = true;
    const logoutRequest = api.post("/api/logout", {});
    api.clearCsrfToken();
    const currentBarrier = logoutRequest.catch((error) => {
      if (error?.code !== "ABORTED") {
        setStatus(status, errorMessage(error), "error");
      }
    });
    logoutBarrier = currentBarrier;
    await currentBarrier;
    if (logoutBarrier === currentBarrier) {
      logoutPending = false;
    }
  });

  search.addEventListener("input", () => {
    page = 1;
    renderAccounts();
  });
  pageSizeSelect.addEventListener("change", () => {
    pageSize = Number.parseInt(pageSizeSelect.value, 10);
    page = 1;
    renderAccounts();
  });
  previousPage.addEventListener("click", () => {
    page -= 1;
    renderAccounts();
  });
  nextPage.addEventListener("click", () => {
    page += 1;
    renderAccounts();
  });
  selectAll.addEventListener("change", () => {
    const query = search.value.trim().toLocaleLowerCase();
    const filtered = state.accounts.filter((account) => !query
      || account.email.toLocaleLowerCase().includes(query));
    for (const account of paginate(filtered, page, pageSize).items) {
      if (selectAll.checked) {
        selectedIds.add(account.id);
      } else {
        selectedIds.delete(account.id);
      }
    }
    renderAccounts();
  });

  rows.addEventListener("change", (event) => {
    const selection = event.target.closest(".account-select");
    if (!selection) {
      return;
    }
    const id = Number(selection.dataset.accountId);
    if (selection.checked) {
      selectedIds.add(id);
    } else {
      selectedIds.delete(id);
    }
    renderAccounts();
  });

  rows.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) {
      return;
    }
    const id = Number(button.dataset.accountId);
    const account = accountById(state, id);
    if (!account) {
      return;
    }
    try {
      if (button.dataset.action === "reveal") {
        const requestEpoch = authEpoch;
        const requestVersion = ++revealRequestVersion;
        const credentials = await run(
          () => api.post(`/api/accounts/${id}/reveal`),
          "凭据将在 60 秒后清除",
          requestEpoch,
        );
        if (credentials === STALE_RESULT
            || requestEpoch !== authEpoch
            || requestVersion !== revealRequestVersion) {
          return;
        }
        state.setReveal(id, credentials);
        for (const field of IMPORT_FIELDS) {
          doc.querySelector(`#secret-${field}`).textContent = credentials[field];
        }
        revealDialog.dataset.accountId = String(id);
        showDialog(revealDialog);
        if (revealTimer !== null) {
          clearTimeout(revealTimer);
        }
        revealTimer = setTimeout(() => {
          eraseReveal();
          closeDialog(revealDialog);
        }, REVEAL_TTL_MS);
      } else if (button.dataset.action === "mail") {
        mailRequestVersion += 1;
        mailDetailRequestVersion += 1;
        activeAccountId = id;
        activeFolder = "inbox";
        mailMessages = [];
        mailPage = 1;
        verifyRequestVersion += 1;
        mailKeyword.value = "";
        renderMailPage();
        doc.querySelector("#mail-account").textContent = account.email;
        showDialog(mailDialog);
        await run(refreshMail, "邮件已载入");
      } else if (button.dataset.action === "recheck-authorization") {
        await run(
          () => api.post(
            `/api/accounts/${id}/recheck-authorization`,
            {},
            { timeoutMs: MAIL_TIMEOUT_MS },
          ),
          "邮箱授权已恢复",
        );
        await refreshAccounts();
      } else if (button.dataset.action === "toggle-bound") {
        await run(
          () => updateAccount({ api, state, id, changes: { bound: !account.bound } }),
          "绑定状态已更新",
        );
        renderAccounts();
      } else if (button.dataset.action === "toggle-rentable") {
        await run(
          () => updateAccount({ api, state, id, changes: { rentable: !account.rentable } }),
          "出租状态已更新",
        );
        renderAccounts();
      } else if (button.dataset.action === "delete") {
        await run(() => deleteAccount({ api, state, id }), "账号已删除");
        renderAccounts();
      }
    } catch {
      // The status region already contains the actionable API error.
    }
  });

  rentalLookupForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const accessCode = rentalLookupCode.value;
    rentalLookupCode.value = "";
    rentalLookupResult.replaceChildren();
    rentalLookupResult.hidden = true;
    try {
      const rental = await run(
        () => api.post("/api/rentals/lookup", { accessCode }),
        "已找到对应邮箱",
      );
      const email = doc.createElement("strong");
      email.textContent = rental.email;
      const details = doc.createElement("span");
      const expiry = rental.expiresAt
        ? new Date(rental.expiresAt).toLocaleString()
        : "永不过期";
      details.textContent = `租约 #${rental.id} · ${rental.status} · ${expiry} · 访问 ${rental.accessCount} 次`;
      const copy = doc.createElement("button");
      copy.type = "button";
      copy.className = "table-action";
      copy.textContent = "复制邮箱";
      copy.addEventListener("click", async () => {
        await navigator.clipboard.writeText(rental.email);
        setStatus(status, "邮箱已复制", "success");
      });
      rentalLookupResult.append(email, details, copy);
      rentalLookupResult.hidden = false;
    } catch {
      // The status region already contains the actionable API error.
    }
  });

  function setDefaultRentalExpiry() {
    const defaultDate = new Date(
      Date.now() + adminSettings.rentalDefaultMinutes * 60 * 1000,
    );
    const local = new Date(defaultDate.getTime() - defaultDate.getTimezoneOffset() * 60000);
    rentalExpiry.value = local.toISOString().slice(0, 16);
  }

  function syncExpiryMode() {
    const mode = rentalForm.querySelector('input[name="expiry-mode"]:checked').value;
    const timed = mode === "at";
    rentalExpiry.disabled = !timed;
    rentalExpiry.required = timed;
    rentalExpiryLabel.hidden = !timed;
    rentalExpiry.hidden = !timed;
  }

  doc.querySelector("#open-import").addEventListener("click", () => showDialog(importDialog));
  doc.querySelector("#open-rental").addEventListener("click", () => {
    refreshRentalAccountOptions();
    setDefaultRentalExpiry();
    syncExpiryMode();
    showDialog(rentalDialog);
  });
  rentalForm.querySelectorAll('input[name="expiry-mode"]').forEach((input) => {
    input.addEventListener("change", syncExpiryMode);
  });
  rentalForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const accountValue = rentalAccountSelect.value;
    const expiryMode = rentalForm.querySelector('input[name="expiry-mode"]:checked').value;
    const body = { expiryMode };
    if (expiryMode === "at") {
      const expiryDate = new Date(rentalExpiry.value);
      if (!rentalExpiry.value || Number.isNaN(expiryDate.getTime())) {
        setStatus(status, "请选择有效的到期时间", "error");
        return;
      }
      body.expiresAt = expiryDate.toISOString();
    }
    if (accountValue) {
      body.accountId = Number(accountValue);
    }
    try {
      const result = await run(() => api.post("/api/rentals", body), "租约已创建");
      activePickupCode = result.accessCode;
      pickupCodeValue.textContent = activePickupCode;
      closeDialog(rentalDialog);
      showDialog(pickupCodeDialog);
      await Promise.all([refreshRentals(), refreshAccounts()]);
    } catch {
      // The status region already contains the actionable API error.
    }
  });
  rentalRows.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-rental-id]");
    if (!button) return;
    const rentalId = button.dataset.rentalId;
    const action = button.dataset.rentalAction;
    try {
      if (action === "reveal-code") {
        const result = await run(
          () => api.post(`/api/rentals/${rentalId}/reveal-code`, {}),
          "租用码将在 60 秒后清除",
        );
        activePickupCode = result.accessCode;
        pickupCodeValue.textContent = activePickupCode;
        showDialog(pickupCodeDialog);
        return;
      }
      if (action === "reset-code") {
        if (!globalThis.confirm("重置后旧租用码和已登录客户会话将立即失效。确定继续吗？")) {
          return;
        }
        const result = await run(
          () => api.post(`/api/rentals/${rentalId}/reset-code`, {}),
          "租用码已重置",
        );
        activePickupCode = result.accessCode;
        pickupCodeValue.textContent = activePickupCode;
        showDialog(pickupCodeDialog);
        await refreshRentals();
        return;
      }
      await run(() => api.post(`/api/rentals/${rentalId}/revoke`, {}), "租约已撤销");
      await Promise.all([refreshRentals(), refreshAccounts()]);
    } catch {
      // The status region already contains the actionable API error.
    }
  });
  doc.querySelector("#copy-pickup-code").addEventListener("click", async () => {
    if (!activePickupCode) return;
    await navigator.clipboard.writeText(activePickupCode);
    setStatus(status, "取件码已复制", "success");
  });
  doc.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = doc.querySelector(button.dataset.closeDialog);
      if (dialog === revealDialog) {
        eraseReveal();
      }
      if (dialog === mailDialog) {
        mailRequestVersion += 1;
        mailDetailRequestVersion += 1;
        verifyRequestVersion += 1;
        mailMessages = [];
        mailList.replaceChildren();
        mailDetail.replaceChildren();
      }
      if (dialog === pickupCodeDialog) {
        activePickupCode = "";
        pickupCodeValue.textContent = "";
      }
      closeDialog(dialog);
    });
  });
  importForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const file = importFile.files[0];
      if (file && file.size > MAX_IMPORT_BYTES) {
        throw new Error("导入文件不能超过 2 MiB");
      }
      const source = file ? await file.text() : importText.value;
      const accounts = parseImportText(source);
      await run(() => importAccounts({ api, state, accounts }), `已导入 ${accounts.length} 个账号`);
      importText.value = "";
      importFile.value = "";
      page = 1;
      renderAccounts();
      closeDialog(importDialog);
    } catch (error) {
      setStatus(status, errorMessage(error), "error");
    }
  });

  doc.querySelector("#export-button").addEventListener("click", async () => {
    try {
      const blob = await run(
        () => api.post("/api/accounts/export", undefined, { responseType: "blob" }),
        "导出文件已生成",
      );
      const objectUrl = URL.createObjectURL(blob);
      const link = doc.createElement("a");
      link.href = objectUrl;
      link.download = "accounts.jsonl";
      link.click();
      URL.revokeObjectURL(objectUrl);
    } catch {
      // The status region already contains the actionable API error.
    }
  });

  doc.querySelector("#delete-selected").addEventListener("click", async () => {
    const ids = [...selectedIds];
    if (ids.length === 0) {
      setStatus(status, "请先选择账号", "error");
      return;
    }
    const protectedAccounts = state.accounts.filter(
      (account) => selectedIds.has(account.id) && account.hasRentalHistory,
    );
    if (protectedAccounts.length > 0) {
      setStatus(
        status,
        `以下邮箱存在租约历史，不能删除：${protectedAccounts.map((item) => item.email).join("、")}`,
        "error",
      );
      return;
    }
    try {
      await run(() => deleteAccounts({ api, state, ids }), `已删除 ${ids.length} 个账号`);
      selectedIds.clear();
      renderAccounts();
    } catch {
      // The status region already contains the actionable API error.
    }
  });

  doc.querySelector("#check-bound").addEventListener("click", async () => {
    const ids = [...selectedIds];
    if (ids.length === 0 || ids.length > 20) {
      setStatus(status, "绑定检测需选择 1–20 个账号", "error");
      return;
    }
    try {
      await run(() => checkBound({ api, state, ids }), "绑定检测完成");
      renderAccounts();
    } catch {
      // The status region already contains the actionable API error.
    }
  });

  doc.querySelector("#random-unbound").addEventListener("click", async () => {
    try {
      const result = await run(
        () => api.post("/api/accounts/random-unbound", {}),
        "已随机选择未绑定账号",
      );
      if (result === STALE_RESULT) {
        return;
      }
      setStatus(status, `随机账号：${result.email}`, "success");
    } catch {
      // The status region already contains the actionable API error.
    }
  });

  doc.querySelectorAll("[data-copy-secret]").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = Number(revealDialog.dataset.accountId);
      const credentials = state.getReveal(id);
      const field = button.dataset.copySecret;
      if (!credentials || !Object.hasOwn(credentials, field)) {
        setStatus(status, "凭据已从内存清除，请重新查看", "error");
        return;
      }
      await navigator.clipboard.writeText(credentials[field]);
      setStatus(status, "已复制到剪贴板", "success");
    });
  });

  doc.querySelectorAll("[data-folder]").forEach((button) => {
    button.addEventListener("click", async () => {
      mailDetailRequestVersion += 1;
      renderMailDetail(mailDetail, null);
      activeFolder = button.dataset.folder;
      try {
        await run(refreshMail, "邮件已载入");
      } catch {
        // The status region already contains the actionable API error.
      }
    });
  });
  mailList.addEventListener("click", async (event) => {
    const item = event.target.closest("[data-mail-index]");
    if (!item) {
      return;
    }
    const offset = (mailPage - 1) * mailItemsPerPage;
    const message = mailMessages[offset + Number(item.dataset.mailIndex)];
    try {
      await loadMailDetail(message);
    } catch (error) {
      renderMailDetail(mailDetail, null);
      setStatus(status, errorMessage(error), "error");
    }
  });
  mailPrevious.addEventListener("click", () => {
    mailPage -= 1;
    renderMailPage();
  });
  mailNext.addEventListener("click", () => {
    mailPage += 1;
    renderMailPage();
  });
  doc.querySelector("#verify-code").addEventListener("click", async () => {
    const requestEpoch = authEpoch;
    const requestVersion = ++verifyRequestVersion;
    const keyword = mailKeyword.value.trim();
    const body = keyword ? { keyword, maxResults: 100 } : { maxResults: 100 };
    try {
      const result = await run(
        () => api.post(
          `/api/accounts/${activeAccountId}/verify-code`,
          body,
          { timeoutMs: MAIL_TIMEOUT_MS },
        ),
        "验证码已找到",
        requestEpoch,
      );
      if (result === STALE_RESULT
          || requestEpoch !== authEpoch
          || requestVersion !== verifyRequestVersion) {
        return;
      }
      setStatus(status, `验证码：${result.code}`, "success");
    } catch {
      // The status region already contains the actionable API error.
    }
  });

  api.addEventListener("unauthorized", () => {
    invalidateSession({ invalidateAuthOperation: !logoutPending });
    api.clearCsrfToken();
    setStatus(status, "会话已失效，请重新登录", "error");
  });

  (async () => {
    const checkEpoch = authEpoch;
    const checkVersion = authOperationVersion;
    try {
      const auth = await api.get("/api/check-auth");
      if (checkEpoch !== authEpoch || checkVersion !== authOperationVersion) {
        return;
      }
      if (!auth.authenticated) {
        showAuthenticated(false);
        return;
      }
      api.setCsrfToken(auth.csrfToken);
      showAuthenticated(true);
      await Promise.all([refreshAccounts(), refreshRentals()]);
    } catch (error) {
      if (checkEpoch !== authEpoch || checkVersion !== authOperationVersion) {
        return;
      }
      showAuthenticated(false);
      setStatus(status, errorMessage(error), "error");
    }
  })();

  return { api, state, renderAccounts, refreshAccounts };
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => bootstrap());
  } else {
    bootstrap();
  }
}
