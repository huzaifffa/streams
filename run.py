import os
import tempfile

import numpy as np

# --- Compatibility patches for NumPy 2.x (pysheds uses removed functions) ---
# This MUST come before importing pysheds.
if not hasattr(np, "in1d"):
    np.in1d = np.isin
if not hasattr(np, "bool8"):
    np.bool8 = np.bool_

import geopandas as gpd
import rasterio
from rasterio.crs import CRS
from rasterio.features import shapes as rio_shapes
from rasterio.warp import calculate_default_transform, reproject, Resampling, transform as warp_transform
from pysheds.grid import Grid
from shapely.geometry import shape, Point


DEFAULT_DEM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "n32_w117_1arc_v3.tif")
DEFAULT_THRESHOLD = 1000


def _reproject_to_utm(dem_path, out_path):
    """Reproject a lat/lon DEM to the matching UTM zone. Returns (path, src_crs)."""
    with rasterio.open(dem_path) as src:
        src_crs = src.crs
        if src_crs is None:
            raise ValueError("DEM has no CRS defined.")

        if not src_crs.is_geographic:
            return dem_path, src_crs  # already projected, use as-is

        lon = (src.bounds.left + src.bounds.right) / 2
        lat = (src.bounds.top + src.bounds.bottom) / 2
        zone = int((lon + 180) / 6) + 1
        epsg = (32600 if lat >= 0 else 32700) + zone
        dst_crs = f"EPSG:{epsg}"

        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds
        )
        meta = src.meta.copy()
        meta.update(crs=dst_crs, transform=transform, width=width, height=height)

        nodata = src.nodata if src.nodata is not None else -32768
        meta.update(nodata=nodata)

        with rasterio.open(out_path, "w", **meta) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear,
                src_nodata=nodata,
                dst_nodata=nodata,
            )
    return out_path, src_crs


def _prepare_flow(proj_path, accumulation_threshold=DEFAULT_THRESHOLD):
    """Condition a (projected) DEM and compute flow direction + accumulation."""
    grid = Grid.from_raster(proj_path)
    dem = grid.read_raster(proj_path)

    pit_filled = grid.fill_pits(dem)
    flooded = grid.fill_depressions(pit_filled)
    inflated = grid.resolve_flats(flooded)

    fdir = grid.flowdir(inflated)
    acc = grid.accumulation(fdir)

    if not np.any(np.asarray(acc) > accumulation_threshold):
        raise ValueError(
            "No stream cells found. Try lowering accumulation_threshold "
            f"(current: {accumulation_threshold})."
        )

    return grid, fdir, acc


def _write_gdf(gdf, output_path, layer=None):
    ext = output_path.lower()
    if ext.endswith(".geojson"):
        gdf.to_file(output_path, driver="GeoJSON")
    elif ext.endswith(".shp"):
        gdf.to_file(output_path, driver="ESRI Shapefile")
    else:
        gdf.to_file(output_path, driver="GPKG", layer=layer or "streams")
    return output_path


def dem_to_streams(dem_path, output_path, accumulation_threshold=DEFAULT_THRESHOLD):
    """
    Extract a stream network (vector lines) from a DEM.

    dem_path : input DEM raster (GeoTIFF)
    output_path : output vector file (.gpkg recommended, .shp or .geojson also work)
    accumulation_threshold : minimum upstream cells for a cell to be a stream
    Returns output_path.
    """
    with tempfile.TemporaryDirectory() as tmp:
        proj_path, src_crs = _reproject_to_utm(dem_path, os.path.join(tmp, "dem_utm.tif"))
        grid, fdir, acc = _prepare_flow(proj_path, accumulation_threshold)
        branches = grid.extract_river_network(fdir, acc > accumulation_threshold)

        if not branches["features"]:
            raise ValueError("No streams found. Try lowering accumulation_threshold.")

        gdf = gpd.GeoDataFrame.from_features(branches["features"], crs=grid.crs)

    # Return streams in the original DEM's CRS
    if src_crs is not None and gdf.crs != src_crs:
        gdf = gdf.to_crs(src_crs)

    return _write_gdf(gdf, output_path)


