#!/usr/bin/env python3
"""Cross-platform GUI for finding recent EverQuest TLP auction sellers."""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    VERTICAL,
    W,
    X,
    Y,
    filedialog,
    messagebox,
    ttk,
)
import tkinter as tk


API_BASE_URL = "https://www.tlp-auctions.com"
DEFAULT_SERVER = "Frostreaver"
APP_DIR = Path.home() / ".eq_auction_scanner"
WATCHLIST_PATH = APP_DIR / "watchlist.txt"
CATALOG_CACHE_PATH = APP_DIR / "catalog_frostreaver.json"
REQUEST_TIMEOUT_SECONDS = 30
API_MIN_REQUEST_INTERVAL_SECONDS = 0.75
API_MAX_RETRIES = 3
API_BACKOFF_SECONDS = 2
MAX_BULK_ITEMS = 200
RECENT_SALES_PER_ITEM = 20


@dataclass(frozen=True)
class CatalogItem:
    item_id: int
    name: str
    median_price: int | float | None


@dataclass(frozen=True)
class ResolvedItem:
    requested_name: str
    item: CatalogItem | None
    status: str


@dataclass(frozen=True)
class ResultRow:
    item_name: str
    seller: str
    price: str
    plat_price: float
    krono_price: float
    age: str
    sold_at: str
    sold_at_datetime: datetime
    inquiry: str


class ApiError(RuntimeError):
    """Raised when the TLP Auctions API cannot be reached or parsed."""


