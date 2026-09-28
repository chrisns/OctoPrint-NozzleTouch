# coding=utf-8
"""Nozzle-touch bed levelling and T1 height for the Snapmaker 2.0 dual extruder.

This is the only file that imports OctoPrint. Every other module in the package is plain
Python, so the tests run the whole calibration against a simulated printer.

One button runs the sequence in routine.py: heat, retract, wait while the user brushes
the nozzles, cool, touch the plate with the cold nozzle at every mesh node, write the mesh,
then touch a few points with both nozzles and set the T1 height.
"""
from __future__ import absolute_import

import collections
import json
import os
import threading
import time

import flask
import octoprint.plugin
from flask_babel import gettext
from octoprint.access import ADMIN_GROUP, USER_GROUP
from octoprint.access.permissions import Permissions
from octoprint.events import Events

from .gcode import GcodeBridge
from .routine import Calibration
from .settings import DEFAULTS, SettingsError, routine_config

__plugin_name__ = "Nozzle Touch"
__plugin_pythoncompat__ = ">=3.7,<4"

LAST_FILE = "last_result.json"
HISTORY_FILE = "history.jsonl"
LOG_LINES = 600


class NozzleTouchPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.EventHandlerPlugin,
):

    def __init__(self):
        self._bridge = None
        self._routine = None
        self._lock = threading.RLock()
        self._log = collections.deque(maxlen=LOG_LINES)
        self._progress = None
        self._last = None
        self._last_error = None
        self._held = False

    # -- permissions ------------------------------------------------------

    def get_additional_permissions(self):
        """Two permissions, split by what the action can do to the machine.

        Reading the last result is harmless. A run drives a cold nozzle onto the plate and
        writes the mesh and the T1 offset to the firmware, so it is kept to administrators.
        """
        return [
            {
                "key": "VIEW",
                "name": "View the nozzle touch calibration",
                "description": gettext("Allows reading the calibration progress and the last result."),
                "default_groups": [USER_GROUP, ADMIN_GROUP],
                "roles": ["view"],
            },
            {
                "key": "CALIBRATE",
                "name": "Run the nozzle touch calibration",
                "description": gettext(
                    "Allows touching the plate with the nozzles, and writing the bed mesh "
                    "and the T1 offset to the printer firmware."
                ),
                "default_groups": [ADMIN_GROUP],
                "roles": ["calibrate"],
            },
        ]

    # Which permission each API command needs. A command missing from here is
    # refused, so a new command cannot be added without deciding who may run it.
    # Anyone who can watch a run can stop it.
    COMMAND_PERMISSIONS = {
        "start": "CALIBRATE",
        "confirm_wipe": "CALIBRATE",
        "abort": "VIEW",
    }

    @staticmethod
    def _allowed(key):
        return getattr(Permissions, "PLUGIN_NOZZLETOUCH_" + key).can()

    # -- settings ---------------------------------------------------------

    def get_settings_defaults(self):
        return dict(DEFAULTS)

    def get_settings_version(self):
        return 1

    def on_settings_save(self, data):
        """Check the settings before they are saved, not after.

        Every value ends up in a G-code command, some of them with soft endstops off.
        """
        merged = self._config()
        merged.update(dict((k, v) for k, v in (data or {}).items() if k in DEFAULTS))
        try:
            routine_config(merged)
        except SettingsError as exception:
            self._logger.warning("refused a settings save: %s", exception)
            raise ValueError(str(exception))
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        self._apply_split_setting()

    def _config(self):
        return dict((key, self._settings.get([key])) for key in DEFAULTS)

    def _apply_split_setting(self):
        if self._bridge is not None:
            self._bridge.fix_split_ok = bool(self._settings.get_boolean(["fix_split_ok"])) or self._running()

    # -- templates and assets ---------------------------------------------

    def is_template_autoescaped(self):
        return True

    def get_template_configs(self):
        return [
            dict(type="tab", name="Nozzle Touch", template="nozzletouch_tab.jinja2",
                 custom_bindings=True, icon="fas fa-ruler-vertical"),
            # False on purpose. The settings pane binds straight through OctoPrint's
            # own settings view model, so `settings.plugins.nozzletouch.<key>`
            # resolves. With custom bindings on, nothing owns the pane and every
            # binding fails silently, which leaves the page blank.
            dict(type="settings", name="Nozzle Touch",
                 template="nozzletouch_settings.jinja2", custom_bindings=False),
        ]

    def get_assets(self):
        return dict(js=["js/nozzletouch.js"], css=["css/nozzletouch.css"])

    # -- lifecycle --------------------------------------------------------

    def on_after_startup(self):
        self._bridge = GcodeBridge(self._printer, self._logger)
        self._apply_split_setting()
        self._last = self._read_json(LAST_FILE)
        self._logger.info("nozzle touch calibration ready")

    def on_shutdown(self):
        self._stop_routine("OctoPrint is shutting down")

    # -- events -----------------------------------------------------------

    def on_event(self, event, payload):
        if event in (Events.DISCONNECTED, Events.ERROR):
            self._stop_routine("the printer disconnected")
        elif event == Events.PRINT_STARTED and self._running():
            # The job is on hold, so no line of it has been sent. Stop the calibration,
            # and let the print go once the nozzle is up again.
            self._stop_routine("a print was started")

    # -- G-code hook ------------------------------------------------------

    def gcode_received(self, comm_instance, line, *args, **kwargs):
        if self._bridge is None:
            return line
        return self._bridge.on_gcode_received(comm_instance, line, *args, **kwargs)

    # -- the API ----------------------------------------------------------

    def is_api_protected(self):
        return True

    def get_api_commands(self):
        return dict(start=[], confirm_wipe=[], abort=[])

    def on_api_command(self, command, data):
        needed = self.COMMAND_PERMISSIONS.get(command)
        if needed is None:
            return flask.abort(400, "unknown command")
        if not self._allowed(needed):
            return flask.abort(403, "you do not have permission to do that")
        try:
            handler = getattr(self, "_api_" + command)
        except AttributeError:
            return flask.abort(400, "unknown command")
        if self._bridge is None:
            return flask.make_response(flask.jsonify(error="still starting up"), 503)
        try:
            result = handler(data)
        except ValueError as exception:
            return flask.make_response(flask.jsonify(error=str(exception)), 400)
        except Exception as exception:                       # noqa: BLE001
            self._logger.exception("api command %s failed", command)
            return flask.make_response(flask.jsonify(error=str(exception)), 500)
        return flask.jsonify(result or dict(ok=True))

    def on_api_get(self, request):
        if not self._allowed("VIEW"):
            return flask.abort(403)
        return flask.jsonify(self._status())

    def _status(self):
        routine = self._routine
        running = self._running()
        try:
            routine_config(self._config())
            settings_error = None
        except SettingsError as exception:
            settings_error = str(exception)
        return dict(
            running=running,
            phase=routine.phase if running else None,
            waiting_for_wipe=bool(running and routine.waiting_for_wipe),
            progress=self._progress if running else None,
            log=list(self._log),
            last=self._last,
            error=self._last_error,
            settings_error=settings_error,
        )

    def _api_start(self, data):
        with self._lock:
            if self._running():
                raise ValueError("a calibration is already running")
            if not self._printer.is_operational():
                raise ValueError("the printer is not connected")
            if self._printer.is_printing() or self._printer.is_paused():
                raise ValueError("the printer is busy with a print")
            try:
                config = routine_config(self._config())
            except SettingsError as exception:
                raise ValueError("the settings are not safe to run: %s" % exception)
            # The user says the nozzles are already clean: no heat, retract or brush step.
            config["skip_wipe"] = bool((data or {}).get("skip_wipe"))
            self._log.clear()
            self._progress = None
            self._last_error = None
            self._on_message(dict(type="started", message="Started."))
            self._hold(True)
            self._bridge.fix_split_ok = True
            try:
                self._routine = _Routine(self, self._bridge, self._temperatures, self._on_message,
                                         config, logger=self._logger)
                self._routine.start()
            except Exception:
                self._finished()
                raise
        return dict(started=True)

    def _api_confirm_wipe(self, data):
        routine = self._routine
        if not self._running() or not routine.waiting_for_wipe:
            raise ValueError("the calibration is not waiting for the nozzles to be brushed")
        routine.confirm_wipe()
        self._on_message(dict(type="progress", phase="cool", message="Brushing confirmed."))
        return dict(ok=True)

    def _api_abort(self, data):
        self._stop_routine("stopped by the user")
        return dict(ok=True)

    # -- helpers ----------------------------------------------------------

    def _running(self):
        return self._routine is not None and self._routine.is_alive()

    def _stop_routine(self, reason):
        """Stop the routine as fast as possible.

        The abort flag alone is not enough: the routine may be waiting on the printer, so
        the bridge is woken too. The routine then raises the nozzle and turns on the soft
        endstops.
        """
        with self._lock:
            if self._running():
                self._logger.info("stopping the calibration: %s", reason)
                self._routine.abort()
                if self._bridge is not None:
                    self._bridge.interrupt()

    def _temperatures(self):
        try:
            current = self._printer.get_current_temperatures() or {}
        except Exception:                                    # noqa: BLE001
            return {}
        return dict((name, (current.get(name) or {}).get("actual"))
                    for name in ("bed", "tool0", "tool1"))

    def _hold(self, value):
        """Hold any print job while the calibration runs, and release it after.

        OctoPrint lets a print start while the printer is idle, and the calibration looks
        idle to it. With the job on hold, not one line of a print reaches the printer
        until the nozzle is up again.
        """
        if value == self._held:
            return
        try:
            self._printer.set_job_on_hold(value)
            self._held = value
        except Exception as exception:                       # noqa: BLE001
            self._logger.warning("could not %s the job hold: %s",
                                 "take" if value else "release", exception)
            if not value:
                self._held = False

    def _finished(self):
        """Called by the routine thread after its safe end."""
        self._hold(False)
        self._apply_split_setting()

    def _on_message(self, payload):
        kind = payload.get("type")
        if kind == "progress":
            self._progress = payload
        text = payload.get("message")
        if kind == "done":
            result = payload.get("result") or {}
            self._last = result
            self._last_error = None
            self._keep(result)
            text = self._done_text(result)
        elif kind == "failed":
            self._last_error = payload.get("message")
            text = "Failed: %s" % payload.get("message")
        elif kind == "wipe":
            text = None
        if text:
            self._log.append("%s  %s" % (time.strftime("%H:%M:%S"), text))
            self._logger.info("%s", text)
        self._plugin_manager.send_plugin_message(self._identifier, payload)

    @staticmethod
    def _done_text(result):
        t1 = result.get("t1") or {}
        return ("Done in %.0f min. Mesh saved. T1 offset %.3f -> %.3f%s." % (
            result.get("minutes") or 0, t1.get("before") or 0, t1.get("after") or 0,
            "" if t1.get("written") else " (not written)"))

    def _keep(self, result):
        folder = self.get_plugin_data_folder()
        try:
            with open(os.path.join(folder, LAST_FILE), "w") as handle:
                json.dump(result, handle)
            with open(os.path.join(folder, HISTORY_FILE), "a") as handle:
                handle.write(json.dumps(result) + "\n")
        except Exception:                                    # noqa: BLE001
            self._logger.exception("could not write the result")

    def _read_json(self, name):
        path = os.path.join(self.get_plugin_data_folder(), name)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as handle:
                return json.load(handle)
        except Exception:                                    # noqa: BLE001
            self._logger.exception("could not read %s", path)
            return None

    # -- updates ----------------------------------------------------------

    def get_update_information(self):
        """Let OctoPrint offer updates from the GitHub releases."""
        return dict(
            nozzletouch=dict(
                displayName="Nozzle Touch",
                displayVersion=self._plugin_version,
                type="github_release",
                user="chrisns",
                repo="OctoPrint-NozzleTouch",
                current=self._plugin_version,
                stable_branch=dict(name="Stable", branch="main", comittish=["main"]),
                pip="https://github.com/chrisns/OctoPrint-NozzleTouch/archive/{target_version}.zip",
            )
        )


class _Routine(Calibration):
    """The calibration, which tells the plugin when it has finished."""

    def __init__(self, plugin, *args, **kwargs):
        Calibration.__init__(self, *args, **kwargs)
        self._plugin = plugin

    def run(self):
        try:
            Calibration.run(self)
        finally:
            self._plugin._finished()


def __plugin_load__():
    global __plugin_implementation__
    global __plugin_hooks__
    __plugin_implementation__ = NozzleTouchPlugin()
    __plugin_hooks__ = {
        "octoprint.comm.protocol.gcode.received":
            __plugin_implementation__.gcode_received,
        "octoprint.plugin.softwareupdate.check_config":
            __plugin_implementation__.get_update_information,
        "octoprint.access.permissions":
            __plugin_implementation__.get_additional_permissions,
    }
