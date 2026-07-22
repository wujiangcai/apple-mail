import { expect, test } from "vitest";

import { paginate } from "../../mail_manager/static/js/pagination.js";


test("pagination converts string controls to bounded integers", () => {
  const values = Array.from({ length: 31 }, (_, index) => index + 1);

  const result = paginate(values, "99", "10");

  expect(result.page).toBe(4);
  expect(result.pageSize).toBe(10);
  expect(result.totalPages).toBe(4);
  expect(result.items).toEqual([31]);
});


test("invalid pagination values fall back to a usable first page", () => {
  const result = paginate(["a", "b"], "not-a-page", "0");

  expect(result.page).toBe(1);
  expect(result.pageSize).toBe(20);
  expect(result.items).toEqual(["a", "b"]);
});


test("pagination does not mutate its source collection", () => {
  const source = [1, 2, 3];

  paginate(source, 1, 2);

  expect(source).toEqual([1, 2, 3]);
});
