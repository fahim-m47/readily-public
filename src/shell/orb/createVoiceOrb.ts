import type { OrbColors } from "./identity";
import { fragmentShader, vertexShader } from "./shaders";

// What an orb wears until its Voice is set: the old opal preset.
const DEFAULT_COLORS: OrbColors = ["#287cf4", "#aa9aee", "#ffd1bc", "#85d7ef"];

export type VoiceOrbOptions = {
  colors?: OrbColors;
  seed?: number;
  /** 0–1. Higher values introduce more folds in the fluid. */
  complexity?: number;
  /**
   * 0–1. A normalized audio envelope, not raw audio samples. A function is
   * sampled every frame, for a source that moves faster than React should.
   */
  level?: number | (() => number);
  /** 0–2. Default 0.35 is a slow idle drift. */
  speed?: number;
  animated?: boolean;
};

// One step of a first-order envelope follower with the given time constant.
function follow(current: number, target: number, seconds: number, delta: number) {
  return current + (target - current) * (1 - Math.exp(-delta / seconds));
}

function finiteRange(value: number, min: number, max: number, fallback: number) {
  return Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;
}

// destroy() frees a canvas's WebGL context on the next task rather than at
// once. WebKit caps live contexts at about sixteen, so an unmounted orb has to
// give its context back, but StrictMode destroys and recreates every orb on
// the same canvas in one go, and a lost context is the only one that canvas
// would ever hand back. Recreating before the timer fires cancels the loss.
const pendingLoss = new WeakMap<HTMLCanvasElement, ReturnType<typeof setTimeout>>();

