// 刀具库界面：列表 + 新建/编辑表单（弹窗），以及"给工序选刀"的选取器。
//
// 用弹窗而不是塞进左侧参数面板，是因为刀具库是**全局**的、和当前工序无关：
// 用一个 760px 的双栏对话框（左列表、右表单）比挤在 280px 的侧栏里清楚得多，
// 而且打开时不会打断刀路编程的上下文。
//
// 表单控件同样由后端的参数声明生成（`/api/tools` 的 `catalog.types`），
// 所以后端给某种刀具加一个参数（例如丝锥的螺距），这里一行都不用改。
//
// 本模块只负责"显示 + 收集输入"，落盘与重算由 main.js 编排（与 cam-panel 一致）。

import { buildControl, clone, refreshVisibility } from "./controls.js";

/** 把一条工具记录压成一行摘要文本（列表里用）。 */
export function toolSummary(tool) {
  const values = (tool && tool.values) || {};
  const parts = [`D${formatNumber(values.diameter_mm)}`];
  if (tool && tool.kind === "bull_nose_mill") parts.push(`R${formatNumber(values.corner_radius_mm)}`);
  if (tool && tool.kind === "taper_mill") parts.push(`${formatNumber(values.taper_angle_deg)}°`);
  if (tool && tool.kind === "drill") parts.push(`${formatNumber(values.tip_angle_deg)}°`);
  if (tool && tool.kind === "tap") parts.push(`P${formatNumber(values.pitch_mm)}`);
  parts.push(`L${formatNumber(values.length_mm)}`);
  return parts.join(" · ");
}

function formatNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return Number.isInteger(number) ? String(number) : String(Math.round(number * 1000) / 1000);
}

/**
 * 刀具库对话框。
 *
 * @param {object} options
 * @param {import("./modal.js").Modal} options.modal 复用的模态框实例
 * @param {object[]} options.tools 当前刀具列表（后端 to_payload 的结果）
 * @param {object} options.catalog `/api/tools` 返回的 catalog（类型目录）
 * @param {object} [options.selectedId] 打开时高亮的刀具
 * @param {(tool: object) => Promise|void} [options.onSelect] 双击/「用作当前刀具」回调
 * @param {(draft: object) => Promise} [options.onSave] 保存（新建或更新）后返回最新刀具
 * @param {(toolId: string) => Promise} [options.onDelete]
 * @param {(toolId: string) => Promise} [options.onDuplicate]
 * @param {() => Promise} [options.onRestoreDefaults]
 * @param {(payload: object|null) => void} [options.onPreview] 三维预览（null = 清除）
 * @param {boolean} [options.pickMode] true 时点列表即选中（给工序选刀用）
 * @param {(message: string, kind?: string) => void} [options.onMessage] 提示条
 */
export class ToolLibraryDialog {
  constructor(options) {
    this.modal = options.modal;
    this.catalog = options.catalog || { types: [] };
    this.tools = Array.isArray(options.tools) ? options.tools.slice() : [];
    this.onSelect = options.onSelect || null;
    this.onSave = options.onSave || (async () => {});
    this.onDelete = options.onDelete || (async () => {});
    this.onDuplicate = options.onDuplicate || (async () => {});
    this.onRestoreDefaults = options.onRestoreDefaults || (async () => {});
    this.onPreview = options.onPreview || (() => {});
    this.onMessage = options.onMessage || (() => {});
    this.pickMode = Boolean(options.pickMode);

    /** null = 还没选中任何刀具（右栏此时显示"新建"表单或空提示） */
    this.currentId = options.selectedId || (this.tools[0] ? this.tools[0].id : null);
    this.draft = null;         // 右栏正在编辑的数据：{ id|null, name, kind, values, note }
    this.dirty = false;
    this.error = "";
    this.rows = [];
    this.footerNote = null;
    this._resetDraft();
  }

  // ------------------------------------------------------------- 对外接口
  open() {
    this._render();
    this._preview();
  }

  /** 后端返回了新的刀具列表后刷新界面。 */
  setTools(tools, selectedId) {
    this.tools = Array.isArray(tools) ? tools.slice() : [];
    if (selectedId) this.currentId = selectedId;
    if (!this.tools.some((item) => item.id === this.currentId)) {
      this.currentId = this.tools[0] ? this.tools[0].id : null;
    }
    this._resetDraft();
    this._render();
  }

