const DEFAULT_PAGE_SIZE = 20;
const MAX_PAGE_SIZE = 100;


function positiveInteger(value, fallback) {
  const parsed = Number.parseInt(String(value), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}


export function paginate(source, requestedPage = 1, requestedPageSize = DEFAULT_PAGE_SIZE) {
  const values = Array.from(source);
  const pageSize = Math.min(
    positiveInteger(requestedPageSize, DEFAULT_PAGE_SIZE),
    MAX_PAGE_SIZE,
  );
  const totalPages = Math.max(1, Math.ceil(values.length / pageSize));
  const page = Math.min(positiveInteger(requestedPage, 1), totalPages);
  const start = (page - 1) * pageSize;

  return {
    items: values.slice(start, start + pageSize),
    page,
    pageSize,
    total: values.length,
    totalPages,
    start,
    end: Math.min(start + pageSize, values.length),
  };
}