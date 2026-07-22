const REVEAL_TTL_MS = 60_000;


function normalizeAccount(account) {
  return {
    id: account.id,
    email: String(account.email || ""),
    lastRefreshTime: account.lastRefreshTime || null,
    bound: Boolean(account.bound),
    boundAt: account.boundAt || null,
    tokenValid: account.tokenValid !== false,
    rentable: account.rentable !== false,
    rented: Boolean(account.rented),
  };
}


function copyReveal(credentials) {
  return {
    email: String(credentials.email || ""),
    password: String(credentials.password || ""),
    clientId: String(credentials.clientId || ""),
    refreshToken: String(credentials.refreshToken || ""),
  };
}


export function createState(initialAccounts = [], { revealTtlMs = REVEAL_TTL_MS } = {}) {
  let accounts = initialAccounts.map(normalizeAccount);
  const reveals = new Map();
  const revealTimers = new Map();

  function clearReveal(accountId) {
    const timer = revealTimers.get(accountId);
    if (timer !== undefined) {
      clearTimeout(timer);
      revealTimers.delete(accountId);
    }
    reveals.delete(accountId);
  }

  return {
    get accounts() {
      return accounts.map((account) => ({ ...account }));
    },

    setAccounts(nextAccounts) {
      accounts = nextAccounts.map(normalizeAccount);
      const activeIds = new Set(accounts.map((account) => account.id));
      for (const accountId of reveals.keys()) {
        if (!activeIds.has(accountId)) {
          clearReveal(accountId);
        }
      }
    },

    setReveal(accountId, credentials) {
      clearReveal(accountId);
      reveals.set(accountId, copyReveal(credentials));
      revealTimers.set(accountId, setTimeout(() => clearReveal(accountId), revealTtlMs));
    },

    getReveal(accountId) {
      const credentials = reveals.get(accountId);
      return credentials ? { ...credentials } : undefined;
    },

    clearReveal,

    clearSecrets() {
      for (const accountId of [...reveals.keys()]) {
        clearReveal(accountId);
      }
    },
  };
}
