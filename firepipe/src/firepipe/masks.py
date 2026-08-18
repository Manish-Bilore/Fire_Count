"""Cropland masking backends.

Three interchangeable sources, one interface:

* ``esa_worldcover``      - ESA WorldCover 10 m, class 40 = cropland (v100/2020,
                            v200/2021). Read straight off the public S3 bucket
                            over /vsicurl, or downloaded tile-by-tile.
* ``dynamic_world``       - Google Dynamic World V1 near-real-time land cover,
                            class 4 = crops, reduced over each season window so
                            the mask is contemporaneous with the detections.
* ``precomputed_raster``  - any local categorical raster the user already has.

Sampling is either at the detection centroid (``point``) or as a cropland
fraction within the sensor footprint (``fraction``). Centroid sampling against
a 10 m mask is a coarse test for a 375 m or 1 km detection; fraction sampling
is the defensible option when a reviewer asks about mixed pixels.
"""

from __future__ import annotations

import hashlib
import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .config import MaskConfig

log = logging.getLogger("firepipe.masks")

ESA_S3 = "https://esa-worldcover.s3.eu-central-1.amazonaws.com"

#: GDAL settings that matter a great deal when sampling over /vsicurl. Without
#: these, every point read re-lists the bucket directory and re-reads headers.
#: rasterio is strict about types here: GDAL_CACHEMAX must be an int, not "512".
VSICURL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MULTIPLEX": True,
    "GDAL_HTTP_VERSION": "2",
    "VSI_CACHE": True,
    "VSI_CACHE_SIZE": 100_000_000,
    "GDAL_CACHEMAX": 512,
}
ESA_TILE_DEG = 3

WORLDCOVER_CLASSES = {
    10: "Tree cover",
    20: "Shrubland",
    30: "Grassland",
    40: "Cropland",
    50: "Built-up",
    60: "Bare / sparse vegetation",
    70: "Snow and ice",
    80: "Permanent water bodies",
    90: "Herbaceous wetland",
    95: "Mangroves",
    100: "Moss and lichen",
}

DYNAMIC_WORLD_CLASSES = {
    0: "water",
    1: "trees",
    2: "grass",
    3: "flooded_vegetation",
    4: "crops",
    5: "shrub_and_scrub",
    6: "built",
    7: "bare",
    8: "snow_and_ice",
}


class MaskError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# base
# --------------------------------------------------------------------------


