// 参数面板完全由 /api/catalog 生成：每个能力在 Python 里声明一次参数，
// 这里负责把它们渲染出来。新增一个区域、曲面或策略，刷新页面就会出现，不用改本文件。

const DISPLAY_OPTIONS = [
  { key: "showWorkpiece", label: "工件" },
  { key: "showPath", label: "刀路", color: "var(--orange)" },
  { key: "showRapid", label: "快移", color: "var(--cyan)" },
  { key: "showTrace", label: "已走轨迹", color: "var(--teal)" },
  { key: "showTool", label: "刀具" },
];

const SIMULATION_OPTIONS = [
  { key: "showStock", label: "材料切除仿真" },
];

const FIXED_NOTES = [
  ["安全高度", "fixed.safe_height_mm", "mm"],
  ["快移速度", "fixed.rapid_feed_mm_per_min", "mm/min"],
];

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function defaultsOf(item) {
  const values = {};
  for (const spec of item.parameters || []) values[spec.key] = spec.default;
  return values;
}

function carryOver(previous, item) {
  const values = defaultsOf(item);
  for (const spec of item.parameters || []) {
    if (previous && Object.prototype.hasOwnProperty.call(previous, spec.key)) {
      values[spec.key] = previous[spec.key];
    }
  }
  return values;
}

function formatNumber(value) {
  if (typeof value !== "number") return String(value);
  return Number.isInteger(value) ? String(value) : String(Math.round(value * 1000) / 1000);
}

export class ParameterPanel {
  constructor(options) {
    this.root = options.root;
    this.catalog = options.catalog;
    this.onChange = options.onChange || (() => {});
    this.onDisplayChange = options.onDisplayChange || (() => {});
    this.onModelFile = options.onModelFile || (() => {});
    this.onModelChange = options.onModelChange || (() => {});
    this.importedModel = null;
    this.importSelection = "hull";
    this.state = {
      tool: clone(this.catalog.tool.defaults),
      region: {
        id: this.catalog.regions.default_id,
        values: clone(this.catalog.regions.defaults),
      },
      surface: {
        id: this.catalog.surfaces.default_id,
        values: clone(this.catalog.surfaces.defaults),
      },
      planner: {
        id: this.catalog.planners.default_id,
        values: clone(this.catalog.planners.defaults),
      },
      display: {
        showWorkpiece: true, showPath: true, showRapid: true, showTrace: true,
        showTool: true, showStock: false,
      },
    };
    this.rows = [];
    this.render();
  }

  payload() {
    const region = this.importedModel
      ? {
        shape: "polygon",
        parameters: { boundary: clone(this.importedModel.boundaries[this.importSelection]) },
      }
      : { shape: this.state.region.id, parameters: clone(this.state.region.values) };
    return {
      tool: clone(this.state.tool),
      region,
      surface: { type: this.state.surface.id, parameters: clone(this.state.surface.values) },
      planner: { id: this.state.planner.id, parameters: clone(this.state.planner.values) },
    };
  }

  setImportedModel(model) {
    this.importedModel = model || null;
    if (!model) this.importSelection = "hull";
    this.render();
    this.onModelChange(this.importedModel);
    this.onChange();
  }

  displayOptions() {
    return Object.assign({}, this.state.display);
  }

  // ------------------------------------------------------------- 渲染
  render() {
    this.rows = [];
    this.root.replaceChildren(
      this._capabilitySection("刀具", this.catalog.tool.parameters, this.state.tool, "tool"),
      this._modelSection(),
      this._regionSection(),
      this._surfaceSection(),
      this._plannerSection(),
      this._displaySection(),
      this._simulationSection(),
      this._noteSection()
    );
    this.refreshVisibility();
  }

