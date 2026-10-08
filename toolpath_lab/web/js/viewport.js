// three.js viewport: workpiece, region contour, toolpath, cutter and playback indicator.
//
// Interaction and look conventions:
//   left drag orbits / middle wheel zooms / right drag pans (and suppresses the context menu),
//   the standard view toolbar centred at the top (fit, front, back, left, right, top, bottom),
//   the appearance toggles at the top left (live shadows, white background, grid floor),
//   a "studio" look with dark background, fog, environment reflections and shadows.

import * as THREE from "three";
import { OrbitControls } from "../vendor/OrbitControls.js";
import { RoomEnvironment } from "../vendor/RoomEnvironment.js";

const COLORS = {
  background: 0x071014,
  white: 0xffffff,
  workpiece: 0x5b6b7e,
  contour: 0x54d6c4,
  cut: 0xffa726,
  cutSlow: 0xff5c33,
  link: 0xf2c94c,
  rapid: 0x4fc3f7,
  trace: 0x54d6c4,
  tool: 0xffcc00,
  holder: 0xb0bcc6,
  uncut: 0xff8a80,
};

// Toolpaths on the machining plane (cut / link / trace) are lifted slightly to avoid z-fighting.
// Rapids are not lifted: they keep their real Z, so a retract to the safe height stays visible.
const PATH_LIFT_MM = 0.05;

// Buttons of the view toolbar, in the order they are used most.
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

// Machining-plane polylines are lifted slightly to avoid z-fighting with the top face;
// points already above PATH_LIFT_MM (retracts) keep their real height.
// Points below the machining plane (a step-down layer) keep their real depth as well: they sit
// inside the workpiece, which setResult renders translucent so they stay visible.
// Exported like uncutGeometry so the Z rule can be checked in Node without a browser.
export function liftPath(points) {
  return points.map((point) => [
    point[0],
    point[1],
    point[2] < 0 ? point[2] : Math.max(point[2], PATH_LIFT_MM),
  ]);
}

function liftPaths(polylines) {
  return polylines.map(liftPath);
}

// Uncut area overlay: two triangles per rectangle, sitting just above the machining plane
// (below the toolpath lift of 0.05, so it never covers the toolpath).
// Exported so this pure function can be checked in Node with the bundled three.js, no browser needed.
// Profile of a cutting head as [radius, height] pairs, for a lathe revolve: flat bottom out to
// R - Rc, then the corner radius arc up to the full radius R at height Rc, then the flank -- straight
// for a cylindrical tool, opening by tan(taper) of radius per millimetre for a tapered one.
// One formula covers all three kinds -- flat mills (Rc = 0), ball nose (Rc = R) and bull nose --
// exactly like Tool.wall_clearance_mm does in the backend. Exported so it can be checked in Node.
export function toolProfile(radiusMm, cornerRadiusMm, headHeightMm, segments = 16, taperAngleDeg = 0) {
  const radius = Math.max(radiusMm, 1e-6);
  const corner = Math.min(Math.max(cornerRadiusMm, 0), radius);
  const head = Math.max(headHeightMm, corner + 1e-6);
  const flat = radius - corner;
  const slope = Math.tan((Math.max(taperAngleDeg, 0) * Math.PI) / 180);
  const flank = (heightMm) => radius + Math.max(heightMm - corner, 0) * slope;
  const points = [[0, 0]];
  if (flat > 1e-6) {
    points.push([flat, 0]);
  }
  if (corner > 1e-6) {
    for (let step = 1; step <= segments; step += 1) {
      const angle = (Math.PI / 2) * (step / segments);
      points.push([flat + corner * Math.sin(angle), corner * (1 - Math.cos(angle))]);
    }
  }
  points.push([flank(head), head]);
  points.push([0, head]);
  return points;
}