def delineate_catchment(dem_path, lon, lat, output_path, accumulation_threshold=DEFAULT_THRESHOLD):
    """
    Delineate the catchment (contributing area) that drains to an outlet point.

    dem_path : input DEM raster (GeoTIFF)
    lon, lat : outlet coordinates in decimal degrees (EPSG:4326)
    output_path : output vector file (.gpkg recommended)
    accumulation_threshold : minimum upstream cells for a cell to be a stream
    Returns a dict with the output path, area, snapped outlet and any warnings.
    """
    with tempfile.TemporaryDirectory() as tmp:
        proj_path, src_crs = _reproject_to_utm(dem_path, os.path.join(tmp, "dem_utm.tif"))
        grid, fdir, acc = _prepare_flow(proj_path, accumulation_threshold)

        # Convert the user's lat/lon outlet into the projected working CRS
        xs, ys = warp_transform(src_crs, grid.crs, [lon], [lat])
        x, y = float(xs[0]), float(ys[0])

        # Snap the outlet onto the nearest stream cell so the catchment is
        # guaranteed to terminate on the channel network.
        snapped, dist = grid.snap_to_mask(acc > accumulation_threshold, np.array([[x, y]]), return_dist=True)
        x_snap, y_snap = float(snapped[0][0]), float(snapped[0][1])

        catch = grid.catchment(x=x_snap, y=y_snap, fdir=fdir, xytype="coordinate")
        catch_arr = np.asarray(grid.view(catch))
        if not catch_arr.any():
            raise ValueError("Catchment is empty at that outlet point.")

        # Polygonize the boolean catchment mask in projected (metric) space
        mask = catch_arr.astype(np.uint8)
        geoms = [shape(g) for g, val in rio_shapes(mask, mask=mask, transform=catch.affine) if val == 1]
        if not geoms:
            raise ValueError("Failed to polygonize the catchment mask.")

        area_m2 = sum(g.area for g in geoms)
        gdf = gpd.GeoDataFrame(geometry=geoms, crs=grid.crs)
        gdf["area_km2"] = gdf.area / 1e6
        gdf["area_pct"] = gdf["area_km2"] / (area_m2 / 1e6) * 100

        # A catchment running off the edge of the DEM is only partially captured
        truncated = bool(
            mask[0, :].any() or mask[-1, :].any() or mask[:, 0].any() or mask[:, -1].any()
        )

        # Report the outlet at the snapped cell's centre, which sits inside the
        # polygon (snap_to_mask returns the cell corner).
        col, row = (~catch.affine * (x_snap, y_snap))[:2]
        cx, cy = catch.affine * (int(np.floor(col)) + 0.5, int(np.floor(row)) + 0.5)
        sn_lon, sn_lat = warp_transform(grid.crs, src_crs, [cx], [cy])
        snapped_lon, snapped_lat = float(sn_lon[0]), float(sn_lat[0])
        snap_dist_m = float(dist[0])

    if src_crs is not None and gdf.crs != src_crs:
        gdf = gdf.to_crs(src_crs)

    _write_gdf(gdf, output_path, layer="catchment")
    _write_gdf(
        gpd.GeoDataFrame(geometry=[Point(lon, lat)], crs=src_crs),
        output_path,
        layer="outlet_input",
    )
    _write_gdf(
        gpd.GeoDataFrame(geometry=[Point(snapped_lon, snapped_lat)], crs=src_crs),
        output_path,
        layer="outlet_snapped",
    )

    warnings = []
    if truncated:
        warnings.append("Catchment reaches the edge of the DEM - the area is a lower bound.")
    if snap_dist_m > 500:
        warnings.append(
            f"Outlet was {snap_dist_m:,.0f} m from the nearest channel - "
            "check the point, or lower the accumulation threshold."
        )

    return {
        "path": output_path,
        "area_km2": area_m2 / 1e6,
        "snapped_lon": snapped_lon,
        "snapped_lat": snapped_lat,
        "snap_dist_m": snap_dist_m,
        "threshold": accumulation_threshold,
        "truncated": truncated,
        "warnings": warnings,
    }


# --- Interactive prompts ---


