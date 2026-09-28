---
layout: plugin

id: nozzletouch
title: Nozzle Touch
description: Levels the bed of a Snapmaker 2.0 dual extruder by touching the plate with the cold nozzle, and sets the T1 height with M218.
authors:
- Chris Nesbitt-Smith
license: MIT

date: 2026-09-26

homepage: https://github.com/chrisns/OctoPrint-NozzleTouch
source: https://github.com/chrisns/OctoPrint-NozzleTouch
archive: https://github.com/chrisns/OctoPrint-NozzleTouch/archive/refs/heads/main.zip

tags:
- calibration
- bed levelling
- mesh
- marlin
- snapmaker
- dual extruder

screenshots:
- url: /assets/img/plugins/nozzletouch/tab-result.png
  alt: The Nozzle Touch tab after a run
  caption: The summary, the mesh map and the T1 touches after a run
- url: /assets/img/plugins/nozzletouch/tab-brush.png
  alt: The tab while the run waits for the brush step
  caption: The run waits while you brush the nozzles
- url: /assets/img/plugins/nozzletouch/settings.png
  alt: The plugin settings
  caption: Every value is checked before it reaches a G-code command

featuredimage: /assets/img/plugins/nozzletouch/tab-result.png

compatibility:
  octoprint:
  - 1.8.0
  os:
  - linux
  - windows
  - macos
  - freebsd
  python: ">=3.7,<4"
---

Press one button. The plugin heats both nozzles and retracts the filament, waits while you
brush the nozzles, and cools them. It then touches the plate with the cold nozzle at every
node of the firmware's 11 x 11 mesh, writes the mesh, and sets the T1 nozzle height from
touches of both nozzles.

The plugin talks only to the printer over its serial connection. It uses no cloud service
and collects no data.

**Safety.** The nozzles touch the plate. The plugin lowers the nozzle in steps of 0.1 mm and
0.02 mm, reads the nozzle sensor after every step, and never goes below a floor. It refuses
to move down unless bed levelling is on and the stored mesh is sane. Every stop or error
raises the nozzle, turns the soft endstops back on and turns the heaters off.
