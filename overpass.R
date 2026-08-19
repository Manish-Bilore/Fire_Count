## ---------------------------------------------------------------
## Nominal satellite overpass time (IST)
## MODIS Terra/Aqua + VIIRS SNPP / NOAA-20 / NOAA-21
##
## Works for any admin GeoPackage (single state or all-India) --
## set cfg$gpkg, cfg$layer and cfg$region and everything else,
## including figure titles, filenames and the methods sidecar,
## derives from cfg.
## ---------------------------------------------------------------

library(sf)
library(dplyr)
library(tidyr)
library(readr)
library(ggplot2)
library(viridis)
library(patchwork)

setwd("/media/mb/HDD/Fire_count_version_2")


## ================================================================
## 1. CONFIG
## ================================================================

cfg <- list(
  # --- input -----------------------------------------------------
  gpkg      = "gpkg/uttar_pradesh_admin.gpkg",
  layer     = "district_boundary",
  unit_col  = "DISTRICT",        # district / unit name column
  state_col = NA_character_,     # set to "STATE" if present, else NA
  
  # --- labelling -------------------------------------------------
  region       = "uttar_pradesh",   # filename slug
  region_label = "Uttar Pradesh",   # figure titles
  window       = "April-May 2026",
  
  # --- method ----------------------------------------------------
  lat_correction = TRUE,   # FALSE = longitude-only (original method)
  contour_min    = 10,     # isochrone interval, minutes (Figure 3)
  grid_res       = 0.1,    # degrees, Figure 3 raster
  
  # --- output ----------------------------------------------------
  out_dir = "outputs",
  fig_dir = "figures"
)

dir.create(cfg$out_dir, showWarnings = FALSE, recursive = TRUE)
dir.create(cfg$fig_dir, showWarnings = FALSE, recursive = TRUE)

# columns that identify a unit; drops state_col cleanly when NA
id_cols <- as.character(stats::na.omit(c(cfg$state_col, cfg$unit_col)))

stem <- sprintf("overpass_%s_2026", cfg$region)

# Inspect layers / columns if unsure:
# st_layers(cfg$gpkg)


## ================================================================
## 2. HELPERS
## ================================================================

# Offset in local solar time (hours) between the equator crossing and
# the overpass at latitude phi, from sun-synchronous orbit geometry:
#   sin(phi)  = sin(u) * sin(i)              u = argument of latitude
#   d_alpha   = atan2(cos(i)*sin(u), cos(u)) RA offset from asc. node
# The Earth-rotation term over the ascent is sub-second and dropped.
# Negative for ascending daytime passes in the N hemisphere (earlier
# than the equator crossing); positive for Terra's descending pass.
lst_offset <- function(lat, inc_deg, node) {
  i <- inc_deg * pi / 180
  s <- pmax(pmin(sin(lat * pi / 180) / sin(i), 1), -1)
  u <- asin(s)
  u <- ifelse(node == "descending", pi - u, u)
  
  d <- atan2(cos(i) * sin(u), cos(u)) * 180 / pi
  d <- ifelse(node == "descending", d - 180, d)
  d <- ((d + 180) %% 360) - 180          # wrap to (-180, 180]
  
  d / 15                                  # degrees -> hours
}

decimal_to_time <- function(x) {
  h <- floor(x)
  m <- round((x - h) * 60)
  h <- ifelse(m == 60, h + 1, h)
  m <- ifelse(m == 60, 0, m)
  sprintf("%02d:%02d", h %% 24, m)
}


## ================================================================
## 3. SATELLITE CONSTANTS
## ================================================================
## LT_node = mean local solar time at the node carrying the daytime
## pass:  Terra -> descending (LTDN);  all others -> ascending (LTAN).
##
## Terra and Aqua are both drifting (no station-keeping since 2020 /
## 2021). Values below are interpolated to Apr-May 2026 from NASA
## drift schedules and are approximate to a few minutes. The JPSS
## trio share one orbital plane and one crossing time (13:25); they
## differ in phase, not local time.
##
## Calibrate these against your own FIRMS acq_time -- see section 9.

satellites <- tibble(
  satellite   = c("MODIS Terra", "MODIS Aqua",
                  "VIIRS SNPP", "VIIRS NOAA-20", "VIIRS NOAA-21"),
  node        = c("descending", "ascending", "ascending",
                  "ascending", "ascending"),
  LT_node     = c(8 + 48/60,     # ~08:48, drifting ~2.5 min/month earlier
                  15 + 30/60,    # ~15:30, drifting ~3 min/month later
                  13 + 25/60,    # station-kept
                  13 + 25/60,
                  13 + 25/60),
  inclination = c(98.2, 98.2, 98.7, 98.7, 98.7)
) |>
  mutate(satellite = factor(satellite, levels = satellite))


