"use strict";

// A head wearing headphones, drawn with flat-shaded polygons on a 2D canvas.
// Model axes follow the head tracker: X = left ear to right, Y = back to nose, Z = up.
// The view is mirrored, like looking in a mirror: turn left and the head on screen turns left.
window.createHead3D = (() => {
  const CAMERA = 6; // camera distance in front of the face (+Y)
  const LIGHT = normalize([-0.45, 0.75, 0.55]); // from the viewer's upper left
  // Details that sit on a surface are drawn after it even when their depths tie
  const DEPTH_BIAS = { eye: 0.12, cup: 0.05, accent: 0.09 };
  // The camera looks down on the head a little (radians, about X)
  const VIEW_TILT_COS = Math.cos(-0.22);
  const VIEW_TILT_SIN = Math.sin(-0.22);

  return function createHead3D(canvas) {
    const ctx = canvas.getContext("2d");
    const faces = buildModel();
    let colors = readColors();
    let shown = [1, 0, 0, 0];
    let target = [1, 0, 0, 0];
    let active = false;
    let frame = 0;

    function draw() {
      const dpr = window.devicePixelRatio || 1;
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      if (!width || !height) return;
      if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
        canvas.width = Math.round(width * dpr);
        canvas.height = Math.round(height * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      const focal = height * 0.37 * CAMERA;
      const cx = width / 2;
      const cy = height * 0.46;

      // Soft shadow under the head
      const shadow = ctx.createRadialGradient(cx, height * 0.93, 4, cx, height * 0.93, height * 0.32);
      shadow.addColorStop(0, "rgba(0, 0, 0, 0.16)");
      shadow.addColorStop(1, "rgba(0, 0, 0, 0)");
      ctx.fillStyle = shadow;
      ctx.fillRect(0, 0, width, height);

      const visible = [];
      for (const face of faces) {
        const pts = face.pts.map((p) => tilt(rotate(shown, p)));
        const normal = tilt(rotate(shown, face.normal));
        const centre = average(pts);
        if (dot(normal, [-centre[0], CAMERA - centre[1], -centre[2]]) <= 0) continue; // facing away
        visible.push({ pts, normal, depth: centre[1] + (DEPTH_BIAS[face.part] || 0), part: face.part });
      }
      visible.sort((a, b) => a.depth - b.depth); // far to near

      const palette = active ? colors.active : colors.faded;
      ctx.lineJoin = "round";
      for (const face of visible) {
        const light = face.part === "accent" ? 1 : 0.42 + 0.62 * Math.max(0, dot(face.normal, LIGHT));
        const [r, g, b] = palette[face.part].map((c) => Math.min(255, Math.round(c * light)));
        ctx.fillStyle = `rgb(${r}, ${g}, ${b})`;
        ctx.beginPath();
        face.pts.forEach((p, i) => {
          const s = focal / (CAMERA - p[1]);
          const x = cx + p[0] * s; // mirrored
          const y = cy - p[2] * s;
          if (i) ctx.lineTo(x, y);
          else ctx.moveTo(x, y);
        });
        ctx.closePath();
        ctx.fill();
        ctx.strokeStyle = ctx.fillStyle; // hides hairline seams between faces
        ctx.lineWidth = 0.8;
        ctx.stroke();
      }
    }

    function tick() {
      frame = 0;
      // Ease towards the latest pose so 25 updates per second look smooth
      const flip = dot4(shown, target) < 0 ? -1 : 1;
      shown = normalize(shown.map((v, i) => v + (flip * target[i] - v) * 0.35));
      draw();
      if (1 - Math.abs(dot4(shown, target)) > 1e-7) frame = requestAnimationFrame(tick);
    }

    function animate() {
      if (!frame) frame = requestAnimationFrame(tick);
    }

    window.addEventListener("resize", draw);
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      colors = readColors();
      draw();
    });
    requestAnimationFrame(draw);

    return {
      setTarget(q) {
        target = q;
        animate();
      },
      reset() {
        target = [1, 0, 0, 0];
        animate();
      },
      setActive(on) {
        active = on;
        draw();
      },
    };
  };

  function buildModel() {
    const list = [];
    const add = (pts, normal, part) => list.push({ pts, normal: normalize(normal), part });

    // Head: an ellipsoid, open at the bottom where the neck joins
    const R = [0.72, 0.84, 0.9];
    const C = [0, 0, 0.08];
    const lon = 36;
    const lat = 20;
    const bottom = -0.72 * (Math.PI / 2);
    const point = (t, u) => [
      C[0] + R[0] * Math.cos(u) * Math.cos(t),
      C[1] + R[1] * Math.cos(u) * Math.sin(t),
      C[2] + R[2] * Math.sin(u),
    ];
    for (let i = 0; i < lon; i++) {
      const t0 = (i / lon) * 2 * Math.PI;
      const t1 = ((i + 1) / lon) * 2 * Math.PI;
      for (let j = 0; j < lat; j++) {
        const u0 = bottom + (j / lat) * (Math.PI / 2 - bottom);
        const u1 = bottom + ((j + 1) / lat) * (Math.PI / 2 - bottom);
        const tm = (t0 + t1) / 2;
        const um = (u0 + u1) / 2;
        const n = [
          (Math.cos(um) * Math.cos(tm)) / R[0],
          (Math.cos(um) * Math.sin(tm)) / R[1],
          Math.sin(um) / R[2],
        ];
        add([point(t0, u0), point(t1, u0), point(t1, u1), point(t0, u1)], n, "head");
      }
    }

    // Neck
    cylinderZ(add, [0, -0.05], 0.3, 0.32, -1.0, -0.5, 16, "head");

    // Nose: a small pyramid on the face
    const tip = [0, 0.99, -0.12];
    const base = [[-0.1, 0.76, -0.24], [0.1, 0.76, -0.24], [0.06, 0.8, 0.06], [-0.06, 0.8, 0.06]];
    for (let k = 0; k < 4; k++) {
      const a = base[k];
      const b = base[(k + 1) % 4];
      const centroid = average([a, b, tip]);
      add([a, b, tip], sub(centroid, [0, 0.72, -0.1]), "head");
    }

    // Eyes
    for (const side of [-1, 1]) {
      const pts = [];
      for (let k = 0; k < 10; k++) {
        const a = (k / 10) * 2 * Math.PI;
        pts.push([side * 0.25 + 0.065 * Math.cos(a), 0.8, 0.2 + 0.045 * Math.sin(a)]);
      }
      add(pts, [side * 0.35, 1, 0.25], "eye");
    }

    // Ear cups: short cylinders along X
    for (const side of [-1, 1]) {
      const N = 32;
      const inner = side * 0.6;
      const outer = side * 0.92;
      const ring = (x, scale) => {
        const pts = [];
        for (let k = 0; k < N; k++) {
          const a = (k / N) * 2 * Math.PI;
          pts.push([x, -0.02 + 0.27 * scale * Math.cos(a), -0.02 + 0.35 * scale * Math.sin(a)]);
        }
        return pts;
      };
      const a = ring(inner, 1);
      const b = ring(outer, 0.93);
      for (let k = 0; k < N; k++) {
        const m = ((k + 0.5) / N) * 2 * Math.PI;
        add([a[k], a[(k + 1) % N], b[(k + 1) % N], b[k]], [0, Math.cos(m) / 0.27, Math.sin(m) / 0.35], "phones");
      }
      add(b, [side, 0, 0], "cup");
      add(ring(outer + side * 0.004, 0.45), [side, 0, 0], "accent");
    }

    // Headband: a flat arc over the head, ending in a slider above each cup
    const Y = [-0.11, 0.07];
    const rIn = 1.0;
    const rOut = 1.08;
    const steps = 32;
    const start = (22 * Math.PI) / 180;
    const end = Math.PI - start;
    const arc = (r, a, y) => [r * Math.cos(a), y, 0.04 + r * Math.sin(a)];
    for (let k = 0; k < steps; k++) {
      const a0 = start + (k / steps) * (end - start);
      const a1 = start + ((k + 1) / steps) * (end - start);
      const am = (a0 + a1) / 2;
      const radial = [Math.cos(am), 0, Math.sin(am)];
      add([arc(rOut, a0, Y[0]), arc(rOut, a1, Y[0]), arc(rOut, a1, Y[1]), arc(rOut, a0, Y[1])], radial, "phones");
      add([arc(rIn, a0, Y[0]), arc(rIn, a0, Y[1]), arc(rIn, a1, Y[1]), arc(rIn, a1, Y[0])], scale(radial, -1), "phones");
      add([arc(rIn, a0, Y[1]), arc(rOut, a0, Y[1]), arc(rOut, a1, Y[1]), arc(rIn, a1, Y[1])], [0, 1, 0], "phones");
      add([arc(rIn, a0, Y[0]), arc(rIn, a1, Y[0]), arc(rOut, a1, Y[0]), arc(rOut, a0, Y[0])], [0, -1, 0], "phones");
    }
    for (const side of [-1, 1]) {
      const x0 = side * 0.85;
      const x1 = side * 0.93;
      const top = 0.04 + rIn * Math.sin(start);
      const box = [[x0, Y[0] + 0.03, 0.2], [x1, Y[0] + 0.03, 0.2], [x1, Y[1] - 0.03, 0.2], [x0, Y[1] - 0.03, 0.2]];
      const lift = (p) => [p[0], p[1], top + 0.02];
      add([box[0], box[1], lift(box[1]), lift(box[0])], [0, -1, 0], "phones");
      add([box[3], lift(box[3]), lift(box[2]), box[2]], [0, 1, 0], "phones");
      add([box[1], box[2], lift(box[2]), lift(box[1])], [side, 0, 0], "phones");
    }
    return list;
  }

  function cylinderZ(add, center, rx, ry, z0, z1, n, part) {
    for (let k = 0; k < n; k++) {
      const a0 = (k / n) * 2 * Math.PI;
      const a1 = ((k + 1) / n) * 2 * Math.PI;
      const am = (a0 + a1) / 2;
      const p = (a, z) => [center[0] + rx * Math.cos(a), center[1] + ry * Math.sin(a), z];
      add([p(a0, z0), p(a1, z0), p(a1, z1), p(a0, z1)], [Math.cos(am) / rx, Math.sin(am) / ry, 0], part);
    }
  }

  function readColors() {
    const style = getComputedStyle(document.documentElement);
    const color = (name) => hexToRgb(style.getPropertyValue(name));
    const phones = color("--phones");
    const parts = {
      head: color("--head"),
      eye: mix(phones, [0, 0, 0], 0.3),
      phones,
      cup: mix(phones, [255, 255, 255], 0.08),
      accent: color("--phones-accent"),
    };
    // While head tracking is off the model is drawn faded into the background
    // (opaque, so overlapping edges do not show as a grid)
    const background = color("--stage-base");
    const faded = {};
    for (const [part, rgb] of Object.entries(parts)) faded[part] = mix(rgb, background, 0.55);
    return { active: parts, faded };
  }

  function rotate(q, v) {
    const [w, x, y, z] = q;
    const tx = 2 * (y * v[2] - z * v[1]);
    const ty = 2 * (z * v[0] - x * v[2]);
    const tz = 2 * (x * v[1] - y * v[0]);
    return [
      v[0] + w * tx + (y * tz - z * ty),
      v[1] + w * ty + (z * tx - x * tz),
      v[2] + w * tz + (x * ty - y * tx),
    ];
  }

  function tilt(v) {
    return [v[0], v[1] * VIEW_TILT_COS - v[2] * VIEW_TILT_SIN, v[1] * VIEW_TILT_SIN + v[2] * VIEW_TILT_COS];
  }

  function dot(a, b) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  }

  function dot4(a, b) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3];
  }

  function normalize(v) {
    const length = Math.hypot(...v) || 1;
    return v.map((x) => x / length);
  }

  function sub(a, b) {
    return a.map((x, i) => x - b[i]);
  }

  function scale(v, k) {
    return v.map((x) => x * k);
  }

  function average(points) {
    const sum = [0, 0, 0];
    for (const p of points) for (let i = 0; i < 3; i++) sum[i] += p[i];
    return sum.map((x) => x / points.length);
  }

  function hexToRgb(text) {
    let hex = text.trim().replace("#", "");
    if (hex.length === 3) hex = [...hex].map((c) => c + c).join("");
    const n = parseInt(hex, 16) || 0;
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function mix(a, b, t) {
    return a.map((x, i) => Math.round(x + (b[i] - x) * t));
  }
})();
