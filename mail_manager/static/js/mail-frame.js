const MAIL_CSP = [
  "default-src 'none'",
  "script-src 'none'",
  "img-src data:",
  "style-src 'unsafe-inline'",
  "form-action 'none'",
  "base-uri 'none'",
].join("; ");


export function createMailFrame(mailHtml) {
  const frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "");
  frame.referrerPolicy = "no-referrer";
  frame.title = "邮件正文";
  frame.className = "mail-frame";
  frame.srcdoc = [
    "<!doctype html><html><head><meta charset=\"utf-8\">",
    `<meta http-equiv="Content-Security-Policy" content="${MAIL_CSP}">`,
    "<meta name=\"referrer\" content=\"no-referrer\">",
    "<style>html{color-scheme:light}body{margin:0;padding:18px;color:#26352f;font:15px/1.65 ui-sans-serif,sans-serif;overflow-wrap:anywhere}img{max-width:100%;height:auto}</style>",
    "</head><body>",
    String(mailHtml || ""),
    "</body></html>",
  ].join("");
  return frame;
}


export { MAIL_CSP };