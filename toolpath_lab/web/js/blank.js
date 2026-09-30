// 毛坯的外形：竖直面**贴紧区域**（XY 不留余量），顶面在工件之上留 topMarginMm 余量，
// 底面与工件底面齐平——料是从下面那块基体一直长到顶面之上的。
//
// 这里只负责"未切削的毛坯长什么样"：外形尺寸、轮廓棱线、半透明实体。
// 真正的切削仿真在 stock.js（高度场），它复用这里的 blankSize 以保证两者对得上。

import * as THREE from "three";

export const BLANK_TOP_MARGIN_MM = 2.0;
export const BLANK_COLOUR = 0xb07cf0;
export const BLANK_EDGE_COLOUR = 0xe2ccff;

export function blankSize(region, topMarginMm = BLANK_TOP_MARGIN_MM) {
  const [xRange, yRange] = region.bounds_mm;
  const surface = region.surface || {};
  const baseZ = Number(surface.base_z_mm || 0);
  const topZ = Number(surface.top_z_mm != null ? surface.top_z_mm : baseZ);
  const thickness = Number(region.thickness_mm) > 0 ? Number(region.thickness_mm) : 0;
  const margin = Math.max(Number(topMarginMm) || 0, 0);
  const width = xRange[1] - xRange[0];
  const depth = yRange[1] - yRange[0];
  return {
    width,
    depth,
    diameter: width,
    height: topZ - (baseZ - thickness) + margin,
    bottom: baseZ - thickness,
    top: topZ + margin,
    topMarginMm: margin,
    centreX: (xRange[0] + xRange[1]) / 2,
    centreY: (yRange[0] + yRange[1]) / 2,
  };
}

export function blankCylindrical(region) {
  return String((region && region.id) || "") === "circle";
}

function blankGeometry(region, topMarginMm) {
  const size = blankSize(region, topMarginMm);
  const geometry = blankCylindrical(region)
    ? new THREE.CylinderGeometry(size.diameter / 2, size.diameter / 2, size.height, 96)
    : new THREE.BoxGeometry(size.width, size.depth, size.height);
  return { size, geometry };
}

/** 毛坯的棱线：既是"未切削毛坯"的轮廓，也是切削时"被削掉了多少"的参照。 */
export function buildBlankOutline(region, topMarginMm = BLANK_TOP_MARGIN_MM,
                                  edgeColour = BLANK_EDGE_COLOUR) {
  const { size, geometry } = blankGeometry(region, topMarginMm);
  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(geometry),
    new THREE.LineBasicMaterial({ color: edgeColour, transparent: true, opacity: 0.9 })
  );
  if (blankCylindrical(region)) {
    // 圆柱默认沿 Y，转成竖直（沿 Z）与工件的圆形端面对齐。
    edges.rotation.x = Math.PI / 2;
  }
  edges.position.set(size.centreX, size.centreY, size.bottom + size.height / 2);
  return edges;
}

/** 未切削的毛坯实体：半透明 + 棱线，与工件颜色明显区分。 */
export function buildBlankMesh(region, topMarginMm = BLANK_TOP_MARGIN_MM,
                               colour = BLANK_COLOUR, edgeColour = BLANK_EDGE_COLOUR) {
  const { size, geometry } = blankGeometry(region, topMarginMm);
  const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({
    color: colour, metalness: 0.1, roughness: 0.9,
    transparent: true, opacity: 0.22, depthWrite: false,
  }));
  const edges = buildBlankOutline(region, topMarginMm, edgeColour);
  const cylindrical = blankCylindrical(region);
  if (cylindrical) mesh.rotation.x = Math.PI / 2;
  mesh.position.set(size.centreX, size.centreY, size.bottom + size.height / 2);
  const group = new THREE.Group();
  group.name = "blank";
  group.add(mesh, edges);
  return group;
}
