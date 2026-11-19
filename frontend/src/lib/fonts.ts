import { useEffect, useState } from "react";
import type { CaptionPreset } from "../api/types";

const loaded = new Map<string, Promise<void>>();

/** CSS family name we register for a caption font file (served by the API, identical to the
 * file libass burns into exports, so the preview matches the render). */
export const fontFamilyFor = (preset: CaptionPreset) => `cf-${preset.font_file.replace(/\.[a-z]+$/i, "")}`;

export function loadCaptionFont(preset: CaptionPreset): Promise<void> {
  const family = fontFamilyFor(preset);
  let p = loaded.get(family);
  if (!p) {
    const face = new FontFace(family, `url(/api/fonts/${preset.font_file})`);
    p = face.load().then((f) => {
      document.fonts.add(f);
    });
    loaded.set(family, p);
  }
  return p;
}

export function useCaptionFonts(presets: CaptionPreset[] | undefined): boolean {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    if (!presets) return;
    let alive = true;
    Promise.all(presets.map(loadCaptionFont))
      .catch(() => undefined)
      .finally(() => alive && setReady(true));
    return () => {
      alive = false;
    };
  }, [presets]);
  return ready;
}
