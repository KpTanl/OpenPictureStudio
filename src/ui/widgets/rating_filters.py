"""评分筛选的选项：筛选模式、文案键与默认文案。模式的含义由 ``core.rating_store.matches_rating`` 定义。"""

from __future__ import annotations

RATING_FILTERS: tuple[tuple[str, str, str], ...] = (
    ("all", "rating.filter.all", "All Images"),
    ("5", "rating.filter.5", "5 Stars (★★★★★)"),
    ("4+", "rating.filter.4_plus", "4 Stars & Up (★★★★)"),
    ("3+", "rating.filter.3_plus", "3 Stars & Up (★★★)"),
    ("2+", "rating.filter.2_plus", "2 Stars & Up (★★)"),
    ("1+", "rating.filter.1_plus", "1 Star & Up (★)"),
    ("rated", "rating.filter.rated", "Rated Only"),
    ("unrated", "rating.filter.unrated", "Unrated Only"),
)
