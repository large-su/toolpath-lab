// 工序树面板：把 GET /api/operations 的 payload 渲染成"一行一道工序"，并把点击、
// 改名、启用、生成这些用户意图通过回调交给 main.js。这里刻意不碰网络、也不 import
// api.js —— 宿主拿到回调后自己发请求、自己再 setOperations()，面板只负责显示与手势。
//
// 一行的信息顺序对齐 NX 的工序导航器：序号 → 名称 → 加工类型 → 加工状态 → 启用开关，
// 参数摘要放在第二行，免得把 264px 宽的侧栏撑爆。

/** 双击判定窗口：两条点击间隔小于它就当作"双击名称进入改名"。 */
const DOUBLE_CLICK_MS = 400;

/** 参数摘要用到的键与显示前缀，数组顺序就是摘要里从左到右的顺序。 */
const SUMMARY_FIELDS = [
  ["tool_diameter_mm", "D"],
  ["stepover_mm", "ae"],
  ["cut_depth_mm", "ap"],
  ["feed_mm_per_min", "F"],
];

/** 主标题里用到的状态：draft 淡、generated 青、verified 蓝。 */
const STATE_CLASS = {
  draft: "tree-state-draft",
  generated: "tree-state-generated",
  verified: "tree-state-verified",
};

function span(className, text) {
  const node = document.createElement("span");
  node.className = className;
  node.textContent = text;
  return node;
}

// 整数不补 .0（D10 比 D10.0 好读），小数最多三位；非数字返回 null 让调用方跳过该项。
function formatNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return Number.isInteger(number) ? String(number) : String(Math.round(number * 1000) / 1000);
}

// 摘要只列"后端真的给了"的参数：草稿工序可能一个参数都没有，那时返回空串。
function summaryOf(operation) {
  const parameters = operation.parameters || {};
  const parts = [];
  for (const [key, prefix] of SUMMARY_FIELDS) {
    const text = formatNumber(parameters[key]);
    if (text !== null) parts.push(prefix + text);
  }
  return parts.join(" · ");
}

export class OperationTreePanel {
  constructor({ root, onChange, onSelect, onGenerate } = {}) {
    this.root = root;
    this.onChange = onChange || (() => {});
    this.onSelect = onSelect || (() => {});
    this.onGenerate = onGenerate || (() => {});
    this.operations = [];
    this.selectedId = null;
    // 改名状态放在实例上而不是 DOM 上：宿主每次 setOperations() 都会重建行，
    // 状态留在 DOM 里的话，一次后台刷新就会把用户正在输入的内容吞掉。
    this.renamingId = null;
    this.renameFocusPending = false;
    // id -> 行元素，仅用于切换高亮；重建行时整体替换。
    this.rows = new Map();
    this.lastClickId = null;
    this.lastClickAt = 0;
    this.render();
  }

  // ------------------------------------------------------------- 对外接口
  /** 宿主拉完 /api/operations 后整包塞进来；count/enabled_count 由宿主自己展示，这里不存。 */
  setOperations(payload) {
    const data = payload || {};
    this.operations = Array.isArray(data.operations) ? data.operations : [];
    // 工序可能已被删除或重建：悬空的选中/改名 id 必须清掉，否则高亮会留在错误的行上。
    if (this.selectedId && !this._find(this.selectedId)) this.selectedId = null;
    if (this.renamingId && !this._find(this.renamingId)) this.renamingId = null;
    this.render();
  }

  getSelectedId() {
    return this.selectedId;
  }

  /** 纯程序化选中：只改高亮，不发 onSelect，避免宿主自己把自己绕回来。 */
  select(id) {
    this.selectedId = id === null || id === undefined ? null : String(id);
    this._applySelection();
  }

  // ------------------------------------------------------------- 渲染
  render() {
    this.rows = new Map();
    this.root.replaceChildren();
    if (this.operations.length === 0) {
      this.root.appendChild(this._emptyState());
      return;
    }
    // 序号以后端 sequence 为准，但仍在本地排一次序：宿主可能先乐观更新再等刷新。
    const ordered = this.operations
      .slice()
      .sort((left, right) => Number(left.sequence || 0) - Number(right.sequence || 0));
    ordered.forEach((operation, index) => {
      this.root.appendChild(this._row(operation, index));
    });
    this._applySelection();
  }

  _emptyState() {
    const box = document.createElement("div");
    box.className = "tree-empty";
    const title = document.createElement("p");
    title.textContent = "还没有工序。";
    const steps = document.createElement("ol");
    for (const text of [
      "用顶栏的「导入模型」载入 STEP",
      "在视口里拾取要加工的面",
      "点上面的「新增工序」",
    ]) {
      const item = document.createElement("li");
      item.textContent = text;
      steps.appendChild(item);
    }
    const tail = document.createElement("p");
    tail.textContent = "新增之后，每道工序会按顺序列在这里。";
    box.append(title, steps, tail);
    return box;
  }

