// All dismissible notices share a real keyboard-accessible close button.
// Update the text without replacing the button while a live collision is moving.
export function renderNotice(element, message, onClose, label = "关闭警告", role = "alert") {
  let text = element.querySelector(".notice-message");
  let close = element.querySelector(".notice-close");
  if (!text || !close) {
    text = document.createElement("span");
    text.className = "notice-message";
    close = document.createElement("button");
    close.type = "button";
    close.className = "notice-close";
    close.textContent = "×";
    close.addEventListener("pointerdown", event => event.stopPropagation());
    element.replaceChildren(text, close);
  }
  text.textContent = message;
  close.setAttribute("aria-label", label);
  close.title = label;
  close.onclick = event => {
    event.preventDefault();
    event.stopPropagation();
    onClose();
  };
  element.setAttribute("role", role);
}
