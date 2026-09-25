import { useEffect, useRef } from "react";
import { createVoiceOrb, type VoiceOrbOptions } from "./createVoiceOrb";
import type { OrbColors } from "./identity";

export type VoiceOrbProps = Omit<VoiceOrbOptions, "colors"> & {
  colors: OrbColors;
  size?: number;
  label?: string;
  className?: string;
};

/** Decorative unless labelled. Owns and cleans up its WebGL renderer. */
export function VoiceOrb({ colors, seed = 1, complexity = 0.65, level = 0,
  speed = 0.35, animated = true, size = 160, label, className }: VoiceOrbProps) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const renderer = useRef<ReturnType<typeof createVoiceOrb>>(null);
  useEffect(() => {
    if (!canvas.current) return;
    renderer.current = createVoiceOrb(canvas.current);
    return () => { renderer.current?.destroy(); renderer.current = null; };
  }, []);
  // Each colour on its own: `colors` is a fresh array from every render.
  const [first, second, third, fourth] = colors;
  useEffect(() => {
    const worn: OrbColors = [first, second, third, fourth];
    renderer.current?.setOptions({ colors: worn, seed, complexity, level, speed, animated });
    if (canvas.current) {
      canvas.current.style.background = renderer.current ? "none"
        : `linear-gradient(35deg, ${worn.join(", ")})`;
    }
  }, [first, second, third, fourth, seed, complexity, level, speed, animated]);
  return <span className={className} role={label ? "img" : undefined}
    aria-label={label} aria-hidden={label ? undefined : true}
    style={{ display: "inline-block", width: size, height: size, flexShrink: 0,
      borderRadius: "50%", overflow: "hidden", verticalAlign: "middle" }}>
    <canvas ref={canvas} style={{ display: "block", width: "100%", height: "100%" }} />
  </span>;
}
