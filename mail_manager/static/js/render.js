import { createMailFrame } from "./mail-frame.js";


function element(tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = String(text);
  }
  return node;
}


function actionButton(action, label, accountId) {
  const button = element("button", "table-action", label);
  button.type = "button";
  button.dataset.action = action;
  if (accountId !== undefined) {
    button.dataset.accountId = String(accountId);
  }
  return button;
}


function statusBadge(enabled, positiveText, negativeText) {
  return element(
    "span",
    enabled ? "status-badge is-positive" : "status-badge is-muted",
    enabled ? positiveText : negativeText,
  );
}


export function renderAccountRow(parent, account, { selected = false } = {}) {
  const row = document.createElement("tr");
  row.dataset.accountId = String(account.id);

  const selectionCell = document.createElement("td");
  const selection = document.createElement("input");
  selection.type = "checkbox";
  selection.className = "account-select";
  selection.checked = selected;
  selection.dataset.accountId = String(account.id);
  selection.setAttribute("aria-label", `选择 ${String(account.email || "")}`);
  selectionCell.append(selection);

  const emailCell = element("td", "account-email", account.email || "");
  const boundCell = document.createElement("td");
  boundCell.append(statusBadge(Boolean(account.bound), "已绑定", "未绑定"));
  const tokenCell = document.createElement("td");
  tokenCell.append(statusBadge(account.tokenValid !== false, "可用", "已失效"));
  const rentalCell = document.createElement("td");
  rentalCell.append(statusBadge(Boolean(account.rented), "租用中", account.rentable ? "可出租" : "已停用"));
  const refreshCell = element("td", "account-refresh", account.lastRefreshTime || "尚未刷新");
  const actionsCell = element("td", "account-actions");
  actionsCell.append(
    actionButton("reveal", "查看", account.id),
    actionButton("mail", "邮件", account.id),
    actionButton("toggle-bound", account.bound ? "取消绑定" : "标记绑定", account.id),
    actionButton("toggle-rentable", account.rentable ? "停止出租" : "恢复出租", account.id),
    actionButton("delete", "删除", account.id),
  );

  row.append(selectionCell, emailCell, boundCell, tokenCell, rentalCell, refreshCell, actionsCell);
  parent.append(row);
  return row;
}


export function renderAccountRows(parent, accounts, selectedIds = new Set()) {
  parent.replaceChildren();
  for (const account of accounts) {
    renderAccountRow(parent, account, { selected: selectedIds.has(account.id) });
  }
  if (accounts.length === 0) {
    const row = document.createElement("tr");
    const cell = element("td", "empty-cell", "没有匹配的账号");
    cell.colSpan = 7;
    row.append(cell);
    parent.append(row);
  }
}


export function renderMailList(parent, messages) {
  parent.replaceChildren();
  messages.forEach((message, index) => {
    const button = element("button", "mail-list-item");
    button.type = "button";
    button.dataset.mailIndex = String(index);
    button.append(
      element("strong", "mail-subject", message.subject || "（无主题）"),
      element("span", "mail-sender", message.sender || message.from || "未知发件人"),
      element(
        "time",
        "mail-time",
        message.receivedAt || message.receivedDateTime || message.date || "",
      ),
    );
    parent.append(button);
  });
  if (messages.length === 0) {
    parent.append(element("p", "empty-state", "该文件夹暂无邮件"));
  }
}


export function renderMailDetail(parent, message) {
  parent.replaceChildren();
  if (!message) {
    parent.append(element("p", "empty-state", "选择一封邮件查看正文"));
    return;
  }

  const header = element("header", "mail-detail-header");
  header.append(
    element("h3", "mail-detail-subject", message.subject || "（无主题）"),
    element("p", "mail-detail-sender", message.sender || message.from || "未知发件人"),
  );
  parent.append(header);

  const body = message.body || {};
  const contentType = String(body.contentType || body.content_type || "text").toLowerCase();
  const content = body.content ?? message.bodyPreview ?? "";
  if (contentType === "html") {
    parent.append(createMailFrame(content));
    return;
  }
  parent.append(element("pre", "mail-plain-text", content));
}


export function setStatus(target, message, tone = "neutral") {
  target.textContent = String(message || "");
  target.dataset.tone = tone;
}
