export const vertexShader = `
attribute vec2 position;
varying vec2 uv;
void main() {
  uv = position;
  gl_Position = vec4(position, 0.0, 1.0);
}`;

export const fragmentShader = `
precision highp float;
varying vec2 uv;
uniform vec2 resolution;
uniform float time;
uniform float clock;
uniform float seed;
uniform float complexity;
uniform float level;
uniform float energy;
uniform vec3 colors[4];

float hash(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}

float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  f = f*f*f*(f*(f*6.0-15.0)+10.0);
  return mix(mix(hash(i), hash(i+vec2(1,0)), f.x),
             mix(hash(i+vec2(0,1)), hash(i+vec2(1,1)), f.x), f.y);
}

float fbm(vec2 p) {
  float sum = 0.0;
  float amplitude = 0.5;
  mat2 turn = mat2(0.8, -0.6, 0.6, 0.8);
  for (int i = 0; i < 4; i++) {
    sum += amplitude * noise(p);
    p = turn * p * 2.03 + 13.7;
    amplitude *= 0.35;
  }
  return sum;
}

void main() {
  // The disc rests a little inside the canvas and swells into the margin:
  // a slow breath always, a quick one on every syllable.
  float breath = 0.5 + 0.5*sin(clock*1.1 + seed);
  float scale = 0.93 + 0.012*breath + 0.045*level;
  vec2 p = uv * resolution / min(resolution.x, resolution.y) / scale;
  float radius = length(p);
  float edge = 2.0 / (min(resolution.x, resolution.y) * scale);
  float alpha = 1.0 - smoothstep(0.985-edge, 0.985, radius);
  if (alpha == 0.0) { gl_FragColor = vec4(0.0); return; }

  // The lens bulges towards the viewer as the voice pushes.
  float z = sqrt(max(0.0, 1.0-dot(p,p)));
  vec2 q = p / (0.82 + 0.18*z*(1.0 + 0.7*level));
  // Sway: the whole field leans on a slow figure of eight, further the
  // harder the voice has been working, and the tilt wanders with it.
  float angle = 0.3*sin(seed*1.7) + 0.09*sin(clock*0.6 + seed)*(0.3 + energy);
  q = mat2(cos(angle), -sin(angle), sin(angle), cos(angle))*q;
  q += vec2(sin(clock*0.9 + seed), sin(clock*0.7 + seed*1.3)) * (0.03 + 0.14*energy);
  vec2 drift = vec2(time*0.09, -time*0.06);
  vec2 origin = vec2(seed*3.71, seed*1.93);
  vec2 warp = vec2(fbm(q*1.35 + origin + drift),
                   fbm(q*1.35 + origin + 7.3 - drift));
  // The clouds fold harder on a syllable and mix more while the voice is up.
  q += (warp-0.46) * (0.6 + complexity*0.65 + level*0.45 + energy*0.3);
  float cloud = fbm(q*(1.5+complexity*0.4)+origin+warp*0.6+drift);
  float flow = q.y + q.x*0.35 + (cloud-0.4)*(0.65+complexity*0.45);
  flow += (0.27 + 0.22*level)*sin(q.x*2.5 + time*0.14 + seed);

  vec3 color = mix(colors[0], colors[1], smoothstep(-1.0,0.15,flow));
  color = mix(color, colors[2], smoothstep(-0.3,0.75,flow));
  float side = q.x - q.y*0.22 + (cloud-0.45)*0.85;
  color = mix(color, colors[3], smoothstep(-0.1,1.1,side));

  // The bright seam widens and glows with the voice.
  float mist = exp(-pow((flow-0.06)*(3.2 - 1.2*level), 2.0));
  mist *= (0.24 + 0.16*fbm(q*2.0+origin+19.0)) * (1.0 + 0.7*level);
  color = mix(color, vec3(1.0,0.985,0.965), mist);
  color += (cloud-0.46)*0.085;
  color = mix(color, vec3(1.0), 0.07 + 0.11*smoothstep(-0.6,0.9,p.y) + 0.05*level);
  color *= 0.97 + 0.03*z;
  float grain = (hash(gl_FragCoord.xy)-0.5)/255.0;
  gl_FragColor = vec4(clamp(color+grain,0.0,1.0), alpha);
}`;
