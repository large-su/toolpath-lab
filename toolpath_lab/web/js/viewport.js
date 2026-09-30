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
// 注意是"抬高"而不是"抹平"：斜面/曲面上的刀点本身带 Z，必须保留，否则刀路会横在基准平面上。
export function liftPaths(polylines) {
  return polylines.map((points) =>
    points.map((point) => [point[0], point[1], (point[2] || 0) + PATH_LIFT_MM])
  );
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

// 加工面不是平面时的工件实体：顶面按后端给的分片扇形三角化，侧壁把 XY 轮廓挤出到底面，
// 底面封口。分片都由区域保证是共面的凸多边形，所以扇形三角化足够。
export function buildSurfaceGeometry(region, thickness) {
  const outline = region.boundary || [];
  const patches = region.top_patches || [];
  // 工件挂在加工面最低处之下：斜面是低边（0），水平面区域就是它的高度。
  const bottomZ = Number(((region.surface || {}).base_z_mm) || 0) - thickness;
  const positions = [];
  const triangle = (a, b, c) => positions.push(...a, ...b, ...c);

  for (const patch of patches) {
    for (let index = 1; index + 1 < patch.length; index += 1) {
      triangle(patch[0], patch[index], patch[index + 1]); // 俯视逆时针 → 法向朝上
    }
  }
  for (let index = 0; index < outline.length; index += 1) {
    const top = outline[index];
    const next = outline[(index + 1) % outline.length];
    const topFoot = [top[0], top[1], bottomZ];
    const nextFoot = [next[0], next[1], bottomZ];
    triangle(top, nextFoot, next); // 侧壁：外向法向
    triangle(top, topFoot, nextFoot);
  }
  const floor = outline.map((point) => [point[0], point[1], bottomZ]);
  for (let index = 1; index + 1 < floor.length; index += 1) {
    triangle(floor[0], floor[index + 1], floor[index]); // 底面：法向朝下
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.computeVertexNormals();
  return geometry;
}

// 刀具实体：平底刀是一段圆柱；球头刀是"球的下半部分 + 同半径圆柱刃部"；圆鼻刀是
// "平底 + 圆环过渡 + 圆柱刃部"。返回若干 mesh，局部坐标里**刀尖在 Z = 0、刀轴沿 +Z**，
// 因此挂到刀具组后刀尖会跟着播放头走（Z = 0 就是加工面）。
export function buildToolParts(tool) {
  const radius = Math.max(tool.radius_mm, 0.2);
  const length = tool.length_mm;
  const isBall = tool.kind === "ball";
  const corner = Math.min(Math.max(Number(tool.corner_radius_mm) || 0, 0), radius);
  // 圆鼻刀用回转母线画（平底 + 圆角过渡）；Rc 小到 0 时就退化成平底刀。
  const isBull = tool.kind === "bull" && corner > 0.01;
  // 刀头本身要占掉一段高度（球头一个半径、圆鼻一个圆角），刃部至少要留出它。
  const tipHeight = isBall ? radius : isBull ? corner : 0;
  const flute = Math.max(Math.min(length * 0.65, radius * 6), tipHeight);
  const holder = Math.max(length - flute, length * 0.2);

  const cuttingMaterial = new THREE.MeshStandardMaterial({
    color: COLORS.tool, metalness: 0.5, roughness: 0.34,
  });
  const parts = [];

  if (isBull) {
    // 圆鼻刀的轮廓是一条回转母线：从轴心沿平底走到内切圆，再以 Rc 为半径转 90° 接上圆柱。
    // LatheGeometry 的 profile 用 (半径, 高度) 并绕 Y 轴回转，所以画完再绕 X 转 90° 对齐 Z 轴。
    const flatRadius = Math.max(radius - corner, 0);
    const profile = [];
    if (flatRadius > 1e-6) profile.push(new THREE.Vector2(0, 0));
    const arcSegments = 16;
    for (let step = 0; step <= arcSegments; step += 1) {
      const angle = -Math.PI / 2 + (Math.PI / 2) * (step / arcSegments);
      profile.push(
        new THREE.Vector2(
          flatRadius + corner * Math.cos(angle),
          corner + corner * Math.sin(angle)
        )
      );
    }
    profile.push(new THREE.Vector2(radius, flute));
    const body = new THREE.Mesh(new THREE.LatheGeometry(profile, 64), cuttingMaterial);
    body.rotation.x = Math.PI / 2;
    parts.push(body);
  } else if (isBall) {
    // 刀头是球的下半部分：极点朝下、球赤道朝上。先把"上半球"绕 X 反转 90°（极点落到
    // Z = -R、赤道落到 Z = 0），再整体抬高一个半径，于是球心在 Z = R、刀尖（极点）正好
    // 落在加工面 Z = 0 上，赤道圆在 Z = R 与刃部相切。
    const head = new THREE.Mesh(
      new THREE.SphereGeometry(radius, 64, 32, 0, Math.PI * 2, 0, Math.PI / 2),
      cuttingMaterial
    );
    head.rotation.x = -Math.PI / 2;
    head.position.z = radius;
    parts.push(head);
    const body = new THREE.Mesh(
      new THREE.CylinderGeometry(radius, radius, flute - radius, 48), cuttingMaterial
    );
    body.rotation.x = Math.PI / 2;
    body.position.z = radius + (flute - radius) / 2;
    parts.push(body);
  } else {
    const body = new THREE.Mesh(
      new THREE.CylinderGeometry(radius, radius, flute, 64), cuttingMaterial
    );
    body.rotation.x = Math.PI / 2;
    body.position.z = flute / 2;
    parts.push(body);
  }

  const shank = new THREE.Mesh(
    new THREE.CylinderGeometry(radius * 1.25, radius * 1.25, holder, 48),
    new THREE.MeshStandardMaterial({
      color: COLORS.holder, metalness: 0.92, roughness: 0.24,
    })
  );
  shank.rotation.x = Math.PI / 2;
  shank.position.z = flute + holder / 2;
  parts.push(shank);
  return parts;
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
    this.scene.add(
      this.gridGroup, this.workpieceGroup,
      this.contourGroup, this.pathGroup, this.traceGroup, this.toolGroup
    );

    this.tool = null;
    this.toolMesh = null;
    this.traceLine = null;
    this.rapidLine = null;

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

    // 部件厚度来自区域参数（加工面以下那块基体有多厚）；载荷没带这个字段时按跨度估算。
    const thickness = Number(region.thickness_mm) > 0
      ? Number(region.thickness_mm)
      : this._thickness(span);
    // 加工面的最低 Z：水平面区域就是它的高度（默认 0），斜面是低边（0）。
    const surfaceZ = Number(((region.surface || {}).base_z_mm) || 0);
    this.workpieceGroup.add(this._workpiece(region, thickness, surfaceZ));
    // 轮廓画的是"刀路覆盖的范围"：斜坡只加工斜面段时它比工件轮廓窄。
    this.contourGroup.add(this._contour(region.machining_boundary || region.boundary));
    this._rebuildGrid(span, thickness, surfaceZ);

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
    // 球头刀的半球底面贴着加工面、圆鼻刀回转母线的顶部开口被刀柄盖住，所以都看不到缺口。
    for (const mesh of buildToolParts(tool)) {
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

  _workpiece(region, thickness, surfaceZ) {
    const material = new THREE.MeshStandardMaterial({
      color: COLORS.workpiece, metalness: 0.65, roughness: 0.42,
    });
    if (region.surface && region.surface.kind !== "flat" && (region.top_patches || []).length) {
      // 斜面这类加工面：顶面由后端给的分片决定，前端只负责三角化与挤出。
      const mesh = new THREE.Mesh(buildSurfaceGeometry(region, thickness), material);
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      return mesh;
    }
    // 水平面区域：实体顶面落在加工面高度上（surfaceZ），基体挂在它下面。
    if (region.id === "circle") {
      const radius = (region.bounds_mm[0][1] - region.bounds_mm[0][0]) / 2;
      const mesh = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, thickness, 128), material);
      mesh.rotation.x = Math.PI / 2;
      mesh.position.z = surfaceZ - thickness / 2;
      mesh.receiveShadow = true;
      return mesh;
    }
    const side = region.bounds_mm[0][1] - region.bounds_mm[0][0];
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(side, side, thickness), material);
    mesh.position.z = surfaceZ - thickness / 2;
    mesh.receiveShadow = true;
    return mesh;
  }

  _contour(boundary) {
    // 轮廓跟着加工面走：斜面区域的 boundary 自带 Z。
    const points = boundary.map(
      (point) => new THREE.Vector3(point[0], point[1], (point[2] || 0) + PATH_LIFT_MM * 2)
    );
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

  _rebuildGrid(span, thickness, surfaceZ = 0) {
    this._clear(this.gridGroup);
    const size = Math.max(Math.ceil((span * 3) / 20) * 20, 100);
    const grid = new THREE.GridHelper(size, Math.max(4, Math.round(size / 10)), 0x2d6c69, 0x173331);
    grid.rotation.x = Math.PI / 2;
    // 网格是"地面"：铺在基准面 Z = 0 与工件底面里更低的那一处——抬高的工件因此明显悬在它之上，
    // 而默认（顶面在 Z = 0）时它仍旧贴着工件底面、不穿过工件。
    grid.position.z = Math.min(surfaceZ - thickness, 0) - 0.1;
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