## ================================================================
## 4. BOUNDARIES + REPRESENTATIVE POINTS
## ================================================================

adm <- st_read(cfg$gpkg, layer = cfg$layer, quiet = TRUE) |>
  st_transform(4326)

stopifnot(cfg$unit_col %in% names(adm))

# Equal-area CRS centred on India. UTM 44N is fine for a single state
# but distorts badly at 68E / 97E, so LAEA covers both cases.
crs_laea <- paste(
  "+proj=laea +lat_0=23 +lon_0=80 +x_0=0 +y_0=0",
  "+datum=WGS84 +units=m +no_defs"
)

# point_on_surface rather than centroid: guarantees the point falls
# inside concave units (coastal, riverine districts)
pts <- adm |>
  st_transform(crs_laea) |>
  st_point_on_surface() |>
  st_transform(4326)

xy <- st_coordinates(pts)

adm <- adm |>
  mutate(
    longitude = xy[, 1],
    latitude  = xy[, 2],
    unit_id   = row_number()
  )

outline <- adm |> st_union() |> st_sf()


## ================================================================
## 5. OVERPASS TABLE
## ================================================================
## No date dimension: the nominal crossing time is fixed within the
## window (Terra/Aqua drift ~5 min over two months). Add a date
## column only if you want to model drift inside the season.

overpass <- expand_grid(
  unit_id   = adm$unit_id,
  satellite = satellites$satellite
) |>
  left_join(
    adm |>
      st_drop_geometry() |>
      select(unit_id, longitude, latitude, any_of(id_cols)),
    by = "unit_id"
  ) |>
  left_join(satellites, by = "satellite") |>
  mutate(
    lat_offset   = if (cfg$lat_correction)
      lst_offset(latitude, inclination, node) else 0,
    LST_local    = LT_node + lat_offset,
    IST_decimal  = (LST_local + 5.5 - longitude / 15) %% 24,
    overpass_IST = decimal_to_time(IST_decimal)
  )

# Sanity check. If lat_offset_range is near zero the latitude
# correction is doing nothing at this extent -- worth saying so in
# the caption rather than leaving it implied.
spread_tbl <- overpass |>
  group_by(satellite) |>
  summarise(
    earliest          = decimal_to_time(min(IST_decimal)),
    latest            = decimal_to_time(max(IST_decimal)),
    spread_min        = round((max(IST_decimal) - min(IST_decimal)) * 60, 1),
    lat_offset_range  = round((max(lat_offset) - min(lat_offset)) * 60, 1),
    .groups = "drop"
  )

print(spread_tbl)

overpass_sf <- overpass |>
  left_join(adm |> select(unit_id), by = "unit_id") |>
  st_as_sf()


## ================================================================
## 6. CAPTION (derived from cfg, so it cannot drift from the method)
## ================================================================

cap <- paste0(
  "Nominal local overpass time (IST), ", cfg$window, ". Derived from ",
  "equator-crossing mean local solar time and unit longitude",
  if (cfg$lat_correction)
    ",\nwith a sun-synchronous geometry correction for unit latitude. "
  else ".\n",
  "Terra and Aqua crossing times are drifting and approximate.\n",
  "Nominal times only: actual acquisition time for a given pixel ",
  "varies with swath position by up to ~50 min."
)


## ================================================================
## 7. FIGURES
## ================================================================

## --- Figure 1: shared colour scale ------------------------------
## Shows absolute Terra / VIIRS / Aqua separation, but compresses
## the within-region gradient.

p_shared <- ggplot(overpass_sf) +
  geom_sf(aes(fill = IST_decimal), colour = NA) +
  geom_sf(data = outline, fill = NA, colour = "grey20", linewidth = 0.2) +
  facet_wrap(~ satellite, ncol = 3) +
  scale_fill_viridis_c(
    name   = "Overpass\n(IST)",
    labels = decimal_to_time,
    option = "turbo"
  ) +
  labs(
    title    = paste("Satellite overpass times across", cfg$region_label),
    subtitle = cfg$window,
    caption  = cap
  ) +
  theme_void(base_size = 11) +
  theme(
    strip.text      = element_text(face = "bold", margin = margin(b = 4)),
    plot.caption    = element_text(hjust = 0, colour = "grey30", size = 8),
    legend.position = "right"
  )

