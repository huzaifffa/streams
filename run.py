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
from rasterio.warp import calculate_default_transform, reproject, Resampling
from pysheds.grid import Grid


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


def dem_to_streams(dem_path, output_path, accumulation_threshold=1000):
    """
    Extract a stream network (vector lines) from a DEM.

    dem_path : input DEM raster (GeoTIFF)
    output_path : output vector file (.gpkg recommended, .shp or .geojson also work)
    accumulation_threshold : minimum upstream cells for a cell to be a stream
    Returns output_path.
    """
    with tempfile.TemporaryDirectory() as tmp:
        proj_path, src_crs = _reproject_to_utm(dem_path, os.path.join(tmp, "dem_utm.tif"))

        grid = Grid.from_raster(proj_path)
        dem = grid.read_raster(proj_path)

        # Condition the DEM
        pit_filled = grid.fill_pits(dem)
        flooded = grid.fill_depressions(pit_filled)
        inflated = grid.resolve_flats(flooded)

        # Flow direction and accumulation
        fdir = grid.flowdir(inflated)
        acc = grid.accumulation(fdir)

        # Extract stream network
        branches = grid.extract_river_network(fdir, acc > accumulation_threshold)

        if not branches["features"]:
            raise ValueError("No streams found. Try lowering accumulation_threshold.")

        gdf = gpd.GeoDataFrame.from_features(branches["features"], crs=grid.crs)

    # Return streams in the original DEM's CRS
    if src_crs is not None and gdf.crs != src_crs:
        gdf = gdf.to_crs(src_crs)

    ext = output_path.lower()
    if ext.endswith(".geojson"):
        gdf.to_file(output_path, driver="GeoJSON")
    elif ext.endswith(".shp"):
        gdf.to_file(output_path, driver="ESRI Shapefile")
    else:
        gdf.to_file(output_path, driver="GPKG")

    return output_path


if __name__ == "__main__":
    result = dem_to_streams(
        r"C:/Users/grace/Downloads/gis/DEM/n32_w117_1arc_v3.tif",
        r"C:/Users/grace/Downloads/gis/DEM/streams.gpkg",
        accumulation_threshold=1000,
    )
    print("Saved:", result)