// Cutting moves do not all run at the same feed once corner slowdown (or any strategy that varies
// the feed) is on: the fastest cutting feed in the toolpath is the programmed one, anything below it
// is a slowed stretch. Split them so the slow bits can be drawn in their own colour; exported for the
// same reason as liftPath, so it can be checked in Node without a browser.
export function splitCutByFeed(moves) {
  const cut = moves.filter((move) => move.kind === "cut");
  const fastest = cut.reduce((max, move) => Math.max(max, move.feed_mm_per_min), 0);
  const fast = [];
  const slow = [];
  for (const move of cut) {
    (move.feed_mm_per_min < fastest - 1e-9 ? slow : fast).push(move.points);
  }
  return { fast, slow };
}

export function uncutGeometry(rects) {
  const positions = [];
  const z = PATH_LIFT_MM * 0.4;
  for (const [x0, y0, x1, y1] of rects) {
    positions.push(x0, y0, z, x1, y0, z, x1, y1, z);
    positions.push(x0, y0, z, x1, y1, z, x0, y1, z);
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  return geometry;
}

// Depth ramp of the height map: the top face (nothing was removed) is the cool end and the deepest
// floor the warm end, so the terraces of a stepped plan read as a gradient. The stops are built once
// because a plan can carry a few thousand cells. Exported so the mapping can be checked in Node.
const DEPTH_STOPS = [0x2f4d5c, 0x54d6c4, 0xffa726].map((hex) => new THREE.Color(hex));

export function depthColor(zMm, floorMm, target = new THREE.Color()) {
  // `floorMm` is the deepest Z reached (negative) and z is the material height of one cell, so
  // z / floorMm runs from 0 at the top face to 1 at the floor.
  const floor = Math.min(floorMm, 0);
  const ratio = floor < -1e-6 ? Math.min(Math.max(zMm / floor, 0), 1) : 0;
  const scaled = ratio * (DEPTH_STOPS.length - 1);
  const index = Math.min(Math.floor(scaled), DEPTH_STOPS.length - 2);
  return target.copy(DEPTH_STOPS[index]).lerp(DEPTH_STOPS[index + 1], scaled - index);
}

// Height map overlay: one quad per grid cell of the (already reduced) 3D height map, each at the Z of
// the deepest cut recorded for that cell, so a stepped plan shows its terraces at their real depth.
// Cells that are null lie outside the region and are skipped, which is what keeps the overlay off the
// bounding box corners of a circle. Exported for the same Node check as uncutGeometry.
export function heightMapGeometry(map, liftMm = PATH_LIFT_MM * 0.2) {
  const positions = [];
  const colors = [];
  const [stepX, stepY] = map.cell_size_mm;
  const [originX, originY] = map.origin_mm;
  const color = new THREE.Color();
  map.cells.forEach((row, rowIndex) => {
    row.forEach((z, columnIndex) => {
      if (z === null || z === undefined) return;
      const left = originX + columnIndex * stepX;
      const bottom = originY + rowIndex * stepY;
      const right = left + stepX;
      const top = bottom + stepY;
      const height = z + liftMm;
      positions.push(left, bottom, height, right, bottom, height, right, top, height);
      positions.push(left, bottom, height, right, top, height, left, top, height);
      depthColor(z, map.floor_mm, color);
      for (let corner = 0; corner < 6; corner += 1) {
        colors.push(color.r, color.g, color.b);
      }
    });
  });
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
  return geometry;
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
      showUncut: true, showHeight: true,
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
    this.uncutGroup = new THREE.Group();
    this.heightGroup = new THREE.Group();
    this.contourGroup = new THREE.Group();
    this.pathGroup = new THREE.Group();
    this.traceGroup = new THREE.Group();
    this.toolGroup = new THREE.Group();
    this.scene.add(
      this.gridGroup, this.workpieceGroup, this.uncutGroup, this.heightGroup,
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

  // ------------------------------------------------------------ lifecycle
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

  // ---------------------------------------------------------------- result
  setResult(payload) {
    this._clear(this.workpieceGroup);
    this._clear(this.uncutGroup);
    this._clear(this.heightGroup);
    this._clear(this.contourGroup);
    this._clear(this.pathGroup);
    this._clear(this.traceGroup);

    const region = payload.region;
    const [xMin, xMax] = region.bounds_mm[0];
    const [yMin, yMax] = region.bounds_mm[1];
    const span = Math.max(xMax - xMin, yMax - yMin);

    // Deepest point of the toolpath: 0 for a single layer, negative once step-down is switched on.
    let lowestZ = 0;
    for (const move of payload.toolpath.moves) {
      for (const point of move.points) {
        if (point[2] < lowestZ) {
          lowestZ = point[2];
        }
      }
    }

    const thickness = this._thickness(span, lowestZ);
    // A layered toolpath cuts below the top face, so the workpiece is drawn translucent: otherwise
    // the deeper layers would be hidden inside an opaque solid.
    const layered = lowestZ < -1e-6;
    this.workpieceGroup.add(this._workpiece(region, thickness, layered));
    this.contourGroup.add(this._contour(region.boundary));
    this._rebuildGrid(span, thickness);

    const rects = (payload.coverage && payload.coverage.uncut_rects) || [];
    if (rects.length) {
      const mesh = new THREE.Mesh(
        uncutGeometry(rects),
        new THREE.MeshBasicMaterial({
          color: COLORS.uncut, transparent: true, opacity: 0.32,
          depthWrite: false, side: THREE.DoubleSide,
        })
      );
      this.uncutGroup.add(mesh);
    }

    // The machined floor, coloured by how deep the tool reached each cell. The quads sit at the Z of
    // that cut, so the terraces of a stepped plan are visible through the translucent workpiece (which
    // is exactly the case this overlay exists for: a single layer has no depth to show and the
    // backend sends no map at all). Basic material on purpose: this is a data overlay, not another
    // solid for the lights and shadows to work on.
    const heightMap = payload.removal && payload.removal.height_map;
    if (heightMap && heightMap.cells && heightMap.floor_mm < -1e-6) {
      this.heightGroup.add(
        new THREE.Mesh(
          heightMapGeometry(heightMap),
          new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide })
        )
      );
    }

    const groups = { link: [], rapid: [] };
    for (const move of payload.toolpath.moves) {
      if (move.kind === "cut") {
        continue;
      }
      (groups[move.kind] || groups.link).push(move.points);
    }
    const { fast, slow } = splitCutByFeed(payload.toolpath.moves);
    this.pathGroup.add(this._line(liftPaths(fast), COLORS.cut, 1));
    // Slowed stretches (corner slowdown) in their own colour, a touch brighter to stand out.
    if (slow.length) {
      this.pathGroup.add(this._line(liftPaths(slow), COLORS.cutSlow, 1.2));
    }
    this.pathGroup.add(this._line(liftPaths(groups.link), COLORS.link, 1));
    // Rapids use their real Z: retract and plunge are vertical, traverses sit at the safe height.
    this.rapidLine = this._line(groups.rapid, COLORS.rapid, 0.75, true);
    this.pathGroup.add(this.rapidLine);

    if (payload.timeline && payload.timeline.positions) {
      // The trace matches the toolpath: lifted on the machining plane, real height on retracts.
      const geometry = polylineGeometry([liftPath(payload.timeline.positions)]);
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
    // Keep the upper half of the cutter inside the framing too (its length comes from the response).
    const toolLength = Number((payload.tool && payload.tool.length_mm) || 0);
    if (toolLength > 0) {
      this.bounds.expandByPoint(new THREE.Vector3(0, 0, toolLength * 0.5));
    }
    this._lastTraversed = -1;
    this.setDisplayOptions(this.display);
    this._autoFrame();
  }

  // Re-frame only when the workpiece size changed or on the first result:
  // a changed stepover pulling the camera back to default is a very annoying experience.
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
    // Flute and shank come from the backend, so the drawn tool is exactly the geometry the collision
    // check measured; the fallbacks keep an older or partial response drawable.
    const flute = Math.min(
      tool.flute_mm > 0 ? tool.flute_mm : Math.min(length * 0.65, radius * 6), length
    );
    const shankRadius = tool.shank_radius_mm > 0 ? tool.shank_radius_mm : radius * 1.25;
    // The shank fills the tool between the flutes and its end; a tool that is all flutes has none.
    const holder = Math.max(length - flute, 0);

    // The cutting head is the profile of the real tool, revolved: a ball nose is drawn round and a
    // bull nose really has its corner radius, so the cutter in the view is the tool the plan was
    // computed for. DoubleSide keeps it solid whichever way the revolve winds.
    const head = toolProfile(radius, tool.corner_radius_mm || 0, flute, 16,
                             tool.taper_angle_deg || 0).map(
      ([r, h]) => new THREE.Vector2(r, h)
    );
    const cutting = new THREE.Mesh(
      new THREE.LatheGeometry(head, 64),
      new THREE.MeshStandardMaterial({
        color: COLORS.tool, metalness: 0.5, roughness: 0.34, side: THREE.DoubleSide,
      })
    );
    cutting.rotation.x = Math.PI / 2; // LatheGeometry revolves around +Y, the app is Z-up
    const meshes = [cutting];
    if (holder > 1e-6) {
      const shank = new THREE.Mesh(
        new THREE.CylinderGeometry(shankRadius, shankRadius, holder, 48),
        new THREE.MeshStandardMaterial({
          color: COLORS.holder, metalness: 0.92, roughness: 0.24,
        })
      );
      shank.rotation.x = Math.PI / 2;
      shank.position.z = flute + holder / 2;
      meshes.push(shank);
    }
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
    this.workpieceGroup.visible = this.display.showWorkpiece;
    this.uncutGroup.visible = this.display.showUncut;
    this.heightGroup.visible = this.display.showHeight;
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

  // ---------------------------------------------------------------- view
  applyView(view) {
    if (!this.bounds || this.bounds.isEmpty()) return;
    const { direction, up } = orientation(view);
    const center = this.bounds.getCenter(new THREE.Vector3());
    const radius = Math.max(this.bounds.getBoundingSphere(new THREE.Sphere()).radius, 1);
    const right = new THREE.Vector3().crossVectors(up, direction).normalize();
    const vertical = new THREE.Vector3().crossVectors(direction, right).normalize();
    const verticalLimit = Math.tan(THREE.MathUtils.degToRad(this.camera.getEffectiveFOV() / 2)) * 0.82;
    const horizontalLimit = verticalLimit * this.camera.aspect;

    // Put all eight corners of the bounding box in the frustum: each has its own depth, take the farthest.
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

  // -------------------------------------------------------------- geometry
  _thickness(span, lowestZ = 0) {
    // The blank also has to be thick enough for the deepest layer to stay inside the solid.
    return Math.max(Math.min(Math.max(span * 0.09, 4), 24), -lowestZ + 2);
  }

  _workpiece(region, thickness, translucent = false) {
    // The workpiece is extruded from the region boundary polygon, so a new shape needs no change here.
    // ExtrudeGeometry extrudes along +Z; translating it by the thickness puts the top face at Z = 0.
    const shape = new THREE.Shape(
      region.boundary.map((point) => new THREE.Vector2(point[0], point[1]))
    );
    const geometry = new THREE.ExtrudeGeometry(shape, {
      depth: thickness,
      bevelEnabled: false,
    });
    geometry.translate(0, 0, -thickness);
    const mesh = new THREE.Mesh(
      geometry,
      new THREE.MeshStandardMaterial({
        color: COLORS.workpiece, metalness: 0.65, roughness: 0.42,
        transparent: translucent, opacity: translucent ? 0.3 : 1,
      })
    );
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
    // The grid is the "floor": it lies on the bottom face of the workpiece, not through it.
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
