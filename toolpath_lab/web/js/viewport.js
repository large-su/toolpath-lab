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
  stock: 0x93a4b8,
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

// 刀路整体抬高一点点画，避免与工件上表面互相穿插（z-fighting）。
function liftPaths(polylines) {
  return polylines.map((points) => points.map((point) => [point[0], point[1], PATH_LIFT_MM]));
}

// ------------------------------------------------------------------ 高度场
// 材料切除仿真把毛坯离散成一张高度场，载荷里每帧只带"相对上一帧变化的格子"。
// 每个游程是 [行号, 起始列, 值串]：值串里**每两个字符是一个格子**（一个小端
// uint16，低字节在前），还原公式是 value * step_mm + offset_mm。
function decodeStockFrames(payload) {
  const { rows, columns, encoding, stock_top_mm: stockTop } = payload;
  const state = new Uint16Array(rows * columns);
  const initial = Math.round((stockTop - encoding.offset_mm) / encoding.step_mm);
  state.fill(initial);
  const frames = [];
  for (const runs of payload.frames) {
    for (const [row, start, packed] of runs) {
      const base = row * columns + start;
      const count = packed.length >> 1;
      for (let index = 0; index < count; index += 1) {
        // 一个格子占两个码点：低位字节在前。
        state[base + index] =
          packed.charCodeAt(index * 2) + (packed.charCodeAt(index * 2 + 1) << 8);
      }
    }
    frames.push(state.slice());
  }
  return frames;
}

//: 被切掉的表面按深度染色：坑底偏冷偏暗，原始上表面是浅灰。
//: 几何上坑底和上表面都是水平面、受光相同，只靠明暗分不出来，所以用颜色补足可读性。
const CUT_TINT = { r: 0.42, g: 0.58, b: 0.72 };

function stockVertexColor(height, stockTop, floor, target) {
  if (!target) return 1;
  const span = Math.max(stockTop - floor, 1e-6);
  const depth = Math.min(Math.max((stockTop - height) / span, 0), 1);
  // 只给"切下去"的部分上色，未切削处保持原色。
  const wash = Math.min(depth * 2.6, 1);
  target[0] = 1 - wash * (1 - CUT_TINT.r);
  target[1] = 1 - wash * (1 - CUT_TINT.g);
  target[2] = 1 - wash * (1 - CUT_TINT.b);
  return target[0];
}

function frameIndexFor(times, time) {
  if (times.length < 2) return 0;
  let low = 0;
  let high = times.length - 1;
  while (low < high) {
    const middle = (low + high + 1) >> 1;
    if (times[middle] <= time) low = middle;
    else high = middle - 1;
  }
  return low;
}

//: 材料切除开启时，刀路线条调淡多少。曲面上那一层线如果保持全不透明，
//: 斜看就会糊成一片噪点，把刚削出来的形状盖掉。
const PATH_DIM_WITH_STOCK = 0.4;
const RAPID_DIM_WITH_STOCK = 0.45;

function lineOpacity(dashed, showStock) {
  if (!showStock) return 1;
  return dashed ? RAPID_DIM_WITH_STOCK : PATH_DIM_WITH_STOCK;
}

