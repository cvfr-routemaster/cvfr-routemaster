# CVFR Route Master

An Israel CVFR route-planning assistant for flight-simulator use.

CVFR Route Master helps VATSIM pilots plan and study the route for CVFR
flights in Israel, for both GA-CVFR (called just CVFR in the rest of this document) and LSA-CVFR (called just LSA in the rest of this document) routes. It combines the CAAI's CVFR chart with satellite imagery,
computes distances, times and bearings between waypoints (both the official
reporting points and arbitrary intermediate points if needed — for example
across segments around Jericho), and reads route altitudes from the arrows
printed on the chart. You can also search for waypoints by name, open a
satellite view of a waypoint on third-party imagery sites (Google, Apple,
Bing) and more. There is also an option to manually enter control
frequencies before takeoff based on what is available on the network at the
time.

The tool produces a flight-plan route in a format that is easy to copy into
a VATSIM flight plan, and routes can be saved and loaded from disk. The
route and reporting points are displayed both on the chart and on the
satellite imagery, making it easy to study visual landmarks along the
route. The tool can also display VATSIM traffic, but it is **not a GPS** —
its precision and update rate are the same as VATSIM Radar (every 15
seconds — this is the limit VATSIM allows for access to their servers, and
the tool honours it). When the traffic option is enabled, for every
aircraft over Israel's virtual airspace it shows: callsign, aircraft type,
altitude and speed (the colour indicates the wake category).

## Basic usage

1. **No installation required** — just extract the ZIP wherever you like
   and run the executable. The current release is built for windows only, but running from source is possible also on Linux, see the bottom section of this document.
2. Due to copyright, the tool does not ship with the chart or the satellite
   imagery — but it will download them for your personal use under the
   respective providers' terms of use.
3. On first launch the tool downloads the CVFR charts from CAAI, converts
   them from PDF to images, and loads them on the main screen. This process can take some time on the first use when the CVFR or LSA maps are chosen, however, subsequent launches are rapid. The process is identical for CVFR and LSA maps, with CVFR being loaded by default on first launch, and the process repeating itself the first time the user switches to LSA. Please be patient, this can take a couple of minutes as rendering the huge map images from PDF takes time, even on a powerful CPU.
4. After that, the tool starts downloading satellite imagery — about 2 GB
   total, across four different zoom levels (z=12 through z=15) which the
   tool uses dynamically depending on how far you are zoomed in (closer
   views are served from higher-resolution tiles). Zoom level 12 is fetched
   first, then 13, then 14, and finally 15. Each level becomes available to
   the satellite view as soon as it finishes, so imagery appears fairly
   quickly and the available resolution improves as the download
   progresses. It is possible to copy the existing sattelite tile cache from a previous version of the program, simply copy .cvfr_routemaster/satellite_tiles to the ./cvfr_routemaster folder of your new version. This will save you the download time between versions.
5. It is fine to close the program while a download is in progress — it
   will resume on the next launch.
6. The bottom-right of the window shows download progress for each zoom
   level. The download rate is intentionally modest because the satellite
   imagery provider also has terms of use and the tool honours them.
7. You can use the tool in chart mode while the download is in progress.
8. **To plan a route:** `SHIFT + LEFT CLICK` on points on the chart (for
   example LLHZ Herzliya first, then BAZRA, and so on).
9. **To remove a point from the route:** `SHIFT + RIGHT CLICK` on a point
   in the route. There is also a button to clear the entire route and
   start over.
10. There are a few isolated locations where the magnetic bearing of a
    segment will be off by one degree from what is printed on the chart.
    The reason is that the CAAI chart's rounding is not always consistent
    and the tool computes bearings from the actual coordinates of the
    points.
    - A one-degree deviation is not a meaningful issue for VATSIM at the
      segment lengths involved in Israel.
11. The buttons across the top of the window are split into four groups:
    1. **Program settings** `[PROGRAM SETTINGS]` — you generally will not
       need to touch these.
    2. **What you see on screen** `[VIEW TOGGLES]`:
       - **Airplane mode** `[AIRPLANE MODE]` — flight-plan-only mode (no
         chart).
       - **Hide the waypoint list** `[HIDE WAYPOINT VIEW]` — to free up
         screen space.
       - **Hide the on-screen help text** `[HIDE USAGE HINTS]`.
       - **Show live VATSIM traffic** `[SHOW VATSIM TRAFFIC]` — 15-second
         update interval.
    3. **Satellite mode** `[SATELLITE VIEW]` — swaps the chart for
       satellite imagery of Israel.
    4. **Copyright and licensing information** `[LEGAL AND COPYRIGHT INFO]`.

## Usage limitations and copyright

***This program is NOT intended for use in real-world aviation.***

The program is released under the GNU AGPL license. Anyone is free to
redistribute it, modify it, and redistribute their modifications, etc.,
provided the distribution complies with the AGPL terms. The source code is
distributed alongside the program and is written in Python. The program is
completely free and will always remain so.

## Running from source and on Linux

The program is written in python, so it will happily work from sources (the cvfr_routemaster folder is a self-contained python module), including on Linux.

To do this, first, clone the repo.

There are two caveats for running from source at this stage of the program development:
1. You will need to take the .cvfr_routemaster folder from the windows release ZIP to obtain the map calibration data. Or you will need to calibrate the maps yourself. To do the latter, open the map settings menu, load map files (or point to the actual URLs on the CAAI site) and then perform the calibration of the map (simply follow the instructions on the screen). The .cvfr_routemaster folder must be placed next to the cvfr_routemaster module folder for the program to pick up the contents, if you opt for this route. I recommend you just take the folder from the windows release ZIP and save some time.
2. You need tesseract installed to perform OCR. On Debian (and Ubuntu, Mint, etc), install it with: sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-heb. If you are on a different distro, you probably know what the package install commands are. On windows, you need tesseract executables. The release ZIP contains a tesseract folder, copy it over next to cvfr_routemaster and you are good to go.

Ensure that you have all of the contents of requirements.txt installed in your env or venv, and then:

py -m cvfr_routemaster

Enjoy!
