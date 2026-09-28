// 声明式参数控件的共用实现。
//
// 后端为每个能力发布一份 ParameterSpec（kind / min / max / step / unit / choices），
// 前端据此渲染控件。原来的 panel.js 里这套逻辑是内联的；CAM 面板需要同一套东西，
// 因此抽到这里，两边共用一份实现——避免"参数面板长了两副面孔"。

export function formatNumber(value) {
  if (typeof value !== "number") return String(value);
  return Number.isInteger(value) ? String(value) : String(Math.round(value * 1000) / 1000);
}

export function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

/** 某能力的默认参数值。 */
export function defaultsOf(item) {
  const values = {};
  for (const spec of (item && item.parameters) || []) values[spec.key] = spec.default;
  return values;
}

/** 切换能力（形状 / 策略 / 加工类型）时尽量保留同名参数。 */
export function carryOver(previous, item) {
  const values = defaultsOf(item);
  for (const spec of (item && item.parameters) || []) {
    if (previous && Object.prototype.hasOwnProperty.call(previous, spec.key)) {
      values[spec.key] = previous[spec.key];
    }
  }
  return values;
}

/**
 * 生成一个参数控件。
 * 返回 { node, slider, setValue }：slider 是可选滑块（需要单独占一行），
 * setValue 用于程序化刷新（例如切换工序时把旧控件更新成新值）。
 */
export function buildControl(spec, value, onChange) {
  if (spec.kind === "bool") {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
    input.addEventListener("change", () => onChange(input.checked));
    return { node: input, setValue: (next) => { input.checked = Boolean(next); } };
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
    return { node: select, setValue: (next) => { select.value = String(next); } };
  }
  if (spec.kind === "string") {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value === null || value === undefined ? "" : String(value);
    input.addEventListener("change", () => onChange(input.value));
    return { node: input, setValue: (next) => { input.value = String(next ?? ""); } };
  }
  return buildNumberControl(spec, Number(value), onChange);
}

/** 数值控件：数字框 + （有范围时）滑块。 */
export function buildNumberControl(spec, value, onChange) {
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
  const setValue = (nextValue) => {
    number.value = formatNumber(Number(nextValue));
    if (slider) slider.value = String(Number(nextValue));
  };
  return { node: wrapper, slider, setValue };
}

/**
 * 按分组渲染一组参数控件。
 * `specs` 是 ParameterSpec 数组，`values` 是可变对象，改动会写回并触发 onChange。
 */
export function renderParameterRows(container, specs, values, onChange, options = {}) {
  const rows = [];
  for (const spec of specs) {
    const row = document.createElement("div");
    row.className = "row";
    row.dataset.key = spec.key;
    const label = document.createElement("label");
    label.textContent = spec.label;
    if (spec.help) label.title = spec.help;
    const control = buildControl(spec, values[spec.key], (value) => {
      values[spec.key] = value;
      if (options.onVisibility) options.onVisibility();
      onChange();
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
    container.appendChild(row);
    rows.push({ row, spec, control });
  }
  return rows;
}

/** 按 `visible_if` 规则刷新行的显隐。 */
export function refreshVisibility(rows, values) {
  for (const entry of rows) {
    const rules = entry.spec.visible_if;
    if (!rules || Object.keys(rules).length === 0) continue;
    let visible = true;
    for (const [key, expected] of Object.entries(rules)) {
      if (String(values[key]) !== String(expected)) visible = false;
    }
    entry.row.hidden = !visible;
  }
}
