$(function () {
    // The steps a run goes through, and the routine phases in each.
    var STEPS = [
        {label: "Heat, and retract the filament", phases: ["starting", "heat", "retract"]},
        {label: "Brush the nozzles", phases: ["wipe"]},
        {label: "Cool the nozzles", phases: ["cool"]},
        {label: "Touch the plate at every node", phases: ["home", "mesh"]},
        {label: "Save the mesh", phases: ["write"]},
        {label: "Set the T1 height", phases: ["offset"]}
    ];
    // Diverging scale: low is blue, high is red, zero is neutral.
    var LOW = [42, 120, 214], MID = [240, 239, 236], HIGH = [227, 73, 72];

    function errorText(response) {
        if (response && response.responseJSON && response.responseJSON.error) {
            return response.responseJSON.error;
        }
        return (response && response.statusText) || "failed";
    }

    function signed(value, digits) {
        return (value >= 0 ? "+" : "") + value.toFixed(digits);
    }

    function colour(t) {
        var end = t < 0 ? LOW : HIGH, a = Math.min(Math.abs(t), 1);
        var rgb = MID.map(function (m, k) { return Math.round(m + (end[k] - m) * a); });
        return "rgb(" + rgb.join(",") + ")";
    }

    function NozzleTouchViewModel(parameters) {
        var self = this;

        self.loginState = parameters[0];
        self.access = parameters[1];
        self.printerState = parameters[2];
        self.settings = parameters[3];

        self.running = ko.observable(false);
        self.phase = ko.observable("");
        self.message = ko.observable("");
        self.done = ko.observable(0);
        self.total = ko.observable(0);
        self.waitingForWipe = ko.observable(false);
        self.log = ko.observable("");
        self.last = ko.observable(null);
        self.error = ko.observable("");
        self.settingsError = ko.observable("");
        self.plateClear = ko.observable(false);
        self.view = ko.observable("shape");

        var wipeNotice = null;

        // The buttons that move the machine need the CALIBRATE permission. The server
        // refuses without it either way; hiding them keeps the page honest about what
        // this user may do.
        self.canCalibrate = ko.pureComputed(function () {
            return self.loginState.hasPermission(
                self.access.permissions.PLUGIN_NOZZLETOUCH_CALIBRATE);
        });

        function setting(key, fallback) {
            try {
                var value = self.settings.settings.plugins.nozzletouch[key]();
                return value === undefined || value === null ? fallback : value;
            } catch (e) {
                return fallback;
            }
        }

        self.wipeTimeoutMin = ko.pureComputed(function () {
            return setting("wipe_timeout_min", 30);
        });

        // One pass of the 100 nodes took 23 min on 2026-09-25. Heating, cooling and
        // the T1 touches add about 15 min.
        self.durationText = ko.pureComputed(function () {
            var passes = parseInt(setting("passes", 2), 10) || 2;
            return (15 + 25 * passes) + " min";
        });

        self.readyText = ko.pureComputed(function () {
            if (!self.printerState.isOperational()) return "The printer is not connected.";
            if (self.printerState.isPrinting() || self.printerState.isPaused()) {
                return "The printer is busy with a print.";
            }
            if (!self.canCalibrate()) return "You do not have permission to run this.";
            if (self.settingsError()) return "The settings are not safe to run: " + self.settingsError();
            return "";
        });

        self.canStart = ko.pureComputed(function () {
            return !self.running() && self.plateClear() && self.readyText() === "";
        });

        self.percent = ko.pureComputed(function () {
            return self.total() ? Math.round(100 * self.done() / self.total()) : 0;
        });

        self.steps = STEPS.map(function (step, index) {
            return {
                label: step.label,
                state: ko.pureComputed(function () {
                    if (!self.running()) return "todo";
                    var current = -1;
                    STEPS.forEach(function (s, k) {
                        if (s.phases.indexOf(self.phase()) >= 0) current = k;
                    });
                    if (index < current) return "done";
                    return index === current ? "active" : "todo";
                })
            };
        });

        self.phaseText = ko.pureComputed(function () {
            var found = "";
            STEPS.forEach(function (s) {
                if (s.phases.indexOf(self.phase()) >= 0) found = s.label + ".";
            });
            return found;
        });

        // -- the last result ----------------------------------------------

        self.hasChange = ko.pureComputed(function () {
            var r = self.last();
            return !!(r && r.change);
        });

        self.summaryRows = ko.pureComputed(function () {
            var r = self.last();
            if (!r) return [];
            var s = r.summary || {}, t1 = r.t1 || {};
            var rows = [
                {label: "Finished", value: new Date(r.finished * 1000).toLocaleString() +
                    ", " + Math.round(r.minutes) + " min, " + r.passes +
                    (r.passes === 1 ? " pass" : " passes")},
                {label: "Pass agreement", value: r.pass_spread === null || r.pass_spread === undefined
                    ? "one pass only"
                    : "the passes differ by " + r.pass_spread.toFixed(3) + " mm at most"},
                {label: "Plate tilt", value: "X " + signed(s.tilt_x_per_100mm, 2) + " mm, Y " +
                    signed(s.tilt_y_per_100mm, 2) + " mm, for each 100 mm"},
                {label: "Flatness, tilt removed", value: signed(s.flatness_min, 3) + " to " +
                    signed(s.flatness_max, 3) + " mm, RMS " + s.flatness_rms.toFixed(3) + " mm"},
                {label: "T1 offset (M218 T1 Z)", value: t1.before.toFixed(3) + " before, " +
                    t1.after.toFixed(3) + " after" + (t1.written ? ", written" : ", not written")},
                {label: "T1 minus T0", value: signed(t1.mean_difference, 3) + " mm on average, spread " +
                    t1.spread.toFixed(3) + " mm"}
            ];
            if (r.change_max !== null && r.change_max !== undefined) {
                rows.splice(4, 0, {label: "Largest change from the old mesh",
                                   value: r.change_max.toFixed(3) + " mm"});
            }
            if (r.fake_acks) {
                rows.push({label: "Lost acknowledgements restored", value: String(r.fake_acks)});
            }
            return rows;
        });

        self.t1Rows = ko.pureComputed(function () {
            var r = self.last();
            if (!r || !r.t1 || !r.t1.points) return [];
            function trip(v) { return v === null || v === undefined ? "none" : signed(v, 2); }
            return r.t1.points.map(function (p) {
                var both = p.t0 !== null && p.t1 !== null && p.t0 !== undefined && p.t1 !== undefined;
                return {
                    point: "X" + Math.round(p.x) + " Y" + Math.round(p.y),
                    t0: trip(p.t0),
                    t1: trip(p.t1),
                    diff: both ? signed(p.t1 - p.t0, 2) : ""
                };
            });
        });

        function mapValues(r) {
            if (self.view() === "change" && r.change) return r.change;
            var flat = [];
            r.mesh.forEach(function (column) { flat = flat.concat(column); });
            var mean = flat.reduce(function (a, b) { return a + b; }, 0) / flat.length;
            return r.mesh.map(function (column) {
                return column.map(function (v) { return v - mean; });
            });
        }

        self.mapCaption = ko.pureComputed(function () {
            var r = self.last();
            if (!r) return "";
            var what = self.view() === "change" && r.change
                ? "Each node's new height minus the old mesh at the same place, in mm."
                : "Each node's height above the mean of the mesh, in mm. Tilt is included.";
            return what + " Hatched nodes are past the reach of the nozzle: the firmware " +
                   "needs them, so they are extended from the nearest touched nodes.";
        });

        self.mapSvg = ko.pureComputed(function () {
            var r = self.last();
            if (!r || !r.mesh) return "";
            var z = mapValues(r);
            var n = z.length, cell = 38, left = 46, top = 8, bottom = 64;
            var width = left + n * cell + 8, height = top + n * cell + bottom;
            var limit = 0;
            z.forEach(function (column) {
                column.forEach(function (v) { limit = Math.max(limit, Math.abs(v)); });
            });
            limit = Math.max(0.05, Math.ceil(limit / 0.05) * 0.05);
            var area = r.probed_area || [0, 0, 1e9, 1e9];
            var start = r.grid.start, spacing = r.grid.spacing;
            var out = ['<svg viewBox="0 0 ' + width + " " + height + '" width="100%" ' +
                       'role="img" aria-label="Bed mesh map" xmlns="http://www.w3.org/2000/svg">',
                       '<defs><pattern id="nozzletouch_hatch" width="6" height="6" ' +
                       'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">' +
                       '<line x1="0" y1="0" x2="0" y2="6" stroke="#555" stroke-width="1.5" ' +
                       'stroke-opacity="0.45"/></pattern></defs>'];
            for (var i = 0; i < n; i++) {
                for (var j = 0; j < n; j++) {
                    var v = z[i][j], t = v / limit;
                    var x = start[0] + i * spacing[0], y = start[1] + j * spacing[1];
                    var px = left + i * cell, py = top + (n - 1 - j) * cell;
                    var outside = x > area[2] + 0.5 || y > area[3] + 0.5;
                    var ink = Math.abs(t) > 0.6 ? "#fff" : "#222";
                    out.push('<g><title>X' + Math.round(x) + " Y" + Math.round(y) + ": " +
                             signed(v, 3) + " mm" + (outside ? ", extended" : ", touched") +
                             "</title>");
                    out.push('<rect x="' + (px + 1) + '" y="' + (py + 1) + '" width="' + (cell - 2) +
                             '" height="' + (cell - 2) + '" rx="3" fill="' + colour(t) + '"/>');
                    if (outside) {
                        out.push('<rect x="' + (px + 1) + '" y="' + (py + 1) + '" width="' +
                                 (cell - 2) + '" height="' + (cell - 2) +
                                 '" rx="3" fill="url(#nozzletouch_hatch)"/>');
                    }
                    out.push('<text x="' + (px + cell / 2) + '" y="' + (py + cell / 2 + 4) +
                             '" text-anchor="middle" font-size="10.5" fill="' + ink + '">' +
                             signed(v, 2) + "</text></g>");
                }
            }
            // axes: every second node
            for (var k = 0; k < n; k += 2) {
                out.push('<text x="' + (left + k * cell + cell / 2) + '" y="' + (top + n * cell + 16) +
                         '" text-anchor="middle" font-size="10.5" fill="currentColor">' +
                         Math.round(start[0] + k * spacing[0]) + "</text>");
                out.push('<text x="' + (left - 6) + '" y="' + (top + (n - 1 - k) * cell + cell / 2 + 4) +
                         '" text-anchor="end" font-size="10.5" fill="currentColor">' +
                         Math.round(start[1] + k * spacing[1]) + "</text>");
            }
            out.push('<text x="' + (left + n * cell / 2) + '" y="' + (top + n * cell + 32) +
                     '" text-anchor="middle" font-size="11" fill="currentColor">X, mm</text>');
            out.push('<text x="12" y="' + (top + n * cell / 2) + '" text-anchor="middle" ' +
                     'font-size="11" fill="currentColor" transform="rotate(-90 12 ' +
                     (top + n * cell / 2) + ')">Y, mm</text>');
            // legend
            var ly = top + n * cell + 42, lw = n * cell * 0.6, lx = left + (n * cell - lw) / 2;
            out.push('<defs><linearGradient id="nozzletouch_scale">' +
                     '<stop offset="0" stop-color="' + colour(-1) + '"/>' +
                     '<stop offset="0.5" stop-color="' + colour(0) + '"/>' +
                     '<stop offset="1" stop-color="' + colour(1) + '"/></linearGradient></defs>');
            out.push('<rect x="' + lx + '" y="' + ly + '" width="' + lw + '" height="8" rx="2" ' +
                     'fill="url(#nozzletouch_scale)"/>');
            out.push('<text x="' + (lx - 6) + '" y="' + (ly + 8) + '" text-anchor="end" ' +
                     'font-size="10.5" fill="currentColor">' + signed(-limit, 2) + " lower</text>");
            out.push('<text x="' + (lx + lw + 6) + '" y="' + (ly + 8) + '" font-size="10.5" ' +
                     'fill="currentColor">' + signed(limit, 2) + " higher</text>");
            out.push("</svg>");
            return out.join("");
        });

        // -- actions ------------------------------------------------------

        self.append = function (line) {
            self.log(self.log() + line + "\n");
            // keep the newest line in view, the way a terminal does
            var pane = document.getElementById("nozzletouch_log");
            if (pane) {
                window.setTimeout(function () { pane.scrollTop = pane.scrollHeight; }, 0);
            }
        };

        function stamp(text) {
            var now = new Date();
            function two(v) { return (v < 10 ? "0" : "") + v; }
            return two(now.getHours()) + ":" + two(now.getMinutes()) + ":" +
                   two(now.getSeconds()) + "  " + text;
        }

        self.start = function () {
            self.error("");
            OctoPrint.simpleApiCommand("nozzletouch", "start", {})
                .done(function () {
                    self.running(true);
                    self.plateClear(false);
                })
                .fail(function (response) { self.error(errorText(response)); });
        };

        self.confirmWipe = function () {
            OctoPrint.simpleApiCommand("nozzletouch", "confirm_wipe", {})
                .done(function () { self.waitingForWipe(false); closeWipeNotice(); })
                .fail(function (response) { self.error(errorText(response)); });
        };

        self.abort = function () {
            OctoPrint.simpleApiCommand("nozzletouch", "abort", {});
        };

        function closeWipeNotice() {
            if (wipeNotice) {
                wipeNotice.remove();
                wipeNotice = null;
            }
        }

        function notify(title, text, type, sticky) {
            return new PNotify({title: title, text: text, type: type, hide: !sticky});
        }

        self.refresh = function () {
            OctoPrint.simpleApiGet("nozzletouch").done(function (data) {
                self.running(!!data.running);
                self.phase(data.phase || "");
                self.waitingForWipe(!!data.waiting_for_wipe);
                self.log((data.log || []).join("\n") + (data.log && data.log.length ? "\n" : ""));
                self.last(data.last || null);
                self.error(data.error ? "Failed: " + data.error : "");
                self.settingsError(data.settings_error || "");
                var p = data.progress;
                self.message(p ? p.message : "");
                self.done(p && p.done ? p.done : 0);
                self.total(p && p.total ? p.total : 0);
                if (!self.hasChange()) self.view("shape");
            });
        };

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "nozzletouch") return;
            if (data.type === "started") {
                self.running(true);
                self.error("");
                self.log("");
                self.done(0);
                self.total(0);
                self.append(stamp(data.message));
            } else if (data.type === "progress") {
                self.running(true);
                self.phase(data.phase);
                self.message(data.message);
                if (data.total) {
                    self.done(data.done);
                    self.total(data.total);
                }
                if (data.phase !== "wipe") self.waitingForWipe(false);
                self.append(stamp(data.message));
            } else if (data.type === "wipe") {
                self.waitingForWipe(true);
                closeWipeNotice();
                wipeNotice = notify("Brush the nozzles",
                    "Both nozzles are hot. Brush them, then press Continue on the Nozzle Touch tab.",
                    "info", true);
            } else if (data.type === "done") {
                self.running(false);
                self.waitingForWipe(false);
                closeWipeNotice();
                self.last(data.result);
                self.view(data.result && data.result.change ? self.view() : "shape");
                self.append(stamp("Done. The mesh and the T1 offset are saved."));
                notify("Nozzle touch calibration", "Done. The mesh is saved.", "success", false);
            } else if (data.type === "failed") {
                self.running(false);
                self.waitingForWipe(false);
                closeWipeNotice();
                self.error("Failed: " + data.message);
                self.append(stamp("Failed: " + data.message));
                notify("Nozzle touch calibration failed", data.message, "error", true);
            } else if (data.type === "aborted") {
                self.running(false);
                self.waitingForWipe(false);
                closeWipeNotice();
                self.append(stamp(data.message));
                notify("Nozzle touch calibration stopped", data.message, "notice", false);
            }
        };

        self.onBeforeBinding = function () { self.refresh(); };
        self.onUserLoggedIn = function () { self.refresh(); };
        self.onEventSettingsUpdated = function () { self.refresh(); };

        // OctoPrint's own tab markup ignores the `icon` key in get_template_configs, so
        // the icon has to be put in by hand. The classes match the ones UI Customizer
        // uses on the built-in tabs. It only adds an icon when the link has none.
        self.addTabIcon = function () {
            var link = document.querySelector("#tab_plugin_nozzletouch_link a");
            if (!link || link.querySelector("i")) return;
            var icon = document.createElement("i");
            icon.className = "UICPadRight hidden-tablet fas fa-ruler-vertical";
            link.insertBefore(document.createTextNode(" "), link.firstChild);
            link.insertBefore(icon, link.firstChild);
        };

        self.onAllBound = function () {
            self.addTabIcon();
            // Again a moment later, in case another plugin rebuilds the tab bar.
            setTimeout(self.addTabIcon, 500);
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: NozzleTouchViewModel,
        dependencies: ["loginStateViewModel", "accessViewModel",
                       "printerStateViewModel", "settingsViewModel"],
        elements: ["#nozzletouch_tab"]
    });
});
