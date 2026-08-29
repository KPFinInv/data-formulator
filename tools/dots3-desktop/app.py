import base64
import json
import mimetypes
import os
import sys
import traceback
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path

import keyring
import requests
from ddgs import DDGS
from pypdf import PdfReader
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QAction, QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMainWindow, QMessageBox, QPushButton, QCheckBox, QDialog,
    QDialogButtonBox, QFormLayout, QTextBrowser, QTextEdit, QVBoxLayout,
    QWidget, QSplitter, QMenu
)

APP_NAME = "Dots3 Desktop"
APP_VERSION = "1.2.0"
KEYRING_SERVICE = "Dots3Desktop"
KEYRING_USER = "OpenRouterAPIKey"
DEFAULT_MODEL = "dots-studio/dots-3-note-preview:free"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
RELEASES_API = "https://api.github.com/repos/KPFinInv/data-formulator/releases"
RELEASE_TAG_PREFIX = "dots3-desktop-v"

TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".py", ".sql", ".xml", ".yaml", ".yml", ".html", ".htm", ".js", ".ts", ".tsx", ".jsx", ".css", ".ini", ".log"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
SYSTEM_PROMPT = (
    "You are Dots3 Desktop, a capable assistant. Be concise but complete. "
    "When Agent Mode is enabled, use tools when they materially improve the answer. "
    "Never claim to have read a local file unless a tool or attachment supplied its content."
)


def app_data_dir() -> Path:
    root = Path(os.getenv("APPDATA") or Path.home() / ".dots3-desktop")
    path = root / "Dots3Desktop"
    path.mkdir(parents=True, exist_ok=True)
    return path

HISTORY_FILE = app_data_dir() / "history.json"


def safe_read_text(path: Path, max_chars: int = 120_000) -> str:
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)[:max_chars]
    return path.read_text(encoding="utf-8", errors="replace")[:max_chars]


def to_data_url(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime or 'image/png'};base64,{data}"


def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")


def version_tuple(v: str):
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0,)


class SettingsDialog(QDialog):
    def __init__(self, parent=None, model=DEFAULT_MODEL):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(560, 180)
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


class UpdateWorker(QThread):
    found = Signal(str, str)
    none = Signal()

    def run(self):
        try:
            r = requests.get(RELEASES_API, timeout=12)
            r.raise_for_status()
            for rel in r.json():
                tag = rel.get("tag_name", "")
                if not tag.startswith(RELEASE_TAG_PREFIX):
                    continue
                ver = tag[len(RELEASE_TAG_PREFIX):]
                if version_tuple(ver) > version_tuple(APP_VERSION):
                    asset_url = rel.get("html_url", "")
                    for asset in rel.get("assets", []):
                        if asset.get("name", "").lower().endswith("setup.exe"):
                            asset_url = asset.get("browser_download_url", asset_url)
                            break
                    self.found.emit(ver, asset_url)
                else:
                    self.none.emit()
                return
            self.none.emit()
        except Exception:
            self.none.emit()