ggsave(file.path(cfg$fig_dir, paste0(stem, "_shared.png")),
       p_shared, width = 11, height = 8, dpi = 300, bg = "white")


## --- Figure 2: independent scale per panel ----------------------
## Recommended: makes the east-west gradient and latitude tilt
## visible within each sensor.

make_panel <- function(sat) {
  ggplot(filter(overpass_sf, satellite == sat)) +
    geom_sf(aes(fill = IST_decimal), colour = NA) +
    geom_sf(data = outline, fill = NA, colour = "grey20", linewidth = 0.2) +
    scale_fill_viridis_c(
      name   = NULL,
      labels = decimal_to_time,
      option = "turbo",
      guide  = guide_colourbar(barwidth = 0.6, barheight = 5)
    ) +
    labs(title = sat) +
    theme_void(base_size = 10) +
    theme(
      plot.title  = element_text(face = "bold", hjust = 0.5, size = 10),
      legend.text = element_text(size = 7)
    )
}

p_free <- wrap_plots(
  lapply(levels(satellites$satellite), make_panel),
  ncol = 3
) +
  plot_annotation(
    title    = paste("Satellite overpass times across", cfg$region_label),
    subtitle = paste0(cfg$window,
                      " -- independent colour scale per panel"),
    caption  = cap,
    theme = theme(
      plot.caption = element_text(hjust = 0, colour = "grey30", size = 8)
    )
  )

ggsave(file.path(cfg$fig_dir, paste0(stem, "_free.png")),
       p_free, width = 12, height = 8, dpi = 300, bg = "white")


## --- Figure 3: continuous field + isochrones --------------------
## The underlying quantity is continuous, so a choropleth is an
## artificial quantisation. This is usually the better figure.

sat_pick <- "VIIRS NOAA-20"
sp <- satellites |> filter(satellite == sat_pick)

bb <- st_bbox(adm)

grd <- expand_grid(
  longitude = seq(bb[["xmin"]], bb[["xmax"]], by = cfg$grid_res),
  latitude  = seq(bb[["ymin"]], bb[["ymax"]], by = cfg$grid_res)
) |>
  mutate(
    lat_offset  = if (cfg$lat_correction)
      lst_offset(latitude, sp$inclination, sp$node) else 0,
    IST_decimal = (sp$LT_node + lat_offset + 5.5 - longitude / 15) %% 24
  )

inside <- st_intersects(
  st_as_sf(grd, coords = c("longitude", "latitude"), crs = 4326),
  outline,
  sparse = FALSE
)[, 1]

grd <- grd[inside, ]

p_field <- ggplot() +
  geom_raster(data = grd, aes(longitude, latitude, fill = IST_decimal)) +
  geom_contour(
    data    = grd,
    aes(longitude, latitude, z = IST_decimal),
    breaks  = seq(0, 24, by = cfg$contour_min / 60),
    colour  = "white", linewidth = 0.25, alpha = 0.7
  ) +
  geom_sf(data = outline, fill = NA, colour = "grey15", linewidth = 0.3) +
  coord_sf(expand = FALSE) +
  scale_fill_viridis_c(
    name = "Overpass\n(IST)", labels = decimal_to_time, option = "turbo"
  ) +
  labs(
    title    = paste(sat_pick, "overpass time --", cfg$region_label),
    subtitle = paste0(cfg$window, " -- isochrones at ",
                      cfg$contour_min, "-minute intervals"),
    caption  = cap
  ) +
  theme_void(base_size = 11) +
  theme(plot.caption = element_text(hjust = 0, colour = "grey30", size = 8))

ggsave(file.path(cfg$fig_dir, paste0(stem, "_field.png")),
       p_field, width = 8, height = 8, dpi = 300, bg = "white")


## ================================================================
## 8. CSV EXPORT
## ================================================================

## --- long: one row per unit x satellite -------------------------

overpass_long <- overpass |>
  mutate(
    lat_offset_min = round(lat_offset * 60, 1),
    LST_local      = round(LST_local, 4),
    IST_decimal    = round(IST_decimal, 4),
    longitude      = round(longitude, 5),
    latitude       = round(latitude, 5)
  ) |>
  select(
    any_of(id_cols),
    longitude, latitude,
    satellite, node, LT_node, inclination,
    lat_offset_min, LST_local, IST_decimal, overpass_IST
  ) |>
  arrange(satellite, .data[[cfg$unit_col]])

write_csv(overpass_long,
          file.path(cfg$out_dir, paste0(stem, "_long.csv")))