/** Owns a canvas until destroy(). Returns null when WebGL is unavailable or the shaders will not build. */
export function createVoiceOrb(canvas: HTMLCanvasElement, initial: VoiceOrbOptions = {}) {
  clearTimeout(pendingLoss.get(canvas));
  pendingLoss.delete(canvas);
  const context = canvas.getContext("webgl", { alpha: true, premultipliedAlpha: false, antialias: false });
  if (!context) return null;
  const gl = context;

  let options: Required<VoiceOrbOptions> = { colors: DEFAULT_COLORS, seed: 1, complexity: 0.65, level: 0, speed: 0.35, animated: true, ...initial };
  let program: WebGLProgram | null = null;
  let buffer: WebGLBuffer | null = null;
  let uniforms: Record<string, WebGLUniformLocation | null> = {};
  let frame = 0;
  let elapsed = 0;
  let lastTime = 0;
  let lastDraw = 0;
  // Two followers on the one envelope. `smoothedLevel` snaps up on a
  // syllable and lets go over a fifth of a second: the pulse. `energy` takes
  // most of a second either way: how hard the voice has been working, which
  // is what the clouds' churn and drift answer to. And `clock` is wall time,
  // unscaled by speed, for the breath that runs under everything.
  let smoothedLevel = 0;
  let energy = 0;
  let clock = 0;
  let visible = true;
  let destroyed = false;
  const motion = matchMedia("(prefers-reduced-motion: reduce)");

  function compile(type: number, source: string) {
    const shader = gl.createShader(type);
    if (!shader) throw new Error("Could not allocate an orb shader");
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const message = gl.getShaderInfoLog(shader);
      gl.deleteShader(shader);
      throw new Error(`Orb shader compilation failed: ${message}`);
    }
    return shader;
  }

  function initialize() {
    const vertex = compile(gl.VERTEX_SHADER, vertexShader);
    let fragment: WebGLShader;
    try { fragment = compile(gl.FRAGMENT_SHADER, fragmentShader); }
    catch (error) { gl.deleteShader(vertex); throw error; }
    program = gl.createProgram();
    if (!program) {
      gl.deleteShader(vertex);
      gl.deleteShader(fragment);
      throw new Error("Could not allocate an orb program");
    }
    gl.attachShader(program, vertex);
    gl.attachShader(program, fragment);
    gl.linkProgram(program);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      const message = gl.getProgramInfoLog(program);
      gl.deleteProgram(program);
      throw new Error(`Orb shader link failed: ${message}`);
    }
    buffer = gl.createBuffer();
    if (!buffer) { gl.deleteProgram(program); throw new Error("Could not allocate an orb buffer"); }
    gl.useProgram(program);
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, -1,1, 1,-1, 1,1]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, "position");
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    uniforms = {};
    for (const name of ["resolution", "time", "clock", "seed", "complexity", "level", "energy", "colors[0]"]) {
      uniforms[name] = gl.getUniformLocation(program, name);
    }
  }

  function draw() {
    if (destroyed || gl.isContextLost()) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.min(768, Math.round(canvas.clientWidth * ratio)));
    const height = Math.max(1, Math.min(768, Math.round(canvas.clientHeight * ratio)));
    if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
    gl.viewport(0, 0, width, height);
    gl.useProgram(program);
    gl.uniform2f(uniforms.resolution, width, height);
    gl.uniform1f(uniforms.time, elapsed);
    gl.uniform1f(uniforms.clock, clock);
    gl.uniform1f(uniforms.seed, finiteRange(options.seed, -1000, 1000, 1));
    gl.uniform1f(uniforms.complexity, finiteRange(options.complexity, 0, 1, 0.65));
    gl.uniform1f(uniforms.level, motion.matches ? 0 : smoothedLevel);
    gl.uniform1f(uniforms.energy, motion.matches ? 0 : energy);
    const colors = options.colors.flatMap(hex => [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16) / 255));
    gl.uniform3fv(uniforms["colors[0]"], colors);
    gl.drawArrays(gl.TRIANGLES, 0, 6);
  }

  function tick(now: number) {
    frame = 0;
    const delta = lastTime ? Math.min((now - lastTime) / 1000, 0.1) : 0;
    lastTime = now;
    const level = finiteRange(typeof options.level === "function" ? options.level() : options.level, 0, 1, 0);
    smoothedLevel = follow(smoothedLevel, level, level > smoothedLevel ? 0.03 : 0.2, delta);
    energy = follow(energy, level, level > energy ? 0.5 : 0.9, delta);
    clock += delta;
    elapsed += delta * finiteRange(options.speed, 0, 2, 0.35) * (1 + energy * 1.2 + smoothedLevel * 0.6);
    if (now - lastDraw >= 1000 / 30) { draw(); lastDraw = now; }
    frame = requestAnimationFrame(tick);
  }

  function refresh() {
    cancelAnimationFrame(frame);
    frame = 0;
    lastTime = 0;
    if (destroyed || gl.isContextLost() || document.hidden || !visible) return;
    // A stopped orb settles rather than freezing mid-pulse.
    if (!options.animated || motion.matches) { smoothedLevel = 0; energy = 0; }
    draw();
    if (options.animated && !motion.matches) frame = requestAnimationFrame(tick);
  }

  function lost(event: Event) { event.preventDefault(); cancelAnimationFrame(frame); }
  function restored() {
    try { initialize(); } catch { return; }
    refresh();
  }
  try { initialize(); }
  catch { gl.getExtension("WEBGL_lose_context")?.loseContext(); return null; }
  const resize = new ResizeObserver(refresh);
  resize.observe(canvas);
  const intersection = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; refresh(); });
  intersection.observe(canvas);
  motion.addEventListener("change", refresh);
  document.addEventListener("visibilitychange", refresh);
  canvas.addEventListener("webglcontextlost", lost);
  canvas.addEventListener("webglcontextrestored", restored);
  refresh();

  return {
    setOptions(next: VoiceOrbOptions) {
      const wasAnimated = options.animated;
      options = { ...options, ...next };
      if (wasAnimated !== options.animated) refresh();
      else if (!frame && visible && !document.hidden) draw();
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      cancelAnimationFrame(frame);
      resize.disconnect();
      intersection.disconnect();
      motion.removeEventListener("change", refresh);
      document.removeEventListener("visibilitychange", refresh);
      canvas.removeEventListener("webglcontextlost", lost);
      canvas.removeEventListener("webglcontextrestored", restored);
      gl.deleteBuffer(buffer);
      gl.deleteProgram(program);
      pendingLoss.set(canvas, setTimeout(() => {
        pendingLoss.delete(canvas);
        gl.getExtension("WEBGL_lose_context")?.loseContext();
      }));
    },
  };
}