//: 表面相对播放时刻的超前量占帧间隔的比例。
//:
//: 表面按关键帧插值，因此最多落后刀具"一个帧间隔"的路程。这个滞后会让人看到怪现象：
//: 往复加工换行时刀具已经掉头，槽却还在朝原来的方向长——看起来"刀和切削方向相反"。
//: 把表面整体推进半个帧间隔，刀具就始终落在它刚切出的位置上。
const SURFACE_LEAD_RATIO = 0.5;

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

    // 半球光负责整体环境色，但强度要压低：它太强时坑的内壁和顶面亮度几乎一样，
    // 削出来的形状就没有立体感了。
    this.scene.add(new THREE.HemisphereLight(0xb9e9ff, 0x111513, 0.85));
    this.keyLight = new THREE.DirectionalLight(0xffffff, 2.4);
    this.keyLight.position.set(220, -320, 620);
    this.keyLight.castShadow = true;
    this.keyLight.shadow.mapSize.set(1024, 1024);
    this.keyLight.shadow.bias = -0.0005;
    this.keyLight.shadow.normalBias = 0.6;
    this.scene.add(this.keyLight);
    // 压低角度的侧光：专门照亮坑的竖直内壁，让"挖下去"这件事看得出来。
    this.sideLight = new THREE.DirectionalLight(0xffffff, 1.15);
    this.sideLight.position.set(-420, -180, 120);
    this.scene.add(this.sideLight);
    const rim = new THREE.DirectionalLight(0x52d8c5, 1.2);
    rim.position.set(-520, 420, 220);
    this.scene.add(rim);

    this.gridGroup = new THREE.Group();
    this.workpieceGroup = new THREE.Group();
    this.stockGroup = new THREE.Group();
    this.contourGroup = new THREE.Group();
    this.pathGroup = new THREE.Group();
    this.traceGroup = new THREE.Group();
    this.toolGroup = new THREE.Group();
    this.scene.add(
      this.gridGroup, this.workpieceGroup, this.stockGroup,
      this.contourGroup, this.pathGroup, this.traceGroup, this.toolGroup
    );

    this.tool = null;
    this.toolMesh = null;
    this.traceLine = null;
    this.rapidLine = null;
    this.lineMaterials = [];       //: 刀路线条材质，随"材料切除"开关调制浓淡
    this.stockSimulation = null;   //: 高度场仿真的载荷（含编码参数）
    this.stockFrames = [];         //: 解码后的逐帧顶面高度（量化整数）
    this.stockMesh = null;
    this.stockShell = null;        //: 侧壁 + 底面（不随削料变化）
    this.stockGeometry = null;
    this._stockFrameIndex = -1;

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

  // ------------------------------------------------------------ 材料切除
  // 把高度场画成一张网格：顶点的 Z 就是该处材料的顶面高度，随播放进度逐帧下降。
  _buildStock(stock) {
    this.stockSimulation = null;
    this.stockFrames = [];
    this.stockMesh = null;
    this.stockShell = null;
    this.stockGeometry = null;
    this._stockFrameIndex = -1;
    if (!stock || !stock.enabled || !stock.columns || !stock.rows) return;

    this.stockSimulation = stock;
    this.stockFrames = decodeStockFrames(stock);

    const rows = stock.rows;
    const columns = stock.columns;
    const geometry = new THREE.PlaneGeometry(
      stock.x_range_mm[1] - stock.x_range_mm[0],
      stock.y_range_mm[1] - stock.y_range_mm[0],
      columns - 1,
      rows - 1
    );
    const mesh = new THREE.Mesh(
      geometry,
      new THREE.MeshStandardMaterial({
        color: COLORS.stock, metalness: 0.12, roughness: 0.78,
        side: THREE.DoubleSide, vertexColors: true,
      })
    );
    // 顶点色由高度决定：切得越深越偏冷偏暗，用来补足"水平面之间没有明暗差"的问题。
    geometry.setAttribute(
      "color",
      new THREE.Float32BufferAttribute(new Float32Array(geometry.attributes.position.count * 3).fill(1), 3)
    );
    // 平面的行是从上往下排的；网格首行对应 y_min，所以对 y 翻转一次。
    mesh.scale.set(1, -1, 1);
    mesh.position.set(
      (stock.x_range_mm[0] + stock.x_range_mm[1]) / 2,
      (stock.y_range_mm[0] + stock.y_range_mm[1]) / 2,
      0
    );
    // 毛坯是主视觉，但自己给自己投阴影会在网格面上留下细密的条纹（shadow acne），
    // 而它只需要接收刀路与刀具的阴影。
    mesh.castShadow = false;
    mesh.receiveShadow = true;
    this.stockGroup.add(mesh);
    this.stockMesh = mesh;
    this.stockGeometry = geometry;
    this._buildStockShell(stock);
    this._updateStockSurface(stock.times[0]);
  }

  // 毛坯的外壳：四周侧壁 + 底面（**不含顶面**）。
  // 只画一张上表面是不够的——削掉材料之后，俯视会直接看到穿透过去的黑暗背景，
  // 看起来像"一张漂浮的平面"而不是"被挖掉一块的材料"。
  // 顶面必须留给高度场网格：外壳自己再铺一层顶面，就会重新把坑盖住。
  _buildStockShell(stock) {
    const [x0, x1] = stock.x_range_mm;
    const [y0, y1] = stock.y_range_mm;
    const top = stock.stock_top_mm;
    const floor = stock.floor_mm;

    const quad = (a, b, c, d) => [...a, ...b, ...c, ...a, ...c, ...d];
    const positions = [
      // 南墙 y = y0
      ...quad([x0, y0, floor], [x1, y0, floor], [x1, y0, top], [x0, y0, top]),
      // 北墙 y = y1
      ...quad([x1, y1, floor], [x0, y1, floor], [x0, y1, top], [x1, y1, top]),
      // 西墙 x = x0
      ...quad([x0, y1, floor], [x0, y0, floor], [x0, y0, top], [x0, y1, top]),
      // 东墙 x = x1
      ...quad([x1, y0, floor], [x1, y1, floor], [x1, y1, top], [x1, y0, top]),
      // 底面 z = floor
      ...quad([x0, y0, floor], [x1, y0, floor], [x1, y1, floor], [x0, y1, floor]),
    ];
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    geometry.computeVertexNormals();
    const shell = new THREE.Mesh(
      geometry,
      new THREE.MeshStandardMaterial({
        color: COLORS.stock, metalness: 0.12, roughness: 0.78,
        side: THREE.DoubleSide,
      })
    );
    shell.castShadow = false;
    shell.receiveShadow = true;
    this.stockGroup.add(shell);
    this.stockShell = shell;
  }

  // 每帧只改顶点高度；法线只在跨到下一个关键帧时重算一次，省掉大部分开销。
  _updateStockSurface(time) {
    const stock = this.stockSimulation;
    const geometry = this.stockGeometry;
    if (!stock || !geometry) return;
    const times = stock.times;
    const index = Math.min(frameIndexFor(times, time), this.stockFrames.length - 1);
    if (index < 0) return;
    const next = Math.min(index + 1, this.stockFrames.length - 1);
    const t0 = times[index];
    const t1 = times[next];
    const ratio = next === index || t1 <= t0 ? 0 : (time - t0) / (t1 - t0);
    const start = this.stockFrames[index];
    const end = this.stockFrames[next];
    const position = geometry.attributes.position;
    const array = position.array;
    const step = stock.encoding.step_mm;
    const offset = stock.encoding.offset_mm;
    const rows = stock.rows;
    const columns = stock.columns;
    // 每个顶点 3 个浮点数（x, y, z），所以一行的跨距是"列数 × 3"，
    // 而 z 在本行的偏移是"列号 × 3 + 2"。
    const rowStride = columns * 3;
    const color = geometry.attributes.color;
    const colors = color ? color.array : null;
    const top = stock.stock_top_mm;
    const floor = stock.floor_mm;
    const tint = [0, 0, 0];

    for (let row = 0; row < rows; row += 1) {
      const base = (rows - 1 - row) * rowStride;
      const source = row * columns;
      for (let column = 0; column < columns; column += 1) {
        const cell = source + column;
        const removed = start[cell] + ratio * (end[cell] - start[cell]);
        const height = removed * step + offset;
        array[base + column * 3 + 2] = height;
        if (colors) {
          stockVertexColor(height, top, floor, tint);
          const at = base + column * 3;
          colors[at] = tint[0];
          colors[at + 1] = tint[1];
          colors[at + 2] = tint[2];
        }
      }
    }
    position.needsUpdate = true;
    if (color) color.needsUpdate = true;
    if (index !== this._stockFrameIndex) {
      this._stockFrameIndex = index;
      geometry.computeVertexNormals();
    }
  }

  setSimulationTime(timeSeconds) {
    const stock = this.stockSimulation;
    let time = Math.max(0, timeSeconds);
    // 扣掉关键帧插值带来的固有滞后（见 SURFACE_LEAD_RATIO）。
    // 只能在"还没到终态"时提前，否则会把终态提前放出来。
    if (stock && stock.times.length > 1) {
      const last = stock.times[stock.times.length - 1];
      const interval = (last - stock.times[0]) / (stock.times.length - 1);
      time = Math.min(last, time + interval * SURFACE_LEAD_RATIO);
    }
    this._updateStockSurface(time);
  }

  // ---------------------------------------------------------------- 结果
  setResult(payload) {
    this._clear(this.workpieceGroup);
    this._clear(this.contourGroup);
    this._clear(this.pathGroup);
    this._clear(this.traceGroup);
    this._clear(this.stockGroup);
    // 旧材质的引用留在 lineMaterials 里会变成野指针，重建前先清掉。
    this.lineMaterials = [];

    const region = payload.region;
    const [xMin, xMax] = region.bounds_mm[0];
    const [yMin, yMax] = region.bounds_mm[1];
    const span = Math.max(xMax - xMin, yMax - yMin);

    const thickness = this._thickness(span);
    this.workpieceGroup.add(this._workpiece(region, thickness));
    this.contourGroup.add(this._contour(region.boundary));
    this._rebuildGrid(span, thickness);
    this._buildStock(payload.stock);

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
      const traceMaterial = new THREE.LineBasicMaterial({
        color: COLORS.trace, transparent: true,
        opacity: 0.95 * lineOpacity(false, this.display.showStock),
      });
      this.traceLine = new THREE.LineSegments(geometry, traceMaterial);
      this.traceLine.geometry.setDrawRange(0, 0);
      this.traceGroup.add(this.traceLine);
      this.lineMaterials.push({ material: traceMaterial, base: 0.95 });
    } else {
      this.traceLine = null;
    }

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
    const material = new THREE.MeshStandardMaterial({
      color: COLORS.tool, metalness: 0.5, roughness: 0.34,
    });

    // 切削段：圆柱按长度留出刀底所占的那一截，底部再补上真正的刀底形状。
    // 球头刀的刀底是半球，所以圆柱要从球心以上开始，否则刀会"长"出球面外。
    const isBall = tool.kind === "ball";
    const bottom = isBall ? radius : 0;          // 刀底占用的高度
    const body = Math.max(flute - bottom, radius * 0.5);
    const meshes = [];
    const cutting = new THREE.Mesh(
      new THREE.CylinderGeometry(radius, radius, body, 64), material
    );
    cutting.rotation.x = Math.PI / 2;
    cutting.position.z = bottom + body / 2;
    meshes.push(cutting);

    if (isBall) {
      // 半球：球心在 z = radius，最低点正好落在刀尖（z = 0）。
      const ball = new THREE.Mesh(
        new THREE.SphereGeometry(radius, 48, 24, 0, Math.PI * 2, Math.PI / 2, Math.PI / 2),
        material
      );
      ball.rotation.x = -Math.PI / 2;
      ball.position.z = radius;
      meshes.push(ball);
    } else {
      // 平底刀：封一层薄薄的底盖，让它看起来是实体而不是缺面的壳。
      const cap = new THREE.Mesh(
        new THREE.CylinderGeometry(radius, radius, Math.max(radius * 0.12, 0.2), 64), material
      );
      cap.rotation.x = Math.PI / 2;
      cap.position.z = Math.max(radius * 0.06, 0.1);
      meshes.push(cap);
    }

    const shank = new THREE.Mesh(
      new THREE.CylinderGeometry(radius * 1.25, radius * 1.25, holder, 48),
      new THREE.MeshStandardMaterial({
        color: COLORS.holder, metalness: 0.92, roughness: 0.24,
      })
    );
    shank.rotation.x = Math.PI / 2;
    shank.position.z = flute + holder / 2;
    meshes.push(shank);

    for (const mesh of meshes) {
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      this.toolGroup.add(mesh);
    }
    this.toolMesh = this.toolGroup;
    this.toolGroup.visible = this.display.showTool;
  }

  setPlayhead(position, traversedSegments) {
    this.toolGroup.position.set(position[0], position[1], position[2]);
    if (this.traceLine && traversedSegments !== this._lastTraversed) {
      this._lastTraversed = traversedSegments;
      this.traceLine.geometry.setDrawRange(0, Math.max(0, traversedSegments) * 2);
    }
  }

  setDisplayOptions(options) {
    this.display = Object.assign({}, this.display, options || {});
    this.stockGroup.visible = this.display.showStock && this.stockGroup.children.length > 0;
    // 毛坯显示时收起"工件参考块"：它顶面在加工面（z = 0），而坑底被切到加工面以下，
    // 于是它会像一块盖板盖在坑上，把削出来的形状全遮住。
    this.workpieceGroup.visible = this.display.showWorkpiece && !this.stockGroup.visible;
    this.contourGroup.visible = this.display.showWorkpiece && !this.stockGroup.visible;
    this.pathGroup.visible = this.display.showPath;
    this.traceGroup.visible = this.display.showPath && this.display.showTrace;
    this.toolGroup.visible = this.display.showTool;
    if (this.rapidLine) this.rapidLine.visible = this.display.showRapid;
    // 材料切除开关直接决定刀路线条的浓淡，所以每次都要跟着更新。
    const dim = this.display.showStock && this.stockGroup.children.length > 0;
    for (const entry of this.lineMaterials) {
      entry.material.opacity = entry.base * lineOpacity(
        entry.material.isLineDashedMaterial === true, dim
      );
    }
    for (const child of this.pathGroup.children) child.renderOrder = dim ? 0 : 2;
    if (this.traceLine) this.traceLine.renderOrder = dim ? 0 : 2;
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
    for (const group of [this.workpieceGroup, this.stockGroup, this.toolGroup]) {
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

  _workpiece(region, thickness) {
    const material = new THREE.MeshStandardMaterial({
      color: COLORS.workpiece, metalness: 0.65, roughness: 0.42,
    });
    if (region.id === "circle") {
      const radius = (region.bounds_mm[0][1] - region.bounds_mm[0][0]) / 2;
      const mesh = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, thickness, 128), material);
      mesh.rotation.x = Math.PI / 2;
      mesh.position.z = -thickness / 2;
      mesh.receiveShadow = true;
      return mesh;
    }
    const side = region.bounds_mm[0][1] - region.bounds_mm[0][0];
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(side, side, thickness), material);
    mesh.position.z = -thickness / 2;
    mesh.receiveShadow = true;
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
    const start = lineOpacity(dashed, this.display.showStock);
    let material;
    if (dashed) {
      material = new THREE.LineDashedMaterial({
        color, transparent: true, opacity: opacity * start, dashSize: 3, gapSize: 3,
      });
    } else {
      material = new THREE.LineBasicMaterial({
        color, transparent: true, opacity: opacity * start,
      });
    }
    const line = new THREE.LineSegments(geometry, material);
    if (dashed) line.computeLineDistances();
    // 刀路是"程序走在哪"的参考线。材料切除打开时把它画在曲面之后并调淡，
    // 否则一条条线会盖住刚削出来的形状，看上去像满屏噪点。
    line.renderOrder = this.display.showStock ? 0 : 2;
    this.lineMaterials.push({ material, base: opacity });
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