  /** 当前选中的刀具记录（没有就返回 null）。 */
  selected() {
    return this.tools.find((item) => item.id === this.currentId) || null;
  }

  /** 右栏正在编辑的内容。 */
  currentDraft() {
    return this.draft ? { ...this.draft, values: clone(this.draft.values) } : null;
  }

  // --------------------------------------------------------------- 内部
  _resetDraft() {
    const tool = this.selected();
    this.draft = tool
      ? { id: tool.id, name: tool.name, kind: tool.kind, values: clone(tool.values),
          note: tool.note || "" }
      : null;
    this.dirty = false;
    this.error = "";
  }

  _typeEntry(kind) {
    return (this.catalog.types || []).find((item) => item.id === kind) || null;
  }

  /** 某种刀具类型的参数声明（后端已经按类型过滤好了）。 */
  _specs(kind) {
    const entry = this._typeEntry(kind);
    return (entry && entry.parameters) || [];
  }

  _defaults(kind) {
    const entry = this._typeEntry(kind);
    const values = clone((entry && entry.defaults) || {});
    values.kind = kind;
    return values;
  }

  _startNew(kind) {
    const types = this.catalog.types || [];
    const toolType = kind || (types[0] ? types[0].id : "flat_end_mill");
    this.currentId = null;
    this.draft = {
      id: null,
      name: this._suggestName(toolType),
      kind: toolType,
      values: this._defaults(toolType),
      note: "",
    };
    this.dirty = false;
    this.error = "";
    this._render();
    this._preview();
  }

  /** 新建时的建议名称：跟着类型与主尺寸走，省一次输入。 */
  _suggestName(kind) {
    const entry = this._typeEntry(kind);
    const label = ((entry && entry.label) || kind).split(" ")[0];
    const used = new Set(this.tools.map((item) => item.name));
    let index = this.tools.length + 1;
    let candidate = `${label} ${index}`;
    while (used.has(candidate)) {
      index += 1;
      candidate = `${label} ${index}`;
    }
    return candidate;
  }

  _preview() {
    const draft = this.draft;
    if (!draft) {
      this.onPreview(null);
      return;
    }
    // 预览用与后端同一套换算（值 → 几何），让"表单里的数字"和"看到的刀"一致。
    const values = draft.values || {};
    const kind = draft.kind;
    const diameter = Number(values.diameter_mm) || 6;
    const length = Number(values.length_mm) || 40;
    let toolKind = "flat";
    if (kind === "ball_end_mill") toolKind = "ball";
    else if (kind === "bull_nose_mill") toolKind = "bull";
    const corner = kind === "bull_nose_mill"
      ? Math.min(Number(values.corner_radius_mm) || 0, diameter / 2)
      : 0;
    this.onPreview({
      kind: toolKind,
      kind_label: (this._typeEntry(kind) || {}).label || kind,
      diameter_mm: diameter,
      radius_mm: diameter / 2,
      length_mm: length,
      corner_radius_mm: toolKind === "ball" ? diameter / 2 : corner,
      footprint_radius_mm: toolKind === "ball" ? 0 : Math.max(0, diameter / 2 - corner),
      flute_mm: Number(values.flute_length_mm) || 0,
    });
  }

  // ------------------------------------------------------------- 渲染
  _render() {
    this._renderList();
    this._renderEditor();
    this._renderFooter();
  }