class ChatWorker(QThread):
    done = Signal(str)
    failed = Signal(str)
    status = Signal(str)
    activity = Signal(str)

    def __init__(self, messages, model, api_key, agent_mode=False, workspace=None):
        super().__init__()
        self.messages = messages
        self.model = model
        self.api_key = api_key
        self.agent_mode = agent_mode
        self.workspace = Path(workspace).resolve() if workspace else None
        self.cancelled = False

    def request(self, messages, tools=None):
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "HTTP-Referer": "https://localhost/dots3-desktop", "X-Title": APP_NAME}
        payload = {"model": self.model, "messages": messages, "temperature": 0.2}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        r = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=180)
        if not r.ok:
            raise RuntimeError(f"OpenRouter error {r.status_code}: {r.text[:1000]}")
        return r.json()["choices"][0]["message"]

    def tools_schema(self):
        return [
            {"type": "function", "function": {"name": "web_search", "description": "Search the public web for current information.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
            {"type": "function", "function": {"name": "list_workspace_files", "description": "List files inside the selected local workspace folder.", "parameters": {"type": "object", "properties": {"relative_path": {"type": "string", "default": "."}}}}},
            {"type": "function", "function": {"name": "read_workspace_file", "description": "Read a text or PDF file inside the selected workspace folder.", "parameters": {"type": "object", "properties": {"relative_path": {"type": "string"}}, "required": ["relative_path"]}}},
        ]

    def _workspace_path(self, rel: str) -> Path:
        if not self.workspace:
            raise RuntimeError("No workspace folder selected.")
        p = (self.workspace / rel).resolve()
        try:
            p.relative_to(self.workspace)
        except ValueError:
            raise RuntimeError("Access denied: path is outside the selected workspace.")
        return p

    def run_tool(self, name, args):
        if name == "web_search":
            query = str(args.get("query", "")).strip()
            self.activity.emit(f"Web search: {query}")
            results = list(DDGS().text(query, max_results=6)) if query else []
            return json.dumps([{"title": r.get("title"), "href": r.get("href"), "body": r.get("body")} for r in results], ensure_ascii=False)
        if name == "list_workspace_files":
            rel = args.get("relative_path", ".") or "."
            self.activity.emit(f"Listing workspace: {rel}")
            p = self._workspace_path(rel)
            if not p.exists(): return "Path does not exist."
            if p.is_file(): return str(p.relative_to(self.workspace))
            return "\n".join(("[DIR] " if c.is_dir() else "[FILE] ") + str(c.relative_to(self.workspace)) for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:200])
        if name == "read_workspace_file":
            rel = args.get("relative_path", "")
            self.activity.emit(f"Reading file: {rel}")
            p = self._workspace_path(rel)
            if not p.exists() or not p.is_file(): return "File does not exist."
            if p.suffix.lower() not in TEXT_EXTENSIONS | {".pdf"}: return "Unsupported file type for text reading."
            return safe_read_text(p, 100_000)
        return f"Unknown tool: {name}"

    def run(self):
        try:
            self.status.emit("Thinking...")
            if not self.agent_mode:
                self.done.emit(self.request(self.messages).get("content") or "")
                return
            messages, tools = list(self.messages), self.tools_schema()
            for step in range(10):
                if self.cancelled:
                    self.failed.emit("Stopped.")
                    return
                self.status.emit(f"Agent step {step + 1}/10")
                reply = self.request(messages, tools)
                messages.append(reply)
                calls = reply.get("tool_calls") or []
                if not calls:
                    self.done.emit(reply.get("content") or "")
                    return
                for call in calls:
                    fn = call.get("function", {})
                    try: args = json.loads(fn.get("arguments") or "{}")
                    except Exception: args = {}
                    try: output = self.run_tool(fn.get("name", ""), args)
                    except Exception as e: output = f"Tool error: {e}"
                    messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": output})
            self.done.emit("Agent reached its step limit. Continue with a narrower follow-up if needed.")
        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc(limit=2)}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1220, 820)
        icon = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "dots3.ico"
        if icon.exists(): self.setWindowIcon(QIcon(str(icon)))
        self.model = DEFAULT_MODEL
        self.workspace = None
        self.attachments = []
        self.worker = None
        self.sessions = self.load_history()
        self.current_id = None
        self.build_ui()
        self.new_chat(save_old=False) if not self.sessions else self.load_session(next(iter(self.sessions)))
        QTimer.singleShot(3000, self.check_updates_silent)

    def load_history(self):
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else {}
            return data if isinstance(data, dict) else {}
        except Exception: return {}

    def save_history(self):
        HISTORY_FILE.write_text(json.dumps(self.sessions, ensure_ascii=False, indent=2), encoding="utf-8")

    def default_messages(self): return [{"role": "system", "content": SYSTEM_PROMPT}]

    def apply_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #0b1020; color: #eaf0ff; font-family: 'Segoe UI'; }
            QMenuBar { background: #11182b; color: #dce6ff; padding: 4px; }
            QMenuBar::item:selected, QMenu::item:selected { background: #26355d; }
            QMenu { background: #11182b; color: #eaf0ff; border: 1px solid #2a3658; }
            QLabel#brand { color: #ffffff; font-weight: 700; }
            QLabel#workspace { color: #8fb3ff; background: #101a33; border: 1px solid #24345d; border-radius: 8px; padding: 8px; }
            QLabel#activity { color: #7dd3fc; padding: 3px 2px; }
            QLabel#attachments { color: #c4b5fd; }
            QListWidget { background: #10172a; border: 1px solid #263453; border-radius: 12px; padding: 6px; }
            QListWidget::item { padding: 10px; margin: 2px; border-radius: 7px; }
            QListWidget::item:selected { background: #243a78; color: white; }
            QListWidget::item:hover { background: #18274d; }
            QTextBrowser { background: #0f172a; border: 1px solid #263453; border-radius: 14px; padding: 14px; color: #e8eefc; }
            QTextEdit, QLineEdit { background: #111827; color: #f8fafc; border: 1px solid #33446f; border-radius: 10px; padding: 9px; selection-background-color: #4f46e5; }
            QTextEdit:focus, QLineEdit:focus { border: 1px solid #7c8cff; }
            QPushButton { background: #1a2440; color: #eaf0ff; border: 1px solid #34466f; border-radius: 9px; padding: 8px 14px; font-weight: 600; }
            QPushButton:hover { background: #24345d; border-color: #6680d8; }
            QPushButton#primary { background: #6d5dfc; color: white; border: none; }
            QPushButton#primary:hover { background: #7f70ff; }
            QPushButton#newChat { background: #0ea5e9; color: white; border: none; }
            QPushButton#newChat:hover { background: #38bdf8; }
            QPushButton#danger { background: #3a1f2d; color: #fecdd3; border-color: #6f2c46; }
            QCheckBox { spacing: 8px; color: #dbeafe; font-weight: 700; }
            QCheckBox::indicator { width: 18px; height: 18px; border: 1px solid #5a6e9a; border-radius: 5px; background: #111827; }
            QCheckBox::indicator:checked { background: #22c55e; border-color: #22c55e; }
            QStatusBar { background: #10172a; color: #93c5fd; border-top: 1px solid #263453; }
            QSplitter::handle { background: #1b2848; width: 2px; }
        """)

    def build_ui(self):
        root = QWidget(); self.setCentralWidget(root)
        outer = QVBoxLayout(root); outer.setContentsMargins(14, 12, 14, 12); outer.setSpacing(10)
        toolbar = QHBoxLayout()
        brand = QLabel("✦  Dots3 Desktop"); brand.setObjectName("brand"); brand.setFont(QFont("Segoe UI", 17, QFont.Weight.Bold)); toolbar.addWidget(brand)
        toolbar.addStretch(1)
        self.agent_mode = QCheckBox("Agent Mode"); self.agent_mode.setChecked(True); toolbar.addWidget(self.agent_mode)
        ws = QPushButton("📁  Workspace"); ws.clicked.connect(self.choose_workspace); toolbar.addWidget(ws)
        st = QPushButton("⚙  Settings"); st.clicked.connect(self.open_settings); toolbar.addWidget(st)
        outer.addLayout(toolbar)

        split = QSplitter(Qt.Orientation.Horizontal)
        side = QWidget(); side_l = QVBoxLayout(side); side_l.setContentsMargins(0, 0, 8, 0)
        new_btn = QPushButton("＋  New Chat"); new_btn.setObjectName("newChat"); new_btn.clicked.connect(self.new_chat); side_l.addWidget(new_btn)
        self.history = QListWidget(); self.history.itemClicked.connect(lambda i: self.load_session(i.data(Qt.ItemDataRole.UserRole))); self.history.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu); self.history.customContextMenuRequested.connect(self.history_menu); side_l.addWidget(self.history, 1)
        split.addWidget(side)

        main = QWidget(); main_l = QVBoxLayout(main); main_l.setContentsMargins(8, 0, 0, 0); main_l.setSpacing(8)
        self.workspace_label = QLabel("Workspace: none"); self.workspace_label.setObjectName("workspace"); self.workspace_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); main_l.addWidget(self.workspace_label)
        self.chat = QTextBrowser(); self.chat.setOpenExternalLinks(True); main_l.addWidget(self.chat, 1)
        self.activity = QLabel(""); self.activity.setObjectName("activity"); main_l.addWidget(self.activity)
        self.attachment_label = QLabel("No attachments"); self.attachment_label.setObjectName("attachments"); main_l.addWidget(self.attachment_label)
        self.input = QTextEdit(); self.input.setPlaceholderText("Ask dots3 anything...  (Ctrl+Enter to send)"); self.input.setMaximumHeight(145); main_l.addWidget(self.input)
        row = QHBoxLayout()
        a = QPushButton("📎  Attach"); a.clicked.connect(self.attach_files); row.addWidget(a)
        ca = QPushButton("Clear Attachments"); ca.clicked.connect(self.clear_attachments); row.addWidget(ca)
        row.addStretch(1)
        self.stop_btn = QPushButton("Stop"); self.stop_btn.setObjectName("danger"); self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self.stop_worker); row.addWidget(self.stop_btn)
        self.send_btn = QPushButton("Send  ➜"); self.send_btn.setObjectName("primary"); self.send_btn.clicked.connect(self.send_message); row.addWidget(self.send_btn)
        main_l.addLayout(row); split.addWidget(main); split.setSizes([255, 965]); outer.addWidget(split, 1)
        self.statusBar().showMessage("Agent Mode ready")

        menu = self.menuBar().addMenu("App")
        act_new = QAction("New Chat", self); act_new.triggered.connect(self.new_chat); menu.addAction(act_new)
        act_settings = QAction("Settings", self); act_settings.triggered.connect(self.open_settings); menu.addAction(act_settings)
        act_update = QAction("Check for Updates", self); act_update.triggered.connect(lambda: self.check_updates(False)); menu.addAction(act_update)
        menu.addSeparator(); about = QAction("About", self); about.triggered.connect(lambda: QMessageBox.information(self, APP_NAME, f"{APP_NAME} v{APP_VERSION}\nPowered through OpenRouter + dots3.")); menu.addAction(about)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.send_message)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self.new_chat)
        self.apply_theme(); self.refresh_history()

    def refresh_history(self):
        self.history.clear()
        ordered = sorted(self.sessions.items(), key=lambda kv: kv[1].get("updated", ""), reverse=True)
        for sid, s in ordered:
            from PySide6.QtWidgets import QListWidgetItem
            item = QListWidgetItem(s.get("title", "New Chat")); item.setData(Qt.ItemDataRole.UserRole, sid); self.history.addItem(item)

    def history_menu(self, pos):
        item = self.history.itemAt(pos)
        if not item: return
        menu = QMenu(self); delete = menu.addAction("Delete Chat")
        if menu.exec(self.history.mapToGlobal(pos)) == delete:
            sid = item.data(Qt.ItemDataRole.UserRole); self.sessions.pop(sid, None); self.save_history(); self.refresh_history()
            if sid == self.current_id: self.new_chat(save_old=False)

    def persist_current(self):
        if not self.current_id: return
        s = self.sessions.setdefault(self.current_id, {})
        s.update({"messages": self.messages, "updated": datetime.utcnow().isoformat(), "model": self.model, "workspace": self.workspace})
        user_texts = [m.get("content") for m in self.messages if m.get("role") == "user"]
        if user_texts:
            first = user_texts[0]
            if isinstance(first, list): first = next((x.get("text", "") for x in first if x.get("type") == "text"), "")
            s["title"] = str(first).strip().replace("\n", " ")[:42] or "New Chat"
        else: s["title"] = "New Chat"
        self.save_history(); self.refresh_history()

    def new_chat(self, save_old=True):
        if save_old: self.persist_current()
        self.current_id = str(uuid.uuid4()); self.messages = self.default_messages(); self.sessions[self.current_id] = {"title":"New Chat","messages":self.messages,"updated":datetime.utcnow().isoformat(),"model":self.model,"workspace":self.workspace}; self.chat.clear(); self.render_welcome(); self.save_history(); self.refresh_history()

    def load_session(self, sid):
        self.persist_current(); s = self.sessions.get(sid)
        if not s: return
        self.current_id = sid; self.messages = s.get("messages", self.default_messages()); self.model = s.get("model", self.model); self.workspace = s.get("workspace") or None; self.workspace_label.setText(f"Workspace: {self.workspace or 'none'}")
        self.chat.clear()
        for m in self.messages:
            if m.get("role") not in ("user", "assistant"): continue
            c = m.get("content", "")
            if isinstance(c, list): c = next((x.get("text", "") for x in c if x.get("type") == "text"), "")
            self.append_chat("You" if m.get("role") == "user" else "Dots3", str(c))
        self.refresh_history()

    def render_welcome(self):
        self.chat.setHtml("<div style='padding:18px'><h2 style='color:#a5b4fc'>Welcome to Dots3 Desktop</h2><p style='font-size:14px'>Agent Mode is enabled by default, so dots3 can use web search and workspace tools when useful.</p><p><b style='color:#7dd3fc'>Tip:</b> choose a workspace folder to let the agent inspect only the files you explicitly allow.</p></div>")

    def append_chat(self, who, text):
        if who == "You":
            self.chat.append(f"<div style='margin:14px 0 14px 18%; padding:12px 14px; background:#243a78; border-radius:12px; color:#f8fbff'><b style='color:#bfdbfe'>You</b><br>{html_escape(text)}</div>")
        elif who == "Dots3":
            self.chat.append(f"<div style='margin:14px 18% 14px 0; padding:12px 14px; background:#172554; border:1px solid #314a92; border-radius:12px; color:#eef2ff'><b style='color:#c4b5fd'>✦ Dots3</b><br>{html_escape(text)}</div>")
        else:
            self.chat.append(f"<div style='margin:12px 0; padding:10px; background:#3f1d2e; border-radius:10px; color:#fecdd3'><b>{who}</b><br>{html_escape(text)}</div>")

    def open_settings(self):
        dlg = SettingsDialog(self, self.model)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.model = dlg.save(); self.persist_current(); self.statusBar().showMessage(f"Model: {self.model}")

    def choose_workspace(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose workspace folder")
        if folder: self.workspace = folder; self.workspace_label.setText(f"Workspace: {folder}"); self.persist_current()

    def attach_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Attach files", "", "Supported (*.txt *.md *.csv *.json *.py *.sql *.pdf *.png *.jpg *.jpeg *.webp);;All files (*.*)")
        for f in files:
            if Path(f).suffix.lower() in TEXT_EXTENSIONS | IMAGE_EXTENSIONS | {".pdf"} and f not in self.attachments: self.attachments.append(f)
        self.update_attachment_label()

    def clear_attachments(self): self.attachments=[]; self.update_attachment_label()
    def update_attachment_label(self): self.attachment_label.setText("Attachments: " + ", ".join(Path(x).name for x in self.attachments) if self.attachments else "No attachments")

    def make_user_content(self, text):
        content = [{"type":"text","text":text}]
        for f in self.attachments:
            p=Path(f); ext=p.suffix.lower()
            try:
                if ext in IMAGE_EXTENSIONS: content.append({"type":"image_url","image_url":{"url":to_data_url(p)}})
                elif ext in TEXT_EXTENSIONS | {".pdf"}: content.append({"type":"text","text":f"\n--- Attached file: {p.name} ---\n{safe_read_text(p)}\n--- End file ---"})
            except Exception as e: content.append({"type":"text","text":f"Could not read attachment {p.name}: {e}"})
        return content

    def send_message(self):
        text=self.input.toPlainText().strip()
        if not text or self.worker: return
        key=keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if not key: QMessageBox.information(self,APP_NAME,"Add your OpenRouter API key in Settings first."); self.open_settings(); return
        content=self.make_user_content(text); self.messages.append({"role":"user","content":content}); self.append_chat("You",text)
        if self.attachments: self.chat.append(f"<i>Attached: {', '.join(Path(x).name for x in self.attachments)}</i>")
        self.input.clear(); self.attachments=[]; self.update_attachment_label(); self.persist_current(); self.send_btn.setEnabled(False); self.stop_btn.setEnabled(True)
        self.worker=ChatWorker(list(self.messages),self.model,key,self.agent_mode.isChecked(),self.workspace); self.worker.status.connect(self.statusBar().showMessage); self.worker.activity.connect(self.activity.setText); self.worker.done.connect(self.on_reply); self.worker.failed.connect(self.on_error); self.worker.start()

    def on_reply(self,text): self.messages.append({"role":"assistant","content":text}); self.append_chat("Dots3",text); self.finish_worker("Ready"); self.persist_current()
    def on_error(self,error): self.append_chat("Error",error); self.finish_worker("Error")
    def finish_worker(self,status): self.send_btn.setEnabled(True); self.stop_btn.setEnabled(False); self.statusBar().showMessage(status); self.activity.setText(""); self.worker=None
    def stop_worker(self):
        if self.worker: self.worker.cancelled=True; self.stop_btn.setEnabled(False); self.statusBar().showMessage("Stopping after current request...")

    def check_updates_silent(self): self.check_updates(True)
    def check_updates(self, silent=False):
        self.update_worker=UpdateWorker(); self.update_worker.found.connect(self.update_found)
        if not silent: self.update_worker.none.connect(lambda: QMessageBox.information(self,APP_NAME,"You already have the latest Dots3 Desktop version."))
        self.update_worker.start()
    def update_found(self, version, url):
        if QMessageBox.question(self,APP_NAME,f"Dots3 Desktop v{version} is available. Open the installer download now?")==QMessageBox.StandardButton.Yes: webbrowser.open(url)

    def closeEvent(self,event): self.persist_current(); event.accept()


def main():
    app=QApplication(sys.argv); app.setApplicationName(APP_NAME); app.setOrganizationName("KPFinInv"); w=MainWindow(); w.show(); sys.exit(app.exec())

if __name__ == "__main__": main()