## --- wide: one row per unit, one column per satellite -----------

overpass_wide <- overpass_long |>
  select(any_of(id_cols), longitude, latitude, satellite, overpass_IST) |>
  pivot_wider(names_from = satellite, values_from = overpass_IST) |>
  arrange(.data[[cfg$unit_col]])

write_csv(overpass_wide,
          file.path(cfg$out_dir, paste0(stem, "_wide.csv")))


## --- per-satellite spread summary -------------------------------

write_csv(spread_tbl,
          file.path(cfg$out_dir, paste0(stem, "_spread.csv")))


## --- methods sidecar, generated from cfg + satellites -----------

writeLines(
  c(
    sprintf("Generated:   %s", format(Sys.time(), "%Y-%m-%d %H:%M:%S %Z")),
    sprintf("Source:      %s :: %s", cfg$gpkg, cfg$layer),
    sprintf("Region:      %s", cfg$region_label),
    sprintf("Window:      %s", cfg$window),
    sprintf("Units:       %d (name column: %s)", nrow(adm), cfg$unit_col),
    sprintf("Point rule:  st_point_on_surface in %s", crs_laea),
    sprintf("Lat. correction: %s",
            if (cfg$lat_correction) "ON (sun-synchronous geometry)" else "OFF"),
    "",
    "IST = LT_node + lat_offset + 5.5 - longitude/15   (mod 24)",
    "",
    "Assumed equator-crossing mean local solar times:",
    with(satellites,
         sprintf("  %-14s %-11s %s   incl %.1f deg",
                 satellite, node, decimal_to_time(LT_node), inclination)),
    "",
    "Terra and Aqua values are interpolated from NASA orbit-drift",
    "schedules and are approximate to a few minutes. The three JPSS",
    "satellites share one orbital plane and therefore one crossing",
    "time; they differ in phase (~50 min apart), not in local time.",
    "",
    "Nominal times only. Actual acquisition time for a given pixel",
    "varies with swath position by up to ~50 min; a point may be",
    "seen on two consecutive orbits ~100 min apart.",
    "",
    "Observed spread by satellite:",
    with(spread_tbl,
         sprintf("  %-14s %s to %s  (%.1f min; lat term %.1f min)",
                 satellite, earliest, latest, spread_min, lat_offset_range)),
    "",
    "Session:",
    sprintf("  R %s | sf %s | dplyr %s | ggplot2 %s",
            getRversion(), packageVersion("sf"),
            packageVersion("dplyr"), packageVersion("ggplot2"))
  ),
  file.path(cfg$out_dir, paste0(stem, "_methods.txt"))
)

message("Wrote ", stem, "_{long,wide,spread}.csv + _methods.txt -> ",
        cfg$out_dir)
message("Wrote ", stem, "_{shared,free,field}.png -> ", cfg$fig_dir)


## ================================================================
## 9. OPTIONAL: calibrate LT_node against your own FIRMS archive
## ================================================================
## acq_time in FIRMS is UTC HHMM. Back out the implied local solar
## crossing time per sensor and feed the medians into `satellites`
## above. This replaces the interpolated Terra/Aqua drift values
## with the actual 2026 ones, and the p10-p90 range gives you the
## real swath-driven spread to quote alongside the nominal map.
#
# sensor_geom <- tibble(
#   satellite   = c("Terra", "Aqua", "N", "1", "2"),   # FIRMS codes
#   node        = c("descending", "ascending", "ascending",
#                   "ascending", "ascending"),
#   inclination = c(98.2, 98.2, 98.7, 98.7, 98.7)
# )
#
# fires <- read_csv("data/firms_2026.csv")
#
# fires |>
#   filter(daynight == "D",
#          acq_date >= as.Date("2026-04-01"),
#          acq_date <= as.Date("2026-05-31")) |>
#   left_join(sensor_geom, by = "satellite") |>
#   mutate(
#     utc_dec         = floor(acq_time / 100) + (acq_time %% 100) / 60,
#     lst_obs         = (utc_dec + longitude / 15) %% 24,
#     lat_off         = lst_offset(latitude, inclination, node),
#     lt_node_implied = lst_obs - lat_off
#   ) |>
#   group_by(satellite) |>
#   summarise(
#     n       = n(),
#     lt_node = decimal_to_time(median(lt_node_implied)),
#     p10     = decimal_to_time(quantile(lt_node_implied, 0.10)),
#     p90     = decimal_to_time(quantile(lt_node_implied, 0.90)),
#     .groups = "drop"
#   )