"""Areas of interest: GeoPackage boundaries, CRS handling and admin attribution."""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
from shapely.geometry import box

from .config import RegionConfig
from .firms import BBox

log = logging.getLogger("firepipe.aoi")

WGS84 = "EPSG:4326"


class AOIError(RuntimeError):
    pass


def list_layers(gpkg: str | Path) -> pd.DataFrame:
    """Layer inventory for a GeoPackage: name, geometry type, feature count."""
    p = Path(gpkg).expanduser()
    if not p.exists():
        raise AOIError(f"GeoPackage not found: {p}")
    rows = []
    for name, geom_type in pyogrio.list_layers(p):
        info = pyogrio.read_info(p, layer=name)
        rows.append(
            {
                "layer": name,
                "geometry": geom_type,
                "features": info.get("features"),
                "crs": info.get("crs"),
                "fields": ", ".join(info.get("fields", [])),
            }
        )
    return pd.DataFrame(rows)


def load_layer(
    gpkg: str | Path, layer: str, where: str | None = None
) -> gpd.GeoDataFrame:
    p = Path(gpkg).expanduser()
    if not p.exists():
        raise AOIError(f"GeoPackage not found: {p}")
    available = [n for n, _ in pyogrio.list_layers(p)]
    if layer not in available:
        raise AOIError(
            f"Layer '{layer}' not in {p.name}. Available layers: {available}"
        )
    gdf = gpd.read_file(p, layer=layer)
    gdf = harmonise_crs(gdf, source=f"{p.name}:{layer}")
    if where:
        gdf = gdf.query(where)
        if gdf.empty:
            raise AOIError(f"Filter '{where}' matched no features in {p.name}:{layer}")
    gdf = _fix_geometry(gdf)
    return gdf


def harmonise_crs(gdf: gpd.GeoDataFrame, source: str = "") -> gpd.GeoDataFrame:
    """Reproject to WGS 84, warning loudly about non-WGS84 datums.

    Survey of India products are sometimes issued on Everest 1830. Reprojecting
    without a datum shift leaves offsets of a few hundred metres, which is on
    the order of a VIIRS pixel and enough to move detections between districts.
    """
    if gdf.crs is None:
        log.warning(
            "%s has no CRS; assuming WGS 84. Verify this before trusting district "
            "attribution.",
            source or "layer",
        )
        return gdf.set_crs(WGS84)

    name = (gdf.crs.name or "").lower()
    if "everest" in name or "kalianpur" in name:
        log.warning(
            "%s is on %s (Everest-family datum). Reprojecting to WGS 84; confirm a "
            "datum transformation is applied, otherwise expect a systematic offset "
            "of a few hundred metres.",
            source or "layer",
            gdf.crs.name,
        )
    if gdf.crs.to_string() != WGS84:
        gdf = gdf.to_crs(WGS84)
    return gdf