class TlpAuctionsClient:
    def __init__(self, base_url: str = API_BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self._last_request_at = 0.0
        self._request_lock = threading.Lock()

    def get_catalog(self, server_name: str) -> list[CatalogItem]:
        query = urllib.parse.urlencode({"serverName": server_name})
        payload = self._request_json(f"/api/items/catalog?{query}")
        items = payload.get("items", [])
        return [
            CatalogItem(
                item_id=int(item["itemId"]),
                name=str(item["name"]),
                median_price=item.get("price"),
            )
            for item in items
            if "itemId" in item and "name" in item
        ]

    def get_recent_sales(
        self, server_name: str, item_ids: list[int]
    ) -> dict[int, list[dict[str, object]]]:
        sales_by_item: dict[int, list[dict[str, object]]] = {}
        for start in range(0, len(item_ids), MAX_BULK_ITEMS):
            chunk = item_ids[start : start + MAX_BULK_ITEMS]
            body = {
                "serverName": server_name,
                "itemIds": chunk,
                "perItemLimit": RECENT_SALES_PER_ITEM,
            }
            payload = self._request_json("/api/sales/bulk", body)
            for item in payload.get("items", []):
                item_id = item.get("itemId")
                if item_id is None:
                    continue
                sales_by_item[int(item_id)] = list(item.get("sales", []))
        return sales_by_item

    def _request_json(
        self, path: str, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json", "User-Agent": "EQAuctionScanner/1.0"}
        method = "GET"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            method = "POST"

        for attempt in range(API_MAX_RETRIES + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            self._wait_for_rate_limit()
            try:
                with urllib.request.urlopen(
                    request, timeout=REQUEST_TIMEOUT_SECONDS
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and attempt < API_MAX_RETRIES:
                    time.sleep(retry_delay(exc, attempt))
                    continue
                raise ApiError(f"API request failed with HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise ApiError(f"API request failed: {exc}") from exc
            except json.JSONDecodeError as exc:
                raise ApiError("API returned invalid JSON") from exc
        raise ApiError("API request failed after retrying rate-limited responses.")

    def _wait_for_rate_limit(self) -> None:
        with self._request_lock:
            elapsed = time.monotonic() - self._last_request_at
            wait_seconds = API_MIN_REQUEST_INTERVAL_SECONDS - elapsed
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self._last_request_at = time.monotonic()


class ItemResolver:
    def __init__(self, catalog: list[CatalogItem]) -> None:
        self.catalog = catalog
        self.by_normalized_name = {self._normalize(item.name): item for item in catalog}

    def resolve_many(self, names: list[str]) -> list[ResolvedItem]:
        return [self.resolve(name) for name in names]

    def resolve(self, requested_name: str) -> ResolvedItem:
        normalized = self._normalize(requested_name)
        exact = self.by_normalized_name.get(normalized)
        if exact is not None:
            return ResolvedItem(requested_name, exact, "exact")

        contains_matches = [
            item for item in self.catalog if normalized in self._normalize(item.name)
        ]
        if len(contains_matches) == 1:
            return ResolvedItem(requested_name, contains_matches[0], "partial")
        if len(contains_matches) > 1:
            return ResolvedItem(
                requested_name,
                None,
                f"ambiguous ({len(contains_matches)} catalog matches)",
            )
        return ResolvedItem(requested_name, None, "not found")

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.casefold().strip().split())


class AuctionScannerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("EverQuest Frostreaver Auction Scanner")
        self.geometry("1120x720")
        self.minsize(900, 560)

        self.client = TlpAuctionsClient()
        self.catalog: list[CatalogItem] = []
        self.result_rows: list[ResultRow] = []
        self.row_data: dict[str, ResultRow] = {}
        self.sort_column = "sold_at"
        self.sort_descending = True
        self.worker_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.is_busy = False

        self._build_widgets()
        self._load_saved_watchlist()
        self.after(100, self._process_worker_queue)

    def _build_widgets(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill=BOTH, expand=True)

        controls = ttk.Frame(root)
        controls.pack(fill=X)

        ttk.Label(controls, text=f"Server: {DEFAULT_SERVER}").pack(side=LEFT)
        self.load_catalog_button = ttk.Button(
            controls, text="Load Catalog", command=self.load_catalog
        )
        self.load_catalog_button.pack(side=LEFT, padx=(12, 0))
        self.search_button = ttk.Button(
            controls, text="Search Watchlist", command=self.search_watchlist
        )
        self.search_button.pack(side=LEFT, padx=6)
        ttk.Button(controls, text="Import Watchlist", command=self.import_watchlist).pack(
            side=LEFT, padx=6
        )
        ttk.Button(controls, text="Export Watchlist", command=self.export_watchlist).pack(
            side=LEFT, padx=6
        )
        ttk.Button(controls, text="Save Watchlist", command=self.save_watchlist).pack(
            side=LEFT, padx=6
        )
        ttk.Button(
            controls,
            text="Copy Inquiry To Clipboard",
            command=self.copy_selected_inquiry,
        ).pack(side=RIGHT)

        ttk.Label(
            root,
            text="Wanted items, one item name per line. Exact names are best; unique partial names also work.",
        ).pack(anchor=W, pady=(12, 4))

        main = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        main.pack(fill=BOTH, expand=True)

        watchlist_frame = ttk.Frame(main)
        self.watchlist_text = tk.Text(watchlist_frame, width=34, wrap="word", undo=True)
        watchlist_scroll = ttk.Scrollbar(
            watchlist_frame, orient=VERTICAL, command=self.watchlist_text.yview
        )
        self.watchlist_text.configure(yscrollcommand=watchlist_scroll.set)
        self.watchlist_text.pack(side=LEFT, fill=BOTH, expand=True)
        watchlist_scroll.pack(side=RIGHT, fill=Y)
        main.add(watchlist_frame, weight=1)

        results_frame = ttk.Frame(main)
        style = ttk.Style(self)
        style.configure("Results.Treeview", rowheight=38)
        columns = ("item", "seller", "price", "age", "sold_at")
        self.results = ttk.Treeview(
            results_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
            style="Results.Treeview",
        )
        headings = {
            "item": "Item",
            "seller": "Seller",
            "price": "Price",
            "age": "How Long Ago",
            "sold_at": "Timestamp",
        }
        for column, title in headings.items():
            self.results.heading(
                column,
                text=title,
                command=lambda selected_column=column: self.sort_results(selected_column),
            )
        self.results.column("item", width=230, anchor=W)
        self.results.column("seller", width=140, anchor=W)
        self.results.column("price", width=120, anchor=W)
        self.results.column("age", width=120, anchor=W)
        self.results.column("sold_at", width=170, anchor=W)
        results_scroll = ttk.Scrollbar(
            results_frame, orient=VERTICAL, command=self.results.yview
        )
        self.results.configure(yscrollcommand=results_scroll.set)
        self.results.pack(side=LEFT, fill=BOTH, expand=True)
        results_scroll.pack(side=RIGHT, fill=Y)
        self.results.bind("<Double-1>", lambda _event: self.copy_selected_inquiry())
        main.add(results_frame, weight=3)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(root, textvariable=self.status_var).pack(fill=X, pady=(8, 0))

    def load_catalog(self) -> None:
        if self.is_busy:
            return
        self._run_worker("Loading catalog...", self._load_catalog_worker)

    def search_watchlist(self) -> None:
        if self.is_busy:
            return
        names = self._watchlist_items()
        if not names:
            messagebox.showinfo("Watchlist Empty", "Add one item name per line first.")
            return
        self.save_watchlist(show_message=False)
        self._run_worker("Searching recent seller listings...", self._search_worker, names)

    def import_watchlist(self) -> None:
        path = filedialog.askopenfilename(
            title="Import Watchlist",
            filetypes=(("Text files", "*.txt"), ("All files", "*.*")),
        )
        if not path:
            return
        text = Path(path).read_text(encoding="utf-8")
        self.watchlist_text.delete("1.0", END)
        self.watchlist_text.insert("1.0", text)
        self.save_watchlist(show_message=False)
        self.status_var.set(f"Imported watchlist from {path}.")

    def export_watchlist(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Export Watchlist",
            defaultextension=".txt",
            filetypes=(("Text files", "*.txt"), ("All files", "*.*")),
        )
        if not path:
            return
        Path(path).write_text(self.watchlist_text.get("1.0", END).strip() + "\n", encoding="utf-8")
        self.status_var.set(f"Exported watchlist to {path}.")

    def save_watchlist(self, show_message: bool = True) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        WATCHLIST_PATH.write_text(
            self.watchlist_text.get("1.0", END).strip() + "\n", encoding="utf-8"
        )
        if show_message:
            self.status_var.set(f"Saved watchlist to {WATCHLIST_PATH}.")

    def copy_selected_inquiry(self) -> None:
        selected = self.results.selection()
        if not selected:
            messagebox.showinfo("No Selection", "Select a seller row first.")
            return
        row = self.row_data.get(selected[0])
        if row is None:
            return
        self.clipboard_clear()
        self.clipboard_append(row.inquiry)
        self.update()
        self.status_var.set(f"Copied inquiry for {row.item_name} from {row.seller}.")

    def _load_saved_watchlist(self) -> None:
        if WATCHLIST_PATH.exists():
            self.watchlist_text.insert("1.0", WATCHLIST_PATH.read_text(encoding="utf-8"))

    def _watchlist_items(self) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for raw_line in self.watchlist_text.get("1.0", END).splitlines():
            name = raw_line.strip()
            key = name.casefold()
            if not name or key in seen:
                continue
            names.append(name)
            seen.add(key)
        return names

    def _run_worker(self, status: str, target: object, *args: object) -> None:
        self.is_busy = True
        self._set_buttons_enabled(False)
        self.status_var.set(status)
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()

    def _load_catalog_worker(self) -> None:
        try:
            catalog = self._load_catalog()
            self.worker_queue.put(("catalog_loaded", catalog))
        except Exception as exc:
            self.worker_queue.put(("error", exc))

    def _search_worker(self, names: list[str]) -> None:
        try:
            catalog = self.catalog or self._load_catalog()
            resolver = ItemResolver(catalog)
            resolved = resolver.resolve_many(names)
            item_ids = [item.item.item_id for item in resolved if item.item is not None]
            sales_by_item = self.client.get_recent_sales(DEFAULT_SERVER, item_ids) if item_ids else {}
            rows = self._build_result_rows(resolved, sales_by_item)
            self.worker_queue.put(("search_complete", (catalog, resolved, rows)))
        except Exception as exc:
            self.worker_queue.put(("error", exc))

    def _load_catalog(self) -> list[CatalogItem]:
        try:
            catalog = self.client.get_catalog(DEFAULT_SERVER)
            self._save_catalog_cache(catalog)
            return catalog
        except ApiError:
            cached = self._load_catalog_cache()
            if cached:
                return cached
            raise

    def _save_catalog_cache(self, catalog: list[CatalogItem]) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        payload = [
            {"itemId": item.item_id, "name": item.name, "price": item.median_price}
            for item in catalog
        ]
        CATALOG_CACHE_PATH.write_text(json.dumps(payload), encoding="utf-8")

    def _load_catalog_cache(self) -> list[CatalogItem]:
        if not CATALOG_CACHE_PATH.exists():
            return []
        payload = json.loads(CATALOG_CACHE_PATH.read_text(encoding="utf-8"))
        return [
            CatalogItem(int(item["itemId"]), str(item["name"]), item.get("price"))
            for item in payload
        ]

    def _build_result_rows(
        self,
        resolved_items: list[ResolvedItem],
        sales_by_item: dict[int, list[dict[str, object]]],
    ) -> list[ResultRow]:
        rows: list[ResultRow] = []
        now = datetime.now(UTC)
        for resolved in resolved_items:
            if resolved.item is None:
                continue
            for sale in sales_by_item.get(resolved.item.item_id, []):
                if sale.get("transactionType") is not False:
                    continue
                seller = str(sale.get("auctioneer") or "")
                item_name = str(sale.get("item") or resolved.item.name)
                sold_at = parse_api_datetime(str(sale.get("datetime") or ""))
                plat_price = numeric_price(sale.get("platPrice"))
                krono_price = numeric_price(sale.get("kronoPrice"))
                rows.append(
                    ResultRow(
                        item_name=item_name,
                        seller=seller,
                        price=format_price(plat_price, krono_price),
                        plat_price=plat_price,
                        krono_price=krono_price,
                        age=format_age(sold_at, now),
                        sold_at=sold_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                        sold_at_datetime=sold_at,
                        inquiry=build_inquiry(seller, item_name),
                    )
                )
        return rows

    def _process_worker_queue(self) -> None:
        try:
            while True:
                event, payload = self.worker_queue.get_nowait()
                if event == "catalog_loaded":
                    self.catalog = payload  # type: ignore[assignment]
                    self.status_var.set(f"Loaded {len(self.catalog):,} catalog items.")
                    self._finish_busy()
                elif event == "search_complete":
                    catalog, resolved, rows = payload  # type: ignore[misc]
                    self.catalog = catalog
                    self._show_results(resolved, rows)
                    self._finish_busy()
                elif event == "error":
                    self._finish_busy()
                    messagebox.showerror("Auction Scanner Error", str(payload))
                    self.status_var.set("Error. See message for details.")
        except queue.Empty:
            pass
        self.after(100, self._process_worker_queue)

    def _show_results(
        self, resolved_items: list[ResolvedItem], rows: list[ResultRow]
    ) -> None:
        self.result_rows = rows
        self.sort_column = "sold_at"
        self.sort_descending = True
        self._render_results()

        unresolved = [item for item in resolved_items if item.item is None]
        partials = [item for item in resolved_items if item.status == "partial"]
        status = f"Found {len(rows):,} seller listing rows."
        if partials:
            status += f" Used {len(partials)} unique partial match(es)."
        if unresolved:
            status += " Unresolved: " + ", ".join(
                f"{item.requested_name} ({item.status})" for item in unresolved[:5]
            )
            if len(unresolved) > 5:
                status += f", and {len(unresolved) - 5} more"
        self.status_var.set(status)

    def sort_results(self, column: str) -> None:
        if column == self.sort_column:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_column = column
            self.sort_descending = column in {"age", "sold_at"}
        self._render_results()

    def _render_results(self) -> None:
        self.results.delete(*self.results.get_children())
        self.row_data.clear()
        rows = sorted(
            self.result_rows,
            key=lambda row: result_sort_key(row, self.sort_column),
            reverse=self.sort_descending,
        )
        for row in rows:
            row_id = self.results.insert(
                "",
                END,
                values=(row.item_name, row.seller, row.price, row.age, row.sold_at),
            )
            self.row_data[row_id] = row

    def _finish_busy(self) -> None:
        self.is_busy = False
        self._set_buttons_enabled(True)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.load_catalog_button.configure(state=state)
        self.search_button.configure(state=state)


def parse_api_datetime(value: str) -> datetime:
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def format_age(sold_at: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    seconds = max(0, int((now - sold_at).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h {minutes % 60}m ago"
    days = hours // 24
    return f"{days}d {hours % 24}h ago"


def format_price(plat_price: object, krono_price: object) -> str:
    plat = numeric_price(plat_price)
    krono = numeric_price(krono_price)
    parts: list[str] = []
    if plat:
        parts.append(f"{plat:g} pp")
    if krono:
        parts.append(f"{krono:g} krono")
    return " + ".join(parts) if parts else "unknown"


def numeric_price(value: object) -> float:
    return float(value or 0)


def retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = exc.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after).astimezone(UTC)
                return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError):
                pass
    return API_BACKOFF_SECONDS * (2**attempt)


def result_sort_key(row: ResultRow, column: str) -> object:
    if column == "item":
        return row.item_name.casefold()
    if column == "seller":
        return row.seller.casefold()
    if column == "price":
        return (row.krono_price, row.plat_price)
    if column == "age":
        return row.sold_at_datetime
    if column == "sold_at":
        return row.sold_at_datetime
    return row.sold_at_datetime


def build_inquiry(seller: str, item_name: str) -> str:
    return f"/tell {seller} Hi, is your {item_name} still available? What price are you asking?"


def main() -> None:
    app = AuctionScannerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
