// 通用界面零件：模态框、提示条、不确定进度条、模型导入对话框。
//
// 这里全是纯 DOM 工具，不含业务状态：调用方（main.js / cam-panel.js）负责提供节点与回调。
// 放进同一个文件，是因为导入对话框要同时用到模态框、提示条和进度条，生命周期绑在一起。

const INFO_BANNER_MS = 4000; // info 类提示默认停留多久：成功消息不需要用户手动关
const PROGRESS_TARGET = 90; // 不确定进度最多走到 90%，剩下的留给 stop() 冲线
const PROGRESS_TICK_MS = 220; // 每步的间隔，配合 CSS transition 就是平滑的假进度
const PROGRESS_HIDE_MS = 260; // 冲到 100% 后停一下再隐藏，否则看着"没走完"

/** body / footer 允许是单节点或节点数组，这里统一成数组喂给 replaceChildren。 */
function asNodes(value) {
  if (value === null || value === undefined) return [];
  return Array.isArray(value) ? value : [value];
}

/** 人类可读的字节数：提示条和文件信息都用它，避免各处各写一份。 */
function formatSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1024 / 1024).toFixed(2) + " MB";
}

// ------------------------------------------------------------------ 模态框
export class Modal {
  /**
   * @param {{ root: HTMLElement }} options root 即页面里的 #modal。
   */
  constructor({ root }) {
    this.root = root;
    // 三个插槽一次性查好；index.html 的结构是固定的。
    this.titleNode = root.querySelector("#modal-title");
    this.bodyNode = root.querySelector("#modal-body");
    this.footNode = root.querySelector("#modal-foot");
    this.closeButton = root.querySelector("#modal-close");
    this.onClose = null;

    // Esc 只在打开期间监听：常驻的 window 监听器会和视口、播放条抢按键。
    this.handleKeydown = (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      this.close();
    };
    // 只有点在遮罩本身（.modal）才算点背景；点 .modal-box 内部是正常操作。
    root.addEventListener("click", (event) => {
      if (event.target === this.root) this.close();
    });
    if (this.closeButton) this.closeButton.addEventListener("click", () => this.close());
  }

  /** 打开（或替换内容）。同一时刻只有一个模态框，重复 open 就是换内容。 */
  open({ title, body, footer, onClose } = {}) {
    this.onClose = typeof onClose === "function" ? onClose : null;
    // textContent / replaceChildren：外部传进来的都是节点，一律不做 HTML 解析。
    this.titleNode.textContent = title === null || title === undefined ? "" : String(title);
    this.bodyNode.replaceChildren(...asNodes(body));
    this.footNode.replaceChildren(...asNodes(footer));
    this.root.classList.remove("hidden");
    // 先移除再添加，重复 open 不会叠加监听器。
    window.removeEventListener("keydown", this.handleKeydown);
    window.addEventListener("keydown", this.handleKeydown);
  }

  /** 隐藏并触发 onClose。已经关掉时直接返回，避免回调里再 close 一次被触发两遍。 */
  close() {
    if (!this.isOpen()) return;
    this.root.classList.add("hidden");
    window.removeEventListener("keydown", this.handleKeydown);
    const callback = this.onClose;
    this.onClose = null; // 先清空再回调，回调里重新 open 不会被这次的收尾影响
    if (callback) callback();
  }

  isOpen() {
    return !this.root.classList.contains("hidden");
  }
}

// ------------------------------------------------------------------ 提示条
export class Banner {
  /**
   * @param {{ root: HTMLElement }} options root 即视口底部的 #banner。
   */
  constructor({ root }) {
    this.root = root;
    this.timer = 0;
  }

  show(message, kind = "error", timeoutMs = 0) {
    window.clearTimeout(this.timer);
    this.root.textContent = message;
    // 直接写 className 就是"重置 + 去掉 hidden"，不必再单独 classList.remove。
    this.root.className = "banner" + (kind === "info" ? " info" : "");
    let delay = Number(timeoutMs) > 0 ? Number(timeoutMs) : 0;
    if (delay === 0 && kind === "info") delay = INFO_BANNER_MS;
    if (delay > 0) {
      this.timer = window.setTimeout(() => this.hide(), delay);
    } else {
      this.timer = 0; // 错误提示留在屏幕上，等下次 show 或 hide 覆盖
    }
  }

  hide() {
    window.clearTimeout(this.timer);
    this.timer = 0;
    this.root.classList.add("hidden");
  }
}

// -------------------------------------------------------------- 不确定进度条
export class Progress {
  /**
   * @param {{ root: HTMLElement }} options root 即视口里的 #progress。
   */
  constructor({ root }) {
    this.root = root;
    this.bar = root.querySelector("span");
    if (!this.bar) {
      // 兼容脱离 index.html 固定结构使用的情况：没有填充条就补一个。
      this.bar = document.createElement("span");
      root.appendChild(this.bar);
    }
    this.bar.classList.add("progress-fill");
    // 文字单独一层：填充条可能只有 0% 宽，文字放进去会被裁掉。
    this.text = root.querySelector(".progress-text");
    if (!this.text) {
      this.text = document.createElement("span");
      this.text.className = "progress-text";
      root.appendChild(this.text);
    }
    this.timer = 0;
    this.hideTimer = 0;
    this.value = 0;
  }

  start(message) {
    // 幂等：先把上一次的定时器撤掉，重复 start 不会出现两条进度并行。
    this._stopTick();
    window.clearTimeout(this.hideTimer);
    this.value = 0;
    this.bar.style.width = "0%";
    this.text.textContent = message === null || message === undefined ? "" : String(message);
    this.root.classList.remove("hidden");
    // 每次补上剩余距离的一小部分，越接近 90% 越慢，看起来像真的在推进。
    this.timer = window.setInterval(() => {
      this.value += (PROGRESS_TARGET - this.value) * 0.18;
      if (PROGRESS_TARGET - this.value < 0.5) this.value = PROGRESS_TARGET;
      this.bar.style.width = this.value.toFixed(1) + "%";
    }, PROGRESS_TICK_MS);
  }