def ask_dem_path(state):
    """Ask for the DEM path once per session and reuse it afterwards."""
    if "dem" in state:
        return state["dem"]
    while True:
        raw = input(f"DEM path [{DEFAULT_DEM}]: ").strip()
        path = raw or DEFAULT_DEM
        if os.path.isfile(path):
            state["dem"] = path
            return path
        print("  ! File not found.")


def ask_threshold(state):
    """Ask for the accumulation threshold once per session and reuse it."""
    if "threshold" in state:
        return state["threshold"]
    while True:
        raw = input(f"Accumulation threshold [{DEFAULT_THRESHOLD}]: ").strip()
        if not raw:
            state["threshold"] = DEFAULT_THRESHOLD
            return DEFAULT_THRESHOLD
        try:
            value = int(raw)
            if value > 0:
                state["threshold"] = value
                return value
        except ValueError:
            pass
        print("  ! Enter a positive whole number.")


def ask_outlet():
    while True:
        raw = input("Outlet longitude, latitude (e.g. -117.05, 32.15): ").strip()
        parts = [p for p in raw.replace(" ", "").split(",") if p]
        if len(parts) != 2:
            print("  ! Enter two values: lon, lat")
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            print("  ! Those are not numbers.")
            continue
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            print("  ! lon must be -180..180 and lat -90..90.")
            continue
        return lon, lat


def ask_catchment_area(state):
    """Ask for the DEM and outlet coordinates, then delineate the catchment."""
    print("\n--- Catchment area ---")
    dem_path = ask_dem_path(state)

    with rasterio.open(dem_path) as src:
        bounds = src.bounds
        src_crs = src.crs
    print(
        f"  DEM extent: lon {bounds.left:.3f}..{bounds.right:.3f}, "
        f"lat {bounds.bottom:.3f}..{bounds.top:.3f} ({src_crs.to_string()})"
    )

    print("  Enter the outlet point (where the catchment drains).")
    print("  The nearest channel cell is used, so precision is not critical.\n")
    lon, lat = ask_outlet()
    if not (bounds.left <= lon <= bounds.right and bounds.bottom <= lat <= bounds.top):
        print("  ! That point is outside the DEM extent.")

    threshold = ask_threshold(state)

    out_dir = os.path.dirname(os.path.abspath(dem_path))
    out_path = os.path.join(out_dir, "catchment.gpkg")

    print("  Processing...")
    result = delineate_catchment(dem_path, lon, lat, out_path, threshold)

    print(f"\n  Outlet given:    {lon:.5f}, {lat:.5f}")
    print(
        f"  Snapped to:      {result['snapped_lon']:.5f}, {result['snapped_lat']:.5f}"
        f"  ({result['snap_dist_m']:,.0f} m away)"
    )
    print(f"  Threshold:       {result['threshold']:,} cells")
    print(f"  Catchment area:  {result['area_km2']:,.2f} km2")
    for warning in result["warnings"]:
        print(f"  ! {warning}")
    print(f"  Saved:           {result['path']}")
    return result["path"]


def ask_stream_network(state):
    """Ask for the DEM, then extract the stream network."""
    print("\n--- Stream network ---")
    dem_path = ask_dem_path(state)
    threshold = ask_threshold(state)

    out_dir = os.path.dirname(os.path.abspath(dem_path))
    out_path = os.path.join(out_dir, "streams.gpkg")

    print("  Processing...")
    path = dem_to_streams(dem_path, out_path, threshold)
    print(f"  Saved: {path}")
    return path


MENU = """
=================== DEM Hydrology ===================
  1) Stream network
  2) Catchment area
  3) Both
  0) Quit
======================================================
"""


def main():
    state = {}  # remembers the DEM path and threshold between steps
    while True:
        print(MENU)
        try:
            choice = input("Choose an option: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        try:
            if choice == "1":
                ask_stream_network(state)
            elif choice == "2":
                ask_catchment_area(state)
            elif choice == "3":
                ask_stream_network(state)
                ask_catchment_area(state)
            elif choice in {"0", "q", "Q"}:
                print("Bye.")
                break
            else:
                print("  ! Invalid choice.")
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        except Exception as exc:
            print(f"  ! Error: {exc}")


if __name__ == "__main__":
    main()