  _row(operation, index) {
    const row = document.createElement("div");
    row.className = "tree-row";
    row.dataset.id = String(operation.id);
    row.tabIndex = 0;
    row.dataset.state = String(operation.state || "draft");
    row.dataset.enabled = operation.enabled === false ? "false" : "true";
    if (operation.enabled === false) row.classList.add("off");
    row.addEventListener("click", (event) => this._onRowClick(event, operation));
    row.addEventListener("keydown", (event) => {
      // 行自己拿到焦点时回车=选中；输入框里的回车由输入框处理，不冒泡到这里。
      if (event.key !== "Enter" || event.target !== row) return;
      this._select(operation, true);
    });

    const sequence = Number(operation.sequence);
    const seq = span("tree-seq", String((Number.isFinite(sequence) ? sequence : index) + 1));

    const main = document.createElement("div");
    main.className = "tree-main";
    const line = document.createElement("div");
    line.className = "tree-line";
    line.appendChild(this._nameCell(operation));
    line.appendChild(span("tree-kind", operation.kind_label || operation.kind || "未知类型"));
    const state = String(operation.state || "draft");
    line.appendChild(
      span("tree-state " + (STATE_CLASS[state] || ""), operation.state_label || state)
    );
    const warnings = Array.isArray(operation.warnings) ? operation.warnings : [];
    if (warnings.length > 0) {
      const warn = span("tree-warn", "⚠");
      // 只把第一条挂成 title：提示框里塞一整串反而看不清重点。
      warn.title = String(warnings[0]);
      line.appendChild(warn);
    }
    main.appendChild(line);

    const summary = document.createElement("div");
    summary.className = "tree-summary";
    const text = summaryOf(operation);
    if (text) {
      summary.textContent = text;
    } else {
      summary.textContent = "参数未设置";
      summary.classList.add("tree-summary-empty");
    }
    main.appendChild(summary);

    row.append(seq, main, this._actions(operation));
    this.rows.set(String(operation.id), row);
    return row;
  }

  _nameCell(operation) {
    if (String(operation.id) === this.renamingId) return this._renameInput(operation);
    const name = span("tree-name", operation.name || "未命名工序");
    name.title = "双击改名";
    name.addEventListener("dblclick", (event) => {
      event.stopPropagation();
      this._startRename(String(operation.id));
    });
    return name;
  }

  _renameInput(operation) {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "tree-rename";
    input.value = String(operation.name || "");
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        this._commitRename(String(operation.id), input.value);
      } else if (event.key === "Escape") {
        event.preventDefault();
        this._cancelRename();
      }
    });
    // 点到别处也算提交：不然用户以为改名生效了，其实输入框只是失焦被丢掉。
    input.addEventListener("blur", () => this._commitRename(String(operation.id), input.value));
    input.addEventListener("click", (event) => event.stopPropagation());

    if (this.renameFocusPending) {
      this.renameFocusPending = false;
      // 输入框此刻还没插进文档，微任务里聚焦才是对已挂载的节点操作。
      queueMicrotask(() => {
        if (!input.isConnected) return;
        input.focus();
        input.select();
      });
    }
    return input;
  }

  _actions(operation) {
    const actions = document.createElement("div");
    actions.className = "tree-actions";

    const label = document.createElement("label");
    label.className = "tree-enable";
    label.title = "启用 / 禁用该工序";
    const enabled = operation.enabled !== false;
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = enabled;
    checkbox.addEventListener("change", () => {
      this.onChange(operation.id, { enabled: checkbox.checked });
    });
    label.append(checkbox, span("tree-enable-text", enabled ? "启用" : "禁用"));
    // label 会把点击语义算给里面的控件，这里挡掉冒泡，避免顺带选中整行。
    label.addEventListener("click", (event) => event.stopPropagation());
    actions.appendChild(label);

    const generate = document.createElement("button");
    generate.type = "button";
    generate.className = "button tiny tree-generate";
    generate.textContent = "生成";
    generate.title = "生成这道工序的刀路";
    generate.addEventListener("click", (event) => {
      event.stopPropagation();
      this.onGenerate(operation);
    });
    actions.appendChild(generate);
    return actions;
  }

  // ------------------------------------------------------------- 交互
  _onRowClick(event, operation) {
    const target = event.target;
    // 行内的复选框、生成按钮、改名输入框有自己的处理，不能让它们顺手选中整行。
    if (target.closest("button, input, label")) return;
    const id = String(operation.id);
    const now = Date.now();
    const onName = Boolean(target.closest(".tree-line"));
    // 两次点击自己判双击、而不是只听 dblclick：宿主会在第一次点击后重新渲染，
    // 行元素被换掉时浏览器可能就补不出 dblclick 事件了。
    if (onName && this.lastClickId === id && now - this.lastClickAt < DOUBLE_CLICK_MS) {
      this.lastClickId = null;
      this._startRename(id);
      return;
    }
    this.lastClickId = id;
    this.lastClickAt = now;
    this._select(operation, true);
  }

  _select(operation, fireEvent) {
    this.selectedId = String(operation.id);
    this._applySelection();
    if (fireEvent) this.onSelect(operation);
  }

  _applySelection() {
    for (const [id, row] of this.rows) row.classList.toggle("active", id === this.selectedId);
  }

  _startRename(id) {
    if (this.renamingId === id) return;
    this.renamingId = id;
    this.renameFocusPending = true;
    this.render();
  }

  _commitRename(id, value) {
    // 回车提交后浏览器还会补一次 blur，靠这个判断挡掉第二次回调。
    if (this.renamingId !== id) return;
    this.renamingId = null;
    this.renameFocusPending = false;
    this.render();
    const name = String(value).trim();
    // 空名字视作取消：后端不接受空名称，这里先把它拦下。
    if (!name) return;
    const operation = this._find(id);
    if (operation && operation.name === name) return;
    this.onChange(id, { name });
  }

  _cancelRename() {
    if (!this.renamingId) return;
    this.renamingId = null;
    this.renameFocusPending = false;
    this.render();
  }

  _find(id) {
    return this.operations.find((operation) => String(operation.id) === String(id)) || null;
  }
}