class BaseMask:
    """Interface: return a land-cover class code per detection."""

    def __init__(self, cfg: MaskConfig):
        self.cfg = cfg
        self.cache_dir = Path(cfg.cache_dir).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def classify(self, df: pd.DataFrame) -> pd.Series:
        raise NotImplementedError

    def crop_fraction(self, df: pd.DataFrame) -> pd.Series:
        raise NotImplementedError

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add ``land_cover``/``crop_fraction`` and a boolean ``is_cropland``."""
        out = df.copy()
        if out.empty:
            out["land_cover"] = pd.Series(dtype="float64")
            out["crop_fraction"] = pd.Series(dtype="float64")
            out["is_cropland"] = pd.Series(dtype="bool")
            return out

        out["land_cover"] = self.classify(out)
        if self.cfg.sampling == "fraction":
            out["crop_fraction"] = self.crop_fraction(out)
            out["is_cropland"] = out["crop_fraction"] >= self.cfg.min_crop_fraction
        else:
            out["crop_fraction"] = np.nan
            out["is_cropland"] = out["land_cover"].isin(self.cfg.crop_classes)
        return out

    # shared helper
    def _radius_deg(self, df: pd.DataFrame) -> np.ndarray:
        default = float(np.mean(list(self.cfg.footprint_radius_m.values())))
        metres = (
            df["platform"].map(self.cfg.footprint_radius_m).fillna(default).to_numpy()
        )
        return metres / 111_320.0


class NoMask(BaseMask):
    def classify(self, df: pd.DataFrame) -> pd.Series:
        return pd.Series(np.nan, index=df.index)

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["land_cover"] = np.nan
        out["crop_fraction"] = np.nan
        out["is_cropland"] = True
        return out


# --------------------------------------------------------------------------
# raster-backed masks
# --------------------------------------------------------------------------


class RasterMask(BaseMask):
    """Sampling logic shared by any single-band categorical raster source."""

    def _paths_for(self, df: pd.DataFrame) -> pd.Series:
        """Raster path (local or /vsicurl) serving each detection."""
        raise NotImplementedError

    # -- sample cache ------------------------------------------------------

    def _cache_key(self) -> str:
        """Identity of the mask source, so a different mask cannot reuse it."""
        c = self.cfg
        if c.kind == "esa_worldcover":
            return f"esa_{c.esa_version}_{c.esa_year}"
        return "raster_" + hashlib.sha1(str(c.raster_path).encode()).hexdigest()[:12]

    def _cache_path(self) -> Path:
        return self.cache_dir / f"samples_{self._cache_key()}.parquet"

    def _load_cache(self) -> pd.DataFrame:
        path = self._cache_path()
        if not (self.cfg.cache_samples and path.exists()):
            return pd.DataFrame(columns=["lon", "lat", "land_cover"])
        try:
            return pd.read_parquet(path)
        except Exception as exc:
            log.warning("Ignoring unreadable sample cache %s (%s)", path, exc)
            return pd.DataFrame(columns=["lon", "lat", "land_cover"])

    def _save_cache(self, cached: pd.DataFrame, fresh: pd.DataFrame) -> None:
        if not self.cfg.cache_samples or fresh.empty:
            return
        combined = pd.concat([cached, fresh], ignore_index=True).drop_duplicates(
            subset=["lon", "lat"], keep="last"
        )
        try:
            combined.to_parquet(self._cache_path(), index=False)
            log.info(
                "  sample cache: %d values stored at %s",
                len(combined),
                self._cache_path(),
            )
        except Exception as exc:
            log.warning("Could not write sample cache (%s); continuing", exc)

    def classify(self, df: pd.DataFrame) -> pd.Series:
        import rasterio

        paths = self._paths_for(df)
        out = pd.Series(np.nan, index=df.index, dtype="float64")

        cached = self._load_cache()
        if not cached.empty:
            lookup = pd.MultiIndex.from_arrays([cached["lon"], cached["lat"]])
            wanted = pd.MultiIndex.from_arrays([df["longitude"], df["latitude"]])
            hit = wanted.isin(lookup)
            if hit.any():
                mapped = pd.Series(
                    cached["land_cover"].to_numpy(), index=lookup
                )
                mapped = mapped[~mapped.index.duplicated()]
                out.loc[df.index[hit]] = mapped.reindex(wanted[hit]).to_numpy()
                log.info(
                    "  sample cache: %d of %d detections already classified",
                    int(hit.sum()),
                    len(df),
                )
        todo = df.index[out.isna()]
        if len(todo) == 0:
            return out
        df = df.loc[todo]
        paths = paths.loc[todo]
        fresh_frames: list[pd.DataFrame] = []

        with rasterio.Env(**VSICURL_ENV):
            for path, idx in paths.groupby(paths).groups.items():
                lons = df.loc[idx, "longitude"].to_numpy(dtype="float64")
                lats = df.loc[idx, "latitude"].to_numpy(dtype="float64")

                # Active-fire products report detections at pixel centres, so a
                # multi-year archive revisits identical coordinates many times.
                # Sampling only the distinct ones cuts remote reads sharply.
                #
                # Coordinates are NOT rounded first: at 10 m resolution even a
                # 1 m nudge can cross a pixel edge, which would make the mask
                # depend on the rounding rule rather than on the land cover.
                pairs = np.column_stack([lons, lats])
                uniq, inverse = np.unique(pairs, axis=0, return_inverse=True)
                if len(uniq) < len(pairs):
                    log.info(
                        "  %s: %d detections at %d distinct locations (%.0f%% fewer reads)",
                        Path(str(path)).name,
                        len(pairs),
                        len(uniq),
                        100 * (1 - len(uniq) / len(pairs)),
                    )

                try:
                    with rasterio.open(path) as src:
                        vals = np.array(
                            [v[0] for v in src.sample(map(tuple, uniq))],
                            dtype="float64",
                        )
                        nodata = src.nodata
                except Exception as exc:
                    log.warning("Could not sample %s (%s); those points get NaN", path, exc)
                    continue
                if nodata is not None:
                    vals[vals == nodata] = np.nan
                out.loc[idx] = vals[inverse]
                fresh_frames.append(
                    pd.DataFrame({"lon": uniq[:, 0], "lat": uniq[:, 1], "land_cover": vals})
                )

        self._save_cache(
            cached,
            pd.concat(fresh_frames, ignore_index=True)
            if fresh_frames
            else pd.DataFrame(columns=["lon", "lat", "land_cover"]),
        )

        missing = int(out.isna().sum())
        if missing:
            log.warning(
                "%d of %d detections had no land-cover value (outside mask coverage)",
                missing,
                len(out),
            )
        return out

    def crop_fraction(self, df: pd.DataFrame) -> pd.Series:
        import rasterio
        from rasterio.windows import from_bounds

        import rasterio as _rio

        paths = self._paths_for(df)
        radii = self._radius_deg(df)
        _env = _rio.Env(**VSICURL_ENV)
        _env.__enter__()
        out = pd.Series(np.nan, index=df.index, dtype="float64")
        crop = set(self.cfg.crop_classes)

        for path, idx in paths.groupby(paths).groups.items():
            try:
                src = rasterio.open(path)
            except Exception as exc:
                log.warning("Could not open %s (%s)", path, exc)
                continue
            with src:
                pos = df.index.get_indexer(idx)
                for i, r in zip(idx, radii[pos]):
                    lon = float(df.at[i, "longitude"])
                    lat = float(df.at[i, "latitude"])
                    rlon = r / max(math.cos(math.radians(lat)), 1e-6)
                    try:
                        win = from_bounds(
                            lon - rlon, lat - r, lon + rlon, lat + r, src.transform
                        )
                        block = src.read(1, window=win, boundless=True, fill_value=0)
                    except Exception:
                        continue
                    if block.size == 0:
                        continue
                    out.at[i] = float(np.isin(block, list(crop)).mean())
        _env.__exit__(None, None, None)
        return out


class ESAWorldCoverMask(RasterMask):
    """ESA WorldCover 10 m, served as 3-degree tiles."""

    def __init__(self, cfg: MaskConfig):
        super().__init__(cfg)
        if cfg.esa_version == "v100" and cfg.esa_year != 2020:
            log.warning("WorldCover v100 is the 2020 epoch; forcing esa_year=2020")
            cfg.esa_year = 2020
        if cfg.esa_version == "v200" and cfg.esa_year != 2021:
            log.warning("WorldCover v200 is the 2021 epoch; forcing esa_year=2021")
            cfg.esa_year = 2021
        self._downloaded: dict[str, str] = {}

    @staticmethod
    def tile_id(lon: float, lat: float) -> str:
        lat0 = int(math.floor(lat / ESA_TILE_DEG) * ESA_TILE_DEG)
        lon0 = int(math.floor(lon / ESA_TILE_DEG) * ESA_TILE_DEG)
        ns = "N" if lat0 >= 0 else "S"
        ew = "E" if lon0 >= 0 else "W"
        return f"{ns}{abs(lat0):02d}{ew}{abs(lon0):03d}"

    def tile_url(self, tile: str) -> str:
        v, y = self.cfg.esa_version, self.cfg.esa_year
        return f"{ESA_S3}/{v}/{y}/map/ESA_WorldCover_10m_{y}_{v}_{tile}_Map.tif"

    def _local_tile(self, tile: str) -> str:
        """Download a tile once, then reuse it. Tiles are roughly 1-2 GB."""
        if tile in self._downloaded:
            return self._downloaded[tile]
        v, y = self.cfg.esa_version, self.cfg.esa_year
        dest = self.cache_dir / f"ESA_WorldCover_10m_{y}_{v}_{tile}_Map.tif"
        if not dest.exists():
            import requests

            url = self.tile_url(tile)
            log.info("Downloading WorldCover tile %s (this is a large file)", tile)
            tmp = dest.with_suffix(".part")
            with requests.get(url, stream=True, timeout=600) as r:
                r.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 22):
                        fh.write(chunk)
            tmp.rename(dest)
        self._downloaded[tile] = str(dest)
        return str(dest)

    def _paths_for(self, df: pd.DataFrame) -> pd.Series:
        tiles = [
            self.tile_id(float(lon), float(lat))
            for lon, lat in zip(df["longitude"], df["latitude"])
        ]
        unique = sorted(set(tiles))
        log.info("WorldCover tiles required: %s", ", ".join(unique))
        if self.cfg.esa_remote:
            lookup = {t: f"/vsicurl/{self.tile_url(t)}" for t in unique}
        else:
            lookup = {t: self._local_tile(t) for t in unique}
        return pd.Series([lookup[t] for t in tiles], index=df.index)


class PrecomputedRasterMask(RasterMask):
    def __init__(self, cfg: MaskConfig):
        super().__init__(cfg)
        self.path = str(Path(cfg.raster_path).expanduser())
        if not Path(self.path).exists():
            raise MaskError(f"Precomputed mask raster not found: {self.path}")

    def _paths_for(self, df: pd.DataFrame) -> pd.Series:
        return pd.Series(self.path, index=df.index)


# --------------------------------------------------------------------------
# Dynamic World
# --------------------------------------------------------------------------


class DynamicWorldMask(BaseMask):
    """Season-contemporaneous cropland from Dynamic World V1 via Earth Engine.

    Unlike WorldCover, Dynamic World is a time series, so the mask can be
    matched to the season the detection belongs to instead of a fixed epoch.
    That removes the "2021 land cover applied to 2025 fires" objection, at the
    cost of an Earth Engine dependency and a slower, quota-bound sample.
    """

    COLLECTION = "GOOGLE/DYNAMICWORLD/V1"
    CHUNK = 4000

    def __init__(self, cfg: MaskConfig):
        super().__init__(cfg)
        self.ee = self._init_ee()

    def _init_ee(self):
        try:
            import ee  # type: ignore
        except ImportError as exc:
            raise MaskError(
                "Dynamic World needs the Earth Engine client: "
                "pip install earthengine-api, then `earthengine authenticate`."
            ) from exc
        try:
            if self.cfg.dw_project:
                ee.Initialize(project=self.cfg.dw_project)
            else:
                ee.Initialize()
        except Exception as exc:
            raise MaskError(
                "Earth Engine failed to initialise. Run `earthengine authenticate` "
                "and set mask.dw_project to a Cloud project with the Earth Engine "
                f"API enabled. Original error: {exc}"
            ) from exc
        return ee

    def classify(self, df: pd.DataFrame) -> pd.Series:
        ee = self.ee
        out = pd.Series(np.nan, index=df.index, dtype="float64")

        # One composite per season-year keeps the mask contemporaneous.
        if {"season", "season_year"}.issubset(df.columns):
            groups = df.groupby(["season_year", "season"], dropna=False)
        else:
            groups = [((None, None), df)]

        for key, grp in groups:
            dates = pd.to_datetime(grp["acq_date"])
            start = (dates.min() - pd.Timedelta(days=15)).strftime("%Y-%m-%d")
            end = (dates.max() + pd.Timedelta(days=15)).strftime("%Y-%m-%d")
            lon0, lon1 = grp["longitude"].min(), grp["longitude"].max()
            lat0, lat1 = grp["latitude"].min(), grp["latitude"].max()
            region = ee.Geometry.Rectangle([lon0 - 0.1, lat0 - 0.1, lon1 + 0.1, lat1 + 0.1])

            coll = (
                ee.ImageCollection(self.COLLECTION)
                .filterDate(start, end)
                .filterBounds(region)
                .select("label")
            )
            reducer = ee.Reducer.mode() if self.cfg.dw_reducer == "mode" else ee.Reducer.min()
            image = coll.reduce(reducer).rename("label")
            log.info("Dynamic World composite %s: %s to %s (%d points)", key, start, end, len(grp))
            out.loc[grp.index] = self._sample(image, grp)
        return out

    def _sample(self, image, grp: pd.DataFrame) -> np.ndarray:
        ee = self.ee
        vals = np.full(len(grp), np.nan)
        for lo in range(0, len(grp), self.CHUNK):
            chunk = grp.iloc[lo : lo + self.CHUNK]
            feats = [
                ee.Feature(ee.Geometry.Point([float(x), float(y)]), {"i": int(i)})
                for i, (x, y) in enumerate(
                    zip(chunk["longitude"], chunk["latitude"]), start=lo
                )
            ]
            fc = ee.FeatureCollection(feats)
            sampled = image.reduceRegions(
                collection=fc, reducer=ee.Reducer.first(), scale=10
            )
            try:
                info = sampled.getInfo()
            except Exception as exc:
                raise MaskError(
                    f"Earth Engine sampling failed on a chunk of {len(chunk)} points: "
                    f"{exc}. Reduce DynamicWorldMask.CHUNK or export via a task."
                ) from exc
            for f in info["features"]:
                props = f["properties"]
                v = props.get("first")
                if v is not None:
                    vals[props["i"] - lo] = v
        return vals

    def crop_fraction(self, df: pd.DataFrame) -> pd.Series:
        raise MaskError(
            "Fraction sampling is not implemented for Dynamic World. Use "
            "mask.sampling='point', or switch to esa_worldcover for footprint "
            "fractions."
        )


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------


def build_mask(cfg: MaskConfig) -> BaseMask:
    if cfg.kind == "none":
        return NoMask(cfg)
    if cfg.kind == "esa_worldcover":
        return ESAWorldCoverMask(cfg)
    if cfg.kind == "precomputed_raster":
        return PrecomputedRasterMask(cfg)
    if cfg.kind == "dynamic_world":
        return DynamicWorldMask(cfg)
    raise MaskError(f"Unknown mask kind '{cfg.kind}'")


def class_names(cfg: MaskConfig) -> dict[int, str]:
    if cfg.kind == "dynamic_world":
        return DYNAMIC_WORLD_CLASSES
    return WORLDCOVER_CLASSES
