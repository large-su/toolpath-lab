// CAM 参数面板：毛坯、加工参数、后处理、模板与显示开关。
//
// 与实验台面板一样，控件全部由后端的参数声明（/api/catalog 的 cam.parameters /
// stock.shapes）生成，因此后端加一个参数、加一种毛坯，界面刷新就会出现。
//
// 面板只负责"编辑 + 回调"，不发任何请求：所有落盘与重算都由 main.js 统一编排，
// 这样"改一个参数要做什么"只有一处逻辑（前端最容易失控的就是状态同步散落各处）。

import {
  buildControl,
  clone,
  defaultsOf,
  formatNumber,
  refreshVisibility,
  renderParameterRows,
} from "./controls.js";

const DISPLAY_OPTIONS = [
  { key: "showPart", label: "零件" },
  { key: "showStock", label: "毛坯" },
  { key: "showSimulation", label: "仿真余料", color: "var(--orange)" },
  { key: "showPath", label: "刀路" },
  { key: "showRapid", label: "快移", color: "var(--cyan)" },
  { key: "showTool", label: "刀具" },
];

export class CamPanel {
  constructor(options) {
    this.root = options.root;
    this.catalog = options.catalog || {};
    this.onChange = options.onChange || (() => {});
    this.onStockChange = options.onStockChange || (() => {});
    this.onStockCommit = options.onStockCommit || (() => {});
    this.onDisplayChange = options.onDisplayChange || (() => {});
    this.onTemplateSave = options.onTemplateSave || (() => {});
    this.onTemplateApply = options.onTemplateApply || (() => {});
    this.onParameterCommit = options.onParameterCommit || (() => {});
    this.onModeSelect = options.onModeSelect || (() => {});

    const camCatalog = this.catalog.cam || { operations: [], parameters: [], defaults: {} };
    const stockCatalog = this.catalog.stock || { shapes: [], default_id: "rectangular", defaults: {} };

    const initialKind = (camCatalog.operations[0] || {}).id || "pocket_mill";
    this.state = {
      stockId: stockCatalog.default_id || "rectangular",
      stockValues: clone(stockCatalog.defaults || {}),
      kind: initialKind,
      values: this._kindDefaults(initialKind),
      display: {
        showPart: true, showStock: true, showSimulation: true,
        showPath: true, showRapid: true, showTool: true,
      },
      controllerValues: clone(camCatalog.controller_defaults || {}),
      templates: [],
      operation: null,
    };
    this.rows = [];
    this.render();
  }

  // ------------------------------------------------------------ 对外接口
  /** 当前编辑中的工序（null 表示还没选中工序）。 */
  setOperation(operation) {
    this.state.operation = operation || null;
    if (operation) {
      this.state.kind = operation.kind;
      this.state.values = Object.assign(
        this._kindDefaults(operation.kind), clone(operation.parameters || {})
      );
    }
    this.render();
  }

  /** 后端的加工类型目录。 */
  _operations() {
    return (this.catalog.cam && this.catalog.cam.operations) || [];
  }

  operationEntry(kind = this.state.kind) {
    return this._operations().find((item) => item.id === kind) || null;
  }

  /** 这一种加工类型的参数声明（2.5 轴用全局那份，曲面用类型自己那份）。 */
  _kindSpecs(kind = this.state.kind) {
    const entry = this.operationEntry(kind);
    if (entry && entry.parameters) return entry.parameters;
    return (this.catalog.cam && this.catalog.cam.parameters) || [];
  }

  /**
   * 这一种加工类型的起始参数值。
   *
   * 曲面加工每种类型有自己的默认值，切类型时必须整份换掉：平行行切的"行距"
   * 跟着带到等高铣上毫无意义，还会让后端收到一堆用不上的键。
   * `implied` 是类型隐含、界面上不显示的参数（目前只有 strategy），
   * 写进参数字典是为了让声明里的 `visible_if` 仍然能正确联动。
   */
  _kindDefaults(kind) {
    const entry = this.operationEntry(kind);
    const values = entry && entry.defaults
      ? clone(entry.defaults)
      : clone((this.catalog.cam && this.catalog.cam.defaults) || {});
    if (entry && entry.implied) Object.assign(values, entry.implied);
    return values;
  }

  /** 当前类型是不是需要拾取加工面（清边铣按毛坯外框算，不需要）。 */
  needsFaces(kind = this.state.kind) {
    const entry = this.operationEntry(kind);
    if (!entry) return true;
    if (typeof entry.needs_faces === "boolean") return entry.needs_faces;
    return !entry.surface;
  }

  setTemplates(templates) {
    this.state.templates = templates || [];
    this.render();
  }

