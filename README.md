# OctoPrint-NozzleTouch

Levels the bed of a Snapmaker 2.0 A350 with the dual extruder toolhead. The cold nozzle
touches the plate at every node of the firmware's 11 x 11 mesh. The plugin then sets the
height of the T1 nozzle from touches of both nozzles. One button runs the whole sequence.

## Why the nozzle is the probe

The dual toolhead's own auto-level measures the plate with an inductive sensor. That sensor
sees the metal under the coating, not the top of the plate. The firmware then sets the zero
with one nozzle touch and a fixed compensation for the quick swap kit. On the author's
printer, with the quick swap kit and the bracing kit, that zero sat about 0.3 mm above the
plate, and the mesh missed the plate top by up to 0.1 mm.

Each nozzle of the dual toolhead floats. When the plate pushes a nozzle up, its
optocoupler trips. The plugin lowers the cold nozzle in small steps and reads the sensor
after every step. The height of each trip, plus the sensor travel, is the height of the
plate. The plugin writes these heights into the firmware's own mesh, so G-code Z 0 sits on
the plate everywhere.

On the author's printer, 2026-09-25:

- Touches at test points after the calibration agreed with the mesh to 0.03 mm or better.
- A first-layer ladder printed under the part confirmed the slicer Z offset that the
  mesh predicted.

## What a run does

1. Homes, and heats the bed and both nozzles.
2. Retracts 6 mm of filament on both nozzles, and parks the head high.
3. Waits while you brush both nozzle tips. Then you press Continue. If nobody presses
   Continue in 30 min, the heaters go off and the run stops.
4. Cools both nozzles below 55 °C with both part fans, then waits 60 s. The bed stays hot.
5. Homes again. Checks that bed levelling is on and that the stored mesh is sane. Turns the
   soft endstops off, because each trip is below G-code Z 0.
6. Touches the 100 reachable nodes of the 11 x 11 grid with T0. Before each row it touches
   a reference point and removes the drift. It does this once for each pass, and averages
   the passes.
7. Writes the mesh with `G1029 P11`, one `M421` for each node, `M420 S1` and `M500`. If a
   step fails, it puts the old mesh back.
8. Touches five points with T0, then the same points with T1. It sets `M218 T1 Z` so both
   nozzles trip at the same height on average, then reads the value back.
9. Always, also after an error or a stop: soft endstops on, nozzle 5 mm up, T0 active,
   nozzle heaters and part fans off, bed off.

Two passes take about 65 min, plus the time you take to brush the nozzles.

The bed heats while the nozzles heat, retract and wait for the brush. The run waits for the bed
only when the nozzles are cold, just before the first touch.

If the nozzle tips are already clean, tick "The nozzle tips are already clean" on the tab.
The run then skips steps 1 to 3: it heats only the bed, cools the nozzles if they are warm,
and starts touching.

## Safety

**WARNING: The nozzles touch the plate. Remove everything from the plate before you
start.**

- The touch lowers the nozzle 0.1 mm at a time, then 0.02 mm at a time, and reads `M119`
  after every step, after `M400`. The firmware answers `M119` at once, before a queued move has
  run, so without `M400` every reading is one step behind. It stops at the first trip. It never goes lower than 0.8 mm below the
  expected trip (1.1 mm on a second try), and never below G-code Z -2.
- The plugin does not use `G30`. On this firmware a `G30` probe move targets machine Z -2,
  and the plate sits at machine Z 42. A sensor that did not trip would drive the nozzle
  44 mm into the plate.
- Before it moves down, the plugin reads "Bed Leveling ON" from `M420 S1`. On a printer
  with the quick swap kit, only the mesh lifts G-code Z 0 to the plate. With levelling off,
  `G0 Z3` is about 39 mm below the plate.
- The plugin refuses a stored mesh with values more than 5 mm apart. `G1029` fills a new
  grid with 75, so a write that stopped half way leaves some nodes 33 mm above the others.
- With the soft endstops off, this firmware also stops checking X and Y. The plugin
  refuses a reference point or a T1 point outside the touched area, X 52 to 304 and Y 43 to
  299.5.
- If a print starts during a run, the plugin holds the print, stops the run, and lets the
  print go when the nozzle is up again.
- If the printer disconnects, the run stops.
- Any user who can see the tab can stop a run.

## The lost acknowledgement

When a nozzle is pushed up, the toolhead sends `active extruder mismatch target: 0!` at
once. When that message lands inside the firmware's `ok`, OctoPrint receives
`oactive extruder mismatch target: 0!` and then `k`. It sees no `ok`, and it stops sending
until its timeout. Every nozzle touch sends the message, so this happens.

The plugin sees the damaged line and gives OctoPrint the lost `ok` back. During a run this
is always on. A setting turns it on outside a run too, which is the default.

## What you need

