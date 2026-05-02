"""Restic Backup Manager TUI."""
import asyncio
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable, Footer, Header, Label, Select, Input, Button, Tree, Static, TextArea,
)

import restic_wrapper as restic

CONFIG_PATH = Path("/mnt/restic_tui/config.yaml")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ── Confirmation dialog ──────────────────────────────────────────────────────

class ConfirmScreen(ModalScreen[bool]):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self.message, id="confirm-msg")
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes", variant="error", id="yes")
                yield Button("No", variant="primary", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


# ── Output screen ────────────────────────────────────────────────────────────

class OutputScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, title: str, text: str) -> None:
        super().__init__()
        self.title_text = title
        self.output_text = text
        self.log_path = Path("/mnt/restic_tui/last_output.log")
        self.log_path.write_text(f"{title}\n{'=' * len(title)}\n{text}\n")

    def compose(self) -> ComposeResult:
        with Vertical(id="output-dialog"):
            yield Label(self.title_text, id="output-title")
            yield TextArea(self.output_text, read_only=True, id="output-body")
            yield Label(f"Saved to {self.log_path}", id="output-path")
            yield Button("Close", id="close-output")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()

    def action_dismiss(self) -> None:
        self.app.pop_screen()


# ── Restore options screen ───────────────────────────────────────────────────

class RestoreScreen(ModalScreen):
    def __init__(self, repo_path: str, password: str, snapshot_id: str, item_path: str | None) -> None:
        super().__init__()
        self.repo_path = repo_path
        self.password = password
        self.snapshot_id = snapshot_id
        self.item_path = item_path  # None = full snapshot

    def compose(self) -> ComposeResult:
        what = self.item_path or "Full snapshot"
        with Vertical(id="re-dialog"):
            yield Label(f"Restore: {what}", id="re-title")
            yield Label("Target directory:")
            yield Input(value="/mnt/restic_tui/restores", id="target")
            with Horizontal(id="re-buttons"):
                yield Button("Restore", variant="warning", id="run")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss()
            return
        target = self.query_one("#target", Input).value.strip()
        if not target:
            return
        output = restic.restore(self.repo_path, self.password, self.snapshot_id, target, self.item_path)
        self.dismiss()
        self.app.push_screen(OutputScreen("Restore Result", output))


# ── Detail browser screen ────────────────────────────────────────────────────

class DetailScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("r", "restore_item", "Restore"),
        Binding("1", "sort_by_1", "Sort:Name"),
        Binding("2", "sort_by_2", "Sort:Size"),
        Binding("3", "sort_by_3", "Sort:Modified"),
    ]

    def __init__(self, repo_path: str, password: str, snapshot_id: str) -> None:
        super().__init__()
        self.repo_path = repo_path
        self.password = password
        self.snapshot_id = snapshot_id
        self.nodes: list[restic.Node] = []
        self.sort_key = "default"

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-screen"):
            yield Label(f"Snapshot: {self.snapshot_id} | \\[r]estore  \\[1]name \\[2]size \\[3]modified  \\[esc]back", id="detail-header")
            yield Label("⏳ Loading snapshot contents...", id="detail-status")
            yield Tree("Contents", id="detail-tree")

    def on_mount(self) -> None:
        self.run_worker(self._load())

    async def _load(self) -> None:
        self.nodes = await asyncio.to_thread(restic.ls_snapshot, self.repo_path, self.password, self.snapshot_id)
        tree = self.query_one("#detail-tree", Tree)
        tree.clear()
        tree.root.expand()
        self._build_tree(tree)
        self.query_one("#detail-status", Label).update(f"✓ {len(self.nodes)} items loaded")

    def _build_tree(self, tree: Tree) -> None:
        dir_nodes: dict[str, any] = {}

        for node in self.nodes:
            # Find or create parent
            parts = [p for p in node.path.split("/") if p]
            if node.type == "dir":
                current = tree.root
                built = ""
                for part in parts:
                    built = f"/{part}" if not built else f"{built}/{part}"
                    if built not in dir_nodes:
                        tn = current.add(f"📁 {part}")
                        tn.data = built
                        dir_nodes[built] = tn
                    current = dir_nodes[built]
            else:
                # File — attach to parent dir
                parent_path = "/".join(parts[:-1])
                parent_path = f"/{parent_path}" if parent_path else "/"
                parent = dir_nodes.get(parent_path, tree.root)
                size = restic.format_size(node.size)
                mtime = node.mtime[:10] if node.mtime else ""
                label = f"📄 {node.name:<50s}  {size:>8s}  {mtime}"
                leaf = parent.add_leaf(label)
                leaf.data = node

        self._sort_tree(tree.root)

    def _sort_tree(self, node) -> None:
        if not node.children:
            return
        folders = [c for c in node.children if c.allow_expand]
        leaves = [c for c in node.children if not c.allow_expand]
        folders.sort(key=lambda n: n.label.plain.lower())

        if self.sort_key == "2":  # Size
            leaves.sort(key=lambda n: n.data.size if isinstance(n.data, restic.Node) else 0, reverse=True)
        elif self.sort_key == "3":  # Modified
            leaves.sort(key=lambda n: n.data.mtime if isinstance(n.data, restic.Node) else "", reverse=True)
        else:  # default / "1" = name
            leaves.sort(key=lambda n: n.label.plain.lower())

        node._children.clear()
        for child in folders + leaves:
            child._parent = node
            node._children.append(child)
        for child in folders:
            self._sort_tree(child)

    def _rebuild_sorted(self) -> None:
        tree = self.query_one("#detail-tree", Tree)
        tree.clear()
        tree.root.expand()
        self._build_tree(tree)
        labels = {"1": "Name", "2": "Size", "3": "Modified", "default": "Name"}
        self.query_one("#detail-status", Label).update(f"✓ {len(self.nodes)} items | sorted by {labels.get(self.sort_key, 'Name')}")

    def action_sort_by_1(self) -> None:
        self.sort_key = "1"
        self._rebuild_sorted()

    def action_sort_by_2(self) -> None:
        self.sort_key = "2"
        self._rebuild_sorted()

    def action_sort_by_3(self) -> None:
        self.sort_key = "3"
        self._rebuild_sorted()

    def _get_selected_path(self) -> str | None:
        tree = self.query_one("#detail-tree", Tree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return None
        if isinstance(node.data, restic.Node):
            return node.data.path
        elif isinstance(node.data, str):
            return node.data
        return None

    def action_restore_item(self) -> None:
        path = self._get_selected_path()
        if path:
            self.app.push_screen(RestoreScreen(self.repo_path, self.password, self.snapshot_id, path))

    def action_go_back(self) -> None:
        self.app.pop_screen()


# ── Main app ──────────────────────────────────────────────────────────────────

class ResticApp(App):
    CSS = """
    #top-bar { height: 3; padding: 0 1; }
    #top-bar Select { width: 24; }
    #top-bar Input { width: 16; }
    #top-bar Button { margin-left: 1; }
    #snapshot-table { height: 1fr; }
    #status-bar { height: 1; padding: 0 1; }
    #confirm-dialog { width: 60; height: 12; border: thick $accent; padding: 1 2; align: center middle; }
    #confirm-buttons { height: 3; align: center middle; }
    #output-dialog { width: 80%; height: 80%; border: thick $accent; padding: 1 2; }
    #output-body { height: 1fr; }
    #re-dialog { width: 70; height: 14; border: thick $accent; padding: 1 2; align: center middle; }
    #re-buttons { height: 3; align: center middle; }
    #detail-screen { width: 100%; height: 100%; }
    #detail-tree { height: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("l", "load_snapshots", "Load"),
        Binding("m", "toggle_mark", "Mark/Unmark"),
        Binding("d", "delete_marked", "Delete Marked"),
        Binding("p", "prune_repo", "Prune"),
        Binding("b", "browse_snapshot", "Browse"),
        Binding("a", "mark_all_visible", "Mark All"),
        Binding("u", "unmark_all", "Unmark All"),
        Binding("s", "toggle_sort", "Sort ↑↓"),
        Binding("i", "show_stats", "Stats"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_config()
        self.machines = restic.load_machines(self.cfg.get("clients_file", ""))
        self.snapshots: list[restic.Snapshot] = []
        self.marked: set[str] = set()
        self.sort_newest_first = True
        self.current_machine: restic.Machine | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="top-bar"):
            yield Select(
                [(m.name, m.name) for m in self.machines],
                prompt="Machine", id="machine-select",
            )
            yield Input(placeholder="From YYYY-MM-DD", id="date-from")
            yield Input(placeholder="To YYYY-MM-DD", id="date-to")
            yield Button("Load", id="load-btn")
        yield DataTable(id="snapshot-table")
        yield Label("Ready. Select a machine and press Load.", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#snapshot-table", DataTable)
        table.cursor_type = "row"
        table.add_column("✓", key="mark", width=3)
        table.add_column("ID", key="id")
        table.add_column("Time", key="time")
        table.add_column("Hostname", key="hostname")
        table.add_column("Paths", key="paths")
        now = datetime.now(timezone.utc)
        self.query_one("#date-from", Input).value = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        self.query_one("#date-to", Input).value = now.strftime("%Y-%m-%d")

    def _status(self, msg: str) -> None:
        self.query_one("#status-bar", Label).update(msg)

    def _get_machine(self) -> restic.Machine | None:
        sel = self.query_one("#machine-select", Select).value
        if sel is Select.BLANK:
            return None
        return next((m for m in self.machines if m.name == sel), None)

    def action_load_snapshots(self) -> None:
        self._do_load()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "load-btn":
            self._do_load()

    def _do_load(self) -> None:
        machine = self._get_machine()
        if not machine:
            self._status("Select a machine first.")
            return

        date_from = self.query_one("#date-from", Input).value.strip()
        date_to = self.query_one("#date-to", Input).value.strip()
        for label, val in [("From", date_from), ("To", date_to)]:
            if val:
                try:
                    datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    self._status(f"Invalid {label} date: {val} (use YYYY-MM-DD)")
                    return

        self.current_machine = machine
        self._status(f"⏳ Loading snapshots for {machine.name}...")
        self.marked.clear()
        table = self.query_one("#snapshot-table", DataTable)
        table.clear()
        self.run_worker(self._load(machine, date_from, date_to))

    async def _load(self, machine: restic.Machine, date_from: str, date_to: str) -> None:
        password = self.cfg.get("restic_password", "")
        try:
            all_snaps = await asyncio.to_thread(restic.list_snapshots, machine.repo_path, password)
        except Exception as e:
            self._status(f"Error: {e}")
            return

        self._status(f"⏳ Filtering {len(all_snaps)} snapshots...")

        try:
            d_from = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc) if date_from else None
        except ValueError:
            d_from = None
        try:
            d_to = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=timezone.utc, hour=23, minute=59, second=59) if date_to else None
        except ValueError:
            d_to = None

        filtered = []
        for s in all_snaps:
            if d_from and s.time < d_from:
                continue
            if d_to and s.time > d_to:
                continue
            filtered.append(s)

        self.snapshots = sorted(filtered, key=lambda s: s.time, reverse=self.sort_newest_first)
        self._populate_table()
        self._status(f"Loaded {len(self.snapshots)} snapshots (filtered from {len(all_snaps)} total).")

    def _populate_table(self) -> None:
        table = self.query_one("#snapshot-table", DataTable)
        table.clear(columns=True)
        table.add_column("✓", key="mark", width=3)
        table.add_column("ID", key="id")
        table.add_column("Time", key="time")
        table.add_column("Hostname", key="hostname")
        table.add_column("Paths", key="paths")
        for s in self.snapshots:
            mark = "✓" if s.short_id in self.marked else ""
            table.add_row(
                mark, s.short_id,
                s.time.strftime("%Y-%m-%d %H:%M"),
                s.hostname,
                ", ".join(s.paths),
                key=s.short_id,
            )

    def action_toggle_mark(self) -> None:
        table = self.query_one("#snapshot-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        sid = row_key.value
        if sid in self.marked:
            self.marked.discard(sid)
            table.update_cell(row_key, "mark", "")
        else:
            self.marked.add(sid)
            table.update_cell(row_key, "mark", "✓")
        if table.cursor_coordinate.row < table.row_count - 1:
            table.move_cursor(row=table.cursor_coordinate.row + 1)
        self._status(f"{len(self.marked)} snapshots marked.")

    def action_mark_all_visible(self) -> None:
        table = self.query_one("#snapshot-table", DataTable)
        for s in self.snapshots:
            self.marked.add(s.short_id)
        for row_key in table.rows:
            table.update_cell(row_key, "mark", "✓")
        self._status(f"{len(self.marked)} snapshots marked.")

    def action_unmark_all(self) -> None:
        table = self.query_one("#snapshot-table", DataTable)
        self.marked.clear()
        for row_key in table.rows:
            table.update_cell(row_key, "mark", "")
        self._status("All marks cleared.")

    def action_toggle_sort(self) -> None:
        self.sort_newest_first = not self.sort_newest_first
        if self.snapshots:
            self.snapshots.reverse()
            self._populate_table()
        self._status(f"Sort: {'newest first' if self.sort_newest_first else 'oldest first'}")

    def action_delete_marked(self) -> None:
        if not self.marked:
            self._status("No snapshots marked.")
            return
        if not self.current_machine:
            return
        count = len(self.marked)
        self.push_screen(
            ConfirmScreen(f"Forget {count} snapshot(s)?\n\nThis removes the snapshot metadata.\nRun Prune after to reclaim space."),
            callback=self._on_delete_confirm,
        )

    def _on_delete_confirm(self, confirmed: bool) -> None:
        if not confirmed:
            self._status("Cancelled.")
            return
        self.run_worker(self._do_delete())

    async def _do_delete(self) -> None:
        password = self.cfg.get("restic_password", "")
        ids = list(self.marked)
        self._status(f"⏳ Forgetting {len(ids)} snapshots...")
        output = await asyncio.to_thread(restic.forget_snapshots, self.current_machine.repo_path, password, ids)
        self.marked.clear()
        self._status(f"Forgot {len(ids)} snapshots. Reload to refresh. Run Prune to reclaim space.")
        self.push_screen(OutputScreen("Forget Results", output))

    def action_prune_repo(self) -> None:
        if not self.current_machine:
            self._status("Select a machine first.")
            return
        self.push_screen(
            ConfirmScreen(f"Prune {self.current_machine.name} repo?\n\nThis removes unreferenced data and may take a while."),
            callback=self._on_prune_confirm,
        )

    def _on_prune_confirm(self, confirmed: bool) -> None:
        if not confirmed:
            return
        self.run_worker(self._do_prune())

    async def _do_prune(self) -> None:
        password = self.cfg.get("restic_password", "")
        self._status(f"⏳ Pruning {self.current_machine.name}... this may take a while")
        output = await asyncio.to_thread(restic.prune, self.current_machine.repo_path, password)
        self._status("Prune completed.")
        self.push_screen(OutputScreen("Prune Results", output))

    def action_show_stats(self) -> None:
        if not self.current_machine:
            self._status("Select a machine first.")
            return
        self.run_worker(self._do_stats())

    async def _do_stats(self) -> None:
        password = self.cfg.get("restic_password", "")
        self._status(f"⏳ Getting stats for {self.current_machine.name}...")
        st = await asyncio.to_thread(restic.stats, self.current_machine.repo_path, password)
        if st:
            size = restic.format_size(st.get("total_size", 0))
            files = st.get("total_file_count", 0)
            snaps = st.get("snapshots_count", 0)
            text = f"Repository: {self.current_machine.name}\nPath: {self.current_machine.repo_path}\n\nTotal size: {size}\nTotal files: {files}\nSnapshots: {snaps}"
        else:
            text = "Failed to get stats."
        self._status("Stats loaded.")
        self.push_screen(OutputScreen("Repository Stats", text))

    def action_browse_snapshot(self) -> None:
        table = self.query_one("#snapshot-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        sid = row_key.value
        if not self.current_machine:
            return
        password = self.cfg.get("restic_password", "")
        self.push_screen(DetailScreen(self.current_machine.repo_path, password, sid))


def main():
    app = ResticApp()
    app.run()


if __name__ == "__main__":
    main()
