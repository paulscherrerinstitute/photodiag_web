import asyncio
from collections import deque
from datetime import datetime
from functools import partial
from threading import Event, Thread

import epics
import numpy as np
from bokeh.layouts import column, row
from bokeh.models import (
    Button,
    ColumnDataSource,
    NumericInput,
    Select,
    Spacer,
    Spinner,
    TabPanel,
    TextInput,
    Toggle,
)
from bokeh.plotting import curdoc, figure
from lmfit.models import GaussianModel

from photodiag_web import SPECT_DEV_CONFIG, epics_collect_data, get_device_domain, push_elog

model = GaussianModel(prefix="g0_") + GaussianModel(prefix="g1_") + GaussianModel(prefix="g2_")


FWHM_TO_SIGMA = 1 / (2 * np.sqrt(2 * np.log(2)))  # ~= 1 / 2.355


def create(title):
    doc = curdoc()
    log = doc.logger

    def motor_scan(pv_name, scan_range, channels, numShots, stop_event):
        motor = epics.Motor(pv_name)
        motor_init = motor.get_position()

        scan_mean = []

        for pos in scan_range:
            val = motor.move(pos, wait=True)
            if val != 0:
                if val == -12:
                    raise ValueError(f"Motor position outside soft limits: {motor.LLM} {motor.HLM}")
                raise ValueError(f"Error moving the motor {pv_name}, error value {val}")

            data = epics_collect_data(channels, numShots)

            autocorr = []
            for wf in data[0]:
                autocorr.append(np.correlate(wf, wf, mode="same"))
            autocorr_mean = np.mean(autocorr, axis=0)
            autocorr_mean_norm = autocorr_mean / np.max(autocorr_mean)
            scan_mean.append(autocorr_mean_norm)

            doc.add_next_tick_callback(partial(_update_calib_plot, pos, autocorr_mean_norm))

            if stop_event.is_set():
                break

        motor.move(motor_init, wait=True)

        return np.asarray(scan_mean)

    def pv_scan(pv_name, scan_range, channels, numShots, stop_event):
        pv = epics.PV(pv_name)
        pv_init = pv.value
        scan_mean = []

        for pos in scan_range:
            pv.put(pos, wait=True)

            data = epics_collect_data(channels, numShots)

            autocorr = []
            for wf in data[0]:
                autocorr.append(np.correlate(wf, wf, mode="same"))
            autocorr_mean = np.mean(autocorr, axis=0)
            autocorr_mean_norm = autocorr_mean / np.max(autocorr_mean)
            scan_mean.append(autocorr_mean_norm)

            doc.add_next_tick_callback(partial(_update_calib_plot, pos, autocorr_mean_norm))

            if stop_event.is_set():
                break

        pv.put(pv_init, wait=True)

        return np.asarray(scan_mean)

    fit_result = None
    params = model.make_params(
        g0_sigma=dict(value=12, min=0.05),
        g0_center=dict(value=0, vary=False),
        g0_amplitude=dict(value=1, min=0),
        g1_sigma=dict(value=6, min=0.05),
        g1_center=dict(value=0, vary=False),
        g1_amplitude=dict(value=1, min=0),
        g2_sigma=dict(value=1.4 * FWHM_TO_SIGMA, min=0.05),
        g2_center=dict(value=0, vary=False),
        g2_amplitude=dict(value=1, min=0),
    )

    async def _update_fit_params():
        device = device_select.value
        if not device:
            return

        vals = []
        # TODO: remove after channel names are fixed for all devices
        tmp = epics.caget(f"{device}:FIT-FWHM")
        chan = f"{device}:SPECTRUM_FWHM" if tmp is None else f"{device}:FIT-FWHM"
        for _ in range(20):
            vals.append(epics.caget(chan))
            await asyncio.sleep(0.1)
        params["g1_sigma"].value = np.mean(vals) * 1.4 * FWHM_TO_SIGMA

    doc.add_periodic_callback(_update_fit_params, 600_000)

    config = SPECT_DEV_CONFIG
    devices = list(config.keys())

    pvs_x = {}
    pvs_y = {}
    pvs_m = {}
    for device in devices:
        pvs_x[device] = epics.PV(f"{device}:SPECTRUM_X")
        pvs_y[device] = epics.PV(f"{device}:SPECTRUM_Y")
        pvs_m[device] = epics.PV(config[device]["motor"])
    doc.pvs.extend([*pvs_x.values(), *pvs_y.values(), *pvs_m.values()])

    # single shot spectrum figure
    autocorr_fig = figure(
        height=250, width=1000, x_axis_label="Lags [eV]", tools="pan,wheel_zoom,save,reset"
    )

    autocorr_lines_source = ColumnDataSource(
        dict(x=[], y_autocorr=[], y_fit=[], y_bkg=[], y_env=[], y_spike=[])
    )
    autocorr_fig.line(source=autocorr_lines_source, y="y_autocorr", legend_label="Autocorrelation")
    autocorr_fig.line(
        source=autocorr_lines_source, y="y_fit", line_color="orange", legend_label="Fit"
    )
    autocorr_fig.line(
        source=autocorr_lines_source, y="y_bkg", line_color="green", legend_label="Background"
    )
    autocorr_fig.line(
        source=autocorr_lines_source, y="y_env", line_color="red", legend_label="Spectral envelope"
    )
    autocorr_fig.line(
        source=autocorr_lines_source,
        y="y_spike",
        line_color="purple",
        legend_label="Spectral spike",
    )

    autocorr_fig.toolbar.logo = None
    autocorr_fig.legend.click_policy = "hide"
    autocorr_fig.y_range.only_visible = True

    # fwhm over time figure
    fwhm_fig = figure(
        height=250,
        width=1000,
        x_axis_label="Wall time",
        x_axis_type="datetime",
        y_axis_label="FWHM [eV]",
        tools="pan,wheel_zoom,save,reset",
    )

    fwhm_lines_source = ColumnDataSource(dict(x=[], fwhm_bkg=[], fwhm_env=[], fwhm_spike=[]))
    fwhm_fig.line(
        source=fwhm_lines_source, y="fwhm_bkg", line_color="green", legend_label="Background"
    )
    fwhm_fig.line(
        source=fwhm_lines_source, y="fwhm_env", line_color="red", legend_label="Spectral envelope"
    )
    fwhm_fig.line(
        source=fwhm_lines_source, y="fwhm_spike", line_color="purple", legend_label="Spectral spike"
    )

    fwhm_fig.toolbar.logo = None
    fwhm_fig.legend.click_policy = "hide"
    fwhm_fig.y_range.only_visible = True

    # calibration figure
    calib_fig = figure(
        height=500,
        width=500,
        x_axis_label="Position",
        y_axis_label="Spectral spike FWHM [eV]",
        tools="pan,wheel_zoom,save,reset",
    )

    calib_line_source = ColumnDataSource(dict(x=[], y=[]))
    calib_fig.line(source=calib_line_source)

    calib_fig.toolbar.logo = None

    lags = []
    buffer_autocorr = deque()

    def update_x(value, **_):
        nonlocal lags
        lags = value - value[int(value.size / 2)]
        params["g0_sigma"].value = (value[-1] - value[0]) * 0.4 * 1.4 * FWHM_TO_SIGMA

        buffer_autocorr.clear()

    def update_y(value, **_):
        buffer_autocorr.append(np.correlate(value, value, mode="same"))

    num_shots_spinner = Spinner(title="Number shots:", mode="int", value=100, low=1, width=100)
    from_spinner = Spinner(title="From:", width=100)
    to_spinner = Spinner(title="To:", width=100)
    step_spinner = Spinner(title="Step:", width=100)
    motor_textinput = TextInput(title="Motor:", disabled=True, width=300)
    readback_numinput = NumericInput(title="Motor readback:", mode="float", disabled=True)

    def move_button_callback():
        if pos_spinner.value is not None:
            try:
                pvs_m[device_select.value].put(pos_spinner.value, wait=False)
            except Exception as e:
                log.error(e)

    pos_spinner = Spinner(title="New position:", value=0, width=100)
    move_button = Button(label="Move")
    move_button.on_click(move_button_callback)

    async def _update_pos(value):
        readback_numinput.value = value

    def _motor_callback(value, **_):
        doc.add_next_tick_callback(partial(_update_pos, value))

    update_plots_periodic_callback = None

    def update_toggle_callback(_attr, _old, new):
        nonlocal update_plots_periodic_callback, lags, buffer_autocorr
        pv_x = pvs_x[device_select.value]
        pv_y = pvs_y[device_select.value]
        if new:
            value = pv_x.value
            lags = value - value[int(value.size / 2)]
            params["g0_sigma"].value = (value[-1] - value[0]) * 0.4 * 1.4 * FWHM_TO_SIGMA
            buffer_autocorr = deque(maxlen=num_shots_spinner.value)

            pv_x.add_callback(update_x)
            pv_y.add_callback(update_y)

            update_plots_periodic_callback = doc.add_periodic_callback(_update_plots, 3000)
            doc.add_next_tick_callback(_live_lock_gui)

            update_toggle.label = "Stop"
            update_toggle.button_type = "success"
        else:
            pv_x.clear_callbacks()
            pv_y.clear_callbacks()

            doc.remove_periodic_callback(update_plots_periodic_callback)
            doc.add_next_tick_callback(_live_unlock_gui)

            update_toggle.label = "Update"
            update_toggle.button_type = "primary"

    update_toggle = Toggle(label="Update", button_type="primary")
    update_toggle.on_change("active", update_toggle_callback)

    is_running = {"calib": False, "live": False}

    async def _calib_lock_gui():
        is_running["calib"] = True
        device_select.disabled = True
        num_shots_spinner.disabled = True
        from_spinner.disabled = True
        to_spinner.disabled = True
        step_spinner.disabled = True
        pos_spinner.disabled = True
        move_button.disabled = True
        push_calib_elog_button.disabled = True

    async def _calib_unlock_gui():
        is_running["calib"] = False
        if not any(is_running.values()):
            device_select.disabled = False
            num_shots_spinner.disabled = False
        from_spinner.disabled = False
        to_spinner.disabled = False
        step_spinner.disabled = False
        pos_spinner.disabled = False
        move_button.disabled = False
        calibrate_button.disabled = False
        calibrate_button.label = "Calibrate"
        calibrate_button.button_type = "primary"
        push_calib_elog_button.disabled = False

    async def _live_lock_gui():
        is_running["live"] = True
        device_select.disabled = True
        num_shots_spinner.disabled = True
        push_fit_elog_button.disabled = True

    async def _live_unlock_gui():
        is_running["live"] = False
        if not any(is_running.values()):
            device_select.disabled = False
            num_shots_spinner.disabled = False
        push_fit_elog_button.disabled = False

    async def _update_calib_plot(x, wf):
        pv_x = pvs_x[device_select.value]
        value = pv_x.value
        lags = value - value[int(value.size / 2)]

        fit_result = model.fit(wf, params, x=lags)
        spike_fwhm = min(
            fit_result.values["g0_fwhm"], fit_result.values["g1_fwhm"], fit_result.values["g2_fwhm"]
        )

        calib_line_source.stream(dict(x=[x], y=[spike_fwhm / 1.4]))

    async def _reset_calib_plot():
        calib_line_source.data.update(x=[], y=[])

    def _calibrate(calib_stop_event):
        device_name = device_select.value
        numShots = num_shots_spinner.value

        pv_name = motor_textinput.value
        scan_range = np.arange(from_spinner.value, to_spinner.value, step_spinner.value)
        channels = [f"{device_name}:SPECTRUM_Y"]

        doc.add_next_tick_callback(_reset_calib_plot)

        # TODO: find a simpler way to scan PVs and Motors
        if device_name == "SARFE10-PSSS059":
            scan_func = motor_scan
        else:
            scan_func = pv_scan

        try:
            wf_mean = scan_func(pv_name, scan_range, channels, numShots, calib_stop_event)
        except ValueError as e:
            log.error(e)
        else:
            log.info(f"{device_name} calibrated")
        finally:
            doc.add_next_tick_callback(_calib_unlock_gui)

    calib_stop_event = Event()

    def calibrate_button_callback(_attr, _old, new):
        if new:
            doc.add_next_tick_callback(_calib_lock_gui)

            calib_stop_event.clear()
            thread = Thread(target=_calibrate, args=(calib_stop_event,))
            thread.start()

            calibrate_button.label = "Stop"
            calibrate_button.button_type = "danger"
        else:
            calib_stop_event.set()

            calibrate_button.disabled = True
            calibrate_button.label = "Stopping"
            calibrate_button.button_type = "warning"

    calibrate_button = Toggle(label="Calibrate", button_type="primary")
    calibrate_button.on_change("active", calibrate_button_callback)

    async def _update_plots():
        nonlocal fit_result
        if len(buffer_autocorr) < 4:
            autocorr_lines_source.data.update(
                x=[], y_autocorr=[], y_fit=[], y_bkg=[], y_env=[], y_spike=[]
            )
            fwhm_lines_source.data.update(x=[], fwhm_bkg=[], fwhm_env=[], fwhm_spike=[])
            return

        autocorr = np.array(buffer_autocorr)
        y_autocorr = autocorr.mean(axis=0)
        y_autocorr /= np.max(y_autocorr)

        fit_result = model.fit(y_autocorr, params, x=lags)
        y_fit = fit_result.best_fit

        # Sort the fwhm values (sometimes they are swapped despite initial guesses)
        fwhm = [
            fit_result.values["g0_fwhm"],
            fit_result.values["g1_fwhm"],
            fit_result.values["g2_fwhm"],
        ]
        spike_ind, env_idx, bkg_idx = np.argsort(fwhm)

        components = fit_result.eval_components(x=lags)

        y_bkg = components[f"g{bkg_idx}_"]
        y_env = components[f"g{env_idx}_"]
        y_spike = components[f"g{spike_ind}_"]

        # Convert fwhm of autocorrelation to fwhm of corresponding gaussian
        fwhm_bkg = fwhm[bkg_idx] / 1.4
        fwhm_env = fwhm[env_idx] / 1.4
        fwhm_spike = fwhm[spike_ind] / 1.4

        # update glyph sources
        autocorr_lines_source.data.update(
            x=lags, y_autocorr=y_autocorr, y_fit=y_fit, y_bkg=y_bkg, y_env=y_env, y_spike=y_spike
        )
        fwhm_lines_source.stream(
            dict(
                x=[datetime.now()],
                fwhm_bkg=[fwhm_bkg],
                fwhm_env=[fwhm_env],
                fwhm_spike=[fwhm_spike],
            ),
            rollover=3600,
        )

    def device_select_callback(_attr, _old, new):
        nonlocal lags
        # reset figures
        lags = []
        buffer_autocorr.clear()
        doc.add_next_tick_callback(_update_plots)
        doc.add_next_tick_callback(_reset_calib_plot)

        # update default widget values
        dev_conf = config[new]
        from_spinner.value = dev_conf["from"]
        to_spinner.value = dev_conf["to"]
        step_spinner.value = dev_conf["step"]
        motor_textinput.value = dev_conf["motor"]

        # connect pos_spinner widget to the PV
        for pv in pvs_m.values():
            pv.clear_callbacks()

        pvs_m[new].add_callback(_motor_callback)
        pvs_m[new].run_callbacks()

    device_select = Select(title="Device:", options=devices)
    device_select.on_change("value", device_select_callback)
    device_select.value = devices[0]

    def push_fit_elog_button_callback():
        device_name = device_select.value
        domain = get_device_domain(device_name)

        msg_id = push_elog(
            figures=((autocorr_layout, "fit.png"),),
            message=fit_result.fit_report(),
            attributes={
                "Author": "sf-photodiag",
                "Entry": "Info",
                "Domain": domain,
                "System": "Diagnostics",
                "Title": f"{device_name} Autocorrelation fit results",
            },
        )
        log.info(
            f"Logbook entry created for {device_name}: "
            f"https://elog-gfa.psi.ch/SF-Photonics-Data/{msg_id}"
        )

    push_fit_elog_button = Button(label="Push fit elog")
    push_fit_elog_button.on_click(push_fit_elog_button_callback)

    def push_calib_elog_button_callback():
        device_name = device_select.value
        domain = get_device_domain(device_name)

        msg_id = push_elog(
            figures=((calib_layout, "calibration.png"),),
            message="",
            attributes={
                "Author": "sf-photodiag",
                "Entry": "Configuration",
                "Domain": domain,
                "System": "Diagnostics",
                "Title": f"{device_name} resolution",
            },
        )
        log.info(
            f"Logbook entry created for {device_name} callibration: "
            f"https://elog-gfa.psi.ch/SF-Photonics-Data/{msg_id}"
        )

    push_calib_elog_button = Button(label="Push calib elog")
    push_calib_elog_button.on_click(push_calib_elog_button_callback)

    autocorr_layout = column(autocorr_fig, fwhm_fig)
    calib_layout = calib_fig
    fig_layout = row(autocorr_layout, calib_layout)
    tab_layout = column(
        fig_layout,
        row(
            device_select,
            motor_textinput,
            readback_numinput,
            pos_spinner,
            column(Spacer(height=18), move_button),
        ),
        row(
            num_shots_spinner,
            Spacer(width=30),
            column(Spacer(height=18), update_toggle),
            column(Spacer(height=18), push_fit_elog_button),
            Spacer(width=30),
            from_spinner,
            to_spinner,
            step_spinner,
            column(Spacer(height=18), calibrate_button),
            column(Spacer(height=18), push_calib_elog_button),
        ),
    )

    return TabPanel(child=tab_layout, title=title)
