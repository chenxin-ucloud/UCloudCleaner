import sys
import os
import json
import math
import tkinter as tk
from tkinter import ttk, messagebox


def _get_assets_dir():
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, 'assets')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'assets')


def _load_regions(filename='region.json'):
    assets = _get_assets_dir()
    with open(os.path.join(assets, filename), 'r', encoding='utf-8') as f:
        return list(json.load(f).keys())


ALL_RESOURCES = [
    "UHost", "UDisk", "NATGW", "UNI", "ALB", "NLB",
    "EIP", "UGN", "UWAN", "IPSecVPN", "SecurityGroup", "ACL", "Subnet", "VPC",
]

_CANVAS_HEIGHT = 320
_APPROX_ROW_H = 22  # 每个复选框行的近似高度（像素）


class ResourceTab(ttk.Frame):
    """资源选择标签页：区域 + 资源类型复选框"""

    def __init__(self, parent, state):
        super().__init__(parent, padding=16)
        self._state = state
        self._region_vars = {}
        self._resource_vars = {}
        self._build()

    def _num_columns(self, count: int) -> int:
        """根据区域数量决定列数，0 表示区域过多须用滚动模式"""
        max_rows = _CANVAS_HEIGHT // _APPROX_ROW_H
        for cols in range(1, 5):
            if count <= cols * max_rows:
                return cols
        return 0

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)

        # ---- 区域选择 ----
        region_frame = ttk.LabelFrame(self, text="区域", padding=8)
        region_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 8))

        btn_frame = ttk.Frame(region_frame)
        btn_frame.pack(fill="x", pady=(0, 4))
        ttk.Button(btn_frame, text="全选", command=self._select_all_regions).pack(side="left", padx=2)
        ttk.Button(btn_frame, text="取消选择", command=self._deselect_all_regions).pack(side="left", padx=2)

        # 动态内容区域：多列 Grid 或 Canvas 滚动
        self._region_container = ttk.Frame(region_frame)
        self._region_container.pack(fill="both", expand=True)

        self._region_canvas = None
        self._region_inner = None
        self._current_region_file = None

        # 初始加载默认区域（正式环境）
        self.load_regions('region.json')

        # ---- 资源类型选择 ----
        res_frame = ttk.LabelFrame(self, text="资源类型", padding=8)
        res_frame.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(0, 8))

        btn_frame2 = ttk.Frame(res_frame)
        btn_frame2.pack(fill="x", pady=(0, 4))
        ttk.Button(btn_frame2, text="全选", command=self._select_all_resources).pack(side="left", padx=2)
        ttk.Button(btn_frame2, text="取消选择", command=self._deselect_all_resources).pack(side="left", padx=2)

        for res in ALL_RESOURCES:
            var = tk.BooleanVar(value=True)
            cb = ttk.Checkbutton(res_frame, text=res, variable=var)
            cb.pack(anchor="w")
            self._resource_vars[res] = var

    def _setup_scroll_area(self):
        """在 _region_container 内创建 Canvas + 滚动条（仅区域数量超出多列容纳能力时使用）"""
        canvas = tk.Canvas(self._region_container, height=_CANVAS_HEIGHT, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self._region_container, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        def _content_overflows():
            bbox = canvas.bbox("all")
            if not bbox:
                return False
            content_height = bbox[3] - bbox[1]
            visible_height = canvas.winfo_height()
            return content_height > visible_height

        def _on_inner_configure(_event):
            del _event
            canvas.configure(scrollregion=canvas.bbox("all"))
            if _content_overflows():
                scrollbar.pack(side="right", fill="y")
            else:
                scrollbar.pack_forget()
                canvas.yview_moveto(0)

        inner.bind("<Configure>", _on_inner_configure)

        def _on_mousewheel(event):
            if not _content_overflows():
                return
            canvas.yview_scroll(int(-1 * (event.delta)), "units")

        def _on_mousewheel_linux(event):
            if not _content_overflows():
                return
            if event.num == 4:
                canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                canvas.yview_scroll(1, "units")

        canvas.bind("<MouseWheel>", _on_mousewheel)
        canvas.bind("<Button-4>", _on_mousewheel_linux)
        canvas.bind("<Button-5>", _on_mousewheel_linux)
        canvas.bind("<Enter>", lambda e: canvas.focus_set())

        canvas.pack(side="left", fill="both", expand=True)

        self._region_canvas = canvas
        self._region_inner = inner

    def load_regions(self, filename='region.json'):
        """根据指定的区域配置文件重新加载区域复选框"""
        if self._current_region_file == filename:
            return

        # 清空容器内所有子控件
        for widget in self._region_container.winfo_children():
            widget.destroy()
        self._region_canvas = None
        self._region_inner = None
        self._region_vars.clear()

        try:
            regions = _load_regions(filename)
        except Exception as e:
            messagebox.showerror(
                "加载区域配置失败",
                f"无法加载 {filename} 文件:\n{e}\n\n请检查文件是否存在且格式正确。"
            )
            self._current_region_file = filename
            return

        remembered = set(getattr(self._state, "selected_regions", None) or [])
        use_remembered = bool(remembered.intersection(regions))

        num_cols = self._num_columns(len(regions))

        if num_cols > 0:
            # 多列 Grid 模式：内容放得下，无需滚动，背景自然与资源类型面板一致
            grid_frame = ttk.Frame(self._region_container)
            grid_frame.pack(fill="both", expand=True, anchor="nw")
            rows_per_col = math.ceil(len(regions) / num_cols)
            for i, region in enumerate(regions):
                default = (region in remembered) if use_remembered else True
                var = tk.BooleanVar(value=default)
                col = i // rows_per_col
                row = i % rows_per_col
                cb = ttk.Checkbutton(grid_frame, text=region, variable=var)
                cb.grid(row=row, column=col, sticky="w", padx=(0, 16))
                self._region_vars[region] = var
        else:
            # 滚动模式：区域数量超出 4 列仍放不下时才启用
            self._setup_scroll_area()
            for region in regions:
                default = (region in remembered) if use_remembered else True
                var = tk.BooleanVar(value=default)
                cb = ttk.Checkbutton(self._region_inner, text=region, variable=var)
                cb.pack(anchor="w")
                self._region_vars[region] = var

        self._current_region_file = filename
        if self._region_canvas is not None:
            self._region_canvas.yview_moveto(0)

    def load_resource_selection(self):
        """根据 state.selected_resources 恢复资源类型勾选状态"""
        remembered = set(getattr(self._state, "selected_resources", None) or [])
        if not remembered:
            return
        for res, var in self._resource_vars.items():
            var.set(res in remembered)

    def _select_all_regions(self):
        for v in self._region_vars.values():
            v.set(True)

    def _deselect_all_regions(self):
        for v in self._region_vars.values():
            v.set(False)

    def _select_all_resources(self):
        for v in self._resource_vars.values():
            v.set(True)

    def _deselect_all_resources(self):
        for v in self._resource_vars.values():
            v.set(False)

    def get_values(self):
        self._state.selected_regions = [r for r, v in self._region_vars.items() if v.get()]
        self._state.selected_resources = [r for r, v in self._resource_vars.items() if v.get()]

    def focus_region_canvas(self):
        """设置区域 Canvas 的焦点，使滚轮立即生效（仅滚动模式有效）"""
        if self._region_canvas is not None:
            self._region_canvas.focus_set()
