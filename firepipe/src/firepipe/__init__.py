"""firepipe - a configurable NASA FIRMS agricultural-fire analysis pipeline.

    from firepipe import Config, Pipeline, render

    cfg = Config.from_yaml("configs/igp.yaml")
    df = Pipeline(cfg).run()
    render(cfg, df)
"""

from .config import (
    Config,
    FirmsConfig,
    MaskConfig,
    PlotConfig,
    RegionConfig,
    SeasonConfig,
    SeasonWindow,
    SensorConfig,
)
from .firms import BBox, FirmsClient, FirmsError, ingest_csv
from .masks import build_mask
from .pipeline import Pipeline, detection_density, qc_tables
from .plots import FigureSuite, render
from .plots_simple import SimpleFigureSuite, render_both, render_simple
from .seasons import SeasonCalendar

__version__ = "1.1.0"

__all__ = [
    "Config",
    "FirmsConfig",
    "MaskConfig",
    "PlotConfig",
    "RegionConfig",
    "SeasonConfig",
    "SeasonWindow",
    "SensorConfig",
    "BBox",
    "FirmsClient",
    "FirmsError",
    "ingest_csv",
    "build_mask",
    "Pipeline",
    "detection_density",
    "qc_tables",
    "FigureSuite",
    "render",
    "SimpleFigureSuite",
    "render_simple",
    "render_both",
    "SeasonCalendar",
    "__version__",
]
