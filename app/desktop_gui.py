#!/usr/bin/env python3
"""Tk desktop UI for local GitHub repositories and credential-helper auth."""

from __future__ import annotations

import fnmatch
from datetime import datetime
import queue
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import webbrowser

from desktop_backend import DesktopBackendError
from desktop_client import DesktopBackendClient
from environment_check import run_environment_check
import windows_repository_credentials as repository_credentials


class DesktopApp(tk.Tk):
    def __init__(self, backend=None):
        super().__init__()
        self.title("GitHub PR 小助手 · Desktop")
        self.geometry("1180x850")
        self.minsize(940, 680)
        self.backend = backend or DesktopBackendClient()
        self.events = queue.Queue()
        self.busy = False
        self.repo_info = None
        self.remote_fresh = False
        self.tags_fresh = False
        self.tag_load_pending = False
        self.workflow_loaded = False
        self.workflow_load_pending = False
        self.workflow_has_more = False
        self.workflow_rows = []
        self.workflow_refreshed_at = ""
        self._workflow_selected_index = None
        self.comparison = None
        self.inspection = None
        self.file_items = []
        self.file_tree_files = {}
        self.source_commit_items = []
        self._commit_detail_cache = {}
        self._pending_commit_sha = None
        self.remote_branches = []
        self.remote_branch_shas = {}
        self.reviewers = []
        self.selected_reviewers = []
        self.verified_reviewers = set()
        self.special_reviewers = set()
        self.draft_context = None
        self.result_url = ""
        self._compare_after_id = None
        self._tag_resize_after_id = None
        self._tag_canvas_width = None
        self._environment_check_after_id = None

        self.repo_path = tk.StringVar()
        self.workspace_path = tk.StringVar()
        self.workspace_repositories = {}
        self.account = tk.StringVar(value="Not connected / 未连接")
        self.base = tk.StringVar()
        self.source = tk.StringVar()
        self.base_search = tk.StringVar()
        self.source_search = tk.StringVar()
        self.base_search_count = tk.StringVar(value="0")
        self.source_search_count = tk.StringVar(value="0")
        self.query = tk.StringVar()
        self.title_var = tk.StringVar()
        self.tag_branch_search = tk.StringVar()
        self.tag_suggestion_branches = set()
        self._tag_branch_default_active = True
        self.tag_search = tk.StringVar()
        self.tag_show_related = tk.BooleanVar(value=False)
        self.workflow_show_related = tk.BooleanVar(value=False)
        self.branch_tag_data = []
        self.tag_rows = {}
        self.status = tk.StringVar(value="选择本地 GitHub 仓库，然后连接 / Select a local GitHub repository")

        self._build()
        self._drain_after_id = self.after(80, self._drain_events)
        self._restore_after_id = self.after(120, self.restore_last_repository_path)
        self._environment_check_after_id = self.after(200, self._check_environment_once)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self):
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        workspace = ttk.LabelFrame(outer, text="工作文件夹 / Workspace", padding=8)
        workspace.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        workspace.columnconfigure(0, weight=1)
        ttk.Entry(workspace, textvariable=self.workspace_path, state="readonly").grid(
            row=0, column=0, sticky="ew", padx=(0, 8))
        self.browse_button = ttk.Button(workspace, text="选择工作文件夹… / Browse…", command=self.browse)
        self.browse_button.grid(row=0, column=1)
        panes = ttk.Panedwindow(outer, orient="horizontal")
        panes.grid(row=1, column=0, sticky="nsew")
        sidebar = ttk.LabelFrame(panes, text="Git 仓库 / Repositories", padding=8)
        sidebar.rowconfigure(0, weight=1)
        sidebar.columnconfigure(0, weight=1)
        self.repo_tree = ttk.Treeview(sidebar, show="tree", selectmode="browse")
        self.repo_tree.grid(row=0, column=0, sticky="nsew")
        repo_scroll = ttk.Scrollbar(sidebar, orient="vertical", command=self.repo_tree.yview)
        repo_scroll.grid(row=0, column=1, sticky="ns")
        self.repo_tree.configure(yscrollcommand=repo_scroll.set)
        self.repo_tree.bind("<<TreeviewSelect>>", self._repository_selected)
        root = ttk.Frame(panes, padding=(12, 0, 0, 0))
        panes.add(sidebar, weight=1)
        panes.add(root, weight=4)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        ttk.Label(root, text="GitHub PR 小助手", font=("Segoe UI", 17, "bold")).grid(
            row=0, column=0, sticky="w")
        ttk.Label(root, textvariable=self.account).grid(row=0, column=1, sticky="e", padx=8)
        self.sign_out_button = ttk.Button(root, text="断开 / Sign out", command=self.sign_out, state="disabled")
        self.sign_out_button.grid(row=0, column=2, sticky="e")

        repo_frame = ttk.LabelFrame(root, text="当前仓库 / Selected repository", padding=8)
        repo_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 6))
        repo_frame.columnconfigure(0, weight=1)
        ttk.Label(repo_frame, textvariable=self.repo_path).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.connect_button = ttk.Button(repo_frame, text="重新连接 / Reconnect", command=self.connect)
        self.connect_button.grid(row=0, column=1, padx=(4, 0))
        self.token_button = ttk.Button(repo_frame, text="使用 Token… / Use token…", command=self._enter_token)
        self.token_button.grid(row=0, column=2, padx=(6, 0))

        branch_frame = ttk.LabelFrame(root, text="远程分支 / Remote branches", padding=8)
        branch_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=6)
        branch_frame.columnconfigure(1, weight=1)
        branch_frame.columnconfigure(3, weight=1)
        ttk.Label(branch_frame, text="Base · 远程").grid(row=0, column=0, padx=(0, 5))
        self.base_box = ttk.Combobox(branch_frame, textvariable=self.base, state="readonly")
        self.base_box.grid(row=0, column=1, sticky="ew", padx=(0, 16))
        ttk.Label(branch_frame, text="Source · 远程").grid(row=0, column=2, padx=(0, 5))
        self.source_box = ttk.Combobox(branch_frame, textvariable=self.source, state="readonly")
        self.source_box.grid(row=0, column=3, sticky="ew", padx=(0, 8))
        self.compare_button = ttk.Button(branch_frame, text="比较 / Compare", command=self.compare, state="disabled")
        self.compare_button.grid(row=0, column=4)
        self.fetch_button = ttk.Button(branch_frame, text="Fetch origin / 同步远程", command=self.fetch_remote, state="disabled")
        self.fetch_button.grid(row=0, column=5, padx=(6, 0))
        ttk.Label(branch_frame, text="搜索远程 Base").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.base_search_entry = ttk.Entry(branch_frame, textvariable=self.base_search)
        self.base_search_entry.grid(row=1, column=1, sticky="ew", padx=(0, 16), pady=(6, 0))
        ttk.Label(branch_frame, text="搜索远程 Source").grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.source_search_entry = ttk.Entry(branch_frame, textvariable=self.source_search)
        self.source_search_entry.grid(row=1, column=3, sticky="ew", padx=(0, 8), pady=(6, 0))
        ttk.Label(branch_frame, textvariable=self.base_search_count).grid(row=2, column=1, sticky="e")
        ttk.Label(branch_frame, textvariable=self.source_search_count).grid(row=2, column=3, sticky="e")
        self.base_box.bind("<<ComboboxSelected>>", self._branch_selection_changed)
        self.source_box.bind("<<ComboboxSelected>>", self._branch_selection_changed)
        self.base_search.trace_add("write", lambda *_args: self._filter_branch_options())
        self.source_search.trace_add("write", lambda *_args: self._filter_branch_options())

        notebook = ttk.Notebook(root)
        self.notebook = notebook
        notebook.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=6)
        diff_tab = ttk.Frame(notebook, padding=8)
        pr_tab = ttk.Frame(notebook, padding=8)
        commits_tab = ttk.Frame(notebook, padding=8)
        activity_tab = ttk.Frame(notebook, padding=8)
        tags_tab = ttk.Frame(notebook, padding=8)
        workflow_tab = ttk.Frame(notebook, padding=8)
        self.tags_tab = tags_tab
        self.workflow_tab = workflow_tab
        notebook.add(diff_tab, text="代码差异 / Changes")
        notebook.add(pr_tab, text="PR 与审核人 / PR & Reviewers")
        notebook.add(tags_tab, text="Tags / 标签")
        notebook.add(workflow_tab, text="Workflow / 部署")
        notebook.add(commits_tab, text="Source 提交记录 / Source Commits")
        notebook.add(activity_tab, text="运行日志 / Activity")
        notebook.bind("<<NotebookTabChanged>>", self._notebook_tab_changed)

        tags_tab.columnconfigure(0, weight=1)
        tags_tab.rowconfigure(1, weight=1)
        tag_controls = ttk.Frame(tags_tab)
        tag_controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tag_controls.columnconfigure(1, weight=1)
        tag_controls.columnconfigure(3, weight=1)
        ttk.Label(tag_controls, text="Branch 筛选").grid(row=0, column=0, padx=(0, 6))
        self.tag_branch_search_entry = ttk.Entry(tag_controls, textvariable=self.tag_branch_search, state="disabled")
        self.tag_branch_search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(tag_controls, text="搜索 Tag").grid(row=0, column=2, padx=(0, 6))
        self.tag_search_entry = ttk.Entry(tag_controls, textvariable=self.tag_search, state="disabled")
        self.tag_search_entry.grid(row=0, column=3, sticky="ew")
        self.tag_related_checkbox = ttk.Checkbutton(
            tag_controls, text="显示其他关联分支 / Show other reachable branches",
            variable=self.tag_show_related, command=self._render_branch_tags, state="disabled")
        self.tag_related_checkbox.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.tag_branch_search.trace_add("write", self._tag_branch_filter_changed)
        self.tag_search.trace_add("write", self._tag_filter_changed)
        tag_table = ttk.Frame(tags_tab)
        tag_table.grid(row=1, column=0, columnspan=2, sticky="nsew")
        tag_table.columnconfigure(0, weight=1)
        tag_table.rowconfigure(1, weight=1)
        self.tag_header = tk.Canvas(tag_table, height=28, highlightthickness=0, background="#f2f5f8")
        self.tag_header.grid(row=0, column=0, sticky="ew")
        self.tag_list = tk.Canvas(tag_table, highlightthickness=0, background="white", takefocus=True)
        self.tag_list.grid(row=1, column=0, sticky="nsew")
        tag_scroll = ttk.Scrollbar(tag_table, orient="vertical", command=self.tag_list.yview)
        tag_scroll.grid(row=1, column=1, sticky="ns")
        self.tag_list.configure(yscrollcommand=tag_scroll.set)
        self.tag_list.bind("<Configure>", self._tag_list_resized)
        self.tag_list.bind("<Button-1>", self._tag_list_click)
        self.tag_list.bind("<MouseWheel>", self._tag_list_mousewheel)
        self.tag_list.bind("<Button-4>", lambda _event: self.tag_list.yview_scroll(-3, "units"))
        self.tag_list.bind("<Button-5>", lambda _event: self.tag_list.yview_scroll(3, "units"))
        self.tag_result = ttk.Label(tags_tab, text="", wraplength=1060)
        self.tag_result.grid(row=2, column=0, sticky="ew", pady=(6, 0))

        workflow_tab.columnconfigure(0, weight=1)
        workflow_tab.rowconfigure(1, weight=3)
        workflow_tab.rowconfigure(2, weight=2)
        workflow_header = ttk.Frame(workflow_tab)
        workflow_header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        workflow_header.columnconfigure(0, weight=1)
        self.workflow_name = tk.StringVar(value="")
        ttk.Label(workflow_header, text="Workflow · 只读 / Read only").grid(row=0, column=0, sticky="w")
        self.workflow_name_entry = ttk.Entry(workflow_header, textvariable=self.workflow_name, width=28, state="readonly")
        self.workflow_refresh_button = ttk.Button(workflow_header, text="刷新 / Refresh",
                                                  command=lambda: self.load_workflow_tags(reset=True))
        self.workflow_refresh_button.grid(row=0, column=2, padx=(6, 0))
        self.workflow_more_button = ttk.Button(workflow_header, text="加载更多 / Load more",
                                               command=lambda: self.load_workflow_tags(reset=False))
        self.workflow_more_button.grid(row=0, column=3, padx=(6, 0))
        workflow_list_frame = ttk.Frame(workflow_tab)
        workflow_list_frame.grid(row=1, column=0, sticky="nsew")
        workflow_list_frame.rowconfigure(0, weight=1)
        workflow_list_frame.columnconfigure(0, weight=1)
        self.workflow_list = ttk.Treeview(
            workflow_list_frame, columns=("state", "success", "pending", "failed", "branches", "time", "run"),
            selectmode="browse")
        self.workflow_list.heading("#0", text="Tag / ref")
        self.workflow_list.column("#0", width=180)
        for column, title, width in (
                ("state", "Workflow 状态", 115), ("success", "部署 Job 成功", 190),
                ("pending", "等待 / 运行", 180), ("failed", "失败 / 取消", 160),
                ("branches", "关联分支", 260), ("time", "运行创建时间", 180),
                ("run", "运行编号", 100)):
            self.workflow_list.heading(column, text=title)
            self.workflow_list.column(column, width=width, stretch=column in ("success", "pending", "failed", "branches"))
        self.workflow_list.grid(row=0, column=0, sticky="nsew")
        workflow_scroll = ttk.Scrollbar(workflow_list_frame, orient="vertical", command=self.workflow_list.yview)
        workflow_scroll.grid(row=0, column=1, sticky="ns")
        workflow_horizontal = ttk.Scrollbar(workflow_list_frame, orient="horizontal", command=self.workflow_list.xview)
        workflow_horizontal.grid(row=1, column=0, sticky="ew")
        self.workflow_list.configure(yscrollcommand=workflow_scroll.set, xscrollcommand=workflow_horizontal.set)
        self.workflow_list.tag_configure("has_failures", foreground="#b42318")
        self.workflow_list.bind("<<TreeviewSelect>>", self._workflow_selected)
        workflow_detail = ttk.LabelFrame(workflow_tab, text="运行详情 / Run details")
        workflow_detail.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        workflow_detail.rowconfigure(0, weight=1)
        workflow_detail.columnconfigure(0, weight=1)
        self.workflow_detail = tk.Text(workflow_detail, wrap="word", height=7, state="disabled")
        self.workflow_detail.grid(row=0, column=0, columnspan=2, sticky="nsew")
        workflow_detail_scroll = ttk.Scrollbar(workflow_detail, orient="vertical", command=self.workflow_detail.yview)
        workflow_detail_scroll.grid(row=0, column=2, sticky="ns")
        self.workflow_detail.configure(yscrollcommand=workflow_detail_scroll.set)
        self.workflow_related_checkbox = ttk.Checkbutton(
            workflow_detail, text="显示其他关联分支 / Show other reachable branches",
            variable=self.workflow_show_related, command=self._workflow_selected)
        self.workflow_related_checkbox.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.workflow_open_button = ttk.Button(workflow_detail, text="在 GitHub 查看 / Open on GitHub",
                                               command=self._open_workflow_run)
        self.workflow_open_button.grid(row=1, column=1, sticky="e", pady=(6, 0))
        self.workflow_result = ttk.Label(workflow_tab, text="打开页面时加载 / Loaded when opened", wraplength=1060)
        self.workflow_result.grid(row=3, column=0, sticky="ew", pady=(6, 0))

        commits_tab.columnconfigure(0, weight=1)
        commits_tab.rowconfigure(1, weight=1)
        self.commit_summary = tk.StringVar(value="Fetch origin and compare remote branches to view source history.")
        ttk.Label(commits_tab, textvariable=self.commit_summary, wraplength=1060).grid(
            row=0, column=0, sticky="ew", pady=(0, 6))
        commit_panes = ttk.Panedwindow(commits_tab, orient="vertical")
        commit_panes.grid(row=1, column=0, sticky="nsew")
        commit_list_frame = ttk.Frame(commit_panes)
        commit_list_frame.rowconfigure(0, weight=1)
        commit_list_frame.columnconfigure(0, weight=1)
        self.commit_list = ttk.Treeview(commit_list_frame, columns=("date", "author", "subject"),
                                        selectmode="browse")
        self.commit_list.heading("#0", text="Commit")
        self.commit_list.heading("date", text="作者时间 / Author time")
        self.commit_list.heading("author", text="作者 / Author")
        self.commit_list.heading("subject", text="提交标题 / Subject")
        self.commit_list.column("#0", width=90, stretch=False)
        self.commit_list.column("date", width=190, stretch=False)
        self.commit_list.column("author", width=150, stretch=False)
        self.commit_list.column("subject", width=420)
        self.commit_list.grid(row=0, column=0, sticky="nsew")
        commit_scroll = ttk.Scrollbar(commit_list_frame, orient="vertical", command=self.commit_list.yview)
        commit_scroll.grid(row=0, column=1, sticky="ns")
        self.commit_list.configure(yscrollcommand=commit_scroll.set)
        self.commit_list.bind("<<TreeviewSelect>>", self._commit_selected)
        commit_detail_frame = ttk.LabelFrame(commit_panes, text="提交详情 / Commit details")
        commit_detail_frame.rowconfigure(0, weight=1)
        commit_detail_frame.columnconfigure(0, weight=1)
        self.commit_view = tk.Text(commit_detail_frame, wrap="word", font=("Consolas", 10), state="disabled")
        self.commit_view.grid(row=0, column=0, sticky="nsew")
        detail_scroll = ttk.Scrollbar(commit_detail_frame, orient="vertical", command=self.commit_view.yview)
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.commit_view.configure(yscrollcommand=detail_scroll.set)
        commit_panes.add(commit_list_frame, weight=3)
        commit_panes.add(commit_detail_frame, weight=2)

        activity_header = ttk.Frame(activity_tab)
        activity_header.pack(fill="x", pady=(0, 6))
        ttk.Label(activity_header, text="本次运行记录（不含 token、远程 URL 或本地路径） / Current session log (redacted)").pack(side="left")
        self.refresh_activity_button = ttk.Button(activity_header, text="刷新 / Refresh", command=self.refresh_activity)
        self.refresh_activity_button.pack(side="right")
        activity_content = ttk.Frame(activity_tab)
        activity_content.pack(fill="both", expand=True)
        activity_content.rowconfigure(0, weight=1)
        activity_content.columnconfigure(0, weight=1)
        self.activity_view = tk.Text(activity_content, wrap="word", font=("Consolas", 10), state="disabled")
        self.activity_view.grid(row=0, column=0, sticky="nsew")
        activity_scroll = ttk.Scrollbar(activity_content, orient="vertical", command=self.activity_view.yview)
        activity_scroll.grid(row=0, column=1, sticky="ns")
        self.activity_view.configure(yscrollcommand=activity_scroll.set)

        diff_tab.columnconfigure(0, weight=1)
        diff_tab.rowconfigure(1, weight=1)
        self.summary = tk.StringVar(value="选择远程分支并比较 / Select remote branches and compare")
        ttk.Label(diff_tab, textvariable=self.summary, wraplength=1060).grid(
            row=0, column=0, sticky="ew", pady=(0, 8))
        panes = ttk.Panedwindow(diff_tab, orient="horizontal")
        panes.grid(row=1, column=0, sticky="nsew")
        left, right = ttk.Frame(panes), ttk.Frame(panes)
        panes.add(left, weight=1)
        panes.add(right, weight=3)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.files = ttk.Treeview(left, columns=("count", "add", "delete"), selectmode="browse")
        self.files.heading("#0", text="变更文件 / Changed files")
        self.files.column("#0", width=260)
        self.files.heading("count", text="文件数")
        self.files.column("count", width=65, anchor="e", stretch=False)
        self.files.heading("add", text="+")
        self.files.heading("delete", text="−")
        self.files.column("add", width=55, anchor="e", stretch=False)
        self.files.column("delete", width=55, anchor="e", stretch=False)
        self.files.grid(row=0, column=0, sticky="nsew")
        file_scroll = ttk.Scrollbar(left, orient="vertical", command=self.files.yview)
        file_scroll.grid(row=0, column=1, sticky="ns")
        file_horizontal = ttk.Scrollbar(left, orient="horizontal", command=self.files.xview)
        file_horizontal.grid(row=1, column=0, sticky="ew")
        self.files.configure(yscrollcommand=file_scroll.set, xscrollcommand=file_horizontal.set)
        self.files.bind("<<TreeviewSelect>>", self.show_file)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.patch = tk.Text(right, wrap="none", font=("Consolas", 10), state="disabled")
        self.patch.grid(row=0, column=0, sticky="nsew")
        patch_scroll = ttk.Scrollbar(right, orient="vertical", command=self.patch.yview)
        patch_scroll.grid(row=0, column=1, sticky="ns")
        self.patch.configure(yscrollcommand=patch_scroll.set)
        self.patch.tag_configure("add", foreground="#116329", background="#e6ffec")
        self.patch.tag_configure("del", foreground="#82071e", background="#ffebe9")
        self.patch.tag_configure("hunk", foreground="#0550ae", background="#ddf4ff")

        pr_tab.columnconfigure(1, weight=1)
        pr_tab.rowconfigure(3, weight=1)
        ttk.Label(pr_tab, text="标题 / Title").grid(row=0, column=0, sticky="nw", padx=(0, 8), pady=4)
        ttk.Entry(pr_tab, textvariable=self.title_var).grid(row=0, column=1, columnspan=3, sticky="ew", pady=4)
        ttk.Label(pr_tab, text="正文 / Description").grid(row=1, column=0, sticky="nw", padx=(0, 8), pady=4)
        self.body = tk.Text(pr_tab, height=8, wrap="word")
        self.body.grid(row=1, column=1, columnspan=3, sticky="ew", pady=4)
        reviewer_frame = ttk.LabelFrame(pr_tab, text="仓库可分配审核人 / Assignable reviewers", padding=8)
        reviewer_frame.grid(row=2, column=0, columnspan=4, sticky="ew", pady=8)
        reviewer_frame.columnconfigure(0, weight=1)
        reviewer_frame.columnconfigure(2, weight=1)
        ttk.Entry(reviewer_frame, textvariable=self.query).grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.reviewer_search_button = ttk.Button(
            reviewer_frame, text="搜索 / Search", command=self.search_reviewers, state="disabled")
        self.reviewer_search_button.grid(row=0, column=1, padx=4)
        self.reviewer_results = tk.Listbox(reviewer_frame, height=5, exportselection=False)
        self.reviewer_results.grid(row=1, column=0, sticky="ew", pady=5)
        self.reviewer_add_button = ttk.Button(
            reviewer_frame, text="添加 → / Add", command=self.add_reviewer, state="disabled")
        self.reviewer_add_button.grid(row=1, column=1, padx=5)
        self.reviewer_selected = tk.Listbox(reviewer_frame, height=5, exportselection=False)
        self.reviewer_selected.grid(row=1, column=2, sticky="ew", pady=5)
        self.reviewer_remove_button = ttk.Button(
            reviewer_frame, text="移除 / Remove", command=self.remove_reviewer, state="disabled")
        self.reviewer_remove_button.grid(row=1, column=3, padx=5)

        action_row = ttk.Frame(pr_tab)
        action_row.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(4, 0))
        action_row.columnconfigure(0, weight=1)
        self.save_button = ttk.Button(action_row, text="保存设置 / Save", command=self.save_settings, state="disabled")
        self.save_button.grid(row=0, column=1, padx=4)
        self.inspect_button = ttk.Button(action_row, text="检查已有 PR / Inspect", command=self.inspect, state="disabled")
        self.inspect_button.grid(row=0, column=2, padx=4)
        self.create_button = ttk.Button(action_row, text="核对并创建 / Review & Create", command=self.confirm_create, state="disabled")
        self.create_button.grid(row=0, column=3, padx=(4, 0))

        self.pr_result = ttk.Label(pr_tab, text="", wraplength=1050)
        self.pr_result.grid(row=5, column=0, columnspan=4, sticky="w", pady=5)
        self.pr_link = ttk.Label(pr_tab, text="", foreground="#0969da", cursor="hand2")
        self.pr_link.grid(row=6, column=0, columnspan=4, sticky="w")
        self.pr_link.bind("<Button-1>", lambda _event: webbrowser.open(self.result_url) if self.result_url else None)

        ttk.Label(root, textvariable=self.status, wraplength=1120).grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(7, 0))
        self._update_controls()

    @staticmethod
    def _find_workspace_repositories(path):
        root = Path(path)
        if not root.is_dir():
            raise DesktopBackendError("workspace_unavailable", "工作文件夹不可用 / Workspace folder is unavailable.")
        try:
            children = sorted((p for p in root.iterdir() if p.is_dir() and not p.is_symlink()),
                              key=lambda p: p.name.casefold())
            return [(str(p), p.name) for p in [root, *children] if (p / ".git").exists()]
        except OSError:
            raise DesktopBackendError("workspace_unavailable", "无法读取工作文件夹 / Cannot read workspace folder.") from None

    def browse(self):
        selected = filedialog.askdirectory(parent=self, mustexist=True)
        if selected:
            self._select_workspace(selected, remember=True)

    def _select_workspace(self, path, *, remember=False):
        self._clear_repository_state()
        self.workspace_path.set(path)
        self.workspace_repositories.clear()
        self.repo_tree.delete(*self.repo_tree.get_children())

        def work():
            repositories = self._find_workspace_repositories(path)
            if remember:
                self.backend.remember_local_repository_path(path)
            return repositories

        self._run_async("查找 Git 仓库… / Finding Git repositories…", work, self._workspace_loaded)

    def _workspace_loaded(self, repositories):
        root = self.repo_tree.insert("", "end", text=self.workspace_path.get(), open=True)
        for path, name in repositories:
            iid = self.repo_tree.insert(root, "end", text=name)
            self.workspace_repositories[iid] = path
        self.status.set(f"找到 {len(repositories)} 个 Git 仓库；请选择仓库 / Select a repository")

    def _repository_selected(self, _event=None):
        selection = self.repo_tree.selection()
        path = self.workspace_repositories.get(selection[0]) if selection else None
        if self.busy:
            return
        if not path:
            self._clear_repository_state()
            self.status.set("请选择一个 Git 仓库 / Select a Git repository")
            return
        self._clear_repository_state()
        self.repo_path.set(path)
        self.connect()

    def _check_environment_once(self):
        self._environment_check_after_id = None
        result = run_environment_check()
        if result["ok"]:
            return
        details = "\n".join(
            f"- {item['message']}\n  {item['hint']}" for item in result["failures"])
        messagebox.showwarning(
            "启动环境检查 / Environment check",
            "发现启动依赖问题，请按提示安装或修复：\n" + details,
            parent=self,
        )
        self.status.set("启动环境检查未通过；请按提示修复后重试 / Environment check failed")

    def restore_last_repository_path(self):
        if self.busy or self.workspace_path.get().strip():
            return

        def loaded(result):
            draft = result.get("draft", {}) if isinstance(result, dict) else {}
            path = draft.get("local_repository_path") if isinstance(draft, dict) else None
            if isinstance(path, str) and path:
                self._select_workspace(path)

        self._run_async("读取上次选择的目录… / Restoring last selected directory…",
                        self.backend.load_settings, loaded)

    def connect(self, *, token=None):
        path = self.repo_path.get().strip()
        if not path:
            self.status.set("先选择本地仓库文件夹 / Choose a local repository folder first")
            return

        def work():
            try:
                if token is not None:
                    identity = self.backend.describe_repository(path)["repository"]
                    info = self.backend.connect_repository(path, token=token, expected_repository=identity)
                    try:
                        repository_credentials.save(info["repository"], token)
                    except repository_credentials.CredentialStorageError:
                        info["credential_storage_warning"] = True
                else:
                    helper_error = None
                    try:
                        info = self.backend.connect_repository(path)
                        identity = info["repository"]
                    except DesktopBackendError as error:
                        if error.code not in ("credential_missing", "github_auth_failed", "github_access_denied"):
                            raise
                        helper_error = error
                        identity = self.backend.describe_repository(path)["repository"]
                    try:
                        saved_token = repository_credentials.read(identity)
                        storage_unavailable = False
                    except repository_credentials.CredentialStorageError:
                        saved_token = None
                        storage_unavailable = True
                    if saved_token:
                        # An explicitly chosen repository credential remains the
                        # user's selection even if a generic Git credential works.
                        info = self.backend.connect_repository(
                            path, token=saved_token, expected_repository=identity)
                    elif helper_error:
                        raise helper_error
                    elif storage_unavailable:
                        info["credential_read_warning"] = True
                fetch_error = ""
                try:
                    fetch = self.backend.fetch_remote()
                except DesktopBackendError as error:
                    fetch = None
                    fetch_error = error.message
                branches = self.backend.load_branches()
                settings = self.backend.load_settings()
                return (info, branches, settings.get("draft", {}), fetch, fetch_error,
                        settings.get("branch_selection"), settings.get("tag_rules", {}))
            except Exception:
                self.backend.sign_out()
                raise

        self._run_async("读取本地 origin、Git 凭据和远程分支… / Connecting and loading remote branches…", work,
                        self._connected, on_error=self._connect_failed)

    def _connect_failed(self, error):
        if error.code not in ("credential_missing", "credential_invalid", "github_auth_failed"):
            self.status.set(error.message)
            return
        self.status.set("Git 凭据无效或缺失，请输入另一账号的 PAT（不会覆盖无关仓库凭据） / Enter this repository's PAT")
        self._enter_token()

    def _enter_token(self):
        if not self.repo_path.get().strip() or self.busy:
            return
        token = simpledialog.askstring(
            "GitHub PAT", "请输入此仓库使用的 GitHub PAT：\n"
            "用户名由 PAT 验证的账号提供；PAT 填在密码位置。验证成功后按仓库保存，不覆盖另一个仓库。", show="*", parent=self)
        if token:
            self.connect(token=token.strip())

    def _apply_tag_rules(self, tag_rules):
        self.tag_branch_search.set(tag_rules.get("branch_filter", ""))
        self._tag_branch_default_active = True
        self.tag_suggestion_branches = {rule["branch"] for rule in tag_rules.get("suggestions", [])}

    def _connected(self, result):
        info, branches, draft, _fetch, fetch_error, branch_selection, tag_rules = result
        self.tag_load_pending = False
        self._apply_tag_rules(tag_rules)
        names = [item["name"] for item in branches]
        self.remote_branches = names
        self.remote_branch_shas = {item["name"]: item["sha"] for item in branches}
        self.repo_info = info
        self.remote_fresh = not bool(fetch_error)
        self.tags_fresh = bool(_fetch and _fetch.get("tags_fresh"))
        self.base_search.set("")
        self.source_search.set("")
        self._filter_branch_options()
        repo_key = info["repository"].casefold()
        saved = (branch_selection if isinstance(branch_selection, dict)
                 and isinstance(branch_selection.get("repository"), str)
                 and branch_selection["repository"].casefold() == repo_key else None)
        legacy_matches = (isinstance(draft, dict) and isinstance(draft.get("repository"), str)
                          and draft["repository"].casefold() == repo_key
                          and draft.get("base_branch") in names
                          and draft.get("source_branch") in names
                          and draft.get("base_branch") != draft.get("source_branch"))
        missing_saved_branch = False
        if saved is not None:
            saved_base, saved_source = saved.get("base_branch", ""), saved.get("source_branch", "")
            base = saved_base if saved_base in names else ""
            source = saved_source if saved_source in names else ""
            missing_saved_branch = bool((saved_base and not base) or (saved_source and not source))
        elif legacy_matches:
            # Backward compatibility with drafts saved before per-repo memory.
            base, source = draft["base_branch"], draft["source_branch"]
        else:
            # Do not silently choose GitHub's default branch.
            base = ""
            current = info.get("current_branch", "")
            source = current if current in names else ""
        self.base.set(f"origin/{base}" if base else "")
        self.source.set(f"origin/{source}" if source else "")
        draft_matches = bool(legacy_matches and base == draft["base_branch"]
                             and source == draft["source_branch"])
        if draft_matches:
            self.title_var.set(draft.get("title", ""))
            self.body.delete("1.0", "end")
            self.body.insert("1.0", draft.get("body", ""))
            self.selected_reviewers = list(draft.get("reviewers", []))
            self.special_reviewers = {
                login for login in self.selected_reviewers
                if login.casefold() == "copilot"
            }
            self.draft_context = (info["repository"], base, source)
        else:
            self.title_var.set("")
            self.body.delete("1.0", "end")
            self.selected_reviewers = []
            self.special_reviewers.clear()
            self.draft_context = None
        # Saved reviewer choices are only prefills. They become verified after
        # the current repository-scoped search returns the same login.
        self.verified_reviewers.clear()
        self._render_selected_reviewers()
        self.branch_tag_data = []
        self._render_branch_tags()
        self._reset_workflow_view()
        self.account.set(f"Connected as {info['login']} / 已连接 {info['login']}")
        visibility = "private" if info.get("private") else "public"
        if fetch_error:
            self.status.set(f"已连接 {info['repository']} ({visibility})；Fetch 失败，分支可能是缓存 / Fetch failed; cached branches may be stale: {fetch_error}")
        else:
            self.status.set(f"已连接 {info['repository']} ({visibility})；Fetch 完成，已加载 {len(names)} 个远程分支 / Fetched {len(names)} remote branches")
        if missing_saved_branch:
            self.status.set(self.status.get() + "；上次选择的分支已不可用，请重新选择 / A saved branch is unavailable; select it again")
        if info.get("credential_storage_warning"):
            self.status.set(self.status.get() + "；Token 未能安全保存，仅本次使用 / Token not saved; session only")
        elif info.get("credential_read_warning"):
            self.status.set(self.status.get() + "；无法读取安全凭据，已使用 Git 凭据 / Secure credentials unavailable; Git credential used")
        self.tag_result.configure(
            text="点击 Tags 标签页时再加载远端 Tag / Remote tags load when the Tags tab is opened")
        self.sign_out_button.configure(state="normal")
        self._update_controls()
        if (self.remote_fresh and self._selected_remote_branch(self.base)
                and self._selected_remote_branch(self.source)
                and self._selected_remote_branch(self.base) != self._selected_remote_branch(self.source)):
            self._schedule_auto_compare()
        if self._tag_tab_is_selected():
            self._queue_branch_tag_load()
        elif self.notebook.select() == str(self.workflow_tab):
            self._queue_workflow_load()

    def fetch_remote(self):
        if not self.repo_info:
            self.status.set("先连接本地仓库 / Connect a local repository first")
            return

        def work():
            result = self.backend.fetch_remote()
            branches = self.backend.load_branches()
            return result, branches

        self._run_async("Fetch origin，更新本地远程跟踪分支… / Fetching origin…", work, self._fetched)

    def _fetched(self, result):
        fetch, branches = result
        self.tag_load_pending = False
        old_base = self._selected_remote_branch(self.base)
        old_source = self._selected_remote_branch(self.source)
        self.remote_branches = [item["name"] for item in branches]
        self.remote_branch_shas = {item["name"]: item["sha"] for item in branches}
        self.remote_fresh = True
        self.tags_fresh = bool(fetch.get("tags_fresh"))
        self._filter_branch_options()
        self.base.set(f"origin/{old_base}" if old_base in self.remote_branches else "")
        self.source.set(f"origin/{old_source}" if old_source in self.remote_branches else "")
        self.branch_tag_data = []
        self._render_branch_tags()
        self._reset_workflow_view()
        self.comparison = None
        self.inspection = None
        self.files.delete(*self.files.get_children())
        self.file_items = []
        self.file_tree_files.clear()
        self._set_patch("Fetch completed. Compare the selected remote branches.")
        self._clear_commit_history("Compare branches to view source branch history.")
        self.summary.set("Remote branch refs refreshed from origin.")
        self.status.set(
            f"Fetch 完成：{fetch['count']} 个远程分支，新增 {fetch['added']}，更新 {fetch['updated']} / "
            f"Fetch complete: {fetch['count']} branches, {fetch['added']} new, {fetch['updated']} updated")
        self.tag_result.configure(
            text="点击 Tags 标签页时再加载远端 Tag / Remote tags load when the Tags tab is opened")
        self._update_controls()
        if (self._selected_remote_branch(self.base) and self._selected_remote_branch(self.source)
                and self._selected_remote_branch(self.base) != self._selected_remote_branch(self.source)):
            self._schedule_auto_compare()
        if self._tag_tab_is_selected():
            self._queue_branch_tag_load()
        elif self.notebook.select() == str(self.workflow_tab):
            self._queue_workflow_load()

    def load_branch_tags(self):
        if not self.repo_info:
            self.tag_result.configure(text="先连接本地仓库 / Connect a local repository first")
            return
        if self.tags_fresh:
            self.tag_load_pending = False
            return
        if not self.remote_fresh:
            self.tag_result.configure(text="先 Fetch 分支和远端 Tags / Fetch branches and remote tags first")
            return
        if self.busy:
            self.tag_load_pending = True
            return
        self.tag_load_pending = False
        self._run_async("整理远程分支与可达 Tags… / Mapping remote tags to branches…",
                        self.backend.list_branch_tags, self._branch_tags_loaded,
                        on_error=self._branch_tags_failed)

    def _branch_tags_failed(self, error):
        self.tags_fresh = False
        self.tag_result.configure(text="Tag 加载失败，请切换标签页重试 / Tag loading failed; reopen this tab to retry")
        self.status.set(error.message)

    def _branch_tags_loaded(self, result):
        tags = result.get("tags", [])
        branches = result.get("branches", [])
        if (not isinstance(tags, list) or not isinstance(branches, list)
                or any(not isinstance(branch, dict) or not isinstance(branch.get("tag_indexes"), list)
                       or any(type(index) is not int or not 0 <= index < len(tags)
                              for index in branch["tag_indexes"]) for branch in branches)):
            self._branch_tags_failed(DesktopBackendError("tag_list_invalid", "Invalid tag list returned by backend."))
            return
        self.tags_fresh = True
        self.branch_tag_data = [{**branch, "tags": [tags[index] for index in branch["tag_indexes"]]}
                                for branch in branches]
        self._render_branch_tags()
        self._update_controls()

    def _tag_tab_is_selected(self):
        return self.notebook.select() == str(self.tags_tab)

    def _notebook_tab_changed(self, _event=None):
        if self._tag_tab_is_selected():
            self._queue_branch_tag_load()
        elif self.notebook.select() == str(self.workflow_tab):
            self._queue_workflow_load()

    def _queue_branch_tag_load(self):
        if not self.repo_info or not self.remote_fresh or self.tags_fresh:
            return
        self.tag_load_pending = True
        if not self.busy:
            self.load_branch_tags()

    def _reset_workflow_view(self):
        self.workflow_loaded = False
        self.workflow_load_pending = False
        self.workflow_has_more = False
        self.workflow_rows = []
        self.workflow_refreshed_at = ""
        self._workflow_selected_index = None
        self.workflow_show_related.set(False)
        self.workflow_list.delete(*self.workflow_list.get_children())
        self._set_text(self.workflow_detail, "选择运行查看详情 / Select a run to view details")
        self.workflow_result.configure(text="打开页面时加载 / Loaded when opened")

    def _queue_workflow_load(self):
        if not self.repo_info or self.workflow_loaded:
            return
        self.workflow_load_pending = True
        if not self.busy:
            self.load_workflow_tags(reset=True)

    def load_workflow_tags(self, *, reset):
        if not self.repo_info:
            self.workflow_result.configure(text="先连接仓库 / Connect a repository first")
            return
        if self.busy:
            if reset:
                self.workflow_load_pending = True
            return
        self.workflow_load_pending = False
        workflow_name = self.workflow_name.get().strip()
        if not workflow_name:
            repository_name = (self.repo_info["repository"].rsplit("/", 1)[-1].casefold()
                               if self.repo_info else "")
            workflow_name = ("build-and-push"
                             if repository_name in ("tsp-common-model", "tsp-common-lib")
                             else "build-and-deploy")
            self.workflow_name.set(workflow_name)
        self._run_async("读取 Workflow Tag 和部署状态… / Loading workflow tags and deployments…",
                        lambda: self.backend.list_workflow_tag_runs(reset=reset, workflow_name=workflow_name),
                        lambda result: self._workflow_tags_loaded(result, reset=reset),
                        on_error=self._workflow_load_failed)

    @staticmethod
    def _workflow_job_groups(run):
        groups = {"success": [], "pending": [], "failed": [], "other": []}
        for job in run.get("jobs", []):
            state = job.get("state", "unknown")
            label = job.get("environment") or "环境未知: " + job.get("job_name", "")
            if state == "success":
                label += " ✓" if job.get("sync_confirmed") else " (同步未确认)"
                group = "success"
            elif state in ("waiting", "in_progress"):
                group = "pending"
            elif state in ("failure", "cancelled", "timed_out", "action_required", "startup_failure"):
                group = "failed"
            else:
                group = "other"
            groups[group].append((label, state))
        # Put real failures ahead of cancellations so the most actionable
        # environment stays visible in the width-limited summary column.
        groups["failed"].sort(key=lambda item: item[1] == "cancelled")
        return groups

    @staticmethod
    def _workflow_compact(values):
        text = ", ".join(values)
        return text if len(text) <= 150 else text[:147] + "…"

    @classmethod
    def _workflow_branch_summary(cls, branches):
        if not branches.get("available"):
            return "未能关联 / unavailable"
        exact = branches.get("exact", [])
        related = branches.get("related", [])
        if exact:
            return f"头匹配 {branches.get('exact_count', len(exact))}: " + cls._workflow_compact(
                ["origin/" + name for name in exact])
        if related:
            return f"关联 {branches.get('related_count', len(related))}: " + cls._workflow_compact(
                ["origin/" + name for name in related])
        return "无关联分支 / No reachable branches"

    @staticmethod
    def _workflow_local_time(value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M %z")
        except (ValueError, AttributeError, OSError):
            return value or "—"

    def _workflow_tags_loaded(self, result, *, reset):
        self.workflow_load_pending = False
        rows = result.get("runs", [])
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            self._workflow_load_failed(DesktopBackendError("workflow_invalid", "Invalid workflow data returned by backend."))
            return
        if reset:
            self.workflow_rows = []
            self.workflow_list.delete(*self.workflow_list.get_children())
            self._workflow_selected_index = None
            self.workflow_show_related.set(False)
            self.workflow_refreshed_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        self.workflow_loaded = True
        self.workflow_has_more = bool(result.get("has_more"))
        for row in rows:
            index = len(self.workflow_rows)
            self.workflow_rows.append(row)
            groups = self._workflow_job_groups(row)
            branches = row.get("branches") or {}
            branch_text = self._workflow_branch_summary(branches)
            state = row.get("status", "") or "unknown"
            if row.get("conclusion"):
                state += " / " + row["conclusion"]
            unavailable = "读取失败" if row.get("jobs_error") else "—"
            values = (
                state,
                self._workflow_compact([name for name, _ in groups["success"]]) or unavailable,
                self._workflow_compact([name for name, _ in groups["pending"]]) or unavailable,
                self._workflow_compact([name for name, _ in groups["failed"]]) or unavailable,
                branch_text, self._workflow_local_time(row.get("created_at")),
                f"#{row.get('run_number', '')} / {row.get('run_attempt', 1)}",
            )
            self.workflow_list.insert("", "end", iid=str(index), text=row.get("tag", ""), values=values,
                                      tags=("has_failures",) if groups["failed"] else ())
        if reset and self.workflow_rows:
            self.workflow_list.selection_set("0")
            self._workflow_selected()
        elif reset:
            self._set_text(self.workflow_detail, "没有可显示的 Tag 运行记录 / No matching tag runs")
        summary = (f"{self.workflow_name.get().strip()} · 首批刷新完成：{self.workflow_refreshed_at or '—'}；"
                   f"已显示 {len(self.workflow_rows)} 个 Tag。"
                   "状态不会自动更新，请点击刷新；按运行创建时间，每个 Tag 取最新一次。"
                   "仅匹配当前远端 Tag 和提交（已删除或移动的 Tag 可能不显示）；"
                   "Job 成功不代表当前环境版本。")
        jobs_unavailable = sum(bool(row.get("jobs_error")) for row in self.workflow_rows)
        branches_unavailable = sum(not (row.get("branches") or {}).get("available") for row in self.workflow_rows)
        if jobs_unavailable:
            summary += f" {jobs_unavailable} 条的部署 Job 未能读取，可刷新重试。"
        if branches_unavailable:
            summary += f" {branches_unavailable} 条的分支关联不可用。"
        if result.get("scan_limited"):
            summary += " 已达到历史扫描上限。"
        if result.get("warning"):
            summary += " 部分历史加载失败，可继续重试。"
        self.workflow_result.configure(text=summary)
        self.status.set("Workflow 数据已加载 / Workflow data loaded")
        self._update_controls()

    def _workflow_load_failed(self, error):
        self.workflow_load_pending = False
        self.workflow_result.configure(text="Workflow 加载失败；已有数据显示可能已过时 / Load failed; displayed data may be stale")
        self.status.set(error.message)
        self._update_controls()

    def _workflow_selected(self, _event=None):
        selected = self.workflow_list.selection()
        try:
            index = int(selected[0]) if selected else -1
            row = self.workflow_rows[index] if 0 <= index < len(self.workflow_rows) else None
        except (ValueError, IndexError):
            row, index = None, -1
        if row is None:
            self._set_text(self.workflow_detail, "选择运行查看详情 / Select a run to view details")
            self._update_controls()
            return
        if self._workflow_selected_index != index:
            self.workflow_show_related.set(False)
            self._workflow_selected_index = index
        groups = self._workflow_job_groups(row)
        lines = [f"Tag / ref: {row.get('tag', '')}",
                 f"Workflow: {row.get('status', '')} / {row.get('conclusion') or '—'}",
                 f"Run: #{row.get('run_number', '')} · attempt {row.get('run_attempt', 1)} · "
                 f"{self._workflow_local_time(row.get('created_at'))}",
                 f"Actor: {row.get('actor') or '—'}", ""]
        if row.get("jobs_error"):
            lines.append("部署环境未能读取 / Deployment jobs unavailable")
        else:
            if not row.get("jobs"):
                lines.append("没有检测到部署 Job（可能尚未运行或 Job 名称不同） / No deploy jobs detected")
            for key, title in (("success", "部署 Job 成功"), ("pending", "等待 / 运行"),
                               ("failed", "失败 / 取消"), ("other", "跳过 / 其他")):
                items = groups[key]
                lines.append(title + ": " + (", ".join(f"{name} ({state})" for name, state in items) or "—"))
        branches = row.get("branches") or {}
        lines.append("")
        if not branches.get("available"):
            lines.append("关联分支不可用（可能未 Fetch 到该提交） / Branch association unavailable")
        else:
            exact = branches.get("exact", [])
            lines.append("已 Fetch 分支头匹配: " + (", ".join("origin/" + name for name in exact) or "—"))
            if branches.get("exact_count", 0) > len(exact):
                lines.append(f"[分支头匹配列表截断，共 {branches['exact_count']} 个]")
            related = branches.get("related", [])
            count = branches.get("related_count", 0)
            if self.workflow_show_related.get() or not exact:
                lines.append("其他关联分支: " + (", ".join("origin/" + name for name in related) or "—"))
                if count > len(related):
                    lines.append(f"[关联分支列表截断，共 {count} 个]")
            else:
                lines.append(f"其他关联分支: {count} 个（可展开）")
        lines.append("\n✓ 表示该次 Job 的 Argo 同步及健康检查步骤成功；同步未确认不代表失败。"
                     "关联分支不代表 Tag 创建来源；Job 成功不保证环境当前仍运行该版本。")
        self._set_text(self.workflow_detail, "\n".join(lines))
        self._update_controls()

    def _open_workflow_run(self):
        selected = self.workflow_list.selection()
        try:
            row = self.workflow_rows[int(selected[0])] if selected else None
            run_id = row.get("id") if row else None
        except (ValueError, IndexError):
            run_id = None
        if self.repo_info and type(run_id) is int and run_id > 0:
            webbrowser.open(f"https://github.com/{self.repo_info['repository']}/actions/runs/{run_id}")

    def _tag_branch_filter_changed(self, *_args):
        self._tag_branch_default_active = False
        self._render_branch_tags()

    def _tag_filter_changed(self, *_args):
        self.tag_show_related.set(False)
        self._render_branch_tags()
        self._update_controls()

    @staticmethod
    def _matches_branch_filter(name, query):
        name = name.casefold()
        if query.startswith("origin/"):
            name = "origin/" + name
        if "*" in query or "?" in query:
            return fnmatch.fnmatchcase(name, query)
        return query in name

    def _tag_list_resized(self, event):
        if event.width == self._tag_canvas_width:
            return
        self._tag_canvas_width = event.width
        if self._tag_resize_after_id:
            self.after_cancel(self._tag_resize_after_id)
        self._tag_resize_after_id = self.after(120, self._redraw_resized_tags)

    def _redraw_resized_tags(self):
        self._tag_resize_after_id = None
        self._render_branch_tags(preserve_scroll=True)

    def _tag_list_mousewheel(self, event):
        self.tag_list.yview_scroll(-int(event.delta / 120) * 3, "units")
        return "break"

    def _render_branch_tags(self, *, preserve_scroll=False):
        position = self.tag_list.yview()[0] if preserve_scroll else 0.0
        self.tag_list.delete("all")
        self.tag_header.delete("all")
        self.tag_rows.clear()
        width = max(self.tag_list.winfo_width(), 500)
        branch_width = min(220, max(140, width // 4))
        action_width = 140
        tags_width = max(80, width - branch_width - action_width - 24)
        action_x = branch_width + tags_width + 16
        self.tag_header.create_text(8, 14, anchor="w", text="远程 Branch / Remote branch")
        self.tag_header.create_text(branch_width + 8, 14, anchor="w", text="关联 Tags / Reachable tags")
        self.tag_header.create_text(action_x, 14, anchor="w", text="操作 / Action")
        branch_query = self.tag_branch_search.get().strip().casefold()
        tag_query = self.tag_search.get().strip().casefold()
        # The default branch filter is suspended for Tag searches, but an
        # explicitly entered branch filter continues to narrow the results.
        effective_branch_query = "" if tag_query and self._tag_branch_default_active else branch_query
        exact = []
        related = []
        for branch in self.branch_tag_data:
            name = branch.get("name", "")
            tags = branch.get("tags", [])
            if effective_branch_query and not self._matches_branch_filter(name, effective_branch_query):
                continue
            if tag_query:
                matching_tags = [tag for tag in tags if tag_query in tag.get("name", "").casefold()]
                if not matching_tags:
                    continue
                branch_sha = branch.get("sha", "").casefold()
                tip_tags = [tag for tag in matching_tags if branch_sha
                            and isinstance(tag.get("sha"), str) and tag["sha"].casefold() == branch_sha]
                tip_match = bool(tip_tags)
                shown_tags = matching_tags[:5]
                if tip_match and not any(tag in tip_tags for tag in shown_tags):
                    shown_tags = matching_tags[:4] + [tip_tags[0]]
                (exact if tip_match else related).append((branch, shown_tags, tip_match))
            else:
                if not tags and (not branch_query or self._tag_branch_default_active):
                    continue
                exact.append((branch, tags[:5], False))
        visible = exact + (related if tag_query and self.tag_show_related.get() else [])
        rendered = 0
        y = 0
        for branch, shown_tags, tip_match in visible:
            name = branch.get("name", "")
            tag_names = ", ".join(tag.get("name", "") for tag in shown_tags) or "— 无 Tag / no tags —"
            relation = ("分支头匹配 / Fetched tip match" if tip_match else
                        "关联分支 / Reachable branch" if tag_query else "")
            branch_label = f"origin/{name}" + ("\n" + relation if relation else "")
            branch_item = self.tag_list.create_text(
                8, y + 8, anchor="nw", text=branch_label, width=branch_width - 12)
            tag_item = self.tag_list.create_text(
                branch_width + 8, y + 8, anchor="nw", text=tag_names, width=tags_width)
            self.tag_list.create_text(action_x, y + 8, anchor="nw", text="创建 Tag / Create tag",
                                      fill="#0969da", width=action_width - 8)
            branch_box = self.tag_list.bbox(branch_item)
            tag_box = self.tag_list.bbox(tag_item)
            row_bottom = max(y + 34, (branch_box[3] if branch_box else y) + 8,
                             (tag_box[3] if tag_box else y) + 8)
            self.tag_list.create_line(0, row_bottom, width, row_bottom, fill="#e5e9ef")
            self.tag_rows[str(rendered)] = {"branch": branch, "tags": tag_names,
                                            "relation": relation, "tag_item": tag_item, "top": y,
                                            "bottom": row_bottom, "action_x": action_x}
            rendered += 1
            y = row_bottom + 1
        self.tag_list.configure(scrollregion=(0, 0, width, max(y, 1)))
        self.tag_list.yview_moveto(position)
        if not self.branch_tag_data:
            self.tag_result.configure(text="尚未加载分支 Tag / Branch tags have not been loaded")
        elif tag_query:
            scope = "（搜索全部分支）" if self._tag_branch_default_active else "（按 Branch 筛选）"
            self.tag_result.configure(
                text=f"{scope} 分支头匹配 {len(exact)} 个，其他关联分支 {len(related)} 个；"
                     "可勾选显示关联分支。匹配不代表创建来源。 / "
                     f"{len(exact)} fetched tip matches, {len(related)} other reachable branches")
        else:
            self.tag_result.configure(
                text=f"找到 {rendered} 个匹配分支；每行最多 5 个关联 Tag（不代表创建分支） / "
                     f"{rendered} matching branches; up to 5 reachable tags per row")

    def _tag_list_click(self, event):
        if self.busy or not self.repo_info or not self.remote_fresh or not self.tags_fresh:
            return
        x = self.tag_list.canvasx(event.x)
        y = self.tag_list.canvasy(event.y)
        for row in self.tag_rows.values():
            if row["top"] <= y < row["bottom"] and x >= row["action_x"]:
                self.confirm_create_tag(row["branch"])
                return "break"

    def confirm_create_tag(self, branch_info):
        branch = branch_info.get("name", "")
        target_sha = self.remote_branch_shas.get(branch, branch_info.get("sha", ""))
        if self.busy or not self.repo_info or not self.remote_fresh or not self.tags_fresh or not branch or not target_sha:
            self.tag_result.configure(text="请先 Fetch 并选择有效远程分支 / Fetch and select a valid remote branch first")
            return
        tag_name = tk.StringVar()
        dialog = tk.Toplevel(self)
        dialog.title("创建远端 Tag / Create remote tag")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("600x280")
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=(f"仓库 / Repository: {self.repo_info['repository']}\n"
                               f"目标分支 / Branch: origin/{branch}\n"
                               f"目标 Commit / Commit: {target_sha}"),
                  justify="left").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="Tag 名 / Tag name").grid(row=1, column=0, sticky="w", pady=(12, 4))
        name_entry = ttk.Entry(frame, textvariable=tag_name)
        name_entry.grid(row=2, column=0, sticky="ew")
        suggestion_status = ttk.Label(frame, text="", wraplength=560)
        suggestion_status.grid(row=3, column=0, sticky="w", pady=(6, 0))
        bar = ttk.Frame(frame)
        bar.grid(row=4, column=0, sticky="e", pady=(16, 0))
        ttk.Button(bar, text="取消 / Cancel", command=dialog.destroy).pack(side="right", padx=4)
        create_button = ttk.Button(bar, text="确认创建远端 Tag / Create remote tag", state="disabled")
        create_button.pack(side="right", padx=4)
        pending = {"suggestion": branch in self.tag_suggestion_branches}

        def update_create_button(*_args):
            if dialog.winfo_exists():
                create_button.configure(state="normal" if tag_name.get().strip() and not pending["suggestion"] else "disabled")

        def confirm():
            name = tag_name.get().strip()
            if not name or pending["suggestion"]:
                return
            dialog.destroy()
            self._run_async(
                "检查远端 Tag 重名并创建… / Checking for a remote tag name conflict…",
                lambda: self.backend.create_remote_tag(branch, name, confirmed=True),
                self._tag_created,
            )

        create_button.configure(command=confirm)
        tag_name.trace_add("write", update_create_button)
        name_entry.focus_set()

        if pending["suggestion"]:
            suggestion_status.configure(text="正在读取整个远端仓库的 Tag… / Checking all origin tags…")

            def suggestion_loaded(result):
                pending["suggestion"] = False
                if not dialog.winfo_exists():
                    return
                suggestion = result.get("suggestion", "")
                if suggestion:
                    if not tag_name.get().strip():
                        tag_name.set(suggestion)
                    suggestion_status.configure(
                        text="已按远端最新版本建议；创建前仍会检查重名 / Suggested from origin; duplicates are checked before creation")
                else:
                    suggestion_status.configure(
                        text="远端没有同格式 Tag，请手动输入 / No matching origin tags; enter a name manually")
                update_create_button()

            def suggestion_failed(error):
                pending["suggestion"] = False
                if not dialog.winfo_exists():
                    return
                suggestion_status.configure(text="无法读取远端版本，请手动输入 / Could not check origin versions; enter a name manually")
                self.status.set(error.message)
                update_create_button()

            self._run_async("读取远端 Tag 版本… / Checking origin tag versions…",
                            lambda: self.backend.suggest_tag_name(branch), suggestion_loaded,
                            on_error=suggestion_failed)

    def _tag_created(self, result):
        self.tags_fresh = False
        self._reset_workflow_view()
        self.tag_result.configure(
            text=f"已在 GitHub 创建 Tag {result.get('tag')} → origin/{result.get('branch')} / "
                 "Remote tag created at the selected branch tip")
        self._update_controls()
        self.load_branch_tags()

    def _branch_selection_changed(self, _event=None):
        self._invalidate_selection()
        base = self._selected_remote_branch(self.base)
        source = self._selected_remote_branch(self.source)
        if not self.repo_info or not self.remote_fresh or self.busy or not (base or source):
            self._schedule_auto_compare()
            return
        self._run_async("记住此仓库的分支选择… / Remembering branch selection…",
                        lambda: self.backend.remember_branch_selection(base, source),
                        self._branch_selection_saved,
                        on_error=self._branch_selection_save_failed)

    def _branch_selection_saved(self, _result):
        self.status.set("已记住此仓库的分支选择 / Branch selection saved for this repository")
        self._schedule_auto_compare()

    def _branch_selection_save_failed(self, error):
        self.status.set("分支选择未能保存 / Branch selection could not be saved")
        messagebox.showwarning("分支选择未保存 / Branches not saved", error.message, parent=self)
        self._schedule_auto_compare()

    def _schedule_auto_compare(self):
        if self._compare_after_id:
            try:
                self.after_cancel(self._compare_after_id)
            except tk.TclError:
                pass
            self._compare_after_id = None
        base = self._selected_remote_branch(self.base)
        source = self._selected_remote_branch(self.source)
        if self.busy or not self.remote_fresh or not base or not source or base == source:
            return
        self._compare_after_id = self.after(350, self._run_auto_compare)

    def _run_auto_compare(self):
        self._compare_after_id = None
        if self.busy:
            self._compare_after_id = self.after(200, self._run_auto_compare)
            return
        self.compare()

    def compare(self):
        if self._compare_after_id:
            try:
                self.after_cancel(self._compare_after_id)
            except tk.TclError:
                pass
            self._compare_after_id = None
        base, source = self._selected_remote_branch(self.base), self._selected_remote_branch(self.source)
        if not base or not source or base == source:
            self.status.set("请选择两个不同的远程分支 / Choose two distinct remote branches")
            return
        self._run_async("读取远程比较… / Comparing remote branches…",
                        lambda: self.backend.compare(base, source), self._compared)

    def _compared(self, result):
        context = (self.repo_info["repository"], result.get("base"), result.get("source"))
        if self.draft_context != context:
            self.selected_reviewers = []
            self.special_reviewers.clear()
        self.verified_reviewers.clear()
        self._render_selected_reviewers()
        self.comparison = result
        self.inspection = None
        self.reviewer_results.delete(0, "end")
        self.reviewers = []
        if self.draft_context != context:
            self.title_var.set(result.get("default_title", ""))
            self.body.delete("1.0", "end")
            self.body.insert("1.0", result.get("default_body", ""))
        self.draft_context = context
        self.file_items = list(result.get("files", []))
        self._render_file_tree()
        self._set_patch("Select a changed file to view its remote patch.")
        self.summary.set(
            f"{result.get('status', 'unknown')} · +{result.get('ahead_by', '?')} / "
            f"-{result.get('behind_by', '?')} commits · {len(self.file_items)} files · "
            f"complete={result.get('complete')}\n{result.get('base_sha', '')[:7]}...{result.get('source_sha', '')[:7]}")
        self.source_commit_items = list(result.get("source_commits", []))
        self._commit_detail_cache.clear()
        self._pending_commit_sha = None
        self.commit_list.delete(*self.commit_list.get_children())
        for index, item in enumerate(self.source_commit_items):
            self.commit_list.insert("", "end", iid=str(index), text=item.get("sha", "")[:8],
                                    values=(item.get("authored_at", ""), item.get("author", ""),
                                            item.get("subject", "")))
        truncated = "（最多 100 条） / (maximum 100)" if result.get("source_commits_truncated") else ""
        self.commit_summary.set(f"{len(self.source_commit_items)} 条来源分支提交 {truncated}")
        self._set_text(self.commit_view, "选择提交查看完整说明 / Select a commit to view its message")
        self.pr_result.configure(text="")
        self.pr_link.configure(text="")
        self.result_url = ""
        if result.get("complete"):
            self.status.set("远程比较完成 / Remote comparison complete")
        else:
            self.status.set("远程比较不完整，PR 操作不可用 / Incomplete comparison; PR actions are disabled")
        self._update_controls()
        if result.get("complete"):
            self.pr_result.configure(text="检查匹配的开放 PR… / Checking for a matching open PR…")
            self.inspect()

    def _clear_commit_history(self, summary):
        self.source_commit_items = []
        self._commit_detail_cache.clear()
        self._pending_commit_sha = None
        self.commit_list.delete(*self.commit_list.get_children())
        self.commit_summary.set(summary)
        self._set_text(self.commit_view, "")

    def _selected_commit_sha(self):
        selection = self.commit_list.selection()
        if not selection:
            return ""
        try:
            return self.source_commit_items[int(selection[0])]["sha"]
        except (ValueError, IndexError, KeyError):
            return ""

    def _commit_selected(self, _event=None):
        sha = self._selected_commit_sha()
        if not sha:
            return
        self._pending_commit_sha = None
        if sha in self._commit_detail_cache:
            self._set_text(self.commit_view, self._commit_detail_cache[sha])
        elif self.busy:
            self._pending_commit_sha = sha
            self._set_text(self.commit_view, "等待当前操作完成… / Waiting for the current operation…")
        else:
            self._load_commit_detail(sha)

    def _load_commit_detail(self, sha):
        if sha != self._selected_commit_sha() or not self.comparison:
            return
        if sha in self._commit_detail_cache:
            self._set_text(self.commit_view, self._commit_detail_cache[sha])
            return
        self._set_text(self.commit_view, "读取提交说明… / Loading commit message…")
        self._run_async("读取提交详情… / Loading commit details…",
                        lambda: self.backend.source_commit_detail(sha),
                        lambda result: self._commit_detail_loaded(sha, result),
                        on_error=lambda error: self._commit_detail_failed(sha, error))

    def _commit_detail_loaded(self, sha, result):
        message = result.get("message", "") or "（无提交说明 / No commit message）"
        if result.get("truncated"):
            message += "\n\n[提交说明超过长度限制，已截断 / Commit message truncated]"
        self._commit_detail_cache[sha] = message
        if sha == self._selected_commit_sha():
            self._set_text(self.commit_view, message)

    def _commit_detail_failed(self, sha, error):
        self.status.set(error.message)
        if sha == self._selected_commit_sha():
            self._set_text(self.commit_view, "读取提交说明失败 / Could not load commit message")

    def refresh_activity(self):
        self._run_async("读取本次运行日志… / Loading activity log…",
                        self.backend.activity, self._render_activity)

    def _render_activity(self, result):
        records = result.get("records", []) if isinstance(result, dict) else []
        lines = [f"{item.get('time', '')} · {item.get('stage', '')} · "
                 f"{item.get('outcome', '')} · {item.get('detail', '')}"
                 for item in records]
        self._set_text(self.activity_view, "\n".join(lines) or "No activity yet / 暂无运行记录")
        self.activity_view.see("end")

    @staticmethod
    def _set_text(widget, value):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _render_file_tree(self):
        self.files.delete(*self.files.get_children())
        self.file_tree_files.clear()
        directories = {}
        totals = {}
        for index, item in enumerate(self.file_items):
            filename = item.get("filename", "")
            parts = filename.split("/")
            parent = ""
            for depth, part in enumerate(parts[:-1], 1):
                path = tuple(parts[:depth])
                if path not in directories:
                    iid = f"dir:{len(directories)}"
                    directories[path] = iid
                    totals[iid] = {"count": 0, "add": 0, "delete": 0,
                                   "add_known": True, "delete_known": True}
                    self.files.insert(parent, "end", iid=iid, text=(part or "(empty)") + "/", open=True)
                parent = directories[path]
                total = totals[parent]
                total["count"] += 1
                for field, key in (("additions", "add"), ("deletions", "delete")):
                    value = item.get(field)
                    if type(value) is int and value >= 0:
                        total[key] += value
                    else:
                        total[key + "_known"] = False
            iid = f"file:{index}"
            self.file_tree_files[iid] = index
            additions, deletions = item.get("additions"), item.get("deletions")
            self.files.insert(parent, "end", iid=iid, text=parts[-1] or filename or "(empty)",
                              values=("", additions if type(additions) is int and additions >= 0 else "?",
                                      deletions if type(deletions) is int and deletions >= 0 else "?"))
        for iid, total in totals.items():
            self.files.item(iid, values=(total["count"], total["add"] if total["add_known"] else "?",
                                         total["delete"] if total["delete_known"] else "?"))
        for parent in ("", *directories.values()):
            children = self.files.get_children(parent)
            ordered = sorted(children, key=lambda iid: (
                0 if iid in totals else 1, self.files.item(iid, "text").casefold(),
                self.files.item(iid, "text")))
            for position, iid in enumerate(ordered):
                self.files.move(iid, parent, position)

    def show_file(self, _event=None):
        selection = self.files.selection()
        if not selection:
            return
        index = self.file_tree_files.get(selection[0])
        if index is None:
            self._set_patch("请选择文件查看差异 / Select a file to view its patch.")
            return
        item = self.file_items[index]
        if item.get("patch_status") != "available" or not isinstance(item.get("patch"), str):
            status = item.get("patch_status", "unavailable")
            message = "Patch is oversized." if status == "oversized" else "GitHub did not provide a complete patch."
            self._set_patch(message)
            return
        self._set_patch(item["patch"])

    def _set_patch(self, text):
        self.patch.configure(state="normal")
        self.patch.delete("1.0", "end")
        self.patch.insert("1.0", text)
        for row, line in enumerate(text.splitlines(), 1):
            tag = "add" if line.startswith("+") else "del" if line.startswith("-") else "hunk" if line.startswith("@@") else None
            if tag:
                self.patch.tag_add(tag, f"{row}.0", f"{row}.end")
        self.patch.configure(state="disabled")

    def search_reviewers(self):
        query = self.query.get().strip()
        if not query:
            self.status.set("请输入审核人搜索词 / Enter a reviewer search query")
            return
        self._run_async("搜索可分配审核人… / Searching assignable reviewers…",
                        lambda: self.backend.search_reviewers(query), self._reviewers_found)

    def _reviewers_found(self, result):
        self.reviewers = result.get("reviewers", [])
        self.special_reviewers.update(
            item["login"] for item in self.reviewers if item.get("kind") == "copilot")
        returned = {item["login"].casefold(): item["login"] for item in self.reviewers}
        canonical = []
        self.verified_reviewers.clear()
        for login in self.selected_reviewers:
            match = returned.get(login.casefold())
            if match:
                canonical.append(match)
                self.verified_reviewers.add(match)
            else:
                canonical.append(login)
        self.selected_reviewers = canonical
        self.reviewer_results.delete(0, "end")
        for item in self.reviewers:
            if item.get("kind") == "copilot":
                label = "GitHub Copilot code review / Copilot 代码审查"
            else:
                label = f"{item['login']} — {item['name']}" if item.get("name") else item["login"]
            self.reviewer_results.insert("end", label)
        self.status.set(f"找到 {len(self.reviewers)} 个可分配候选人 / {len(self.reviewers)} assignable candidates")
        self._render_selected_reviewers()
        self._update_controls()

    def add_reviewer(self):
        selection = self.reviewer_results.curselection()
        if not selection:
            return
        candidate = self.reviewers[selection[0]]
        login = candidate["login"]
        if login.casefold() not in {item.casefold() for item in self.selected_reviewers}:
            self.selected_reviewers.append(login)
        self.verified_reviewers.add(login)
        if candidate.get("kind") == "copilot":
            self.special_reviewers.add(login)
        self._render_selected_reviewers()
        self._update_controls()

    def remove_reviewer(self):
        selection = self.reviewer_selected.curselection()
        if not selection:
            return
        login = self.selected_reviewers.pop(selection[0])
        self.verified_reviewers.discard(login)
        self.special_reviewers.discard(login)
        self._render_selected_reviewers()
        self._update_controls()

    def _render_selected_reviewers(self):
        self.reviewer_selected.delete(0, "end")
        for login in self.selected_reviewers:
            if login in self.special_reviewers:
                label = "GitHub Copilot code review / Copilot 代码审查"
                marker = " · 特殊 reviewer / special reviewer"
            else:
                label = login
                marker = "" if login in self.verified_reviewers else " · 未验证 / unverified"
            self.reviewer_selected.insert("end", label + marker)

    def save_settings(self):
        if not self.repo_info:
            self.status.set("先连接本地仓库 / Connect a local repository first")
            return
        draft = {
            "repository": self.repo_info["repository"],
            "local_repository_path": self.workspace_path.get(),
            "base_branch": self._selected_remote_branch(self.base),
            "source_branch": self._selected_remote_branch(self.source),
            "title": self.title_var.get(),
            "body": self.body.get("1.0", "end-1c"),
            "reviewers": list(self.selected_reviewers),
        }
        self._run_async("保存本地 PR 设置（不含 token）… / Saving local PR settings (no token)…",
                        lambda: self.backend.save_settings(draft),
                        lambda _result: self.status.set("设置已保存；reviewer 下次加载时需重新验证 / Settings saved; reviewers will be revalidated next time"))

    def inspect(self):
        base, source = self._selected_remote_branch(self.base), self._selected_remote_branch(self.source)
        self._run_async("检查是否已有匹配的开放 PR… / Inspecting open PRs…",
                        lambda: self.backend.inspect(base, source), self._inspected)

    def _inspected(self, result):
        self.inspection = result
        if result.get("exists"):
            pr = result.get("pull_request") or {}
            self.result_url = pr.get("url", "")
            self.pr_result.configure(text=f"已有匹配的开放 PR #{pr.get('number')}：{pr.get('title', '')} / Existing matching open PR")
            self.pr_link.configure(text=self.result_url)
            self.status.set("发现匹配 PR；已禁用重复创建 / Matching PR found; duplicate creation disabled")
        else:
            self.result_url = ""
            self.pr_link.configure(text="")
            self.pr_result.configure(text="未发现匹配的开放 PR / No matching open PR found")
            self.status.set("可核对标题和正文后创建 / Review the title and description before creating")
        self._update_controls()

    def confirm_create(self):
        if not self._can_create():
            return
        base, source = self._selected_remote_branch(self.base), self._selected_remote_branch(self.source)
        title = self.title_var.get().strip()
        body = self.body.get("1.0", "end-1c")
        reviewers = list(self.selected_reviewers)
        info = self.comparison
        dialog = tk.Toplevel(self)
        dialog.title("核对并确认 PR / Review and confirm PR")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("760x600")
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        summary = (f"Repository: {self.repo_info['repository']}\nBase: origin/{base}\nSource: origin/{source}\n"
                   f"SHAs: {info['base_sha'][:7]}...{info['source_sha'][:7]}\n"
                   f"Reviewers: {', '.join('GitHub Copilot code review' if login in self.special_reviewers else login for login in reviewers) if reviewers else 'None'}\n\n"
                   f"Title: {title}\n\nBody:\n{body}")
        text = tk.Text(frame, wrap="word")
        text.grid(row=0, column=0, sticky="nsew")
        text.insert("1.0", summary)
        text.configure(state="disabled")
        bar = ttk.Frame(frame)
        bar.grid(row=2, column=0, sticky="e", pady=(10, 0))
        ttk.Button(bar, text="返回编辑 / Back", command=dialog.destroy).pack(side="right", padx=4)

        def confirm():
            dialog.destroy()
            self._run_async(
                "向 GitHub 提交一次 PR 创建请求… / Submitting one PR create request…",
                lambda: self.backend.create(base, source, title, body, reviewers, confirmed=True),
                self._created,
            )

        ttk.Button(bar, text="确认并创建 / Confirm and create", command=confirm).pack(side="right", padx=4)

    def _created(self, result):
        if result.get("created"):
            pr = result.get("pull_request") or {}
            self.result_url = pr.get("url", "")
            copilot = result.get("copilot_review") or {}
            copilot_status = (
                "；Copilot review 已请求 / Copilot review requested"
                if copilot.get("confirmed") else
                "；Copilot review 未确认，请检查 PR / Copilot review not confirmed; inspect the PR"
                if copilot.get("requested") else "")
            self.pr_result.configure(text=f"PR 已创建 #{pr.get('number')} / Pull request created{copilot_status}")
            self.pr_link.configure(text=self.result_url)
            self.status.set("PR 已创建；审核人请求结果见返回状态 / PR created; reviewer outcome is in the result")
        elif result.get("existing"):
            pr = result["existing"]
            self.result_url = pr.get("url", "")
            self.pr_result.configure(text=f"创建前发现已有 PR #{pr.get('number')}，未重复创建 / Existing PR; no duplicate created")
            self.pr_link.configure(text=self.result_url)
            self.inspection = {"exists": True, "pull_request": pr}
            self.status.set("没有创建重复 PR / No duplicate PR was created")
        self._update_controls()

    def sign_out(self):
        self.backend.sign_out()
        self._clear_repository_state(keep_path=True)
        self.account.set("Not connected / 未连接")
        self.status.set("已断开；Git 凭据仍由本机 Git helper 管理 / Disconnected; Git helper storage is unchanged")

    def _invalidate_selection(self):
        self.comparison = None
        self.inspection = None
        self.files.delete(*self.files.get_children())
        self.file_items = []
        self.file_tree_files.clear()
        self._set_patch("Compare the new branch pair to view remote patches.")
        self._clear_commit_history("分支已变更，请等待重新比较 / Branch changed; waiting for comparison")
        self.summary.set("分支选择已变更，正在重新比较 / Branch selection changed; comparison will rerun")
        self.pr_result.configure(text="")
        self.pr_link.configure(text="")
        self.result_url = ""
        self._update_controls()

    def _clear_repository_state(self, *, keep_path=False):
        if self._compare_after_id:
            try:
                self.after_cancel(self._compare_after_id)
            except tk.TclError:
                pass
            self._compare_after_id = None
        self.backend.sign_out()
        self.repo_info = None
        self.remote_fresh = False
        self.tags_fresh = False
        self.tag_load_pending = False
        self.comparison = None
        self.inspection = None
        self.file_items = []
        self.file_tree_files.clear()
        self.remote_branches = []
        self.remote_branch_shas = {}
        self.branch_tag_data = []
        self.tag_rows.clear()
        self.tag_search.set("")
        self.tag_branch_search.set("")
        self.tag_suggestion_branches.clear()
        repository_name = (self.repo_info["repository"].rsplit("/", 1)[-1].casefold()
                           if self.repo_info else "")
        self.workflow_name.set("build-and-push" if repository_name in (
            "tsp-common-model", "tsp-common-lib") else "build-and-deploy")
        self._tag_branch_default_active = True
        self.tag_list.delete("all")
        self.tag_result.configure(text="")
        self._reset_workflow_view()
        self.reviewers = []
        self.selected_reviewers = []
        self.verified_reviewers.clear()
        self.special_reviewers.clear()
        self.draft_context = None
        self.base.set("")
        self.source.set("")
        self.base_search.set("")
        self.source_search.set("")
        self.base_box.configure(values=())
        self.source_box.configure(values=())
        self.base_search_count.set("0")
        self.source_search_count.set("0")
        self.files.delete(*self.files.get_children())
        self._set_patch("Choose and connect a local repository.")
        self.summary.set("选择本地仓库并连接 / Choose and connect a local repository")
        self._clear_commit_history("Fetch origin and compare remote branches to view source commits.")
        self.reviewer_results.delete(0, "end")
        self.reviewer_selected.delete(0, "end")
        self.pr_result.configure(text="")
        self.pr_link.configure(text="")
        self.result_url = ""
        self.account.set("Not connected / 未连接")
        if not keep_path:
            self.repo_path.set("")
        self._update_controls()

    def _filter_branch_options(self):
        def filter_values(query):
            term = query.strip().casefold()
            return [f"origin/{name}" for name in self.remote_branches
                    if not term or term in name.casefold() or term in f"origin/{name}".casefold()]

        base_matches = filter_values(self.base_search.get())
        source_matches = filter_values(self.source_search.get())
        self.base_box.configure(values=base_matches)
        self.source_box.configure(values=source_matches)
        total = len(self.remote_branches)
        self.base_search_count.set(f"{len(base_matches)}/{total}")
        self.source_search_count.set(f"{len(source_matches)}/{total}")

    @staticmethod
    def _selected_remote_branch(variable):
        value = variable.get()
        return value[len("origin/"):] if value.startswith("origin/") else ""

    def _can_create(self):
        return bool(self.repo_info and self.comparison and self.comparison.get("complete")
                    and isinstance(self.comparison.get("ahead_by"), int)
                    and self.comparison["ahead_by"] > 0 and self.inspection
                    and not self.inspection.get("exists") and self.title_var.get().strip()
                    and all(login in self.verified_reviewers for login in self.selected_reviewers)
                    and not self.busy)

    def _update_controls(self):
        state = "disabled" if self.busy else "normal"
        self.browse_button.configure(state=state)
        self.repo_tree.configure(selectmode="none" if self.busy else "browse")
        self.connect_button.configure(state="normal" if self.repo_path.get() and not self.busy else "disabled")
        self.token_button.configure(state="normal" if self.repo_path.get() and not self.busy else "disabled")
        self.sign_out_button.configure(state="normal" if self.repo_info and not self.busy else "disabled")
        branch_state = "readonly" if self.repo_info and not self.busy else "disabled"
        self.base_box.configure(state=branch_state)
        self.source_box.configure(state=branch_state)
        search_state = "normal" if self.repo_info and not self.busy else "disabled"
        self.base_search_entry.configure(state=search_state)
        self.source_search_entry.configure(state=search_state)
        self.compare_button.configure(
            state="normal" if self.repo_info and self.remote_fresh and not self.busy else "disabled")
        self.reviewer_search_button.configure(state="normal" if self.comparison and not self.busy else "disabled")
        self.reviewer_add_button.configure(state="normal" if self.reviewers and not self.busy else "disabled")
        self.reviewer_remove_button.configure(state="normal" if self.selected_reviewers and not self.busy else "disabled")
        self.save_button.configure(state="normal" if self.repo_info and not self.busy else "disabled")
        self.fetch_button.configure(state="normal" if self.repo_info and not self.busy else "disabled")
        tag_search_state = "normal" if self.repo_info and self.tags_fresh and not self.busy else "disabled"
        self.tag_branch_search_entry.configure(state=tag_search_state)
        self.tag_search_entry.configure(state=tag_search_state)
        self.tag_related_checkbox.configure(
            state="normal" if tag_search_state == "normal" and self.tag_search.get().strip() else "disabled")
        workflow_ready = bool(self.repo_info and not self.busy)
        self.workflow_refresh_button.configure(state="normal" if workflow_ready else "disabled")
        self.workflow_more_button.configure(
            state="normal" if workflow_ready and self.workflow_loaded and self.workflow_has_more else "disabled")
        self.workflow_name_entry.configure(state="readonly" if workflow_ready else "disabled")
        workflow_selected = self.workflow_list.selection()
        selected_row = None
        if workflow_selected:
            try:
                index = int(workflow_selected[0])
                if 0 <= index < len(self.workflow_rows):
                    selected_row = self.workflow_rows[index]
            except ValueError:
                pass
        self.workflow_open_button.configure(state="normal" if workflow_ready and selected_row else "disabled")
        self.workflow_related_checkbox.configure(
            state="normal" if workflow_ready and selected_row
            and (selected_row.get("branches") or {}).get("related_count", 0) else "disabled")
        can_inspect = bool(self.comparison and self.comparison.get("complete") and not self.busy)
        self.inspect_button.configure(state="normal" if can_inspect else "disabled")
        self.create_button.configure(state="normal" if self._can_create() else "disabled")

    def _run_async(self, message, operation, callback, *, on_error=None):
        if self.busy:
            return
        self.busy = True
        self.status.set(message)
        self._update_controls()

        def worker():
            try:
                result = operation()
                self.events.put((callback, result, None, on_error))
            except DesktopBackendError as error:
                self.events.put((callback, None, error, on_error))
            except Exception:
                error = DesktopBackendError("internal_error", "The operation failed unexpectedly. Check the sanitized activity log.")
                self.events.put((callback, None, error, on_error))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_events(self):
        self._drain_after_id = None
        try:
            while True:
                callback, result, error, on_error = self.events.get_nowait()
                self.busy = False
                if error:
                    if on_error:
                        on_error(error)
                    else:
                        self.status.set(error.message)
                else:
                    callback(result)
                if not self.busy:
                    try:
                        self._render_activity(self.backend.activity())
                    except DesktopBackendError:
                        pass
                self._update_controls()
        except queue.Empty:
            pass
        if not self.busy and self._pending_commit_sha:
            sha, self._pending_commit_sha = self._pending_commit_sha, None
            self._load_commit_detail(sha)
        if (not self.busy and self.tag_load_pending and self._tag_tab_is_selected()):
            self.load_branch_tags()
        if (not self.busy and self.workflow_load_pending
                and self.notebook.select() == str(self.workflow_tab)):
            self.load_workflow_tags(reset=True)
        try:
            self._drain_after_id = self.after(80, self._drain_events)
        except tk.TclError:
            self._drain_after_id = None

    def destroy(self):
        for attribute in ("_drain_after_id", "_restore_after_id", "_compare_after_id",
                          "_tag_resize_after_id", "_environment_check_after_id"):
            after_id = getattr(self, attribute, None)
            if after_id:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
                setattr(self, attribute, None)
        super().destroy()

    def _close(self):
        self.backend.sign_out()
        close = getattr(self.backend, "close", None)
        if callable(close):
            close()
        self.destroy()


def main():
    DesktopApp().mainloop()


if __name__ == "__main__":
    main()
