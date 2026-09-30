# streams — DEM Hydrology

Turn a digital elevation model (DEM) into two hydrological products:

1. **Stream networks** — the channel centrelines, as vector `LineString`s.
2. **Catchment areas** — the contributing area that drains to a given outlet point, as vector polygons.

Point it at a DEM, pick an accumulation threshold, and it writes a GeoPackage you can open in QGIS.

The hydrology itself is done by [pysheds](https://github.com/USEPA/pysheds). This repository is
the conditioning + vectorisation wrapper around it, plus an interactive menu.

---

## Requirements

- **Python 3.10 – 3.13** (verified on 3.13; `numba`, a pysheds dependency, is the first thing to
  lose wheel coverage on a brand-new Python release)
- A single-band DEM raster in **GeoTIFF** format, with a CRS attached
- ~1 GB free RAM for large DEMs — the bundled example is 3601 × 3601 cells

## Install

```bash
git clone https://github.com/huzaifffa/streams.git
cd streams

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install pysheds geopandas rasterio shapely
```

`numba`, `pyproj`, `scikit-image`, `scipy` and `pandas` arrive automatically as pysheds
dependencies. You do **not** need to install pysheds from source.

> **NumPy 2.x is supported.** `run.py` patches the two NumPy 2.0 removals that pysheds still calls
> (`np.in1d`, `np.bool8`) at import time. Verified working on numpy 2.5.3. If you refactor that
> file, keep those four lines **above** `import pysheds` or stream extraction will crash.

---

## Quick start

```bash
python run.py
```

A sample DEM ships with the repo, so you can run this with zero setup — press Enter at the first
prompt to accept the default path.

```
=================== DEM Hydrology ===================
  1) Stream network
  2) Catchment area
  3) Both
  0) Quit
======================================================
Choose an option: 3

--- Stream network ---
DEM path [/Users/you/streams/n32_w117_1arc_v3.tif]:      <- Enter, for the bundled example
Accumulation threshold [1000]:                           <- Enter, for the default
  Processing...
  Saved: /Users/you/streams/streams.gpkg

--- Catchment area ---
  DEM extent: lon -117.000..-116.000, lat 32.000..33.000 (EPSG:4326)
  Enter the outlet point (where the catchment drains).
  The nearest channel cell is used, so precision is not critical.

Outlet longitude, latitude (e.g. -117.05, 32.15): -116.3, 32.4
  Processing...

  Outlet given:    -116.30000, 32.40000
  Snapped to:      -116.29986, 32.40013  (29 m away)
  Threshold:       1,000 cells
  Catchment area:  234.60 km2
  Saved:           /Users/you/streams/catchment.gpkg
```

The whole run above takes about **45 seconds** on an Apple Silicon laptop (the DEM is conditioned
twice — once per option — and flat resolution dominates). The first call in a process is slower still,
because `numba` JIT-compiles pysheds' kernels.

> **Note on the prompt's own example.** The tool suggests `-117.05, 32.15`, but that point is
> *outside* the bundled DEM, which only reaches lon −117.000. Entering it produces
> `! That point is outside the DEM extent`, snaps ~4.7 km to the raster edge, and returns a
> meaningless ~1 km² catchment. Use `-116.3, 32.4` for the bundled sample instead.

### Menu

| Key | Action |
|-----|--------|
| `1` | Stream network only |
| `2` | Catchment area only |
| `3` | Both, in sequence (asks for the DEM and threshold only once) |
| `0` / `q` | Quit |

`Ctrl-C` or `Ctrl-D` at any prompt exits cleanly.

### The prompts

| Prompt | Default | Notes |
|--------|---------|-------|
| `DEM path` | the bundled `n32_w117_1arc_v3.tif` | Must exist — it re-asks until it does. Asked once per session. |
| `Accumulation threshold` | `1000` | Positive whole number only (`1e5` is rejected). Asked once per session. |
| `Outlet longitude, latitude` | none | Decimal degrees, **EPSG:4326**, comma separated. Re-asked for every catchment run. |

**Where the output goes:** the same directory as your DEM, under fixed names — `streams.gpkg` and
`catchment.gpkg`. **Both are overwritten silently on every run.** Move a previous result first if you
want to keep it. The library functions below let you choose the path yourself.

---

## Using it as a library

Both pipelines are plain importable functions. The interactive menu is a thin layer over exactly
these, so you can script against them and control output paths.

```python
import run

# Stream network
run.dem_to_streams("dem.tif", "out/streams.gpkg", accumulation_threshold=1000)

# Catchment
result = run.delineate_catchment(
    "dem.tif", lon=-116.3, lat=32.4,
    output_path="out/catchment.gpkg",
    accumulation_threshold=1000,
)

print(result["area_km2"])     # 234.6
print(result["warnings"])     # [] or a list of human-readable warnings
print(result["truncated"])    # True if the catchment reached the DEM edge
print(result["snapped_lon"], result["snapped_lat"])
```

### `dem_to_streams(dem_path, output_path, accumulation_threshold=1000) -> str`

Returns the output path.

### `delineate_catchment(dem_path, lon, lat, output_path, accumulation_threshold=1000) -> dict`

| Key | Type | Meaning |
|-----|------|---------|
| `path` | `str` | Output path |
| `area_km2` | `float` | Total catchment area, km² |
| `snapped_lon` / `snapped_lat` | `float` | Outlet after snapping to a channel, in the DEM's CRS |
| `snap_dist_m` | `float` | How far the outlet was moved, in metres |
| `threshold` | `int` | The threshold used |
| `truncated` | `bool` | `True` if the catchment reached the DEM edge |
| `warnings` | `list[str]` | Human-readable warnings |

### Scripting the menu

The menu reads stdin and **always exits `0`, even on error**, so it will never fail a CI job. Errors
surface as `  ! Error: ...` and the menu re-renders. For automation, call the functions above instead.

```bash
printf '3\n\n1000\n-116.3, 32.4\n0\n' | python run.py
```

---

## Output

All output is **vector**, in the **CRS of your input DEM** (reprojected back from the UTM working CRS
at the end). Areas are computed in metric space *before* that reprojection, so `area_km2` is a real
square-kilometre value even though the file is in EPSG:4326.

### Streams — `streams.gpkg`, layer `streams`

`LineString` features split at junctions. No attributes. For the bundled DEM at threshold 1000:
**6,705 features, 8,965 km of channel.**

### Catchment — `catchment.gpkg`, three layers

| Layer | Geometry | Contents |
|-------|----------|----------|
| `catchment` | Polygon, one row per part | `area_km2` — part area; `area_pct` — that part's share of the total |
| `outlet_input` | Point (1 row) | The outlet exactly as you typed it |
| `outlet_snapped` | Point (1 row) | The outlet at the snapped channel cell, which falls inside the polygon |

### File formats

The driver is chosen from the output file extension:

| Extension | Driver | Use it? |
|-----------|--------|---------|
| `.gpkg` | GeoPackage | **Yes.** The only format that stores all three catchment layers. |
| `.shp` | ESRI Shapefile | Streams only. |
| `.geojson` | GeoJSON | Streams only. |

`.shp` and `.geojson` ignore the layer name, so a *catchment* written to either silently collapses to
the last layer — a single point, with the polygon lost. The interactive menu always writes `.gpkg`;
this only bites you if you call `delineate_catchment` with a different extension.

---

## The accumulation threshold

This is the one parameter that matters, and the one most likely to surprise you.

**The threshold is a count of upstream cells, not an area.** `grid.accumulation()` is called without
weights, so a cell's value is literally "how many cells drain into me, including me". A cell becomes
stream if that count exceeds the threshold.

To convert to real ground area, multiply by the working pixel size:

```
area_km2 = threshold x pixel_size_m^2 / 1e6
```

For the bundled DEM, the geographic → UTM (EPSG:32611) reprojection produces **28.5406 m** cells
(814.6 m² each), so:

| Threshold | Approx. contributing area |
|-----------|--------------------------|
| `200` | 0.16 km² |
| `1000` (default) | 0.81 km² |
| `5000` | 4.07 km² |
| `20000` | 16.29 km² |

**Lower threshold → denser, more detailed network.** Small channels appear and small catchments
become possible. **Higher threshold → sparse, major rivers only.** If you get
`No stream cells found`, your threshold is too high for the DEM's resolution.

To find the working pixel size for your own DEM:

```bash
python -c "
import rasterio, run, tempfile, os
with tempfile.TemporaryDirectory() as t:
    p, _ = run._reproject_to_utm('your_dem.tif', os.path.join(t, 'd.tif'))
    with rasterio.open(p) as s:
        print('cell:', round(s.transform.a, 4), 'm ->', round(s.transform.a**2, 1), 'm2')
"
```

### Outlet snapping

Your outlet does not have to sit on a channel. The tool snaps it to the nearest cell that already
qualified as stream, so the catchment always terminates on the network, and tells you how far it
moved. Beyond 500 m you get a warning — at that distance either your point is wrong, or the threshold
is too high to have a channel nearby.

---

## How it works

```
DEM (GeoTIFF)
  │
  ├─ reproject to UTM ......... geographic → EPSG:326xx/327xx, zone from the DEM's centre
  │                             longitude, bilinear resampling, into a temp dir deleted on exit
  │
  ├─ condition the DEM ......... fill pits → fill depressions (Priority-Flood) → resolve flats
  │
  ├─ flow routing .............. D8 flow direction → unweighted flow accumulation
  │
  ├─ 1) stream network ......... extract_river_network(acc > threshold) → LineStrings
  │
  └─ 2) catchment .............. snap outlet to a channel cell → trace the upstream catchment
                                → polygonize the mask → compute area
  │
  ▼
reproject back to the input DEM's CRS → write vector file
```

R8/MFD routing, depression-aware or multiple-flow-direction accumulation, and stream-order
classification (Strahler, Shreve) are **not** implemented — everything uses pysheds' D8 defaults.

---

## Troubleshooting

| Message | Cause | Fix |
|---------|-------|-----|
| `Error: DEM has no CRS defined.` | The GeoTIFF has no CRS tag | Assign one: `gdal_edit.py -a_srs EPSG:4326 dem.tif` |
| `Error: No stream cells found. Try lowering accumulation_threshold` | No cell exceeds the threshold | Lower it. `1000` is already permissive on a 1 arc-second DEM; on a coarse 90 m DEM try `10`. |
| `Error: No streams found. Try lowering accumulation_threshold` | Mask was non-empty but yielded no network features | Same — lower the threshold. |
| `Error: Catchment is empty at that outlet point.` | The snapped point has no upstream cells | Check the point is inside the DEM and that you are not sitting on a divide. |
| `Catchment reaches the edge of the DEM - the area is a lower bound.` | The catchment drains off the raster | Expected for an outlet near the DEM edge. Use a larger DEM for the true area. |
| `Outlet was N m from the nearest channel` | Point is far from any channel | Verify the coordinate, or lower the threshold. |
| `! That point is outside the DEM extent.` | Coordinate is off the raster | Check for swapped lon/lat. The run continues anyway — check the result before trusting it. |
| `! Invalid choice.` | Menu input was not 0–3 | Re-enter. |

---

## Assumptions and limitations

- **Elevation must be in metres.** A DEM in feet yields nonsense areas.
- **A CRS is mandatory.** The tool aborts if the raster has none.
- **The DEM is not resampled for you.** The UTM working resolution is whatever
  `calculate_default_transform` derives from the source, and there is no resolution control. To
  process faster or coarser, pre-resample your DEM — e.g. `gdalwarp -tr 90 90 dem.tif coarse.tif`.
- **UTM zone is picked from the DEM's centre longitude.** A DEM spanning a zone boundary is
  reprojected wholesale into one zone, with distortion at the edges. The formula has no
  Norway/Svalbard exception.
- **A projected but non-UTM CRS is used as-is**, with no reprojection. Correct — provided its units
  are metres.
- **The threshold is in cells, not area** — see above.
- **Areas are planar**, measured in UTM. Fine at catchment scale, increasingly off at continental
  scale.
- **Nodata handling is basic.** If the DEM declares no nodata value, `-32768` is assumed. Unfilled
  voids become spurious sinks, and therefore spurious streams.
- **Single downslope neighbour per cell.** No flow-through or multi-outlet representation.
- **Flat resolution is slow.** `resolve_flats` is the dominant cost; it is why large DEMs are slow
  here.

---

## Bundled example data

`n32_w117_1arc_v3.tif` — a DTED Level 2 tile (SRTM-derived), US NIMA designator `DTED2`:

| Property | Value |
|----------|-------|
| Size | 3601 × 3601 (1 arc-second) |
| CRS | EPSG:4326 (WGS 84) |
| Extent | lon −117.0001 … −115.9999, lat 31.9999 … 33.0001 |
| Coverage | San Diego / southern California, USA |
| Band 1 | Int16, metres, nodata −32767 |
| Elevations | −6 m to 1976 m, mean 733 m |

A good outlet point inside the tile is **`-116.3, 32.4`** (≈235 km² catchment, snaps 29 m).

The file is ~25 MB and is committed to the repository, which is most of the repo's size.

---

## License & attribution

This repository has no license file — add one before distributing it.

Hydrology is computed by [pysheds](https://github.com/USEPA/pysheds) (BSD-3-Clause, US EPA), which
builds on the `richdem` and `whitebox` predecessors. Flow algorithms follow Barnes, Lehman and Mulla
(2014, *Priority-Flood: An Optimal Depression-Filling and Watershed-Labeling Algorithm*); flat
resolution follows Barnes, Lehman and Mulla (2015, *Rapid Quantization-Aware Flooding of Flats*).
Vector I/O is [GDAL](https://gdal.org) via [GeoPandas](https://geopandas.org) and
[Rasterio](https://rasterio.readthedocs.io).
