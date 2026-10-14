"""Connect content resource monitor -- interactive Shiny-for-Python dashboard.

A thin reactive view over collector.py. Deploy as Connect content; it shows the true
real-time CPU/memory of every running content item (renders included), measured from
/proc subtrees and mapped to content via GET /__api__/metrics/procs, alongside Connect's
own (undercounted) numbers. Every poll is appended to a CSV (see collector.RECORD_PATH).

Configuration (set as Connect content environment variables):
    CONNECT_SERVER, CONNECT_API_KEY (admin), MONITOR_POLL_SECONDS, MONITOR_RECORD_PATH,
    CONNECT_VERIFY.  See collector.py for details.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from shiny import App, reactive, render, ui  # noqa: E402

import collector  # noqa: E402

POLL_SECONDS = float(os.environ.get("MONITOR_POLL_SECONDS", "5"))

app_ui = ui.page_fluid(
    ui.tags.style(".small-note{color:#666;font-size:0.85em}"),
    ui.h2("Connect content resource monitor"),
    ui.p(
        "Real CPU/memory of every running content item, measured from /proc subtrees and "
        "mapped to content via the Connect API. ",
        ui.tags.span(
            f"Host: {collector.NCPU} cores. Polling every {POLL_SECONDS:g}s. "
            f"Recording to {collector.RECORD_PATH}.",
            class_="small-note",
        ),
    ),
    ui.output_ui("status"),
    ui.layout_columns(
        ui.value_box("Content running", ui.output_text("n_content")),
        ui.value_box("Total CPU (cores)", ui.output_text("total_cpu")),
        ui.value_box("Total RSS", ui.output_text("total_rss")),
        col_widths=[4, 4, 4],
    ),
    ui.card(
        ui.card_header("Per-content usage — measured (subtree) vs. Connect-reported"),
        ui.output_data_frame("table"),
    ),
    ui.card(
        ui.card_header("Total CPU cores in use over time"),
        ui.output_plot("history_plot", height="260px"),
    ),
    ui.download_button("download", "Download recorded CSV"),
)


def server(input, output, session):
    @reactive.calc
    def snapshot():
        reactive.invalidate_later(POLL_SECONDS)
        return collector.compute_snapshot()

    @render.ui
    def status():
        _, err = snapshot()
        if err:
            return ui.div(ui.strong("⚠ "), err, class_="text-danger")
        return ui.div(f"Last update: {dt.datetime.now().strftime('%H:%M:%S')}", class_="small-note")

    @render.text
    def n_content():
        df, _ = snapshot()
        return "0" if df.empty else str(df["content_guid"].nunique())

    @render.text
    def total_cpu():
        df, _ = snapshot()
        return "0.00" if df.empty else f"{df['cpu_cores'].sum():.2f}"

    @render.text
    def total_rss():
        df, _ = snapshot()
        return "0 MB" if df.empty else f"{df['rss_mb'].sum() / 1024:.2f} GB"

    @render.data_frame
    def table():
        df, _ = snapshot()
        if df.empty:
            return pd.DataFrame({"info": ["No running content processes reported."]})
        show = df[[
            "content_name", "type", "job_key", "n_procs", "cpu_cores", "cpu_pct_host",
            "rss_mb", "connect_cpu_current", "connect_ram_mb",
        ]].rename(columns={
            "cpu_cores": "cpu_cores(meas)", "rss_mb": "rss_mb(meas)",
            "connect_cpu_current": "cpu(connect)", "connect_ram_mb": "ram_mb(connect)",
        })
        return render.DataGrid(show, width="100%")

    @render.plot
    def history_plot():
        snapshot()  # dependency so the plot refreshes each poll
        if not collector.history:
            fig, ax = plt.subplots()
            ax.text(0.5, 0.5, "collecting…", ha="center", va="center")
            ax.axis("off")
            return fig
        ts = [h[0] for h in collector.history]
        cpu = [h[1] for h in collector.history]
        fig, ax = plt.subplots()
        ax.plot(ts, cpu, marker=".", linewidth=1)
        ax.axhline(collector.NCPU, color="r", linestyle="--", linewidth=0.8,
                   label=f"{collector.NCPU} cores (host max)")
        ax.set_ylabel("CPU cores in use")
        ax.set_ylim(bottom=0)
        ax.legend(loc="upper left", fontsize=8)
        fig.autofmt_xdate()
        return fig

    @render.download(filename="connect_content_metrics.csv")
    def download():
        if Path(collector.RECORD_PATH).exists():
            with open(collector.RECORD_PATH, "rb") as fh:
                yield fh.read()
        else:
            yield b"(no data recorded yet)\n"


app = App(app_ui, server)