  stop() {
    this._stopTick();
    window.clearTimeout(this.hideTimer);
    this.value = 100;
    this.bar.style.width = "100%"; // 由 CSS transition 补上最后一段动画
    // 延迟隐藏：先让用户看见 100%，同时给下一次 start 留出干净的起点。
    this.hideTimer = window.setTimeout(() => {
      this.root.classList.add("hidden");
      this.value = 0;
      this.bar.style.width = "0%";
    }, PROGRESS_HIDE_MS);
  }

  _stopTick() {
    window.clearInterval(this.timer);
    this.timer = 0;
  }
}

// ---------------------------------------------------------- 模型导入对话框
/**
 * 打开"导入模型"对话框。
 *
 * @param {object} options
 * @param {Modal} options.modal 复用的模态框实例。
 * @param {string[]} options.suffixes 允许的后缀，例如 [".step", ".stp"]。
 * @param {number} options.maxBytes 单文件体积上限（字节）。
 * @param {(file: File) => void} options.onFile 校验通过后回调。
 * @param {(message: string) => void} options.onError 校验失败时回调（一般是 Banner）。
 */
export function openImportDialog({ modal, suffixes, maxBytes, onFile, onError }) {
  // 每次调用都重新建节点，所以"重置状态"是天然的：上一次的文件引用随闭包一起丢掉。
  const extensions = (Array.isArray(suffixes) && suffixes.length ? suffixes : [".step", ".stp"]).map(
    (item) => String(item).toLowerCase()
  );
  const limit = Number(maxBytes) > 0 ? Number(maxBytes) : 0;
  const maxMb = limit / (1024 * 1024);
  const maxMbText = Number.isInteger(maxMb) ? String(maxMb) : maxMb.toFixed(1);
  let file = null;

  // 错误行常驻在 body 里占位，避免出现/消失时对话框高度跳动。
  const error = document.createElement("div");
  error.className = "modal-error";

  // 校验失败：对话框内的错误行给用户看，onError 交给调用方（通常是提示条）。
  const fail = (message) => {
    error.textContent = message;
    if (typeof onError === "function") onError(message);
  };

  const input = document.createElement("input");
  input.type = "file";
  input.accept = extensions.join(",");
  input.className = "drop-input";

  const zone = document.createElement("div");
  zone.className = "drop-zone";
  const zoneText = document.createElement("div");
  zoneText.textContent = "把模型文件拖到这里，或点击选择";
  const hint = document.createElement("div");
  hint.className = "drop-hint";
  hint.textContent =
    "支持 " + extensions.join(" / ") + (limit > 0 ? "，单个文件不超过 " + maxMbText + " MB" : "");
  // input 铺满整个拖放区、只把视觉藏起来，用户的点击就直接落在 input 上。
  // 不能只靠 input.click()：Chromium 要求选择框必须由"用户激活"触发，
  // 程序化调用一旦拿不到激活就会被拒绝（控制台报
  // "File chooser dialog can only be shown with a user activation."），
  // 界面上就表现为"点了没反应"。直接把 input 当点击目标最稳。
  zone.append(input, zoneText, hint);

  const info = document.createElement("div");
  info.className = "drop-file";
  info.textContent = "尚未选择文件";

  const pick = document.createElement("button");
  pick.type = "button";
  pick.className = "button";
  pick.textContent = "选择文件";
  // 这里是真实用户点击，本身就带用户激活，input.click() 是允许的。
  pick.addEventListener("click", () => input.click());

  const setFile = (next) => {
    if (!next) return;
    file = next;
    info.textContent = next.name + "（" + formatSize(next.size) + "）";
    error.textContent = "";
  };

  input.addEventListener("change", () => {
    const picked = input.files && input.files.length ? input.files[0] : null;
    // 清空 input：File 对象已经留在闭包里，这样重复选同一个文件也会再触发 change。
    input.value = "";
    setFile(picked);
  });

  // 不阻止默认行为的话，浏览器会直接打开被拖进来的文件、离开当前页面。
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("drop-active");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("drop-active"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("drop-active");
    const dropped = event.dataTransfer && event.dataTransfer.files ? event.dataTransfer.files[0] : null;
    setFile(dropped || null);
  });

  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "button";
  cancel.textContent = "取消";
  cancel.addEventListener("click", () => modal.close());

  const submit = () => {
    if (!file) {
      fail("请先选择要导入的模型文件");
      return;
    }
    const name = String(file.name || "").toLowerCase();
    if (!extensions.some((ext) => name.endsWith(ext))) {
      fail("只支持 " + extensions.join(" / ") + " 文件");
      return;
    }
    if (limit > 0 && file.size > limit) {
      fail("文件过大：" + formatSize(file.size) + "，上限 " + maxMbText + " MB");
      return;
    }
    // 先关对话框再回调：回调里往往会弹提示条或进度条，不能被子窗口盖住。
    modal.close();
    if (typeof onFile === "function") onFile(file);
  };

  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.className = "button primary";
  confirm.textContent = "导入";
  confirm.addEventListener("click", submit);

  modal.open({
    title: "导入模型",
    // input 已经铺在拖放区里面（见上），这里**不能再单独列出**它——
    // 否则 replaceChildren 会把它从拖放区里挪走，点击区就没法选文件了。
    body: [zone, pick, info, error],
    footer: [cancel, confirm],
  });
}