- A Snapmaker 2.0 A350 with the dual extruder toolhead. The grid area in `mesh.py` is the
  A350's, X 52 to 332 and Y 43 to 328. Do not use the plugin on an A150 or an A250 until you
  change it.
- OctoPrint 1.8 or newer, Python 3.7 or newer. Tested on OctoPrint 1.11.8.
- A brass wire brush.

## Install

In the OctoPrint interface, open Settings, then Plugin Manager, then Get More, and paste
this into "... from URL":

```
https://github.com/chrisns/OctoPrint-NozzleTouch/archive/refs/heads/main.zip
```

On a normal Linux install:

```bash
~/oprint/bin/pip install https://github.com/chrisns/OctoPrint-NozzleTouch/archive/refs/heads/main.zip
sudo service octoprint restart
```

On Windows, from the folder that holds OctoPrint's Python:

```bat
C:\OctoPrint\WPy64-31050\python-3.10.5.amd64\python.exe -m pip install https://github.com/chrisns/OctoPrint-NozzleTouch/archive/refs/heads/main.zip
net stop OctoPrint5000 & net start OctoPrint5000
```

Updates then come through OctoPrint's own software update plugin.

## Use

1. Remove everything from the plate. Keep filament loaded in both nozzles.
2. Open the Nozzle Touch tab. Tick the box, then press Start.
3. When the tab says so, brush both nozzle tips with the brass brush until no plastic is
   left on them. Then press Continue. A notification also tells you when to brush.
4. Wait for the run to finish. The tab shows the new mesh and the new T1 offset.
5. Print a first-layer test before a long print. With this mesh, G-code Z 0 is the plate
   surface. Set the squish of the first layer with the slicer's Z offset.

**CAUTION: Do not run the auto-level on the touchscreen after a run. It replaces this mesh
with its own.**

## Permissions

The plugin adds two OctoPrint permissions.

| Permission | Default | What it allows |
|---|---|---|
| `PLUGIN_NOZZLETOUCH_VIEW` | users and admins | Reading the progress and the last result, and stopping a run |
| `PLUGIN_NOZZLETOUCH_CALIBRATE` | admins | Starting a run, and confirming the brush step |

A command with no permission listed is refused, so a new command cannot be added without
deciding who may run it.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| Bed | 75 °C | The plate is measured at this temperature. Use the one you print at. |
| T0, T1 | 240, 215 °C | Hot enough to brush the filament off each nozzle. |
| Retract | 6 mm | Stops the hot nozzles oozing while you brush them. |
| Park | X160 Y30 Z60 | Where the head waits while you brush. |
| Brush time limit | 30 min | The heaters go off after this. |
| Touch below | 55 °C | The nozzles touch the plate only below this temperature. |
| Settle | 60 s | A wait after the fans stop. |
| Passes | 2 | Each pass touches all 100 nodes. The mesh is the mean. |
| Reference | X178 Y171 | Touched before each row, to measure the drift. |
| Sensor travel | 0.72 mm | Carriage travel from the first touch to the trip. |
| T1 points | 5 points | Both nozzles touch each one. |
| Write T1 offset | on | Sends `M218 T1 Z`. The toolhead stores it at once. |
| Bed off at end | on | Turns the bed off after the run. |
| Restore lost ok | on | Also outside a run. See above. |

## The numbers behind it

All measured on the author's A350 on 2026-09-25.

| What | Value | What the plugin does with it |
|---|---|---|
| Sensor travel after first touch | 0.72 mm, clean nozzle | Adds it to every trip |
| Repeat at one point | ±0.01 mm | One touch for each node and pass |
| Trip drift while hot | up to 0.2 mm an hour | Touches a reference before each row |
| Bed heater swing | 73.5 to 75.5 °C, ±0.04 mm | Averages two passes |
| Dirty nozzle | read 0.2 to 0.38 mm high | Brush step before every run |
| Interpolation error of the 11 x 11 grid | 0.03 mm at most | Uses the largest grid the firmware has |

The firmware's spacing is a whole number of millimetres, so the 11 x 11 grid is 28 x 28 mm,
not 28 x 28.5. The plugin probes at the nozzle's reach, interpolates onto the firmware's
own nodes, and extends the heights to the nodes the nozzle cannot reach.

For T1, the firmware's own nozzle routine adds a different compensation for each nozzle,
0.573 and 0.497 mm on this toolhead. A print ladder showed that this puts T1 about 0.07 mm
too low. The plugin takes the travel to the trip as equal for both nozzles.

## Development

```bash
git clone https://github.com/chrisns/OctoPrint-NozzleTouch.git
cd OctoPrint-NozzleTouch
python3 -m pytest -q
```

The tests need no OctoPrint and no printer. `tests/fakes.py` simulates the printer: a
plate with a tilt and a bump, the firmware mesh, and two floating nozzles.

## Licence

MIT. See [LICENSE](LICENSE).
