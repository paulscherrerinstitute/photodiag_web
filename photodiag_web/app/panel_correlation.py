import logging
from collections import deque
from datetime import datetime
from threading import Thread

import bsread
import numpy as np
from bokeh.layouts import column, gridplot, row
from bokeh.models import Button, ColumnDataSource, Select, Spacer, Spinner, TabPanel, Toggle
from bokeh.plotting import curdoc, figure

from photodiag_web import DEVICES, push_elog

log = logging.getLogger(__name__)

device_name = ""
device2_name = ""


def create():
    doc = curdoc()

    # xcorr figure
    xcorr_fig = figure(title=" ", height=500, width=500, tools="pan,wheel_zoom,save,reset")

    xcorr_even_scatter_source = ColumnDataSource(dict(x=[], y=[]))
    xcorr_fig.circle(source=xcorr_even_scatter_source, legend_label="even")

    xcorr_odd_scatter_source = ColumnDataSource(dict(x=[], y=[]))
    xcorr_fig.circle(
        source=xcorr_odd_scatter_source, line_color="red", fill_color="red", legend_label="odd"
    )

    xcorr_fig.plot.legend.click_policy = "hide"

    # ycorr figure
    ycorr_fig = figure(title=" ", height=500, width=500, tools="pan,wheel_zoom,save,reset")

    ycorr_even_scatter_source = ColumnDataSource(dict(x=[], y=[]))
    ycorr_fig.circle(source=ycorr_even_scatter_source, legend_label="even")

    ycorr_odd_scatter_source = ColumnDataSource(dict(x=[], y=[]))
    ycorr_fig.circle(
        source=ycorr_odd_scatter_source, line_color="red", fill_color="red", legend_label="odd"
    )

    ycorr_fig.plot.legend.click_policy = "hide"

    # icorr figure
    icorr_fig = figure(title=" ", height=500, width=500, tools="pan,wheel_zoom,save,reset")

    icorr_even_scatter_source = ColumnDataSource(dict(x=[], y=[]))
    icorr_fig.circle(source=icorr_even_scatter_source, legend_label="even")

    icorr_odd_scatter_source = ColumnDataSource(dict(x=[], y=[]))
    icorr_fig.circle(
        source=icorr_odd_scatter_source, line_color="red", fill_color="red", legend_label="odd"
    )

    icorr_fig.plot.legend.click_policy = "hide"

    buffer = deque()

    def _collect_data():
        nonlocal buffer
        buffer = deque(maxlen=num_shots_spinner.value)

        xpos = f"{device_name}:XPOS"
        xpos2 = f"{device2_name}:XPOS"
        ypos = f"{device_name}:YPOS"
        ypos2 = f"{device2_name}:YPOS"
        intensity = f"{device_name}:INTENSITY"
        intensity2 = f"{device2_name}:INTENSITY"

        with bsread.source(channels=[intensity, xpos, ypos, intensity2, xpos2, ypos2]) as stream:
            while update_toggle.active:
                message = stream.receive()
                is_odd = message.data.pulse_id % 2
                data = message.data.data
                buffer.append(
                    (
                        is_odd,
                        data[xpos].value,
                        data[ypos].value,
                        data[intensity].value,
                        data[xpos2].value,
                        data[ypos2].value,
                        data[intensity2].value,
                    )
                )

    async def _update_plots():
        if not buffer:
            xcorr_fig.title.text = " "
            ycorr_fig.title.text = " "
            icorr_fig.title.text = " "

            xcorr_even_scatter_source.data.update(x=[], y=[])
            ycorr_even_scatter_source.data.update(x=[], y=[])
            icorr_even_scatter_source.data.update(x=[], y=[])

            xcorr_odd_scatter_source.data.update(x=[], y=[])
            ycorr_odd_scatter_source.data.update(x=[], y=[])
            icorr_odd_scatter_source.data.update(x=[], y=[])
            return

        datetime_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title = f"{device2_name} vs {device_name}, {datetime_now}"
        xcorr_fig.title.text = title
        ycorr_fig.title.text = title
        icorr_fig.title.text = title

        data_array = np.array(buffer)
        is_even = data_array[:, 0] == 0
        data_even = data_array[is_even, :]
        data_odd = data_array[~is_even, :]

        xcorr_even_scatter_source.data.update(x=data_even[:, 1], y=data_even[:, 4])
        ycorr_even_scatter_source.data.update(x=data_even[:, 2], y=data_even[:, 5])
        icorr_even_scatter_source.data.update(x=data_even[:, 3], y=data_even[:, 6])

        xcorr_odd_scatter_source.data.update(x=data_odd[:, 1], y=data_odd[:, 4])
        ycorr_odd_scatter_source.data.update(x=data_odd[:, 2], y=data_odd[:, 5])
        icorr_odd_scatter_source.data.update(x=data_odd[:, 3], y=data_odd[:, 6])

    def device_select_callback(_attr, _old, new):
        global device_name
        device_name = new

        # reset figures
        buffer.clear()
        doc.add_next_tick_callback(_update_plots)

    device_select = Select(title="Device:", options=DEVICES)
    device_select.on_change("value", device_select_callback)
    device_select.value = DEVICES[0]

    def device2_select_callback(_attr, _old, new):
        global device2_name
        device2_name = new

        # reset figures
        buffer.clear()
        doc.add_next_tick_callback(_update_plots)

    device2_select = Select(title="Device #2:", options=DEVICES)
    device2_select.on_change("value", device2_select_callback)
    device2_select.value = DEVICES[1]

    num_shots_spinner = Spinner(title="Number shots:", mode="int", value=100, step=100, low=100)

    update_plots_periodic_callback = None

    def update_toggle_callback(_attr, _old, new):
        nonlocal update_plots_periodic_callback
        if new:
            thread = Thread(target=_collect_data)
            thread.start()

            update_plots_periodic_callback = doc.add_periodic_callback(_update_plots, 1000)

            xpos = f"{device_name}:XPOS"
            xpos2 = f"{device2_name}:XPOS"
            ypos = f"{device_name}:YPOS"
            ypos2 = f"{device2_name}:YPOS"
            intensity = f"{device_name}:INTENSITY"
            intensity2 = f"{device2_name}:INTENSITY"

            xcorr_fig.xaxis.axis_label = xpos
            xcorr_fig.yaxis.axis_label = xpos2
            ycorr_fig.xaxis.axis_label = ypos
            ycorr_fig.yaxis.axis_label = ypos2
            icorr_fig.xaxis.axis_label = intensity
            icorr_fig.yaxis.axis_label = intensity2

            device_select.disabled = True
            device2_select.disabled = True
            num_shots_spinner.disabled = True
            push_elog_button.disabled = True

            update_toggle.label = "Stop"
            update_toggle.button_type = "success"
        else:
            doc.remove_periodic_callback(update_plots_periodic_callback)

            device_select.disabled = False
            device2_select.disabled = False
            num_shots_spinner.disabled = False
            push_elog_button.disabled = False

            update_toggle.label = "Update"
            update_toggle.button_type = "primary"

    update_toggle = Toggle(label="Update", button_type="primary")
    update_toggle.on_change("active", update_toggle_callback)

    def push_elog_button_callback():
        msg_id = push_elog(
            figures=((fig_layout, "correlation.png"),),
            message="",
            attributes={
                "Author": "sf-photodiag",
                "Entry": "Info",
                "Domain": "ARAMIS",
                "System": "Diagnostics",
                "Title": f"{device2_name} vs {device_name} correlation",
            },
        )
        log.info(
            f"Logbook entry created for {device2_name} vs {device_name} correlation: "
            f"https://elog-gfa.psi.ch/SF-Photonics-Data/{msg_id}"
        )

    push_elog_button = Button(label="Push elog")
    push_elog_button.on_click(push_elog_button_callback)

    fig_layout = gridplot([[xcorr_fig, ycorr_fig, icorr_fig]], toolbar_options={"logo": None})
    tab_layout = column(
        fig_layout,
        row(
            device_select,
            device2_select,
            num_shots_spinner,
            column(Spacer(height=18), row(update_toggle, push_elog_button)),
        ),
    )

    return TabPanel(child=tab_layout, title="correlation")
