import { describe, expect, it } from "vitest";
import type { Clip, LayoutInfo } from "../api/types";
import { cropCenterAt, cropRect } from "./crop";
import { fmtCompact, fmtDuration, fmtTime, lastStartingBefore, plural, scoreTone } from "./format";

const vertical: LayoutInfo = { id: "vertical_fill", name: "", width: 1080, height: 1920, description: "" };

function clip(edit: Partial<Clip["edit"]>): Clip {
  return {
    id: "c", video_id: "v", project_id: "p", origin: "ai", status: "suggested", start_ms: 10_000, end_ms: 40_000,
    title: "", hook: null, reason: null, hashtags: [], score: 80, score_breakdown: {}, edit, created_at: "",
    updated_at: "", latest_render: null, video_title: null,
  };
}

describe("format", () => {
  it("formats timecodes", () => {
    expect(fmtTime(0)).toBe("0:00");
    expect(fmtTime(65_400, true)).toBe("1:05.4");
    expect(fmtTime(3_725_000)).toBe("1:02:05");
    expect(fmtDuration(35_000)).toBe("35s");
    expect(fmtDuration(125_000)).toBe("2m 5s");
  });

  it("pluralises and compacts", () => {
    expect(plural(1, "video")).toBe("1 video");
    expect(plural(2, "video")).toBe("2 videos");
    expect(fmtCompact(15_300)).toBe("15.3K");
    expect(scoreTone(91)).toBe("hot");
    expect(scoreTone(65)).toBe("warm");
    expect(scoreTone(null)).toBe("cool");
  });

  it("binary-searches the last item starting before t", () => {
    const items = [0, 100, 200, 300];
    expect(lastStartingBefore(items, -1, (x) => x)).toBe(-1);
    expect(lastStartingBefore(items, 0, (x) => x)).toBe(0);
    expect(lastStartingBefore(items, 250, (x) => x)).toBe(2);
    expect(lastStartingBefore(items, 9999, (x) => x)).toBe(3);
  });
});

describe("crop (mirrors the renderer)", () => {
  it("follows the stored face track only while its range matches the clip", () => {
    const tracked = clip({ reframe: "auto", track: [[0, 0.2], [5, 0.8]], track_range: [10_000, 40_000] });
    expect(cropCenterAt(tracked, 12_000)).toBe(0.2);
    expect(cropCenterAt(tracked, 16_000)).toBe(0.8);
    const stale = clip({ reframe: "auto", track: [[0, 0.2]], track_range: [0, 30_000] });
    expect(cropCenterAt(stale, 12_000)).toBe(0.5);
  });

  it("honours manual and centre framing", () => {
    expect(cropCenterAt(clip({ reframe: "manual", crop_x: 0.33 }), 20_000)).toBe(0.33);
    expect(cropCenterAt(clip({ reframe: "center", track: [[0, 0.1]], track_range: [10_000, 40_000] }), 20_000)).toBe(0.5);
  });

  it("keeps the 9:16 window inside a 16:9 frame", () => {
    const r = cropRect(vertical, 1920, 1080, 0.99);
    expect(r.sw).toBeCloseTo(607.5);
    expect(r.sx + r.sw).toBeCloseTo(1920);
    expect(cropRect(vertical, 1920, 1080, 0).sx).toBe(0);
    const portrait = cropRect(vertical, 1080, 2400, 0.5);
    expect(portrait.sw).toBe(1080);
    expect(portrait.sy).toBeCloseTo((2400 - 1920) / 2);
  });
});
