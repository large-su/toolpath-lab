// The parameter panel comes entirely from /api/catalog: each capability declares its parameters
// once in Python, this file renders them. A new shape or strategy appears on refresh, no change here.

const DISPLAY_OPTIONS = [
  { key: "showWorkpiece", label: "工件" },
  { key: "showPath", label: "刀路", color: "var(--orange)" },
  { key: "showRapid", label: "快移", color: "var(--cyan)" },
  { key: "showTrace", label: "已走轨迹", color: "var(--teal)" },
  { key: "showUncut", label: "未切除", color: "var(--danger)" },
  { key: "showTool", label: "刀具" },
];

// An imported outline is the one region the catalogue cannot describe: its geometry is a point list
// that only exists after a file has been read. So the option is added here by hand, and only once a
// drawing is in -- core/region.py keeps ImportedOutlineRegion out of the catalogue on purpose, so the
// panel must never offer a shape it has no points for.
const IMPORTED_ID = "imported";
const IMPORTED_SHAPE = {
  id: IMPORTED_ID,
  label: "导入轮廓",
  description: "从图纸导入的闭合轮廓；点串随规划请求一起发给后端",
  parameters: [],
};

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

// Drawings are text, and their structure is ASCII, but layer names are often written in a legacy code
// page (a Chinese CAD exports GBK). A UTF-8 read that produced replacement characters is therefore
// retried with GBK; either way the parser sees the structure intact.
async function readDrawingText(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const utf8 = new TextDecoder("utf-8").decode(bytes);
  if (!utf8.includes("\ufffd")) return utf8;
  try {
    return new TextDecoder("gbk").decode(bytes);
  } catch (error) {
    return utf8;
  }
}

function outlineLabel(outline, index) {
  const points = (outline && outline.points) || [];
  let text = "轮廓 " + (index + 1) + " · " + points.length + " 点";
  if (outline && outline.layer) text += " · 图层 " + outline.layer;
  if (outline && outline.closed === false) text += "（未闭合）";
  return text;
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
    // Reading the chosen file and posting it belongs to the app (main.js); the panel only wires the
    // button to it. Both callbacks stay optional so a bare panel still renders.
    this.importDxf = options.importDxf || (async () => {
      throw new Error("面板没有接上导入接口");
    });
    this.notice = options.notice || (() => {});
    this.state = {
      tool: clone(this.catalog.tool.defaults),
      region: {
        id: this.catalog.regions.default_id,
        values: clone(this.catalog.regions.defaults),
        outlines: [],  // outlines of the last imported drawing; empty until a file has been read
        outlineIndex: 0,  // which of them is being machined
        fileName: "",
      },
      planner: {
        id: this.catalog.planners.default_id,
        values: clone(this.catalog.planners.defaults),
      },
      display: { showWorkpiece: true, showPath: true, showRapid: true, showTrace: true,
                 showUncut: true, showTool: true },
    };
    this.rows = [];
    this.render();
  }

  payload() {
    const region = {
      shape: this.state.region.id,
      parameters: clone(this.state.region.values),
    };
    if (this.state.region.id === IMPORTED_ID) {
      // The one region whose geometry is not a parameter map: the outline id and its points travel
      // with the request, and the backend builds the region from them.
      const outline = this.selectedOutline();
      region.parameters = { points: outline ? clone(outline.points) : [] };
    }
    return {
      tool: clone(this.state.tool),
      region: region,
      planner: { id: this.state.planner.id, parameters: clone(this.state.planner.values) },
    };
  }

  selectedOutline() {
    const outlines = this.state.region.outlines;
    return outlines[this.state.region.outlineIndex] || outlines[0] || null;
  }

  displayOptions() {
    return Object.assign({}, this.state.display);
  }

  // ------------------------------------------------------------- render
  render() {
    this.rows = [];
    this.root.replaceChildren(
      this._capabilitySection("刀具", this.catalog.tool.parameters, this.state.tool, "tool"),
      this._regionSection(),
      this._plannerSection(),
      this._displaySection()
    );
    this.refreshVisibility();
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
    const shapes = this.catalog.regions.shapes;
    const current = this._currentShape(shapes);
    const choices = shapes.map((item) => ({ value: item.id, label: item.label }));
    if (this.state.region.outlines.length) {
      // Only offered once a drawing has been read, because only then are there points to send.
      choices.push({ value: IMPORTED_ID, label: IMPORTED_SHAPE.label });
    }
    const selectorSpec = {
      key: "__shape__",
      label: "形状",
      kind: "choice",
      choices: choices,
      help: current.description,
    };
    const selector = this._buildControl(selectorSpec, this.state.region.id, (value) => {
      this.state.region.id = value;
      if (value !== IMPORTED_ID) {
        // The imported outline has no parameters of its own, and the catalogue values are kept so
        // switching back and forth does not throw the settings away.
        this.state.region.values = carryOver(
          this.state.region.values,
          this._item(shapes, value)
        );
      }
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
    section.appendChild(this._importRow());
    if (this.state.region.id === IMPORTED_ID) {
      section.appendChild(this._outlineRow());
    }
    return section;
  }

  _currentShape(shapes) {
    if (this.state.region.id === IMPORTED_ID) return IMPORTED_SHAPE;
    return this._item(shapes, this.state.region.id);
  }

  _importRow() {
    const row = document.createElement("div");
    row.className = "row";
    const label = document.createElement("label");
    label.textContent = "DXF 图纸";
    const cell = document.createElement("div");
    cell.className = "control";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button";
    button.textContent = "导入…";
    button.title = this.state.region.fileName
      ? "已导入 " + this.state.region.fileName + "，可以再选一张图纸"
      : "选择一张 DXF 图纸，读出其中的闭合轮廓";
    const picker = document.createElement("input");
    picker.type = "file";
    picker.accept = ".dxf,text/plain";
    picker.hidden = true;
    button.addEventListener("click", () => picker.click());
    picker.addEventListener("change", () => {
      const file = picker.files && picker.files[0];
      if (file) this._importFile(file);
    });
    cell.append(button, picker);
    row.append(label, cell);
    return row;
  }

  _outlineRow() {
    const row = document.createElement("div");
    row.className = "row";
    const label = document.createElement("label");
    label.textContent = "轮廓";
    const cell = document.createElement("div");
    cell.className = "control";
    const select = document.createElement("select");
    this.state.region.outlines.forEach((outline, index) => {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = outlineLabel(outline, index);
      option.selected = index === this.state.region.outlineIndex;
      select.appendChild(option);
    });
    select.addEventListener("change", () => {
      this.state.region.outlineIndex = Number(select.value);
      this.render();
      this.onChange();
    });
    cell.appendChild(select);
    row.append(label, cell);
    return row;
  }

  async _importFile(file) {
    try {
      const result = await this.importDxf(await readDrawingText(file));
      const outlines = (result && result.outlines) || [];
      if (!outlines.length) throw new Error("图纸里没有可用的闭合轮廓");
      this.state.region.outlines = outlines;
      this.state.region.outlineIndex = 0;
      this.state.region.fileName = file.name;
      this.state.region.id = IMPORTED_ID;
      this.render();
      this.onChange();
      const warnings = ((result && result.warnings) || []).join("；");
      // Warnings are what the parser had to skip; they matter more than the success message.
      if (warnings) this.notice(warnings);
      else this.notice("已导入 " + outlines.length + " 条轮廓：" + file.name, "info");
    } catch (error) {
      this.notice("导入失败：" + error.message);
    }
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

  // ------------------------------------------------------------- controls
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
      // The slider gets a row of its own, so the number box is never squeezed out of the panel.
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