  _renderList() {
    const list = this.listNode || document.createElement("div");
    list.className = "tool-list";
    list.replaceChildren();
    this.listNode = list;

    if (!this.tools.length) {
      const empty = document.createElement("div");
      empty.className = "note";
      empty.textContent = "刀库是空的：点下方「新建刀具」加一把。";
      list.appendChild(empty);
    }

    for (const tool of this.tools) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "tool-row";
      row.dataset.id = tool.id;
      if (tool.id === this.currentId) row.classList.add("active");
      const name = document.createElement("span");
      name.className = "tool-name";
      name.textContent = tool.name;
      const spec = document.createElement("span");
      spec.className = "tool-spec";
      spec.textContent = toolSummary(tool);
      const kind = document.createElement("span");
      kind.className = "tool-kind";
      kind.textContent = (tool.kind_label || "").split(" ")[0];
      row.append(name, spec, kind);
      row.title = `${tool.kind_label}\n${toolSummary(tool)}`;
      row.addEventListener("click", () => {
        this.currentId = tool.id;
        this._resetDraft();
        this._render();
        this._preview();
        if (this.pickMode && this.onSelect) this._choose(tool);
      });
      row.addEventListener("dblclick", () => {
        if (!this.onSelect) return;
        this._choose(tool);
      });
      list.appendChild(row);
    }
  }

  _renderEditor() {
    const panel = this.editorNode || document.createElement("div");
    panel.className = "tool-editor";
    panel.replaceChildren();
    this.editorNode = panel;
    this.rows = [];

    if (!this.draft) {
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = "左侧选一把刀来编辑，或点「新建刀具」。";
      panel.appendChild(note);
      return;
    }

    // -- 名称 --
    const nameRow = document.createElement("div");
    nameRow.className = "row";
    const nameLabel = document.createElement("label");
    nameLabel.textContent = "刀具名称";
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "text-input";
    nameInput.value = this.draft.name;
    nameInput.placeholder = "例如：D10 平底刀（铝）";
    nameInput.addEventListener("change", () => {
      this.draft.name = nameInput.value;
      this._markDirty();
    });
    nameRow.append(nameLabel, nameInput);
    panel.appendChild(nameRow);

    // -- 类型：换类型 = 换一整套参数（和加工类型同样的处理）--
    const kindRow = document.createElement("div");
    kindRow.className = "row";
    const kindLabel = document.createElement("label");
    kindLabel.textContent = "刀具类型";
    const kindControl = buildControl({
      key: "__tool_kind__",
      label: "刀具类型",
      kind: "choice",
      choices: (this.catalog.types || []).map((item) => ({ value: item.id, label: item.label })),
    }, this.draft.kind, (value) => {
      this.draft.kind = value;
      this.draft.values = this._defaults(value);
      if (this.draft.id === null) this.draft.name = this._suggestName(value);
      this._markDirty();
      this._renderEditor();
      this._preview();
    });
    kindRow.append(kindLabel, kindControl.node);
    panel.appendChild(kindRow);

    // -- 该类型的参数（后端声明驱动）--
    const specs = this._specs(this.draft.kind);
    const groups = new Map();
    for (const spec of specs) {
      if (spec.key === "kind") continue;
      const group = spec.group || "常规";
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(spec);
    }
    for (const [group, groupSpecs] of groups) {
      const heading = document.createElement("h4");
      heading.className = "group-head";
      heading.textContent = group;
      panel.appendChild(heading);
      for (const spec of groupSpecs) {
        const row = document.createElement("div");
        row.className = "row";
        row.dataset.key = spec.key;
        const label = document.createElement("label");
        label.textContent = spec.label;
        if (spec.help) label.title = spec.help;
        const control = buildControl(spec, this.draft.values[spec.key], (value) => {
          this.draft.values[spec.key] = value;
          this._markDirty();
          refreshVisibility(this.rows, this.draft.values);
          this._preview();
        });
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
        panel.appendChild(row);
        this.rows.push({ row, spec, control });
      }
    }
    refreshVisibility(this.rows, this.draft.values);

    // -- 备注 --
    const noteRow = document.createElement("div");
    noteRow.className = "row";
    const noteLabel = document.createElement("label");
    noteLabel.textContent = "备注";
    const noteInput = document.createElement("input");
    noteInput.type = "text";
    noteInput.className = "text-input";
    noteInput.value = this.draft.note || "";
    noteInput.placeholder = "适用材料 / 用途，可留空";
    noteInput.addEventListener("change", () => {
      this.draft.note = noteInput.value;
      this._markDirty();
    });
    noteRow.append(noteLabel, noteInput);
    panel.appendChild(noteRow);

    // -- 摘要 --
    const summary = document.createElement("div");
    summary.className = "note";
    summary.textContent = this.draft.id
      ? `库内 id：${this.draft.id}`
      : "尚未保存：点下方「保存」加入刀具库";
    panel.appendChild(summary);

    const error = document.createElement("div");
    error.className = "modal-error";
    error.textContent = this.error;
    panel.appendChild(error);
  }

  _markDirty() {
    this.dirty = true;
    if (this.footerNote) this.footerNote.textContent = "有未保存的改动";
  }

  _renderFooter() {
    const foot = this.footerNode || document.createElement("div");
    foot.className = "tool-foot";
    foot.replaceChildren();
    this.footerNode = foot;

    const left = document.createElement("div");
    left.className = "tool-foot-left";
    const create = this._button("新建刀具", () => this._startNew(this.draft ? this.draft.kind : ""));
    create.classList.add("primary");
    const restore = this._button("恢复出厂刀具", async () => {
      try {
        await this.onRestoreDefaults();
      } catch (error) {
        this._fail(error.message);
      }
    });
    left.append(create, restore);
    this.footerNote = document.createElement("span");
    this.footerNote.className = "tool-foot-note";
    this.footerNote.textContent = this.pickMode ? "点击左侧刀具即可切换当前工序用刀" : "";
    left.appendChild(this.footerNote);

    const right = document.createElement("div");
    right.className = "tool-foot-right";
    if (this.draft && this.draft.id) {
      right.appendChild(this._button("复制", async () => {
        try {
          await this.onDuplicate(this.draft.id);
        } catch (error) {
          this._fail(error.message);
        }
      }));
      right.appendChild(this._button("删除", async () => {
        try {
          await this.onDelete(this.draft.id);
        } catch (error) {
          this._fail(error.message);
        }
      }, "danger"));
    }
    if (this.pickMode) {
      right.appendChild(this._button("用作当前刀具", () => {
        const tool = this.selected();
        if (tool) this._choose(tool);
      }, "primary"));
    }
    right.appendChild(this._button("保存", () => this._save(), "primary"));
    right.appendChild(this._button("关闭", () => this.modal.close()));

    foot.append(left, right);
    if (this.footerNote && this.dirty) this.footerNote.textContent = "有未保存的改动";
  }

  _button(text, handler, extra = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button tiny" + (extra ? " " + extra : "");
    button.textContent = text;
    button.addEventListener("click", handler);
    return button;
  }

  async _save() {
    if (!this.draft) return;
    if (!String(this.draft.name || "").trim()) {
      this._fail("请填写刀具名称");
      return;
    }
    try {
      const saved = await this.onSave({
        id: this.draft.id,
        name: this.draft.name.trim(),
        kind: this.draft.kind,
        values: clone(this.draft.values),
        note: this.draft.note || "",
      });
      if (saved && saved.id) this.currentId = saved.id;
      this.dirty = false;
      this.error = "";
      this._resetDraft();
      this._render();
      this._preview();
    } catch (error) {
      this._fail(error.message);
    }
  }

  _fail(message) {
    this.error = message || "操作失败";
    if (this.editorNode) {
      let node = this.editorNode.querySelector(".modal-error");
      if (!node) {
        node = document.createElement("div");
        node.className = "modal-error";
        this.editorNode.appendChild(node);
      }
      node.textContent = this.error;
    }
    this.onMessage(this.error);
  }

  _choose(tool) {
    if (!this.onSelect) return;
    this.modal.close();
    this.onSelect(tool);
  }
}

/**
 * 打开刀具库对话框。
 *
 * `pickMode: true` 时是"给当前工序选刀"的选取器（点一下就选），
 * 否则是完整的刀具库管理页（增删改）。
 */
export function openToolLibrary(options) {
  const dialog = new ToolLibraryDialog(options);
  const body = document.createElement("div");
  body.className = "tool-layout" + (options.wide ? " wide" : "");
  const listPane = document.createElement("div");
  listPane.className = "tool-pane tool-pane-list";
  const editorPane = document.createElement("div");
  editorPane.className = "tool-pane tool-pane-editor";
  body.append(listPane, editorPane);

  dialog.listNode = listPane;
  dialog.editorNode = editorPane;
  const footer = document.createElement("div");
  footer.className = "tool-foot";
  dialog.footerNode = footer;

  options.modal.open({
    title: options.pickMode ? "选择刀具" : "刀具库",
    body,
    footer,
    onClose: () => {
      // 关闭时把三维预览还原（否则视口里会留下一把"没在任何工序里"的刀）
      options.onPreview(null);
      if (options.onClose) options.onClose();
    },
  });
  dialog.open();
  return dialog;
}