  setStock(stock) {
    if (!stock) return;
    if (stock.id) this.state.stockId = stock.id;
    this.state.stockValues = clone(stock.parameters || this.state.stockValues);
    this.render();
  }

  setController(values) {
    this.state.controllerValues = Object.assign(this.state.controllerValues, values || {});
    this.render();
  }

  /** 新建工序时用的参数（当前编辑中的值）。 */
  parameters() {
    return clone(this.state.values);
  }

  /** 当前毛坯设置。 */
  stockPayload() {
    return { shape: this.state.stockId, parameters: clone(this.state.stockValues) };
  }

  /** 后处理参数。 */
  controllerPayload() {
    return clone(this.state.controllerValues);
  }

  displayOptions() {
    return Object.assign({}, this.state.display);
  }

  /** 后端回写校验后的参数（例如 coerce 后的默认值）。 */
  syncValues(values) {
    this.state.values = Object.assign(clone(this.state.values), values || {});
    this.render();
  }

  // ------------------------------------------------------------ 渲染
  render() {
    this.rows = [];
    this.root.replaceChildren(
      this._operationSection(),
      this._stockSection(),
      this._parameterSection(),
      this._controllerSection(),
      this._templateSection(),
      this._displaySection()
    );
    this.refreshVisibility();
  }

  _section(title, hint) {
    const section = document.createElement("section");
    section.className = "section";
    const heading = document.createElement("h3");
    heading.textContent = title;
    if (hint) heading.title = hint;
    section.appendChild(heading);
    return section;
  }

  _operationSection() {
    const section = this._section("工序");
    const operations = this._operations();
    const current = operations.find((item) => item.id === this.state.kind) || operations[0];
    if (!current) {
      section.appendChild(this._note("后端没有发布任何加工类型"));
      return section;
    }
    const spec = {
      key: "__kind__",
      label: "加工类型",
      kind: "choice",
      choices: operations.map((item) => ({ value: item.id, label: item.label })),
      help: current.description,
    };
    const control = buildControl(spec, this.state.kind, (value) => {
      this.state.kind = value;
      // 换类型 = 换一整套参数：每种类型的参数含义不同，留着旧的只会误导用户
      this.state.values = this._kindDefaults(value);
      this.render();
      this.onChange(value, this.parameters());
    });
    section.appendChild(this._row(spec, control, null));

    const target = this.state.operation;
    const summary = document.createElement("div");
    summary.className = "note";
    if (target) {
      summary.textContent = `当前工序：${target.name} · ${target.kind_label} · ${target.state_label}`;
      if (target.faces && target.faces.length) {
        const faces = document.createElement("div");
        faces.textContent = "加工面：" + target.faces.map((id) => "#" + id).join("、");
        summary.appendChild(faces);
      }
    } else {
      summary.textContent = current.surface
        ? "尚未选中工序：曲面加工可以直接新增（不选面就是加工整个零件）。"
        : "尚未选中工序：先在下方选择加工面，再点顶部「新增工序」。";
    }
    section.appendChild(summary);
    return section;
  }

  _stockSection() {
    const section = this._section("毛坯");
    const shapes = (this.catalog.stock && this.catalog.stock.shapes) || [];
    if (!shapes.length) {
      section.appendChild(this._note("后端没有发布毛坯类型"));
      return section;
    }
    const current = shapes.find((item) => item.id === this.state.stockId) || shapes[0];
    const spec = {
      key: "__stock__",
      label: "类型",
      kind: "choice",
      choices: shapes.map((item) => ({ value: item.id, label: item.label })),
      help: current.description,
    };
    const control = buildControl(spec, current.id, (value) => {
      this.state.stockId = value;
      this.state.stockValues = defaultsOf(
        shapes.find((item) => item.id === value)
      );
      this.render();
      this.onStockChange(this.stockPayload());
    });
    section.appendChild(this._row(spec, control, null));

    for (const item of current.parameters || []) {
      const field = buildControl(item, this.state.stockValues[item.key], (value) => {
        this.state.stockValues[item.key] = value;
        // 拖动滑块时先预览，松手（change）再落盘
        this.onStockChange(this.stockPayload());
      });
      section.appendChild(this._row(item, field, this.state.stockValues));
    }

    const actions = document.createElement("div");
    actions.className = "row-actions";
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "button tiny";
    apply.textContent = "生成毛坯";
    apply.addEventListener("click", () => this.onStockCommit(this.stockPayload()));
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "button tiny";
    reset.textContent = "重置";
    reset.addEventListener("click", () => {
      this.state.stockValues = defaultsOf(current);
      this.render();
      this.onStockChange(this.stockPayload());
    });
    actions.append(apply, reset);
    section.appendChild(actions);
    return section;
  }

