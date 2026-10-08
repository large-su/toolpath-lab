// 刀身与高度场毛坯的几何干涉检查。绘制和检测共享同一套轴向尺寸。
export function toolEnvelope(tool) {
  const radius = Math.max(Number(tool.radius_mm || tool.diameter_mm / 2), 0.1);
  const length = Math.max(Number(tool.length_mm), 0.1);
  const cuttingLength = Math.min(length, tool.cutting_length_mm ?? (tool.kind === "ball"
    ? radius * 2 : Math.min(length * 0.65, radius * 6)));
  return { radius, length, cuttingLength, shankRadius: tool.shank_radius_mm ?? radius * 1.25 };
}

import { slerpAxis } from "./orientation.js";

export function unitAxis(axis = [0, 0, 1]) {
  const norm = Math.hypot(...axis);
  return norm > 1e-9 ? axis.map((value) => value / norm) : [0, 0, 1];
}

// 固定 XY 处的竖直直线与有限圆柱相交，返回 Z 区间。
// 两端用轴向平面裁剪，不使用会在端面外突出一截的胶囊体近似。
export function cylinderColumnInterval(x, y, position, axis, start, end, radius) {
  const dx = x - position[0];
  const dy = y - position[1];
  const [ax, ay, az] = axis;
  const dotXY = ax * dx + ay * dy;
  let lower = -Infinity;
  let upper = Infinity;
  if (Math.abs(az) > 1e-10) {
    const z0 = (start - dotXY) / az;
    const z1 = (end - dotXY) / az;
    lower = Math.min(z0, z1);
    upper = Math.max(z0, z1);
  } else if (dotXY < start || dotXY > end) return null;
  const a = ax * ax + ay * ay;
  const b = -2 * az * dotXY;
  const c = dx * dx + dy * dy - dotXY * dotXY - radius * radius;
  if (a < 1e-12) {
    if (c > 0) return null;
  } else {
    const discriminant = b * b - 4 * a * c;
    if (discriminant < 0) return null;
    const root = Math.sqrt(discriminant);
    lower = Math.max(lower, (-b - root) / (2 * a));
    upper = Math.min(upper, (-b + root) / (2 * a));
  }
  return upper >= lower ? [position[2] + lower, position[2] + upper] : null;
}

export function detectShankCollision(stock, position, rawAxis, tool) {
  const shape = toolEnvelope(tool);
  if (shape.length - shape.cuttingLength <= 1e-6) return null;
  const axis = unitAxis(rawAxis);
  const start = position.map((value, i) => value + axis[i] * shape.cuttingLength);
  const end = position.map((value, i) => value + axis[i] * shape.length);
  const bottom = Number(stock.spec.bottom_z_mm);
  const tolerance = 0.1;
  // 整根刀身都在毛坯顶面以上时，直接跳过网格检查。
  const radialZ = shape.shankRadius * Math.sqrt(Math.max(0, 1 - axis[2] ** 2));
  if (Math.min(start[2], end[2]) - radialZ >= stock.spec.initial_top_z_mm - tolerance) return null;
  const [[stockX0, stockX1], [stockY0, stockY1]] = stock.spec.bounds_mm;
  const x0 = Math.max(stockX0, Math.min(start[0], end[0]) - shape.shankRadius);
  const x1 = Math.min(stockX1, Math.max(start[0], end[0]) + shape.shankRadius);
  const y0 = Math.max(stockY0, Math.min(start[1], end[1]) - shape.shankRadius);
  const y1 = Math.min(stockY1, Math.max(start[1], end[1]) + shape.shankRadius);
  if (x0 > x1 || y0 > y1) return null;
  const spacing = Math.max(0.1, Math.min(stock.spec.resolution_mm / 2, shape.shankRadius / 2));
  const nx = Math.max(1, Math.ceil((x1 - x0) / spacing));
  const ny = Math.max(1, Math.ceil((y1 - y0) / spacing));
  let hit = null;
  for (let row = 0; row <= ny; row += 1) {
    const y = y0 + (y1 - y0) * row / ny;
    for (let col = 0; col <= nx; col += 1) {
      const x = x0 + (x1 - x0) * col / nx;
      const top = stock.heightAt(x, y);
      if (top === null || top <= bottom + tolerance) continue;
      const interval = cylinderColumnInterval(x, y, position, axis,
        shape.cuttingLength, shape.length, shape.shankRadius);
      if (!interval) continue;
      const lower = Math.max(interval[0], bottom);
      const upper = Math.min(interval[1], top);
      const overlap = upper - lower;
      if (overlap <= tolerance) continue;
      if (!hit || overlap > hit.overlap_mm) {
        hit = { part: "刀身", position: [x, y, (lower + upper) / 2], overlap_mm: overlap };
      }
    }
  }
  return hit;
}

export function poseSamples(start, end, axisStart, axisEnd, tool, resolution) {
  const a = unitAxis(axisStart);
  const b = unitAxis(axisEnd);
  const translation = Math.hypot(...end.map((value, i) => value - start[i]));
  const rotation = toolEnvelope(tool).length * Math.hypot(...b.map((value, i) => value - a[i]));
  const step = Math.max(0.25, Math.min(resolution / 2, toolEnvelope(tool).radius / 2));
  const count = Math.max(1, Math.ceil((translation + rotation) / step));
  return Array.from({ length: count }, (_, i) => {
    const ratio = (i + 1) / count;
    return {
      ratio,
      position: start.map((value, j) => value + (end[j] - value) * ratio),
      axis: slerpAxis(a, b, ratio),
    };
  });
}
