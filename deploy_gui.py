#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Visual front end for auto_deploy.py.

Shows each of the 8 deployment stages with a live status (pending / running /
done / failed / aborted / skipped), streams command output into a log pane,
and lets you stop the run at any point via a Stop button - the in-flight
command is killed and the pipeline halts before the next stage starts.

This is a thin GUI: all the actual deployment logic (what commands to run,
in what order, how to detect success) lives in auto_deploy.py. This file
only adds visualization and cancellation on top of it, using the pluggable
HOOKS / STOP_EVENT / run_stages() machinery that module exposes.

Usage:
  python deploy_gui.py
"""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import auto_deploy as ad

STATUS_COLORS = {
    "pending": "#888888",
    "running": "#1a73e8",
    "done": "#188038",
    "failed": "#d93025",
    "aborted": "#e37400",
    "skipped": "#999999",
}
STATUS_LABELS = {
    "pending": "Pending",
    "running": "Running...",
    "done": "Done",
    "failed": "Failed",
    "aborted": "Stopped",
    "skipped": "Skipped",
}


class GuiHooks(ad.UIHooks):
    """Bridges auto_deploy's stage logic (running on a worker thread) to
    Tkinter widgets (which may only be touched from the main thread)."""

    def __init__(self, app):
        self.app = app

    def confirm(self, prompt, auto_yes):
        if auto_yes:
            return True
        return self.app.ask_on_main(lambda: messagebox.askyesno("Confirm", prompt))

    def ask_text(self, prompt, default=""):
        result = self.app.ask_on_main(
            lambda: simpledialog.askstring("Input needed", prompt, initialvalue=default,
                                            parent=self.app.root))
        return (result or "").strip() or default

    def emit(self, line):
        self.app.log_queue.put(line)

    def emit_progress(self, text):
        self.app.progress_queue.put(text)

    def stage_status(self, name, status):
        self.app.status_queue.put((name, status))


class DeployApp:
    def __init__(self, root):
        self.root = root
        root.title("xiaozhi-esp32-server - Auto Deploy")
        # Fit common laptop screens while giving the process log enough room.
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        width = min(1100, max(800, screen_w - 80))
        height = min(820, max(600, screen_h - 100))
        root.geometry(f"{width}x{height}")
        root.minsize(min(820, width), min(600, height))

        self.log_queue = queue.Queue()
        self.progress_queue = queue.Queue()
        self.status_queue = queue.Queue()
        self.result_queue = queue.Queue()
        self.ui_requests = queue.Queue()
        self.worker = None
        self.last_run_success = None

        self.all_stages = list(ad.STAGES) + list(ad.OPTIONAL_STAGES)
        self.active_stages = list(ad.STAGES)  # stages included in the most recent/current run
        self.stage_vars = {name: tk.BooleanVar(value=True) for name in ad.STAGES}
        self.stage_vars.update({name: tk.BooleanVar(value=False) for name in ad.OPTIONAL_STAGES})
        self.auto_yes_var = tk.BooleanVar(value=False)
        self.no_install_var = tk.BooleanVar(value=False)
        self.no_mirror_var = tk.BooleanVar(value=False)
        self.reconfigure_var = tk.BooleanVar(value=False)
        self.restart_var = tk.BooleanVar(value=False)

        self._build_widgets()
        ad.HOOKS = GuiHooks(self)
        self.root.after(100, self._poll)
        self.refresh_status()

    # -- layout -----------------------------------------------------------
    def _build_widgets(self):
        # A movable sash lets users trade control-panel space for log space.
        # The log pane has a minimum height and receives all extra height.
        self.main_pane = tk.PanedWindow(
            self.root,
            orient="vertical",
            sashrelief="raised",
            sashwidth=7,
            bd=0,
            opaqueresize=True,
        )
        self.main_pane.pack(fill="both", expand=True)

        top = ttk.Frame(self.main_pane, padding=8)
        self.main_pane.add(top, minsize=300, stretch="never")
        self.top_panel = top

        left = ttk.LabelFrame(top, text="Stages", padding=8)
        left.pack(side="left", fill="y")
        self.stage_tree = ttk.Treeview(left, columns=("status",), show="tree headings",
                                        height=len(self.all_stages), selectmode="none")
        self.stage_tree.heading("#0", text="Stage")
        self.stage_tree.heading("status", text="Status")
        self.stage_tree.column("#0", width=140)
        self.stage_tree.column("status", width=110)
        for name in self.all_stages:
            self.stage_tree.insert("", "end", iid=name, text=name,
                                    values=(STATUS_LABELS["pending"],))
        self.stage_tree.pack()

        check_frame = ttk.Frame(left)
        check_frame.pack(fill="x", pady=(8, 0))
        for index, name in enumerate(ad.STAGES):
            ttk.Checkbutton(check_frame, text=f"run {name}", variable=self.stage_vars[name]
                             ).grid(row=index // 2, column=index % 2,
                                    sticky="w", padx=(0, 12))
        option_row = (len(ad.STAGES) + 1) // 2
        ttk.Separator(check_frame, orient="horizontal").grid(
            row=option_row, column=0, columnspan=2, sticky="ew", pady=4
        )
        ttk.Label(check_frame, text="optional add-ons:").grid(
            row=option_row + 1, column=0, columnspan=2, sticky="w"
        )
        for index, name in enumerate(ad.OPTIONAL_STAGES):
            ttk.Checkbutton(check_frame, text=f"run {name}", variable=self.stage_vars[name]
                             ).grid(row=option_row + 2, column=index,
                                    sticky="w", padx=(0, 12))

        right = ttk.Frame(top, padding=(12, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)

        opts = ttk.LabelFrame(right, text="Options", padding=8)
        opts.pack(fill="x")
        option_widgets = [
            ("Auto-confirm installs (--yes)", self.auto_yes_var),
            ("Check only; never install missing tools (--no-install)", self.no_install_var),
            ("Skip China mirror config (--no-mirror)", self.no_mirror_var),
            ("Redo secret/config step (--reconfigure)", self.reconfigure_var),
            ("Restart selected running services (--restart)", self.restart_var),
        ]
        for index, (label, variable) in enumerate(option_widgets):
            ttk.Checkbutton(opts, text=label, variable=variable).grid(
                row=index // 2, column=index % 2, sticky="w", padx=(0, 18)
            )
        opts.columnconfigure(0, weight=1)
        opts.columnconfigure(1, weight=1)

        btns = ttk.Frame(right, padding=(0, 8))
        btns.pack(fill="x")
        self.start_btn = ttk.Button(btns, text="Start", command=self.on_start)
        self.start_btn.pack(side="left", padx=(0, 6))
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Refresh Status", command=self.refresh_status).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Stop All Services", command=self.on_stop_services).pack(side="left")

        links = ttk.Frame(right)
        links.pack(fill="x")
        ttk.Button(
            links, text="Open Admin Console",
            command=lambda: ad.webbrowser.open(f"http://127.0.0.1:{ad.WEB_PORT}"),
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            links, text="Open Text Chat",
            command=lambda: ad.webbrowser.open(
                f"http://127.0.0.1:{ad.HTTP_PORT}/text_chat"
            ),
        ).pack(side="left")

        self.progress = ttk.Progressbar(right, maximum=len(ad.STAGES), value=0)
        self.progress.pack(fill="x", pady=(4, 0))
        self.activity_label = ttk.Label(right, text="Idle.")
        self.activity_label.pack(fill="x", anchor="w")

        status_frame = ttk.LabelFrame(right, text="Live status", padding=8)
        status_frame.pack(fill="x", pady=(8, 0))
        status_frame.columnconfigure(0, weight=1)
        status_frame.rowconfigure(0, weight=1)
        self.status_box = tk.Text(
            status_frame,
            height=9,
            wrap="none",
            state="disabled",
            font=("Consolas", 9),
            relief="flat",
            background=self.root.cget("background"),
        )
        status_scroll = ttk.Scrollbar(
            status_frame, orient="vertical", command=self.status_box.yview
        )
        self.status_box.configure(yscrollcommand=status_scroll.set)
        self.status_box.grid(row=0, column=0, sticky="nsew")
        status_scroll.grid(row=0, column=1, sticky="ns")
        self._set_status_text("(not refreshed yet)")

        log_frame = ttk.LabelFrame(self.main_pane, text="Process Log", padding=8)
        self.main_pane.add(log_frame, minsize=240, stretch="always")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame,
            wrap="none",
            state="disabled",
            height=18,
            font=("Consolas", 10),
        )
        log_y_scroll = ttk.Scrollbar(
            log_frame, orient="vertical", command=self.log_text.yview
        )
        log_x_scroll = ttk.Scrollbar(
            log_frame, orient="horizontal", command=self.log_text.xview
        )
        self.log_text.configure(
            yscrollcommand=log_y_scroll.set,
            xscrollcommand=log_x_scroll.set,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_y_scroll.grid(row=0, column=1, sticky="ns")
        log_x_scroll.grid(row=1, column=0, sticky="ew")
        self.root.after_idle(self._position_initial_sash)

    def _position_initial_sash(self):
        """Keep controls visible but reserve a useful initial log viewport."""
        try:
            total_height = self.main_pane.winfo_height()
            requested_top = self.top_panel.winfo_reqheight()
            top_height = min(max(300, requested_top), max(300, total_height - 260))
            self.main_pane.sash_place(0, 0, top_height)
        except tk.TclError:
            pass

    def _set_status_text(self, text):
        self.status_box.configure(state="normal")
        self.status_box.delete("1.0", "end")
        self.status_box.insert("1.0", text)
        self.status_box.configure(state="disabled")

    # -- worker-thread -> main-thread bridges ------------------------------
    def ask_on_main(self, fn):
        """Call a Tkinter function on the main thread from the worker thread
        and block the worker until it returns. Used for confirm/askstring
        dialogs, which Tkinter refuses to run off the main thread."""
        result_holder = {}
        done = threading.Event()

        def wrapper():
            result_holder["value"] = fn()
            done.set()

        self.ui_requests.put(wrapper)
        done.wait()
        return result_holder.get("value")

    def _poll(self):
        # ui dialog requests (confirm / ask_text) - must run here, main thread
        try:
            while True:
                wrapper = self.ui_requests.get_nowait()
                wrapper()
        except queue.Empty:
            pass

        # log lines
        drained = []
        try:
            while True:
                drained.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if drained:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "\n".join(drained) + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        # progress (single-line, in-place)
        last_progress = None
        try:
            while True:
                last_progress = self.progress_queue.get_nowait()
        except queue.Empty:
            pass
        if last_progress is not None:
            self.activity_label.configure(text=last_progress)

        # stage status updates
        try:
            while True:
                name, status = self.status_queue.get_nowait()
                self._set_stage_status(name, status)
        except queue.Empty:
            pass

        try:
            while True:
                self.last_run_success = self.result_queue.get_nowait()
        except queue.Empty:
            pass

        if self.worker is not None and not self.worker.is_alive():
            self.worker = None
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            if self.last_run_success is True:
                self.activity_label.configure(text="Deployment completed successfully.")
            elif self.last_run_success is False:
                self.activity_label.configure(text="Deployment stopped: see the failed stage and log.")
            else:
                self.activity_label.configure(text="Idle.")
            self.refresh_status()

        self.root.after(100, self._poll)

    def _set_stage_status(self, name, status):
        label = STATUS_LABELS.get(status, status)
        self.stage_tree.item(name, values=(label,), tags=(status,))
        self.stage_tree.tag_configure(status, foreground=STATUS_COLORS.get(status, "black"))
        done_count = sum(
            1 for s in self.active_stages
            if self.stage_tree.item(s, "values")[0] in (STATUS_LABELS["done"], STATUS_LABELS["skipped"])
        )
        self.progress.configure(value=done_count)
        if status == "running":
            self.activity_label.configure(text=f"Running stage: {name}")

    # -- actions ------------------------------------------------------------
    def on_start(self):
        if self.worker is not None:
            return
        stages = [name for name in self.all_stages if self.stage_vars[name].get()]
        if not stages:
            messagebox.showwarning("Nothing to do", "Select at least one stage.")
            return

        self.active_stages = stages
        self.last_run_success = None
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        for name in self.all_stages:
            self._set_stage_status(name, "pending" if name in stages else "skipped")
        self.progress.configure(maximum=len(stages), value=0)

        args = ad.SimpleNamespace(
            yes=self.auto_yes_var.get(),
            no_install=self.no_install_var.get(),
            no_mirror=self.no_mirror_var.get(),
            reconfigure=self.reconfigure_var.get(),
            restart=self.restart_var.get(),
            foreground=False,
        )

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.activity_label.configure(text="Starting...")
        self.worker = threading.Thread(target=self._run_stages_worker,
                                       args=(stages, args), daemon=True)
        self.worker.start()

    def _run_stages_worker(self, stages, args):
        self.result_queue.put(ad.run_stages(stages, args))

    def on_stop(self):
        if self.worker is None:
            return
        self.activity_label.configure(text="Stopping...")
        ad.STOP_EVENT.set()
        self.stop_btn.configure(state="disabled")

    def on_stop_services(self):
        if not messagebox.askyesno(
                "Stop all services",
                "Kill the manager-api / manager-web / xiaozhi-server processes "
                "this tool started (and offer to stop the DB/Redis containers)?"):
            return
        args = ad.SimpleNamespace(yes=False)
        threading.Thread(target=self._stop_services_worker, args=(args,), daemon=True).start()

    def _stop_services_worker(self, args):
        ad.do_stop(args)
        self.refresh_status()

    def refresh_status(self):
        def work():
            s = ad.collect_status()
            lines = [
                f"Docker daemon      : {'ready' if s['docker_ready'] else 'unavailable'}",
                f"MySQL container   : {'running' if s['containers'][ad.MYSQL_CONTAINER] else 'stopped/absent'}",
                f"Redis container   : {'running' if s['containers'][ad.REDIS_CONTAINER] else 'stopped/absent'}",
            ]
            for label, info in s["services"].items():
                lines.append(f"{label:16s}: {'UP' if info['up'] else 'down'} (port {info['port']})")
            lines.append(f"secret configured : {s['secret_set']}")
            lines.append("")
            vp = s["voiceprint"]
            lines.append("voiceprint-api    : " + (
                f"{'UP' if vp['up'] else 'down'} (port {ad.VOICEPRINT_PORT})" if vp["installed"] else "not installed"))
            mcp = s["mcp"]
            lines.append("mcp-endpoint-svr  : " + (
                f"{'UP' if mcp['up'] else 'down'} (port {ad.MCP_ENDPOINT_PORT})" if mcp["installed"] else "not installed"))
            lines.append("")
            lines.append(f"console : http://{s['ip']}:{ad.WEB_PORT}")
            lines.append(f"api     : http://{s['ip']}:{ad.API_PORT}/xiaozhi")
            lines.append(f"ws      : ws://{s['ip']}:{ad.SERVER_PORT}/xiaozhi/v1/")
            text = "\n".join(lines)
            self.root.after(0, lambda: self._set_status_text(text))

        threading.Thread(target=work, daemon=True).start()


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    DeployApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
