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
  contour: 0x54d6c4,
  cut: 0xffa726,
  link: 0xf2c94c,
  rapid: 0x4fc3f7,
  trace: 0x54d6c4,
  tool: 0xffcc00,
  holder: 0xb0bcc6,
  // CAM：零件、毛坯、拾取高亮、仿真后毛坯
  part: 0x8fa6b8,
  partSelected: 0xffa726,
  stock: 0x4a6270,
  stockCut: 0xb5894a,
  edge: 0x243642,
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
      // showStock / showSimulation 由工具栏按钮控制；仿真时毛坯默认让位给 simulationGroup
      showStock: true, showSimulation: true,
    };
    // 当前仿真显示状态（几何只建一次，帧是 height 快照；见 _buildSimulationGeometry）
    this._sim = null;
    this.bounds = null;
    this.activeView = "fit";
    this._lastTraversed = -1;
    //: "bench" | "cam"：决定哪一组内容可见（见 _applyVisibility）
    this.sceneMode = "bench";

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
    this.contourGroup = new THREE.Group();
    this.pathGroup = new THREE.Group();
    this.traceGroup = new THREE.Group();
    this.toolGroup = new THREE.Group();
    // CAM 用的组：零件、毛坯、仿真后的毛坯、拾取高亮
    this.partGroup = new THREE.Group();
    this.stockGroup = new THREE.Group();
    this.simulationGroup = new THREE.Group();
    this.pickGroup = new THREE.Group();
    this.scene.add(
      this.gridGroup, this.workpieceGroup,
      this.contourGroup, this.pathGroup, this.traceGroup, this.toolGroup,
      this.partGroup, this.stockGroup, this.simulationGroup, this.pickGroup
    );

    this.tool = null;
    this.toolMesh = null;
    this.traceLine = null;
    this.rapidLine = null;
    // CAM 状态
    this.partData = null;          // 导入的零件（{ positions, indices, faces, ... }）
    this.faceMeshes = [];          // 每个面一个 mesh，用于拾取与高亮
    this.selectedFaces = new Set();
    this.pickable = false;
    this.onFacePick = null;        // (faceId, { additive }) => void
    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    this._pickHandlers = null;

    this.resize();
    if (typeof ResizeObserver !== "undefined") {
      this.observer = new ResizeObserver(() => this.resize());
      this.observer.observe(container);
    }
  }

  // ------------------------------------------------------------ 生命周期
  /**
   * 按容器尺寸调整画布。
   *
   * 这里有两条必须守住的规则，否则会出现"画布无限长高 → 相机被推到天边 → 模型看不见"：
   *   1. `setSize` 要**更新 CSS**（第三个参数留默认的 true）。以前传 false 再自己写
   *      `height:100%`，一旦父容器高度是 auto，百分比解析成 auto，画布就退回按自身的
   *      height 属性排版；而本函数又用 clientHeight 去设那个属性 —— 每触发一次放大一倍。
   *   2. 尺寸没变就直接返回。ResizeObserver 会因为"自己刚造成的布局变化"再次回调，
   *      不拦住就是死循环（控制台那条 ResizeObserver loop 警告）。
   */
  resize() {
    const width = this.container.clientWidth;
    const height = this.container.clientHeight;
    if (!width || !height) return;
    if (width === this._canvasWidth && height === this._canvasHeight) return;
    this._canvasWidth = width;
    this._canvasHeight = height;
    this.renderer.setSize(width, height);
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
    if (!this._usableBox(this.bounds)) return;
    const diagonal = this.bounds.getSize(new THREE.Vector3()).length();
    if (!Number.isFinite(diagonal) || diagonal <= 0) return;
    const changed = !this.fittedDiagonal
      || Math.abs(diagonal - this.fittedDiagonal) / this.fittedDiagonal > 0.12;
    if (changed) {
      this.applyView("fit");
      this.fittedDiagonal = diagonal;
    }
  }

  resetView() {
    this.applyView("fit");
    if (this._usableBox(this.bounds)) {
      this.fittedDiagonal = this.bounds.getSize(new THREE.Vector3()).length();
    }
  }

  /**
   * 包围盒是否可用：非空、且六个坐标全是有限数。
   *
   * 这一步是必需的防线：Box3 一旦被并进一个 NaN 顶点（典型来源是几何算错时产生的
   * 非有限坐标），`isEmpty()` 会返回 false（NaN 的比较恒为 false），于是相机被摆到
   * NaN 位置，用户看到的现象就是"模型不见了"或者"只看得见一角"。
   */
  _usableBox(box) {
    if (!box || box.isEmpty()) return false;
    const { min, max } = box;
    return [min.x, min.y, min.z, max.x, max.y, max.z].every(Number.isFinite);
  }

  setTool(tool) {
    this._clear(this.toolGroup);
    this.tool = tool || null;
    this.toolMesh = null;
    // 传 null 就是"撤掉刀具显示"（刀具库关闭预览、清空刀路时都走这里），
    // 早期版本直接读 tool.radius_mm，所以调用方必须自己判空——现在不用了。
    if (!tool) return;
    const radius = Math.max(tool.radius_mm, 0.2);
    const length = tool.length_mm;
    const flute = Math.min(length * 0.65, radius * 6);
    const holder = Math.max(length - flute, length * 0.2);

    // 两段都用封闭圆柱（端面带封口），所以刀具是实体而不是缺面的壳；
    // 黄色切削段对齐 UGNX 的刀具配色。
    const cutting = new THREE.Mesh(
      new THREE.CylinderGeometry(radius, radius, flute, 64),
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
    cutting.rotation.x = Math.PI / 2;
    cutting.position.z = flute / 2;
    shank.rotation.x = Math.PI / 2;
    shank.position.z = flute + holder / 2;

    // 球头刀的刀尖是个半球。曲面加工里球头刀是真实可用的（opencamlib 支持），
    // 画成平底会在对刀位置上看走眼，所以这里按刀型补出来。
    if (tool.kind === "ball") {
      const tip = new THREE.Mesh(
        new THREE.SphereGeometry(radius, 48, 24, 0, Math.PI * 2, Math.PI / 2, Math.PI / 2),
        new THREE.MeshStandardMaterial({
          color: COLORS.tool, metalness: 0.5, roughness: 0.34,
        })
      );
      tip.castShadow = true;
      tip.receiveShadow = true;
      this.toolGroup.add(tip);
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
    this._applyVisibility();
  }

  /**
   * 切换场景模式：实验台（bench）与 CAM 加工（cam）的内容必须严格互斥。
   *
   * 以前可见性只在 setMode() 里设一次，而启动时"恢复工程"会再调 setPart / setStockMesh，
   * 它们各自把分组重新点亮——结果导入的零件与毛坯压在实验台的规则工件上，
   * 相机也被拉去框住零件+毛坯，用户看到的就是"实验台的基础模型只剩一角"。
   * 现在所有分组可见性都由这里统一算，谁调 setPart 都破坏不了互斥。
   */
  setSceneMode(mode) {
    this.sceneMode = mode === "cam" ? "cam" : "bench";
    this._applyVisibility();
    if (this.sceneMode === "cam") this._updateBounds();
  }

  /** 按"当前场景模式 + 显示开关 + 是否有仿真网格"算出每个分组的可见性。 */
  _applyVisibility() {
    const isCam = this.sceneMode === "cam";
    const display = this.display || {};
    this.workpieceGroup.visible = !isCam && display.showWorkpiece !== false;
    this.contourGroup.visible = !isCam && display.showWorkpiece !== false;
    this.pathGroup.visible = display.showPath !== false;
    this.traceGroup.visible = display.showPath !== false && display.showTrace !== false;
    this.toolGroup.visible = display.showTool !== false;
    if (this.rapidLine) this.rapidLine.visible = display.showRapid !== false;
    this.partGroup.visible = isCam && display.showPart !== false;
    // 仿真一旦出网格就代替原始毛坯，避免两块料重叠
    const simulating = this.simulationGroup.children.length > 0;
    this.simulationGroup.visible = isCam && simulating && display.showSimulation !== false;
    this.stockGroup.visible = isCam && !simulating && display.showStock !== false;
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
    if (!this._usableBox(this.bounds)) return;
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

  // ============================================================== CAM 零件
  // setPart 之后视口进入"加工模式"：显示零件与毛坯，并可以拾取面。
  // 与实验台（区域 + 栅格刀路）互不干扰，两套内容各用一组 Group。

  /** 载入导入的零件：按面拆成独立 mesh，因此每一面都能单独高亮与拾取。 */
  setPart(payload, { frame = true } = {}) {
    this._clear(this.partGroup);
    this._clear(this.pickGroup);
    this.faceMeshes = [];
    this.selectedFaces = new Set();
    this.partData = payload;
    if (!payload || !payload.mesh) return;

    const mesh = payload.mesh;
    const positions = new Float32Array(mesh.positions.flat());
    const indices = mesh.indices;
    const faceOfTriangle = mesh.face_of_triangle || [];
    const faceMeta = new Map((mesh.faces || []).map((face) => [face.id, face]));

    // 按面收集三角形下标（载荷里已按面排列，这里只做分组）
    const groups = new Map();
    for (let triangle = 0; triangle < faceOfTriangle.length; triangle += 1) {
      const faceId = faceOfTriangle[triangle];
      if (!groups.has(faceId)) groups.set(faceId, []);
      groups.get(faceId).push(triangle);
    }

    const material = new THREE.MeshStandardMaterial({
      color: COLORS.part, metalness: 0.55, roughness: 0.45,
      flatShading: false, side: THREE.DoubleSide,
    });
    for (const [faceId, triangles] of groups) {
      const localIndices = [];
      for (const triangle of triangles) {
        localIndices.push(indices[triangle * 3], indices[triangle * 3 + 1], indices[triangle * 3 + 2]);
      }
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      geometry.setIndex(localIndices);
      geometry.computeVertexNormals();
      const faceMesh = new THREE.Mesh(geometry, material.clone());
      faceMesh.castShadow = true;
      faceMesh.receiveShadow = true;
      faceMesh.userData.faceId = faceId;
      faceMesh.userData.meta = faceMeta.get(faceId) || null;
      this.faceMeshes.push(faceMesh);
      this.partGroup.add(faceMesh);
    }
    // 边线：让模型轮廓更清楚，也更容易看清被选中的面。
    // 注意**不能**把无索引几何交给 EdgesGeometry：它靠索引三角形工作，无索引时会产出
    // NaN 顶点，而一个 NaN 顶点就会让整场景的包围盒变成 NaN，相机随即失焦
    //（表现就是"模型不见了"或"只看得见一角"）。这里直接按网格索引提取唯一的边。
    const edges = new THREE.LineSegments(
      new THREE.BufferGeometry().setAttribute(
        "position", new THREE.BufferAttribute(this._uniqueEdgePositions(positions, indices), 3)
      ),
      new THREE.LineBasicMaterial({ color: COLORS.edge, transparent: true, opacity: 0.55 })
    );
    this.partGroup.add(edges);

    this._applyVisibility();
    this._updateBounds();
    if (frame) this._autoFrame();
  }

  /**
   * 由（可能不共享顶点的）三角网格提取**唯一的边**，返回线段的顶点坐标数组。
   *
   * 为什么要自己算而不用 EdgesGeometry：导入的网格是先焊接顶点再离散的，索引里同一个
   * 顶点被多个三角形共用，EdgesGeometry 可以直接用；但对无索引几何它会产出 NaN。
   * 这里按"量化后的顶点对"去重，同一位置的顶点只保留一条边，线框因此更干净。
   */
  _uniqueEdgePositions(positions, indices) {
    const seen = new Set();
    const output = [];
    const key = (index) => {
      const x = Math.round(positions[index * 3] * 1000);
      const y = Math.round(positions[index * 3 + 1] * 1000);
      const z = Math.round(positions[index * 3 + 2] * 1000);
      return `${x},${y},${z}`;
    };
    const push = (a, b) => {
      const keyA = key(a);
      const keyB = key(b);
      const marker = keyA < keyB ? `${keyA}|${keyB}` : `${keyB}|${keyA}`;
      if (seen.has(marker)) return;
      seen.add(marker);
      output.push(
        positions[a * 3], positions[a * 3 + 1], positions[a * 3 + 2],
        positions[b * 3], positions[b * 3 + 1], positions[b * 3 + 2]
      );
    };
    const vertexCount = positions.length / 3;
    for (let index = 0; index + 2 < indices.length; index += 3) {
      const a = indices[index];
      const b = indices[index + 1];
      const c = indices[index + 2];
      if (a >= vertexCount || b >= vertexCount || c >= vertexCount) continue;
      push(a, b);
      push(b, c);
      push(c, a);
    }
    // 兜底：没有索引（或全被过滤）时至少给出所有顶点的连续折线
    if (output.length === 0) {
      for (let index = 0; index < vertexCount; index += 1) {
        output.push(positions[index * 3], positions[index * 3 + 1], positions[index * 3 + 2]);
      }
    }
    return new Float32Array(output);
  }

  setSelectedFaces(ids) {
    this.selectedFaces = new Set((ids || []).map((item) => Number(item)));
    for (const mesh of this.faceMeshes) {
      const selected = this.selectedFaces.has(Number(mesh.userData.faceId));
      mesh.material.color.setHex(selected ? COLORS.partSelected : COLORS.part);
      mesh.material.emissive.setHex(selected ? 0x3a2300 : 0x000000);
    }
  }

  setPickable(enabled) {
    if (enabled === this.pickable) return;
    this.pickable = enabled;
    if (enabled) {
      this._pickHandlers = {
        pointerdown: (event) => this._onPointerDown(event),
      };
      this.renderer.domElement.addEventListener("pointerdown", this._pickHandlers.pointerdown);
      this.renderer.domElement.style.cursor = "crosshair";
    } else {
      if (this._pickHandlers) {
        this.renderer.domElement.removeEventListener("pointerdown", this._pickHandlers.pointerdown);
      }
      this._pickHandlers = null;
      this.renderer.domElement.style.cursor = "";
    }
  }

  _onPointerDown(event) {
    if (!this.pickable) return;
    // 左键拖动是旋转视角，因此只在"几乎没有移动"的点击上拾取
    const { clientX, clientY } = event;
    const element = this.renderer.domElement;
    const rect = element.getBoundingClientRect();
    this.pointer.set(
      ((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1
    );
    const start = { x: clientX, y: clientY };
    const finish = (upEvent) => {
      element.removeEventListener("pointerup", finish);
      const moved = Math.hypot(upEvent.clientX - start.x, upEvent.clientY - start.y);
      if (moved > 4) return;
      if (event.button !== 0) return;
      this.raycaster.setFromCamera(this.pointer, this.camera);
      const hits = this.raycaster.intersectObjects(this.faceMeshes, false);
      if (!hits.length) return;
      const faceId = hits[0].object.userData.faceId;
      if (this.onFacePick) this.onFacePick(faceId, { additive: upEvent.shiftKey });
    };
    element.addEventListener("pointerup", finish);
  }

  // ============================================================== CAM 毛坯
  /** 载入毛坯网格（矩形块预览或圆柱预览）。 */
  setStockMesh(payload) {
    this._clear(this.stockGroup);
    if (!payload || !payload.positions) return;
    const mesh = this._meshFromPayload(payload, COLORS.stock, 0.35);
    mesh.userData.isStock = true;
    this.stockGroup.add(mesh);
    this._applyVisibility();
    // 毛坯换了尺寸就要重算取景范围：否则 bounds 还停在上一个毛坯上，
    // 导入一个比原来小的零件时相机会被按旧尺寸拉开，模型看着又小又偏。
    this._updateBounds();
  }

  /**
   * 仿真结果的显示载体：**单个 mesh**，几何拓扑只建一次。
   *
   * 旧实现为每一帧预建一个完整 BufferGeometry（180 帧 × 30~50ms ≈ 5~9s 主线程
   * 阻塞 + 几百 MB 显存），帧切换只切 visible。现在顶点的 x/y 与三角形索引都与
   * 高度无关（高度只写进 position 的 z 分量），所以：
   *
   * - 预计算 = 建一次几何 + 记下"每个顶点的 z 来自哪个格子"；
   * - 换锚帧 = 从帧快照重写全部 z（几万格的线性写入，1~2ms）；
   * - 帧间播放 = ``sweepSimulation`` 按刀路段局部压低 z（材料被刀连续扫走），
   *   动画步长因此与仿真帧数解耦——帧再少也不卡。
   *
   * 顶面顶点的法线用高度场的解析梯度（中心差分）；侧壁顶点不共享顶面顶点、
   * 且墙面垂直，法线恒为朝外的水平方向，一次算好不再动。
   */
  _buildSimulationGeometry(grid, height) {
    const { rows, cols, x0, y0, cell_mm: cell, bottom_z: bottom, active } = grid || {};
    if (!rows || !cols || !height || height.length < rows * cols) return null;
    const isActive = (i, j) => !active || active[i * cols + j] !== false;
    const hAt = (i, j) => height[i * cols + j];
    const topNormal = (i, j) => {
      const im = Math.max(i - 1, 0), ip = Math.min(i + 1, rows - 1);
      const jm = Math.max(j - 1, 0), jp = Math.min(j + 1, cols - 1);
      const dhx = (hAt(ip, j) - hAt(im, j)) / ((ip - im) * cell);
      const dhy = (hAt(i, jp) - hAt(i, jm)) / ((jp - jm) * cell);
      const len = Math.sqrt(dhx * dhx + dhy * dhy + 1);
      return [-dhx / len, -dhy / len, 1 / len];
    };

    const index = new Int32Array(rows * cols).fill(-1);
    const cellTop = new Int32Array(rows * cols).fill(-1);
    const cellVerts = new Array(rows * cols);
    const vertices = [];
    const normals = [];
    // 每个顶点的 z 动态来源：-1 = 固定底面；>=0 = 该格高度（2 还要 max 底面，
    // 用于"切穿的格子"——墙退化成零高度，不会翻到料下面去）。
    const zCell = [];
    const zKind = [];
    const push = (x, y, z, nx, ny, nz, owner, kind) => {
      vertices.push(x, y, z);
      normals.push(nx, ny, nz);
      zCell.push(owner);
      zKind.push(kind);
      return vertices.length / 3 - 1;
    };

    // 顶面（格中心一个顶点）
    for (let i = 0; i < rows; i += 1) {
      for (let j = 0; j < cols; j += 1) {
        if (!isActive(i, j)) continue;
        const cellIndex = i * cols + j;
        const normal = topNormal(i, j);
        index[cellIndex] = push(x0 + (i + 0.5) * cell, y0 + (j + 0.5) * cell,
                                hAt(i, j), normal[0], normal[1], normal[2], cellIndex, 1);
        cellTop[cellIndex] = index[cellIndex];
        cellVerts[cellIndex] = [index[cellIndex]];
      }
    }

    const indices = [];
    for (let i = 0; i < rows - 1; i += 1) {
      for (let j = 0; j < cols - 1; j += 1) {
        const a = index[i * cols + j];
        const b = index[(i + 1) * cols + j];
        const c = index[(i + 1) * cols + j + 1];
        const d = index[i * cols + j + 1];
        if (a < 0 || b < 0 || c < 0 || d < 0) continue;
        indices.push(a, b, c, a, c, d);
      }
    }

    // 侧壁：与旧实现同一布局（每面墙独立的四个角点），法线朝外的水平方向。
    // 角点逆时针绕行（左下→右下→右上→左上），因此 side 0..3 依次是 -Y、+X、+Y、-X
    // 四条边——邻居方向必须跟着转，否则边界格会把墙画到隔壁那条边上（错位 90°）。
    const neighbours = [[0, -1], [1, 0], [0, 1], [-1, 0]];
    const wallNormals = [[0, -1], [1, 0], [0, 1], [-1, 0]];
    for (let i = 0; i < rows; i += 1) {
      for (let j = 0; j < cols; j += 1) {
        if (!isActive(i, j)) continue;
        const top = index[i * cols + j];
        if (top < 0) continue;
        const x = x0 + (i + 0.5) * cell;
        const y = y0 + (j + 0.5) * cell;
        const z = hAt(i, j);
        const half = cell * 0.5;
        const corners = [
          [x - half, y - half], [x + half, y - half], [x + half, y + half], [x - half, y + half],
        ];
        const cellIndex = i * cols + j;
        for (let side = 0; side < 4; side += 1) {
          const ni = i + neighbours[side][0];
          const nj = j + neighbours[side][1];
          const inside = ni >= 0 && nj >= 0 && ni < rows && nj < cols && isActive(ni, nj);
          if (inside) continue;
          const [ax, ay] = corners[side];
          const [bx, by] = corners[(side + 1) % 4];
          const [nx, ny] = wallNormals[side];
          const base = vertices.length / 3;
          const topA = push(ax, ay, z, nx, ny, 0, cellIndex, 2);
          const topB = push(bx, by, z, nx, ny, 0, cellIndex, 2);
          push(ax, ay, bottom, nx, ny, 0, -1, 0);
          push(bx, by, bottom, nx, ny, 0, -1, 0);
          cellVerts[cellIndex].push(topA, topB);
          indices.push(base, base + 1, base + 2, base, base + 2, base + 3);
        }
      }
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(vertices, 3));
    geometry.setAttribute("normal", new THREE.Float32BufferAttribute(normals, 3));
    geometry.setIndex(indices);
    return {
      geometry,
      topCell: cellTop,
      cellVerts,
      zCell: Int32Array.from(zCell),
      zKind: Uint8Array.from(zKind),
    };
  }

  /** 把显示状态（height 数组）整体写进几何 z 并重算顶面法线。 */
  _writeSimulationZ() {
    const sim = this._sim;
    if (!sim) return;
    const position = sim.geometry.attributes.position.array;
    const normal = sim.geometry.attributes.normal.array;
    const { rows, cols, cell_mm: cell, bottom_z: bottom } = sim.grid;
    const height = sim.height;
    for (let v = 0; v < sim.zCell.length; v += 1) {
      const owner = sim.zCell[v];
      if (owner < 0) continue;
      const h = height[owner];
      position[v * 3 + 2] = sim.zKind[v] === 2 ? Math.max(h, bottom) : h;
    }
    for (let i = 0; i < rows; i += 1) {
      for (let j = 0; j < cols; j += 1) {
        const v = sim.topCell[i * cols + j];
        if (v < 0) continue;
        const im = Math.max(i - 1, 0), ip = Math.min(i + 1, rows - 1);
        const jm = Math.max(j - 1, 0), jp = Math.min(j + 1, cols - 1);
        const dhx = (height[ip * cols + j] - height[im * cols + j]) / ((ip - im) * cell);
        const dhy = (height[i * cols + jp] - height[i * cols + jm]) / ((jp - jm) * cell);
        const len = Math.sqrt(dhx * dhx + dhy * dhy + 1);
        normal[v * 3] = -dhx / len;
        normal[v * 3 + 1] = -dhy / len;
        normal[v * 3 + 2] = 1 / len;
      }
    }
    sim.geometry.attributes.position.needsUpdate = true;
    sim.geometry.attributes.normal.needsUpdate = true;
  }

  /** 只更新被格子集合触碰的顶点 z（±1 邻域的顶面法线）。 */
  _touchSimulationCells(cells) {
    const sim = this._sim;
    if (!sim) return;
    const { rows, cols, cell_mm: cell, bottom_z: bottom } = sim.grid;
    const height = sim.height;
    const position = sim.geometry.attributes.position.array;
    const normal = sim.geometry.attributes.normal.array;
    const normalCells = new Set();
    for (const cellIndex of cells) {
      const h = height[cellIndex];
      for (const v of sim.cellVerts[cellIndex]) {
        position[v * 3 + 2] = sim.zKind[v] === 2 ? Math.max(h, bottom) : h;
      }
      const i = Math.floor(cellIndex / cols), j = cellIndex % cols;
      for (let di = -1; di <= 1; di += 1) {
        for (let dj = -1; dj <= 1; dj += 1) {
          const ni = i + di, nj = j + dj;
          if (ni < 0 || nj < 0 || ni >= rows || nj >= cols) continue;
          const v = sim.topCell[ni * cols + nj];
          if (v >= 0) normalCells.add(ni * cols + nj);
        }
      }
    }
    for (const cellIndex of normalCells) {
      const i = Math.floor(cellIndex / cols), j = cellIndex % cols;
      const v = sim.topCell[cellIndex];
      const im = Math.max(i - 1, 0), ip = Math.min(i + 1, rows - 1);
      const jm = Math.max(j - 1, 0), jp = Math.min(j + 1, cols - 1);
      const dhx = (height[ip * cols + j] - height[im * cols + j]) / ((ip - im) * cell);
      const dhy = (height[i * cols + jp] - height[i * cols + jm]) / ((jp - jm) * cell);
      const len = Math.sqrt(dhx * dhx + dhy * dhy + 1);
      normal[v * 3] = -dhx / len;
      normal[v * 3 + 1] = -dhy / len;
      normal[v * 3 + 2] = 1 / len;
    }
    sim.geometry.attributes.position.needsUpdate = true;
    sim.geometry.attributes.normal.needsUpdate = true;
  }

  /**
   * 帧间连续切削：把刀在 ``[上一位置, 当前位置]`` 扫过的材料压低。
   *
   * segments = [[ax, ay, az, bx, by, bz, rapid], …]，公式与后端
   * ``cut_sim._cut_segment`` 逐字一致（盘内参数区间两端 Z 取最小、容差 0.02），
   * 因此扫掠到锚帧边界时与下一帧快照严丝合缝，切换无跳变。
   * 只有被扫到的格子会被写（bbox 内的几十~几千格），单帧成本微秒级。
   */
  sweepSimulation(segments, radius) {
    const sim = this._sim;
    if (!sim || !segments || !segments.length) return;
    const { rows, cols, x0, y0, cell_mm: cell, active } = sim.grid;
    const height = sim.height;
    const tolerance = 0.02;
    const radiusSquared = radius * radius;
    const touched = new Set();
    for (const [ax, ay, az, bx, by, bz, rapid] of segments) {
      if (rapid) continue;
      const dx = bx - ax, dy = by - ay, dz = bz - az;
      const aSquared = dx * dx + dy * dy;
      const i0 = Math.max(0, Math.floor((Math.min(ax, bx) - radius - x0) / cell));
      const i1 = Math.min(rows - 1, Math.ceil((Math.max(ax, bx) + radius - x0) / cell));
      const j0 = Math.max(0, Math.floor((Math.min(ay, by) - radius - y0) / cell));
      const j1 = Math.min(cols - 1, Math.ceil((Math.max(ay, by) + radius - y0) / cell));
      for (let i = i0; i <= i1; i += 1) {
        const gx = x0 + (i + 0.5) * cell;
        for (let j = j0; j <= j1; j += 1) {
          const cellIndex = i * cols + j;
          if (active && active[cellIndex] === false) continue;
          const gy = y0 + (j + 0.5) * cell;
          const px = gx - ax, py = gy - ay;
          let floorZ;
          if (aSquared <= 1e-12) {
            if (px * px + py * py > radiusSquared) continue;
            floorZ = Math.min(az, bz);
          } else {
            const bCoef = -2 * (px * dx + py * dy);
            const cCoef = px * px + py * py;
            const discriminant = bCoef * bCoef - 4 * aSquared * (cCoef - radiusSquared);
            if (discriminant < 0) continue;
            const root = Math.sqrt(discriminant);
            const tEnter = (-bCoef - root) / (2 * aSquared);
            const tExit = (-bCoef + root) / (2 * aSquared);
            if (tEnter > 1 || tExit < 0) continue;
            const clamp = (t) => Math.min(Math.max(t, 0), 1);
            floorZ = Math.min(az + dz * clamp(tEnter), az + dz * clamp(tExit));
          }
          floorZ -= tolerance;
          if (height[cellIndex] > floorZ) {
            height[cellIndex] = floorZ;
            touched.add(cellIndex);
          }
        }
      }
    }
    if (touched.size) this._touchSimulationCells(touched);
  }

  /** 仿真结果：显示某一个高度状态（单帧版，冒烟自检与外部脚本用）。 */
  setSimulationMesh(payload, height) {
    this._clear(this.simulationGroup);
    this._sim = null;
    if (!payload || !height) return;
    const built = this._buildSimulationGeometry(payload, height);
    if (!built) return;
    const mesh = new THREE.Mesh(built.geometry, this._simulationMaterial());
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    this.simulationGroup.add(mesh);
    this._sim = Object.assign({
      grid: payload,
      height: Float64Array.from(height),
      mesh,
    }, built);
    this._applyVisibility();
    this._updateBounds();
  }

  _simulationMaterial() {
    return new THREE.MeshStandardMaterial({
      color: COLORS.stockCut, metalness: 0.35, roughness: 0.7,
      side: THREE.DoubleSide, flatShading: false,
    });
  }

  /**
   * 载入仿真帧：几何只建一次，``frames`` 只是 height 快照表。
   *
   * 旧实现逐帧预建 mesh（见方法开头的说明）；现在换帧走 ``setSimulationFrame``
   * 的全量 z 重写（1~2ms），帧间播放走 ``sweepSimulation`` 局部扫掠。
   */
  precomputeSimulationFrames(grid, frames) {
    this._clear(this.simulationGroup);
    this._sim = null;
    if (!grid || !frames || frames.length === 0) {
      this._applyVisibility();
      return;
    }
    const first = frames[0].height;
    const built = this._buildSimulationGeometry(grid, first);
    if (!built) {
      this._applyVisibility();
      return;
    }
    const mesh = new THREE.Mesh(built.geometry, this._simulationMaterial());
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    this.simulationGroup.add(mesh);
    this._sim = Object.assign({
      grid,
      frames,
      frameIndex: 0,
      height: Float64Array.from(first),
      mesh,
    }, built);
    this._applyVisibility();
    this._updateBounds();
  }

  /**
   * 重置到锚帧 ``index``：从帧快照恢复整张显示状态。
   * 前一锚帧上"预扫"过的内容在这里被干净覆盖，所以任意拖动都从快照重扫。
   */
  setSimulationFrame(index) {
    const sim = this._sim;
    if (!sim || !sim.frames || sim.frames.length === 0) return;
    const clamped = Math.max(0, Math.min(index | 0, sim.frames.length - 1));
    const source = sim.frames[clamped].height;
    if (source) sim.height.set(source);
    sim.frameIndex = clamped;
    this._writeSimulationZ();
  }

  clearSimulation() {
    this._clear(this.simulationGroup);
    this._sim = null;
    this._applyVisibility();
    this._updateBounds();
  }

  /** 把服务端 mesh payload（positions/indices）变成三角网格。 */
  _meshFromPayload(payload, color, metalness) {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute(
      "position", new THREE.Float32BufferAttribute(Float32Array.from(payload.positions.flat()), 3)
    );
    geometry.setIndex(payload.indices);
    geometry.computeVertexNormals();
    const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({
      color, metalness, roughness: 0.45, transparent: true, opacity: 0.85,
      side: THREE.DoubleSide,
    }));
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(geometry, 24),
      new THREE.LineBasicMaterial({ color: COLORS.contour, transparent: true, opacity: 0.5 })
    );
    const group = new THREE.Group();
    group.add(mesh, edges);
    return group;
  }

  /** 在 CAM 模式下把取景范围限制在零件/毛坯/刀路之内。 */
  _updateBounds() {
    // 实验台有自己的取景（setResult 里直接算），这里绝不能改它的 bounds
    if (this.sceneMode !== "cam") return;
    const box = new THREE.Box3();
    for (const group of [this.partGroup, this.stockGroup, this.simulationGroup, this.pathGroup]) {
      if (!group.visible) continue;
      const groupBox = new THREE.Box3().setFromObject(group);
      if (this._usableBox(groupBox)) box.union(groupBox);
    }
    if (!this._usableBox(box)) return;
    this.bounds = box;
    const toolLength = Number((this.tool && this.tool.length_mm) || 0);
    if (toolLength > 0) this.bounds.expandByPoint(new THREE.Vector3(0, 0, toolLength * 0.5));
  }

  /** CAM 显示开关：零件 / 毛坯 / 仿真 / 刀路 / 刀具。 */
  setCamDisplay(options) {
    this.display = Object.assign({}, this.display, options || {});
    this._applyVisibility();
  }

  /** 只画刀路（CAM 模式：没有规则区域，只有导入零件 + 刀路）。 */
  setPathOnly(toolpath, { includeRapid = true } = {}) {
    this._clear(this.pathGroup);
    this._clear(this.traceGroup);
    if (!toolpath || !toolpath.moves) return;
    const groups = { cut: [], link: [], rapid: [] };
    for (const move of toolpath.moves) {
      (groups[move.kind] || groups.cut).push(move.points);
    }
    // 刀路不再抬到 Z=0 上表面：CAM 的刀路本来就在三维空间里
    this.pathGroup.add(this._line(groups.cut, COLORS.cut, 1));
    this.pathGroup.add(this._line(groups.link, COLORS.link, 1));
    this.rapidLine = this._line(groups.rapid, COLORS.rapid, 0.7, true);
    this.rapidLine.visible = includeRapid && this.display.showRapid !== false;
    this.pathGroup.add(this.rapidLine);
    this.pathGroup.visible = this.display.showPath !== false;
    this._updateBounds();
  }

  /** 刀路整体清空（CAM 模式切换工序时用）。 */
  clearToolpath() {
    this._clear(this.pathGroup);
    this._clear(this.traceGroup);
    this._clear(this.toolGroup);
    this.toolMesh = null;
    this.traceLine = null;
    this.rapidLine = null;
  }
}
