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
  model: 0x6d7f92,
  stock: 0x9fb4c4,
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
      showWorkpiece: true, showStock: true, showPath: true, showRapid: true,
      showTrace: true, showTool: true,
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
    this.modelGroup = new THREE.Group();
    this.stockGroup = new THREE.Group();
    this.contourGroup = new THREE.Group();
    this.pathGroup = new THREE.Group();
    this.traceGroup = new THREE.Group();
    this.toolGroup = new THREE.Group();
    this.scene.add(
      this.gridGroup, this.workpieceGroup, this.modelGroup, this.stockGroup,
      this.contourGroup, this.pathGroup, this.traceGroup, this.toolGroup
    );

    this.tool = null;
    this.toolMesh = null;
    this.traceLine = null;
    this.rapidLine = null;
    // 导入模型的三角网格（存在时替代参数化工件块）与它的包围盒信息。
    this.modelPayload = null;
    this.modelBottom = 0;
    // 毛坯（轴对齐长方体）是否已经画出来。
    this.hasStock = false;

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
    this._clear(this.contourGroup);
    this._clear(this.pathGroup);
    this._clear(this.traceGroup);

    const region = payload.region;
    const [xMin, xMax] = region.bounds_mm[0];
    const [yMin, yMax] = region.bounds_mm[1];
    const span = Math.max(xMax - xMin, yMax - yMin);

    // 有模型时，工件就是这个模型：不再画"按区域尺寸做出来的方块"，
    // 否则刀路看上去会悬在一个和零件无关的块料上。
    const useModel = Boolean(this.modelPayload);
    const thickness = this._thickness(span);
    if (!useModel) {
      this.workpieceGroup.add(this._workpiece(region, thickness));
    }
    this.contourGroup.add(this._contour(region.boundary));
    this.setStock(payload.stock);
    this._rebuildGrid(span, useModel ? -this.modelBottom : thickness);

    const groups = { cut: [], link: [], rapid: [] };
    for (const move of payload.toolpath.moves) {
      (groups[move.kind] || groups.cut).push(move.points);
    }
    // 曲面刀路本身带 Z，不能再统一压平，否则整条刀路会贴在 Z = 0 上。
    const planar = Boolean(payload.surface && payload.surface.is_planar);
    for (const kind of Object.keys(groups)) {
      groups[kind] = planar ? liftPaths(groups[kind]) : groups[kind];
    }
    this.pathGroup.add(this._line(groups.cut, COLORS.cut, 1));
    this.pathGroup.add(this._line(groups.link, COLORS.link, 1));
    this.rapidLine = this._line(groups.rapid, COLORS.rapid, 0.75, true);
    this.pathGroup.add(this.rapidLine);

    if (payload.timeline && payload.timeline.positions) {
      const trace = planar
        ? liftPaths([payload.timeline.positions])[0]
        : payload.timeline.positions;
      const geometry = polylineGeometry([trace]);
      this.traceLine = new THREE.LineSegments(
        geometry,
        new THREE.LineBasicMaterial({ color: COLORS.trace, transparent: true, opacity: 0.95 })
      );
      this.traceLine.geometry.setDrawRange(0, 0);
      this.traceGroup.add(this.traceLine);
    } else {
      this.traceLine = null;
    }

    this.bounds = new THREE.Box3().setFromObject(
      useModel ? this.modelGroup : this.workpieceGroup
    );
    const pathBounds = new THREE.Box3().setFromObject(this.pathGroup);
    if (!pathBounds.isEmpty()) this.bounds.union(pathBounds);
    if (this.hasStock) this.bounds.union(new THREE.Box3().setFromObject(this.stockGroup));
    // 让刀具的上半截也落在取景范围内（长度直接来自响应，不依赖调用顺序）。
    const toolLength = Number((payload.tool && payload.tool.length_mm) || 0);
    if (toolLength > 0) {
      this.bounds.expandByPoint(new THREE.Vector3(0, 0, toolLength * 0.5));
    }
    this._lastTraversed = -1;
    this.setDisplayOptions(this.display);
    this._autoFrame();
  }

  // ---------------------------------------------------------------- 毛坯
  /** 画（或清除）毛坯：一个半透明的轴对齐长方体 + 棱线。 */
  setStock(payload) {
    this._clear(this.stockGroup);
    this.hasStock = false;
    const bounds = payload && payload.is_set ? payload.bounds_mm : null;
    if (!bounds) {
      this.stockGroup.visible = false;
      return;
    }
    const [xMin, xMax] = bounds[0];
    const [yMin, yMax] = bounds[1];
    const [zMin, zMax] = bounds[2];
    const geometry = new THREE.BoxGeometry(
      Math.max(xMax - xMin, 1e-4), Math.max(yMax - yMin, 1e-4), Math.max(zMax - zMin, 1e-4)
    );
    const mesh = new THREE.Mesh(
      geometry,
      new THREE.MeshStandardMaterial({
        color: COLORS.stock, transparent: true, opacity: 0.1, metalness: 0.2,
        roughness: 0.8, depthWrite: false, side: THREE.DoubleSide,
      })
    );
    mesh.position.set((xMin + xMax) / 2, (yMin + yMax) / 2, (zMin + zMax) / 2);
    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(geometry),
      new THREE.LineBasicMaterial({ color: COLORS.stock, transparent: true, opacity: 0.5 })
    );
    edges.position.copy(mesh.position);
    this.stockGroup.add(mesh, edges);
    this.hasStock = true;
    this.stockGroup.visible = this.display.showStock;
  }

  // ---------------------------------------------------------------- 模型
  /** 显示（或清除）导入模型：payload 来自 GET /api/models/{id}。 */
  setModel(payload) {
    this._clear(this.modelGroup);
    this.modelPayload = payload && payload.positions && payload.positions.length
      ? payload
      : null;
    if (!this.modelPayload) {
      this.modelGroup.visible = false;
      this.modelBottom = 0;
      return;
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute(
      "position", new THREE.Float32BufferAttribute(this.modelPayload.positions, 3)
    );
    // 三角汤（非索引）算出来的就是逐面法线，正是我们要的"机加工表面"观感。
    geometry.computeVertexNormals();
    const mesh = new THREE.Mesh(
      geometry,
      new THREE.MeshStandardMaterial({
        color: COLORS.model, metalness: 0.62, roughness: 0.44, side: THREE.DoubleSide,
      })
    );
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    this.modelGroup.add(mesh);
    this.modelGroup.visible = true;
    // GridHelper.position.z 用的是"网格铺在哪一层"。
    geometry.computeBoundingBox();
    this.modelBottom = geometry.boundingBox.min.z;
    this._autoFrame();
  }

  // 只在"工件尺寸变了"或第一次出结果时重新取景：
  // 调一个切宽就把视角拉回默认，是很烦人的体验。
  _autoFrame() {
    if (!this.bounds) return;
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
    // 刀具的形状由后端给出（tool.segments：每段一条半剖回转轮廓），
    // 这里只负责旋成实体并配色——平底刀是一根带底的圆柱，
    // 球头刀是"半球 + 圆柱"，两者共用 z = R 处的端面。
    // 所以以后再加刀具类型（比如圆鼻刀）只需要改 Python 里的几何。
    for (const segment of tool.segments || []) {
      const points = (segment.profile_mm || [])
        .map(([radius, height]) => new THREE.Vector2(Math.max(radius, 0), height));
      if (points.length < 2) continue;
      const isShank = segment.name === "shank";
      const mesh = new THREE.Mesh(
        new THREE.LatheGeometry(points, 96),
        new THREE.MeshStandardMaterial(isShank
          ? { color: COLORS.holder, metalness: 0.92, roughness: 0.24 }
          : { color: COLORS.tool, metalness: 0.5, roughness: 0.34 })
      );
      // LatheGeometry 绕 Y 轴回转，转到 Z 轴向上后刀尖正好落在 (0, 0, 0)。
      mesh.rotation.x = Math.PI / 2;
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
    this.workpieceGroup.visible = this.display.showWorkpiece;
    this.modelGroup.visible = this.display.showWorkpiece && Boolean(this.modelPayload);
    this.stockGroup.visible = this.display.showStock && this.hasStock;
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
    for (const group of [this.workpieceGroup, this.modelGroup, this.toolGroup]) {
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
