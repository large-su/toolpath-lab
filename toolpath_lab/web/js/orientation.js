// 有向刀轴的大圆插值：刀尖到刀柄不允许用反号替代。
export function slerpAxis(first, second, ratio) {
  const normalize = v => {
    const n = Math.hypot(...v);
    return n > 1e-12 ? v.map(x => x / n) : [0, 0, 1];
  };
  const a = normalize(first), b = normalize(second);
  const t = Math.max(0, Math.min(1, ratio));
  if (t === 0) return a;
  if (t === 1) return b;
  const dot = Math.max(-1, Math.min(1, a.reduce((sum, x, i) => sum + x * b[i], 0)));
  let tangent = b.map((x, i) => x - dot * a[i]);
  if (Math.hypot(...tangent) < 1e-10) {
    if (dot > 0) return normalize(a.map((x, i) => (1 - t) * x + t * b[i]));
    const j = a.reduce((best, x, i) => Math.abs(x) < Math.abs(a[best]) ? i : best, 0);
    tangent = a.map((x, i) => (i === j ? 1 : 0) - a[j] * x);
  }
  tangent = normalize(tangent);
  const angle = Math.acos(dot) * t;
  return normalize(a.map((x, i) => Math.cos(angle) * x + Math.sin(angle) * tangent[i]));
}