  _parameterSection() {
    const section = this._section("加工参数");
    const specs = this._kindSpecs();
    const groups = new Map();
    for (const spec of specs) {
      const group = spec.group || "常规";
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(spec);
    }
    for (const [group, groupSpecs] of groups) {
      const heading = document.createElement("h4");
      heading.className = "group-head";
      heading.textContent = group;
      section.appendChild(heading);
      const rows = renderParameterRows(section, groupSpecs, this.state.values, () => {
        this.onChange(this.state.kind, this.parameters());
      }, { onVisibility: () => this.refreshVisibility() });
      this.rows.push(...rows);
    }
    const entry = this.operationEntry();
    if (entry && entry.surface) {
      section.appendChild(this._note(
        "曲面加工的刀具类型由这里的「刀具类型」决定：球头刀与圆鼻刀在曲面刀路里"
        + "是真实支持的（2.5 轴工序仍只支持平底刀）。"
      ));
    }
    return section;
  }

  _controllerSection() {
    const section = this._section("后处理");
    const specs = (this.catalog.cam && this.catalog.cam.controller) || [];
    for (const spec of specs) {
      const field = buildControl(spec, this.state.controllerValues[spec.key], (value) => {
        this.state.controllerValues[spec.key] = value;
        this.onParameterCommit("controller", clone(this.state.controllerValues));
      });
      section.appendChild(this._row(spec, field, this.state.controllerValues));
    }
    return section;
  }

  _templateSection() {
    const section = this._section("参数模板");
    const list = document.createElement("div");
    list.className = "template-list";
    const applicable = this.state.templates.filter((item) => item.kind === this.state.kind);
    if (!applicable.length) {
      list.appendChild(this._note("当前加工类型还没有保存过模板"));
    }
    for (const template of applicable) {
      const row = document.createElement("div");
      row.className = "template-row";
      const apply = document.createElement("button");
      apply.type = "button";
      apply.className = "button tiny";
      apply.textContent = template.name;
      apply.title = "应用该模板的参数";
      apply.addEventListener("click", () => {
        this.state.values = Object.assign(this.state.values, template.parameters || {});
        this.render();
        this.onTemplateApply(template);
      });
      row.appendChild(apply);
      if (!template.builtin) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "button tiny danger";
        remove.textContent = "删除";
        remove.addEventListener("click", () => this.onTemplateSave("__delete__", template));
        row.appendChild(remove);
      }
      list.appendChild(row);
    }
    section.appendChild(list);

    const actions = document.createElement("div");
    actions.className = "row-actions";
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "模板名称";
    input.className = "text-input";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "button tiny";
    save.textContent = "保存当前参数";
    save.addEventListener("click", () => {
      const name = (input.value || "").trim();
      if (!name) return;
      this.onTemplateSave(name, {
        kind: this.state.kind,
        parameters: this.parameters(),
      });
      input.value = "";
    });
    actions.append(input, save);
    section.appendChild(actions);
    return section;
  }

  _displaySection() {
    const section = this._section("显示");
    for (const option of DISPLAY_OPTIONS) {
      const row = document.createElement("label");
      row.className = "checkbox-row";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = this.state.display[option.key];
      input.addEventListener("change", () => {
        this.state.display[option.key] = input.checked;
        this.onDisplayChange(this.displayOptions());
      });
      row.appendChild(input);
      if (option.color) {
        const dot = document.createElement("i");
        dot.className = "dot";
        dot.style.background = option.color;
        row.appendChild(dot);
      }
      const text = document.createElement("span");
      text.textContent = option.label;
      row.appendChild(text);
      section.appendChild(row);
    }
    return section;
  }

  // ------------------------------------------------------------ 小工具
  _row(spec, control, values) {
    const row = document.createElement("div");
    row.className = "row";
    row.dataset.key = spec.key;
    const label = document.createElement("label");
    label.textContent = spec.label;
    if (spec.help) label.title = spec.help;
    const cell = document.createElement("div");
    cell.className = "control";
    cell.appendChild(control.node);
    if (spec.unit) {
      const unit = document.createElement("span");
      unit.className = "unit";
      unit.textContent = spec.unit;
      cell.appendChild(unit);
    }
    row.append(label, cell);
    if (control.slider) row.appendChild(control.slider);
    if (values) this.rows.push({ row, spec, values });
    return row;
  }

  _note(text) {
    const note = document.createElement("div");
    note.className = "note";
    note.textContent = text;
    return note;
  }

  refreshVisibility() {
    refreshVisibility(this.rows, this.state.values);
  }
}

export { formatNumber };