  _modelSection() {
    const section = this._section("模型导入");
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".obj,.stl,.step,.stp";
    input.title = "选择 OBJ 或 STL 三角网格模型";
    input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      if (file) this.onModelFile(file);
      input.value = "";
    });
    const row = document.createElement("div");
    row.className = "model-import-row";
    row.appendChild(input);
    section.appendChild(row);

    const note = document.createElement("div");
    note.className = "note";
    note.textContent = this.importedModel
      ? `${this.importedModel.name} · ${this.importedModel.format} · ${this.importedModel.triangle_count} 个三角面`
      : "支持 OBJ / STL；导入后按 XY 投影生成加工区域。";
    section.appendChild(note);

    if (this.importedModel) {
      const selectorSpec = {
        key: "__model_selection__",
        label: "区域选择",
        kind: "choice",
        choices: [
          { value: "hull", label: "XY 投影轮廓（凸包）" },
          { value: "bounds", label: "XY 包围矩形" },
        ],
        help: "选择模型投影到 XY 平面后用于刀路规划的区域边界",
      };
      const selector = this._buildControl(selectorSpec, this.importSelection, (value) => {
        this.importSelection = value;
        this.onChange();
      });
      section.appendChild(this._wrapRow(selectorSpec, selector, null, {}));

      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "button model-clear";
      clear.textContent = "清除模型，恢复参数区域";
      clear.addEventListener("click", () => this.setImportedModel(null));
      section.appendChild(clear);
    }
    return section;
  }

  _section(title) {
    const section = document.createElement("section");
    section.className = "section";
    const heading = document.createElement("h3");
    heading.textContent = title;
    section.appendChild(heading);
    return section;
  }

  _capabilitySection(title, specs, values, capability) {
    const section = this._section(title);
    for (const spec of specs) {
      const control = this._buildControl(spec, values[spec.key], (value) => {
        values[spec.key] = value;
        this.refreshVisibility();
        this.onChange();
      });
      section.appendChild(this._wrapRow(spec, control, capability, values));
    }
    return section;
  }

  _regionSection() {
    const section = this._section("区域");
    if (this.importedModel) {
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = "当前使用导入模型的投影区域；清除模型后可恢复方形、圆形或椭圆区域。";
      section.appendChild(note);
      return section;
    }
    const shapes = this.catalog.regions.shapes;
    const current = this._item(shapes, this.state.region.id);
    const selectorSpec = {
      key: "__shape__",
      label: "形状",
      kind: "choice",
      choices: shapes.map((item) => ({ value: item.id, label: item.label })),
      help: current.description,
    };
    const selector = this._buildControl(selectorSpec, this.state.region.id, (value) => {
      this.state.region.id = value;
      this.state.region.values = carryOver(
        this.state.region.values,
        this._item(shapes, value)
      );
      this.render();
      this.onChange();
    });
    section.appendChild(this._wrapRow(selectorSpec, selector, null, this.state.region.values));
    for (const spec of current.parameters) {
      const control = this._buildControl(spec, this.state.region.values[spec.key], (value) => {
        this.state.region.values[spec.key] = value;
        this.onChange();
      });
      section.appendChild(
        this._wrapRow(spec, control, "region", this.state.region.values)
      );
    }
    return section;
  }

  _plannerSection() {
    const section = this._section("刀路");
    const planners = this.catalog.planners.list;
    const current = this._item(planners, this.state.planner.id);
    const selectorSpec = {
      key: "__planner__",
      label: "策略",
      kind: "choice",
      choices: planners.map((item) => ({ value: item.id, label: item.label })),
      help: current.description,
    };
    const selector = this._buildControl(selectorSpec, this.state.planner.id, (value) => {
      this.state.planner.id = value;
      this.state.planner.values = carryOver(
        this.state.planner.values,
        this._item(planners, value)
      );
      this.render();
      this.onChange();
    });
    section.appendChild(this._wrapRow(selectorSpec, selector, null, this.state.planner.values));
    for (const spec of current.parameters) {
      const control = this._buildControl(spec, this.state.planner.values[spec.key], (value) => {
        this.state.planner.values[spec.key] = value;
        this.refreshVisibility();
        this.onChange();
      });
      section.appendChild(
        this._wrapRow(spec, control, "planner", this.state.planner.values)
      );
    }
    return section;
  }

  _surfaceSection() {
    const section = this._section("曲面");
    const surfaces = this.catalog.surfaces.types;
    const current = this._item(surfaces, this.state.surface.id);
    const selectorSpec = {
      key: "__surface__",
      label: "类型",
      kind: "choice",
      choices: surfaces.map((item) => ({ value: item.id, label: item.label })),
      help: current.description,
    };
    const selector = this._buildControl(selectorSpec, this.state.surface.id, (value) => {
      this.state.surface.id = value;
      this.state.surface.values = carryOver(
        this.state.surface.values,
        this._item(surfaces, value)
      );
      this.render();
      this.onChange();
    });
    section.appendChild(this._wrapRow(selectorSpec, selector, null, this.state.surface.values));
    for (const spec of current.parameters) {
      const control = this._buildControl(spec, this.state.surface.values[spec.key], (value) => {
        this.state.surface.values[spec.key] = value;
        this.onChange();
      });
      section.appendChild(
        this._wrapRow(spec, control, "surface", this.state.surface.values)
      );
    }
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

  _simulationSection() {
    const section = this._section("仿真");
    for (const option of SIMULATION_OPTIONS) {
      const row = document.createElement("label");
      row.className = "checkbox-row";
      row.title = "勾选后才创建毛坯高度场，并随播放或拖动时间轴切除材料";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = this.state.display[option.key];
      input.addEventListener("change", () => {
        this.state.display[option.key] = input.checked;
        this.onDisplayChange(this.displayOptions());
      });
      row.append(input);
      const text = document.createElement("span");
      text.textContent = option.label;
      row.append(text);
      section.append(row);
    }
    const hint = document.createElement("div");
    hint.className = "note";
    hint.textContent = "高度场教学仿真：播放刀路时逐步降低毛坯顶部高度。";
    section.append(hint);
    return section;
  }

  _noteSection() {
    const section = this._section("固定设置");
    const note = document.createElement("div");
    note.className = "note";
    const fixed = this.catalog.fixed || {};
    const lines = [
      ["安全高度", fixed.safe_height_mm, "mm"],
      ["快移速度", fixed.rapid_feed_mm_per_min, "mm/min"],
    ];
    for (const [label, value, unit] of lines) {
      const line = document.createElement("div");
      line.append(label + " ");
      const strong = document.createElement("b");
      strong.textContent = value + " " + unit;
      line.appendChild(strong);
      note.appendChild(line);
    }
    const hint = document.createElement("div");
    hint.textContent = "边界内缩一个刀具半径；想改成可调参数，见 docs/extending.md";
    note.appendChild(hint);
    section.appendChild(note);
    return section;
  }

  // ------------------------------------------------------------- 控件
  _item(items, id) {
    return items.find((item) => item.id === id) || items[0];
  }

  _wrapRow(spec, control, capability, values) {
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
    if (control.slider) {
      // 滑块单独占一行，数值框因此永远不会被挤出面板。
      row.appendChild(control.slider);
    }
    this.rows.push({ row, spec, values });
    return row;
  }

  _buildControl(spec, value, onChange) {
    if (spec.kind === "bool") {
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = Boolean(value);
      input.addEventListener("change", () => onChange(input.checked));
      return { node: input };
    }
    if (spec.kind === "choice") {
      const select = document.createElement("select");
      for (const choice of spec.choices || []) {
        const option = document.createElement("option");
        option.value = choice.value;
        option.textContent = choice.label;
        option.disabled = Boolean(choice.disabled);
        if (choice.value === value) option.selected = true;
        select.appendChild(option);
      }
      select.addEventListener("change", () => onChange(select.value));
      return { node: select };
    }
    return this._numberControl(spec, Number(value), onChange);
  }

  _numberControl(spec, value, onChange) {
    const wrapper = document.createElement("div");
    wrapper.className = "control";
    const step = spec.step || (spec.kind === "int" ? 1 : 0.1);
    const number = document.createElement("input");
    number.type = "number";
    number.step = String(step);
    if (spec.min !== null && spec.min !== undefined) number.min = String(spec.min);
    if (spec.max !== null && spec.max !== undefined) number.max = String(spec.max);
    number.value = formatNumber(value);

    let slider = null;
    if (spec.kind !== "int" && spec.min !== null && spec.min !== undefined
        && spec.max !== null && spec.max !== undefined) {
      slider = document.createElement("input");
      slider.type = "range";
      slider.min = String(spec.min);
      slider.max = String(spec.max);
      slider.step = String(step);
      slider.value = String(value);
      slider.addEventListener("input", () => {
        number.value = slider.value;
        onChange(Number(slider.value));
      });
    }
    number.addEventListener("change", () => {
      let next = Number(number.value);
      if (!Number.isFinite(next)) return;
      if (spec.min !== null && spec.min !== undefined) next = Math.max(spec.min, next);
      if (spec.max !== null && spec.max !== undefined) next = Math.min(spec.max, next);
      number.value = formatNumber(next);
      if (slider) slider.value = String(next);
      onChange(next);
    });
    wrapper.appendChild(number);
    return { node: wrapper, slider };
  }

  refreshVisibility() {
    for (const entry of this.rows) {
      const rules = entry.spec.visible_if;
      if (!rules || Object.keys(rules).length === 0) continue;
      let visible = true;
      for (const [key, expected] of Object.entries(rules)) {
        if (String(entry.values[key]) !== String(expected)) visible = false;
      }
      entry.row.hidden = !visible;
    }
  }
}
