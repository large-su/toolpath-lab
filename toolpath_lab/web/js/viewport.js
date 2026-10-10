// three.js 视口：工件、区域轮廓、刀路、刀具与播放指示。
//
// 交互与观感约定：
//   左键旋转 / 中键缩放 / 右键平移（并屏蔽右键菜单），
//   顶部居中的标准视图工具条（最佳、前、后、左、右、上、下），
//   左上角的外观开关（实时阴影、白色背景、网格地面），
//   深色背景 + 雾 + 环境反射 + 阴影的"工作室"外观。

import * as THREE from "three";
import { OrbitControls } from "../vendor/OrbitControls.js";
import { RoomEnvironment } from "../vendor/RoomEnvironment.js";

const COLORS = {
  background: 0x071014,
  white: 0xffffff,
  workpiece: 0x5b6b7e,
  machined: 0x8fd4ff,
  contour: 0x54d6c4,
  cut: 0xffa726,
  link: 0xf2c94c,
  rapid: 0x4fc3f7,
  trace: 0x54d6c4,
  tool: 0xffcc00,
  holder: 0xb0bcc6,
};

//: 刀路画在工件上表面之上一点点，避免与上表面 z-fighting。
const PATH_LIFT_MM = 0.05;

//: 一段线段被扫成"胶囊"时，六个半圆分段产生的顶点数（(6+1)*2 个轮廓点 → 12 个三角 → 36 顶点）。
const CAPSULE_VERTICES_PER_SEGMENT = 36;

//: 已加工面相对工件上表面（z = 0）的抬高量，仅用于避开 z-fighting。
const MACHINED_LIFT_MM = 0.02;

//: 视图工具条上的按钮，按常用顺序排列。
export const VIEW_BUTTONS = [
  { view: "fit", label: "最佳", sub: "FIT" },
  { view: "front", label: "前", sub: "−Y" },
  { view: "back", label: "后", sub: "+Y" },
  { view: "left", label: "左", sub: "−X" },
  { view: "right", label: "右", sub: "+X" },
  { view: "top", label: "上", sub: "+Z" },
  { view: "bottom", label: "下", sub: "−Z" },
];

const OPPOSITE_VIEW = {
  front: "back", back: "front", left: "right", right: "left", top: "bottom", bottom: "top",
};

const Z_UP = new THREE.Vector3(0, 0, 1);

function orientation(view) {
  const table = {
    front: { direction: new THREE.Vector3(0, -1, 0), up: Z_UP },
    back: { direction: new THREE.Vector3(0, 1, 0), up: Z_UP },
    left: { direction: new THREE.Vector3(-1, 0, 0), up: Z_UP },
    right: { direction: new THREE.Vector3(1, 0, 0), up: Z_UP },
    top: { direction: new THREE.Vector3(0, 0, 1), up: new THREE.Vector3(0, 1, 0) },
    bottom: { direction: new THREE.Vector3(0, 0, -1), up: new THREE.Vector3(0, -1, 0) },
  };
  return table[view] || {
    direction: new THREE.Vector3(1, -1, 0.72).normalize(),
    up: Z_UP,
  };
}

