IMAGE_QUALITY_RANGE = (1, 100)


# 支持的图片文件扩展名列表
SUPPORTED_FORMATS = [
    "*.jpg", "*.jpeg", "*.jpe",
    "*.png",
    "*.bmp",
    "*.gif",
    "*.webp",
    "*.tiff", "*.tif",
    "*.ico",
    "*.ppm",
    "*.pgm",
    "*.pbm",
    # RAW
    "*.cr2", "*.cr3",
    "*.nef", "*.nrw",
    "*.arw",
    "*.raf",
    "*.dng",
    "*.orf",
    "*.pef",
    "*.rw2",
]

# RAW 扩展名集合（无通配符）
RAW_EXTENSIONS = {
    ".cr2", ".cr3",
    ".nef", ".nrw",
    ".arw",
    ".raf",
    ".dng",
    ".orf",
    ".pef",
    ".rw2",
}

# 可导出的图片格式（保持不含 RAW）
SUPPORTED_OUTPUT_FORMATS = [
    "*.jpg", "*.jpeg", "*.jpe",
    "*.png",
    "*.bmp",
    "*.webp",
    "*.tiff", "*.tif",
]

# 不带通配符的扩展名列表，用于文件关联注册等场景
SUPPORTED_EXTENSIONS = [fmt.lstrip("*") for fmt in SUPPORTED_FORMATS]

# 用于 QFileDialog 的文件过滤器字符串，格式如 "Images (*.jpg *.png ...)"
SUPPORTED_FORMATS_FILTER = "Images (%s)" % " ".join(SUPPORTED_FORMATS)
SUPPORTED_OUTPUT_FORMATS_FILTER = "Images (%s)" % " ".join(SUPPORTED_OUTPUT_FORMATS)