def _fix_geometry(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    invalid = ~gdf.geometry.is_valid
    if invalid.any():
        log.info("Repairing %d invalid geometries with a zero buffer", int(invalid.sum()))
        gdf.loc[invalid, "geometry"] = gdf.loc[invalid, "geometry"].buffer(0)
    return gdf[~gdf.geometry.is_empty].copy()


class Region:
    """A loaded region: outer boundary plus optional admin subdivisions."""

    def __init__(self, cfg: RegionConfig):
        self.cfg = cfg
        self.name = cfg.name
        self.boundary = load_layer(cfg.gpkg, cfg.boundary_layer)
        self.admin: gpd.GeoDataFrame | None = None
        if cfg.admin_layer:
            try:
                self.admin = load_layer(cfg.gpkg, cfg.admin_layer, cfg.where)
            except AOIError as exc:
                log.warning("%s: no admin layer (%s); district figures skipped", cfg.name, exc)
        if self.admin is not None and cfg.admin_field not in self.admin.columns:
            raise AOIError(
                f"{cfg.name}: field '{cfg.admin_field}' not in layer "
                f"'{cfg.admin_layer}'. Available: {list(self.admin.columns)}"
            )

    @property
    def bbox(self) -> BBox:
        w, s, e, n = self.boundary.total_bounds
        return BBox(float(w), float(s), float(e), float(n)).padded(self.cfg.bbox_pad_deg)

    @property
    def n_admin(self) -> int:
        return 0 if self.admin is None else len(self.admin)

    def dissolved(self) -> gpd.GeoDataFrame:
        geom = self.boundary.union_all() if hasattr(self.boundary, "union_all") else self.boundary.unary_union
        return gpd.GeoDataFrame({"region": [self.name]}, geometry=[geom], crs=WGS84)


def load_regions(cfgs: list[RegionConfig]) -> list[Region]:
    return [Region(c) for c in cfgs]


def union_bbox(regions: list[Region]) -> BBox:
    bb = regions[0].bbox
    for r in regions[1:]:
        bb = bb.union(r.bbox)
    return bb


def bbox_efficiency(regions: list[Region]) -> float:
    """Share of the union query bbox actually occupied by the region polygons.

    Transactions, not bytes, are the scarce FIRMS resource, so a low value is
    usually still worth accepting - the union strategy just returns detections
    that the spatial join then discards. Worth reporting, not worth fixing.
    """
    u = union_bbox(regions)
    u_area = (u.east - u.west) * (u.north - u.south)
    if not u_area:
        return 1.0
    covered = gpd.GeoSeries(
        [r.boundary.union_all() for r in regions], crs=WGS84
    ).union_all()
    return float(min(covered.area / u_area, 1.0))


def points_to_gdf(df: pd.DataFrame) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df["longitude"], df["latitude"]),
        crs=WGS84,
    )


def attach_region(
    df: pd.DataFrame, region: Region, keep_outside: bool = False
) -> pd.DataFrame:
    """Clip detections to a region and label them with their admin unit."""
    if df.empty:
        out = df.copy()
        for col in ("region", "admin_unit", "admin_parent"):
            out[col] = pd.Series(dtype=object)
        return out

    pts = points_to_gdf(df)
    target = region.admin if region.admin is not None else region.boundary
    cols = ["geometry"]
    rename: dict[str, str] = {}
    if region.admin is not None:
        cols.append(region.cfg.admin_field)
        rename[region.cfg.admin_field] = "admin_unit"
        if region.cfg.parent_field and region.cfg.parent_field in region.admin.columns:
            cols.append(region.cfg.parent_field)
            rename[region.cfg.parent_field] = "admin_parent"

    joined = gpd.sjoin(pts, target[cols], how="left", predicate="within")
    joined = joined.rename(columns=rename)
    joined["region"] = region.name

    if "admin_unit" not in joined:
        joined["admin_unit"] = pd.NA
    if "admin_parent" not in joined:
        joined["admin_parent"] = pd.NA

    inside = joined["index_right"].notna()
    n_out = int((~inside).sum())
    if n_out:
        log.info(
            "%s: %d of %d detections fall outside the boundary (bbox overshoot)%s",
            region.name,
            n_out,
            len(joined),
            "" if keep_outside else " - dropped",
        )
    if not keep_outside:
        joined = joined[inside]

    drop = [c for c in ("index_right", "geometry") if c in joined.columns]
    return pd.DataFrame(joined.drop(columns=drop)).reset_index(drop=True)


def admin_area_km2(region: Region) -> pd.DataFrame:
    """Per-admin-unit area in km2, for detection-density figures."""
    if region.admin is None:
        return pd.DataFrame(columns=["admin_unit", "area_km2"])
    g = region.admin.to_crs("EPSG:7755")  # WGS84 / India NSF LCC, equal-ish area
    return pd.DataFrame(
        {
            "admin_unit": region.admin[region.cfg.admin_field].values,
            "area_km2": (g.geometry.area / 1e6).values,
        }
    )


def bbox_geom(bb: BBox) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": ["query_bbox"]},
        geometry=[box(bb.west, bb.south, bb.east, bb.north)],
        crs=WGS84,
    )