// 把一段闭合的 XY 多边形拉伸成棱柱（毛坯）。
// 三个面组：上端面、下端面、侧壁。用显式三角扇而不是 THREE.ExtrudeGeometry，
// 因为后端保证轮廓是逆时针的普通多边形（不含孔），扇形三角化对凹形状同样成立。
function extrudedPrism(points, thickness) {
  const half = thickness / 2;
  const count = points.length;
  const positions = [];
  const indices = [];
  const push = (x, y, z) => {
    positions.push(x, y, z);
    return positions.length / 3 - 1;
  };

  // 上下端面各复制一份顶点，法线才能一个朝上一个朝下。
  const top = points.map(([x, y]) => push(x, y, half));
  const bottom = points.map(([x, y]) => push(x, y, -half));

  // 逆时针扇形三角化：以 0 号点为扇心。凹多边形这样切仍然有效，
  // 因为每条边与扇心构成的三角形落在同一条有向边之内。
  for (let i = 1; i + 1 < count; i += 1) {
    indices.push(top[0], top[i], top[i + 1]);
    indices.push(bottom[0], bottom[i + 1], bottom[i]);
  }
  // 侧壁：每条边一个四边形，拆成两个三角形；法线朝外。
  for (let i = 0; i < count; i += 1) {
    const next = (i + 1) % count;
    indices.push(top[i], bottom[i], bottom[next]);
    indices.push(top[i], bottom[next], top[next]);
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.setIndex(indices);
  geometry.computeVertexNormals();
  return geometry;
}

// 刀具的刀尖轮廓：把 (半径, 高度) 剖面绕轴旋转成型。
// 平底刀是"平面 + 直壁"，球头刀是"半球 + 直壁"，圆鼻刀是"平面 + 圆角 + 直壁"。
// 刀尖在 z = 0（加工面上），往上长。
function toolProfileGeometry(kind, radius, length, cornerRadius, segments = 64) {
  const points = [];
  const push = (r, z) => points.push(new THREE.Vector2(Math.max(r, 0), z));

  if (kind === "ball") {
    // 半球刀尖：从轴心 (0,0) 沿圆弧到 (R, R)。
    const steps = Math.max(12, Math.round(segments / 4));
    for (let i = 0; i <= steps; i += 1) {
      const angle = (i / steps) * (Math.PI / 2);
      push(radius * Math.sin(angle), radius - radius * Math.cos(angle));
    }
  } else if (kind === "bull") {
    const rc = Math.min(cornerRadius, radius * 0.999);
    // 刀尖平面从轴心到 R - Rc，再由圆角过渡到直壁。
    push(0, 0);
    push(radius - rc, 0);
    const steps = Math.max(6, Math.round(segments / 8));
    for (let i = 1; i <= steps; i += 1) {
      const angle = (i / steps) * (Math.PI / 2);
      push(radius - rc + rc * Math.sin(angle), rc - rc * Math.cos(angle));
    }
  } else {
    push(0, 0);
    push(radius, 0);
  }

  // 直壁延伸到指定高度。
  const top = points[points.length - 1];
  if (top.y < length - 1e-6) push(top.x, length);

  const geometry = new THREE.LatheGeometry(points, segments);
  // LatheGeometry 绕 Y 轴成型，而本工程 Z 轴朝上。
  geometry.rotateX(Math.PI / 2);
  geometry.computeVertexNormals();
  return geometry;
}

// 一段 XY 线段被半径 r 的刀具扫过，形成的"胶囊"区域——平面铣里被切掉的那一条。
// 返回三角扇用的顶点序列（[中心1, 半径1, 中心2, 半径2] 交替）。
// 半径 0（球头刀刀尖只在一点接触）时退化成极窄的一条，保证仍有可见痕迹。
function capsulePoints(from, to, radius, arcSegments = 6) {
  const points = [];
  const push = (x, y) => points.push(x, y);
  if (radius <= 1e-6) {
    push(from[0], from[1]); push(to[0], to[1]);
    return points;
  }
  const dx = to[0] - from[0];
  const dy = to[1] - from[1];
  const length = Math.hypot(dx, dy);
  const base = Math.atan2(dy, dx);
  // 起点半圆：从 -90° 绕到 +90°
  for (let i = 0; i <= arcSegments; i += 1) {
    const angle = base - Math.PI / 2 + (Math.PI * i) / arcSegments;
    push(from[0] + radius * Math.cos(angle), from[1] + radius * Math.sin(angle));
  }
  // 终点半圆：反向绕回起点方向
  for (let i = 0; i <= arcSegments; i += 1) {
    const angle = base + Math.PI / 2 - (Math.PI * i) / arcSegments;
    push(to[0] + radius * Math.cos(angle), to[1] + radius * Math.sin(angle));
  }
  return points;
}

// 把所有切削段扫过的区域拼成一个三角带几何体。
// 按"每个三角 3 个顶点"的线性缓冲组织，配合 setDrawRange 就能逐段显影，
// 因此播放时不需要重建几何。
function sweptAreaGeometry(polylines, radius, z) {
  const positions = [];
  for (const points of polylines) {
    for (let i = 0; i + 1 < points.length; i += 1) {
      const outline = capsulePoints(points[i], points[i + 1], radius);
      const count = outline.length / 2;
      // 扇形三角化：绕轮廓的第一个点展开。
      for (let k = 1; k + 1 < count; k += 1) {
        positions.push(
          outline[0], outline[1], z,
          outline[k * 2], outline[k * 2 + 1], z,
          outline[(k + 1) * 2], outline[(k + 1) * 2 + 1], z
        );
      }
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.computeVertexNormals();
  return geometry;
}

// 刀路整体抬高一点点画，避免与工件上表面互相穿插（z-fighting）。
function liftPaths(polylines) {
  return polylines.map((points) => points.map((point) => [point[0], point[1], PATH_LIFT_MM]));
}

function polylineGeometry(polylines, dashed = false) {
  const positions = [];
  for (const points of polylines) {
    for (let index = 0; index + 1 < points.length; index += 1) {
      positions.push(
        points[index][0], points[index][1], points[index][2],
        points[index + 1][0], points[index + 1][1], points[index + 1][2]
      );
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  if (dashed) geometry.computeBoundingSphere();
  return geometry;
}

export class Viewport {
  constructor(container) {
    this.container = container;
    this.appearance = { shadows: true, white: false, grid: true };
    this.display = {
      showWorkpiece: true, showPath: true, showRapid: true, showTrace: true, showTool: true,
      showMachined: true,
    };
    this.bounds = null;
    this.activeView = "fit";
    this._lastTraversed = -1;

    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.0;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFShadowMap;
    this.renderer.domElement.style.touchAction = "none";
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(COLORS.background);
    this.scene.fog = new THREE.FogExp2(COLORS.background, 0.00004);

    const environment = new RoomEnvironment();
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.environmentTexture = pmrem.fromScene(environment, 0.04).texture;
    this.scene.environment = this.environmentTexture;
    environment.dispose();
    pmrem.dispose();

    this.camera = new THREE.PerspectiveCamera(42, 1, 0.5, 100000);
    this.camera.up.set(0, 0, 1);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.mouseButtons.LEFT = THREE.MOUSE.ROTATE;
    this.controls.mouseButtons.MIDDLE = THREE.MOUSE.DOLLY;
    this.controls.mouseButtons.RIGHT = THREE.MOUSE.PAN;
    this.renderer.domElement.addEventListener("contextmenu", (event) => event.preventDefault());

    this.scene.add(new THREE.HemisphereLight(0xb9e9ff, 0x111513, 1.5));
    this.keyLight = new THREE.DirectionalLight(0xffffff, 2.6);
    this.keyLight.position.set(220, -320, 620);
    this.keyLight.castShadow = true;
    this.keyLight.shadow.mapSize.set(1024, 1024);
    this.keyLight.shadow.bias = -0.0005;
    this.keyLight.shadow.normalBias = 0.6;
    this.scene.add(this.keyLight);
    const rim = new THREE.DirectionalLight(0x52d8c5, 1.4);
    rim.position.set(-520, 420, 220);
    this.scene.add(rim);

    this.gridGroup = new THREE.Group();
    this.workpieceGroup = new THREE.Group();
    this.machinedGroup = new THREE.Group();
    this.contourGroup = new THREE.Group();
    this.pathGroup = new THREE.Group();
    this.traceGroup = new THREE.Group();
    this.toolGroup = new THREE.Group();
    this.scene.add(
      this.gridGroup, this.workpieceGroup, this.machinedGroup,
      this.contourGroup, this.pathGroup, this.traceGroup, this.toolGroup
    );

    this.tool = null;
    this.toolMesh = null;
    this.traceLine = null;
    this.rapidLine = null;
    this.machinedMesh = null;
    // 每条切削段在时间轴上结束的采样下标，用于按播放进度显影。
    this.cutSegmentEnds = [];

    this.resize();
    if (typeof ResizeObserver !== "undefined") {
      this.observer = new ResizeObserver(() => this.resize());
      this.observer.observe(container);
    }
  }

  // ------------------------------------------------------------ 生命周期
  resize() {
    const width = this.container.clientWidth;
    const height = this.container.clientHeight;
    if (!width || !height) return;
    this.renderer.setSize(width, height, false);
    this.renderer.domElement.style.width = "100%";
    this.renderer.domElement.style.height = "100%";
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
  }

  render() {
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  // ---------------------------------------------------------------- 结果
  setResult(payload) {
    this._clear(this.workpieceGroup);
    this._clear(this.machinedGroup);
    this._clear(this.contourGroup);
    this._clear(this.pathGroup);
    this._clear(this.traceGroup);

    const region = payload.region;
    const [xMin, xMax] = region.bounds_mm[0];
    const [yMin, yMax] = region.bounds_mm[1];
    const span = Math.max(xMax - xMin, yMax - yMin);

    const thickness = this._thickness(span);
    this.workpieceGroup.add(this._workpiece(region, thickness));
    this.contourGroup.add(this._contour(region.boundary));
    this._rebuildGrid(span, thickness);

    const groups = { cut: [], link: [], rapid: [] };
    for (const move of payload.toolpath.moves) {
      (groups[move.kind] || groups.cut).push(move.points);
    }
    for (const kind of Object.keys(groups)) groups[kind] = liftPaths(groups[kind]);
    this.pathGroup.add(this._line(groups.cut, COLORS.cut, 1));
    this.pathGroup.add(this._line(groups.link, COLORS.link, 1));
    this.rapidLine = this._line(groups.rapid, COLORS.rapid, 0.75, true);
    this.pathGroup.add(this.rapidLine);

    if (payload.timeline && payload.timeline.positions) {
      const geometry = polylineGeometry([liftPaths([payload.timeline.positions])[0]]);
      this.traceLine = new THREE.LineSegments(
        geometry,
        new THREE.LineBasicMaterial({ color: COLORS.trace, transparent: true, opacity: 0.95 })
      );
      this.traceLine.geometry.setDrawRange(0, 0);
      this.traceGroup.add(this.traceLine);
    } else {
      this.traceLine = null;
    }

    this.machinedMesh = this._buildMachined(payload);
    if (this.machinedMesh) this.machinedGroup.add(this.machinedMesh);

    this.bounds = new THREE.Box3().setFromObject(this.workpieceGroup);
    const pathBounds = new THREE.Box3().setFromObject(this.pathGroup);
    if (!pathBounds.isEmpty()) this.bounds.union(pathBounds);
    // 让刀具的上半截也落在取景范围内（长度直接来自响应，不依赖调用顺序）。
    const toolLength = Number((payload.tool && payload.tool.length_mm) || 0);
    if (toolLength > 0) {
      this.bounds.expandByPoint(new THREE.Vector3(0, 0, toolLength * 0.5));
    }
    this._lastTraversed = -1;
    this.setDisplayOptions(this.display);
    this._autoFrame();
  }

  // 只在"工件尺寸变了"或第一次出结果时重新取景：
  // 调一个切宽就把视角拉回默认，是很烦人的体验。
  _autoFrame() {
    const size = this.bounds.getSize(new THREE.Vector3());
    const diagonal = size.length();
    const changed = !this.fittedDiagonal
      || Math.abs(diagonal - this.fittedDiagonal) / this.fittedDiagonal > 0.12;
    if (changed) {
      this.applyView("fit");
      this.fittedDiagonal = diagonal;
    }
  }

  resetView() {
    this.applyView("fit");
    if (this.bounds) this.fittedDiagonal = this.bounds.getSize(new THREE.Vector3()).length();
  }

  setTool(tool) {
  this.tool = tool;
  this._clear(this.toolGroup);
  const radius = Math.max(tool.radius_mm, 0.2);
  const length = tool.length_mm;
  const flute = Math.min(length * 0.65, radius * 6);
  const holder = Math.max(length - flute, length * 0.2);
  const cornerRadius = Number(tool.corner_radius_mm) || 0;

  // 切削段按刀尖类型成型（平底 / 球头 / 圆鼻），刀柄一律是圆柱。
  // 黄色切削段对齐 UGNX 的刀具配色。
  const cutting = new THREE.Mesh(
    toolProfileGeometry(tool.kind, radius, flute, cornerRadius),
    new THREE.MeshStandardMaterial({
      color: COLORS.tool, metalness: 0.5, roughness: 0.34,
    })
  );
  const shank = new THREE.Mesh(
    new THREE.CylinderGeometry(radius * 1.25, radius * 1.25, holder, 48),
    new THREE.MeshStandardMaterial({
      color: COLORS.holder, metalness: 0.92, roughness: 0.24,
    })
  );
  for (const mesh of [cutting, shank]) {
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    this.toolGroup.add(mesh);
  }
  shank.rotation.x = Math.PI / 2;
  shank.position.z = flute + holder / 2;
  this.toolMesh = this.toolGroup;
  this.toolGroup.visible = this.display.showTool;
}

  // 材料切除（已加工面积）：把所有切削段被刀具扫过的区域预先拼成一个几何体，
// 再按播放进度用 setDrawRange 逐段显影。这样每帧只改一个数字，不必重建几何。
_buildMachined(payload) {
    const timeline = payload.timeline;
    const tool = payload.tool || {};
    if (!timeline || !timeline.positions || !timeline.move_runs) return null;

    // 足迹半径决定切掉的宽度。球头刀足迹为 0（刀尖只在一点接触），
    // 这里给一点下限，否则玩家完全看不到加工痕迹。
    const footprint = Math.max(Number(tool.footprint_radius_mm) || 0, 0.15 * (Number(tool.radius_mm) || 0));
    if (footprint <= 1e-6) return null;

    const moves = payload.toolpath.moves;
    const runs = timeline.move_runs;   // [[起始采样下标, 运动段号], ...]
    const sampleCount = timeline.sample_count || timeline.positions.length;
    if (sampleCount <= 0) return null;

    // 每个采样点属于哪一段运动
    const moveOfSample = new Int32Array(sampleCount);
    for (let r = 0; r < runs.length; r += 1) {
      const start = runs[r][0];
      const end = r + 1 < runs.length ? runs[r + 1][0] : sampleCount;
      for (let i = start; i < end && i < sampleCount; i += 1) moveOfSample[i] = runs[r][1];
    }
    // 一次遍历求出每一段运动最后一个采样点的下标
    const lastSampleOfMove = new Int32Array(moves.length).fill(-1);
    for (let i = sampleCount - 1; i >= 0; i -= 1) lastSampleOfMove[moveOfSample[i]] = i;

    const cutPolylines = [];
    this.cutReveal = [];
    for (let index = 0; index < moves.length; index += 1) {
      const move = moves[index];
      if (move.kind !== "cut") continue;
      cutPolylines.push(move.points);
      // 该切削段扫过的区域一共占多少顶点，显影时按段累加。
      const lineCount = Math.max(move.points.length - 1, 0);
      this.cutReveal.push({
        end: Math.max(lastSampleOfMove[index], 0),
        vertices: lineCount * CAPSULE_VERTICES_PER_SEGMENT,
      });
    }
    if (!cutPolylines.length) return null;

    // 平面扫过的区域画在工件上表面略上方，避免 z-fighting
    // 工件上表面在 z = 0（extrudedPrism 建的棱柱随后整体下移 thickness/2），
    // 已加工面只抬高一点点避免 z-fighting，不能用 thickness/2 —— 那会浮在半空。
    const geometry = sweptAreaGeometry(cutPolylines, footprint, MACHINED_LIFT_MM);
    geometry.setDrawRange(0, 0);

    const mesh = new THREE.Mesh(
      geometry,
      new THREE.MeshStandardMaterial({
        color: COLORS.machined,
        metalness: 0.55,
        roughness: 0.30,
        side: THREE.DoubleSide,
      })
    );
    mesh.receiveShadow = true;
    return mesh;
  }

  // 按播放进度显影已加工面积。拖动进度条来回拖动也正确，因为只是设一个上界。
  _revealMachined(traversedSegments) {
    if (!this.machinedMesh || !this.cutReveal || !this.cutReveal.length) return;
    let count = 0;
    let visible = 0;
    for (const item of this.cutReveal) {
      if (item.end <= traversedSegments) {
        count += item.vertices;
        visible += 1;
      } else break;
    }
    const total = this.machinedMesh.geometry.attributes.position.count;
    this.machinedMesh.geometry.setDrawRange(0, Math.min(count, total));
    this.machinedMesh.visible = visible > 0;
  }

  setPlayhead(position, traversedSegments) {
    this.toolGroup.position.set(position[0], position[1], position[2]);
    if (this.traceLine && traversedSegments !== this._lastTraversed) {
      this._lastTraversed = traversedSegments;
      this.traceLine.geometry.setDrawRange(0, Math.max(0, traversedSegments) * 2);
    }
    this._revealMachined(Math.max(0, traversedSegments));
  }

  setDisplayOptions(options) {
    this.display = Object.assign({}, this.display, options || {});
    this.workpieceGroup.visible = this.display.showWorkpiece;
    this.machinedGroup.visible = this.display.showWorkpiece && this.display.showMachined;
    this.pathGroup.visible = this.display.showPath;
    this.traceGroup.visible = this.display.showPath && this.display.showTrace;
    this.toolGroup.visible = this.display.showTool;
    if (this.rapidLine) this.rapidLine.visible = this.display.showRapid;
    this.contourGroup.visible = this.display.showWorkpiece;
  }

  setAppearance(options) {
    this.appearance = Object.assign({}, this.appearance, options || {});
    const { shadows, white, grid } = this.appearance;
    const background = new THREE.Color(white ? COLORS.white : COLORS.background);
    this.scene.background = background;
    this.scene.fog.color.copy(background);
    this.renderer.shadowMap.enabled = shadows;
    this.renderer.shadowMap.needsUpdate = true;
    this.keyLight.castShadow = shadows;
    this.gridGroup.visible = grid;
    for (const group of [this.workpieceGroup, this.toolGroup]) {
      group.traverse((object) => {
        if (object.isMesh) object.castShadow = shadows;
      });
    }
  }

  // ---------------------------------------------------------------- 视角
  applyView(view) {
    if (!this.bounds || this.bounds.isEmpty()) return;
    const { direction, up } = orientation(view);
    const center = this.bounds.getCenter(new THREE.Vector3());
    const radius = Math.max(this.bounds.getBoundingSphere(new THREE.Sphere()).radius, 1);
    const right = new THREE.Vector3().crossVectors(up, direction).normalize();
    const vertical = new THREE.Vector3().crossVectors(direction, right).normalize();
    const verticalLimit = Math.tan(THREE.MathUtils.degToRad(this.camera.getEffectiveFOV() / 2)) * 0.82;
    const horizontalLimit = verticalLimit * this.camera.aspect;

    // 把包围盒八个角都放进视锥：每个角还有自己的进深，取最远的那个。
    let distance = radius * 2;
    for (const x of [this.bounds.min.x, this.bounds.max.x]) {
      for (const y of [this.bounds.min.y, this.bounds.max.y]) {
        for (const z of [this.bounds.min.z, this.bounds.max.z]) {
          const offset = new THREE.Vector3(x, y, z).sub(center);
          const depth = offset.dot(direction);
          distance = Math.max(
            distance,
            depth + Math.abs(offset.dot(right)) / horizontalLimit,
            depth + Math.abs(offset.dot(vertical)) / verticalLimit,
            depth + radius * 0.05
          );
        }
      }
    }

    this.camera.up.copy(up);
    this.camera.position.copy(center).addScaledVector(direction, distance);
    this.camera.near = Math.max(radius / 500, 0.1);
    this.camera.far = Math.max(distance + radius * 40, 2000);
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(center);
    this.controls.minDistance = Math.max(radius * 0.1, 1);
    this.controls.maxDistance = Math.max(radius * 20, 500);
    this.controls.update();
    this.activeView = view;
  }

  oppositeOf(view) {
    return OPPOSITE_VIEW[view] || "fit";
  }

  // -------------------------------------------------------------- 几何构造
  _thickness(span) {
    return Math.min(Math.max(span * 0.09, 4), 24);
  }

  // 把区域轮廓拉伸成毛坯实体：上下两个端面 + 一圈侧壁。
  // 直接用后端给的 boundary 多边形，而不是按形状 id 特判——这样新增任何形状
  // （椭圆、圆角矩形、凹多边形）毛坯都自动跟着变，不需要改前端。
  _workpiece(region, thickness) {
    const material = new THREE.MeshStandardMaterial({
      color: COLORS.workpiece, metalness: 0.65, roughness: 0.42,
    });
    const points = region.boundary.map((p) => [p[0], p[1]]);
    const geometry = extrudedPrism(points, thickness);
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.z = -thickness / 2;
    mesh.receiveShadow = true;
    mesh.castShadow = true;
    return mesh;
  }

  _contour(boundary) {
    const points = boundary.map((point) => new THREE.Vector3(point[0], point[1], PATH_LIFT_MM * 2));
    const geometry = new THREE.BufferGeometry().setFromPoints(points);
    return new THREE.LineLoop(
      geometry,
      new THREE.LineBasicMaterial({ color: COLORS.contour, transparent: true, opacity: 0.9 })
    );
  }

  _line(polylines, color, opacity, dashed = false) {
    const geometry = polylineGeometry(polylines);
    let material;
    if (dashed) {
      material = new THREE.LineDashedMaterial({
        color, transparent: true, opacity, dashSize: 3, gapSize: 3,
      });
    } else {
      material = new THREE.LineBasicMaterial({ color, transparent: opacity < 1, opacity });
    }
    const line = new THREE.LineSegments(geometry, material);
    if (dashed) line.computeLineDistances();
    return line;
  }

  _rebuildGrid(span, thickness) {
    this._clear(this.gridGroup);
    const size = Math.max(Math.ceil((span * 3) / 20) * 20, 100);
    const grid = new THREE.GridHelper(size, Math.max(4, Math.round(size / 10)), 0x2d6c69, 0x173331);
    grid.rotation.x = Math.PI / 2;
    // 网格是"地面"：铺在工件底面，而不是穿过工件。
    grid.position.z = -thickness - 0.1;
    grid.material.transparent = true;
    grid.material.opacity = 0.7;
    this.gridGroup.add(grid);
  }

  _clear(group) {
    for (const child of group.children.slice()) {
      group.remove(child);
      if (child.geometry) child.geometry.dispose();
      const materials = Array.isArray(child.material) ? child.material : [child.material];
      for (const material of materials) {
        if (material) material.dispose();
      }
    }
  }
}
