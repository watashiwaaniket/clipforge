import assert from "node:assert/strict";
import test from "node:test";

import {
  downloadSlug,
  formatClipRange,
  formatDuration,
  formatRelativeTime,
  isUuid,
  listRefetchInterval,
  safeHttpUrl,
  truncateUrl,
} from "./jobs";

test("listRefetchInterval polls at 2500ms when any status is active", () => {
  for (const status of ["queued", "downloading", "transcribing", "scoring", "clipping"]) {
    assert.equal(listRefetchInterval([status]), 2500);
  }
});

test("listRefetchInterval stops polling for terminal or empty lists", () => {
  assert.equal(listRefetchInterval([]), false);
  assert.equal(listRefetchInterval(["completed"]), false);
  assert.equal(listRefetchInterval(["failed"]), false);
  assert.equal(listRefetchInterval(["completed", "failed"]), false);
});

test("listRefetchInterval returns 2500ms for mixed active and terminal statuses", () => {
  assert.equal(listRefetchInterval(["completed", "scoring", "failed"]), 2500);
  assert.equal(listRefetchInterval(["failed", "queued"]), 2500);
});

test("downloadSlug strips unsafe characters and respects 60-char cap", () => {
  assert.equal(downloadSlug(null), "clip");
  assert.equal(downloadSlug(""), "clip");
  assert.equal(downloadSlug('He just "YEETED"\r\nthe car!'), "He-just-YEETED-the-car");
  assert.equal(downloadSlug("!!!"), "clip");
  assert.equal(downloadSlug("a".repeat(80)).length, 60);
});

test("isUuid validates standard UUID strings", () => {
  assert.equal(isUuid("00000000-0000-4000-8000-000000000001"), true);
  assert.equal(isUuid("not-a-uuid"), false);
  assert.equal(isUuid("../secret"), false);
});

test("formatting and URL helpers handle valid and edge inputs", () => {
  assert.equal(formatDuration(0), "0:00");
  assert.equal(formatDuration(65), "1:05");
  assert.equal(formatDuration(3661), "1:01:01");
  assert.equal(formatDuration(null), null);
  assert.equal(formatClipRange(12.3, 41.0), "0:12 – 0:41");
  assert.equal(truncateUrl("https://www.youtube.com/watch?v=abc", 20), "www.youtube.com/wat…");
  assert.equal(
    formatRelativeTime("2026-11-18T12:00:00.000Z", Date.parse("2026-11-18T12:05:00.000Z")),
    "5m ago"
  );
  assert.equal(safeHttpUrl("https://www.twitch.tv/videos/123"), "https://www.twitch.tv/videos/123");
  assert.equal(safeHttpUrl("javascript:alert(1)"), null);
});
