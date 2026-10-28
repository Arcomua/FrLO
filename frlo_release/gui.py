"""Chinese desktop front end for the FrLO release commands."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

def find_root():
    if not getattr(sys, 'frozen', False):
        return Path(__file__).resolve().parents[1]
    executable_dir = Path(sys.executable).resolve().parent
    for path in (executable_dir, *executable_dir.parents):
        if (path / 'config' / 'project.json').is_file() and (path / 'release').is_dir():
            return path
    return executable_dir


ROOT = find_root()
PROJECT = ROOT / 'config' / 'project.json'
RELEASE = ROOT / 'release' / 'release.json'
NOTES = ROOT / 'release' / 'manual.md'
DEFAULT_PROJECT = {'name': 'FrLO', 'author': '', 'github_repository': 'Arcomua/FrLO', 'branch': 'main', 'modrinth_project_id': '64radtE0', 'environment': 'client_and_server', 'curseforge_project_id': ''}
DEFAULT_RELEASE = {'version': '', 'channel': 'beta'}
REPO_PATTERN = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')


def read_json(path, defaults):
    if not path.exists():
        return dict(defaults)
    with path.open(encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f'{path.name} 必须是 JSON 对象')
    return {**defaults, **data}


def atomic_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=path.parent, delete=False) as f:
            name = f.name
            f.write(content)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def write_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('FrLO 发布助手')
        self.geometry('1100x850')
        self.minsize(1000, 800)
        self.events = queue.Queue()
        self.busy = False
        self.buttons = []
        try:
            self.project = read_json(PROJECT, DEFAULT_PROJECT)
            self.release = read_json(RELEASE, DEFAULT_RELEASE)
            notes = NOTES.read_text(encoding='utf-8') if NOTES.exists() else ''
        except (OSError, ValueError) as e:
            messagebox.showerror('读取配置失败', str(e), parent=self)
            self.destroy()
            return
        self.vars = {k: tk.StringVar(value=str(v)) for k, v in {
            'file': '', 'version': self.release.get('version', ''), 'channel': self.release.get('channel', 'beta'),
            'github_repository': self.project.get('github_repository', ''), 'modrinth_project_id': self.project.get('modrinth_project_id', ''),
            'curseforge_project_id': self.project.get('curseforge_project_id', ''), 'MODRINTH_TOKEN': '', 'CURSEFORGE_TOKEN': '', 'CF_API_KEY': ''}.items()}
        self.targets = {'github': tk.BooleanVar(value=True), 'modrinth': tk.BooleanVar(value=True), 'curseforge': tk.BooleanVar(value=bool(self.vars['curseforge_project_id'].get()))}
        self.create_widgets(notes)
        self.after(100, self.poll)

    def create_widgets(self, notes):
        style = ttk.Style(self)
        if 'clam' in style.theme_names(): style.theme_use('clam')
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 19, 'bold'))
        style.configure('Subtitle.TLabel', foreground='#5c6775')
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='FrLO 发布助手', style='Title.TLabel').pack(anchor='w')
        ttk.Label(outer, text='导入整合包，整理更新说明，然后选择本地或云端发布。', style='Subtitle.TLabel').pack(anchor='w', pady=(2, 10))
        form = ttk.LabelFrame(outer, text='发行信息', padding=10)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1); form.columnconfigure(3, weight=1)
        self.field(form, 'Prism 导出文件', 'file', 0, 0, span=3)
        self.button(form, '浏览…', self.browse).grid(row=0, column=4, padx=(8, 0), sticky='ew')
        self.field(form, '版本', 'version', 1, 0)
        ttk.Label(form, text='发布渠道').grid(row=1, column=2, padx=(16, 6), pady=4, sticky='w')
        ttk.Combobox(form, textvariable=self.vars['channel'], values=('release', 'beta', 'alpha'), state='readonly', width=18).grid(row=1, column=3, sticky='ew', pady=4)
        self.field(form, 'GitHub 仓库', 'github_repository', 2, 0)
        self.field(form, 'Modrinth 项目 ID', 'modrinth_project_id', 2, 2)
        self.field(form, 'CurseForge 项目 ID', 'curseforge_project_id', 3, 0)
        target_frame = ttk.Frame(form); target_frame.grid(row=3, column=2, columnspan=3, sticky='w', padx=(16, 0))
        ttk.Label(target_frame, text='发布到：').pack(side='left')
        for key, label in [('github', 'GitHub'), ('modrinth', 'Modrinth'), ('curseforge', 'CurseForge')]:
            ttk.Checkbutton(target_frame, text=label, variable=self.targets[key]).pack(side='left', padx=(0, 8))
        secret = ttk.LabelFrame(outer, text='本次会话凭据（不会保存到本地）', padding=10); secret.pack(fill='x', pady=(10, 0))
        secret.columnconfigure(1, weight=1); secret.columnconfigure(3, weight=1)
        self.field(secret, 'Modrinth Token', 'MODRINTH_TOKEN', 0, 0, masked=True)
        self.field(secret, 'CurseForge Token', 'CURSEFORGE_TOKEN', 0, 2, masked=True)
        self.field(secret, 'CF API Key（查询）', 'CF_API_KEY', 1, 0, masked=True)
        self.button(secret, '保存已填写凭据到 GitHub Secrets', self.save_secrets).grid(row=1, column=2, columnspan=2, sticky='e', pady=4)
        actions = ttk.Frame(outer); actions.pack(fill='x', pady=10)
        for label, command in [('导入 / 预处理', self.do_import), ('本地构建', self.do_build), ('查询 CurseForge ID', self.do_resolve), ('本地发布', self.do_publish), ('云端发布', self.do_dispatch)]:
            self.button(actions, label, command).pack(side='left', padx=(0, 8))
        self.button(actions, 'GitHub 登录', self.gh_login).pack(side='right')
        panes = ttk.Panedwindow(outer, orient='vertical'); panes.pack(fill='both', expand=True)
        notes_frame = ttk.LabelFrame(panes, text='手写更新说明（保存至 release/manual.md）', padding=6)
        self.notes = tk.Text(notes_frame, height=7, wrap='word', undo=True, font=('Microsoft YaHei UI', 10)); self.notes.pack(fill='both', expand=True); self.notes.insert('1.0', notes)
        panes.add(notes_frame, weight=1)
        log_frame = ttk.LabelFrame(panes, text='运行日志', padding=6)
        self.log = tk.Text(log_frame, height=8, wrap='word', state='disabled', font=('Consolas', 10)); self.log.pack(fill='both', expand=True)
        panes.add(log_frame, weight=1)
        footer = ttk.Frame(outer); footer.pack(fill='x', pady=(10, 0))
        self.button(footer, '保存配置和说明', self.save_only).pack(side='left')
        self.button(footer, '打开输出目录', self.open_dist).pack(side='left', padx=8)
        self.button(footer, '打开 GitHub Actions', self.open_workflow).pack(side='left')
        self.button(footer, '查看自动更新说明', self.open_changes).pack(side='left', padx=8)
        self.button(footer, '查看未解析项目', self.open_unresolved).pack(side='left')
        self.status = ttk.Label(footer, text='就绪', style='Subtitle.TLabel'); self.status.pack(side='right')

    def field(self, parent, label, key, row, col, span=1, masked=False):
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky='w', padx=(0 if col == 0 else 16, 6), pady=4)
        ttk.Entry(parent, textvariable=self.vars[key], show='•' if masked else '').grid(row=row, column=col+1, columnspan=span, sticky='ew', pady=4)

    def button(self, parent, text, command):
        b = ttk.Button(parent, text=text, command=command); self.buttons.append(b); return b

    def browse(self):
        path = filedialog.askopenfilename(parent=self, title='选择 Prism 导出的整合包', filetypes=[('Modrinth 整合包', '*.mrpack'), ('所有文件', '*.*')])
        if path: self.vars['file'].set(path)

    def save(self, include_version=True):
        self.project = read_json(PROJECT, DEFAULT_PROJECT)
        for k in ('github_repository', 'modrinth_project_id', 'curseforge_project_id'):
            self.project[k] = self.vars[k].get().strip()
        self.release = read_json(RELEASE, DEFAULT_RELEASE)
        if include_version: self.release['version'] = self.vars['version'].get().strip()
        self.release['channel'] = self.vars['channel'].get()
        write_json(PROJECT, self.project); write_json(RELEASE, self.release)
        atomic_text(NOTES, self.notes.get('1.0', 'end-1c'))

    def save_only(self):
        try:
            self.save(); self.append('配置和更新说明已保存。\n')
        except (OSError, ValueError) as e: messagebox.showerror('保存失败', str(e), parent=self)

    def append(self, text):
        self.log.configure(state='normal'); self.log.insert('end', text); self.log.see('end'); self.log.configure(state='disabled')

    def selected(self): return [key for key, var in self.targets.items() if var.get()]

    def prepare(self, needs_version=True):
        if needs_version and not self.vars['version'].get().strip():
            raise ValueError('请先填写版本号。')
        self.save()

    def validate_targets(self, targets, local):
        if not targets: raise ValueError('请至少选择一个发布平台。')
        if 'github' in targets and not REPO_PATTERN.fullmatch(self.vars['github_repository'].get().strip()): raise ValueError('GitHub 仓库应为 owner/repo 格式。')
        if 'modrinth' in targets and not self.vars['modrinth_project_id'].get().strip(): raise ValueError('请填写 Modrinth 项目 ID。')
        if 'curseforge' in targets and not self.vars['curseforge_project_id'].get().strip().isdigit(): raise ValueError('请填写数字形式的 CurseForge 项目 ID。')
        if local:
            for target, key in [('modrinth', 'MODRINTH_TOKEN'), ('curseforge', 'CURSEFORGE_TOKEN')]:
                if target in targets and not (self.vars[key].get().strip() or os.environ.get(key, '').strip()): raise ValueError(f'本地发布到 {target} 需要填写 {key}。')

    def run_action(self, action):
        try: action()
        except (ValueError, OSError) as e: messagebox.showerror('无法执行', str(e), parent=self)

    def do_import(self):
        def action():
            path = self.vars['file'].get().strip()
            if not path or not Path(path).is_file() or Path(path).suffix.lower() != '.mrpack': raise ValueError('请选择有效的 .mrpack 文件。')
            version = self.vars['version'].get().strip()
            self.save(include_version=False)
            self.start('导入', ['import', path], import_version=version, import_channel=self.vars['channel'].get())
        self.run_action(action)

    def do_build(self):
        def action():
            self.prepare(); formats = 'modrinth' + (',curseforge' if self.targets['curseforge'].get() else '')
            self.start('本地构建', ['build', '--formats', formats])
        self.run_action(action)

    def do_resolve(self):
        def action():
            if not (self.vars['CF_API_KEY'].get().strip() or os.environ.get('CF_API_KEY', '').strip()): raise ValueError('请填写用于查询的 CF API Key。')
            self.prepare(); self.start('查询 CurseForge ID', ['resolve-curseforge'])
        self.run_action(action)

    def publish(self, cloud):
        def action():
            targets = self.selected(); self.validate_targets(targets, not cloud); self.prepare()
            verb = '云端发布' if cloud else '本地发布'
            repo = self.vars['github_repository'].get().strip()
            if messagebox.askyesno('确认' + verb, f'版本：{self.vars["version"].get().strip()}\n平台：{", ".join(targets)}\nGitHub 仓库：{repo}\n\n确定开始{verb}？', parent=self):
                self.start(verb, ['dispatch' if cloud else 'publish', '--targets', ','.join(targets)])
        self.run_action(action)

    def do_publish(self): self.publish(False)
    def do_dispatch(self): self.publish(True)

    def save_secrets(self):
        def action():
            repo = self.vars['github_repository'].get().strip()
            if not REPO_PATTERN.fullmatch(repo): raise ValueError('GitHub 仓库应为 owner/repo 格式。')
            secrets = {key: self.vars[key].get() for key in ('MODRINTH_TOKEN', 'CURSEFORGE_TOKEN', 'CF_API_KEY') if self.vars[key].get().strip()}
            if not secrets: raise ValueError('请至少填写一个凭据。')
            if messagebox.askyesno('确认保存 Secrets', f'仓库：{repo}\n将保存：{", ".join(secrets)}\n\n确定继续？', parent=self):
                self.start('保存 GitHub Secrets', None, secrets=secrets, repo=repo)
        self.run_action(action)

    def gh_login(self): self.start('GitHub 登录', None, login=True)

    def start(self, title, args, import_version=None, import_channel=None, secrets=None, repo=None, login=False):
        if self.busy: return
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        tokens = [self.vars[k].get() or env.get(k, '') for k in ('MODRINTH_TOKEN', 'CURSEFORGE_TOKEN', 'CF_API_KEY') if self.vars[k].get() or env.get(k, '')]
        for k in ('MODRINTH_TOKEN', 'CURSEFORGE_TOKEN', 'CF_API_KEY'):
            if self.vars[k].get(): env[k] = self.vars[k].get()
        self.busy = True
        for b in self.buttons: b.configure(state='disabled')
        self.status.configure(text=title + '中…'); self.append('\n▶ ' + title + '\n')
        threading.Thread(target=self.worker, args=(title, args, env, tokens, import_version, import_channel, secrets, repo, login), daemon=True).start()

    def worker(self, title, args, env, tokens, import_version, import_channel, secrets, repo, login):
        def clean(s):
            for token in tokens: s = s.replace(token, '[已隐藏]')
            return s
        try:
            if secrets is not None:
                for name, value in secrets.items():
                    result = subprocess.run(['gh', 'secret', 'set', name, '--repo', repo], input=value, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=ROOT, env=env, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    if result.returncode: raise RuntimeError(f'设置 {name} 失败（退出码 {result.returncode}）。')
                    self.events.put(('log', f'已设置 {name}。\n'))
            else:
                if login:
                    result = subprocess.run(['gh', 'auth', 'login', '--web'], cwd=ROOT, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
                    returncode = result.returncode
                else:
                    cmd = ([sys.executable, '--cli', *args] if getattr(sys, 'frozen', False)
                           else [sys.executable, '-m', 'frlo_release', *args])
                    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                    process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                               text=True, cwd=ROOT, env=env, encoding='utf-8',
                                               errors='replace', creationflags=flags, bufsize=1)
                    for line in process.stdout:
                        self.events.put(('log', clean(line)))
                    process.stdout.close()
                    returncode = process.wait()
                if returncode: raise RuntimeError(f'{title}失败（退出码 {returncode}）。')
                if import_version is not None:
                    release = read_json(RELEASE, DEFAULT_RELEASE)
                    if import_version:
                        release['version'] = import_version
                    release['channel'] = import_channel
                    write_json(RELEASE, release)
                    self.events.put(('version', str(release.get('version', ''))))
            self.events.put(('done', title + '完成。'))
        except Exception as e:
            self.events.put(('error', clean(str(e))))

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log': self.append(value)
                elif kind == 'version': self.vars['version'].set(value)
                elif kind in ('done', 'error'):
                    self.busy = False
                    for b in self.buttons: b.configure(state='normal')
                    self.status.configure(text='就绪' if kind == 'done' else '操作失败')
                    self.append(value + '\n')
                    if kind == 'error': messagebox.showerror('操作失败', value, parent=self)
        except queue.Empty: pass
        self.after(100, self.poll)

    def open_dist(self):
        def action():
            path = ROOT / 'dist'; path.mkdir(exist_ok=True); os.startfile(path)
        self.run_action(action)

    def open_changes(self):
        self.open_report(ROOT / 'release' / 'changes.md')

    def open_unresolved(self):
        self.open_report(ROOT / 'build' / 'curseforge-unresolved.json')

    def open_report(self, path):
        if not path.is_file():
            messagebox.showinfo('文件尚不存在', f'尚未生成：{path.name}', parent=self)
            return
        self.run_action(lambda: os.startfile(path))

    def open_workflow(self):
        repo = self.vars['github_repository'].get().strip()
        if not REPO_PATTERN.fullmatch(repo): messagebox.showerror('仓库无效', 'GitHub 仓库应为 owner/repo 格式。', parent=self); return
        webbrowser.open(f'https://github.com/{repo}/actions')


def main():
    app = App(); app.mainloop()


if __name__ == '__main__': main()
