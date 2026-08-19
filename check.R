setwd("/media/mb/HDD/Fire_count_version_2")

# Check the data csv being produced against the numbers being published in the figures

library(readr)
library(tidyverse)

# Load the data the the pipeline has produced
df <- read_csv("output_platforms_VIIRS_SNPP_VIIRS_NOAA20_MODIS/up/data/detections_uttar_pradesh.csv")

head(df)
print(colnames(df)) 
print(unique(df$confidence))
print(unique(df$instrument))
print(unique(df$satellite))

# land_cover should be uniformly 40, if not, the mask filter misfired
table(df$land_cover, useNA = "ifany")

# platform mix by year, a sensor entering mid-record fakes a trend
df |> count(season_year, satellite) |> pivot_wider(names_from = satellite, values_from = n)

# compare against pipeline's numbers
df |> filter(instrument == "VIIRS") |> count(season_year, season)
