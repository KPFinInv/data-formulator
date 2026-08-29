import base64
import json
import mimetypes
import sys
import traceback
from pathlib import Path

import keyring
import requests
from ddgs import DDGS
from pypdf import PdfReader
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QCheckBox, QDialog, QDialogButtonBox,
    QFormLayout, QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
)

APP_NAME = "Dots3 Desktop"
KEYRING_SERVICE = "Dots3Desktop"
KEYRING_USER = "OpenRouterAPIKey"
DEFAULT_MODEL = "dots-studio/dots-3-note-preview:free"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".py", ".sql", ".xml", ".yaml", ".yml",
    ".html", ".htm", ".js", ".ts", ".tsx", ".jsx", ".css", ".ini", ".log"
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def safe_read_text(path: Path, max_chars: int = 120_000) -> str:
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return text[:max_chars]
    return path.read_text(encoding="utf-8", errors="replace")[:max_chars]


def to_data_url(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if not mime:
        mime = "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


class SettingsDialog(QDialog):
    def __init__(self, parent=None, model=DEFAULT_MODEL):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(520, 150)
        layout = QFormLayout(self)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setText(keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or "")
        self.model = QLineEdit(model)
        layout.addRow("OpenRouter API key", self.api_key)
        layout.addRow("Model", self.model)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def save(self):
        key = self.api_key.text().strip()
        if key:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
        return self.model.text().strip() or DEFAULT_MODEL


class ChatWorker(QThread):
    done = Signal(str)
    failed = Signal(str)
    status = Signal(str)

    def __init__(self, messages, model, api_key, agent_mode=False, workspace=None):
        super().__init__()
        self.messages = messages
        self.model = model
        self.api_key = api_key
        self.agent_mode = agent_mode
        self.workspace = Path(workspace).resolve() if workspace else None
        self.cancelled = False

    def request(self, messages, tools=None):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost/dots3-desktop",
            "X-Title": APP_NAME,
        }
        payload = {"model": self.model, "messages": messages, "temperature": 0.2}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        response = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=180)
        if not response.ok:
            raise RuntimeError(f"OpenRouter error {response.status_code}: {response.text[:1000]}")
        return response.json()["choices"][0]["message"]

    def tools_schema(self):
        return [
            {"type": "function", "function": {
                "name": "web_search", "description": "Search the public web for current information.",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
            }},
            {"type": "function", "function": {
                "name": "list_workspace_files", "description": "List files inside the selected local workspace folder.",
                "parameters": {"type": "object", "properties": {"relative_path": {"type": "string", "default": "."}}}
            }},
            {"type": "function", "function": {
                "name": "read_workspace_file", "description": "Read a text or PDF file inside the selected workspace folder.",
                "parameters": {"type": "object", "properties": {"relative_path": {"type": "string"}}, "required": ["relative_path"]}
            }},
        ]

    def _workspace_path(self, rel: str) -> Path:
        if not self.workspace:
            raise RuntimeError("No workspace folder selected.")
        candidate = (self.workspace / rel).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError:
            raise RuntimeError("Access denied: path is outside the selected workspace.")
        return candidate

    def run_tool(self, name, args):
        if name == "web_search":
            self.status.emit("Searching the web...")
            query = str(args.get("query", "")).strip()
            if not query:
                return "No query provided."
            results = list(DDGS().text(query, max_results=6))
            compact = [{"title": r.get("title"), "href": r.get("href"), "body": r.get("body")} for r in results]
            return json.dumps(compact, ensure_ascii=False)
        if name == "list_workspace_files":
            rel = args.get("relative_path", ".") or "."
            p = self._workspace_path(rel)
            if not p.exists():
                return "Path does not exist."
            if p.is_file():
                return str(p.relative_to(self.workspace))
            items = []
            for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:200]:
                marker = "[DIR]" if child.is_dir() else "[FILE]"
                items.append(f"{marker} {child.relative_to(self.workspace)}")
            return "\n".join(items)
        if name == "read_workspace_file":
            rel = args.get("relative_path", "")
            p = self._workspace_path(rel)
            if not p.exists() or not p.is_file():
                return "File does not exist."
            if p.suffix.lower() not in TEXT_EXTENSIONS | {".pdf"}:
                return "Unsupported file type for text reading."
            return safe_read_text(p, max_chars=100_000)
        return f"Unknown tool: {name}"

    def run(self):
        try:
            self.status.emit("Thinking...")
            if not self.agent_mode:
                reply = self.request(self.messages)
                self.done.emit(reply.get("content") or "")
                return
            messages = list(self.messages)
            tools = self.tools_schema()
            for _ in range(8):
                if self.cancelled:
                    self.failed.emit("Stopped.")
                    return
                reply = self.request(messages, tools=tools)
                messages.append(reply)
                tool_calls = reply.get("tool_calls") or []
                if not tool_calls:
                    self.done.emit(reply.get("content") or "")
                    return
                for call in tool_calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    try:
                        output = self.run_tool(name, args)
                    except Exception as e:
                        output = f"Tool error: {e}"
                    messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": output})
            self.done.emit("Agent stopped after reaching the tool-step limit. Please continue with a narrower request.")
        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc(limit=2)}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1040, 760)
        self.model = DEFAULT_MODEL
        self.workspace = None
        self.attachments = []
        self.messages = [{"role": "system", "content": (
            "You are Dots3 Desktop, a capable assistant. Be concise but complete. "
            "When Agent Mode is enabled, use tools when they materially improve the answer. "
            "Never claim to have read a local file unless a tool or attachment supplied its content."
        )}]
        self.worker = None
        self.build_ui()

    def build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        top = QHBoxLayout()
        title = QLabel("Dots3 Desktop")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.DemiBold))
        top.addWidget(title)
        top.addStretch(1)
        self.agent_mode = QCheckBox("Agent Mode")
        top.addWidget(self.agent_mode)
        self.workspace_btn = QPushButton("Choose Workspace")
        self.workspace_btn.clicked.connect(self.choose_workspace)
        top.addWidget(self.workspace_btn)
        settings_btn = QPushButton("Settings")
        settings_btn.clicked.connect(self.open_settings)
        top.addWidget(settings_btn)
        outer.addLayout(top)
        self.workspace_label = QLabel("Workspace: none")
        self.workspace_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.workspace_label)
        self.chat = QTextBrowser()
        self.chat.setOpenExternalLinks(True)
        self.chat.setStyleSheet("QTextBrowser { padding: 12px; font-size: 14px; }")
        outer.addWidget(self.chat, 1)
        self.attachment_label = QLabel("No attachments")
        outer.addWidget(self.attachment_label)
        self.input = QTextEdit()
        self.input.setPlaceholderText("Ask dots3 anything...")
        self.input.setMaximumHeight(140)
        outer.addWidget(self.input)
        actions = QHBoxLayout()
        attach_btn = QPushButton("Attach Files")
        attach_btn.clicked.connect(self.attach_files)
        actions.addWidget(attach_btn)
        clear_attach_btn = QPushButton("Clear Attachments")
        clear_attach_btn.clicked.connect(self.clear_attachments)
        actions.addWidget(clear_attach_btn)
        reset_btn = QPushButton("New Chat")
        reset_btn.clicked.connect(self.reset_chat)
        actions.addWidget(reset_btn)
        actions.addStretch(1)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_worker)
        actions.addWidget(self.stop_btn)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send_message)
        actions.addWidget(self.send_btn)
        outer.addLayout(actions)
        self.statusBar().showMessage("Ready")
        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(self.open_settings)
        self.menuBar().addAction(settings_action)
        self.render_welcome()

    def render_welcome(self):
        self.chat.setHtml("<h2>Welcome to Dots3 Desktop</h2><p>Connect your OpenRouter API key in <b>Settings</b>, then chat normally.</p><p><b>Agent Mode</b> can search the web and inspect files inside a workspace folder you choose.</p>")

    def open_settings(self):
        dlg = SettingsDialog(self, self.model)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.model = dlg.save()
            self.statusBar().showMessage(f"Model: {self.model}")

    def choose_workspace(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose workspace folder")
        if folder:
            self.workspace = folder
            self.workspace_label.setText(f"Workspace: {folder}")

    def attach_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Attach files", "", "Supported (*.txt *.md *.csv *.json *.py *.sql *.pdf *.png *.jpg *.jpeg *.webp);;All files (*.*)")
        for f in files:
            p = Path(f)
            if p.suffix.lower() in TEXT_EXTENSIONS | IMAGE_EXTENSIONS | {".pdf"} and f not in self.attachments:
                self.attachments.append(f)
        self.update_attachment_label()

    def clear_attachments(self):
        self.attachments = []
        self.update_attachment_label()

    def update_attachment_label(self):
        if not self.attachments:
            self.attachment_label.setText("No attachments")
        else:
            self.attachment_label.setText("Attachments: " + ", ".join(Path(x).name for x in self.attachments))

    def make_user_content(self, text):
        content = [{"type": "text", "text": text}]
        for f in self.attachments:
            p = Path(f)
            ext = p.suffix.lower()
            try:
                if ext in IMAGE_EXTENSIONS:
                    content.append({"type": "image_url", "image_url": {"url": to_data_url(p)}})
                elif ext in TEXT_EXTENSIONS | {".pdf"}:
                    extracted = safe_read_text(p)
                    content.append({"type": "text", "text": f"\n--- Attached file: {p.name} ---\n{extracted}\n--- End file ---"})
            except Exception as e:
                content.append({"type": "text", "text": f"Could not read attachment {p.name}: {e}"})
        return content

    def append_chat(self, who, text):
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        self.chat.append(f"<p><b>{who}</b><br>{safe}</p>")

    def send_message(self):
        text = self.input.toPlainText().strip()
        if not text:
            return
        api_key = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if not api_key:
            QMessageBox.information(self, APP_NAME, "Add your OpenRouter API key in Settings first.")
            self.open_settings()
            return
        if self.agent_mode.isChecked() and not self.workspace:
            answer = QMessageBox.question(self, APP_NAME, "Agent Mode works best with a workspace folder. Continue with web search only?")
            if answer != QMessageBox.StandardButton.Yes:
                self.choose_workspace()
        content = self.make_user_content(text)
        self.messages.append({"role": "user", "content": content})
        self.append_chat("You", text)
        if self.attachments:
            self.chat.append(f"<p><i>Attached: {', '.join(Path(x).name for x in self.attachments)}</i></p>")
        self.input.clear()
        self.attachments = []
        self.update_attachment_label()
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.statusBar().showMessage("Working...")
        self.worker = ChatWorker(list(self.messages), self.model, api_key, self.agent_mode.isChecked(), self.workspace)
        self.worker.status.connect(self.statusBar().showMessage)
        self.worker.done.connect(self.on_reply)
        self.worker.failed.connect(self.on_error)
        self.worker.start()

    def on_reply(self, text):
        self.messages.append({"role": "assistant", "content": text})
        self.append_chat("Dots3", text)
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.statusBar().showMessage("Ready")
        self.worker = None

    def on_error(self, error):
        self.append_chat("Error", error)
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.statusBar().showMessage("Error")
        self.worker = None

    def stop_worker(self):
        if self.worker:
            self.worker.cancelled = True
            self.stop_btn.setEnabled(False)
            self.statusBar().showMessage("Stopping after current network request...")

    def reset_chat(self):
        self.messages = [self.messages[0]]
        self.attachments = []
        self.update_attachment_label()
        self.render_welcome()
        self.statusBar().showMessage("New chat")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
