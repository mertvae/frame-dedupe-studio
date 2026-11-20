"""Desktop interface for the video frame deduplication tool."""
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import subprocess
import tempfile
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from types import SimpleNamespace

import adaptive_timing as timing
from dedupe_video_frames import EXTENSIONS, find_duplicates, inspect, process


class App:
    def __init__(self, root):
        self.root = root
        root.title("Подготовка видеоряда")
        root.geometry("990x570")
        root.minsize(900, 460)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.preview_dir = tempfile.TemporaryDirectory(prefix="frame-review-")
        self.players = {}
        self.files = {}
        self.results = {}
        self.plans = {}
        self.pinned = {}
        self.events = Queue()
        self.busy = False
        self.controls = []
        self.mode = tk.StringVar(value="visual")
        self.drop_audio = tk.BooleanVar(value=False)
        self.skip_existing = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Добавьте файлы или выберите папку с видео.")

        panel = ttk.Frame(root, padding=14)
        panel.pack(fill="both", expand=True)
        source_bar = ttk.Frame(panel)
        source_bar.pack(fill="x")
        self.button(source_bar, "Добавить файлы…", self.add_files)
        self.button(source_bar, "Добавить папку…", self.add_folder)
        self.button(source_bar, "Убрать выбранные", self.remove)

        settings = ttk.Frame(panel)
        settings.pack(fill="x", pady=(9, 5))
        for label, value in (
            ("Похожие подряд", "visual"), ("Похожие: сильнее", "strong"),
            ("Точные подряд", "adjacent"), ("Все точные", "all"),
            ("Обычные двойки", "twos"), ("Адаптивные двойки", "adaptive"),
        ):
            field = ttk.Radiobutton(settings, text=label, variable=self.mode, value=value,
                                    command=self.refresh)
            field.pack(side="left", padx=(0, 10))
            self.controls.append(field)
        export_settings = ttk.Frame(panel)
        export_settings.pack(fill="x", pady=(0, 8))
        for label, variable in (
            ("Убрать звук из сокращённых видео", self.drop_audio),
            ("Пропускать уже сохранённые файлы", self.skip_existing),
        ):
            field = ttk.Checkbutton(export_settings, text=label, variable=variable)
            field.pack(side="left", padx=(0, 12))
            self.controls.append(field)

        table = ttk.Frame(panel)
        table.pack(fill="both", expand=True)
        columns = ("name", "before", "after", "poses", "time", "geometry")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="extended")
        for name, label, width in zip(columns, ("Видео", "Кадры до", "Кадры после", "Экспозиции",
                                               "Длительность", "Сетка H3"),
                                      (320, 88, 95, 96, 125, 90)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, stretch=name == "name",
                             anchor="w" if name == "name" else "center")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _event: self.play_source())

        preview_bar = ttk.Frame(panel)
        preview_bar.pack(fill="x", pady=(12, 4))
        self.button(preview_bar, "Сканировать батч", self.scan)
        self.button(preview_bar, "▶ Исходник", self.play_source)
        self.button(preview_bar, "▶ Результат", self.play_result)
        self.button(preview_bar, "▶ Обычные двойки", lambda: self.play_result("twos"))

        timing_bar = ttk.Frame(panel)
        timing_bar.pack(fill="x", pady=(0, 4))
        self.button(timing_bar, "Закрепить кадры…", self.edit_pins)
        self.button(timing_bar, "Карта экспозиций", self.show_timing)

        save_bar = ttk.Frame(panel)
        save_bar.pack(fill="x", pady=(0, 8))
        self.button(save_bar, "Сохранить выбранные…", lambda: self.save(False))
        self.button(save_bar, "Сохранить весь батч…", lambda: self.save(True))

        ttk.Label(panel, textvariable=self.status, wraplength=750).pack(anchor="w")
        root.after(75, self.poll)

    def button(self, frame, label, action):
        button = ttk.Button(frame, text=label, command=action)
        button.pack(side="left", padx=(0, 8))
        self.controls.append(button)

    def add_files(self):
        paths = filedialog.askopenfilenames(filetypes=[("Видео", "*.mp4 *.mov *.mkv")])
        self.add(Path(path) for path in paths)

    def add_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.add(sorted(Path(folder).iterdir()))

    def add(self, paths):
        existing = set(self.files.values())
        for path in paths:
            path = path.resolve()
            if path.is_file() and path.suffix.lower() in EXTENSIONS and path not in existing:
                row = self.tree.insert("", "end", values=(path.name, "—", "—", "—", "—", "—"))
                self.files[row] = path
                existing.add(path)
        self.refresh()
        self.status.set(f"Загружено видео: {len(self.files)}.")

    def remove(self):
        for row in self.tree.selection():
            path = self.files.pop(row)
            self.pinned.pop(path, None)
            for mode in (*timing.MODES, "visual", "strong", "adjacent", "all"):
                self.results.pop((path, mode), None)
                self.plans.pop((path, mode), None)
            self.tree.delete(row)
        self.status.set(f"Загружено видео: {len(self.files)}.")

    def refresh(self):
        mode = self.mode.get()
        self.tree.heading("before", text="До (24 fps)" if mode in timing.MODES else "Кадры до")
        for row, path in self.files.items():
            stats = self.results.get((path, mode))
            if stats:
                before, after, fps, exposures = stats
                geometry = "OK" if fps == 24 and after >= 5 and after % 17 == 5 else "Проверить"
                values = path.name, before, after, exposures, f"{after / float(fps):.2f} с", geometry
            else:
                values = path.name, "—", "—", "—", "—", "—"
            self.tree.item(row, values=values)

    def selected(self, one=False):
        rows = self.tree.selection()
        if not rows or (one and len(rows) != 1):
            messagebox.showinfo("Выбор видео", "Выберите одну строку." if one else
                                "Выберите строки для сохранения.")
            return []
        return [self.files[row] for row in rows]

    def edit_pins(self):
        paths = self.selected(one=True)
        if not paths:
            return
        path = paths[0]
        plan = self.plans.get((path, "adaptive"))
        limit = f"–{plan.frame_count}" if plan else ""
        initial = ", ".join(str(index + 1) for index in sorted(self.pinned.get(path, ())))
        answer = simpledialog.askstring(
            "Закрепить кадры",
            f"{path.name}\nВведите номера кадров на шкале 24 fps (1{limit}). "
            "Например, 4, 8, 15. Пустое поле снимает все отметки.",
            initialvalue=initial, parent=self.root,
        )
        if answer is None:
            return
        try:
            pins = {int(value) - 1 for value in re.split(r"[,;\s]+", answer.strip()) if value}
            if any(index < 0 or (plan and index >= plan.frame_count) for index in pins):
                raise ValueError
        except ValueError:
            messagebox.showerror("Номера кадров", f"Введите целые номера кадров от 1{limit}.")
            return
        self.pinned[path] = frozenset(pins)
        self.plans.pop((path, "adaptive"), None)
        self.results.pop((path, "adaptive"), None)
        self.mode.set("adaptive")
        self.refresh()
        self.status.set(f"{path.name} · закреплено кадров: {len(pins)}.")

    def show_timing(self):
        paths = self.selected(one=True)
        if not paths:
            return
        mode = "twos" if self.mode.get() == "twos" else "adaptive"
        path = paths[0]
        plan = self.plans.get((path, mode))
        if plan is not None:
            self.timing_window(path, mode, plan)
            return
        pinned = self.pinned.get(path, ())

        def get_map(source, _index):
            plan = self.analyze_timing(source, mode, pinned)
            self.events.put(("timing_window", (source, mode, plan)))

        self.start(paths, get_map, "Анализ экспозиций")

    def timing_window(self, path, mode, plan):
        window = tk.Toplevel(self.root)
        label = "Обычные двойки" if mode == "twos" else "Адаптивные двойки"
        window.title(f"{label} · {path.name}")
        window.geometry("680x460")
        counts = plan.exposure_counts()
        summary = " · ".join(f"{length} кадр. × {counts[length]}" for length in sorted(counts))
        ttk.Label(window, padding=10, text=f"24 fps · {plan.frame_count} кадров · {summary}").pack(anchor="w")
        grid = ttk.Frame(window, padding=(10, 0, 10, 10))
        grid.pack(fill="both", expand=True)
        columns = ("first", "last", "time", "length", "reason")
        table = ttk.Treeview(grid, columns=columns, show="headings")
        for name, label, width in zip(columns, ("Кадр с", "По", "Время, с", "Кадров", "Отметки"),
                                      (75, 55, 85, 65, 280)):
            table.heading(name, text=label)
            table.column(name, width=width, stretch=name == "reason")
        scroll = ttk.Scrollbar(grid, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        table.pack(side="left", fill="both", expand=True)
        for first, duration in plan.exposures():
            reasons = [reason for frames, reason in (
                (plan.cuts, "склейка"), (plan.pinned, "закреплён"),
                (plan.accents, "акцент движения"), (plan.pans, "панорама"),
            ) if first in frames]
            table.insert("", "end", values=(first + 1, first + duration,
                         f"{first / timing.FPS:.3f}", duration, ", ".join(reasons)))

    def analyze_timing(self, path, mode, pinned):
        plan = timing.analyze(path, mode, pinned if mode == "adaptive" else ())
        self.events.put(("plan", (path, mode, plan)))
        return plan

    def scan(self):
        mode = self.mode.get()
        paths = list(self.files.values())
        pins = self.pinned.copy()

        def check(path, _index):
            if mode in timing.MODES:
                self.analyze_timing(path, mode, pins.get(path, ()))
                return
            fps, _audio = inspect(path)
            total, duplicates = find_duplicates(path, mode)
            if not total:
                raise ValueError("Нет декодированных кадров.")
            self.events.put(("stats", (path, mode, total, total - len(duplicates), fps, "—")))

        self.start(paths, check, "Сканирование")

    def play(self, path, side):
        player = self.players.get(side)
        if player and player.poll() is None:
            player.terminate()
        try:
            slot = {"Исходник": 0, "Результат": 1, "Обычные двойки": 2}[side]
            width = min(620, max(280, (self.root.winfo_screenwidth() - 60) // 3))
            offset = 15 + slot * (width + 12)
            self.players[side] = subprocess.Popen(
                ["ffplay", "-autoexit", "-loglevel", "error", "-window_title",
                 f"{side}: {path.name}", "-x", str(width), "-left", str(offset), "-top", "80",
                 "-i", str(path)],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            messagebox.showerror("Воспроизведение", str(exc))

    def play_source(self):
        paths = self.selected(one=True)
        if paths:
            self.play(paths[0], "Исходник")

    def options(self):
        return SimpleNamespace(mode=self.mode.get(), dry_run=False,
                               drop_audio=self.drop_audio.get())

    def export_video(self, path, output, args, pins):
        if args.mode in timing.MODES:
            plan = self.analyze_timing(path, args.mode, pins.get(path, ()))
            timing.render(path, output, plan)
        else:
            process(path, output, args)

    def play_result(self, mode=None):
        paths = self.selected(one=True)
        if not paths:
            return
        args = self.options()
        if mode:
            args.mode = mode
        pins = self.pinned.copy()
        stage = Path(tempfile.mkdtemp(dir=self.preview_dir.name))

        def make_preview(path, _index):
            output = stage / path.name
            self.export_video(path, output, args, pins)
            side = "Обычные двойки" if args.mode == "twos" else "Результат"
            self.events.put(("preview", (output, side)))

        self.start(paths, make_preview, "Подготовка предпросмотра")

    def save(self, all_files):
        paths = list(self.files.values()) if all_files else self.selected()
        if not paths:
            return
        output = filedialog.askdirectory(title="Папка для сохранения видео")
        if not output:
            return
        args = self.options()
        destination = Path(output)
        names = set()
        targets = {}
        for path in paths:
            index = 2
            name = path.name
            while name.casefold() in names:
                name = f"{path.stem}_{index}{path.suffix}"
                index += 1
            targets[path] = destination / name
            names.add(name.casefold())
        pending = [path for path in paths if not
                   (self.skip_existing.get() and targets[path].is_file())]
        skipped = len(paths) - len(pending)
        if not pending:
            self.status.set(f"Уже сохранено в выбранной папке: {skipped}.")
            return
        pins = self.pinned.copy()
        self.start(pending, lambda path, _: self.export_video(path, targets[path], args, pins),
                   "Сохранение", skipped=skipped)

    def start(self, paths, action, label, skipped=0):
        if self.busy:
            return
        if not paths:
            self.status.set("Добавьте видео для обработки.")
            return
        self.busy = True
        for control in self.controls:
            control.configure(state="disabled")

        def worker():
            errors = []
            for index, path in enumerate(paths, 1):
                self.events.put(("status", f"{label}  {index}/{len(paths)}  {path.name}"))
                try:
                    action(path, index)
                except Exception as exc:
                    errors.append(f"{path.name}: {exc}")
            self.events.put(("done", (label, len(paths), errors, skipped)))

        Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "status":
                    self.status.set(value)
                elif event == "stats":
                    path, mode, *stats = value
                    self.results[(path, mode)] = stats
                    self.refresh()
                elif event == "plan":
                    path, mode, plan = value
                    self.plans[(path, mode)] = plan
                    self.results[(path, mode)] = (plan.frame_count, plan.frame_count,
                                                   timing.FPS, len(plan.starts))
                    self.refresh()
                elif event == "timing_window":
                    self.timing_window(*value)
                elif event == "preview":
                    self.play(*value)
                elif event == "done":
                    label, total, errors, skipped = value
                    self.busy = False
                    for control in self.controls:
                        control.configure(state="normal")
                    summary = f"{label} завершено · успешно {total - len(errors)}/{total}"
                    if skipped:
                        summary += f" · уже были в папке: {skipped}"
                    self.status.set(summary)
                    if errors:
                        window = tk.Toplevel(self.root)
                        window.title("Ошибки обработки")
                        report = tk.Text(window, wrap="word", width=86, height=14)
                        report.pack(fill="both", expand=True)
                        report.insert("1.0", "\n\n".join(errors))
                        report.configure(state="disabled")
        except Empty:
            pass
        self.root.after(75, self.poll)

    def close(self):
        if self.busy:
            messagebox.showinfo("Обработка", "Дождитесь завершения текущей операции.")
            return
        for player in self.players.values():
            if player.poll() is None:
                player.terminate()
                player.wait(timeout=5)
        self.preview_dir.cleanup()
        self.root.destroy()


def main():
    root = tk.Tk()
    missing = [name for name in ("ffmpeg", "ffprobe", "ffplay") if not shutil.which(name)]
    if missing:
        root.withdraw()
        messagebox.showerror("Зависимости", "Не найдены в PATH: " + ", ".join(missing))
        root.destroy()
        return
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

